"""Item 2 — the rendered-page engine: CDP over the local WebSocket.

Talks the Chrome DevTools Protocol to a browser engine running on
this machine: a Chromium-family browser Forge launches headless, or
any CDP endpoint already serving (Lightpanda — Item 4 — Kitesurf-
local, or a browser the user started with --remote-debugging-port).
No network service, no key: the engine is a local process and the
protocol is a local WebSocket (forge/browser/_ws.py, standard
library only).

What a session can do:

* navigate(url) and wait for the load event
* html() / page() — the RENDERED page, converted by the same
  html_to_markdown the plain fetch uses, so JavaScript-built pages
  read the way a person sees them
* ax_snapshot() — the page as an indented accessibility tree (role +
  name lines), the compact shape a small local model can act on
* elements() — the interactive elements, numbered; click(i) and
  type_text(i, ...) act by number, the browser-use / playwright-mcp
  pattern, so a small model never has to write selectors
* screenshot(path) — a real PNG via Page.captureScreenshot,
  validated against the PNG signature before it is saved. Engine
  support varies: the Lightpanda nightly tested on 2026-11-11 did
  return a valid 1920x1080 PNG, with basic rendering (unstyled
  layout, some missing font glyphs); Chromium is the full-fidelity
  engine. An engine that cannot capture refuses in plain words.

Resource rules for an 8 GB machine: ONE browser process per session,
one page target, a temporary profile directory deleted on close,
and every wait carries a timeout.
"""

import base64
import binascii
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

from ._http import USER_AGENT, check_fetch_url
from ._ws import WebSocket
from .errors import BrowserError
from .html2md import html_to_markdown

_CHROMIUM_NAMES = [
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "brave-browser", "microsoft-edge", "microsoft-edge-stable",
]

# Kitesurf-local's wrangler dev default port (its README: the
# engine listens on http://127.0.0.1:8787; KITESURF_PORT moves it).
KITESURF_DEFAULT_URL = "http://127.0.0.1:8787"

# G-P19: base64 is stream-decoded in chunks of this size (a
# multiple of 4, so chunk boundaries never split a quantum).
_B64_CHUNK = 1024 * 1024

# G-C12: the AX formatter never recurses past this depth, and never
# visits a node twice — a deeply nested or cyclic tree used to
# raise RecursionError and kill the snapshot.
_MAX_AX_DEPTH = 100

_ELEMENTS_JS = """
(() => {
  const els = [...document.querySelectorAll(
    'a,button,input,select,textarea,[role="button"],[onclick]')]
    .filter(e => { const s = getComputedStyle(e);
      return s.display !== 'none' && s.visibility !== 'hidden'; });
  window.__forgeEls = els;
  return JSON.stringify(els.map((e, i) => ({
    i: i, tag: e.tagName.toLowerCase(),
    type: e.getAttribute('type') || '',
    text: ((e.tagName === 'INPUT' &&
            (e.type === 'password' || e.type === 'hidden'))
           // G-S5: a password or hidden field's value is never
           // captured — snapshots must not carry secrets.
           ? '' : (e.innerText || e.value || e.getAttribute('aria-label')
                   || e.placeholder || '')).trim().slice(0, 80),
    href: e.getAttribute('href') || '',
    name: e.getAttribute('name') || ''
  })));
})()
"""


def find_chromium():
    """Path of a Chromium-family browser on this machine, or None."""
    override = os.environ.get("FORGE_CHROMIUM_PATH", "").strip()
    if override:
        return override if os.path.exists(override) else None
    installed = installed_chromium()
    if installed:
        return installed
    for name in _CHROMIUM_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def installed_chromium():
    """The Chromium build install.sh downloaded, or None.

    Layout: ~/.local/share/forge-browser/chromium/chrome-linux/chrome
    — a full, real Chromium window in Forge Browser's own folder,
    no system install, no sudo.
    """
    from pathlib import Path
    candidate = (Path.home() / ".local" / "share" / "forge-browser"
                 / "chromium" / "chrome-linux" / "chrome")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def default_profile_dir():
    """Forge Browser's own persistent profile folder."""
    from pathlib import Path
    return str(Path.home() / ".forge" / "browser-profile")


