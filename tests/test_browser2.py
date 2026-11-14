"""Tests for Forge Browser Items 2-4 + the added requirements:
CDP engine, universal files, multimodal seats, barriers, vault,
verify, research. Offline throughout: the CDP engine is tested
against a fake CDP WebSocket server built on raw sockets in this
file (real handshake, real framing, scripted replies), the vision
seat against a fake Ollama HTTP server, and file/research paths
against a local HTTP fixture server.
"""

import base64
import gzip
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import BrowserError  # noqa: E402
from forge.browser import barriers, media, vault as vault_module  # noqa: E402
from forge.browser import files as files_module  # noqa: E402
from forge.browser._http import http_get, http_get_to_file  # noqa: E402
from forge.browser.cdp import BrowserSession, format_ax_tree  # noqa: E402
from forge.browser.files import extract_pdf_text, fetch_any  # noqa: E402
from forge.browser.research import make_plan, run_research  # noqa: E402
from forge.browser.vault import Vault, fill_from_vault  # noqa: E402
from forge.browser.verify import verify_in_browser  # noqa: E402

FAKE_PAGE_HTML = """<html><head><title>Fake Title</title></head><body>
<h1>Rendered Heading</h1><p>Rendered body text.</p>
<button>Accept all cookies</button></body></html>"""

FAKE_ELEMENTS = json.dumps([
    {"i": 0, "tag": "button", "type": "", "text": "Accept all cookies", "href": "", "name": ""},
    {"i": 1, "tag": "input", "type": "text", "text": "", "href": "", "name": "username"},
    {"i": 2, "tag": "input", "type": "password", "text": "", "href": "", "name": "password"},
])

FAKE_AX_NODES = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Fake"},
     "childIds": ["2"]},
    {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Accept all cookies"}},
]

FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# Counter behind the fake's browser-context ids (see _cdp_reply).
_BROWSER_CONTEXT_IDS = [0]


# ----------------------------------------------------------------------
# Fake CDP WebSocket server (raw sockets, RFC 6455 server side)
# ----------------------------------------------------------------------

