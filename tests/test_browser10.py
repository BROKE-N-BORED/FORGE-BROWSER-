"""Tests for the doorway (v0.13): the tiny local HTTP endpoint that
lets another program on this PC hand Forge Browser a browsing job.

The tests drive the real Doorway over real HTTP (stdlib urllib, the
way Fyrn will), against a fake CDP engine speaking the real
WebSocket protocol (the same shape as test_browser2's fake, copied
here so this file stands alone). No test touches the network or
the real ~/.forge: every doorway gets a temp base dir.
"""

import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from forge.browser import doorway as doorwaymod
from forge.browser.doorway import Doorway
from forge.browser.errors import BrowserError
from forge.browser.search import SearchResult


FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

FAKE_PAGE_HTML = """<html><head><title>Fake Title</title></head><body>
<h1>Rendered Heading</h1><p>The doorway test page.</p>
</body></html>"""

CAPTCHA_HTML = """<html><head><title>Are you human?</title></head><body>
<div class="g-recaptcha"></div><p>Please verify you are human to continue.</p>
</body></html>"""


# Fake CDP WebSocket engine (raw sockets, RFC 6455 server side)
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


class _FakeCDPServer(threading.Thread):
    """A fake engine: answers CDP over WebSocket. page_html is the
    page it serves (tests swap in a captcha page); last_url is the
    URL of the latest navigate, which location.href reports back."""

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.page_html = FAKE_PAGE_HTML
        self.last_url = "http://fake.test/page"
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

    def _reply(self, method, params):
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.createBrowserContext":
            return {"browserContextId": "CTX1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method in ("Page.enable", "Accessibility.enable",
                      "Target.closeTarget", "Target.disposeBrowserContext"):
            return {}
        if method == "Page.navigate":
            self.last_url = params.get("url") or self.last_url
            return {"frameId": "F1"}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(FAKE_PNG).decode()}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if "outerHTML" in expression:
                value = self.page_html
            elif "document.title" in expression:
                value = "Fake Title"
            elif "location.href" in expression:
                value = self.last_url
            else:
                value = True
            return {"result": {"type": "string", "value": value}}
        return {}

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
            result = self._reply(method, params)
            _ws_send(conn, json.dumps({"id": message["id"],
                                       "result": result}))
            if method == "Page.navigate":
                _ws_send(conn, json.dumps(
                    {"method": "Page.loadEventFired", "params": {},
                     "sessionId": "S1"}))