def open_browser_window(url, port=None):
    """Open Forge Browser as a REAL browser window on the desktop.

    This is the browser itself — a Chromium window with Forge
    Browser's own persistent profile (logins and cookies survive).
    If the window is already open, it is taken to the URL instead.
    The window stays open when this returns; anything (Forge
    included) can drive it through the returned endpoint.

    A4: the debugging port is random per launch (an explicit port
    argument or FORGE_BROWSER_PORT still wins when given), and a
    running window is found through the profile's
    DevToolsActivePort file, not a fixed port. The launch tries
    WITH the sandbox first and falls back to --no-sandbox only
    when the launch fails.

    Returns {"endpoint", "started", "url"}.
    """
    profile = default_profile_dir()
    os.makedirs(profile, exist_ok=True)
    if port is None:
        env_port = (os.environ.get("FORGE_BROWSER_PORT") or "").strip()
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = None
    if port is None:
        discovered = _discover_browser_port(profile)
        if discovered is not None and _version_answers(
                f"http://127.0.0.1:{discovered}"):
            port = discovered
    if port is None:
        port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    if _version_answers(endpoint):
        session = BrowserSession.start(engine=endpoint)
        try:
            # G-S9: the window is the person's own navigation (the
            # Z6 carve-out) — local addresses are their choice.
            session.navigate(url, allow_local=True)
        finally:
            session.release()
        return {"endpoint": endpoint, "started": False, "url": url}
    binary = find_chromium()
    if not binary:
        raise BrowserError(
            "no Chromium to open a window with. Run install.sh — it "
            "downloads a Chromium build into ~/.local/share/forge-browser/ — "
            "or install any Chromium-family browser.")
    process = _launch_chromium(binary, profile, port, headed=True,
                               no_sandbox=False, url=url,
                               start_new_session=True)
    if not _wait_for_endpoint(endpoint, timeout=20):
        _reap_process(process)
        process = _launch_chromium(binary, profile, port, headed=True,
                                   no_sandbox=True, url=url,
                                   start_new_session=True)
        if not _wait_for_endpoint(endpoint, timeout=20):
            raise BrowserError(
                "the browser window did not come up within 20 seconds")
    return {"endpoint": endpoint, "started": True, "url": url}


def find_lightpanda():
    """Path of the Lightpanda binary, or None.

    Checks the explicit override, PATH, and the folder
    browser-setup.sh installs into — the same places, so a machine
    set up by the script is found with no environment fiddling.
    """
    override = os.environ.get("FORGE_LIGHTPANDA_BIN", "").strip()
    if override:
        return override if os.path.exists(override) else None
    path = shutil.which("lightpanda")
    if path:
        return path
    from pathlib import Path
    installed = Path.home() / ".local" / "share" / "forge-browser" / "lightpanda"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return None