def _ws_accept(key):
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_send(conn, text):
    payload = text.encode()
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 65536:
        header.append(126)
        header.extend(len(payload).to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(len(payload).to_bytes(8, "big"))
    conn.sendall(bytes(header) + payload)


def _ws_recv(conn, buffer):
    def need(count):
        while len(buffer) < count:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buffer.extend(chunk)
        out = bytes(buffer[:count])
        del buffer[:count]
        return out
    first, second = need(2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(need(2), "big")
    elif length == 127:
        length = int.from_bytes(need(8), "big")
    mask = need(4) if second & 0x80 else None
    payload = need(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return first & 0x0F, payload


def _cdp_reply(method, expression=""):
    if method == "Target.createTarget":
        return {"targetId": "T1"}
    if method == "Target.createBrowserContext":
        # Every fresh session gets its own context id, so tests can
        # prove two sessions never share one.
        _BROWSER_CONTEXT_IDS[0] += 1
        return {"browserContextId": f"CTX{_BROWSER_CONTEXT_IDS[0]}"}
    if method == "Target.attachToTarget":
        return {"sessionId": "S1"}
    if method in ("Page.enable", "Accessibility.enable", "Target.closeTarget",
                  "Target.disposeBrowserContext"):
        return {}
    if method == "Page.navigate":
        return {"frameId": "F1"}
    if method == "Accessibility.getFullAXTree":
        return {"nodes": FAKE_AX_NODES}
    if method == "Page.captureScreenshot":
        return {"data": base64.b64encode(FAKE_PNG).decode()}
    if method == "Runtime.evaluate":
        if "outerHTML" in expression:
            value = FAKE_PAGE_HTML
        elif "document.title" in expression:
            value = "Fake Title"
        elif "location.href" in expression:
            value = "http://fake.test/page"
        elif "querySelectorAll" in expression:
            value = FAKE_ELEMENTS
        elif "setter.call" in expression or "isContentEditable" in expression:
            value = "typed"
        elif "e.click()" in expression:
            value = "clicked"
        else:
            value = True
        return {"result": {"type": "string", "value": value}}
    return {}


class FakeCDPServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        # Every (method, params) the server answers, in order — the
        # fresh-session tests read this to prove isolation.
        self.calls = []
        # When True, Target.createBrowserContext is refused, like an
        # engine that cannot isolate a fresh page.
        self.refuse_context = False
        self._stopped = False

    def stop(self):
        self._stopped = True
        try:
            self.listener.close()
        except Exception:
            pass

    def run(self):
        # One connection per session, several sessions per server:
        # a fresh-state test opens two sessions against the same
        # fake engine and proves their contexts differ.
        while not self._stopped:
            try:
                conn, _ = self.listener.accept()
            except Exception:
                break
            try:
                self._serve(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            self.listener.close()
        except Exception:
            pass

    def _serve(self, conn):
        buffer = bytearray()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = ""
        for line in request.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n").encode())
        while True:
            opcode, payload = _ws_recv(conn, buffer)
            if opcode == 0x8:
                break
            message = json.loads(payload.decode())
            method = message.get("method", "")
            params = message.get("params") or {}
            self.calls.append((method, params))
            if method == "Target.createBrowserContext" and self.refuse_context:
                _ws_send(conn, json.dumps(
                    {"id": message["id"],
                     "error": {"message": "browser contexts not supported"}}))
                continue
            result = _cdp_reply(method, params.get("expression", ""))
            _ws_send(conn, json.dumps({"id": message["id"], "result": result}))
            if method == "Page.navigate":
                _ws_send(conn, json.dumps(
                    {"method": "Page.loadEventFired", "params": {},
                     "sessionId": "S1"}))


class _VersionHandler(BaseHTTPRequestHandler):
    ws_port = 0

    def do_GET(self):
        body = json.dumps({
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/browser/fake"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CDPSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCDPServer()
        cls.fake.start()
        _VersionHandler.ws_port = cls.fake.port
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
        cls.http_port = cls.http.server_address[1]
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.old_env = os.environ.get("FORGE_CDP_URL")
        os.environ["FORGE_CDP_URL"] = f"http://127.0.0.1:{cls.http_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.fake.stop()
        if cls.old_env is None:
            os.environ.pop("FORGE_CDP_URL", None)
        else:
            os.environ["FORGE_CDP_URL"] = cls.old_env

    def test_session_page_snapshot_screenshot(self):
        with BrowserSession.start(engine="auto") as session:
            page = session.page("http://fake.test/page")
            self.assertTrue(page.rendered)
            self.assertIn("Rendered Heading", page.markdown)
            self.assertEqual(page.title, "Fake Title")
            snapshot = session.ax_snapshot()
            self.assertIn("button: Accept all cookies", snapshot)
            elements = session.elements()
            self.assertEqual(len(elements), 3)
            self.assertTrue(session.click(0))
            outcome = session.fill_login("joshua", "s3cret-value", submit=False)
            self.assertEqual(outcome["filled_fields"], 2)
            self.assertNotIn("s3cret-value", json.dumps(outcome))
            with tempfile.TemporaryDirectory() as tmp:
                shot = os.path.join(tmp, "shot.png")
                session.screenshot(shot)
                with open(shot, "rb") as handle:
                    self.assertEqual(handle.read(), FAKE_PNG)

    def test_ax_tree_format(self):
        lines = format_ax_tree(FAKE_AX_NODES)
        self.assertEqual(lines[0], "- RootWebArea: Fake")
        self.assertEqual(lines[1], "  - button: Accept all cookies")

    def test_screenshot_rejects_non_png(self):
        class StubClient:
            def call(self, method, params=None, timeout=20):
                return {"data": base64.b64encode(b"not a png").decode()}

        session = BrowserSession(StubClient())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BrowserError):
                session.screenshot(os.path.join(tmp, "x.png"))


class _KitesurfVersionHandler(BaseHTTPRequestHandler):
    """Kitesurf's /json/version shape (its README: Puppeteer reads
    the protocol version and WebSocket endpoint from it; its
    VERSION_INFO reports Browser "Kitesurf/0.0.1")."""
    ws_port = 0
    browser = "Kitesurf/0.0.1"

    def do_GET(self):
        body = json.dumps({
            "Browser": self.browser,
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl":
                f"ws://127.0.0.1:{self.ws_port}/devtools/browser",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class KitesurfEngineTests(unittest.TestCase):
    """Item 1 (v0.8): Kitesurf-local as a third engine. The fake
    speaks the same CDP the real engine does (Kitesurf's README:
    /json/version discovery, ws /devtools/browser), so the adapter
    is tested end to end without the real binary."""

    def setUp(self):
        self.fake = FakeCDPServer()
        self.fake.start()
        _KitesurfVersionHandler.ws_port = self.fake.port
        _KitesurfVersionHandler.browser = "Kitesurf/0.0.1"
        self.http = ThreadingHTTPServer(("127.0.0.1", 0),
                                        _KitesurfVersionHandler)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever,
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http_port}"
        self._old_env = {}
        for var in ("FORGE_KITESURF_URL", "FORGE_CDP_URL",
                    "FORGE_LIGHTPANDA_URL"):
            self._old_env[var] = os.environ.pop(var, None)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.fake.stop()
        for var, value in self._old_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def test_default_url_is_wrangler_dev_port(self):
        from forge.browser.cdp import KITESURF_DEFAULT_URL
        self.assertEqual(KITESURF_DEFAULT_URL, "http://127.0.0.1:8787")

    def test_kitesurf_session_renders(self):
        os.environ["FORGE_KITESURF_URL"] = self.url
        with BrowserSession.start(engine="kitesurf") as session:
            self.assertEqual(session.engine_name, "kitesurf")
            page = session.page("http://fake.test/page")
            self.assertTrue(page.rendered)
            self.assertIn("Rendered Heading", page.markdown)

    def test_find_kitesurf_identifies_by_browser_field(self):
        from forge.browser.cdp import find_kitesurf
        os.environ["FORGE_KITESURF_URL"] = self.url
        self.assertEqual(find_kitesurf(), self.url)
        # Something else on the port is not the engine.
        _KitesurfVersionHandler.browser = "Chrome/145.0.0.0"
        try:
            self.assertIsNone(find_kitesurf())
        finally:
            _KitesurfVersionHandler.browser = "Kitesurf/0.0.1"
        # Nothing answering is not the engine either.
        os.environ["FORGE_KITESURF_URL"] = "http://127.0.0.1:1"
        self.assertIsNone(find_kitesurf())

    def test_kitesurf_missing_is_plain_words(self):
        os.environ["FORGE_KITESURF_URL"] = "http://127.0.0.1:1"
        with self.assertRaises(BrowserError) as caught:
            BrowserSession.start(engine="kitesurf")
        self.assertIn("Kitesurf", str(caught.exception))
        self.assertIn("pnpm run dev", str(caught.exception))

    def test_auto_with_kitesurf_url_picks_kitesurf(self):
        os.environ["FORGE_KITESURF_URL"] = self.url
        with BrowserSession.start(engine="auto") as session:
            self.assertEqual(session.engine_name, "kitesurf")

    def test_engines_status_reports_kitesurf(self):
        from forge.browser.engines import engine_status, format_status
        os.environ["FORGE_KITESURF_URL"] = self.url
        status = engine_status()
        self.assertEqual(status["kitesurf_serving"], self.url)
        self.assertIn(self.url, format_status(status))
        os.environ["FORGE_KITESURF_URL"] = "http://127.0.0.1:1"
        self.assertIsNone(engine_status()["kitesurf_serving"])

    def test_cli_render_accepts_engine_flag(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest import mock
        from forge.browser import cli as cli_module
        fake_page = SimpleNamespace(
            title="T", final_url="http://x.example/", markdown="body",
            truncated=False)
        with mock.patch("forge.browser.render.render",
                        return_value=fake_page) as patched:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module.main(
                    ["render", "http://x.example/", "--engine", "kitesurf"])
        self.assertEqual(code, 0)
        self.assertEqual(patched.call_args.kwargs["engine"], "kitesurf")

    def test_cli_rejects_unknown_engine(self):
        import io
        from contextlib import redirect_stderr
        from forge.browser import cli as cli_module
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cli_module.main(["render", "http://x.example/",
                                 "--engine", "netscape"])


class FreshSessionTests(unittest.TestCase):
    """Item 2 (v0.8): a fresh session never shares browser state
    with any earlier run. Against an engine already running, that
    means its own browser context, thrown away on close; a fresh
    launch never attaches to a configured shared engine when it
    can launch instead."""

    def setUp(self):
        self.fake = FakeCDPServer()
        self.fake.start()
        _VersionHandler.ws_port = self.fake.port
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever,
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http_port}"
        self._old_env = {}
        for var in ("FORGE_KITESURF_URL", "FORGE_CDP_URL",
                    "FORGE_LIGHTPANDA_URL"):
            self._old_env[var] = os.environ.pop(var, None)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.fake.stop()
        for var, value in self._old_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _calls(self, method):
        return [params for name, params in self.fake.calls
                if name == method]

    def test_fresh_endpoint_uses_isolated_context(self):
        with BrowserSession.start(engine=self.url, fresh=True) as session:
            self.assertEqual(session.engine_name, "cdp")
            page = session.page("http://fake.test/page")
            self.assertTrue(page.rendered)
            created = self._calls("Target.createBrowserContext")
            self.assertEqual(len(created), 1)
            targets = self._calls("Target.createTarget")
            self.assertEqual(len(targets), 1)
            context_id = targets[0].get("browserContextId")
            self.assertTrue(context_id)
            self.assertTrue(context_id.startswith("CTX"))
        disposed = self._calls("Target.disposeBrowserContext")
        self.assertEqual(len(disposed), 1)
        self.assertEqual(disposed[0].get("browserContextId"), context_id)
        # Disposing the context closes its target; no separate close.
        self.assertEqual(self._calls("Target.closeTarget"), [])

    def test_two_fresh_sessions_get_different_contexts(self):
        contexts = []
        for _ in range(2):
            with BrowserSession.start(engine=self.url,
                                      fresh=True) as session:
                session.page("http://fake.test/page")
            created = self._calls("Target.createBrowserContext")
            contexts.append(
                self._calls("Target.createTarget")[-1]["browserContextId"])
        self.assertEqual(len(created), 2)
        self.assertNotEqual(contexts[0], contexts[1])
        self.assertEqual(len(self._calls("Target.disposeBrowserContext")), 2)

    def test_non_fresh_endpoint_shares_default_context(self):
        with BrowserSession.start(engine=self.url) as session:
            session.page("http://fake.test/page")
        self.assertEqual(self._calls("Target.createBrowserContext"), [])
        self.assertEqual(self._calls("Target.disposeBrowserContext"), [])
        self.assertEqual(len(self._calls("Target.closeTarget")), 1)

    def test_fresh_refused_when_engine_cannot_isolate(self):
        self.fake.refuse_context = True
        with self.assertRaises(BrowserError) as caught:
            BrowserSession.start(engine=self.url, fresh=True)
        self.assertIn("fresh", str(caught.exception))
        self.assertIn("isolated", str(caught.exception))

    def test_fresh_prefers_launch_over_shared_url(self):
        # A configured shared engine is ignored while a launch is
        # possible: the launch is attempted (here /bin/true, which
        # exits at once, so the attempt fails fast naming the
        # launched port — never the shared URL).
        from unittest import mock
        from forge.browser import cdp as cdp_module
        os.environ["FORGE_CDP_URL"] = self.url
        with mock.patch.object(cdp_module, "find_chromium",
                               return_value="/bin/true"):
            with self.assertRaises(BrowserError) as caught:
                BrowserSession.start(engine="auto", fresh=True)
        self.assertNotIn(self.url, str(caught.exception))
        self.assertEqual(self._calls("Target.createBrowserContext"), [])

    def test_fresh_last_resort_attaches_with_isolation(self):
        # Nothing launchable: a fresh run may attach to the
        # configured engine, but only inside its own context.
        from unittest import mock
        from forge.browser import cdp as cdp_module
        os.environ["FORGE_CDP_URL"] = self.url
        with mock.patch.object(cdp_module, "find_chromium",
                               return_value=None), \
             mock.patch.object(cdp_module, "find_lightpanda",
                               return_value=None):
            with BrowserSession.start(engine="auto", fresh=True) as session:
                page = session.page("http://fake.test/page")
                self.assertTrue(page.rendered)
        self.assertEqual(len(self._calls("Target.createBrowserContext")), 1)
        self.assertEqual(len(self._calls("Target.disposeBrowserContext")), 1)

    def test_fresh_kitesurf_uses_isolated_context(self):
        _KitesurfVersionHandler.ws_port = self.fake.port
        kitesurf_http = ThreadingHTTPServer(
            ("127.0.0.1", 0), _KitesurfVersionHandler)
        threading.Thread(target=kitesurf_http.serve_forever,
                         daemon=True).start()
        try:
            os.environ["FORGE_KITESURF_URL"] = (
                f"http://127.0.0.1:{kitesurf_http.server_address[1]}")
            with BrowserSession.start(engine="kitesurf",
                                      fresh=True) as session:
                self.assertEqual(session.engine_name, "kitesurf")
                session.page("http://fake.test/page")
        finally:
            kitesurf_http.shutdown()
            kitesurf_http.server_close()
        self.assertEqual(len(self._calls("Target.createBrowserContext")), 1)
        self.assertEqual(len(self._calls("Target.disposeBrowserContext")), 1)

    def test_verify_starts_fresh(self):
        # End to end through verify_in_browser: the run is rendered
        # AND isolated — the fake saw a context created and thrown
        # away for it.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "index.html"), "w") as handle:
                handle.write("<html><head><title>T</title></head>"
                             "<body><h1>Project Page</h1></body></html>")
            result = verify_in_browser("index.html", tmp, engine=self.url)
        self.assertTrue(result["rendered"])
        self.assertIn("Rendered Heading", result["text"])
        self.assertEqual(len(self._calls("Target.createBrowserContext")), 1)
        self.assertEqual(len(self._calls("Target.disposeBrowserContext")), 1)


# ----------------------------------------------------------------------
# Files / PDF / media fixtures over local HTTP
# ----------------------------------------------------------------------

def _make_pdf():
    stream = zlib.compress(b"BT /F1 24 Tf 100 700 Td (Hello PDF World) Tj ET")
    return (b"%PDF-1.4\n1 0 obj<<>>endobj\n4 0 obj<< /Length "
            + str(len(stream)).encode() + b" >>\nstream\n" + stream
            + b"\nendstream\nendobj\n%%EOF")


class _FileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/big-chunked.bin":
            # No Content-Length: the body ends at connection close.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"x" * 2048)
            return
        if self.path == "/bomb.gz":
            raw = gzip.compress(b"A" * 5000)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path == "/declared-big.bin":
            # Headers first, body 1.5 s later: a client that refuses
            # from Content-Length alone never waits for the body.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "5000")
            self.end_headers()
            time.sleep(1.5)
            try:
                self.wfile.write(b"x" * 5000)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        routes = {
            "/doc.pdf": ("application/pdf", _make_pdf()),
            # A scanned PDF: no text layer at all (no Tj/TJ ops).
            "/scan.pdf": ("application/pdf",
                          b"%PDF-1.4\n1 0 obj<</Length 8>>stream\n"
                          b"xxxxxxxx\nendstream endobj\n"),
            "/pic.png": ("image/png", FAKE_PNG),
            "/data.json": ("application/json", b'{"a": 1}'),
            "/table.csv": ("text/csv", b"name,value\nalpha,1\n"),
            "/song.mp3": ("audio/mpeg", b"not-real-audio"),
            "/big.bin": ("application/octet-stream", b"x" * 2048),
            "/big.txt": ("text/plain", b"y" * 2048),
            # A gate with NO visible barrier phrase: only the markup
            # gives it away (a recaptcha div, a password field).
            "/gated.html": ("text/html",
                            b"<html><head><title>Gated</title></head><body>"
                            b"<div class=\"g-recaptcha\"></div>"
                            b"<form><input type=\"password\" name=\"pw\"></form>"
                            b"<p>Welcome to the article about gears.</p>"
                            b"</body></html>"),
        }
        content_type, body = routes.get(self.path, ("text/plain", b"missing"))
        # W3: honor Range so resume can be tested against a real
        # server — 206 with the rest of the body, 416 when the
        # range starts past the end.
        range_header = self.headers.get("Range")
        status = 200 if self.path in routes else 404
        start = 0
        if range_header and self.path in routes:
            try:
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            except (TypeError, ValueError, IndexError):
                start = 0
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.end_headers()
                return
            if start:
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status == 206:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        self.wfile.write(body[start:])

    def log_message(self, *args):
        pass


class FileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.old_media = os.environ.get("FORGE_BROWSER_MEDIA_DIR")
        os.environ["FORGE_BROWSER_MEDIA_DIR"] = cls.tmp
        media.OLLAMA_URL = "http://127.0.0.1:9"  # no vision seat in tests

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        if cls.old_media is None:
            os.environ.pop("FORGE_BROWSER_MEDIA_DIR", None)
        else:
            os.environ["FORGE_BROWSER_MEDIA_DIR"] = cls.old_media

    def test_pdf_text_extraction(self):
        self.assertIn("Hello PDF World", extract_pdf_text(_make_pdf()))

    def test_pdf_cid_font_text_via_tounicode(self):
        # A CID-keyed font PDF (what Word/Chrome produce): the text
        # lives in glyph codes that only the /ToUnicode CMap decodes.
        cmap = (b"/CIDInit/ProcSet findresource begin begincmap\n"
                b"2 beginbfchar\n<0001> <0048>\n<0002> <0069>\nendbfchar\n"
                b"endcmap")
        content = b"BT /F1 12 Tf <00010002> Tj ET"
        pdf = (b"%PDF-1.4\n"
               b"1 0 obj<</Length " + str(len(cmap)).encode() + b">>stream\n" + cmap + b"\nendstream endobj\n"
               b"2 0 obj<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream endobj\n")
        self.assertEqual(extract_pdf_text(pdf), "Hi")
        result = fetch_any(self.base + "/doc.pdf", allow_local=True)
        self.assertEqual(result.kind, "pdf")
        self.assertIn("Hello PDF World", result.text)

    def test_image_saved_and_honest_note(self):
        result = fetch_any(self.base + "/pic.png", allow_local=True)
        self.assertEqual(result.kind, "image")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertEqual(result.text, "")  # no seat: nothing invented
        self.assertTrue(any("no local vision seat" in n for n in result.notes))

    def test_image_not_described_when_ai_off(self):
        # T4: with the master switch off, fetch still saves the
        # image but never calls the vision seat — and says so.
        from unittest import mock
        from forge.browser import media as media_module
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_SEATS_PATH")
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(
                tmp, "seats.json")
            try:
                seats_module.set_feature("ai", False)
                with mock.patch.object(
                        media_module, "describe_saved_image",
                        side_effect=AssertionError(
                            "described while AI is off")):
                    result = fetch_any(self.base + "/pic.png",
                                       allow_local=True)
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_SEATS_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_SEATS_PATH"] = old
        self.assertEqual(result.kind, "image")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertEqual(result.text, "")
        self.assertTrue(any("AI is off" in n for n in result.notes))

    def test_same_name_downloaded_twice_keeps_both(self):
        # Task #22: the second download gets a uniquified name
        # instead of silently overwriting the first file.
        with tempfile.TemporaryDirectory() as media:
            old = os.environ.get("FORGE_BROWSER_MEDIA_DIR")
            os.environ["FORGE_BROWSER_MEDIA_DIR"] = media
            try:
                first = fetch_any(self.base + "/pic.png", allow_local=True)
                second = fetch_any(self.base + "/pic.png", allow_local=True)
                third = fetch_any(self.base + "/pic.png", allow_local=True)
                names = sorted(os.listdir(media))
                # The isfile checks run here — the temp dir is gone
                # once the block exits.
                for result in (first, second, third):
                    self.assertTrue(os.path.isfile(result.saved_path))
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEDIA_DIR", None)
                else:
                    os.environ["FORGE_BROWSER_MEDIA_DIR"] = old
        self.assertEqual(names, ["pic-1.png", "pic-2.png", "pic.png"])
        self.assertEqual(os.path.basename(first.saved_path), "pic.png")
        self.assertEqual(os.path.basename(second.saved_path), "pic-1.png")
        self.assertEqual(os.path.basename(third.saved_path), "pic-2.png")

    def test_json_and_csv(self):
        result = fetch_any(self.base + "/data.json", allow_local=True)
        self.assertEqual(result.kind, "text")
        self.assertIn('"a": 1', result.text)
        result = fetch_any(self.base + "/table.csv", allow_local=True)
        self.assertIn("| name | value |", result.text)
        self.assertIn("| alpha | 1 |", result.text)

    def test_audio_saved_with_note(self):
        result = fetch_any(self.base + "/song.mp3", allow_local=True)
        self.assertEqual(result.kind, "audio")
        self.assertTrue(os.path.isfile(result.saved_path))

    def test_scanned_pdf_ocr_with_stub_tools(self):
        # F2: with tesseract + pdftoppm present, a scanned PDF is
        # read by OCR instead of reported unreadable. The tools are
        # stubs here (no real tesseract in the sandbox) — they prove
        # the wiring, not the OCR engine.
        import stat as _stat
        from forge.browser import ocr as ocr_module
        bin_dir = tempfile.mkdtemp()
        with open(os.path.join(bin_dir, "pdftoppm"), "w") as handle:
            handle.write("#!/usr/bin/env python3\nimport sys\n"
                         "prefix = sys.argv[-1]\n"
                         "for i in (1, 2):\n"
                         "    open(f\"{prefix}-{i}.png\", \"wb\").write(b\"x\")\n")
        with open(os.path.join(bin_dir, "tesseract"), "w") as handle:
            handle.write("#!/usr/bin/env python3\nimport os, sys\n"
                         "print(\"scanned words from \" + os.path.basename(sys.argv[1]))\n")
        for name in ("pdftoppm", "tesseract"):
            os.chmod(os.path.join(bin_dir, name), 0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old_path
        try:
            self.assertTrue(ocr_module.ocr_available())
            result = fetch_any(self.base + "/scan.pdf", allow_local=True)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(result.kind, "pdf")
        self.assertIn("[page 1]", result.text)
        self.assertIn("scanned words from page-1.png", result.text)
        self.assertTrue(any("read with OCR" in n for n in result.notes))

    def test_scanned_pdf_without_tools_names_the_install(self):
        # F2: no OCR tools — the note says plainly what to install.
        empty = tempfile.mkdtemp()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = empty
        try:
            result = fetch_any(self.base + "/scan.pdf", allow_local=True)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(result.kind, "pdf")
        self.assertEqual(result.text, "")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertTrue(any("tesseract-ocr" in n for n in result.notes))

    def test_gated_page_barriers_from_raw_html(self):
        # The gate is markup-only: no visible phrase says captcha or
        # login, so only detection on the raw HTML can catch it.
        result = fetch_any(self.base + "/gated.html", allow_local=True)
        self.assertEqual(result.kind, "html")
        self.assertIn("Welcome to the article about gears.", result.text)
        kinds = [b["kind"] for b in result.barriers]
        self.assertEqual(kinds, ["captcha", "login"])


class CollisionAuditTests(unittest.TestCase):
    """Task AB: long, percent-encoded, Unicode, mismatched, and
    degenerate filenames are handled — never crashed, mangled, or
    left lying about their content."""

    def _keep(self, url, kind, content_type=""):
        # One _keep_file round-trip in a throwaway media dir;
        # returns (saved basename, the file exists).
        from forge.browser.files import _keep_file
        with tempfile.TemporaryDirectory() as media:
            old = os.environ.get("FORGE_BROWSER_MEDIA_DIR")
            os.environ["FORGE_BROWSER_MEDIA_DIR"] = media
            try:
                fd, tmp = tempfile.mkstemp(dir=media)
                os.close(fd)
                with open(tmp, "wb") as handle:
                    handle.write(b"x")
                path = _keep_file(tmp, url, kind, content_type)
                return os.path.basename(path), os.path.isfile(path)
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEDIA_DIR", None)
                else:
                    os.environ["FORGE_BROWSER_MEDIA_DIR"] = old

    def test_long_name_truncated_not_crashed(self):
        # AB1: the audit's probed crash — a 300-char basename
        # raised raw OSError Errno 36. Now it saves, truncated
        # to the 255-byte limit with the extension kept.
        name, exists = self._keep(
            "http://x.example/" + "a" * 300 + ".pdf",
            "pdf", "application/pdf")
        self.assertTrue(exists)
        self.assertLessEqual(len(name.encode("utf-8")), 255)
        self.assertTrue(name.endswith(".pdf"))

    def test_long_names_uniquify_after_truncation(self):
        # AB1: two long names sharing a 251-byte prefix truncate
        # to the same stem — the second still saves, uniquified,
        # and both fit the limit.
        from forge.browser.files import _keep_file
        with tempfile.TemporaryDirectory() as media:
            old = os.environ.get("FORGE_BROWSER_MEDIA_DIR")
            os.environ["FORGE_BROWSER_MEDIA_DIR"] = media
            try:
                names = []
                for tail in ("one", "two"):
                    fd, tmp = tempfile.mkstemp(dir=media)
                    os.close(fd)
                    with open(tmp, "wb") as handle:
                        handle.write(b"x")
                    path = _keep_file(
                        tmp, "http://x.example/" + "a" * 251 + tail + ".pdf",
                        "pdf", "application/pdf")
                    names.append(os.path.basename(path))
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEDIA_DIR", None)
                else:
                    os.environ["FORGE_BROWSER_MEDIA_DIR"] = old
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertLessEqual(len(name.encode("utf-8")), 255)
            self.assertTrue(name.endswith(".pdf"))

    def test_percent_encoded_name_decoded(self):
        # AB2: %20 becomes a space (then an underscore), %C3%A9
        # becomes é — never my_20report.pdf / caf_C3_A9-menu.pdf.
        name, exists = self._keep(
            "http://x.example/my%20report.pdf", "pdf", "application/pdf")
        self.assertTrue(exists)
        self.assertEqual(name, "my_report.pdf")
        name, exists = self._keep(
            "http://x.example/caf%C3%A9-menu.pdf", "pdf", "application/pdf")
        self.assertTrue(exists)
        self.assertEqual(name, "café-menu.pdf")

    def test_unicode_name_kept(self):
        # AB3: non-ASCII letters survive the sanitizer.
        name, exists = self._keep(
            "http://x.example/café-menu.pdf", "pdf", "application/pdf")
        self.assertTrue(exists)
        self.assertEqual(name, "café-menu.pdf")

    def test_extension_follows_content_kind(self):
        # AB4: the extension must not lie — a .txt URL serving a
        # PDF becomes report.pdf; a .txt URL serving a PNG becomes
        # photo.png. An agreeing extension is kept, and kind
        # "binary" carries no content info to contradict the URL.
        name, _ = self._keep(
            "http://x.example/report.txt", "pdf", "application/pdf")
        self.assertEqual(name, "report.pdf")
        name, _ = self._keep(
            "http://x.example/photo.txt", "image", "image/png")
        self.assertEqual(name, "photo.png")
        name, _ = self._keep(
            "http://x.example/pic.png", "image", "image/png")
        self.assertEqual(name, "pic.png")
        name, _ = self._keep("http://x.example/archive.zip", "binary", "")
        self.assertEqual(name, "archive.zip")

    def test_degenerate_names_fall_back(self):
        # AB5: ".", "..", all-unsafe, and empty basenames become
        # download.<kind> — never ".-1" or "..-1".
        for url in ("http://x.example/.", "http://x.example/..",
                    "http://x.example/!!!.pdf", "http://x.example/"):
            name, exists = self._keep(url, "pdf", "application/pdf")
            self.assertTrue(exists, url)
            self.assertEqual(name, "download.pdf", url)

    def test_filesystem_failure_is_plain_words(self):
        # AB6: an os.link failure that is not a collision (here:
        # the target directory does not exist) raises a
        # plain-words BrowserError, not a raw OSError — and the
        # temp file is cleaned up.
        from forge.browser.files import _link_unique
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, tmp = tempfile.mkstemp(dir=tmpdir)
            os.close(fd)
            with open(tmp, "wb") as handle:
                handle.write(b"x")
            missing = os.path.join(tmpdir, "no-such-dir")
            with self.assertRaises(BrowserError) as caught:
                _link_unique(tmp, missing, "report.pdf")
            self.assertIn("could not save", str(caught.exception))
            self.assertFalse(os.path.exists(tmp))


class OcrTests(unittest.TestCase):
    """F2: OCR for scanned PDFs, via tesseract + pdftoppm."""

    def _stub_bin(self, pdftoppm_ok=True):
        bin_dir = tempfile.mkdtemp()
        pdftoppm = ("#!/usr/bin/env python3\nimport sys\n"
                    "prefix = sys.argv[-1]\n"
                    "for i in (1, 2):\n"
                    "    open(f\"{prefix}-{i}.png\", \"wb\").write(b\"x\")\n"
                    if pdftoppm_ok else
                    "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        with open(os.path.join(bin_dir, "pdftoppm"), "w") as handle:
            handle.write(pdftoppm)
        with open(os.path.join(bin_dir, "tesseract"), "w") as handle:
            handle.write("#!/usr/bin/env python3\nimport os, sys\n"
                         "print(\"words from \" + os.path.basename(sys.argv[1]))\n")
        for name in ("pdftoppm", "tesseract"):
            os.chmod(os.path.join(bin_dir, name), 0o755)
        return bin_dir

    def test_tools_missing_means_no_ocr(self):
        from forge.browser import ocr as ocr_module
        empty = tempfile.mkdtemp()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = empty
        try:
            self.assertEqual(ocr_module.find_ocr_tools(),
                             {"tesseract": None, "pdftoppm": None})
            self.assertFalse(ocr_module.ocr_available())
            self.assertEqual(ocr_module.ocr_pdf("/nonexistent.pdf"), "")
        finally:
            os.environ["PATH"] = old_path

    def test_ocr_pdf_joins_pages_with_markers(self):
        from forge.browser import ocr as ocr_module
        bin_dir = self._stub_bin()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old_path
        try:
            self.assertTrue(ocr_module.ocr_available())
            text = ocr_module.ocr_pdf("/fake/scan.pdf")
        finally:
            os.environ["PATH"] = old_path
        self.assertIn("[page 1]\nwords from page-1.png", text)
        self.assertIn("[page 2]\nwords from page-2.png", text)

    def test_ocr_pdf_empty_when_render_fails(self):
        from forge.browser import ocr as ocr_module
        bin_dir = self._stub_bin(pdftoppm_ok=False)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old_path
        try:
            self.assertEqual(ocr_module.ocr_pdf("/fake/scan.pdf"), "")
        finally:
            os.environ["PATH"] = old_path


class DownloadCapTests(unittest.TestCase):
    """Task #17: no download may fill the machine's memory. Files
    stream to disk with no size cap (Joshua's call, 2026-11-11);
    only reads into memory are capped."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.old_media = os.environ.get("FORGE_BROWSER_MEDIA_DIR")
        os.environ["FORGE_BROWSER_MEDIA_DIR"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        if cls.old_media is None:
            os.environ.pop("FORGE_BROWSER_MEDIA_DIR", None)
        else:
            os.environ["FORGE_BROWSER_MEDIA_DIR"] = cls.old_media

    def test_declared_length_refused_fast(self):
        started = time.monotonic()
        with self.assertRaises(BrowserError) as caught:
            http_get(self.base + "/declared-big.bin", max_bytes=1024, allow_local=True)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("5000", str(caught.exception))
        self.assertIn("1024", str(caught.exception))

    def test_chunked_body_refused_over_cap(self):
        with self.assertRaises(BrowserError):
            http_get(self.base + "/big-chunked.bin", max_bytes=1024, allow_local=True)

    def test_gzip_bomb_refused_on_decompressed_size(self):
        # ~50 bytes on the wire, 5000 bytes out — the cap counts
        # decompressed bytes, so the bomb is refused.
        with self.assertRaises(BrowserError):
            http_get(self.base + "/bomb.gz", max_bytes=1024, allow_local=True)

    def test_get_to_file_caps_and_removes_partial(self):
        path = os.path.join(self.tmp, "partial.bin")
        with self.assertRaises(BrowserError):
            http_get_to_file(self.base + "/big.bin", path, max_bytes=1024, allow_local=True)
        self.assertFalse(os.path.exists(path))
        _url, _headers, size = http_get_to_file(
            self.base + "/big.bin", path, max_bytes=4096, allow_local=True)
        self.assertEqual(size, 2048)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 2048)

    def test_no_file_cap_by_default(self):
        # Joshua's call (2026-11-11): downloads need no cap — his
        # machine has plenty of storage. The default is uncapped;
        # only an explicit max_bytes refuses (see the test above).
        path = os.path.join(self.tmp, "uncapped.bin")
        _url, _headers, size = http_get_to_file(
            self.base + "/big.bin", path, allow_local=True)
        self.assertEqual(size, 2048)
        self.assertTrue(os.path.isfile(path))

    def test_no_file_cap_constant(self):
        from forge.browser import _http
        self.assertFalse(hasattr(_http, "MAX_FILE_BYTES"))

    def test_fetch_any_binary_past_memory_cap(self):
        # A binary over the in-memory cap still downloads — it
        # streams to disk instead of into RAM.
        old = files_module.MAX_BODY_BYTES
        files_module.MAX_BODY_BYTES = 1024
        try:
            result = fetch_any(self.base + "/big.bin", allow_local=True)
        finally:
            files_module.MAX_BODY_BYTES = old
        self.assertEqual(result.kind, "binary")
        self.assertEqual(result.size_bytes, 2048)
        self.assertTrue(os.path.isfile(result.saved_path))

    def test_fetch_any_text_over_memory_cap_saved_not_read(self):
        old = files_module.MAX_BODY_BYTES
        files_module.MAX_BODY_BYTES = 1024
        try:
            result = fetch_any(self.base + "/big.txt", allow_local=True)
        finally:
            files_module.MAX_BODY_BYTES = old
        self.assertEqual(result.kind, "text")
        self.assertEqual(result.text, "")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertTrue(any("too big" in n for n in result.notes))


class _FakeResponse:
    """A canned urlopen result: chunks, then an optional failure."""

    def __init__(self, chunks, fail=None, status=200, headers=None):
        self._chunks = list(chunks)
        self._fail = fail
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return "http://example.org/fake.bin"

    def read(self, size=-1):
        if not self._chunks:
            if self._fail is not None:
                raise self._fail
            return b""
        return self._chunks.pop(0)


class DownloadAuditTests(unittest.TestCase):
    """Audit #17: honest download failures, resume, progress, the
    full-disk guard, the repo's own User-Agent, and stall
    detection."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _patch_urlopen(self, response):
        # Z2 moved the transport behind _http._urlopen (the fenced
        # opener); the fake stands in there now.
        import forge.browser._http as _http_module
        original = _http_module._urlopen
        _http_module._urlopen = lambda request, timeout, allow_local: response
        return original

    def test_mid_download_drop_is_a_network_failure(self):
        # W1/W2: a dropped connection says the connection dropped
        # (never "could not save"), and the partial file is gone.
        import urllib.error as _urlerr
        import urllib.request as _urlreq
        path = os.path.join(self.tmp, "dropped.bin")
        response = _FakeResponse(
            [b"x" * 100],
            fail=_urlerr.URLError("connection dropped"))
        original = self._patch_urlopen(response)
        try:
            with self.assertRaises(BrowserError) as caught:
                http_get_to_file("http://example.org/drop.bin", path)
        finally:
            import forge.browser._http as _http_module
            _http_module._urlopen = original
        self.assertIn("connection dropped", str(caught.exception))
        self.assertNotIn("could not save", str(caught.exception))
        self.assertFalse(os.path.exists(path))

    def test_short_body_is_a_network_failure(self):
        # W1: a body cut short (IncompleteRead) is a network
        # failure too, not a save failure, not a raw traceback.
        import http.client as _http_client
        import urllib.request as _urlreq
        path = os.path.join(self.tmp, "short.bin")
        response = _FakeResponse(
            [b"x" * 100],
            fail=_http_client.IncompleteRead(b"x" * 100, 2048))
        original = self._patch_urlopen(response)
        try:
            with self.assertRaises(BrowserError) as caught:
                http_get_to_file("http://example.org/short.bin", path)
        finally:
            import forge.browser._http as _http_module
            _http_module._urlopen = original
        self.assertIn("connection dropped", str(caught.exception))
        self.assertFalse(os.path.exists(path))

    def test_save_failure_still_says_save(self):
        # W1's other side: a real save failure (the path is a
        # directory) still says the save failed.
        with self.assertRaises(BrowserError) as caught:
            http_get_to_file(self.base + "/big.bin", self.tmp,
                             allow_local=True)
        self.assertIn("could not save", str(caught.exception))

    def test_resume_appends_from_saved_part(self):
        # W3: a 206 appends the rest — the file ends up whole.
        path = os.path.join(self.tmp, "resumed.bin")
        with open(path, "wb") as handle:
            handle.write(b"x" * 1024)
        _url, _headers, size = http_get_to_file(
            self.base + "/big.bin", path, resume=True, allow_local=True)
        self.assertEqual(size, 2048)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 2048)

    def test_resume_restarts_when_server_ignores_range(self):
        # W3: a server that ignores Range restarts the file from
        # zero — the stale partial is discarded, not appended to.
        path = os.path.join(self.tmp, "restarted.bin")
        with open(path, "wb") as handle:
            handle.write(b"y" * 100)
        _url, _headers, size = http_get_to_file(
            self.base + "/big-chunked.bin", path, resume=True,
            allow_local=True)
        self.assertEqual(size, 2048)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 2048)

    def test_resume_refused_keeps_partial(self):
        # W3: a 416 (the saved part does not match) says so in
        # plain words — and the partial survives, so the person
        # can delete it and start over.
        path = os.path.join(self.tmp, "mismatch.bin")
        with open(path, "wb") as handle:
            handle.write(b"x" * 3000)
        with self.assertRaises(BrowserError) as caught:
            http_get_to_file(self.base + "/big.bin", path, resume=True,
                             allow_local=True)
        self.assertIn("refused to resume", str(caught.exception))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.getsize(path), 3000)

    def test_progress_reports_bytes_and_total(self):
        # W4: (bytes so far, total) after each chunk, starting
        # at (0, total).
        path = os.path.join(self.tmp, "progress.bin")
        seen = []
        _url, _headers, size = http_get_to_file(
            self.base + "/big.bin", path, allow_local=True,
            on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(size, 2048)
        self.assertEqual(seen[0], (0, 2048))
        self.assertEqual(seen[-1], (2048, 2048))
        for earlier, later in zip(seen, seen[1:]):
            self.assertLessEqual(earlier[0], later[0])

    def test_disk_guard_stops_cleanly(self):
        # W5: with the floor above free space, the download
        # stops before writing, says the disk is nearly full,
        # and leaves no partial behind.
        old = os.environ.get("FORGE_BROWSER_MIN_FREE_BYTES")
        os.environ["FORGE_BROWSER_MIN_FREE_BYTES"] = str(10 ** 15)
        path = os.path.join(self.tmp, "guarded.bin")
        try:
            with self.assertRaises(BrowserError) as caught:
                http_get_to_file(self.base + "/big.bin", path,
                                 allow_local=True)
        finally:
            if old is None:
                os.environ.pop("FORGE_BROWSER_MIN_FREE_BYTES", None)
            else:
                os.environ["FORGE_BROWSER_MIN_FREE_BYTES"] = old
        self.assertIn("nearly full", str(caught.exception))
        self.assertFalse(os.path.exists(path))

    def test_user_agent_names_this_repo(self):
        # W6: the browser identifies itself, not Code Forge.
        from forge.browser._http import USER_AGENT
        self.assertIn("FORGE-BROWSER-", USER_AGENT)
        self.assertIn("Forge Browser", USER_AGENT)
        self.assertNotIn("CODE-FORGE", USER_AGENT)

    def test_page_refusal_says_page(self):
        # W7: a page fetch over the in-memory cap says "page",
        # not "download".
        with self.assertRaises(BrowserError) as caught:
            http_get(self.base + "/declared-big.bin", max_bytes=1024,
                     allow_local=True)
        self.assertIn("page", str(caught.exception))
        self.assertNotIn("download", str(caught.exception))

    def test_download_refusal_still_says_download(self):
        # W7's other side: a file download over an explicit cap
        # still says "download".
        path = os.path.join(self.tmp, "capped.bin")
        with self.assertRaises(BrowserError) as caught:
            http_get_to_file(self.base + "/big.bin", path, max_bytes=1024,
                             allow_local=True)
        self.assertIn("download", str(caught.exception))

    def test_stalled_download_aborts(self):
        # W8: a dribble (10 bytes, a pause past the window, 10
        # more) aborts as stalled instead of running forever.
        import urllib.request as _urlreq

        class Dribbling(_FakeResponse):
            def __init__(self):
                super().__init__([])
                self._calls = 0

            def read(self, size=-1):
                self._calls += 1
                if self._calls == 1:
                    return b"x" * 10
                if self._calls == 2:
                    time.sleep(0.15)
                    return b"y" * 10
                return b""

        original = self._patch_urlopen(Dribbling())
        try:
            with self.assertRaises(BrowserError) as caught:
                http_get("http://example.org/dribble.bin",
                         stall_window=0.1, stall_min_bytes=1000)
        finally:
            import forge.browser._http as _http_module
            _http_module._urlopen = original
        self.assertIn("stalled", str(caught.exception))

    def test_healthy_download_does_not_trip_stall(self):
        # W8: the same dribble under a realistic floor completes.
        import urllib.request as _urlreq

        class Dribbling(_FakeResponse):
            def __init__(self):
                super().__init__([])
                self._calls = 0

            def read(self, size=-1):
                self._calls += 1
                if self._calls == 1:
                    return b"x" * 10
                if self._calls == 2:
                    time.sleep(0.15)
                    return b"y" * 10
                return b""

        original = self._patch_urlopen(Dribbling())
        try:
            _url, _headers, body = http_get(
                "http://example.org/dribble.bin",
                stall_window=0.1, stall_min_bytes=5)
        finally:
            import forge.browser._http as _http_module
            _http_module._urlopen = original
        self.assertEqual(body, b"x" * 10 + b"y" * 10)


# ----------------------------------------------------------------------
# Media seats against fakes
# ----------------------------------------------------------------------

class _OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"models": [
            {"name": "qwen3-coder:latest"},
            {"name": "eriktonon/bonsai-2-uncensored:2b"}]}).encode()
        self._reply(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if payload.get("images"):
            body = json.dumps({"response": "A fake image description. Text seen: FAKE."}).encode()
        else:
            body = json.dumps({"response": ""}).encode()
        self._reply(body)

    def _reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class MediaSeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.old_url = media.OLLAMA_URL
        media.OLLAMA_URL = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        media.OLLAMA_URL = cls.old_url

    def test_seat_preference_picks_bonsai_vision(self):
        self.assertEqual(media.pick_vision_model(), "eriktonon/bonsai-2-uncensored:2b")

    def test_describe_returns_seat_text(self):
        self.assertIn("fake image description", media.describe_image_bytes(FAKE_PNG).lower())

    def test_transcribe_via_configured_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "fake-whisper.sh")
            with open(script, "w") as handle:
                handle.write('#!/bin/sh\necho "transcribed words here" > "$2/transcript.txt"\n')
            os.chmod(script, 0o755)
            audio = os.path.join(tmp, "a.mp3")
            with open(audio, "wb") as handle:
                handle.write(b"fake")
            old = os.environ.get("FORGE_TRANSCRIBE_CMD")
            os.environ["FORGE_TRANSCRIBE_CMD"] = f"sh {script} {{file}} {{out_dir}}"
            try:
                self.assertEqual(media.transcribe_saved_audio(audio), "transcribed words here")
            finally:
                if old is None:
                    os.environ.pop("FORGE_TRANSCRIBE_CMD", None)
                else:
                    os.environ["FORGE_TRANSCRIBE_CMD"] = old

    def _fake_whisper_cpp(self, bindir, argv_log):
        """A whisper-cli stand-in that records its argv and writes
        a transcript to the -of target."""
        script = os.path.join(bindir, "whisper-cli")
        with open(script, "w") as handle:
            handle.write("#!/bin/sh\n"
                         "out=\"\"\nprev=\"\"\n"
                         "for arg in \"$@\"; do\n"
                         "  if [ \"$prev\" = \"-of\" ]; then out=\"$arg\"; fi\n"
                         "  prev=\"$arg\"\ndone\n"
                         f"printf '%s\\n' \"$@\" > {argv_log}\n"
                         "echo 'hello from whisper' > \"$out.txt\"\n")
        os.chmod(script, 0o755)

    def test_whisper_cpp_gets_model_flag_when_model_exists(self):
        # S1: the model path is passed explicitly with -m.
        from unittest import mock
        from forge.browser import media
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "whisper-bin")
            os.makedirs(bindir)
            argv_log = os.path.join(tmp, "argv.txt")
            self._fake_whisper_cpp(bindir, argv_log)
            model = os.path.join(tmp, "base.en")
            with open(model, "wb") as handle:
                handle.write(b"fake model")
            audio = os.path.join(tmp, "a.wav")
            with open(audio, "wb") as handle:
                handle.write(b"fake audio")
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "FORGE_WHISPER_MODEL_PATH": model,
                   "FORGE_TRANSCRIBE_CMD": ""}
            with mock.patch.dict(os.environ, env):
                text, reason = media.transcribe_saved_audio_detailed(audio)
            self.assertEqual(text, "hello from whisper")
            self.assertIsNone(reason)
            with open(argv_log, encoding="utf-8") as handle:
                argv = handle.read().splitlines()
            self.assertIn("-m", argv)
            self.assertIn(model, argv)

    def test_whisper_cpp_omits_model_flag_when_model_missing(self):
        # S1: with no model file, no -m — whisper.cpp falls back
        # to its own default relative path, as before.
        from unittest import mock
        from forge.browser import media
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "whisper-bin")
            os.makedirs(bindir)
            argv_log = os.path.join(tmp, "argv.txt")
            self._fake_whisper_cpp(bindir, argv_log)
            audio = os.path.join(tmp, "a.wav")
            with open(audio, "wb") as handle:
                handle.write(b"fake audio")
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "FORGE_WHISPER_MODEL_PATH": os.path.join(tmp, "nope.bin"),
                   "FORGE_TRANSCRIBE_CMD": ""}
            with mock.patch.dict(os.environ, env):
                text, reason = media.transcribe_saved_audio_detailed(audio)
            self.assertEqual(text, "hello from whisper")
            self.assertIsNone(reason)
            with open(argv_log, encoding="utf-8") as handle:
                argv = handle.read().splitlines()
            self.assertNotIn("-m", argv)

    def test_transcribe_detailed_reasons(self):
        # S2: no backend, a failing backend, and silence each get
        # their own reason — the dock and CLI can say the right
        # sentence for each.
        import sys
        from unittest import mock
        from forge.browser import media
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "a.wav")
            with open(audio, "wb") as handle:
                handle.write(b"fake audio")
            # No backend anywhere on PATH and no faster-whisper.
            env = {"PATH": tmp, "FORGE_TRANSCRIBE_CMD": ""}
            with mock.patch.dict(os.environ, env):
                with mock.patch.dict(sys.modules, {"faster_whisper": None}):
                    self.assertEqual(
                        media.transcribe_saved_audio_detailed(audio),
                        ("", "no-transcriber"))
            # A backend that exits nonzero.
            env = {"PATH": tmp,
                   "FORGE_TRANSCRIBE_CMD": "false {path} {out_dir}"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    media.transcribe_saved_audio_detailed(audio),
                    ("", "failed"))
            # A backend that runs but hears nothing.
            script = os.path.join(tmp, "quiet.sh")
            with open(script, "w") as handle:
                handle.write("#!/bin/sh\n: > \"$2/transcript.txt\"\n")
            os.chmod(script, 0o755)
            env = {"PATH": tmp,
                   "FORGE_TRANSCRIBE_CMD": f"{script} {{path}} {{out_dir}}"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    media.transcribe_saved_audio_detailed(audio),
                    ("", "no-speech"))

    def test_transcribe_cleans_its_temp_dirs(self):
        # S5: the transcribe temp dirs are removed afterwards —
        # /tmp must not fill with forge-transcribe-* litter.
        import glob
        from unittest import mock
        from forge.browser import media
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "fake.sh")
            with open(script, "w") as handle:
                handle.write("#!/bin/sh\necho hi > \"$2/transcript.txt\"\n")
            os.chmod(script, 0o755)
            audio = os.path.join(tmp, "a.wav")
            with open(audio, "wb") as handle:
                handle.write(b"fake audio")
            pattern = os.path.join(tempfile.gettempdir(),
                                   "forge-transcribe-*")
            before = glob.glob(pattern)
            env = {"FORGE_TRANSCRIBE_CMD": f"{script} {{path}} {{out_dir}}"}
            with mock.patch.dict(os.environ, env):
                media.transcribe_saved_audio(audio)
            self.assertEqual(glob.glob(pattern), before)

    def test_faster_whisper_is_local_only(self):
        # S6: faster-whisper is called with local_files_only=True,
        # so a missing model errors instead of downloading ~150 MB.
        import sys
        import types
        from unittest import mock
        from forge.browser import media
        seen = {}

        class FakeModel:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def transcribe(self, path):
                return [types.SimpleNamespace(text="hello")], None

        fake_module = types.ModuleType("faster_whisper")
        fake_module.WhisperModel = FakeModel
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "a.wav")
            with open(audio, "wb") as handle:
                handle.write(b"fake audio")
            env = {"PATH": tmp, "FORGE_TRANSCRIBE_CMD": ""}
            with mock.patch.dict(os.environ, env):
                with mock.patch.dict(sys.modules,
                                     {"faster_whisper": fake_module}):
                    text = media.transcribe_saved_audio(audio)
        self.assertEqual(text, "hello")
        self.assertIs(seen.get("local_files_only"), True)


