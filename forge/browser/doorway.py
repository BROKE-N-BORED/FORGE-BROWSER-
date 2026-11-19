"""The doorway: a tiny local HTTP endpoint so another program on the
same PC (Fyrn, the local model runner) can hand Forge Browser a
browsing job and get the result back.

Started with `python3 -m forge.browser doorway`. No window, no Qt:
the doorway runs from the CLI and uses the engine room only.

It binds 127.0.0.1 only, so only programs on this machine can reach
it. There is no auth, no key, no token: local-only binding is the
protection, per the order that built it. A --host flag exists but
accepts loopback addresses only and refuses anything else plainly.

Endpoints (JSON in, JSON out):
  POST /task       submit {"type": "open", "url": ...} or
                   {"type": "research", "query": ...}; answers
                   {"task_id": ..., "status": "queued"}
  GET  /task/<id>  live status the client polls while a task runs
  GET  /tasks      recent tasks, most recent first, capped

Tasks run ONE AT A TIME in submission order. A second task waits
queued; it does not fail. Each task opens a fresh engine session
(no cookies or state from any earlier task), navigates, reads the
rendered page as markdown, and takes a real PNG screenshot. The
screenshot lives at ~/.forge/doorway/<task-id>/shot.png and is
updated at each navigation step, so a view window can show the
live page by polling the file; the full markdown is written to
~/.forge/doorway/<task-id>/page.md. The inline "markdown" in the
result JSON is capped (see MARKDOWN_INLINE_CAP); the full text is
always in page.md.

On a page that needs a human (captcha, login), the task fails
plainly naming the barrier. The doorway never solves human checks,
same as the app.

The actual bound port is written to ~/.forge/doorway/port (a plain
file, just the number) so a client can discover the address. When
the requested port is taken, the doorway tries the next few ports.
"""

import errno
import http.server
import ipaddress
import json
import os
import queue
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.parse

from ._http import check_fetch_url
from .barriers import detect_barriers
from .cdp import BrowserSession
from .errors import BrowserError
from .search import search

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18787
PORT_ENV = "FORGE_BROWSER_DOORWAY_PORT"
# How many ports past the requested one to try when it is taken.
PORT_TRIES = 10
# A task submission is a URL or a query: tiny. Anything bigger is
# not a task.
MAX_BODY_BYTES = 65536
# How many task records the doorway keeps (GET /tasks and memory).
MAX_TASKS = 50
# A finished task stays pollable for at least this long before the
# prune may drop it, so a client polling a task it just submitted
# never gets a 404 for a task that finished a moment ago.
PRUNE_AFTER_S = 3600
# The inline markdown in the result JSON is capped at this many
# characters (RAM protection: the JSON answer holds it whole).
# The full text is always in page.md.
MARKDOWN_INLINE_CAP = 20000
# The markdown kept in page.md is capped at this many characters
# (same reason: a pathological page must not fill the disk or the
# reply). Real pages are far smaller.
PAGE_MARKDOWN_CAP = 1000000


def doorway_dir():
    """~/.forge/doorway: the port file and one folder per task."""
    return os.path.join(os.path.expanduser("~"), ".forge", "doorway")


def resolve_port(cli_port=None):
    """The port to listen on: the --port flag wins, then the
    FORGE_BROWSER_DOORWAY_PORT env var, then 18787. 0 means any
    free port (the OS picks). Raises BrowserError, in plain
    words, for a value that is not a port at all."""
    if cli_port is not None:
        _check_port_range(cli_port)
        return cli_port
    raw = os.environ.get(PORT_ENV, "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            raise BrowserError(
                f"{PORT_ENV} is not a port number: {raw!r}") from None
        _check_port_range(port)
        return port
    return DEFAULT_PORT


def _check_port_range(port):
    if not 0 <= port <= 65535:
        raise BrowserError(f"port {port} is out of range (0-65535)")


