"""Tests for Forge Browser v0.10, ENGINE section (review task list
2026-11-15): WebSocket caps and masking, HTTP gzip/pin/opener work,
search fallbacks, CDP client/session/launch fixes, PDF and JSON
memory bounds, the verify single-file server, and the fence-pin and
barrier gaps (G-T2, G-T5). Offline throughout: the CDP engine is
tested against a scripted fake WebSocket server built on raw
sockets in this file, and HTTP paths against scripted sockets or
local fixture servers.
"""

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import BrowserError  # noqa: E402
from forge.browser import _http as _http_module  # noqa: E402
from forge.browser import _ws as _ws_module  # noqa: E402
from forge.browser import barriers  # noqa: E402
from forge.browser import cdp as cdp_module  # noqa: E402
from forge.browser._http import _iter_chunks, http_get, http_head  # noqa: E402
from forge.browser._ws import MAX_WS_MESSAGE_BYTES, WebSocket, _xor_mask  # noqa: E402
from forge.browser.cdp import (BrowserSession, CDPClient,  # noqa: E402
                               _ELEMENTS_JS, _spawn_lightpanda,
                               find_kitesurf, format_ax_tree,
                               open_browser_window)
from forge.browser.files import (_parse_tounicode_cmap,  # noqa: E402
                                 _pretty_json_prefix, extract_pdf_text,
                                 fetch_any)
from forge.browser.search import (parse_bing_html, parse_searxng_json,  # noqa: E402
                                  search)
from forge.browser.verify import verify_in_browser  # noqa: E402

FAKE_PAGE_HTML = """<html><head><title>Fake Title</title></head><body>
<h1>Rendered Heading</h1><p>Rendered body text.</p>
<button>Accept all cookies</button></body></html>"""

FAKE_ELEMENTS = json.dumps([
    {"i": 0, "tag": "button", "type": "", "text": "Accept all cookies", "href": "", "name": ""},
    {"i": 1, "tag": "input", "type": "text", "text": "", "href": "", "name": "username"},
    {"i": 2, "tag": "input", "type": "password", "text": "", "href": "", "name": "password"},
])

FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

FAKE_AX_NODES = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Fake"},
     "childIds": ["2"]},
    {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Accept all cookies"}},
]


# ----------------------------------------------------------------------
# Scripted socket + WebSocket frame helpers (A5 / G-P5)
# ----------------------------------------------------------------------

class _ScriptedSocket:
    """A socket fed canned byte chunks; recv past the end raises
    instead of returning b"" when forbid_after is set — proving
    the reader never asked for more."""

    def __init__(self, chunks, forbid_after=False):
        self._chunks = list(chunks)
        self._forbid_after = forbid_after
        self.sent = []

    def recv(self, n):
        if not self._chunks:
            if self._forbid_after:
                raise AssertionError("recv called past the scripted bytes")
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        pass

    def gettimeout(self):
        return None

    def close(self):
        pass


def _ws_frame(payload, opcode=0x1, fin=True):
    """One unmasked server-side WebSocket frame."""
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, length])
    elif length < 65536:
        header = bytes([first, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first, 127]) + length.to_bytes(8, "big")
    return header + payload


def _ws_huge_header(declared):
    """A frame header declaring `declared` payload bytes, no payload."""
    return bytes([0x81, 127]) + declared.to_bytes(8, "big")


# ----------------------------------------------------------------------
# Fake CDP WebSocket server (raw sockets, RFC 6455 server side)
# ----------------------------------------------------------------------

def _ws_accept(key):
    digest = hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
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


def _cdp_result(method, params):
    if method == "Target.createTarget":
        return {"targetId": "T1"}
    if method == "Target.createBrowserContext":
        return {"browserContextId": "CTX1"}
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
        expression = params.get("expression", "")
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


def _default_cdp_reply(method, params, msg_id):
    frames = [json.dumps({"id": msg_id,
                          "result": _cdp_result(method, params)})]
    if method == "Page.navigate":
        frames.append(json.dumps({"method": "Page.loadEventFired",
                                  "params": {}, "sessionId": "S1"}))
    return frames


class _FakeCDPServer(threading.Thread):
    """A scripted CDP server. reply(method, params, msg_id) returns
    the raw frames to send; the default answers like a real engine.
    `closed` fires when a connection ends (the client hung up)."""

    def __init__(self, reply=None):
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.reply = reply or _default_cdp_reply
        self.calls = []
        self.connections = 0
        self.closed = threading.Event()
        self._stopped = False

    def stop(self):
        self._stopped = True
        try:
            self.listener.close()
        except Exception:
            pass

    def run(self):
        while not self._stopped:
            try:
                conn, _ = self.listener.accept()
            except Exception:
                break
            self.connections += 1
            try:
                self._serve(conn)
            except Exception:
                pass
            finally:
                self.closed.set()
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
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
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
                return
            message = json.loads(payload.decode())
            method = message.get("method", "")
            params = message.get("params") or {}
            self.calls.append((method, params))
            for frame in self.reply(method, params, message.get("id")):
                _ws_send(conn, frame)


class _VersionHandler(BaseHTTPRequestHandler):
    """Serves the JSON body on server.forge_body at /json/version."""

    def do_GET(self):
        body = self.server.forge_body
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _FakeEngine:
    """A fake engine: a CDP WebSocket server plus its /json/version
    HTTP endpoint. `version` overrides keys of the version dict."""

    def __init__(self, reply=None, version=None):
        self.ws = _FakeCDPServer(reply)
        self.ws.start()
        body = {"Browser": "Chrome/145.0.0.0", "Protocol-Version": "1.3",
                "webSocketDebuggerUrl":
                    f"ws://127.0.0.1:{self.ws.port}/devtools/browser"}
        if version is not None:
            body = version
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _VersionHandler)
        self.http.forge_body = body
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.ws.stop()

    def navigated_urls(self):
        return [params.get("url") for method, params in self.ws.calls
                if method == "Page.navigate"]


# ----------------------------------------------------------------------
# Fake process (launch tests) and scripted WebSocket (client tests)
# ----------------------------------------------------------------------

class _FakeProcess:
    """A subprocess stand-in: alive or dead on demand, recording
    terminate/wait like the real thing."""

    def __init__(self, alive=True):
        self._alive = alive
        self.returncode = None
        self.args = None
        self.terminated = False
        self.waited = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


class _FakeSock:
    def settimeout(self, timeout):
        pass


