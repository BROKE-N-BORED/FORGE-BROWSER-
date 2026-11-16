"""verify_in_browser — Item 2's bridge to Forge's existing #65 tool.

Same contract as the #65 tool in the shared tree, implemented on
Forge Browser's own engine instead of the Browse connector:

* A project-relative path is served by a throwaway stdlib server
  bound to 127.0.0.1 only, serving ONLY that one file (G-S11) —
  no other file in the workspace, no directory listing — alive
  only for this call. A path escaping the workspace is refused
  (traversal, with symlinks resolved first), as is a path that
  does not exist.
* The page comes back as TEXT (rendered Markdown when an engine is
  available, plain fetch otherwise — the result says which), plus
  the accessibility snapshot when an engine is present.
* A screenshot is saved under the project's .forge/artifacts/ when
  the engine can take one, and its PATH is returned. The image is
  never attached as data — a model that wants the visual account
  asks the vision seat to describe the saved file (media.py),
  keeping the #65/#71 rule: media never rides into a model that
  cannot parse it.

* Every verification starts FRESH: a throwaway profile for a
  launched Chromium, a fresh process for a spawned Lightpanda, an
  isolated browser context when the only engine is one already
  running. No cookie or storage from an earlier run — this one or
  any other page on the engine — can leak into the page under
  test. (Cookies are host-scoped, so the throwaway server's fresh
  port alone would not isolate them.)
"""

import http.server
import mimetypes
import os
import threading
from urllib.parse import quote, unquote, urlparse

from .errors import BrowserError
from .fetch import fetch as _plain_fetch


class _SingleFileHandler(http.server.BaseHTTPRequestHandler):
    """Serve exactly one file, and nothing else (G-S11).

    The old handler rooted SimpleHTTPRequestHandler at the
    workspace, serving every file plus directory listings. This
    one answers only the target's URL path (anything else 404s)
    and never lists a directory. The file to serve rides on the
    server object, not on the request path — the path is only
    ever compared, never opened, so no traversal or encoding
    trick can reach another file.
    """

    def do_GET(self):
        target = self.server.forge_target_path
        wanted = self.server.forge_url_path
        requested = unquote(urlparse(self.path).path)
        if requested != wanted:
            self.send_error(404, "not found")
            return
        try:
            with open(target, "rb") as handle:
                data = handle.read()
        except OSError:
            self.send_error(404, "not found")
            return
        content_type = (mimetypes.guess_type(target)[0]
                        or "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def verify_in_browser(target, workspace, engine="auto", session_factory=None):
    """Verify a project page. Returns a result dict (see module docs).

    session_factory, when given, replaces BrowserSession.start —
    a seam for tests and for hosts that manage their own sessions.
    A custom factory owns freshness itself; the default opener
    always starts fresh (see the module docs).
    """
    workspace = os.path.realpath(workspace)
    if target.startswith(("http://", "https://")):
        raise BrowserError(
            "verify_in_browser covers project files; external pages go "
            "through fetch / render so their source is recorded")
    # G-S11: resolve symlinks BEFORE the containment check — the
    # old abspath check let a symlink inside the workspace point
    # outside it.
    path = os.path.realpath(os.path.join(workspace, target))
    if path != workspace and not path.startswith(workspace + os.sep):
        raise BrowserError(f"refused: {target!r} is outside the project folder")
    if not os.path.isfile(path):
        raise BrowserError(f"refused: {target!r} does not exist in the project")

    relpath = os.path.relpath(path, workspace).replace(os.sep, "/")
    # G-C15: quote each path segment — a space or # in the name
    # used to produce a broken URL.
    url_path = "/" + "/".join(quote(part, safe="")
                              for part in relpath.split("/"))
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _SingleFileHandler)
    server.forge_target_path = path
    server.forge_url_path = "/" + relpath
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}" + url_path
    try:
        result = {"target": target, "url": url, "screenshot": None, "note": ""}
        try:
            from .cdp import BrowserSession
            opener = session_factory or (
                lambda: BrowserSession.start(engine=engine, fresh=True))
            with opener() as session:
                # G-S9: the throwaway server is this call's own
                # local page — the fence is explicitly allowed here.
                page = session.page(url, allow_local=True)
                result["text"] = page.markdown
                result["rendered"] = True
                result["snapshot"] = session.ax_snapshot()
                artifacts = os.path.join(workspace, ".forge", "artifacts")
                os.makedirs(artifacts, exist_ok=True)
                shot = os.path.join(
                    artifacts, "verify-" + os.path.basename(path) + ".png")
                try:
                    session.screenshot(shot)
                    result["screenshot"] = shot
                except BrowserError as exc:
                    result["note"] = str(exc)
            return result
        except BrowserError as exc:
            # No engine available: fall back to the plain read and say so.
            # The page is verify's own throwaway local server, so the
            # fetch fence is explicitly allowed here (task #20).
            page = _plain_fetch(url, allow_local=True)
            result["text"] = page.markdown
            result["rendered"] = False
            result["note"] = (f"no browser engine available ({exc}); "
                              "read the page unrendered, no screenshot")
            return result
    finally:
        server.shutdown()
        server.server_close()