# ----------------------------------------------------------------------
# Barriers
# ----------------------------------------------------------------------

class _StubSession:
    def __init__(self):
        self.clicked = []

    def elements(self):
        return [{"i": 0, "tag": "button", "text": "Reject all"},
                {"i": 1, "tag": "button", "text": "Accept all"}]

    def click(self, index):
        self.clicked.append(index)
        return True


class BarrierTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(barriers.detect_barriers("<p>hello</p>"), [])
        kinds = [b["kind"] for b in barriers.detect_barriers(
            '<div class="g-recaptcha"></div><input type="password">')]
        self.assertEqual(kinds, ["captcha", "login"])
        self.assertTrue(barriers.detect_barriers(
            "<p>Subscribe to continue reading</p>")[0]["kind"] == "paywall")

    def test_consent_prefers_reject(self):
        session = _StubSession()
        self.assertEqual(barriers.dismiss_consent(session), "Reject all")
        self.assertEqual(session.clicked, [0])


class _RetrySession:
    """A consent banner whose first button fails to click."""

    def __init__(self):
        self.clicked = []

    def elements(self):
        return [{"i": 0, "tag": "button", "text": "Reject all"},
                {"i": 1, "tag": "button", "text": "Reject all"}]

    def click(self, index):
        if index == 0:
            raise RuntimeError("click missed")
        self.clicked.append(index)
        return True