def _resolve_host(host):
    """(host, is_ipv6). Loopback only: anything else is refused
    plainly. "localhost" normalizes to 127.0.0.1."""
    if host is None or not str(host).strip():
        return DEFAULT_HOST, False
    text = str(host).strip()
    if text.lower() in ("localhost", "localhost."):
        return "127.0.0.1", False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise BrowserError(
            "the doorway only binds loopback addresses, "
            f"refused {host!r}") from None
    if not address.is_loopback:
        raise BrowserError(
            "the doorway only binds loopback addresses, "
            f"refused {host!r}")
    return text, isinstance(address, ipaddress.IPv6Address)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _make_server(host, port, ipv6):
    """A doorway HTTP server bound to (host, port)."""
    if ipv6:
        class _V6Server(_Server):
            address_family = socket.AF_INET6
        return _V6Server((host, port, 0, 0), _DoorwayHandler)
    return _Server((host, port), _DoorwayHandler)


def _bind(host, port, ipv6):
    """(server, bound_port). Port 0 binds any free port; any other
    port tries the next few when it is taken. Only an address-in-use
    failure moves to the next port; any other bind error fails
    plainly at once."""
    if port == 0:
        server = _make_server(host, 0, ipv6)
        return server, server.server_address[1]
    last = None
    for offset in range(PORT_TRIES):
        try:
            server = _make_server(host, port + offset, ipv6)
            return server, port + offset
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise BrowserError(
                    f"could not listen on {host}:{port} ({exc})") from exc
            last = exc
    raise BrowserError(
        f"no free port from {port} to {port + PORT_TRIES - 1} ({last})")


def _barrier_message(barrier):
    """The plain failure for a page that needs a human."""
    kind = barrier.get("kind")
    if kind == "captcha":
        return ("the page asks for a human check (CAPTCHA), and the "
                "doorway never solves those")
    if kind == "login":
        return ("the page wants a login, and the doorway has no "
                "login for it")
    return barrier.get("note") or (
        "the page needs a human, and the doorway cannot do that part")


def _barrier_note(barrier):
    """The plain note for a barrier that does not stop the task
    (paywall, age gate): what was read may not be the whole page."""
    kind = barrier.get("kind")
    if kind == "paywall":
        return ("the page may be behind a paywall, so what was read "
                "could be preview text only")
    if kind == "age_gate":
        return ("the page shows an age gate, so some content may not "
                "have been read")
    return None