class _ScriptedWs:
    """A WebSocket stand-in feeding scripted recv_message outcomes
    (a str to return, or an Exception to raise)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.sent = []
        self._sock = _FakeSock()

    def send_text(self, text):
        self.sent.append(text)

    def recv_message(self):
        if not self._outcomes:
            raise socket.timeout("no more scripted messages")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        pass


class _StubClient:
    """A CDPClient stand-in that only needs close()."""

    def close(self):
        pass


# ----------------------------------------------------------------------
# A5 + G-P5: WebSocket frame cap and chunked masking
# ----------------------------------------------------------------------

class WsCapTests(unittest.TestCase):
    def test_frame_over_cap_refused_before_reading(self):
        # A5: a frame declaring more than the cap is refused from
        # the header alone — the payload is never read.
        with mock.patch.object(_ws_module, "MAX_WS_MESSAGE_BYTES", 100):
            sock = _ScriptedSocket([_ws_huge_header(200)],
                                   forbid_after=True)
            ws = WebSocket(sock)
            with self.assertRaises(BrowserError) as caught:
                ws.recv_message()
            self.assertIn("too big", str(caught.exception))

    def test_giant_declared_length_refused(self):
        # A5: the 64-bit length from the peer is not trusted — a
        # 2**40 declaration refuses at once.
        sock = _ScriptedSocket([_ws_huge_header(2 ** 40)],
                               forbid_after=True)
        ws = WebSocket(sock)
        with self.assertRaises(BrowserError):
            ws.recv_message()

    def test_fragments_over_cap_refused(self):
        # A5: fragments accumulate toward the same cap.
        with mock.patch.object(_ws_module, "MAX_WS_MESSAGE_BYTES", 100):
            sock = _ScriptedSocket([
                _ws_frame(b"x" * 60, fin=False),
                _ws_frame(b"y" * 60, fin=True),
            ])
            ws = WebSocket(sock)
            with self.assertRaises(BrowserError) as caught:
                ws.recv_message()
            self.assertIn("too big", str(caught.exception))

    def test_frame_at_cap_passes(self):
        with mock.patch.object(_ws_module, "MAX_WS_MESSAGE_BYTES", 100):
            sock = _ScriptedSocket([_ws_frame(b"x" * 100)])
            ws = WebSocket(sock)
            self.assertEqual(ws.recv_message(), "x" * 100)

    def test_small_frames_unaffected(self):
        sock = _ScriptedSocket([
            _ws_frame(b"hel", fin=False),
            _ws_frame(b"lo", fin=True),
            _ws_frame(b"second"),
        ])
        ws = WebSocket(sock)
        self.assertEqual(ws.recv_message(), "hello")
        self.assertEqual(ws.recv_message(), "second")


class WsMaskTests(unittest.TestCase):
    def test_mask_matches_naive_xor(self):
        # G-P5: the chunked mask is byte-identical to the old
        # per-byte loop at every length, including block edges.
        import random
        rng = random.Random(20261115)
        for size in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 17,
                     31, 32, 33, 100, 1000, 65536):
            data = bytes(rng.randrange(256) for _ in range(size))
            mask = bytes(rng.randrange(256) for _ in range(4))
            naive = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            self.assertEqual(_xor_mask(data, mask), naive,
                             msg=f"size {size}")

    def test_mask_is_its_own_inverse(self):
        data = os.urandom(1000)
        mask = os.urandom(4)
        self.assertEqual(_xor_mask(_xor_mask(data, mask), mask), data)

    def test_send_frame_masks_payload(self):
        # The masked frame a real server would see decodes back to
        # the sent text.
        sock = _ScriptedSocket([])
        ws = WebSocket(sock)
        text = "hello world, a longer message crossing block sizes"
        ws.send_text(text)
        sent = b"".join(sock.sent)
        self.assertEqual(sent[0], 0x81)  # fin + text
        self.assertTrue(sent[1] & 0x80)  # masked
        length = sent[1] & 0x7F
        mask = sent[2:6]
        payload = sent[6:6 + length]
        self.assertEqual(_xor_mask(payload, mask), text.encode())


# ----------------------------------------------------------------------
# G-P2: gzip bomb transient
# ----------------------------------------------------------------------

class _FakeGzipResponse:
    def __init__(self, body, encoding=None):
        self._body = body
        self._pos = 0
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class GzipCapTests(unittest.TestCase):
    def test_gzip_bomb_refused_within_budget(self):
        # G-P2: a bomb expanding to 20 MB is refused against a
        # 1 MB budget without ever holding the expansion.
        bomb = gzip.compress(b"\0" * (20 * 1024 * 1024), compresslevel=9)
        response = _FakeGzipResponse(bomb, encoding="gzip")
        tracemalloc.start()
        try:
            with self.assertRaises(BrowserError):
                b"".join(_iter_chunks(response, 1024 * 1024,
                                      "http://example.org/bomb",
                                      kind="page"))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 5 * 1024 * 1024)

    def test_gzip_under_cap_still_reads(self):
        plain = os.urandom(200000)  # incompressible: several reads
        response = _FakeGzipResponse(gzip.compress(plain), encoding="gzip")
        self.assertEqual(
            b"".join(_iter_chunks(response, 1024 * 1024,
                                  "http://example.org/x", kind="page")),
            plain)

    def test_gzip_exactly_at_cap_reads(self):
        plain = b"a" * 4096
        response = _FakeGzipResponse(gzip.compress(plain), encoding="gzip")
        self.assertEqual(
            b"".join(_iter_chunks(response, 4096,
                                  "http://example.org/x", kind="page")),
            plain)

    def test_plain_chunk_over_cap_still_refused(self):
        response = _FakeGzipResponse(b"x" * 2000)
        with self.assertRaises(BrowserError):
            b"".join(_iter_chunks(response, 1000,
                                  "http://example.org/x", kind="page"))


# ----------------------------------------------------------------------
# G-C9: http_head dropped-connection handler
# ----------------------------------------------------------------------

class HttpHeadDropTests(unittest.TestCase):
    def _patch_urlopen(self, exc):
        original = _http_module._urlopen

        def raising(request, timeout, allow_local):
            raise exc

        _http_module._urlopen = raising
        return original

    def test_head_reset_is_browser_error(self):
        import http.client
        original = self._patch_urlopen(
            http.client.RemoteDisconnected("remote end closed"))
        try:
            with self.assertRaises(BrowserError) as caught:
                http_head("http://127.0.0.1:9/x", allow_local=True)
        finally:
            _http_module._urlopen = original
        self.assertIn("connection dropped", str(caught.exception))

    def test_head_connection_reset_is_browser_error(self):
        original = self._patch_urlopen(
            ConnectionResetError("connection reset by peer"))
        try:
            with self.assertRaises(BrowserError) as caught:
                http_head("http://127.0.0.1:9/x", allow_local=True)
        finally:
            _http_module._urlopen = original
        self.assertIn("connection dropped", str(caught.exception))


# ----------------------------------------------------------------------
# G-P4: opener reuse
# ----------------------------------------------------------------------

class OpenerReuseTests(unittest.TestCase):
    def test_same_opener_reused_for_same_setting(self):
        first = _http_module._opener_for(False)
        self.assertIs(_http_module._opener_for(False), first)

    def test_different_opener_per_allow_local(self):
        self.assertIsNot(_http_module._opener_for(False),
                         _http_module._opener_for(True))

    def test_proxy_env_change_rebuilds_opener(self):
        before = _http_module._opener_for(False)
        with mock.patch.dict(os.environ,
                             {"http_proxy": "http://proxy.invalid:3128"}):
            changed = _http_module._opener_for(False)
        self.assertIsNot(changed, before)
        # And the original env gets the original opener back.
        self.assertIs(_http_module._opener_for(False), before)


# ----------------------------------------------------------------------
# G-S8: DNS-pinning fail-open
# ----------------------------------------------------------------------

class PinFailClosedTests(unittest.TestCase):
    def setUp(self):
        self._orig_getaddrinfo = socket.getaddrinfo
        self._orig_create_connection = socket.create_connection
        self._orig_proxy_env = {}
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            if var in os.environ:
                self._orig_proxy_env[var] = os.environ.pop(var)
        self.connects = []

    def tearDown(self):
        socket.getaddrinfo = self._orig_getaddrinfo
        socket.create_connection = self._orig_create_connection
        for var, value in self._orig_proxy_env.items():
            os.environ[var] = value

    def test_unresolvable_name_fails_without_connecting(self):
        # G-S8: the pin resolution fails, so the fetch fails —
        # it must never fall back to an unchecked connect.
        def failing(host, port, *args, **kwargs):
            raise socket.gaierror("no such host")

        def recording(address, timeout=None, source_address=None):
            self.connects.append(address)
            raise AssertionError("must not connect unchecked")

        socket.getaddrinfo = failing
        socket.create_connection = recording
        with self.assertRaises(BrowserError) as caught:
            http_get("http://does-not-exist.invalid/")
        self.assertIn("did not resolve", str(caught.exception))
        self.assertEqual(self.connects, [])

    def test_empty_pin_raises_at_connection(self):
        from forge.browser._http import _PinnedHTTPConnection
        conn = _PinnedHTTPConnection("example.com", pinned_addrs=[])
        with self.assertRaises(BrowserError) as caught:
            conn.connect()
        self.assertIn("did not resolve", str(caught.exception))

    def test_none_pin_connects_normally(self):
        # None (no pin: literal, localhost, proxied, allow-local)
        # still takes the default path.
        from forge.browser._http import _PinnedHTTPConnection

        class _Sock:
            def settimeout(self, timeout):
                pass

            def setsockopt(self, *args):
                pass

        socket.create_connection = lambda address, timeout=None, \
            source_address=None: (self.connects.append(address), _Sock())[1]
        conn = _PinnedHTTPConnection("example.com", pinned_addrs=None)
        conn.connect()
        self.assertEqual(self.connects, [("example.com", 80)])


# ----------------------------------------------------------------------
# G-C6 / G-C7 / G-C8: search fallbacks and parsers
# ----------------------------------------------------------------------

class SearchFallbackTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop("FORGE_SEARXNG_URL", None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FORGE_SEARXNG_URL", None)
        else:
            os.environ["FORGE_SEARXNG_URL"] = self._old_env

    def test_down_searxng_falls_back_to_ddg(self):
        # G-C6: a configured-but-down SearXNG falls through to the
        # keyless chain instead of breaking search.
        from forge.browser.search import SearchResult
        os.environ["FORGE_SEARXNG_URL"] = "http://127.0.0.1:1"
        found = [SearchResult(title="T", url="https://example.org/",
                              snippet="s", engine="duckduckgo")]
        with mock.patch("forge.browser.search.search_ddg",
                               return_value=found) as patched:
            results = search("example query")
        self.assertEqual(results, found)
        patched.assert_called_once()

    def test_down_searxng_falls_through_empty_backends(self):
        # The chain keeps going past a backend that answers empty.
        from forge.browser.search import SearchResult
        os.environ["FORGE_SEARXNG_URL"] = "http://127.0.0.1:1"
        found = [SearchResult(title="T", url="https://example.org/",
                              snippet="s", engine="bing")]
        with mock.patch("forge.browser.search.search_ddg",
                               return_value=[]), \
                mock.patch("forge.browser.search.search_bing",
                                  return_value=found):
            self.assertEqual(search("example query"), found)

    def test_all_backends_down_raises(self):
        os.environ["FORGE_SEARXNG_URL"] = "http://127.0.0.1:1"
        with mock.patch("forge.browser.search.search_ddg",
                               side_effect=BrowserError("ddg down")), \
                mock.patch("forge.browser.search.search_bing",
                                  side_effect=BrowserError("bing down")):
            with self.assertRaises(BrowserError) as caught:
                search("example query")
        self.assertIn("bing down", str(caught.exception))

    def test_searxng_success_still_wins(self):
        from forge.browser.search import SearchResult
        os.environ["FORGE_SEARXNG_URL"] = "http://127.0.0.1:8888"
        found = [SearchResult(title="T", url="https://example.org/",
                              snippet="s", engine="searxng")]
        with mock.patch("forge.browser.search.search_searxng",
                               return_value=found) as patched, \
                mock.patch("forge.browser.search.search_ddg",
                                  side_effect=AssertionError("must not fall through")):
            self.assertEqual(search("example query"), found)
        patched.assert_called_once()

    def test_non_dict_searxng_json_raises(self):
        # G-C7: a list, string, or number answer is a bad answer
        # in plain words, not a raw AttributeError.
        for text in ('["a", "b"]', '"just a string"', "42", "null"):
            with self.assertRaises(BrowserError, msg=text):
                parse_searxng_json(text, 5)

    def test_non_dict_searxng_item_raises(self):
        with self.assertRaises(BrowserError):
            parse_searxng_json('{"results": ["not an object"]}', 5)
        with self.assertRaises(BrowserError):
            parse_searxng_json('{"results": "not a list"}', 5)
        with self.assertRaises(BrowserError):
            parse_searxng_json('{"results": null}', 5)

    def test_valid_searxng_json_still_parses(self):
        results = parse_searxng_json(
            '{"results": [{"title": "T", "url": "https://example.org/",'
            ' "content": "snippet here", "engines": ["bing"]}]}', 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "T")
        self.assertEqual(results[0].engine, "bing")

    def test_bing_nested_li_keeps_rest_of_result(self):
        # G-C8: a nested list item inside a result must not close
        # the result — the rest of the snippet survives.
        html = ('<li class="b_algo"><h2><a href="https://example.org/a">'
                'First Title</a></h2>'
                '<p>Snippet start <ul><li>nested bit</li></ul>'
                ' snippet end</p></li>'
                '<li class="b_algo"><h2><a href="https://example.org/b">'
                'Second Title</a></h2><p>Second snippet</p></li>')
        results = parse_bing_html(html, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "First Title")
        self.assertEqual(results[0].snippet,
                         "Snippet start nested bit snippet end")
        self.assertEqual(results[1].title, "Second Title")

    def test_bing_plain_results_unchanged(self):
        html = ('<li class="b_algo"><h2><a href="https://example.org/a">'
                'Title A</a></h2><p>Snippet A</p></li>')
        results = parse_bing_html(html, 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.org/a")
        self.assertEqual(results[0].snippet, "Snippet A")


# ----------------------------------------------------------------------
# G-C2 / G-C11 / G-P6: CDP client waits, frames, event stash
# ----------------------------------------------------------------------

class CdpClientTests(unittest.TestCase):
    def test_wait_event_raises_on_dead_engine_fast(self):
        # G-C2: a dead engine fails at once instead of busy-spinning
        # to the deadline.
        client = CDPClient(_ScriptedWs([BrowserError("engine closed")]))
        started = time.monotonic()
        with self.assertRaises(BrowserError):
            client.wait_event("Page.loadEventFired", timeout=15)
        self.assertLess(time.monotonic() - started, 5)

    def test_wait_event_retries_on_timeout(self):
        client = CDPClient(_ScriptedWs([
            socket.timeout("quiet"), socket.timeout("quiet"),
            json.dumps({"method": "Page.loadEventFired", "params": {}}),
        ]))
        self.assertTrue(client.wait_event("Page.loadEventFired", timeout=10))

    def test_wait_event_reset_is_browser_error(self):
        client = CDPClient(_ScriptedWs([
            ConnectionResetError("reset by peer")]))
        with self.assertRaises(BrowserError) as caught:
            client.wait_event("Page.loadEventFired", timeout=10)
        self.assertIn("closed the connection", str(caught.exception))

    def test_call_malformed_frame_is_browser_error(self):
        # G-C11: a non-JSON reply is a plain-words failure, not a
        # raw ValueError.
        client = CDPClient(_ScriptedWs(["this is not json{"]))
        with self.assertRaises(BrowserError) as caught:
            client.call("Page.enable")
        self.assertIn("could not read", str(caught.exception))

    def test_wait_event_malformed_frame_is_browser_error(self):
        client = CDPClient(_ScriptedWs(["garbage{"]))
        with self.assertRaises(BrowserError) as caught:
            client.wait_event("Page.loadEventFired", timeout=10)
        self.assertIn("could not read", str(caught.exception))

    def test_event_stash_capped(self):
        # G-P6: past 100 stashed events the oldest drop.
        events = [json.dumps({"method": f"Other.event.{n}", "params": {}})
                  for n in range(150)]
        client = CDPClient(_ScriptedWs(events))
        self.assertFalse(client.wait_event("Never.arrives", timeout=1))
        self.assertEqual(len(client._events), 100)
        self.assertEqual(client._events[0]["method"], "Other.event.50")
        self.assertEqual(client._events[-1]["method"], "Other.event.149")

    def test_call_stash_capped(self):
        events = [json.dumps({"method": f"Other.event.{n}", "params": {}})
                  for n in range(150)]
        events.append(json.dumps({"id": 1, "result": {}}))
        client = CDPClient(_ScriptedWs(events))
        self.assertEqual(client.call("Page.enable"), {})
        self.assertEqual(len(client._events), 100)

    def test_stashed_event_still_found(self):
        client = CDPClient(_ScriptedWs([
            json.dumps({"method": "Page.loadEventFired", "params": {}}),
        ]))
        self.assertTrue(client.wait_event("Page.loadEventFired", timeout=5))
        self.assertEqual(client._events, [])


# ----------------------------------------------------------------------
# G-C10 / G-C22 / G-C28 / G-C29: session start paths
# ----------------------------------------------------------------------

class SessionStartTests(unittest.TestCase):
    def setUp(self):
        self._old_env = {}
        for var in ("FORGE_KITESURF_URL", "FORGE_CDP_URL",
                    "FORGE_LIGHTPANDA_URL"):
            self._old_env[var] = os.environ.pop(var, None)
        self._engines = []

    def tearDown(self):
        for engine in self._engines:
            engine.stop()
        for var, value in self._old_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _engine(self, reply=None, version=None):
        engine = _FakeEngine(reply, version)
        self._engines.append(engine)
        return engine

    def test_start_cleans_up_on_non_browser_error(self):
        # G-C10: a malformed reply (no targetId) raises KeyError —
        # and the session is still cleaned up (the socket closes).
        def reply(method, params, msg_id):
            if method == "Target.createTarget":
                return [json.dumps({"id": msg_id, "result": {}})]
            return _default_cdp_reply(method, params, msg_id)

        engine = self._engine(reply=reply)
        with self.assertRaises(KeyError):
            BrowserSession.start(engine=engine.url)
        self.assertTrue(engine.ws.closed.wait(timeout=5))

    def test_find_kitesurf_non_dict_version_returns_none(self):
        # G-C22: a list or string /json/version is not the engine,
        # not an AttributeError.
        for body in (["Kitesurf/0.0.1"], "Kitesurf/0.0.1", 42):
            engine = self._engine(version=body)
            os.environ["FORGE_KITESURF_URL"] = engine.url
            self.assertIsNone(find_kitesurf())

    def test_missing_websocket_url_fails_fast(self):
        # G-C22: a version dict with no webSocketDebuggerUrl fails
        # at once instead of polling for 25 s.
        engine = self._engine(version={"Browser": "Kitesurf/0.0.1",
                                       "Protocol-Version": "1.3"})
        os.environ["FORGE_KITESURF_URL"] = engine.url
        started = time.monotonic()
        with self.assertRaises(BrowserError) as caught:
            BrowserSession.start(engine="kitesurf")
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("WebSocket address", str(caught.exception))

    def test_auto_chain_prefers_cdp_over_kitesurf(self):
        # G-C22: with two engines configured, the chain order is
        # FORGE_CDP_URL first.
        first = self._engine()
        second = self._engine()
        os.environ["FORGE_CDP_URL"] = first.url
        os.environ["FORGE_KITESURF_URL"] = second.url
        with BrowserSession.start(engine="auto") as session:
            self.assertEqual(session.engine_name, "chromium")
            page = session.page("http://fake.test/page")
            self.assertTrue(page.rendered)
        self.assertEqual(first.ws.connections, 1)
        self.assertEqual(second.ws.connections, 0)

    def test_auto_chain_prefers_lightpanda_url_over_kitesurf(self):
        first = self._engine()
        second = self._engine()
        os.environ["FORGE_LIGHTPANDA_URL"] = first.url
        os.environ["FORGE_KITESURF_URL"] = second.url
        with BrowserSession.start(engine="auto") as session:
            self.assertEqual(session.engine_name, "lightpanda")
        self.assertEqual(first.ws.connections, 1)
        self.assertEqual(second.ws.connections, 0)

    def test_poll_uses_timeout(self):
        # G-C29: the /json/version poll honors the timeout instead
        # of a hardcoded 25 s.
        started = time.monotonic()
        with self.assertRaises(BrowserError) as caught:
            BrowserSession.start(engine="http://127.0.0.1:1", timeout=2)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("did not answer", str(caught.exception))

    def test_close_reaps_process(self):
        # G-C28: close() waits for the engine process — no zombie.
        # The process ignores SIGTERM, so the test exercises the
        # kill() path (the one that never waited before): after
        # close(), the kill has been waited on too.
        with tempfile.TemporaryDirectory() as tmp:
            ready = os.path.join(tmp, "ready")
            process = subprocess.Popen(
                [sys.executable, "-c",
                 "import signal, time; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 f"open({ready!r}, 'w').close(); "
                 "time.sleep(60)"])
            # Wait until the handler is installed — a SIGTERM
            # during interpreter startup would kill it first.
            deadline = time.monotonic() + 10
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(ready),
                            "child never became ready")
            session = BrowserSession(_StubClient(), process=process)
            try:
                session.close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        # returncode is set only by wait/poll/communicate — close()
        # must have waited, since nothing polled before this line.
        # -9: SIGTERM was ignored, so the kill finished it.
        self.assertEqual(process.returncode, -9)

    def test_spawn_lightpanda_failure_reaps(self):
        # G-C28: a Lightpanda that never comes up is reaped, not
        # left a zombie. (8 s: the spawn deadline, with a live
        # process that never serves.)
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "sleeper")
            with open(script, "w") as handle:
                handle.write("#!/bin/sh\nexec sleep 30\n")
            os.chmod(script, 0o755)
            captured = {}
            real_popen = subprocess.Popen

            def capturing(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                captured["proc"] = proc
                return proc

            old = os.environ.get("FORGE_LIGHTPANDA_BIN")
            os.environ["FORGE_LIGHTPANDA_BIN"] = script
            try:
                with mock.patch("subprocess.Popen",
                                side_effect=capturing):
                    with self.assertRaises(BrowserError):
                        _spawn_lightpanda()
            finally:
                if old is None:
                    os.environ.pop("FORGE_LIGHTPANDA_BIN", None)
                else:
                    os.environ["FORGE_LIGHTPANDA_BIN"] = old
        self.assertIsNotNone(captured["proc"].returncode)


# ----------------------------------------------------------------------
# A4: sandbox-first launch, random debugging port
# ----------------------------------------------------------------------

class LaunchTests(unittest.TestCase):
    def setUp(self):
        self._old_env = {}
        for var in ("FORGE_BROWSER_PORT", "FORGE_CDP_URL",
                    "FORGE_LIGHTPANDA_URL", "FORGE_KITESURF_URL"):
            self._old_env[var] = os.environ.pop(var, None)
        self._engines = []

    def tearDown(self):
        for engine in self._engines:
            engine.stop()
        for var, value in self._old_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def _engine(self):
        engine = _FakeEngine()
        self._engines.append(engine)
        return engine

    def _version_for(self, engine):
        return {"webSocketDebuggerUrl":
                f"ws://127.0.0.1:{engine.ws.port}/devtools/browser"}

    def test_launch_tries_sandbox_first(self):
        # A4: a launch that works never passes --no-sandbox.
        engine = self._engine()
        created = []

        def fake_popen(args, **kwargs):
            proc = _FakeProcess(alive=True)
            proc.args = list(args)
            created.append(proc)
            return proc

        with mock.patch.object(cdp_module, "find_chromium",
                               return_value="/fake/chromium"), \
                mock.patch("subprocess.Popen", side_effect=fake_popen), \
                mock.patch.object(cdp_module, "_http_json",
                                  return_value=self._version_for(engine)):
            with BrowserSession.start(engine="chromium") as session:
                page = session.page("http://fake.test/page")
                self.assertTrue(page.rendered)
        self.assertEqual(len(created), 1)
        self.assertNotIn("--no-sandbox", created[0].args)

    def test_launch_falls_back_to_no_sandbox(self):
        # A4: a sandboxed launch whose process dies at once is
        # retried once with --no-sandbox.
        engine = self._engine()
        created = []

        def fake_popen(args, **kwargs):
            proc = _FakeProcess(alive=len(created) >= 1)
            proc.args = list(args)
            created.append(proc)
            return proc

        def fake_http_json(url, timeout=5):
            if len(created) < 2:
                raise urllib.error.URLError("connection refused")
            return self._version_for(engine)

        with mock.patch.object(cdp_module, "find_chromium",
                               return_value="/fake/chromium"), \
                mock.patch("subprocess.Popen", side_effect=fake_popen), \
                mock.patch.object(cdp_module, "_http_json",
                                  side_effect=fake_http_json):
            with BrowserSession.start(engine="chromium") as session:
                page = session.page("http://fake.test/page")
                self.assertTrue(page.rendered)
        self.assertEqual(len(created), 2)
        self.assertNotIn("--no-sandbox", created[0].args)
        self.assertIn("--no-sandbox", created[1].args)
        self.assertTrue(created[0].terminated)
        self.assertTrue(created[0].waited)

    def test_launch_failure_reaps_both_attempts(self):
        # A4 + G-C28 + G-C29: two real processes that never serve
        # (sandboxed, then --no-sandbox) are both reaped, and the
        # whole failure takes ~2 timeouts, not ~50 s.
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "sleeper")
            with open(script, "w") as handle:
                handle.write("#!/bin/sh\nexec sleep 30\n")
            os.chmod(script, 0o755)
            created = []
            real_popen = subprocess.Popen

            def capturing(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                proc.args = list(args[0])
                created.append(proc)
                return proc

            started = time.monotonic()
            with mock.patch.object(cdp_module, "find_chromium",
                                   return_value=script), \
                    mock.patch("subprocess.Popen", side_effect=capturing):
                with self.assertRaises(BrowserError):
                    BrowserSession.start(engine="chromium", timeout=2)
            elapsed = time.monotonic() - started
        self.assertEqual(len(created), 2)
        self.assertNotIn("--no-sandbox", created[0].args)
        self.assertIn("--no-sandbox", created[1].args)
        for proc in created:
            self.assertIsNotNone(proc.returncode)
        self.assertLess(elapsed, 15)

    def test_open_window_uses_random_port(self):
        # A4: the window's debugging port is random per launch —
        # not the fixed 9222.
        with tempfile.TemporaryDirectory() as profile:
            created = []

            def fake_popen(args, **kwargs):
                proc = _FakeProcess(alive=True)
                proc.args = list(args)
                created.append(proc)
                return proc

            def fake_http_json(url, timeout=5):
                if not created:
                    raise urllib.error.URLError("connection refused")
                return {"Browser": "Chrome/145.0.0.0"}

            with mock.patch.object(cdp_module, "default_profile_dir",
                                   return_value=profile), \
                    mock.patch.object(cdp_module, "find_chromium",
                                      return_value="/fake/chromium"), \
                    mock.patch("subprocess.Popen", side_effect=fake_popen), \
                    mock.patch.object(cdp_module, "_http_json",
                                      side_effect=fake_http_json):
                result = open_browser_window("http://example.org/")
        self.assertTrue(result["started"])
        port = int(result["endpoint"].rsplit(":", 1)[1])
        self.assertNotEqual(port, 9222)
        self.assertEqual(len(created), 1)
        self.assertIn(f"--remote-debugging-port={port}", created[0].args)
        self.assertNotIn("--no-sandbox", created[0].args)

    def test_open_window_discovers_running_window(self):
        # A4: a running window is found through the profile's
        # DevToolsActivePort file — no new launch, no fixed port.
        engine = self._engine()
        with tempfile.TemporaryDirectory() as profile:
            with open(os.path.join(profile, "DevToolsActivePort"),
                      "w") as handle:
                handle.write(
                    f"{engine.http.server_address[1]}\n/devtools/browser\n")
            with mock.patch.object(cdp_module, "default_profile_dir",
                                   return_value=profile), \
                    mock.patch("subprocess.Popen",
                               side_effect=AssertionError("must not launch")):
                result = open_browser_window("http://fake.test/page")
        self.assertFalse(result["started"])
        self.assertEqual(result["endpoint"], engine.url)
        self.assertEqual(engine.navigated_urls(), ["http://fake.test/page"])

    def test_open_window_honors_env_port(self):
        # An explicit FORGE_BROWSER_PORT still wins over random.
        with tempfile.TemporaryDirectory() as profile:
            created = []

            def fake_popen(args, **kwargs):
                proc = _FakeProcess(alive=True)
                proc.args = list(args)
                created.append(proc)
                return proc

            def fake_http_json(url, timeout=5):
                if not created:
                    raise urllib.error.URLError("connection refused")
                return {"Browser": "Chrome/145.0.0.0"}

            with mock.patch.dict(os.environ,
                                 {"FORGE_BROWSER_PORT": "9999"}), \
                    mock.patch.object(cdp_module, "default_profile_dir",
                                      return_value=profile), \
                    mock.patch.object(cdp_module, "find_chromium",
                                      return_value="/fake/chromium"), \
                    mock.patch("subprocess.Popen", side_effect=fake_popen), \
                    mock.patch.object(cdp_module, "_http_json",
                                      side_effect=fake_http_json):
                result = open_browser_window("http://example.org/")
        self.assertEqual(result["endpoint"], "http://127.0.0.1:9999")
        self.assertIn("--remote-debugging-port=9999", created[0].args)


# ----------------------------------------------------------------------
# G-C12: AX tree depth cap and cycle guard
# ----------------------------------------------------------------------

class AxTreeTests(unittest.TestCase):
    def test_cyclic_tree_terminates(self):
        nodes = [
            {"nodeId": "1", "role": {"value": "group"},
             "name": {"value": "A"}, "childIds": ["2"]},
            {"nodeId": "2", "role": {"value": "group"},
             "name": {"value": "B"}, "childIds": ["1"]},
        ]
        lines = format_ax_tree(nodes)
        self.assertEqual(lines, ["- group: A", "  - group: B"])

    def test_self_referencing_node_terminates(self):
        nodes = [
            {"nodeId": "1", "role": {"value": "group"},
             "name": {"value": "A"}, "childIds": ["1"]},
        ]
        self.assertEqual(format_ax_tree(nodes), ["- group: A"])

    def test_deep_tree_capped(self):
        depth = 500
        nodes = []
        for index in range(depth):
            node = {"nodeId": str(index), "role": {"value": "group"},
                    "name": {"value": f"N{index}"}}
            if index + 1 < depth:
                node["childIds"] = [str(index + 1)]
            nodes.append(node)
        lines = format_ax_tree(nodes)
        self.assertLess(len(lines), 120)
        self.assertEqual(
            sum("cut off" in line for line in lines), 1)

    def test_normal_tree_unchanged(self):
        lines = format_ax_tree(FAKE_AX_NODES)
        self.assertEqual(lines[0], "- RootWebArea: Fake")
        self.assertEqual(lines[1], "  - button: Accept all cookies")


# ----------------------------------------------------------------------
# G-P19: screenshot stream-decode
# ----------------------------------------------------------------------

class ScreenshotTests(unittest.TestCase):
    def test_large_screenshot_written_exactly(self):
        # A 5 MB PNG (valid signature, arbitrary body) round-trips
        # through the chunked decode byte for byte.
        payload = b"\x89PNG\r\n\x1a\n" + os.urandom(5 * 1024 * 1024 - 8)

        class StubClient:
            def call(self, method, params=None, timeout=20):
                return {"data": base64.b64encode(payload).decode()}

        session = BrowserSession(StubClient())
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "big.png")
            session.screenshot(shot)
            with open(shot, "rb") as handle:
                self.assertEqual(handle.read(), payload)

    def test_bad_base64_is_browser_error_and_saves_nothing(self):
        class StubClient:
            def call(self, method, params=None, timeout=20):
                return {"data": "abc"}  # not a multiple of 4

        session = BrowserSession(StubClient())
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "bad.png")
            with self.assertRaises(BrowserError):
                session.screenshot(shot)
            self.assertFalse(os.path.exists(shot))

    def test_non_png_still_rejected(self):
        class StubClient:
            def call(self, method, params=None, timeout=20):
                return {"data": base64.b64encode(b"not a png").decode()}

        session = BrowserSession(StubClient())
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "x.png")
            with self.assertRaises(BrowserError):
                session.screenshot(shot)
            self.assertFalse(os.path.exists(shot))


# ----------------------------------------------------------------------
# G-S5: element snapshots must not capture password values
# ----------------------------------------------------------------------

class ElementsJsTests(unittest.TestCase):
    def test_js_guards_password_and_hidden(self):
        self.assertIn("password", _ELEMENTS_JS)
        self.assertIn("hidden", _ELEMENTS_JS)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_masks_password_values(self):
        # Run the real _ELEMENTS_JS in node against a stub DOM:
        # password and hidden values come back empty, other
        # fields keep their text.
        harness = """