class _HandlePageSession:
    """A stub CDP session for handle_page: html() changes after
    the age-gate button is clicked; fill_login records its call."""

    def __init__(self, html_before, html_after, buttons):
        self._before = html_before
        self._after = html_after
        self._buttons = buttons
        self._gate_clicked = False
        self.clicked = []
        self.filled = None
        self.navigated = None

    def navigate(self, url):
        self.navigated = url

    def html(self):
        return self._after if self._gate_clicked else self._before

    def elements(self):
        return [{"i": i, "tag": "button", "text": text}
                for i, text in enumerate(self._buttons)]

    def click(self, index):
        self.clicked.append(index)
        self._gate_clicked = True
        return True

    def fill_login(self, username, password, submit=False):
        self.filled = (username, password, submit)
        return {"filled_fields": 2, "submitted": bool(submit)}


class BarrierAuditTests(unittest.TestCase):
    """Audit #19: gates must appear in a gate context — articles
    about gates, login boxes, and loaded scripts do not flag."""

    def test_article_about_gates_flags_nothing(self):
        # Y1/Y8: an article discussing paywalls, CAPTCHAs, age
        # verification, and logins is not itself gated.
        html = ("<html><body><article><p>Paywalls changed online "
                "news. CAPTCHAs try to tell humans from bots, and "
                "this article explains how reCAPTCHA works and why "
                "publishers use a paywall. Age verification laws "
                "are debated in several states. Many sites ask "
                "visitors to log in, and the login form sits in "
                "the header beside the search box.</p></article>"
                "</body></html>")
        self.assertEqual(barriers.detect_barriers(html), [])

    def test_login_box_on_readable_page_flags_nothing(self):
        # Y1/Y8: a password field in the header of a readable
        # article is a login box, not a gate.
        article = "The quick brown fox jumps over the lazy dog. " * 20
        html = ("<html><body><header><form>"
                "<input type=\"password\" name=\"pw\"></form></header>"
                f"<article><p>{article}</p></article></body></html>")
        self.assertEqual(barriers.detect_barriers(html), [])

    def test_login_page_still_flags(self):
        # Y1: a password field on a page too short to be anything
        # but the login form still flags.
        html = ("<html><body><p>Welcome back.</p><form>"
                "<input type=\"password\" name=\"pw\"></form>"
                "</body></html>")
        kinds = [b["kind"] for b in barriers.detect_barriers(html)]
        self.assertEqual(kinds, ["login"])

    def test_script_only_recaptcha_flags_nothing(self):
        # Y1/Y8: a page that only LOADS the reCAPTCHA script
        # (invisible v3, no check shown) is not gated.
        html = ("<html><head><script src=\"https://www.google.com/"
                "recaptcha/api.js\"></script></head><body>"
                "<p>Welcome to the article.</p></body></html>")
        self.assertEqual(barriers.detect_barriers(html), [])
        inline = ("<html><body><script>var x = \"recaptcha\";"
                  "</script><p>Welcome.</p></body></html>")
        self.assertEqual(barriers.detect_barriers(inline), [])

    def test_recaptcha_widget_still_flags(self):
        # Y1: the widget div in the markup is a real gate.
        html = "<div class=\"g-recaptcha\"></div>"
        kinds = [b["kind"] for b in barriers.detect_barriers(html)]
        self.assertEqual(kinds, ["captcha"])
        html = "<div class=\"h-captcha\" data-sitekey=\"x\"></div>"
        kinds = [b["kind"] for b in barriers.detect_barriers(html)]
        self.assertEqual(kinds, ["captcha"])

    def test_js_shell_is_the_known_blind_spot(self):
        # Y8/Y9: a JS app shell has no markers in the raw HTML, so
        # the fetch path reports clear — and a JS-rendered barrier
        # would be missed. The CDP path (handle_page) is the
        # answer for those; this test documents the blind spot.
        html = ("<html><body><div id=\"app\"></div>"
                "<script src=\"/bundle.js\"></script></body></html>")
        self.assertEqual(barriers.detect_barriers(html), [])

    def test_paywall_phrase_still_flags_with_soft_note(self):
        # Y7: the note says "may be" — preview-only was never
        # verified.
        html = "<p>Subscribe to continue reading</p>"
        found = barriers.detect_barriers(html)
        self.assertEqual([b["kind"] for b in found], ["paywall"])
        self.assertIn("may be behind a paywall", found[0]["note"])

    def test_non_english_markers(self):
        # Y5: German CAPTCHA, French paywall.
        html = "<p>Ich bin kein Roboter</p>"
        kinds = [b["kind"] for b in barriers.detect_barriers(html)]
        self.assertEqual(kinds, ["captcha"])
        html = "<p>Abonnez-vous pour continuer à lire</p>"
        kinds = [b["kind"] for b in barriers.detect_barriers(html)]
        self.assertEqual(kinds, ["paywall"])

    def test_barrier_carries_site(self):
        # Y4: the url parameter lands on the barrier as its site.
        html = "<div class=\"g-recaptcha\"></div>"
        found = barriers.detect_barriers(
            html, "https://news.example.org/story")
        self.assertEqual(found[0]["site"], "news.example.org")
        found = barriers.detect_barriers(html)
        self.assertEqual(found[0]["site"], "")

    def test_click_labelled_retries_after_failure(self):
        # Y6: a failed click tries the next matching element.
        session = _RetrySession()
        self.assertEqual(barriers.dismiss_consent(session), "Reject all")
        self.assertEqual(session.clicked, [1])

    def test_handle_page_clears_age_gate(self):
        # Y3: the confirm button is clicked, and the barriers are
        # re-detected after the click.
        before = ("<html><body><p>Confirm you are over 18 to enter."
                  "</p><button>I am over 18</button></body></html>")
        after = ("<html><body><p>Welcome to the article about gears."
                 "</p></body></html>")
        session = _HandlePageSession(before, after, ["I am over 18"])
        result = barriers.handle_page(
            session, "https://example.org/gated")
        self.assertTrue(result["age_gate_cleared"])
        self.assertEqual(result["barriers"], [])
        self.assertFalse(result["needs_human"])
        self.assertEqual(session.clicked, [0])

    def test_handle_page_fills_login_from_vault(self):
        # Y2: a vault entry fills the login — never submitted.
        from forge.browser.vault import Vault
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vault.json")
            vault = Vault.create(path, "correct horse battery")
            vault.set("example.org", "joshua", "s3cret-pass", "")
            html = ("<html><body><p>Sign in to continue</p><form>"
                    "<input type=\"text\" name=\"user\">"
                    "<input type=\"password\" name=\"pw\"></form>"
                    "</body></html>")
            session = _HandlePageSession(html, html, [])
            result = barriers.handle_page(
                session, "https://example.org/login", vault=vault)
            self.assertTrue(result["login_filled"])
            self.assertEqual(session.filled,
                             ("joshua", "s3cret-pass", False))

    def test_handle_page_without_vault_entry_reports_login(self):
        # Y2: no vault entry, no fill — the login still reports.
        from forge.browser.vault import Vault
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vault.json")
            vault = Vault.create(path, "correct horse battery")
            html = ("<html><body><p>Sign in to continue</p><form>"
                    "<input type=\"password\" name=\"pw\"></form>"
                    "</body></html>")
            session = _HandlePageSession(html, html, [])
            result = barriers.handle_page(
                session, "https://example.org/login", vault=vault)
            self.assertFalse(result["login_filled"])
            self.assertEqual([b["kind"] for b in result["barriers"]],
                             ["login"])
            self.assertTrue(result["needs_human"])


