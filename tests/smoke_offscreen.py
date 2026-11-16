#!/usr/bin/env python3
"""Forge Browser offscreen smoke run (G-T1).

Drives the real window under the offscreen Qt platform and proves
it works end to end: the window shows, the single-instance guard
refuses a second launch (and pings the running copy to show
itself), and the debug log starts. The suite's AppWiringTests
pin the wiring in source text; this script proves the Qt behavior
those tests cannot.

Run from the repo root with the app venv (PySide6):

    python3 tests/smoke_offscreen.py

Exit 0 on success, 1 on failure. The script re-invokes itself
with --second-instance for the second-launch half; that mode is
not run by hand.
"""

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
# No EGL display offscreen: without these flags QtWebEngine's GPU
# process aborts the run on teardown (SIGABRT after a green run).
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-software-rasterizer "
    "--disable-gpu-compositing")
os.environ.setdefault("QT_OPENGL", "software")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _second_instance():
    """The second launch: the guard must refuse it (None) and the
    running copy must have been pinged. Prints SECOND-REFUSED on
    success, SECOND-ALLOWED when the guard failed."""
    from PySide6 import QtCore
    from forge.browser.app import _take_single_instance
    app = QtCore.QCoreApplication([])
    guard = _take_single_instance()
    print("SECOND-REFUSED" if guard is None else "SECOND-ALLOWED")
    return 0


def _check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}"
          + (f" ({detail})" if detail and not ok else ""))
    return ok


def main():
    sys.path.insert(0, _repo_root())
    if len(sys.argv) > 1 and sys.argv[1] == "--second-instance":
        return _second_instance()
    ok = True
    home = tempfile.mkdtemp(prefix="forge-smoke-home-")
    os.environ["HOME"] = home
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtNetwork import QLocalSocket
    from forge.browser.app import (_handle_show_request,
                                   _take_single_instance, create_window,
                                   get_debug_log)
    from forge.browser.webui import LocalPageServer

    app = QtWidgets.QApplication([])
    log = get_debug_log()
    log.action("smoke run started")
    ok &= _check("debug log starts", log.app_name == "forge-browser")

    guard = _take_single_instance()
    ok &= _check("first instance takes the lock", guard is not None)
    if guard is None:
        return 1
    lock, local_server = guard

    server = LocalPageServer().start()
    profile = QWebEngineProfile("forge-smoke")
    started = time.time()
    window = create_window(server, profile, server.home_url)
    local_server.newConnection.connect(
        lambda: _handle_show_request(local_server, window))
    window.show()
    app.processEvents()
    ok &= _check("window shows", window.isVisible())
    ok &= _check("window has a tab", window.tabs.count() >= 1)
    ok &= _check("window paints before the brain pick",
                 window._chat_session is None,
                 "the brain pick blocked window creation (G-P1)")
    ok &= _check("window paints fast with Ollama down",
                 time.time() - started < 20,
                 f"create took {time.time() - started:.1f}s")

    # A second launch is refused and pings the running copy.
    env = dict(os.environ)
    env["PYTHONPATH"] = _repo_root()
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__),
             "--second-instance"],
            capture_output=True, text=True, timeout=60, env=env)
        refused = "SECOND-REFUSED" in proc.stdout
    except subprocess.TimeoutExpired:
        refused = False
    ok &= _check("second instance is refused", refused)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.1)
    ok &= _check("running copy still shows after the ping",
                 window.isVisible())

    # A direct show-request raises the window too.
    sock = QLocalSocket()
    sock.connectToServer("forge-browser")
    connected = sock.waitForConnected(2000)
    if connected:
        sock.write(b"show")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.1)
    ok &= _check("show-request ping connects", connected)
    ok &= _check("window visible after show-request",
                 window.isVisible())

    server.stop()
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