const elements = [
  {tagName: 'INPUT', type: 'password', value: 's3cret-pw', innerText: '',
   placeholder: '', getAttribute: (n) => n === 'type' ? 'password' : (n === 'name' ? 'pw' : '')},
  {tagName: 'INPUT', type: 'hidden', value: 'tok123', innerText: '',
   placeholder: '', getAttribute: (n) => n === 'type' ? 'hidden' : ''},
  {tagName: 'INPUT', type: 'text', value: 'hello', innerText: '',
   placeholder: '', getAttribute: (n) => n === 'type' ? 'text' : ''},
  {tagName: 'BUTTON', type: 'submit', value: '', innerText: 'Go',
   placeholder: '', getAttribute: () => ''},
];
global.document = {querySelectorAll: () => elements};
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
global.window = {};
const out = eval(%s);
console.log(out);
""" % json.dumps(_ELEMENTS_JS)
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "elements.js")
            with open(script, "w") as handle:
                handle.write(harness)
            proc = subprocess.run(["node", script], capture_output=True,
                                  text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        found = json.loads(proc.stdout)
        by_type = {el["type"]: el for el in found}
        self.assertEqual(by_type["password"]["text"], "")
        self.assertEqual(by_type["hidden"]["text"], "")
        self.assertEqual(by_type["text"]["text"], "hello")
        self.assertEqual(found[3]["text"], "Go")


# ----------------------------------------------------------------------
# G-S9: navigate is fenced
# ----------------------------------------------------------------------

class NavigateFenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = _FakeEngine()

    def tearDown(self):
        self.engine.stop()

    def test_navigate_refuses_local(self):
        with BrowserSession.start(engine=self.engine.url) as session:
            with self.assertRaises(BrowserError) as caught:
                session.navigate("http://127.0.0.1:9/")
            self.assertIn("local addresses", str(caught.exception))
        self.assertEqual(self.engine.navigated_urls(), [])

    def test_navigate_allows_local_when_flagged(self):
        with BrowserSession.start(engine=self.engine.url) as session:
            session.navigate("http://127.0.0.1:9/", allow_local=True)
        self.assertEqual(self.engine.navigated_urls(),
                         ["http://127.0.0.1:9/"])

    def test_navigate_public_name_still_works(self):
        with BrowserSession.start(engine=self.engine.url) as session:
            session.navigate("http://fake.test/page")
        self.assertEqual(self.engine.navigated_urls(),
                         ["http://fake.test/page"])

    def test_page_threads_allow_local(self):
        with BrowserSession.start(engine=self.engine.url) as session:
            page = session.page("http://127.0.0.1:9/x",
                                allow_local=True)
        self.assertTrue(page.rendered)
        self.assertIn("Rendered Heading", page.markdown)


# ----------------------------------------------------------------------
# G-C15 / G-S11: verify URL quoting and the single-file server
# ----------------------------------------------------------------------

class _ProbingSession:
    """A session stub whose page() probes the throwaway server
    while it is alive, recording each status code."""

    def __init__(self):
        self.probes = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _probe(self, url):
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                self.probes[url] = response.status
        except urllib.error.HTTPError as exc:
            self.probes[url] = exc.code

    def page(self, url, allow_local=False):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self._probe(url)
        self._probe(origin + "/")
        self._probe(origin + "/other.txt")
        self._probe(origin + "/../outside.txt")
        return SimpleNamespace(markdown="probed")

    def ax_snapshot(self):
        return ""

    def screenshot(self, path):
        raise BrowserError("no screenshot in this test")


class VerifyServerTests(unittest.TestCase):
    def test_spaces_and_hash_quoted_in_url(self):
        # G-C15: a filename with a space and # makes a working URL.
        with tempfile.TemporaryDirectory() as tmp:
            name = "my page #1.html"
            with open(os.path.join(tmp, name), "w") as handle:
                handle.write("<html><body><h1>Spaced Page</h1></body></html>")

            def no_engine():
                raise BrowserError("no engine in this test")

            result = verify_in_browser(name, tmp, session_factory=no_engine)
        self.assertIn("my%20page%20%231.html", result["url"])
        self.assertIn("Spaced Page", result["text"])
        self.assertFalse(result["rendered"])

    def test_other_files_not_served(self):
        # G-S11: only the target file answers — no listings, no
        # other files, no traversal.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "index.html"), "w") as handle:
                handle.write("<html><body><h1>Target</h1></body></html>")
            with open(os.path.join(tmp, "other.txt"), "w") as handle:
                handle.write("secret other file")
            session = _ProbingSession()
            result = verify_in_browser("index.html", tmp,
                                       session_factory=lambda: session)
        self.assertTrue(result["rendered"])
        self.assertEqual(session.probes[result["url"]], 200)
        origin = result["url"].rsplit("/", 1)[0]
        self.assertEqual(session.probes[origin + "/"], 404)
        self.assertEqual(session.probes[origin + "/other.txt"], 404)
        self.assertEqual(session.probes[origin + "/../outside.txt"], 404)

    def test_symlink_escape_refused(self):
        # G-S11: a symlink inside the workspace pointing outside
        # is refused — the check resolves symlinks first.
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            workspace = os.path.join(tmp, "workspace")
            os.makedirs(outside)
            os.makedirs(workspace)
            with open(os.path.join(outside, "secret.txt"), "w") as handle:
                handle.write("outside secret")
            os.symlink(os.path.join(outside, "secret.txt"),
                       os.path.join(workspace, "link.html"))

            def no_engine():
                raise BrowserError("no engine in this test")

            with self.assertRaises(BrowserError) as caught:
                verify_in_browser("link.html", workspace,
                                  session_factory=no_engine)
            self.assertIn("outside the project folder",
                          str(caught.exception))

    def test_symlink_inside_allowed(self):
        # A symlink to another file inside the workspace serves it.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "real.html"), "w") as handle:
                handle.write("<html><body><h1>Real Page</h1></body></html>")
            os.symlink(os.path.join(tmp, "real.html"),
                       os.path.join(tmp, "link.html"))

            def no_engine():
                raise BrowserError("no engine in this test")

            result = verify_in_browser("link.html", tmp,
                                       session_factory=no_engine)
        self.assertIn("Real Page", result["text"])


# ----------------------------------------------------------------------
# G-T2: fence pinning — flip-to-local at pin time, and IPv6
# ----------------------------------------------------------------------

class _FakeHttpSocket:
    """Enough of a socket for http.client: sendall swallows the
    request, makefile serves the canned response."""

    def __init__(self, response):
        self._response = response
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode):
        import io
        return io.BytesIO(self._response)

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def gettimeout(self):
        return None

    def close(self):
        pass


class FencePinFlipTests(unittest.TestCase):
    _PUBLIC = "93.184.216.34"

    def setUp(self):
        self._orig_getaddrinfo = socket.getaddrinfo
        self._orig_create_connection = socket.create_connection
        self._orig_proxy_env = {}
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            if var in os.environ:
                self._orig_proxy_env[var] = os.environ.pop(var)
        self.targets = []

    def tearDown(self):
        socket.getaddrinfo = self._orig_getaddrinfo
        socket.create_connection = self._orig_create_connection
        for var, value in self._orig_proxy_env.items():
            os.environ[var] = value

    def _install_fakes(self, answers, responses):
        calls = {"n": 0}
        pending = list(responses)

        def fake_getaddrinfo(host, port, *args, **kwargs):
            calls["n"] += 1
            if callable(answers):
                ips = answers(host, calls["n"])
            else:
                ips = answers.get(host, [])
            out = []
            for ip in ips:
                family = (socket.AF_INET6 if ":" in ip
                          else socket.AF_INET)
                sockaddr = ((ip, 0, 0, 0) if family == socket.AF_INET6
                            else (ip, 0))
                out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
            return out

        def fake_create_connection(address, timeout=None,
                                   source_address=None):
            self.targets.append(address)
            if not pending:
                raise OSError("no canned response left")
            return _FakeHttpSocket(pending.pop(0))

        socket.getaddrinfo = fake_getaddrinfo
        socket.create_connection = fake_create_connection
        return calls

    def test_flip_after_check_cannot_steer_connection(self):
        # G-T2, updated for G-P3: there is no pin-time
        # resolution any more — the fence resolves once and the
        # checked address passes through as the pin. A DNS that
        # would flip to 127.0.0.1 on a second resolution is
        # never consulted again, so the fetch connects to the
        # checked public address.
        calls = self._install_fakes(
            lambda host, n: ([self._PUBLIC] if n == 1
                             else ["127.0.0.1"]),
            [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
             b"Content-Length: 5\r\n\r\nhello"])
        _final, _headers, body = http_get("http://flipping.example/")
        self.assertEqual(body, b"hello")
        self.assertEqual(self.targets, [(self._PUBLIC, 80)])
        self.assertEqual(calls["n"], 1)

    def test_ipv6_pin_connects_to_checked_address(self):
        # G-T2: an IPv6 answer pins like an IPv4 one — the socket
        # goes to the checked IPv6 address.
        self._install_fakes(
            {"example.org": ["2001:db8::1"]},
            [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
             b"Content-Length: 5\r\n\r\nhello"])
        _final, _headers, body = http_get("http://example.org/")
        self.assertEqual(body, b"hello")
        self.assertEqual(self.targets, [("2001:db8::1", 80)])

    def test_ipv6_flip_after_check_cannot_steer_connection(self):
        # G-T2, updated for G-P3: same single-resolution pin as
        # the IPv4 case — the flipped ::1 answer is never
        # consulted, so the fetch connects to the checked IPv6
        # address.
        calls = self._install_fakes(
            lambda host, n: (["2001:db8::1"] if n == 1 else ["::1"]),
            [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
             b"Content-Length: 5\r\n\r\nhello"])
        _final, _headers, body = http_get("http://flipping.example/")
        self.assertEqual(body, b"hello")
        self.assertEqual(self.targets, [("2001:db8::1", 80)])
        self.assertEqual(calls["n"], 1)


# ----------------------------------------------------------------------
# G-T5: barrier handling — captcha/paywall handle_page, scroll_page
# ----------------------------------------------------------------------

class _BarrierStubSession:
    def __init__(self, html, buttons=()):
        self._html = html
        self._buttons = list(buttons)
        self.clicked = []
        self.navigated = None
        self.evaluated = []

    def navigate(self, url):
        self.navigated = url

    def html(self):
        return self._html

    def elements(self):
        return [{"i": i, "tag": "button", "text": text}
                for i, text in enumerate(self._buttons)]

    def click(self, index):
        self.clicked.append(index)
        return True

    def _evaluate(self, expression):
        self.evaluated.append(expression)
        return True


class BarrierHandleTests(unittest.TestCase):
    def test_handle_page_captcha_reports_and_never_clears(self):
        # G-T5: a captcha page reports needs_human — and nothing
        # is clicked to clear it.
        html = ('<html><body><div class="g-recaptcha"></div>'
                '<p>Verify you are human to continue.</p></body></html>')
        session = _BarrierStubSession(html)
        result = barriers.handle_page(session, "https://example.org/x")
        self.assertEqual([b["kind"] for b in result["barriers"]],
                         ["captcha"])
        self.assertTrue(result["needs_human"])
        self.assertEqual(session.clicked, [])
        self.assertFalse(result["age_gate_cleared"])
        self.assertFalse(result["login_filled"])

    def test_handle_page_paywall_reports_without_needs_human(self):
        # G-T5: a paywall reports, but needs no human.
        html = ("<html><body><p>Subscribe to continue reading this "
                "article.</p></body></html>")
        session = _BarrierStubSession(html)
        result = barriers.handle_page(session, "https://example.org/x")
        self.assertEqual([b["kind"] for b in result["barriers"]],
                         ["paywall"])
        self.assertFalse(result["needs_human"])
        self.assertEqual(session.clicked, [])

    def test_scroll_page_scrolls_and_returns(self):
        # G-T5: scroll_page scrolls down step by step, then back
        # to the top.
        session = _BarrierStubSession("<html></html>")
        barriers.scroll_page(session, steps=3)
        self.assertEqual(
            session.evaluated,
            ["window.scrollBy(0, window.innerHeight); true"] * 3
            + ["window.scrollTo(0, 0); true"])

    def test_scroll_page_zero_steps_still_returns_to_top(self):
        session = _BarrierStubSession("<html></html>")
        barriers.scroll_page(session, steps=0)
        self.assertEqual(session.evaluated,
                         ["window.scrollTo(0, 0); true"])

    def test_handle_page_with_scroll_scrolls(self):
        session = _BarrierStubSession("<html><body><p>plain</p></body></html>")
        result = barriers.handle_page(session, "https://example.org/x",
                                      scroll=True)
        self.assertEqual(result["barriers"], [])
        self.assertEqual(len(session.evaluated), 4)


# ----------------------------------------------------------------------
# G-P8 / G-P9: PDF extraction memory bounds
# ----------------------------------------------------------------------

class PdfStreamTests(unittest.TestCase):
    def test_many_streams_bounded_memory(self):
        # G-P8: ten 2 MB streams are read one at a time, not all
        # held at once.
        body = b"%PDF-1.4\n" + b"".join(
            b"stream\n" + b"\0" * (2 * 1024 * 1024) + b"\nendstream\n"
            for _ in range(10))
        tracemalloc.start()
        try:
            text = extract_pdf_text(body)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(text, "")
        self.assertLess(peak, 10 * 1024 * 1024)

    def test_pdf_text_unchanged(self):
        # The streaming pass still extracts plain and CID text.
        content = b"BT /F1 12 Tf (Hello world) Tj ET"
        pdf = (b"%PDF-1.4\n"
               b"1 0 obj<</Length " + str(len(content)).encode()
               + b">>stream\n" + content + b"\nendstream endobj\n")
        self.assertEqual(extract_pdf_text(pdf), "Hello world")
        cmap = (b"/CIDInit/ProcSet findresource begin begincmap\n"
                b"2 beginbfchar\n<0001> <0048>\n<0002> <0069>\nendbfchar\n"
                b"endcmap")
        content = b"BT /F1 12 Tf <00010002> Tj ET"
        pdf = (b"%PDF-1.4\n"
               b"1 0 obj<</Length " + str(len(cmap)).encode()
               + b">>stream\n" + cmap + b"\nendstream endobj\n"
               b"2 0 obj<</Length " + str(len(content)).encode()
               + b">>stream\n" + content + b"\nendstream endobj\n")
        self.assertEqual(extract_pdf_text(pdf), "Hi")

    def test_cmap_bfrange_capped(self):
        # G-P9: ranges that would name 131,072 codes stop at the
        # 65,536 cap.
        cmap = (b"begincmap\n"
                b"1 beginbfrange\n<0000> <FFFF> <0041>\nendbfrange\n"
                b"1 beginbfrange\n<10000> <1FFFF> <0041>\nendbfrange\n"
                b"endcmap")
        mapping, _code_len = _parse_tounicode_cmap(cmap)
        self.assertEqual(len(mapping), 65536)

    def test_cmap_small_range_full(self):
        cmap = (b"begincmap\n"
                b"1 beginbfrange\n<0000> <0009> <0041>\nendbfrange\n"
                b"endcmap")
        mapping, _code_len = _parse_tounicode_cmap(cmap)
        self.assertEqual(len(mapping), 10)
        self.assertEqual(mapping[0], "A")
        self.assertEqual(mapping[9], "J")


# ----------------------------------------------------------------------
# G-P10: JSON pretty-print streams to the limit
# ----------------------------------------------------------------------

class _JsonHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class JsonPrefixTests(unittest.TestCase):
    def test_prefix_matches_dumps_slice(self):
        for doc in ({"a": [1, 2, 3]}, [1, 2, {"b": "x" * 100}],
                    {"n": list(range(1000))}):
            text = json.dumps(doc)
            for limit in (10, 100, 1000, 200000):
                self.assertEqual(_pretty_json_prefix(text, limit),
                                 json.dumps(json.loads(text),
                                            indent=2)[:limit])

    def test_bad_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _pretty_json_prefix("{not json", 100)

    def test_fetch_any_json_truncated_to_limit(self):
        data = {"items": list(range(50000)), "note": "x" * 1000}
        text = json.dumps(data)
        _JsonHandler.body = text.encode()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with tempfile.TemporaryDirectory() as media:
                with mock.patch.dict(
                        os.environ, {"FORGE_BROWSER_MEDIA_DIR": media}):
                    result = fetch_any(base + "/big.json",
                                       allow_local=True)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.kind, "text")
        self.assertEqual(result.text,
                         json.dumps(json.loads(text), indent=2)[:200000])
        self.assertEqual(len(result.text), 200000)


if __name__ == "__main__":
    unittest.main()