# ----------------------------------------------------------------------
# Vault
# ----------------------------------------------------------------------

class VaultTests(unittest.TestCase):
    def test_roundtrip_security_and_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vault.json")
            vault = Vault.create(path, "correct horse battery")
            vault.set("example.com", "joshua", "p@ssw0rd-plain-never", "main login")
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertNotIn(b"p@ssw0rd-plain-never", raw)
            self.assertNotIn(b"joshua", raw)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            reopened = Vault.open(path, "correct horse battery")
            self.assertEqual(reopened.get("example.com")["password"], "p@ssw0rd-plain-never")
            self.assertNotIn("p@ssw0rd", json.dumps(reopened.list_entries()))
            with self.assertRaises(BrowserError):
                Vault.open(path, "wrong passphrase")
            # Tamper: change one character of the encrypted data.
            with open(path) as handle:
                blob = json.load(handle)
            blob["data"] = ("A" if blob["data"][0] != "A" else "B") + blob["data"][1:]
            with open(path, "w") as handle:
                json.dump(blob, handle)
            with self.assertRaises(BrowserError):
                Vault.open(path, "correct horse battery")

    def test_fill_from_vault_hides_password(self):
        class FakeSession:
            def __init__(self):
                self.calls = []

            def fill_login(self, username, password, submit=False):
                self.calls.append((username, password))
                return {"filled_fields": 2, "submitted": submit}

        with tempfile.TemporaryDirectory() as tmp:
            vault = Vault.create(os.path.join(tmp, "v.json"), "long-enough-pass")
            vault.set("site.test", "user1", "topsecret")
            session = FakeSession()
            outcome = fill_from_vault(session, vault, "site.test")
            self.assertEqual(session.calls, [("user1", "topsecret")])
            self.assertNotIn("topsecret", json.dumps(outcome))


# ----------------------------------------------------------------------
# Research + verify
# ----------------------------------------------------------------------

class ResearchTests(unittest.TestCase):
    def test_plan_gate_and_cited_report(self):
        plan = make_plan("Test topic", queries=["q one"])
        with self.assertRaises(BrowserError):
            run_research(plan, approved=False)

        def fake_search(query, max_results=5):
            from forge.browser.search import SearchResult
            return [SearchResult(title="Src", url="http://x.test/a", snippet="")]

        class Found:
            kind = "html"
            title = "Src Title"
            text = "The source says the thing plainly."

        report, sources = run_research(
            plan, approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found())
        self.assertEqual(len(sources), 1)
        self.assertIn("[1] http://x.test/a", report)
        self.assertIn("The source says the thing plainly.", report)

    def test_run_research_reports_progress(self):
        lines = []
        plan = make_plan("Test topic", queries=["q one"])

        def fake_search(query, max_results=5):
            from forge.browser.search import SearchResult
            return [SearchResult(title="Src", url="http://x.test/a", snippet="")]

        class Found:
            kind = "html"
            title = "Src Title"
            text = "The source says the thing plainly."

        report, sources = run_research(
            plan, approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found(),
            on_progress=lines.append)
        self.assertTrue(any("Researching" in line for line in lines))
        self.assertTrue(any("Searched" in line for line in lines))
        self.assertTrue(any("Reading 1 of" in line for line in lines))
        self.assertTrue(any(line.startswith("Done") for line in lines))

    def test_start_research_job_runs_in_background(self):
        import threading
        from forge.browser.research import (
            research_job_running, research_queue, start_research_job)
        started = threading.Event()
        release = threading.Event()

        def fake_search(query, max_results=5):
            from forge.browser.search import SearchResult
            return [SearchResult(title="Src", url="http://x.test/a", snippet="")]

        class Found:
            kind = "html"
            title = "Src Title"
            text = "The source says the thing plainly."

        def fake_fetch(url):
            started.set()
            release.wait(timeout=10)
            return Found()

        done = []
        done_event = threading.Event()

        def on_done(report, sources, error):
            done.append((report, error))
            if len(done) == 2:
                done_event.set()

        topic = start_research_job("Test topic", on_progress=lambda t: None,
                                   on_done=on_done,
                                   search_fn=fake_search, fetch_fn=fake_fetch)
        self.assertEqual(topic, "Test topic")
        self.assertTrue(started.wait(timeout=5))
        # Still running: a second job queues behind it (F1) instead of
        # being refused — strictly sequential, one at a time.
        self.assertEqual(research_job_running(), "Test topic")
        self.assertEqual(start_research_job(
            "Other topic", on_done=on_done,
            search_fn=fake_search, fetch_fn=fake_fetch), "Other topic")
        self.assertEqual(research_queue(), ["Other topic"])
        release.set()
        self.assertTrue(done_event.wait(timeout=10))
        self.assertIn("The source says the thing plainly.", done[0][0])
        self.assertIsNone(done[0][1])
        self.assertIn("Research: Other topic", done[1][0])
        self.assertIsNone(done[1][1])
        self.assertIsNone(research_job_running())
        self.assertEqual(research_queue(), [])

    def test_research_keeps_fetch_side_barriers(self):
        # A fetcher that already detected a captcha in the raw HTML:
        # research must carry it into the source, not lose it.
        plan = make_plan("Test topic", queries=["q one"])

        def fake_search(query, max_results=5):
            from forge.browser.search import SearchResult
            return [SearchResult(title="Src", url="http://x.test/a", snippet="")]

        class Found:
            kind = "html"
            title = "Src Title"
            text = "The source says the thing plainly."
            barriers = [{"kind": "captcha", "needs_human": True, "note": "x"}]

        _report, sources = run_research(
            plan, approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found())
        self.assertEqual([b["kind"] for b in sources[0]["barriers"]],
                         ["captcha"])

    def test_research_text_fallback_and_no_duplicates(self):
        # A fetcher that never ran detection: the visible phrase in
        # the extracted text still catches the login — and a kind
        # found both ways appears exactly once.
        plan = make_plan("Test topic", queries=["q one"])

        def fake_search(query, max_results=5):
            from forge.browser.search import SearchResult
            return [SearchResult(title="Src", url="http://x.test/a", snippet="")]

        class Found:
            kind = "html"
            title = "Src Title"
            text = "Please sign in to continue reading."
            barriers = [{"kind": "login", "needs_human": True, "note": "x"}]

        _report, sources = run_research(
            plan, approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found())
        self.assertEqual([b["kind"] for b in sources[0]["barriers"]],
                         ["login"])

        class PlainFound:
            kind = "html"
            title = "Src Title"
            text = "Please sign in to continue reading."

        _report, sources = run_research(
            plan, approved=True, search_fn=fake_search,
            fetch_fn=lambda url: PlainFound())
        self.assertEqual([b["kind"] for b in sources[0]["barriers"]],
                         ["login"])

    def test_report_to_html(self):
        from forge.browser.research import report_to_html
        report = ("# Research: gears\n\n## Questions\n\n- how do gears work\n\n"
                  "## What the sources say\n\n### [1] Gear Guide\n\n"
                  "The **gears** turn. See [the guide](http://g.example/).\n")
        page = report_to_html(report)
        self.assertIn("<title>Research: gears</title>", page)
        self.assertIn("<h1>Research: gears</h1>", page)
        self.assertIn("<h2>Questions</h2>", page)
        self.assertIn("<li>how do gears work</li>", page)
        self.assertIn("<b>gears</b>", page)
        self.assertIn('<a href="http://g.example/">the guide</a>', page)