def _spawn_lightpanda():
    """Start the installed Lightpanda on a free port.

    Returns (http base url, process). Raises BrowserError when no
    binary is installed or it does not come up.
    """
    binary = find_lightpanda()
    if not binary:
        raise BrowserError(
            "Lightpanda is not installed. Run browser-setup.sh "
            "(it downloads the engine into ~/.local/share/forge-browser/), "
            "or use the chromium engine.")
    port = _free_port()
    process = subprocess.Popen(
        [binary, "serve", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            _http_json(base + "/json/version", timeout=2)
            return base, process
        except Exception:
            if process.poll() is not None:
                break
            time.sleep(0.2)
    _reap_process(process)
    raise BrowserError("the Lightpanda engine did not come up — "
                       "try starting it by hand: lightpanda serve --port 9222")


def find_kitesurf():
    """URL of a running Kitesurf-local engine, or None.

    Kitesurf-local is a dev server the person starts by hand
    (`pnpm run dev` in a Kitesurf checkout); Forge Browser never
    launches it, so this only ever reports one already serving.
    The URL comes from FORGE_KITESURF_URL, else wrangler dev's
    default port 8787. The endpoint only counts when its
    /json/version names itself Kitesurf (its VERSION_INFO reports
    Browser "Kitesurf/0.0.1") — anything else on the port is not
    the engine.
    """
    url = ((os.environ.get("FORGE_KITESURF_URL") or "").strip()
           or KITESURF_DEFAULT_URL)
    try:
        version = _http_json(url.rstrip("/") + "/json/version", timeout=2)
    except Exception:
        return None
    if not isinstance(version, dict):
        # G-C22: a list or string answer used to raise a raw
        # AttributeError on .get — it is simply not the engine.
        return None
    if str(version.get("Browser") or "").startswith("Kitesurf/"):
        return url
    return None


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _reap_process(process, timeout=5):
    """Terminate a spawned engine process and WAIT for it (G-C28):
    terminate()/kill() without wait() leaves a zombie behind."""
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=timeout)
        except Exception:
            pass


def _launch_chromium(binary, profile_dir, port, headed, no_sandbox,
                     url="about:blank", start_new_session=False):
    """Start a Chromium-family binary with a debugging port.

    A4: the sandbox stays ON unless no_sandbox is set — callers
    try sandboxed first and retry without it only when the
    launch fails.
    """
    args = [binary, "--disable-gpu"]
    if no_sandbox:
        args.append("--no-sandbox")
    args += ["--no-first-run", "--disable-dev-shm-usage",
             f"--remote-debugging-port={port}",
             f"--user-data-dir={profile_dir}",
             f"--user-agent={USER_AGENT}", url]
    if not headed:
        args.insert(1, "--headless=new")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=start_new_session)


def _poll_version(cdp_http, process, timeout):
    """Poll /json/version until it names a WebSocket address.

    Returns (ws_url, last_exc): ws_url is None when the deadline
    passes or the process dies first. A 200 answer that names no
    WebSocket address — or one that is not a JSON object — is a
    broken endpoint, not a slow start, so it raises at once
    instead of polling for the whole timeout (G-C22).
    """
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            version = _http_json(cdp_http.rstrip("/") + "/json/version", timeout=3)
        except Exception as exc:
            last_exc = exc
            if process is not None and process.poll() is not None:
                return None, last_exc
            time.sleep(0.3)
            continue
        if not isinstance(version, dict):
            raise BrowserError(
                f"the browser engine at {cdp_http} answered "
                "/json/version with something Forge Browser could "
                "not read")
        ws_url = version.get("webSocketDebuggerUrl")
        if ws_url:
            return ws_url, None
        raise BrowserError(
            f"the browser engine at {cdp_http} did not name a "
            "WebSocket address in its /json/version answer — it "
            "may not be a browser engine")
    return None, last_exc


def _version_answers(endpoint):
    """True when an engine answers /json/version at this endpoint."""
    try:
        _http_json(endpoint + "/json/version", timeout=2)
        return True
    except Exception:
        return False


def _discover_browser_port(profile_dir):
    """The debugging port of a running window, from Chromium's own
    DevToolsActivePort file in the profile (its first line is the
    port). None when no window is running or the file is stale.
    A4: the window's port is random per launch, so this file —
    not a fixed port 9222 — is how a later invocation finds it."""
    try:
        with open(os.path.join(profile_dir, "DevToolsActivePort"),
                  encoding="utf-8") as handle:
            return int(handle.readline().strip())
    except (OSError, ValueError):
        return None