class _VersionHandler(http.server.BaseHTTPRequestHandler):
    ws_port = 0

    def do_GET(self):
        body = json.dumps({
            "webSocketDebuggerUrl":
                f"ws://127.0.0.1:{self.ws_port}/devtools/browser/fake"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _DoorwayTestBase(unittest.TestCase):
    """A doorway on a free port with a temp base dir, backed by the
    fake engine. The client side is plain urllib, like Fyrn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-doorway-test-")
        self.fake = _FakeCDPServer()
        self.fake.start()
        _VersionHandler.ws_port = self.fake.port
        self.http = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _VersionHandler)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever,
                         daemon=True).start()
        self.engine_url = f"http://127.0.0.1:{self.http_port}"
        self.doorway = Doorway(port=0, base_dir=self.tmp,
                               engine=self.engine_url)
        self.base = (f"http://127.0.0.1:{self.doorway.port}")

    def tearDown(self):
        self.doorway.shutdown()
        self.http.shutdown()
        self.http.server_close()
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, method, path, payload=None, raw=None):
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _post_task(self, payload):
        return self._request("POST", "/task", payload=payload)

    def _wait_for(self, task_id, states=("done", "failed"), timeout=30):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, last = self._request("GET", f"/task/{task_id}")
            self.assertEqual(status, 200)
            if last["state"] in states:
                return last
            time.sleep(0.05)
        self.fail(f"task {task_id} never reached {states}: {last}")


class DoorwayLifecycleTests(_DoorwayTestBase):
    def test_open_task_end_to_end(self):
        status, body = self._post_task(
            {"type": "open", "url": "http://fake.test/page"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "queued")
        task_id = body["task_id"]
        self.assertRegex(task_id, r"^[0-9a-f]+$")
        final = self._wait_for(task_id)
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["phase"], "done")
        self.assertIsNone(final["error"])
        result = final["result"]
        self.assertEqual(result["final_url"], "http://fake.test/page")
        self.assertIn("Rendered Heading", result["markdown"])
        self.assertFalse(result["truncated"])
        # The files on disk: a valid PNG and the full markdown.
        with open(result["screenshot_path"], "rb") as handle:
            shot = handle.read()
        self.assertEqual(shot, FAKE_PNG)
        with open(result["markdown_path"], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Rendered Heading", text)
        self.assertIn("The doorway test page.", text)
        # The files live under this task's own folder.
        self.assertTrue(result["screenshot_path"].startswith(
            os.path.join(self.tmp, task_id)))
        self.assertTrue(result["markdown_path"].startswith(
            os.path.join(self.tmp, task_id)))

    def test_status_shape_while_running(self):
        # The worker is held inside the engine start, so polling
        # during the run shows the live shape: running, the phase
        # naming the URL, current_url set, no result yet.
        real_start = doorwaymod.BrowserSession.start
        started = threading.Event()
        release = threading.Event()

        def blocking_start(*args, **kwargs):
            started.set()
            release.wait(timeout=30)
            return real_start(*args, **kwargs)

        with mock.patch.object(doorwaymod.BrowserSession, "start",
                               side_effect=blocking_start):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            task_id = body["task_id"]
            self.assertTrue(started.wait(timeout=10))
            _s, current = self._request("GET", f"/task/{task_id}")
            self.assertEqual(current["state"], "running")
            self.assertEqual(current["phase"],
                             "opening http://fake.test/page")
            self.assertEqual(current["current_url"],
                             "http://fake.test/page")
            self.assertIsNone(current["result"])
            self.assertIsNone(current["error"])
            release.set()
            final = self._wait_for(task_id)
        self.assertEqual(final["state"], "done")

    def test_research_task_opens_best_result(self):
        # The mock stays up until the task finishes: the worker
        # runs on its own thread, after the submit returns.
        with mock.patch.object(
                doorwaymod, "search",
                return_value=[SearchResult(
                    title="Fake", url="http://fake.test/page",
                    snippet="", engine="stub")]):
            status, body = self._post_task(
                {"type": "research", "query": "doorway test"})
            self.assertEqual(status, 200)
            final = self._wait_for(body["task_id"])
        self.assertEqual(final["state"], "done")
        self.assertIn("Rendered Heading",
                      final["result"]["markdown"])

    def test_research_with_no_results_fails_plainly(self):
        with mock.patch.object(doorwaymod, "search", return_value=[]):
            status, body = self._post_task(
                {"type": "research", "query": "nothing anywhere"})
            self.assertEqual(status, 200)
            final = self._wait_for(body["task_id"])
        self.assertEqual(final["state"], "failed")
        self.assertIn("no results", final["error"])

    def test_research_skips_local_results(self):
        with mock.patch.object(
                doorwaymod, "search",
                return_value=[
                    SearchResult(title="Local", url="http://127.0.0.1:9/x",
                                 snippet="", engine="stub"),
                    SearchResult(title="Fake", url="http://fake.test/page",
                                 snippet="", engine="stub")]):
            status, body = self._post_task(
                {"type": "research", "query": "doorway test"})
            self.assertEqual(status, 200)
            final = self._wait_for(body["task_id"])
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["result"]["final_url"],
                         "http://fake.test/page")

    def test_captcha_page_fails_naming_the_barrier(self):
        self.fake.page_html = CAPTCHA_HTML
        status, body = self._post_task(
            {"type": "open", "url": "http://fake.test/page"})
        self.assertEqual(status, 200)
        final = self._wait_for(body["task_id"])
        self.assertEqual(final["state"], "failed")
        self.assertIn("human check", final["error"])
        self.assertIsNone(final["result"])


class DoorwayValidationTests(_DoorwayTestBase):
    def test_open_needs_http_url(self):
        status, body = self._post_task(
            {"type": "open", "url": "ftp://example.org/x"})
        self.assertEqual(status, 400)
        self.assertIn("http(s)", body["error"])

    def test_open_needs_a_url(self):
        status, body = self._post_task({"type": "open"})
        self.assertEqual(status, 400)
        self.assertIn("url", body["error"])

    def test_open_refuses_loopback(self):
        status, body = self._post_task(
            {"type": "open", "url": "http://127.0.0.1:9/admin"})
        self.assertEqual(status, 400)
        self.assertIn("local", body["error"].lower())

    def test_open_refuses_localhost_name(self):
        status, body = self._post_task(
            {"type": "open", "url": "http://localhost/admin"})
        self.assertEqual(status, 400)

    def test_open_refuses_lan_address(self):
        status, body = self._post_task(
            {"type": "open", "url": "http://192.168.1.1/"})
        self.assertEqual(status, 400)

    def test_research_needs_a_query(self):
        status, body = self._post_task({"type": "research", "query": "  "})
        self.assertEqual(status, 400)
        self.assertIn("query", body["error"])

    def test_unknown_type_refused(self):
        status, body = self._post_task({"type": "browse", "url": "http://fake.test/page"})
        self.assertEqual(status, 400)
        self.assertIn("open or research", body["error"])

    def test_bad_json_refused(self):
        status, body = self._request("POST", "/task", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("not valid JSON", body["error"])

    def test_non_object_body_refused(self):
        status, body = self._request("POST", "/task", raw=b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertIn("JSON object", body["error"])

    def test_unknown_task_id_404(self):
        status, body = self._request("GET", "/task/no-such-id")
        self.assertEqual(status, 404)
        self.assertIn("no task", body["error"])

    def test_unknown_endpoint_404(self):
        status, body = self._request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("unknown endpoint", body["error"])

    def test_wrong_method_405(self):
        status, body = self._request("DELETE", "/task/abc")
        self.assertEqual(status, 405)
        status, body = self._request("PUT", "/tasks")
        self.assertEqual(status, 405)

    def test_tasks_list_most_recent_first(self):
        ids = []
        for _ in range(3):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            ids.append(body["task_id"])
        for task_id in ids:
            self._wait_for(task_id)
        status, body = self._request("GET", "/tasks")
        self.assertEqual(status, 200)
        listed = [t["task_id"] for t in body["tasks"]]
        self.assertEqual(listed, list(reversed(ids)))
        self.assertEqual(body["tasks"][0]["state"], "done")


class DoorwayBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-doorway-bind-")
        self.doorways = []

    def tearDown(self):
        for doorway in self.doorways:
            doorway.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doorway(self, **kwargs):
        kwargs.setdefault("base_dir", self.tmp)
        doorway = Doorway(**kwargs)
        self.doorways.append(doorway)
        return doorway

    def test_port_file_holds_the_bound_port(self):
        doorway = self._doorway(port=0)
        with open(doorway.port_file, encoding="utf-8") as handle:
            self.assertEqual(int(handle.read().strip()), doorway.port)

    def test_port_file_removed_on_shutdown(self):
        doorway = self._doorway(port=0)
        path = doorway.port_file
        self.assertTrue(os.path.isfile(path))
        doorway.shutdown()
        self.assertFalse(os.path.exists(path))

    def test_port_conflict_falls_to_next_port(self):
        first = self._doorway(port=0)
        taken = first.port
        second_dir = os.path.join(self.tmp, "second")
        second = Doorway(port=taken, base_dir=second_dir)
        self.doorways.append(second)
        self.assertNotEqual(second.port, taken)
        self.assertEqual(second.port, taken + 1)

    def test_env_port_honored(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
        sock.close()
        with mock.patch.dict(os.environ,
                             {"FORGE_BROWSER_DOORWAY_PORT": str(free)}):
            doorway = self._doorway()
        self.assertEqual(doorway.port, free)

    def test_env_port_bad_value_refused(self):
        with mock.patch.dict(os.environ,
                             {"FORGE_BROWSER_DOORWAY_PORT": "not-a-port"}):
            with self.assertRaises(BrowserError):
                self._doorway()

    def test_cli_port_beats_env_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
        sock.close()
        with mock.patch.dict(os.environ,
                             {"FORGE_BROWSER_DOORWAY_PORT": "1"}):
            doorway = self._doorway(port=free)
        self.assertEqual(doorway.port, free)

    def test_non_loopback_host_refused(self):
        for host in ("0.0.0.0", "192.168.1.1", "8.8.8.8", "example.org"):
            with self.assertRaises(BrowserError, msg=host):
                self._doorway(host=host, port=0)

    def test_localhost_binds_loopback(self):
        doorway = self._doorway(host="localhost", port=0)
        self.assertEqual(doorway.host, "127.0.0.1")

    def test_out_of_range_port_refused(self):
        with self.assertRaises(BrowserError):
            self._doorway(port=70000)
        with self.assertRaises(BrowserError):
            self._doorway(port=-1)


class DoorwayQueueTests(_DoorwayTestBase):
    def test_second_task_waits_queued_then_runs_in_order(self):
        # The first task is held inside the engine start; the
        # second must sit queued (not fail) until the first
        # finishes, then run and finish after it.
        real_start = doorwaymod.BrowserSession.start
        started = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_start(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(timeout=30)
            return real_start(*args, **kwargs)

        with mock.patch.object(doorwaymod.BrowserSession, "start",
                               side_effect=blocking_start):
            _s, first = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            self.assertTrue(started.wait(timeout=10))
            _s, second = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            # The second task stays queued while the first runs.
            for _ in range(5):
                _s, current = self._request(
                    "GET", f"/task/{second['task_id']}")
                self.assertEqual(current["state"], "queued")
                self.assertEqual(current["phase"], "waiting")
                time.sleep(0.1)
            release.set()
            # Completion order: the first task finishes first.
            done_order = []
            deadline = time.monotonic() + 30
            while (len(done_order) < 2
                   and time.monotonic() < deadline):
                for task_id in (first["task_id"], second["task_id"]):
                    if task_id in done_order:
                        continue
                    _s, current = self._request("GET", f"/task/{task_id}")
                    if current["state"] == "done":
                        done_order.append(task_id)
                time.sleep(0.02)
            self.assertEqual(done_order,
                             [first["task_id"], second["task_id"]])

    def test_shutdown_marks_queued_task_failed(self):
        real_start = doorwaymod.BrowserSession.start
        started = threading.Event()
        release = threading.Event()

        def blocking_start(*args, **kwargs):
            started.set()
            release.wait(timeout=30)
            return real_start(*args, **kwargs)

        with mock.patch.object(doorwaymod.BrowserSession, "start",
                               side_effect=blocking_start):
            _s, first = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            self.assertTrue(started.wait(timeout=10))
            _s, second = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            # Shut down while the first task runs: the shutdown
            # waits for it, then marks the queued one failed.
            stopper = threading.Thread(target=self.doorway.shutdown,
                                       daemon=True)
            stopper.start()
            time.sleep(0.5)
            release.set()
            stopper.join(timeout=20)
            self.assertFalse(stopper.is_alive())
            final = self.doorway.status(second["task_id"])
            self.assertEqual(final["state"], "failed")
            self.assertIn("stopped before this task ran",
                          final["error"])

    def test_concurrent_shutdown_waits_for_cleanup(self):
        # Two threads shutting down at once: the second waits for
        # the first to finish instead of returning early, so the
        # port file is really gone when both return.
        real_start = doorwaymod.BrowserSession.start
        started = threading.Event()
        release = threading.Event()

        def blocking_start(*args, **kwargs):
            started.set()
            release.wait(timeout=30)
            return real_start(*args, **kwargs)

        with mock.patch.object(doorwaymod.BrowserSession, "start",
                               side_effect=blocking_start):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            self.assertTrue(started.wait(timeout=10))
            first = threading.Thread(target=self.doorway.shutdown,
                                     daemon=True)
            second = threading.Thread(target=self.doorway.shutdown,
                                      daemon=True)
            first.start()
            time.sleep(0.3)
            second.start()
            time.sleep(0.5)
            # Neither shutdown has finished: the worker is held.
            self.assertTrue(first.is_alive())
            self.assertTrue(second.is_alive())
            release.set()
            first.join(timeout=20)
            second.join(timeout=20)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertFalse(os.path.exists(self.doorway.port_file))

    def test_tasks_list_is_capped(self):
        # A doorway with a 3-task cap: after 5 tasks, only the 3
        # most recent are listed.
        self.doorway.shutdown()
        small = Doorway(port=0, base_dir=self.tmp,
                        engine=self.engine_url, max_tasks=3)
        self.doorway = small
        self.base = f"http://127.0.0.1:{small.port}"
        ids = []
        for _ in range(5):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            ids.append(body["task_id"])
        for task_id in ids:
            self._wait_for(task_id)
        status, body = self._request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([t["task_id"] for t in body["tasks"]],
                         list(reversed(ids[-3:])))

    def test_prune_drops_old_finished_records(self):
        # With no age protection, the oldest finished records are
        # dropped once the cap is passed; the newest stay pollable.
        self.doorway.shutdown()
        small = Doorway(port=0, base_dir=self.tmp,
                        engine=self.engine_url, max_tasks=3,
                        prune_after_s=0)
        self.doorway = small
        self.base = f"http://127.0.0.1:{small.port}"
        ids = []
        for _ in range(5):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            ids.append(body["task_id"])
        self._wait_for(ids[-1])
        self.assertIsNone(small.status(ids[0]))
        self.assertIsNone(small.status(ids[1]))
        self.assertIsNotNone(small.status(ids[-1]))
        status, body = self._request("GET", "/tasks")
        self.assertEqual([t["task_id"] for t in body["tasks"]],
                         list(reversed(ids[-3:])))

    def test_markdown_truncation_note(self):
        # With a tiny inline cap, the JSON markdown is cut with a
        # plain note while page.md keeps the full text.
        with mock.patch.object(doorwaymod, "MARKDOWN_INLINE_CAP", 10):
            _s, body = self._post_task(
                {"type": "open", "url": "http://fake.test/page"})
            final = self._wait_for(body["task_id"])
        self.assertEqual(final["state"], "done")
        result = final["result"]
        self.assertTrue(result["truncated"])
        self.assertIn("cut here", result["markdown"])
        self.assertIn("see page.md", result["markdown"])
        with open(result["markdown_path"], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Rendered Heading", text)
        self.assertIn("The doorway test page.", text)
        self.assertNotIn("cut here", text)


class DoorwayCliTests(unittest.TestCase):
    def test_doorway_help_names_port_and_host(self):
        import contextlib
        import io
        from forge.browser.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                main(["doorway", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--port", out.getvalue())
        self.assertIn("--host", out.getvalue())

    def test_doorway_bad_port_exits_2(self):
        from forge.browser.cli import main
        with self.assertRaises(SystemExit) as caught:
            main(["doorway", "--port", "not-a-port"])
        self.assertEqual(caught.exception.code, 2)

    def test_doorway_bad_host_exits_2(self):
        from forge.browser.cli import main
        self.assertEqual(main(["doorway", "--host", "0.0.0.0"]), 2)

    def test_doorway_out_of_range_port_exits_2(self):
        from forge.browser.cli import main
        self.assertEqual(main(["doorway", "--port", "70000"]), 2)


if __name__ == "__main__":
    unittest.main()