class WebUITests(unittest.TestCase):
    def test_resolve_address(self):
        from forge.browser.webui import resolve_address
        self.assertEqual(resolve_address("example.org"), ("url", "https://example.org"))
        self.assertEqual(resolve_address("https://x.dev/a"), ("url", "https://x.dev/a"))
        self.assertEqual(resolve_address("how do gears work"), ("search", "how do gears work"))
        self.assertEqual(resolve_address(""), ("url", "/"))

    def test_search_page_renders_results(self):
        from forge.browser.webui import search_page
        from forge.browser.search import SearchResult
        page = search_page("gears", results=[
            SearchResult("Gear Facts", "https://gears.example/", "All about gears")])
        self.assertIn("Gear Facts", page)
        self.assertIn("https://gears.example/", page)

    def test_stop_closes_the_listening_socket(self):
        # H1: stop() used to shut down the serve loop but leave
        # the socket open until GC (ResourceWarning). It closes
        # it now.
        from forge.browser import webui
        server = webui.LocalPageServer().start()
        sock = server._server.socket
        self.assertNotEqual(sock.fileno(), -1)
        server.stop()
        self.assertEqual(sock.fileno(), -1)

    def test_local_pages_serve(self):
        import urllib.request
        from forge.browser import webui
        original = webui.keyless_search
        webui.keyless_search = lambda q, max_results=10: []
        try:
            server = webui.LocalPageServer().start()
            try:
                home = urllib.request.urlopen(server.home_url, timeout=5).read().decode()
                self.assertIn("FORGE BROWSER", home)
                results = urllib.request.urlopen(
                    server.search_url("gears"), timeout=5).read().decode()
                self.assertIn("No results", results)
            finally:
                server.stop()
        finally:
            webui.keyless_search = original

    def test_compare_export_routes_serve(self):
        import urllib.request
        from forge.browser import compare as compare_module
        from forge.browser import webui
        rows = [{"title": "Gear A", "url": "http://a.example/",
                 "fields": {"price": "$42"}},
                {"title": "Gear B", "url": "http://b.example/",
                 "fields": {"price": "$55"}}]
        compare_module.set_last_comparison(rows, ["price"])
        server = webui.LocalPageServer().start()
        try:
            with urllib.request.urlopen(
                    server.home_url + "compare.csv",
                    timeout=5) as response:
                csv_body = response.read().decode()
                disposition = response.headers.get("Content-Disposition")
            self.assertIn("Field,Gear A,Gear B", csv_body)
            self.assertIn("price,$42,$55", csv_body)
            self.assertIn("comparison.csv", disposition or "")
            md_body = urllib.request.urlopen(
                server.home_url + "compare.md", timeout=5).read().decode()
            self.assertIn("| Gear A (a.example) | Gear B (b.example) |",
                          md_body)
            refresh = urllib.request.urlopen(
                server.home_url + "compare-refresh",
                timeout=5).read().decode()
            self.assertIn("$42", refresh)
            self.assertIn("<table>", refresh)
        finally:
            server.stop()

    def test_history_routes_serve(self):
        # N11: /history lists visits with a search box; the clear
        # route asks first and only wipes on ?confirm=1.
        import os as _os
        import tempfile as _tempfile
        import urllib.request
        from forge.browser import titles as titles_module
        from forge.browser import webui
        with _tempfile.TemporaryDirectory() as tmp:
            old = _os.environ.get("FORGE_BROWSER_HISTORY_PATH")
            _os.environ["FORGE_BROWSER_HISTORY_PATH"] = _os.path.join(
                tmp, "h.json")
            try:
                titles_module.record_visit("https://gears.example/",
                                           "Gear Guide")
                server = webui.LocalPageServer().start()
                try:
                    page = urllib.request.urlopen(
                        server.home_url + "history", timeout=5).read().decode()
                    self.assertIn("Gear Guide", page)
                    self.assertIn("Clear history", page)
                    ask = urllib.request.urlopen(
                        server.home_url + "history-clear",
                        timeout=5).read().decode()
                    self.assertIn("Clear history?", ask)
                    self.assertEqual(len(titles_module.load_history()), 1)
                    done = urllib.request.urlopen(
                        server.home_url + "history-clear?confirm=1",
                        timeout=5).read().decode()
                    self.assertIn("History cleared", done)
                    self.assertEqual(titles_module.load_history(), [])
                finally:
                    server.stop()
            finally:
                if old is None:
                    _os.environ.pop("FORGE_BROWSER_HISTORY_PATH", None)
                else:
                    _os.environ["FORGE_BROWSER_HISTORY_PATH"] = old

    def test_research_routes_serve(self):
        # Q4: /research lists saved reports; /research/<file>
        # renders one; the start page links to the list.
        import os as _os
        import tempfile as _tempfile
        import urllib.request
        from forge.browser import webui
        from forge.browser.research import list_reports, save_report
        with _tempfile.TemporaryDirectory() as tmp:
            old = _os.environ.get("FORGE_BROWSER_RESEARCH_DIR")
            _os.environ["FORGE_BROWSER_RESEARCH_DIR"] = tmp
            try:
                save_report("Gears", "# Research: Gears\n\ntext one")
                server = webui.LocalPageServer().start()
                try:
                    listed = urllib.request.urlopen(
                        server.home_url + "research",
                        timeout=5).read().decode()
                    self.assertIn("Gears", listed)
                    name = list_reports()[0]["name"]
                    one = urllib.request.urlopen(
                        server.home_url + "research/" + name,
                        timeout=5).read().decode()
                    self.assertIn("Research: Gears", one)
                    home = urllib.request.urlopen(
                        server.home_url, timeout=5).read().decode()
                    self.assertIn("/research", home)
                    self.assertIn("Gears", home)
                    # A name that is not a listed report: no file.
                    missing = urllib.request.urlopen(
                        server.home_url + "research/nope.md",
                        timeout=5).read().decode()
                    self.assertIn("No such report", missing)
                finally:
                    server.stop()
            finally:
                if old is None:
                    _os.environ.pop("FORGE_BROWSER_RESEARCH_DIR", None)
                else:
                    _os.environ["FORGE_BROWSER_RESEARCH_DIR"] = old

    def test_start_page_shows_grouped_history(self):
        from forge.browser import titles as titles_module
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_HISTORY_PATH")
            os.environ["FORGE_BROWSER_HISTORY_PATH"] = os.path.join(tmp, "h.json")
            try:
                titles_module.record_visit("http://shop.example/a", "Shop A")
                titles_module.record_visit("http://news.example/b", "News B")
                page = webui.start_page()
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_HISTORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_HISTORY_PATH"] = old
        self.assertIn("Recent", page)
        self.assertIn("shop.example", page)
        self.assertIn("Shop A", page)
        self.assertIn("http://shop.example/a", page)

    def test_start_page_shows_morning_brief(self):
        import json
        from unittest import mock
        from forge.browser import brief as brief_module
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                forge_dir = os.path.join(tmp, ".forge")
                os.makedirs(forge_dir)
                day = brief_module.yesterday_date()
                with open(os.path.join(forge_dir, f"brief-{day}.json"), "w") as handle:
                    json.dump({"date": day, "summary": "You were reading about gears.",
                               "pages": [{"url": "http://gears.example/",
                                          "title": "Gear Guide"}]}, handle)
                page = webui.start_page()
        self.assertIn("Morning brief", page)
        self.assertIn("You were reading about gears.", page)
        self.assertIn("Gear Guide", page)

    def test_start_page_brief_missing_note(self):
        from unittest import mock
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                page = webui.start_page()
        self.assertIn("Morning brief", page)
        self.assertIn("No brief yet", page)

    def test_start_page_shows_all_brief_pages(self):
        # U5: the start page lists every brief page, not the
        # first 6 — it matches the CLI and chat lists.
        import json
        from unittest import mock
        from forge.browser import brief as brief_module
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                forge_dir = os.path.join(tmp, ".forge")
                os.makedirs(forge_dir)
                day = brief_module.yesterday_date()
                pages = [{"url": f"http://p{i}.example/",
                          "title": f"Page {i}"} for i in range(8)]
                with open(os.path.join(forge_dir, f"brief-{day}.json"), "w") as handle:
                    json.dump({"date": day, "summary": None,
                               "pages": pages}, handle)
                page = webui.start_page()
        for i in range(8):
            self.assertIn(f"Page {i}", page)
        self.assertIn("/brief-refresh", page)

    def test_start_page_shows_today_so_far(self):
        # U6: today's pages so far appear alongside yesterday's
        # brief, as a list.
        from unittest import mock
        from forge.browser import titles as titles_module
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "h.json")
            page_path = os.path.join(tmp, "p.json")
            with open(page_path, "w") as handle:
                json.dump({"pages": []}, handle)
            env = {"HOME": tmp,
                   "FORGE_BROWSER_HISTORY_PATH": history_path,
                   "FORGE_BROWSER_PAGE_MEMORY_PATH": page_path}
            with mock.patch.dict(os.environ, env):
                titles_module.record_visit("http://today.example/x",
                                           "Today Page")
                page = webui.start_page()
        self.assertIn("Today so far", page)
        self.assertIn("Today Page", page)

    def test_brief_refresh_route_rebuilds(self):
        # U7: /brief-refresh starts a forced rebuild in the
        # background and says to reload in a minute; with AI off
        # it refuses instead.
        import json as _json
        import time as _time
        import urllib.request
        from unittest import mock
        from forge.browser import brief as brief_module
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            seats_path = os.path.join(tmp, "seats.json")
            with open(seats_path, "w") as handle:
                _json.dump({"ai_enabled": True,
                            "features": {"chat": True}}, handle)
            env = {"HOME": tmp, "FORGE_BROWSER_SEATS_PATH": seats_path}
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(brief_module,
                                       "build_brief") as rebuilt:
                    server = webui.LocalPageServer().start()
                    try:
                        body = urllib.request.urlopen(
                            server.home_url + "brief-refresh",
                            timeout=5).read().decode()
                        self.assertIn("Rebuilding the brief", body)
                        self.assertIn("Reload", body)
                        for _ in range(50):
                            if rebuilt.call_count:
                                break
                            _time.sleep(0.1)
                        self.assertEqual(rebuilt.call_count, 1)
                        self.assertTrue(
                            rebuilt.call_args[1].get("force"))
                    finally:
                        server.stop()
            with open(seats_path, "w") as handle:
                _json.dump({"ai_enabled": False,
                            "features": {"chat": True}}, handle)
            with mock.patch.dict(os.environ, env):
                server = webui.LocalPageServer().start()
                try:
                    body = urllib.request.urlopen(
                        server.home_url + "brief-refresh",
                        timeout=5).read().decode()
                    self.assertIn("AI is off", body)
                finally:
                    server.stop()

    def test_app_without_qt_says_how_to_fix(self):
        try:
            import PySide6  # noqa: F401
            self.skipTest("PySide6 present in this Python")
        except ImportError:
            pass
        from forge.browser.app import run_app
        with self.assertRaises(BrowserError):
            run_app()