class _HttpError(Exception):
    """An error that is itself the HTTP answer (status + plain
    message). The handler turns it into JSON, never a traceback."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class _DoorwayHandler(http.server.BaseHTTPRequestHandler):
    """One HTTP request. Every answer is JSON; every failure is a
    plain JSON error with the right status, never a traceback and
    never a crash."""

    protocol_version = "HTTP/1.1"
    server_version = "ForgeBrowserDoorway"

    def log_message(self, *args):
        # The terminal stays clean: task lifecycle lines come from
        # the doorway itself, not from per-request logging.
        pass

    # -- plumbing ---------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            # The client went away mid-answer; nothing to do.
            pass

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            raise _HttpError(400, "the request had no body")
        try:
            length = int(length)
        except ValueError:
            raise _HttpError(400, "the request length was not a number")
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, "the request body is too large for a task")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise _HttpError(400, "the request body was empty")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _HttpError(400, "the request body was not valid JSON")
        if not isinstance(data, dict):
            raise _HttpError(400, "the request body must be a JSON object")
        return data

    # -- routing ----------------------------------------------------

    def _dispatch(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            doorway = self.server.doorway
            if self.command == "GET" and path == "/tasks":
                self._send_json(200, {"tasks": doorway.list_tasks()})
            elif self.command == "GET" and path.startswith("/task/"):
                task_id = path[len("/task/"):]
                status = doorway.status(task_id) if task_id else None
                if status is None:
                    raise _HttpError(404, "no task with that id")
                self._send_json(200, status)
            elif self.command == "POST" and path == "/task":
                task_id = doorway.submit(self._read_json_body())
                self._send_json(200, {"task_id": task_id,
                                      "status": "queued"})
            elif path in ("/task", "/tasks") or path.startswith("/task/"):
                raise _HttpError(
                    405, f"{self.command} is not supported on this endpoint")
            else:
                raise _HttpError(404, "unknown endpoint")
        except _HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except BrowserError as exc:
            # A refused submission (bad URL, local address, empty
            # query) is a client error, in plain words.
            self._send_json(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self._send_json(
                500, {"error": "the doorway hit an internal error"})

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()


class Doorway:
    """The doorway server: one HTTP endpoint, one worker thread,
    one task at a time. Constructing it binds the port, writes the
    port file, and starts the worker and the HTTP server thread;
    serve_forever() blocks until shutdown()."""

    def __init__(self, host=None, port=None, base_dir=None,
                 engine="auto", max_tasks=MAX_TASKS,
                 prune_after_s=PRUNE_AFTER_S):
        self._host, self._ipv6 = _resolve_host(host)
        self._engine = engine
        self._max_tasks = max_tasks
        self._prune_after_s = prune_after_s
        self._base_dir = base_dir or doorway_dir()
        os.makedirs(self._base_dir, exist_ok=True)
        self._server, self._bound_port = _bind(
            self._host, resolve_port(port), self._ipv6)
        self._server.doorway = self
        self._write_port_file()
        self._tasks = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._stopping = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        self._shutdown_done = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop,
                                        daemon=True)
        self._worker.start()
        # The HTTP server runs on its own thread from construction:
        # the doorway answers requests as soon as it exists, and
        # shutdown() can stop it (server.shutdown() deadlocks when
        # serve_forever never ran, so the thread must exist before
        # any shutdown path can fire).
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2}, daemon=True)
        self._server_thread.start()

    # -- properties -------------------------------------------------

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        """The port actually bound (the requested one, or the next
        free one when it was taken)."""
        return self._bound_port

    @property
    def base_dir(self):
        return self._base_dir

    @property
    def port_file(self):
        return os.path.join(self._base_dir, "port")

    # -- port file --------------------------------------------------

    def _write_port_file(self):
        try:
            with open(self.port_file, "w", encoding="utf-8") as handle:
                handle.write(str(self._bound_port) + "\n")
        except OSError as exc:
            try:
                self._server.server_close()
            except Exception:
                pass
            raise BrowserError(
                f"could not write the port file {self.port_file} "
                f"({exc})") from exc

    def _remove_port_file(self):
        try:
            os.unlink(self.port_file)
        except OSError:
            pass

    # -- tasks: submit / status / list ------------------------------

    def submit(self, payload):
        """Validate a task submission and queue it. Returns the task
        id. Raises BrowserError, in plain words, for anything
        refused: an unknown type, a bad URL, a local address, an
        empty query."""
        if not isinstance(payload, dict):
            raise BrowserError("the task must be a JSON object")
        kind = payload.get("type")
        if kind == "open":
            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                raise BrowserError("an open task needs a url")
            url = url.strip()
            if not url.startswith(("http://", "https://")):
                raise BrowserError(
                    f"open only takes http(s) pages, refused {url!r}")
            # The fence: no browsing the local machine through the
            # doorway. Local addresses are refused here, before the
            # task is even queued; the engine fences again at
            # navigate time.
            check_fetch_url(url)
            record = self._new_record("open", url=url)
        elif kind == "research":
            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise BrowserError(
                    "a research task needs a non-empty query")
            record = self._new_record("research", query=query.strip())
        else:
            raise BrowserError("a task needs a type: open or research")
        self._queue.put(record["task_id"])
        print(f"doorway: task {record['task_id']} ({kind}) queued",
              flush=True)
        return record["task_id"]

    def _new_record(self, kind, url=None, query=None):
        # Short, filename-safe ids: hex only, no user input in
        # paths, ever.
        task_id = secrets.token_hex(8)
        task_dir = os.path.join(self._base_dir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        record = {"task_id": task_id, "type": kind, "url": url,
                  "query": query, "dir": task_dir,
                  "state": "queued", "phase": "waiting",
                  "current_url": None, "result": None, "error": None,
                  "created": time.time(), "finished_at": None}
        with self._lock:
            self._tasks[task_id] = record
        return record

    def status(self, task_id):
        """The public status dict for one task, or None when no
        task has that id."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            return self._public(record)

    def list_tasks(self):
        """Recent tasks, most recent first, capped at max_tasks."""
        with self._lock:
            records = list(self._tasks.values())[-self._max_tasks:]
            return [self._public(record) for record in reversed(records)]

    @staticmethod
    def _public(record):
        return {"task_id": record["task_id"], "type": record["type"],
                "state": record["state"], "phase": record["phase"],
                "current_url": record["current_url"],
                "result": (dict(record["result"])
                           if record["result"] is not None else None),
                "error": record["error"]}

    def _prune(self):
        """Keep the task list bounded: drop the oldest finished
        records past max_tasks, but only ones finished at least
        prune_after_s ago, so a task that finished a moment ago
        stays pollable. Queued and running tasks are never
        dropped. (The task folders on disk stay until the person
        deletes them; only the listing is bounded.)"""
        now = time.monotonic()
        with self._lock:
            while len(self._tasks) > self._max_tasks:
                for task_id, record in self._tasks.items():
                    if record["state"] in ("done", "failed"):
                        finished = record.get("finished_at")
                        if (finished is not None
                                and now - finished >= self._prune_after_s):
                            del self._tasks[task_id]
                            break
                else:
                    break

    # -- the worker: one task at a time -----------------------------

    def _set_phase(self, record, phase, current_url=None):
        with self._lock:
            record["phase"] = phase
            if current_url is not None:
                record["current_url"] = current_url

    def _fail(self, record, message):
        with self._lock:
            record["state"] = "failed"
            record["phase"] = "failed"
            record["error"] = message
            record["finished_at"] = time.monotonic()
        print(f"doorway: task {record['task_id']} failed: {message}",
              flush=True)

    def _worker_loop(self):
        while True:
            task_id = self._queue.get()
            if task_id is None:
                self._queue.task_done()
                return
            with self._lock:
                record = self._tasks.get(task_id)
            if record is None:
                self._queue.task_done()
                continue
            if self._stopping.is_set():
                # Shutdown began while this task waited: it never
                # starts, it just says so plainly.
                self._fail(record,
                           "the doorway stopped before this task ran")
                self._queue.task_done()
                self._prune()
                continue
            try:
                with self._lock:
                    record["state"] = "running"
                    record["phase"] = "starting"
                result = self._run_task(record)
                with self._lock:
                    record["state"] = "done"
                    record["phase"] = "done"
                    record["result"] = result
                    record["error"] = None
                    record["finished_at"] = time.monotonic()
                print(f"doorway: task {task_id} done", flush=True)
            except BrowserError as exc:
                self._fail(record, str(exc))
            except Exception:
                # Never a traceback to the client: it goes to the
                # operator's terminal, the task just fails plainly.
                traceback.print_exc()
                self._fail(record,
                           "something went wrong while running the task")
            finally:
                self._queue.task_done()
                self._prune()

    def _run_task(self, record):
        if record["type"] == "open":
            return self._run_open(record, record["url"])
        self._set_phase(record, "searching")
        results = search(record["query"], max_results=5)
        if not results:
            raise BrowserError(
                f"no results for {record['query']!r}")
        # The best result first; a result that points at a local
        # address is skipped, not opened.
        for result in results:
            try:
                check_fetch_url(result.url)
            except BrowserError:
                continue
            return self._run_open(record, result.url)
        raise BrowserError(
            "the search results were all local addresses, so none "
            "could be opened")

    def _run_open(self, record, url):
        """Open one page in a fresh engine session: navigate,
        screenshot, read the rendered page as markdown. Returns the
        result dict."""
        task_dir = record["dir"]
        shot_path = os.path.join(task_dir, "shot.png")
        page_path = os.path.join(task_dir, "page.md")
        self._set_phase(record, "opening " + url, current_url=url)
        # fresh=True: every task gets its own browser state, so one
        # job can never see another job's cookies or storage.
        session = BrowserSession.start(engine=self._engine, fresh=True,
                                       timeout=30)
        try:
            session.navigate(url, timeout=30)
            # The live shot: written as soon as the page lands, so a
            # view window polling the file sees the page while the
            # read is still running.
            shot = self._try_screenshot(session, shot_path)
            self._set_phase(record, "reading the page")
            html = session.html()
            barriers = detect_barriers(html, url)
            human = [b for b in barriers if b.get("needs_human")]
            if human:
                raise BrowserError(_barrier_message(human[0]))
            page = session.page(max_chars=PAGE_MARKDOWN_CAP)
            # The final shot: the page as read, after any settling.
            if self._try_screenshot(session, shot_path):
                shot = True
            if not shot:
                raise BrowserError(
                    "the browser engine could not take a screenshot "
                    "of the page")
            markdown = page.markdown
            if page.truncated:
                markdown += ("\n\n[the page was longer than %d "
                             "characters; the rest was not kept]"
                             % PAGE_MARKDOWN_CAP)
            with open(page_path, "w", encoding="utf-8") as handle:
                handle.write(markdown)
            inline = markdown
            truncated = False
            if len(markdown) > MARKDOWN_INLINE_CAP:
                inline = markdown[:MARKDOWN_INLINE_CAP]
                inline += ("\n\n[cut here, the full text is %d "
                           "characters, see page.md]" % len(markdown))
                truncated = True
            result = {"final_url": page.final_url, "markdown": inline,
                      "markdown_path": page_path,
                      "screenshot_path": shot_path,
                      "truncated": truncated}
            notes = [_barrier_note(b) for b in barriers]
            notes = [note for note in notes if note]
            if notes:
                result["note"] = "; ".join(notes)
            with self._lock:
                record["current_url"] = page.final_url
            return result
        finally:
            session.close()

    @staticmethod
    def _try_screenshot(session, path):
        """A screenshot attempt that reports success instead of
        raising: the caller decides whether a missed shot matters."""
        try:
            session.screenshot(path)
            return True
        except BrowserError as exc:
            print(f"doorway: screenshot failed ({exc})",
                  file=sys.stderr, flush=True)
            return False

    # -- serve / stop ------------------------------------------------

    def serve_forever(self):
        """Block until shutdown() is called (the HTTP server itself
        runs on its own thread from construction)."""
        self._server_thread.join()

    def shutdown(self):
        """Stop serving, stop the worker, remove the port file.
        Idempotent: safe to call twice (the signal handler and the
        main thread both call it). A second caller waits for the
        shutdown already running to finish instead of returning
        early, or the process could exit before the port file is
        removed. Tasks still queued are marked failed, in plain
        words."""
        with self._shutdown_lock:
            if self._shut_down:
                already_running = True
            else:
                self._shut_down = True
                already_running = False
        if already_running:
            self._shutdown_done.wait(timeout=30)
            return
        try:
            self._stopping.set()
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server_thread.join(timeout=5)
            self._queue.put(None)
            self._worker.join(timeout=10)
            # Anything still queued never ran: say so, plainly.
            while True:
                try:
                    task_id = self._queue.get_nowait()
                except queue.Empty:
                    break
                if task_id is None:
                    continue
                with self._lock:
                    record = self._tasks.get(task_id)
                if record is not None and record["state"] == "queued":
                    self._fail(record,
                               "the doorway stopped before this task ran")
            self._remove_port_file()
        finally:
            self._shutdown_done.set()


def run_doorway(port=None, host=None, engine="auto"):
    """Build the doorway, serve until Ctrl+C or SIGTERM, shut down
    clean. Returns 0. Raises BrowserError for a bad host, a bad
    port, or a port file that cannot be written."""
    doorway = Doorway(host=host, port=port, engine=engine)
    print(f"Doorway listening on http://{doorway.host}:{doorway.port}")
    print(f"Port file: {doorway.port_file}")
    print("Tasks run one at a time, in submission order. "
          "Ctrl+C stops it.")

    def _stop_on_signal(signum, frame):
        # shutdown() blocks until serve_forever notices, so it runs
        # on its own thread: calling it in the handler would
        # deadlock the main thread it is trying to stop.
        threading.Thread(target=doorway.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _stop_on_signal)
    except (ValueError, OSError):
        # Not the main thread (tests drive Doorway directly). The
        # handler is a CLI nicety, not a requirement.
        pass
    try:
        doorway.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        doorway.shutdown()
    print("Doorway stopped.")
    return 0