def _wait_for_endpoint(endpoint, timeout):
    """True when the endpoint answers /json/version within timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _version_answers(endpoint):
            return True
        time.sleep(0.3)
    return False


class CDPClient:
    """Synchronous CDP caller over one WebSocket.

    Replies are matched by id; events (messages with a method and no
    id) are stashed for wait_event. One session id may be attached —
    after Target.attachToTarget with flatten, page calls carry it.
    """

    # G-P6: the event stash is capped — events and stray replies
    # used to accumulate for the session's life, and only the
    # awaited method was ever removed.
    _MAX_STASHED_EVENTS = 100

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 0
        self._events = []
        self.session_id = None

    def _stash(self, data):
        """Keep an event or stray reply, dropping the oldest past
        the cap (G-P6)."""
        self._events.append(data)
        if len(self._events) > self._MAX_STASHED_EVENTS:
            del self._events[:-self._MAX_STASHED_EVENTS]

    @staticmethod
    def _parse(raw):
        """Parse one CDP frame, in plain words on bad JSON (G-C11):
        a malformed frame used to escape as a raw ValueError."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrowserError(
                "the browser engine answered with a message Forge "
                "Browser could not read") from exc

    def call(self, method, params=None, timeout=20):
        self._next_id += 1
        call_id = self._next_id
        message = {"id": call_id, "method": method, "params": params or {}}
        if self.session_id:
            message["sessionId"] = self.session_id
        self._ws.send_text(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(
                    f"the browser engine did not answer {method} within {timeout}s")
            # The socket itself enforces the deadline: a wedged or
            # frozen engine must fail this call, never hang it.
            self._ws._sock.settimeout(remaining)
            try:
                raw = self._ws.recv_message()
            except socket.timeout:
                raise BrowserError(
                    f"the browser engine did not answer {method} within {timeout}s")
            finally:
                self._ws._sock.settimeout(None)
            data = self._parse(raw)
            if data.get("id") == call_id:
                if "error" in data:
                    raise BrowserError(
                        f"the browser engine refused {method}: "
                        f"{data['error'].get('message', data['error'])}")
                return data.get("result") or {}
            if "method" in data:
                self._stash(data)

    def wait_event(self, method, timeout=15):
        """True when the named event arrives (or already arrived)."""
        deadline = time.monotonic() + timeout
        while True:
            for index, event in enumerate(self._events):
                if event.get("method") == method and (
                        not self.session_id or event.get("sessionId") in (None, self.session_id)):
                    del self._events[index]
                    return True
            if time.monotonic() >= deadline:
                return False
            # Poll the socket briefly by attempting a no-op read cycle:
            # recv would block past the deadline, so use a short call.
            self._ws._sock.settimeout(0.25)
            try:
                raw = self._ws.recv_message()
            except socket.timeout:
                # G-C2: only a timeout is retried. The old code
                # caught every Exception and continued, so a dead
                # engine (recv returning b"" at once) busy-spun at
                # full CPU until the deadline instead of failing.
                continue
            except OSError as exc:
                # A reset connection is a dead engine too — fail
                # in plain words, not as a raw socket error.
                raise BrowserError(
                    "the browser engine closed the connection "
                    f"while waiting for {method} ({exc})") from exc
            finally:
                self._ws._sock.settimeout(None)
            data = self._parse(raw)
            if "method" in data:
                self._stash(data)
            elif "id" in data:
                self._stash(data)  # a stray reply; harmless to keep

    def close(self):
        self._ws.close()


class BrowserSession:
    """One engine, one page. Use as a context manager."""

    def __init__(self, client, process=None, profile_dir=None, engine_name="chromium",
                 persistent=False, browser_context_id=None):
        self._client = client
        self._process = process
        self._profile_dir = profile_dir
        self._persistent = persistent
        self.engine_name = engine_name
        self._target_id = None
        self._browser_context_id = browser_context_id

    # -- startup ------------------------------------------------------
    @classmethod
    def start(cls, engine="auto", timeout=25, profile_dir=None, headed=False,
              fresh=False):
        """Open a session.

        engine: "auto" (FORGE_CDP_URL if set, else
        FORGE_LIGHTPANDA_URL, else FORGE_KITESURF_URL, else launch
        Chromium), "chromium", "lightpanda", "kitesurf", or an
        explicit ws:// / http:// CDP endpoint for an engine
        already running. "kitesurf" connects to a Kitesurf-local
        dev server (FORGE_KITESURF_URL, else 127.0.0.1:8787) —
        Forge Browser never starts one itself.

        profile_dir: a PERSISTENT profile folder to use instead of a
        throwaway one — cookies and logins survive between sessions,
        which is what makes the human handoff work (the person clears
        a gate or logs in once, later sessions reuse it). Persistent
        profiles are never deleted on close.
        headed: launch with a visible window (the handoff mode, for
        the person to clear a human check); default headless.
        fresh: start with NO browser state from any earlier run —
        no cookies, no storage. A launched Chromium gets its usual
        throwaway profile and a spawned Lightpanda its usual fresh
        process; a session that must attach to an engine already
        running gets its own isolated browser context instead, and
        refuses in plain words when the engine cannot isolate one.
        A fresh run never attaches to shared state just because an
        engine URL is configured: with "auto" it launches first and
        only falls back to a configured URL (isolated) when no
        engine can be launched at all. Verify uses this so one
        verification can never see another run's state.
        """
        cdp_http = None
        engine_name = "chromium"
        process = None
        isolate = False
        persistent_profile = profile_dir
        ws_url = None
        last_exc = None
        polled = False
        if engine == "auto":
            if not fresh:
                for var, name in (("FORGE_CDP_URL", "chromium"),
                                  ("FORGE_LIGHTPANDA_URL", "lightpanda"),
                                  ("FORGE_KITESURF_URL", "kitesurf")):
                    value = (os.environ.get(var) or "").strip()
                    if value:
                        cdp_http = value
                        engine_name = name
                        break
        elif engine.startswith(("http://", "ws://")):
            cdp_http = engine
            engine_name = "cdp"
            isolate = fresh
        elif engine in ("chromium", "lightpanda", "kitesurf"):
            engine_name = engine
            if engine == "lightpanda":
                if not fresh:
                    cdp_http = (os.environ.get("FORGE_LIGHTPANDA_URL") or "").strip() or None
                if not cdp_http:
                    cdp_http, process = _spawn_lightpanda()
            elif engine == "kitesurf":
                cdp_http = ((os.environ.get("FORGE_KITESURF_URL") or "").strip()
                            or KITESURF_DEFAULT_URL)
                isolate = fresh
        if cdp_http is None and engine_name in ("chromium", "auto"):
            binary = find_chromium()
            if not binary and engine == "auto" and find_lightpanda():
                cdp_http, process = _spawn_lightpanda()
                engine_name = "lightpanda"
            if not binary and cdp_http is None:
                if fresh and engine == "auto":
                    # Last resort: nothing can be launched, so attach
                    # to a configured engine — but isolated, never
                    # sharing its state.
                    for var, name in (("FORGE_CDP_URL", "chromium"),
                                      ("FORGE_LIGHTPANDA_URL", "lightpanda"),
                                      ("FORGE_KITESURF_URL", "kitesurf")):
                        value = (os.environ.get(var) or "").strip()
                        if value:
                            cdp_http = value
                            engine_name = name
                            isolate = True
                            break
                if cdp_http is None:
                    raise BrowserError(
                        "no browser engine found on this machine: no "
                        "Chromium-family browser (looked for: "
                        + ", ".join(_CHROMIUM_NAMES) + ") and no Lightpanda. "
                        "browser-setup.sh installs the Lightpanda engine.")
            if binary:
                port = _free_port()
                if persistent_profile:
                    os.makedirs(persistent_profile, exist_ok=True)
                    profile_dir = persistent_profile
                else:
                    profile_dir = tempfile.mkdtemp(prefix="forge-browser-profile-")
                cdp_http = f"http://127.0.0.1:{port}"
                # A4: try WITH the sandbox first; fall back to
                # --no-sandbox only when the launch fails. The
                # debugging port is already random per launch
                # (_free_port).
                process = _launch_chromium(binary, profile_dir, port,
                                           headed, no_sandbox=False)
                ws_url, last_exc = _poll_version(cdp_http, process, timeout)
                if ws_url is None:
                    _reap_process(process)
                    process = _launch_chromium(binary, profile_dir, port,
                                               headed, no_sandbox=True)
                    ws_url, last_exc = _poll_version(cdp_http, process, timeout)
                polled = True
        if engine_name == "kitesurf":
            # Fail fast with the fix named: Kitesurf-local is a dev
            # server the person starts by hand, so a silent 25s poll
            # for one that was never started is the wrong wait.
            if find_kitesurf() is None:
                raise BrowserError(
                    "no Kitesurf engine answering at "
                    f"{cdp_http}. Start one with `pnpm run dev` in a "
                    "Kitesurf checkout (it serves on 127.0.0.1:8787), "
                    "or point FORGE_KITESURF_URL at a running "
                    "Kitesurf-local.")
        # Resolve the browser-level WebSocket address. A browser we
        # just launched needs time to open its port — on a hard-drive
        # machine that is seconds, not milliseconds — so poll for it
        # instead of asking once and giving up. (A launched Chromium
        # already polled above, sandbox-first; this covers engines
        # already running or spawned elsewhere.) G-C29: the poll
        # uses the caller's timeout, not a hardcoded 25 s.
        if not polled:
            ws_url, last_exc = _poll_version(cdp_http, process, timeout)
        if ws_url is None:
            if process:
                _reap_process(process)
            raise BrowserError(
                f"the browser engine at {cdp_http} did not answer ({last_exc})")
        client = CDPClient(WebSocket.connect(ws_url, timeout=15))
        session = cls(client, process=process, profile_dir=profile_dir,
                      engine_name=engine_name, persistent=bool(persistent_profile))
        try:
            context_id = None
            if isolate:
                # A fresh page on an engine already running: its own
                # browser context, so no cookie or storage from any
                # other page on that engine can leak in. An engine
                # that cannot isolate refuses instead of sharing.
                try:
                    created = client.call("Target.createBrowserContext",
                                          {}, timeout=timeout)
                except BrowserError as exc:
                    raise BrowserError(
                        "the engine at "
                        f"{cdp_http} cannot start a fresh page: it "
                        "refused an isolated browser context, so a "
                        "run there would share state with other pages "
                        f"on that engine ({exc})") from exc
                context_id = created.get("browserContextId")
                session._browser_context_id = context_id
            target_params = {"url": "about:blank"}
            if context_id:
                target_params["browserContextId"] = context_id
            target = client.call("Target.createTarget", target_params,
                                 timeout=timeout)
            session._target_id = target["targetId"]
            attached = client.call(
                "Target.attachToTarget",
                {"targetId": session._target_id, "flatten": True}, timeout=timeout)
            client.session_id = attached["sessionId"]
            client.call("Page.enable", timeout=timeout)
        except Exception:
            # G-C10: cleanup runs for ANY failure, not just
            # BrowserError — a KeyError on a malformed reply used
            # to leak the WebSocket, the spawned process, and the
            # temp profile. The original error still propagates.
            session.close()
            raise
        return session

    # -- page actions ---------------------------------------------------
    def navigate(self, url, timeout=25, allow_local=False):
        # G-S9: the fence lives here too — every live path fences
        # at render/fetch, but a future caller must not be able to
        # drive the engine at a local address unfenced. Callers
        # that mean a local address say so (the verify flow's own
        # throwaway server, the person's own window).
        check_fetch_url(url, allow_local=allow_local)
        self._client._events = [e for e in self._client._events
                                if e.get("method") != "Page.loadEventFired"]
        self._client.call("Page.navigate", {"url": url}, timeout=timeout)
        if not self._client.wait_event("Page.loadEventFired", timeout=timeout):
            # Some engines answer slowly or fire the event early; the
            # page is still usable — but say so rather than hide it.
            raise BrowserError(f"the page at {url} did not finish loading within {timeout}s")

    def _evaluate(self, expression, timeout=20):
        result = self._client.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True}, timeout=timeout)
        payload = result.get("result") or {}
        if "exceptionDetails" in result:
            raise BrowserError("the page refused a browser action (its scripts raised an error)")
        return payload.get("value")

    def html(self):
        return self._evaluate("document.documentElement.outerHTML") or ""

    def title(self):
        return self._evaluate("document.title") or ""

    def page(self, url=None, max_chars=12000, offset=0, allow_local=False):
        """The rendered page as a fetch.Page (same shape as fetch())."""
        from .fetch import Page
        if url:
            self.navigate(url, allow_local=allow_local)
        html = self.html()
        final_url = self._evaluate("location.href") or (url or "")
        title, markdown, links = html_to_markdown(html, final_url)
        total = len(markdown)
        return Page(url=url or final_url, final_url=final_url, title=title,
                    markdown=markdown[offset:offset + max_chars], links=links,
                    total_chars=total, offset=offset,
                    truncated=(offset + max_chars) < total, rendered=True)

    def ax_snapshot(self, max_lines=200):
        """The accessibility tree as indented role/name lines."""
        try:
            self._client.call("Accessibility.enable")
            result = self._client.call("Accessibility.getFullAXTree")
            nodes = result.get("nodes") or []
        except BrowserError:
            nodes = []
        if not nodes:
            # Fallback for engines without the Accessibility domain:
            # the numbered interactive elements still describe the page.
            elements = self.elements()
            lines = ["(accessibility tree unavailable on this engine; interactive elements:)"]
            lines += [f"[{e['i']}] {e['tag']} {e['text']}".rstrip() for e in elements]
            return "\n".join(lines[:max_lines])
        return "\n".join(format_ax_tree(nodes)[:max_lines])

    def elements(self):
        raw = self._evaluate(_ELEMENTS_JS)
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []

    def click(self, index):
        self.elements()  # refresh window.__forgeEls for this page state
        outcome = self._evaluate(
            f"(() => {{ const e = window.__forgeEls && window.__forgeEls[{int(index)}];"
            " if (!e) return 'missing'; e.click(); return 'clicked'; })()")
        if outcome != "clicked":
            raise BrowserError(f"element {index} is not on the page anymore — take a fresh snapshot")
        return True

    def type_text(self, index, text):
        self.elements()
        literal = json.dumps(text)
        outcome = self._evaluate(
            f"(() => {{ const e = window.__forgeEls && window.__forgeEls[{int(index)}];"
            " if (!e) return 'missing'; e.focus();"
            " const tag = e.tagName.toLowerCase();"
            " if (tag === 'input' || tag === 'textarea') {"
            "   const setter = Object.getOwnPropertyDescriptor("
            "     Object.getPrototypeOf(e), 'value').set;"
            f"   setter.call(e, {literal});"
            "   e.dispatchEvent(new Event('input', {bubbles: true}));"
            "   e.dispatchEvent(new Event('change', {bubbles: true}));"
            f" }} else if (e.isContentEditable) {{ e.textContent = {literal}; }}"
            " else return 'not-typeable'; return 'typed'; })()")
        if outcome == "missing":
            raise BrowserError(f"element {index} is not on the page anymore — take a fresh snapshot")
        if outcome == "not-typeable":
            raise BrowserError(f"element {index} is not a field text can be typed into")
        return True

    def fill_login(self, username, password, submit=False):
        """Type a login into the page's fields WITHOUT reporting values.

        Finds the password field and the nearest preceding text/email
        field, types the credentials, and returns only field counts —
        the values go to the page and nowhere else: not into the
        return value, not into any log. The vault (forge/browser/
        vault.py) is the intended caller.
        """
        elements = self.elements()
        password_index = None
        user_index = None
        for element in elements:
            if element.get("tag") == "input" and element.get("type") == "password":
                password_index = element["i"]
                break
            if element.get("tag") == "input" and element.get("type") in (
                    "text", "email", ""):
                hint = ((element.get("name") or "") + " " +
                        (element.get("text") or "")).lower()
                if user_index is None or any(
                        word in hint for word in ("user", "email", "login", "account")):
                    user_index = element["i"]
        if password_index is None:
            raise BrowserError("no password field on this page — is this the login page?")
        filled = 0
        if user_index is not None and username:
            self.type_text(user_index, username)
            filled += 1
        self.type_text(password_index, password)
        filled += 1
        if submit:
            for element in self.elements():
                label = (element.get("text") or "").lower()
                if element.get("tag") in ("button", "input") and any(
                        word in label for word in ("log in", "login", "sign in", "submit")):
                    self.click(element["i"])
                    break
            else:
                self.press_enter()
        return {"filled_fields": filled, "submitted": bool(submit)}

    def press_enter(self):
        self._client.call("Input.dispatchKeyEvent",
                          {"type": "keyDown", "key": "Enter", "code": "Enter",
                           "windowsVirtualKeyCode": 13})
        self._client.call("Input.dispatchKeyEvent",
                          {"type": "keyUp", "key": "Enter", "code": "Enter",
                           "windowsVirtualKeyCode": 13})
        return True

    def screenshot(self, path):
        try:
            result = self._client.call("Page.captureScreenshot", {"format": "png"}, timeout=30)
        except BrowserError as exc:
            raise BrowserError(
                f"this engine could not take a screenshot: {exc}") from exc
        data = result.get("data")
        if not data:
            raise BrowserError("the browser engine returned no screenshot data")
        # G-P19: stream-decode the base64 in chunks straight to the
        # file — the old code held the base64 text and the whole
        # decoded bytes at once, on top of the JSON text. The PNG
        # signature is checked from the first decoded bytes, and
        # the length is checked, before anything is written, so a
        # non-PNG still saves nothing.
        if len(data) % 4 != 0:
            raise BrowserError("the browser engine's screenshot was not a PNG — not saved")
        try:
            head = base64.b64decode(data[:12])
        except binascii.Error as exc:
            raise BrowserError(
                "the browser engine's screenshot was not a PNG — not saved") from exc
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BrowserError("the browser engine's screenshot was not a PNG — not saved")
        try:
            with open(path, "wb") as handle:
                for start in range(0, len(data), _B64_CHUNK):
                    handle.write(base64.b64decode(data[start:start + _B64_CHUNK]))
        except binascii.Error as exc:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise BrowserError(
                "the browser engine's screenshot was not a PNG — not saved") from exc
        return path

    # -- teardown ---------------------------------------------------------
    def release(self):
        """Detach WITHOUT closing anything: the tab stays open, the
        browser keeps running, the profile stays. For sessions that
        drive a window the person is looking at (forge.browser open)
        — closing is for engines the session owns."""
        try:
            self._client.close()
        except Exception:
            pass

    def close(self):
        disposed = False
        if self._browser_context_id:
            # A fresh session on a shared engine: throw its whole
            # context away, page and state together. If the dispose
            # fails, fall back to closing the target below.
            try:
                self._client.session_id = None
                self._client.call("Target.disposeBrowserContext",
                                  {"browserContextId": self._browser_context_id},
                                  timeout=5)
                disposed = True
            except Exception:
                pass
            self._browser_context_id = None
        try:
            if self._target_id and not disposed:
                self._client.session_id = None
                self._client.call("Target.closeTarget", {"targetId": self._target_id}, timeout=5)
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass
        # G-C28: terminate AND wait — the old kill() path never
        # waited, leaving a zombie.
        _reap_process(self._process)
        if self._profile_dir and not self._persistent:
            shutil.rmtree(self._profile_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def format_ax_tree(nodes):
    """Render CDP accessibility nodes as indented role/name lines."""
    by_id = {node.get("nodeId"): node for node in nodes}
    children = {}
    referenced = set()
    for node in nodes:
        kids = node.get("childIds") or []
        children[node.get("nodeId")] = kids
        referenced.update(kids)
    roots = [node for node in nodes if node.get("nodeId") not in referenced] or nodes[:1]
    lines = []
    seen = set()
    cut_note = []

    def visit(node, depth):
        if depth > _MAX_AX_DEPTH:
            # G-C12: stop descending — the tree is deeper than any
            # page needs, and the note says so once.
            if not cut_note:
                cut_note.append(True)
                lines.append("  " * _MAX_AX_DEPTH + "- ... (deeper branches cut off)")
            return
        node_id = node.get("nodeId")
        if node_id in seen:
            # G-C12: a cycle (or a node listed twice) visits once.
            return
        seen.add(node_id)
        if node.get("ignored"):
            for kid_id in children.get(node.get("nodeId"), []):
                if kid_id in by_id:
                    visit(by_id[kid_id], depth)
            return
        role = ((node.get("role") or {}).get("value") or "unknown")
        name = ((node.get("name") or {}).get("value") or "").strip()
        lines.append("  " * depth + f"- {role}" + (f": {name}" if name else ""))
        for kid_id in children.get(node.get("nodeId"), []):
            if kid_id in by_id:
                visit(by_id[kid_id], depth + 1)

    for root in roots:
        visit(root, 0)
    return lines
