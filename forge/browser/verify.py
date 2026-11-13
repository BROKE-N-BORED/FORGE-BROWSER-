"""verify_in_browser — Item 2's bridge to Forge's existing #65 tool.

Same contract as the #65 tool in the shared tree, implemented on
Forge Browser's own engine instead of the Browse connector:

* A project-relative path is served by a throwaway stdlib server
  bound to 127.0.0.1 only, rooted at the workspace, alive only for
  this call. A path escaping the workspace is refused (traversal),
  as is a path that does not exist.
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

import functools
import http.server
import os
import threading

from .errors import BrowserError
from .fetch import fetch as _plain_fetch


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def verify_in_browser(target, workspace, engine="auto", session_factory=None):
    """Verify a project page. Returns a result dict (see module docs).

    session_factory, when given, replaces BrowserSession.start —
    a seam for tests and for hosts that manage their own sessions.
    A custom factory owns freshness itself; the default opener
    always starts fresh (see the module docs).
    """
    workspace = os.path.abspath(workspace)
    if target.startswith(("http://", "https://")):
        raise BrowserError(
            "verify_in_browser covers project files; external pages go "
            "through fetch / render so their source is recorded")
    path = os.path.abspath(os.path.join(workspace, target))
    if path != workspace and not path.startswith(workspace + os.sep):
        raise BrowserError(f"refused: {target!r} is outside the project folder")
    if not os.path.isfile(path):
        raise BrowserError(f"refused: {target!r} does not exist in the project")

    handler = functools.partial(_QuietHandler, directory=workspace)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/" + os.path.relpath(path, workspace).replace(os.sep, "/")
    try:
        result = {"target": target, "url": url, "screenshot": None, "note": ""}
        try:
            from .cdp import BrowserSession
            opener = session_factory or (
                lambda: BrowserSession.start(engine=engine, fresh=True))
            with opener() as session:
                page = session.page(url)
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
