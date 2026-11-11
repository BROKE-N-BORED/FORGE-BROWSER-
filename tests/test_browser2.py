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
    if method == "Target.attachToTarget":
        return {"sessionId": "S1"}
    if method in ("Page.enable", "Accessibility.enable", "Target.closeTarget"):
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
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def run(self):
        try:
            conn, _ = self.listener.accept()
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
                result = _cdp_reply(method, params.get("expression", ""))
                _ws_send(conn, json.dumps({"id": message["id"], "result": result}))
                if method == "Page.navigate":
                    _ws_send(conn, json.dumps(
                        {"method": "Page.loadEventFired", "params": {},
                         "sessionId": "S1"}))
        except Exception:
            pass
        finally:
            try:
                self.listener.close()
            except Exception:
                pass


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
            "/pic.png": ("image/png", FAKE_PNG),
            "/data.json": ("application/json", b'{"a": 1}'),
            "/table.csv": ("text/csv", b"name,value\nalpha,1\n"),
            "/song.mp3": ("audio/mpeg", b"not-real-audio"),
            "/big.bin": ("application/octet-stream", b"x" * 2048),
            "/big.txt": ("text/plain", b"y" * 2048),
        }
        content_type, body = routes.get(self.path, ("text/plain", b"missing"))
        self.send_response(200 if self.path in routes else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        result = fetch_any(self.base + "/doc.pdf")
        self.assertEqual(result.kind, "pdf")
        self.assertIn("Hello PDF World", result.text)

    def test_image_saved_and_honest_note(self):
        result = fetch_any(self.base + "/pic.png")
        self.assertEqual(result.kind, "image")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertEqual(result.text, "")  # no seat: nothing invented
        self.assertTrue(any("no local vision seat" in n for n in result.notes))

    def test_json_and_csv(self):
        result = fetch_any(self.base + "/data.json")
        self.assertEqual(result.kind, "text")
        self.assertIn('"a": 1', result.text)
        result = fetch_any(self.base + "/table.csv")
        self.assertIn("| name | value |", result.text)
        self.assertIn("| alpha | 1 |", result.text)

    def test_audio_saved_with_note(self):
        result = fetch_any(self.base + "/song.mp3")
        self.assertEqual(result.kind, "audio")
        self.assertTrue(os.path.isfile(result.saved_path))


class DownloadCapTests(unittest.TestCase):
    """Task #17: no download may fill the machine's memory."""

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
            http_get(self.base + "/declared-big.bin", max_bytes=1024)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("5000", str(caught.exception))
        self.assertIn("1024", str(caught.exception))

    def test_chunked_body_refused_over_cap(self):
        with self.assertRaises(BrowserError):
            http_get(self.base + "/big-chunked.bin", max_bytes=1024)

    def test_gzip_bomb_refused_on_decompressed_size(self):
        # ~50 bytes on the wire, 5000 bytes out — the cap counts
        # decompressed bytes, so the bomb is refused.
        with self.assertRaises(BrowserError):
            http_get(self.base + "/bomb.gz", max_bytes=1024)

    def test_get_to_file_caps_and_removes_partial(self):
        path = os.path.join(self.tmp, "partial.bin")
        with self.assertRaises(BrowserError):
            http_get_to_file(self.base + "/big.bin", path, max_bytes=1024)
        self.assertFalse(os.path.exists(path))
        _url, _headers, size = http_get_to_file(
            self.base + "/big.bin", path, max_bytes=4096)
        self.assertEqual(size, 2048)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 2048)

    def test_fetch_any_binary_past_memory_cap(self):
        # A binary over the in-memory cap still downloads — it
        # streams to disk instead of into RAM.
        old = files_module.MAX_BODY_BYTES
        files_module.MAX_BODY_BYTES = 1024
        try:
            result = fetch_any(self.base + "/big.bin")
        finally:
            files_module.MAX_BODY_BYTES = old
        self.assertEqual(result.kind, "binary")
        self.assertEqual(result.size_bytes, 2048)
        self.assertTrue(os.path.isfile(result.saved_path))

    def test_fetch_any_text_over_memory_cap_saved_not_read(self):
        old = files_module.MAX_BODY_BYTES
        files_module.MAX_BODY_BYTES = 1024
        try:
            result = fetch_any(self.base + "/big.txt")
        finally:
            files_module.MAX_BODY_BYTES = old
        self.assertEqual(result.kind, "text")
        self.assertEqual(result.text, "")
        self.assertTrue(os.path.isfile(result.saved_path))
        self.assertTrue(any("too big" in n for n in result.notes))


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