class EngineDiscoveryTests(unittest.TestCase):
    def test_open_window_uses_the_running_browser(self):
        # The already-open branch: Forge Browser's window is a real
        # browser on a known port; open takes it to the URL and
        # leaves it open (release, not close). One combined fake
        # serves /json/version AND the WebSocket on a single port,
        # the way Chromium does.
        from forge.browser.cdp import open_browser_window

        class CombinedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if "websocket" in (self.headers.get("Upgrade") or "").lower():
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept",
                                     _ws_accept(self.headers.get("Sec-WebSocket-Key")))
                    self.end_headers()
                    conn, buffer = self.connection, bytearray()
                    while True:
                        opcode, payload = _ws_recv(conn, buffer)
                        if opcode == 0x8:
                            break
                        message = json.loads(payload.decode())
                        method = message.get("method", "")
                        params = message.get("params") or {}
                        result = _cdp_reply(method, params.get("expression", ""))
                        _ws_send(conn, json.dumps({"id": message["id"], "result": result}))
                        if method == "Page.navigate":
                            _ws_send(conn, json.dumps(
                                {"method": "Page.loadEventFired", "params": {},
                                 "sessionId": "S1"}))
                    return
                body = json.dumps({"webSocketDebuggerUrl":
                                   f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/browser/fake"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), CombinedHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = os.environ.get("FORGE_BROWSER_PORT")
        os.environ["FORGE_BROWSER_PORT"] = str(server.server_address[1])
        try:
            result = open_browser_window("https://example.org/")
        finally:
            if old is None:
                os.environ.pop("FORGE_BROWSER_PORT", None)
            else:
                os.environ["FORGE_BROWSER_PORT"] = old
            server.shutdown()
        self.assertFalse(result["started"])
        self.assertEqual(result["url"], "https://example.org/")

    def test_installed_chromium_is_found(self):
        from forge.browser.cdp import find_chromium
        with tempfile.TemporaryDirectory() as home:
            target = os.path.join(home, ".local", "share", "forge-browser",
                                  "chromium", "chrome-linux")
            os.makedirs(target)
            binary = os.path.join(target, "chrome")
            with open(binary, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binary, 0o755)
            old_home = os.environ.get("HOME")
            old_override = os.environ.pop("FORGE_CHROMIUM_PATH", None)
            os.environ["HOME"] = home
            try:
                self.assertEqual(find_chromium(), binary)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_override is not None:
                    os.environ["FORGE_CHROMIUM_PATH"] = old_override

    def test_installed_lightpanda_is_found(self):
        from forge.browser.cdp import find_lightpanda
        with tempfile.TemporaryDirectory() as home:
            target = os.path.join(home, ".local", "share", "forge-browser")
            os.makedirs(target)
            binary = os.path.join(target, "lightpanda")
            with open(binary, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binary, 0o755)
            old_home = os.environ.get("HOME")
            old_bin = os.environ.pop("FORGE_LIGHTPANDA_BIN", None)
            os.environ["HOME"] = home
            try:
                self.assertEqual(find_lightpanda(), binary)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_bin is not None:
                    os.environ["FORGE_LIGHTPANDA_BIN"] = old_bin


class EnginePinTests(unittest.TestCase):
    """Task #21: engine downloads are pinned to a version + hash."""

    def _pins(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        pins = {}
        with open(os.path.join(root, "engine-pins.txt"), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                pins[parts[0]] = parts[1:]
        return pins

    def test_lightpanda_pins_have_versions_and_hashes(self):
        import re
        pins = self._pins()
        for asset in ("lightpanda-x86_64-linux", "lightpanda-aarch64-linux"):
            self.assertIn(asset, pins)
            version, digest = pins[asset]
            self.assertEqual(version, "1.0.3")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_chromium_pin_carries_revision_and_hashes(self):
        # AA1/AA2: the Chromium pin is the revision + the zip's
        # SHA-256 (verifies the download) + the extracted chrome
        # binary's SHA-256 (re-verifies the installed tree).
        import re
        pins = self._pins()
        revision, zip_hash, bin_hash = pins["chromium-linux-x64"]
        self.assertEqual(revision, "1741687")
        self.assertRegex(zip_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(bin_hash, r"^[0-9a-f]{64}$")

    def test_install_verifies_chromium_against_pinned_hash(self):
        # AA1: the download is verified against the SHA-256 from
        # engine-pins.txt, not the GCS metadata MD5 (same source
        # as the bytes). The metadata fetch is gone.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("CHROME_ZIP_HASH", script)
        self.assertIn('echo "$CHROME_ZIP_HASH  $ZIP" | sha256sum -c -', script)
        self.assertNotIn("EXPECTED_MD5", script)
        self.assertNotIn("md5Hash", script)

    def test_installed_chromium_is_reverified(self):
        # AA2: the installed chrome binary is re-verified by hash
        # on --check and on install, matching lp_installed_ok —
        # the REVISION file alone is not trusted.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("chrome_installed_ok()", script)
        self.assertIn('echo "$CHROME_BIN_HASH  $CHROME_BIN" | sha256sum -c -', script)
        self.assertIn('if chrome_installed_ok; then', script)
        self.assertIn('elif chrome_installed_ok; then', script)

    def test_chromium_extraction_is_atomic(self):
        # AA3: extraction goes to a temp dir and swaps into place;
        # the old tree is kept aside until the swap succeeds.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn('EXTRACT_DIR="$CHROME_DIR/.extract"', script)
        self.assertIn('python3 -m zipfile -e "$ZIP" "$EXTRACT_DIR/"', script)
        self.assertIn('mv "$CHROME_DIR/chrome-linux" "$CHROME_DIR/chrome-linux.old"', script)
        self.assertIn('mv "$EXTRACT_DIR/chrome-linux" "$CHROME_DIR/chrome-linux"', script)
        self.assertNotIn('python3 -m zipfile -e "$ZIP" "$CHROME_DIR/"', script)

    def test_curl_downloads_retry_and_resume(self):
        # AA4: every curl download retries transient failures and
        # resumes a broken download instead of restarting.
        root = os.path.join(os.path.dirname(__file__), "..")
        curl_lines = []
        for name in ("install.sh", "browser-setup.sh"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                for line in handle:
                    if "curl " in line and not line.strip().startswith("#"):
                        curl_lines.append((name, line))
        self.assertGreaterEqual(len(curl_lines), 4)
        for name, line in curl_lines:
            self.assertIn("--retry", line, (name, line))
            self.assertIn("--continue-at -", line, (name, line))

    def test_chmod_tightened_to_known_executables(self):
        # AA5: the whole-tree chmod is gone; only the known
        # executables get +x.
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertNotIn('chmod +x "$CHROME_DIR/chrome-linux/"*', script)
        self.assertIn("for _exe in chrome chrome-wrapper chrome_crashpad_handler chrome_sandbox; do",
                      script)

    def test_install_scripts_fetch_no_nightly(self):
        # No unpinned download URL may survive: the nightly tag or
        # the moving LAST_CHANGE revision. (Comments may mention the
        # words — the URLs must not be there.)
        # The versions live in engine-pins.txt (single source of
        # truth); the scripts read them from there, never hard-code
        # a version, and never fetch an unpinned URL.
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("install.sh", "browser-setup.sh"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                script = handle.read()
            self.assertNotIn("releases/download/nightly/", script, name)
            self.assertNotIn("LAST_CHANGE?alt=media", script, name)
            self.assertIn("engine-pins.txt", script, name)
            self.assertIn("sha256sum", script, name)

    def test_engine_status_reports_installed_versions(self):
        from unittest import mock
        from forge.browser import engines as engines_module
        with tempfile.TemporaryDirectory() as home:
            base = os.path.join(home, ".local", "share", "forge-browser")
            os.makedirs(os.path.join(base, "chromium"))
            with open(os.path.join(base, "lightpanda.version"), "w") as handle:
                handle.write("1.0.3 53eb34da09159dcf8f04865dbfe3e63e9a652062684495aa7ced5ddd7c82e8b0\n")
            with open(os.path.join(base, "chromium", "REVISION"), "w") as handle:
                handle.write("1741687\n")
            with mock.patch.dict(os.environ, {"HOME": home}):
                status = engines_module.engine_status()
        self.assertEqual(status["lightpanda_version"], "1.0.3")
        self.assertEqual(status["chromium_revision"], "1741687")

    def test_engine_status_reports_none_when_absent(self):
        from unittest import mock
        from forge.browser import engines as engines_module
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home}):
                status = engines_module.engine_status()
        self.assertIsNone(status["lightpanda_version"])
        self.assertIsNone(status["chromium_revision"])

    def test_engine_status_lists_all_transcription_backends(self):
        # S8: the status names every installed transcriber, not
        # just the `whisper` CLI — the Settings engines panel
        # shows what the machine actually has.
        from unittest import mock
        from forge.browser import engines as engines_module
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "whisper-bin")
            os.makedirs(bindir)
            for name in ("whisper", "whisper-cli"):
                path = os.path.join(bindir, name)
                with open(path, "w") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(path, 0o755)
            env = {"PATH": bindir, "FORGE_TRANSCRIBE_CMD": ""}
            with mock.patch.dict(os.environ, env):
                backends = engines_module.transcription_backends()
                status = engines_module.engine_status()
        self.assertIn("whisper CLI", backends)
        self.assertIn("whisper.cpp (whisper-cli)", backends)
        self.assertIn("whisper CLI", status["transcription"])
        self.assertIn("whisper.cpp (whisper-cli)", status["transcription"])


class InstallerTests(unittest.TestCase):
    """The double-click installer (Release retrofit, 2026-11-12):
    the icon, the installer script, and the launcher that starts it
    must all be present and well-formed."""

    def _root(self):
        return os.path.join(os.path.dirname(__file__), "..")

    def test_icon_is_valid_svg(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(self._root(),
                                     "forge-browser.svg")).getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertEqual(root.get("width"), "512")
        self.assertEqual(root.get("height"), "512")

    def _executables(self):
        # The manifest is the single source of truth for what ships
        # with the exec bit: the snapshot push stores every file as
        # 100644, so the on-disk checkout bit means nothing.
        path = os.path.join(self._root(), "EXECUTABLES.txt")
        names = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
        return names

    def test_install_sh_offers_whisper_model(self):
        # S12: install.sh offers the whisper.cpp model fetch as
        # an opt-in flag, from the stable HuggingFace URL, into
        # the path the transcriber reads.
        with open(os.path.join(self._root(), "install.sh"),
                  encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("--with-whisper", script)
        self.assertIn("FORGE_BROWSER_INSTALL_WHISPER", script)
        self.assertIn("ggml-base.en.bin", script)
        self.assertIn(".forge/whisper/base.en", script)

    def test_installer_is_double_clickable(self):
        # The installer IS the double-click target: a script with a
        # shebang that the Release zip stores with the exec bit.
        # (A .desktop wrapper was tried and dropped — %k expands to
        # empty when a .desktop file is launched from an arbitrary
        # folder, so it cannot find the script next to it. The
        # script finds itself via $0.)
        # The on-disk exec bit is NOT asserted here: a fresh clone
        # has none (snapshot pushes store 100644), and the shipped
        # bit comes from EXECUTABLES.txt via the zip builder.
        path = os.path.join(self._root(), "Install Forge Browser")
        with open(path, encoding="utf-8") as handle:
            first = handle.readline()
        self.assertTrue(first.startswith("#!/bin/sh"))
        self.assertIn("Install Forge Browser", self._executables())

    def test_executables_manifest_matches_scripts(self):
        # Every manifest entry exists and has a shebang, and every
        # script at the tree root with a shebang is in the manifest
        # — so a new script cannot silently ship non-executable.
        root = self._root()
        names = self._executables()
        self.assertTrue(names, "EXECUTABLES.txt is empty")
        for name in names:
            path = os.path.join(root, name)
            self.assertTrue(os.path.isfile(path), f"{name} is missing")
            with open(path, encoding="utf-8") as handle:
                first = handle.readline()
            self.assertTrue(first.startswith("#!"),
                            f"{name} has no shebang")
        for entry in os.listdir(root):
            path = os.path.join(root, entry)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="ignore") as handle:
                first = handle.readline()
            if first.startswith("#!"):
                self.assertIn(entry, names,
                              f"{entry} has a shebang but is not in "
                              "EXECUTABLES.txt")

    def test_install_scripts_parse(self):
        import subprocess
        for name in ("Install Forge Browser", "install.sh", "run.sh",
                     "browser-setup.sh", "test-forge-browser.sh"):
            result = subprocess.run(
                ["sh", "-n", os.path.join(self._root(), name)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_install_pulls_embed_model(self):
        # I1: install.sh pulls nomic-embed-text when Ollama is
        # present (best-effort, never fails the install) and
        # reports only under --check.
        with open(os.path.join(self._root(), "install.sh"),
                  encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("ollama pull nomic-embed-text", script)
        self.assertIn('grep -q "nomic-embed-text"', script)
        self.assertIn("CHECK_ONLY", script)
        # Best-effort: the pull branch must not set FAIL.
        pull_section = script[script.index("Retrieval model"):]
        pull_section = pull_section[:pull_section.index(
            "Optional seats (report only)")]
        self.assertNotIn("FAIL=1", pull_section)

    def test_install_gui_compiles(self):
        import py_compile
        py_compile.compile(os.path.join(self._root(), "install-gui.py"),
                           doraise=True)

    def test_license_file_present(self):
        # MIT, decided by Joshua 2026-11-12 (task B6).
        path = os.path.join(self._root(), "LICENSE")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 Joshua Lofland", text)

    def test_pyside_pins(self):
        # D1: PySide6 is pinned to the verified versions with hashes,
        # and install.sh installs with --require-hashes.
        root = self._root()
        with open(os.path.join(root, "pyside-requirements.txt"),
                  encoding="utf-8") as handle:
            reqs = handle.read()
        self.assertIn("PySide6==6.12.0", reqs)
        self.assertIn("PySide6-WebEngine==6.12.0.140", reqs)
        # Every package line carries at least one sha256 hash.
        packages = [ln.strip() for ln in reqs.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                    and "==" in ln]
        self.assertTrue(packages, "no pinned packages in the requirements")
        for line in packages:
            self.assertIn("--hash=sha256:", line,
                          f"{line} has no hash")
        with open(os.path.join(root, "install.sh"),
                  encoding="utf-8") as handle:
            install = handle.read()
        self.assertIn("--require-hashes", install)
        self.assertIn("pyside-requirements.txt", install)
        self.assertNotIn('"PySide6>=6.6"', install)

    def test_installer_swap_is_safe(self):
        # D3: the old install is moved aside, never deleted before
        # the new one is in place, and restored if the move fails.
        path = os.path.join(self._root(), "Install Forge Browser")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn('rm -rf "$APP_DIR"', text)
        self.assertIn('APP_OLD="$APP_DIR.old"', text)
        self.assertIn('mv "$APP_DIR" "$APP_OLD"', text)
        self.assertIn('mv "$APP_OLD" "$APP_DIR"', text)
        move_in = text.index('mv "$APP_TMP" "$APP_DIR"')
        remove_old = text.index('rm -rf "$APP_OLD"')
        self.assertLess(move_in, remove_old,
                        "the old install must only be removed after "
                        "the new one is in place")

    def _installer_harness(self, tmp, fail_move_in=False):
        """A fake home + fake source dir + stub zenity (and, for
        the failure run, a stub mv that refuses the move-in) so
        the real installer script runs end to end in seconds."""
        import shutil
        import stat as _stat
        home = os.path.join(tmp, "home")
        src = os.path.join(tmp, "src")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(home)
        os.makedirs(src)
        os.makedirs(bindir)
        shutil.copy(os.path.join(self._root(), "Install Forge Browser"),
                    os.path.join(src, "Install Forge Browser"))
        with open(os.path.join(src, "run.sh"), "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(src, "install.sh"), "w") as handle:
            handle.write("#!/bin/sh\n"
                         "cd \"$(dirname \"$0\")\" || exit 1\n"
                         "mkdir -p .venv/bin\n"
                         "printf '#!/bin/sh\\nexit 0\\n' "
                         "> .venv/bin/python\n"
                         "chmod +x .venv/bin/python\n"
                         "echo stub install ok\n"
                         "exit 0\n")
        with open(os.path.join(src, "new-file.txt"), "w") as handle:
            handle.write("from the new install\n")
        zenity = (
            "#!/bin/sh\n"
            "# --question: first call (install?) yes, later calls no.\n"
            "# --progress: wait for the install-done file, then exit.\n"
            "if [ \"$1\" = \"--question\" ]; then\n"
            "    f=\"$0-count\"\n"
            "    n=$(cat \"$f\" 2>/dev/null || echo 0)\n"
            "    n=$((n + 1)); echo \"$n\" > \"$f\"\n"
            "    [ \"$n\" = \"1\" ] && exit 0 || exit 1\n"
            "fi\n"
            "if [ \"$1\" = \"--progress\" ]; then\n"
            "    while [ ! -f \"$HOME/.local/share/forge-browser-app"
            ".install-done\" ]; do sleep 1; done\n"
            "    cat >/dev/null\n"
            "    exit 0\n"
            "fi\n"
            "exit 0\n")
        zpath = os.path.join(bindir, "zenity")
        with open(zpath, "w") as handle:
            handle.write(zenity)
        os.chmod(zpath, 0o755)
        if fail_move_in:
            # The 2nd mv call is the move-in (1st is the old install
            # aside, 3rd is the restore) — refuse it, delegate rest.
            mv_stub = (
                "#!/bin/sh\n"
                "f=\"$0-count\"\n"
                "n=$(cat \"$f\" 2>/dev/null || echo 0)\n"
                "n=$((n + 1)); echo \"$n\" > \"$f\"\n"
                "[ \"$n\" = \"2\" ] && exit 1\n"
                "exec /bin/mv \"$@\"\n")
            mpath = os.path.join(bindir, "mv")
            with open(mpath, "w") as handle:
                handle.write(mv_stub)
            os.chmod(mpath, 0o755)
        env = dict(os.environ)
        env["HOME"] = home
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        return home, src, env

    def _run_installer(self, src, env):
        import subprocess
        return subprocess.run(
            ["sh", os.path.join(src, "Install Forge Browser")],
            capture_output=True, text=True, env=env, timeout=120)

    def test_installer_swap_replaces_old_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, src, env = self._installer_harness(tmp)
            app_dir = os.path.join(home, ".local", "share",
                                   "forge-browser-app")
            os.makedirs(app_dir)
            with open(os.path.join(app_dir, "OLD-MARKER"), "w") as handle:
                handle.write("old install\n")
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(
                os.path.join(app_dir, "OLD-MARKER")),
                "the old install was not replaced")
            self.assertTrue(os.path.isfile(
                os.path.join(app_dir, "new-file.txt")))
            self.assertFalse(os.path.exists(app_dir + ".old"),
                             "the moved-aside old install was not removed")
            self.assertTrue(os.path.isfile(os.path.join(
                home, ".local", "share", "applications",
                "forge-browser.desktop")))

    def test_installer_swap_restores_old_install_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, src, env = self._installer_harness(tmp, fail_move_in=True)
            app_dir = os.path.join(home, ".local", "share",
                                   "forge-browser-app")
            os.makedirs(app_dir)
            with open(os.path.join(app_dir, "OLD-MARKER"), "w") as handle:
                handle.write("old install\n")
            result = self._run_installer(src, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(os.path.isfile(
                os.path.join(app_dir, "OLD-MARKER")),
                "a failed swap left no working install behind")
            self.assertFalse(os.path.exists(
                os.path.join(app_dir, "new-file.txt")))


class DocsTests(unittest.TestCase):
    def test_docs_carry_the_true_test_count(self):
        # B1: one test count everywhere. The suite itself is the
        # canonical count — this test counts it with the same
        # discovery the suite runs under, then asserts the four
        # docs that state it all agree with it.
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        tests_dir = os.path.join(root, "tests")
        # No top_level_dir: tests/ has no __init__.py, so the suite
        # runs with its modules imported top-level, exactly as
        # `python3 -m unittest discover -s tests` does it.
        loader = unittest.TestLoader()
        suite = loader.discover(start_dir=tests_dir)
        count = suite.countTestCases()
        self.assertGreater(count, 0)
        expected = f"{count} checks"

        def read(rel):
            with open(os.path.join(root, rel), encoding="utf-8") as handle:
                return handle.read()

        self.assertIn(expected, read("README.md"))
        self.assertIn(expected, read("BROWSER.md"))
        self.assertIn(expected, read("test-forge-browser.sh"))
        task_list = read("docs/TASK-LIST.md")
        current = [line for line in task_list.splitlines()
                   if line.startswith("**Suite:**")]
        self.assertEqual(len(current), 1,
                         "TASK-LIST.md needs exactly one **Suite:** line")
        self.assertIn(str(count), current[0])


class UiTextRulesTests(unittest.TestCase):
    """A2: U33 — no em-dashes in visible UI text, test-enforced.

    The deep audit counted 48 em-dash string literals in app.py
    (plus 13 in webui.py's served pages and 2 in compare.py's
    comparison page); the A1 sweep removed them all. This test
    parses the three UI files with ast and fails the suite if an
    em-dash ever appears in a non-docstring string literal again —
    the same test-enforced pattern as the B1 test-count test.
    Docstrings and comments may still use em-dashes; they are
    never shown in the window.
    """

    UI_FILES = ("forge/browser/app.py", "forge/browser/webui.py",
                "forge/browser/compare.py")

    def _em_dash_literals(self, path):
        import ast
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        return [(node.lineno, node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "—" in node.value
                and id(node) not in docstrings]

    def test_no_em_dash_in_visible_ui_strings(self):
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for rel in self.UI_FILES:
            for lineno, value in self._em_dash_literals(
                    os.path.join(root, rel)):
                offenders.append("%s:%d: %r" % (rel, lineno, value[:60]))
        self.assertEqual(offenders, [],
                         "em-dash in visible UI text (U33):\n"
                         + "\n".join(offenders))

    def test_window_title_suffix_and_tagline(self):
        # The window title suffix and the status-bar tagline are the
        # most-seen strings in the app; both used to carry em-dashes.
        from forge.browser.app import _TAGLINE
        self.assertNotIn("—", _TAGLINE)
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "forge/browser/app.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("· Forge Browser", source)


class DebugLogTests(unittest.TestCase):
    """B1: the vendored BNB S61 + T21 debug log layer, working in
    place. A focused port of the upstream smoke test — the module
    itself is copied verbatim, these checks prove the copy."""

    # Deliberate FAKE values, not real credentials: key-shaped
    # fixtures that prove the scrubber redacts key-shaped values.
    FAKE_KEY = "sk-fake-key-1234567890abcdef"
    FAKE_TOKEN = "ghp_faketoken1234567890abcd"

    def _logger(self, tmp, app="testapp", **kwargs):
        from forge.browser.debug_log import DebugLogger
        log = DebugLogger(app, "1.0",
                          log_dir=os.path.join(tmp, "logs"), **kwargs)
        self.addCleanup(self._close, log)
        return log

    @staticmethod
    def _close(log):
        for handler in list(log.logger.handlers):
            try:
                handler.close()
            finally:
                log.logger.removeHandler(handler)

    def test_scrub_text_redacts_secrets_keeps_normal_text(self):
        from forge.browser.debug_log import scrub_text
        self.assertNotIn(self.FAKE_KEY,
                         scrub_text("api_key=" + self.FAKE_KEY))
        self.assertIn("[REDACTED]",
                      scrub_text("api_key=" + self.FAKE_KEY))
        self.assertNotIn("hunter2", scrub_text("password: hunter2"))
        self.assertEqual(scrub_text("model qwen3.5:2b loaded in 4.2s"),
                         "model qwen3.5:2b loaded in 4.2s")

    def test_scrub_dict_redacts_sensitive_keys(self):
        from forge.browser.debug_log import scrub_dict
        scrubbed = scrub_dict({
            "model": "qwen3.5:2b",
            "api_key": self.FAKE_KEY,
            "token": self.FAKE_TOKEN,
            "empty_key": "",
            "nested": {"password": "hunter2", "mode": "blind"},
        })
        self.assertEqual(scrubbed["api_key"], "[REDACTED] — present")
        self.assertEqual(scrubbed["token"], "[REDACTED] — present")
        self.assertEqual(scrubbed["empty_key"], "[absent]")
        self.assertEqual(scrubbed["nested"]["password"],
                         "[REDACTED] — present")
        self.assertEqual(scrubbed["model"], "qwen3.5:2b")
        self.assertEqual(scrubbed["nested"]["mode"], "blind")

    def test_report_has_all_sections_and_no_secrets(self):
        from forge.browser.debug_log import check_ollama
        with tempfile.TemporaryDirectory() as tmp:
            log = self._logger(tmp, history=5)
            log.set_setting("model", "qwen3.5:2b")
            log.set_setting("api_key", self.FAKE_KEY)
            log.register_service(
                "ollama",
                lambda: check_ollama(base_url="http://127.0.0.1:9",
                                     timeout=1))
            log.register_dependency("nonexistent-package-xyz")
            log.info("startup ok")
            try:
                raise ValueError("boom")
            except ValueError:
                log.exception("relay failed")
            for i in range(8):
                log.action("step %d" % i)
            path = log.generate_report()
            self.assertRegex(os.path.basename(path),
                             r"debug-report-testapp-\d{8}-\d{6}\.txt$")
            with open(path, encoding="utf-8") as handle:
                report = handle.read()
            for section in ("== Context ==", "== Settings ==",
                            "== Services ==", "== Recent actions",
                            "== Errors ==", "== Log tail"):
                self.assertIn(section, report)
            self.assertIn("ValueError: boom", report)
            self.assertIn("Traceback", report)
            self.assertNotIn(self.FAKE_KEY, report)
            self.assertNotIn(self.FAKE_TOKEN, report)
            self.assertIn("[REDACTED]", report)
            self.assertIn("ollama: not reachable", report)
            self.assertIn("nonexistent-package-xyz: not installed",
                          report)
            actions = report.split("== Recent actions")[1].split(
                "== Errors ==")[0]
            self.assertIn("step 7", actions)
            self.assertIn("step 3", actions)
            self.assertNotIn("step 2", actions)

    def test_rotation_caps_total_log_size(self):
        import glob
        with tempfile.TemporaryDirectory() as tmp:
            log = self._logger(tmp, app="rotapp", max_bytes=1024,
                               backup_count=2, history=5)
            for i in range(200):
                log.info("filler line %03d %s" % (i, "x" * 40))
            for handler in log.logger.handlers:
                handler.flush()
            sizes = [os.path.getsize(p)
                     for p in glob.glob(log.log_path + "*")]
            self.assertGreater(len(sizes), 1)
            self.assertLessEqual(sum(sizes), 3 * 1024 + 512)

    def test_crash_handler_writes_a_report(self):
        import glob
        import sys as _sys
        with tempfile.TemporaryDirectory() as tmp:
            log = self._logger(tmp, app="crashapp")
            log_dir = os.path.join(tmp, "logs")
            before = set(glob.glob(os.path.join(
                log_dir, "debug-report-*.txt")))
            log.install_crash_handler()
            try:
                _sys.excepthook(ValueError, ValueError("crash-test"),
                                None)
            finally:
                log.uninstall_crash_handler()
            after = set(glob.glob(os.path.join(
                log_dir, "debug-report-*.txt")))
            self.assertGreater(len(after), len(before))


class HowtoTests(unittest.TestCase):
    """B2: the How to Use index reads BROWSER.md's own headings."""

    def test_find_manual_finds_the_repo_manual(self):
        from forge.browser.howto import find_manual
        path = find_manual()
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("BROWSER.md"))
        self.assertTrue(os.path.isfile(path))

    def test_load_sections_from_the_real_manual(self):
        from forge.browser.howto import load_sections
        sections = load_sections()
        self.assertGreaterEqual(len(sections), 30)
        titles = [title for _level, title, _body in sections]
        self.assertEqual(titles[0],
                         "Forge Browser (standalone: the AI browser build)")
        self.assertIn("Install (standalone)", titles)
        self.assertIn("Chat: the AI part", titles)
        self.assertIn("Limits, plainly", titles)
        bodies = dict((title, body)
                      for _level, title, body in sections)
        self.assertIn("double-click", bodies["Install (standalone)"])

    def test_no_loaded_title_carries_an_em_dash(self):
        # Titles are index entries — visible UI text (U33).
        from forge.browser.howto import load_sections
        for _level, title, _body in load_sections():
            self.assertNotIn("—", title)

    def test_load_sections_on_a_fixture(self):
        from forge.browser.howto import load_sections
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "MANUAL.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# Top\n\nintro\n\n## First — part\n\n"
                             "body one\n\n### Deep\n\nbody two\n")
            sections = load_sections(path)
        self.assertEqual(
            [(level, title) for level, title, _b in sections],
            [(1, "Top"), (2, "First: part"), (3, "Deep")])
        self.assertEqual(sections[1][2], "body one")
        self.assertEqual(sections[2][2], "body two")

    def test_missing_manual_is_empty_not_an_error(self):
        from forge.browser.howto import load_sections
        self.assertEqual(
            load_sections("/nonexistent-dir-nope/BROWSER.md"), [])


class HowtoWebTests(unittest.TestCase):
    """B2: the /howto page and the start page's first-run link."""

    def test_howto_route_serves_the_index(self):
        import urllib.request
        from forge.browser import webui
        server = webui.LocalPageServer().start()
        try:
            body = urllib.request.urlopen(
                server.home_url + "howto", timeout=5).read().decode()
        finally:
            server.stop()
        self.assertIn("How to Use", body)
        self.assertIn("Install (standalone)", body)
        self.assertIn("#s0", body)

    def test_start_page_links_howto_on_first_run(self):
        import urllib.request
        from unittest import mock
        from forge.browser import webui
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_HISTORY_PATH":
                   os.path.join(tmp, "no-history.json")}
            with mock.patch.dict(os.environ, env):
                server = webui.LocalPageServer().start()
                try:
                    body = urllib.request.urlopen(
                        server.home_url, timeout=5).read().decode()
                finally:
                    server.stop()
        self.assertIn('href="/howto"', body)
        self.assertIn("First time here?", body)


class AppWiringTests(unittest.TestCase):
    """B1/B3/B4: the window wiring, pinned in the stdlib suite.

    The Qt behavior itself (menu clicks, second-instance exit,
    the icon actually showing) is proven by the offscreen smoke
    run, not here — the suite has no Qt. These checks pin the
    wiring in place so a later edit cannot silently drop it.
    """

    def _app_source(self):
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "forge/browser/app.py"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_help_menu_has_howto_and_debug_report(self):
        source = self._app_source()
        self.assertIn('"How to Use…"', source)
        self.assertIn('"Debug Report…"', source)
        self.assertIn("def _show_howto", source)
        self.assertIn("def _show_debug_report", source)
        self.assertIn("install_crash_handler()", source)

    def test_single_instance_guard_and_window_icon(self):
        source = self._app_source()
        self.assertIn("QLockFile", source)
        self.assertIn("QLocalServer", source)
        self.assertIn("tryLock(0)", source)
        self.assertIn("_take_single_instance()", source)
        self.assertIn("setWindowIcon", source)
        self.assertIn("def app_icon_path", source)

    def test_app_icon_path_finds_the_repo_svg(self):
        from forge.browser.app import app_icon_path
        path = app_icon_path()
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("forge-browser.svg"))
        self.assertTrue(os.path.isfile(path))


class VersionTests(unittest.TestCase):
    """E4: one __version__ for the repo. forge/__init__.py used to
    say "2.5" (the Code Forge parent version) while the browser
    said "0.8" — a reader grepping for the version found two
    answers. The parent mirrors the browser now, pinned here."""

    def test_parent_and_browser_versions_agree(self):
        import forge
        from forge.browser import __version__ as browser_version
        self.assertEqual(forge.__version__, browser_version)


class DocFixTests(unittest.TestCase):
    """E1/E2/E3: the deep audit's doc fixes, pinned."""

    def _read(self, rel):
        root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as handle:
            text = handle.read()
        # Prose wraps mid-sentence; collapse whitespace so the
        # assertions check the words, not the line breaks.
        return " ".join(text.split())

    def test_limits_no_longer_calls_ocr_missing(self):
        text = self._read("BROWSER.md")
        self.assertNotIn("Scanned PDFs need OCR, which is not in "
                         "this build", text)
        self.assertIn("Scanned PDFs are read with OCR when "
                      "tesseract is installed", text)

    def test_limits_names_the_one_page_limit_correctly(self):
        text = self._read("BROWSER.md")
        self.assertNotIn("One engine session = one page. "
                         "No parallel tabs in this build", text)
        self.assertIn("this limit is the engine room's, not the "
                      "window's", text)

    def test_changelog_makes_no_v03_release_claim(self):
        # The 0.3 section itself must make no release claim. (The
        # 0.9 section names the claim while recording its
        # correction — so the assertion is scoped to the 0.3
        # section, not the whole file.)
        from forge.browser.whatsnew import read_changelog_section
        section = " ".join(read_changelog_section("0.3"))
        self.assertNotIn("GitHub Release v0.3", section)
        self.assertIn("shipped together as GitHub Release v0.7", section)


class CliCheckVersionHistoryTests(unittest.TestCase):
    """I2/I3/I4: run.sh check, --version, history export, reports."""

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run_main(self, argv):
        import contextlib
        import io
        from forge.browser.cli import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, buffer.getvalue()

    def test_version_prints_whatsnew_line(self):
        # I3: --version is the window's "what changed" line, not
        # a bare number — engine-room parity.
        from forge.browser import __version__
        code, out = self._run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(
            out.startswith(f"What's new in {__version__}: "),
            out)

    def test_history_export_writes_json(self):
        # I4: the visit history out to a file, as JSON.
        from unittest import mock
        from forge.browser.titles import record_visit
        with tempfile.TemporaryDirectory() as tmp:
            history = os.path.join(tmp, "history.json")
            out_path = os.path.join(tmp, "export.json")
            with mock.patch.dict(os.environ,
                                 {"FORGE_BROWSER_HISTORY_PATH": history}):
                record_visit("https://gears.example/", "Gear Guide")
                record_visit("https://other.example/", "Other Page")
                code, out = self._run_main(
                    ["history", "export", out_path])
            self.assertEqual(code, 0)
            self.assertIn("exported 2 visits to", out)
            with open(out_path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(len(data["visits"]), 2)
            urls = [v["url"] for v in data["visits"]]
            self.assertIn("https://gears.example/", urls)
            self.assertIn("https://other.example/", urls)

    def test_history_export_empty_history(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "export.json")
            with mock.patch.dict(os.environ,
                                 {"FORGE_BROWSER_HISTORY_PATH":
                                  os.path.join(tmp, "no-history.json")}):
                code, out = self._run_main(
                    ["history", "export", out_path])
            self.assertEqual(code, 0)
            self.assertIn("exported 0 visits to", out)
            with open(out_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"visits": []})

    def test_history_bare_prints_usage(self):
        code, out = self._run_main(["history"])
        self.assertEqual(code, 1)
        self.assertIn("usage: history export", out)

    def test_reports_lists_saved_reports(self):
        # I4: the saved research reports, newest first.
        from unittest import mock
        from forge.browser.research import save_report
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "research")
            with mock.patch.dict(os.environ,
                                 {"FORGE_BROWSER_RESEARCH_DIR": folder}):
                save_report("gears", "# Research: gears\n\nbody\n")
                code, out = self._run_main(["reports"])
            self.assertEqual(code, 0)
            self.assertIn("gears", out)

    def test_reports_empty(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"FORGE_BROWSER_RESEARCH_DIR":
                                  os.path.join(tmp, "research")}):
                code, out = self._run_main(["reports"])
            self.assertEqual(code, 0)
            self.assertIn("no saved research reports yet", out)

    def test_run_sh_check_without_ollama_reports_not_run(self):
        # I2: no Ollama answering means the validators are
        # reported as not run — never a crash, never a failure.
        import subprocess
        env = dict(os.environ)
        env["FORGE_OLLAMA_URL"] = "http://127.0.0.1:9"
        proc = subprocess.run(
            ["sh", "run.sh", "check"], cwd=self._root(),
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not run: no Ollama answering", proc.stdout)
        self.assertIn("Threshold validators", proc.stdout)

    def test_run_sh_names_both_validators_and_parses(self):
        import subprocess
        root = self._root()
        with open(os.path.join(root, "run.sh"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("validate_threshold", text)
        self.assertIn("validate_memory_threshold", text)
        proc = subprocess.run(["sh", "-n", "run.sh"], cwd=root,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class CiTests(unittest.TestCase):
    """J1: the free CI script exists, parses, and its verdict
    record carries the current suite count. CI-STATUS.md is
    generated by `sh ci/check.sh` and committed with the push —
    README.md is the count's reference here (DocsTests already
    pins README.md to the true suite count)."""

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, rel):
        with open(os.path.join(self._root(), rel),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_check_script_exists_and_parses(self):
        import subprocess
        path = os.path.join(self._root(), "ci", "check.sh")
        self.assertTrue(os.path.isfile(path))
        proc = subprocess.run(["sh", "-n", path],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_build_zip_script_exists_and_parses(self):
        import subprocess
        path = os.path.join(self._root(), "ci", "build-zip.py")
        self.assertTrue(os.path.isfile(path))
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_ci_status_carries_current_suite_count(self):
        import re
        readme_count = re.search(r"(\d+) checks", self._read("README.md"))
        self.assertIsNotNone(readme_count, "README.md states no count")
        status = self._read("CI-STATUS.md")
        status_count = re.search(r"(\d+) checks", status)
        self.assertIsNotNone(status_count,
                             "CI-STATUS.md carries no suite count")
        self.assertEqual(status_count.group(1), readme_count.group(1))
        self.assertIn("Release zip:", status)
        self.assertIn("modes", status)


class VerifyTests(unittest.TestCase):
    def test_local_page_fallback_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "index.html"), "w") as handle:
                handle.write("<html><head><title>T</title></head>"
                             "<body><h1>Project Page</h1></body></html>")
            def no_engine():
                raise BrowserError("no engine in this test")

            old = os.environ.pop("FORGE_CDP_URL", None)
            try:
                result = verify_in_browser("index.html", tmp,
                                           session_factory=no_engine)
            finally:
                if old:
                    os.environ["FORGE_CDP_URL"] = old
            self.assertIn("Project Page", result["text"])
            self.assertFalse(result["rendered"])  # factory refuses -> plain fetch
            with self.assertRaises(BrowserError):
                verify_in_browser("../outside.html", tmp)


if __name__ == "__main__":
    unittest.main()
