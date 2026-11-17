"""Forge Browser — the program.

A real browser application with its own window: tabs, an address
bar, back/forward/reload, and pages rendered by the Chromium
engine inside Qt WebEngine. The address bar doubles as search —
type words instead of an address and Forge Browser's keyless
search chain answers, on its own local results page. Its start
page and results pages are served by webui.py from this package.

This is the browser itself — not a page inside another browser,
and not a script that opens somebody else's browser. Code Forge
drives the same package (forge.browser) programmatically; this
window is what a person drives.

Needs PySide6 (install.sh puts it in the app venv). Importing
this module without Qt is safe; run_app() says what to do.
"""

import html
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.parse

from .chat import format_duration, step_words
from .errors import BrowserError
from .preview import dwell_ms

# B9: the honest line — nothing leaves the machine, but visits
# and page text ARE kept locally (history.json, page-memory.json).
# The old "nothing tracked" was false.
_TAGLINE = "Forge Browser: no accounts, no API keys. Nothing leaves this machine."

# G-P20: finished download records kept in memory — older ones
# are pruned, so the dict stops growing for the app's life.
_DOWNLOADS_KEEP = 50

# G-UX2/G-UX12: closed tabs remembered for reopening.
_CLOSED_TABS_KEEP = 10

# G-UX9: the chat log on screen and on disk is capped.
_CHAT_LOG_BLOCKS = 1000
_CHAT_HISTORY_MAX_BYTES = 200_000

# G-UX5: the small chat model "Get a chat model" pulls — the
# smallest entry in the brain's preference order, so auto-pick
# finds it as soon as it lands.
_SMALL_CHAT_MODEL = "qwen3:1.7b"

# G-UX4 (extension): the chat dock's width — a sane default under
# half the window, a floor that stays usable, and the size the
# person picks persists across restarts (QSettings).
_CHAT_DOCK_DEFAULT_WIDTH = 360
_CHAT_DOCK_MIN_WIDTH = 240

# B2: the window's ember identity (U15), kept cheap — flat colors
# only, no blur, no gradients, no layout thrash on a 2-core PC.
# Tab text color is deliberately NOT set here: the group colors
# (N12) paint tab text through the palette, and a stylesheet
# color would override them.
EMBER_STYLESHEET = """
QMainWindow, QDockWidget, QDialog { background: #14161a; color: #e8e4da; }
QToolBar { background: #1a1d22; border: none; spacing: 4px; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QListWidget {
  background: #171b22; color: #e8e4da; border: 1px solid #2c313a;
  border-radius: 6px; }
QPushButton { background: #23272f; color: #e8e4da;
  border: 1px solid #3a3f48; border-radius: 6px; padding: 4px 10px; }
QPushButton:hover { border-color: #ffb347; }
QPushButton:pressed { background: #2c313a; }
QPushButton:disabled { color: #6e6a60; }
QTabWidget::pane { border: none; background: #14161a; }
QTabBar::tab { background: #1a1d22; padding: 6px 12px; border: none; }
QTabBar::tab:selected { background: #23272f; }
QMenuBar, QMenu { background: #1a1d22; color: #e8e4da; }
QMenu::item:selected { background: #2c313a; }
QStatusBar { background: #1a1d22; color: #9a958a; }
QStatusBar::item { border: none; }
QDockWidget::title { background: #1a1d22; color: #e8e4da; padding: 4px; }
QLabel, QCheckBox { color: #e8e4da; }
QToolButton#models_toggle { background: #23272f; color: #e8e4da;
  border: 1px solid #3a3f48; border-radius: 6px; padding: 4px 8px; }
QToolButton#models_toggle:hover { border-color: #ffb347; }
QToolButton#models_toggle:checked { background: #2c313a;
  border-color: #ffb347; }
QProgressBar { background: #171b22; border: 1px solid #2c313a;
  border-radius: 4px; color: #e8e4da; }
QProgressBar::chunk { background: #ffb347; }
"""

# N7: tab titles — raw and tidy alike — are capped at this many
# characters on display, so five long words can't make a wide tab.
_TAB_TITLE_CHARS = 28

# P2: a plan the person never answers is denied after 5 minutes —
# a missed dialog cannot hang a turn forever.
PLAN_TIMEOUT_SECONDS = 300

_QT = None

# R3-29: every open window, so a second window (File → New
# Window) is not garbage-collected the moment _new_window
# returns. Windows remove themselves in closeEvent.
_WINDOWS = []


def needs_all_tab_text(text):
    """Whether a send must read every tab's text (J4): the message
    names tabs (@tabN), or asks to compare. Anything else reads
    the current tab only — every send used to wait for all tabs
    (or the 5 s guard) for text nobody asked for."""
    lowered = (text or "").lower()
    return "@tab" in lowered or "compare" in lowered


def turn_status_text(step, started, now=None, waited=0):
    """The dock's working line: 'working: searching… 12s'.
    The step is named in plain words (V1), not the raw action
    kind ("recall_pages"). waited seconds — time spent at the
    plan gate — do not count as work (V2), so the clock pauses
    while the person decides. Under a second the line says
    "<1s", not "0s" (V5)."""
    now = time.monotonic() if now is None else now
    elapsed = max(0.0, now - started - waited)
    return f"working: {step_words(step)}… {format_duration(elapsed)}"


def turn_done_text(outcome, started, now=None, speed=None, waited=0):
    """The dock's finished line: 'Done in 12s.' / 'Stopped after 12s.'
    / 'Failed after 12s.' — with the turn's tok/s when known (G10):
    'Done in 12s (4.2 tok/s).' The clock counts working time only
    (V2); when the person spent time at the plan gate it is named:
    'Done in 12s (11s waiting).' Under a second: 'Done in <1s.' (V5)."""
    now = time.monotonic() if now is None else now
    elapsed = max(0.0, now - started - waited)
    if outcome == "stopped":
        text = f"Stopped after {format_duration(elapsed)}"
    elif outcome == "failed":
        text = f"Failed after {format_duration(elapsed)}"
    else:
        text = f"Done in {format_duration(elapsed)}"
    parts = []
    if speed:
        parts.append(f"{speed:.1f} tok/s")
    if waited >= 1:
        parts.append(f"{int(waited)}s waiting")
    if parts:
        text += " (" + ", ".join(parts) + ")"
    return text + "."


def default_window_size(avail_width, avail_height):
    """The window's first size: 1240x720, or smaller when the screen
    cannot hold it. 720 fits a 1366x768 screen with room for the
    taskbar (the old 1240x820 was 52 px too tall)."""
    return (min(1240, max(320, avail_width)),
            min(720, max(240, avail_height)))


def _load_qt():
    global _QT
    if _QT is not None:
        return _QT
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtWebEngineCore import (
            QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile)
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        raise BrowserError(
            "Forge Browser's window needs PySide6, which is not in "
            "this Python. Run install.sh (it builds the app venv), "
            "then start it with run.sh.") from exc
    _QT = (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
           QWebEngineView, QWebEngineDownloadRequest)
    return _QT


def _human_size(size):
    """1_200_000 -> '1.1 MB'. For the downloads list."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _total_ram_mb():
    """Total RAM in MB from /proc/meminfo, or None elsewhere."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def _read_mem_mb():
    """(used_mb, total_mb) from /proc/meminfo, or None elsewhere
    (B6: the live readout's RAM figures)."""
    try:
        total = available = None
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) // 1024
                if total is not None and available is not None:
                    break
        if total is None:
            return None
        return (total - (available or 0), total)
    except (OSError, ValueError):
        return None


def live_stats_text(used_mb=None, total_mb=None, speed=None, turns=0):
    """The live readout line (B6): RAM in use, model speed, turn
    count. Only the known parts show."""
    parts = []
    if used_mb is not None and total_mb:
        parts.append(f"RAM {used_mb / 1024:.1f}/{total_mb / 1024:.1f} GB")
    if speed:
        parts.append(f"{speed:.1f} tok/s")
    parts.append(f"{turns} turn" if turns == 1 else f"{turns} turns")
    return " · ".join(parts)


# B7: what a model is for, from its name.
_VISION_NAME_PARTS = ("llava", "moondream", "bakllava", "qwen2-vl",
                      "qwen-vl", "minicpm-v", "vision", "llama-vision")


def model_role(name):
    """One plain phrase for what a model does (B7)."""
    low = (name or "").lower()
    if "embed" in low:
        return "finds matching text"
    if any(part in low for part in _VISION_NAME_PARTS):
        return "reads pictures"
    return "chat model (answers and drives the browser)"


def model_fit_note(name, size_bytes, total_ram_mb=None):
    """'fits this PC' / 'too big for this PC' (B7): a model fits
    when it leaves 2 GB for the system and the browser. '' when
    the size or the RAM is unknown."""
    if not size_bytes:
        return ""
    total = (total_ram_mb if total_ram_mb is not None
             else _total_ram_mb())
    if not total:
        return ""
    size_mb = size_bytes / (1024 * 1024)
    if size_mb <= total - 2048:
        return "fits this PC"
    return "too big for this PC"


def model_blurb(name, size_bytes=None, total_ram_mb=None):
    """One plain line about a model (B7): what it does, its size,
    and whether it fits this PC."""
    parts = [model_role(name)]
    if size_bytes:
        parts.append(_human_size(size_bytes))
    fit = model_fit_note(name, size_bytes, total_ram_mb)
    if fit:
        parts.append(fit)
    return f"{name}: " + ", ".join(parts)


def validate_service_url(text):
    """(ok, is_remote, problem) for an Ollama/SearXNG address
    (G-S3). Empty means 'not set' and is fine. A set address must
    be an http(s) URL with a host; a host that is not loopback is
    remote — saving it needs the person's explicit say-so, since
    it sends prompts, page text, and memory off this machine."""
    text = (text or "").strip()
    if not text:
        return (True, False, "")
    if any(ch.isspace() for ch in text):
        return (False, False, "The address must not contain spaces.")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return (False, False,
                "The address must start with http:// or https://.")
    host = (parsed.hostname or "").lower()
    if not host:
        return (False, False, "The address needs a host name.")
    if not re.fullmatch(r"[a-z0-9.\-:\[\]%_]+", host):
        return (False, False,
                "The address has a host name with characters "
                "a host name cannot have.")
    if host == "localhost" or host.startswith("127.") or host == "::1":
        return (True, False, "")
    return (True, True, "")


def chat_history_path():
    """Where the chat log persists (G-UX9)."""
    override = os.environ.get("FORGE_BROWSER_CHAT_HISTORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge",
                        "chat-history.txt")


def save_chat_history(text):
    """Persist the chat log's plain text (G-UX9), capped — the
    newest lines survive, the oldest drop off."""
    try:
        if len(text) > _CHAT_HISTORY_MAX_BYTES:
            text = text[-_CHAT_HISTORY_MAX_BYTES:]
        path = chat_history_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


def load_chat_history():
    """The persisted chat log's plain text (G-UX9), '' when none."""
    try:
        with open(chat_history_path(), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def send_tabs_payload(views, texts, current_view, agent_ids):
    """The tabs payload for a send (G-C21): each live view's title,
    URL, and collected text — the text keyed by view identity
    (id(view)), not by position, so a tab closed or reordered
    while the text was being read keeps its own text."""
    tabs = []
    for view in views:
        if view is None:
            tabs.append({"title": "", "url": "", "text": "",
                         "current": False, "agent": False})
            continue
        tabs.append({"title": view.title(),
                     "url": view.url().toString(),
                     "text": texts.get(id(view), ""),
                     "current": view is current_view,
                     "agent": id(view) in agent_ids})
    return tabs


def load_failed_html(url):
    """The friendly page for a failed load (G-UX6): plain words,
    no ERR_NAME_NOT_RESOLVED."""
    shown = html.escape(url or "that address")
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Could not reach this page</title><style>"
            "body{font-family:sans-serif;margin:48px;"
            "background:#101216;color:#e8e4da}</style></head><body>"
            "<h1>Could not reach this page</h1>"
            f"<p>Forge Browser could not open {shown}.</p>"
            "<p>Check the address, or check your connection, "
            "then try again.</p>"
            "</body></html>")


_DEBUG_LOG = None


def get_debug_log():
    """The app's debug logger (U12, BNB S61 + T21), created on
    first use. Logs live under ~/.forge/logs, so an update never
    wipes them and the install dir stays clean."""
    global _DEBUG_LOG
    if _DEBUG_LOG is None:
        from . import __version__
        from .debug_log import DebugLogger, check_ollama
        _DEBUG_LOG = DebugLogger(
            "forge-browser", __version__,
            log_dir=os.path.join(os.path.expanduser("~"), ".forge",
                                 "logs"))
        _DEBUG_LOG.register_service("ollama", check_ollama)
        _DEBUG_LOG.register_dependency("PySide6")
        _DEBUG_LOG.register_dependency("PySide6-WebEngine")
    return _DEBUG_LOG


def _log_action(text):
    """One line per user action in the debug log (B10, U12).
    Logging never breaks the window, so a failure here is silent."""
    try:
        get_debug_log().action(text)
    except Exception:
        pass


def _log_error(text):
    """One line per caught failure in the debug log (B10, U12)."""
    try:
        get_debug_log().error(text)
    except Exception:
        pass


def _chat_log(kind, text):
    """The debug-log hook the chat session calls (B10): action
    failures caught inside a turn land here, because the turn
    carries on around them and the window's turn-failure log
    would never see them."""
    if kind == "error":
        _log_error(text)
    else:
        _log_action(text)


def app_icon_path():
    """Path to the shipped forge-browser.svg, or None.

    The app root first (covers a repo checkout and the installed
    app, whose tree is copied whole), then the icons dir the
    installer writes to — the icon must show in the installed
    app, not just the repo.
    """
    here = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    candidates = (
        os.path.join(here, "forge-browser.svg"),
        os.path.join(os.path.expanduser("~"), ".local", "share",
                     "icons", "forge-browser.svg"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_SINGLE_INSTANCE_NAME = "forge-browser"


def _take_single_instance():
    """First instance: (lock, server) to hold for the app's life.

    Second instance: asks the running copy to show itself and
    returns None — the caller exits without opening a window.
    A lock whose owner died is retakable at once (stale time 0),
    so a crash never blocks a relaunch; a stale local socket is
    removed before listening. When the lock is held but no server
    answers (a first instance still starting up), the person is
    told plainly instead of watching nothing happen.
    """
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from .cdp import default_profile_dir
    os.makedirs(default_profile_dir(), exist_ok=True)
    lock = QtCore.QLockFile(
        os.path.join(default_profile_dir(), "app.lock"))
    lock.setStaleLockTime(0)
    # tryLock takes its timeout as an argument in this PySide6
    # build — bare tryLock() raises TypeError.
    if not lock.tryLock(0):
        sock = QLocalSocket()
        sock.connectToServer(_SINGLE_INSTANCE_NAME)
        if sock.waitForConnected(1000):
            sock.write(b"show")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
        else:
            QtWidgets.QMessageBox.information(
                None, "Forge Browser",
                "Forge Browser is already running.")
        return None
    QLocalServer.removeServer(_SINGLE_INSTANCE_NAME)
    server = QLocalServer()
    server.listen(_SINGLE_INSTANCE_NAME)
    return lock, server


def _handle_show_request(server, window):
    """A second launch pinged the running copy: raise its window.
    The connection itself is the signal — no payload to read."""
    sock = server.nextPendingConnection()
    if sock is not None:
        sock.disconnectFromServer()
    window.showNormal()
    window.raise_()
    window.activateWindow()


def create_window(server, profile, start_url=None):
    """Build the Forge Browser window around the given page server
    and WebEngine profile. Used by run_app and by smoke tests."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView, QWebEngineDownloadRequest) = _load_qt()
    from .webui import resolve_address
    from .titles import TidyCache, TidyWorker
    # M11: the comparison page's export/refresh links are absolute
    # local-server URLs, so they work from a setHtml tab.
    from .compare import set_server_base
    set_server_base(server.home_url)

    class _ForgePage(QWebEnginePage):
        """A tab's page: when the person navigates it by hand, the
        tab stops being an agent tab (P6) — a 🤖 tab the person
        has taken over is theirs now. Reloads don't unmark."""

        def __init__(self, profile, parent, on_manual_nav):
            super().__init__(profile, parent)
            self._on_manual_nav = on_manual_nav

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            # Qt6 names the members NavigationTypeTyped etc. (the
            # short names do not exist on this PySide6 build).
            # Typed is deliberately NOT here: probed 2026-11-13, a
            # programmatic view.load() reports NavigationTypeTyped,
            # so the hook cannot tell a typed address from the
            # brain opening the tab — including Typed unmarked
            # every agent tab on its initial load. The address bar
            # unmarks explicitly in _go instead.
            if nav_type in (QWebEnginePage.NavigationType.NavigationTypeLinkClicked,
                            QWebEnginePage.NavigationType.NavigationTypeFormSubmitted,
                            QWebEnginePage.NavigationType.NavigationTypeBackForward):
                self._on_manual_nav()
            return super().acceptNavigationRequest(url, nav_type,
                                                   is_main_frame)

    class Window(QtWidgets.QMainWindow):
        tidy_done = QtCore.Signal(object, str, str)  # view, url, tidy title
        preview_ready = QtCore.Signal(str, str)  # url, preview text
        compare_refresh_done = QtCore.Signal(object, str)  # view, new table HTML
        download_retry_progress = QtCore.Signal(int, int, int)  # key, got, total
        download_retry_done = QtCore.Signal(int, bool, str)  # key, ok, final/error
        update_check_done = QtCore.Signal(object)  # v0.12: check result dict
        update_progress = QtCore.Signal(str, int, int)  # v0.12: phase, got, total
        update_install_done = QtCore.Signal(bool, str)  # v0.12: ok, version/reason

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Forge Browser")
            # B2: the window's ember identity (U15).
            self.setStyleSheet(EMBER_STYLESHEET)
            # B4: the window carries the shipped icon (taskbar,
            # window switcher) — resolved from the app root first,
            # the installed icons dir as fallback.
            icon_path = app_icon_path()
            if icon_path is not None:
                icon = QtGui.QIcon(icon_path)
                if not icon.isNull():
                    self.setWindowIcon(icon)
            # Geometry: restore where the window was left last time
            # (QSettings), else open at the default size, centred.
            # A saved geometry from a bigger screen must not strand
            # the window off-screen — it falls back to the default
            # when no part of it is on an available screen.
            settings = QtCore.QSettings("ForgeBrowser", "Forge Browser")
            saved = settings.value("window/geometry")
            screen = QtGui.QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen is not None else None
            restored = False
            if saved is not None and self.restoreGeometry(saved):
                restored = True
                if avail is not None and not avail.intersects(
                        self.frameGeometry()):
                    restored = False
            if restored:
                state = settings.value("window/state")
                if state is not None:
                    self.restoreState(state)
            else:
                if avail is not None:
                    width, height = default_window_size(
                        avail.width(), avail.height())
                else:
                    width, height = 1240, 720
                self.resize(width, height)
                if avail is not None:
                    self.move(avail.center() - self.rect().center())
            # N5: the tidy cache is capped (200 URLs, FIFO) — it no
            # longer grows with every distinct URL for the life of
            # the window. N6: one tidy worker for the window — one
            # tidy at a time, rapid retitles coalesced per tab.
            self._tidy_cache = TidyCache()
            self._tidy_worker = TidyWorker(self._tidy_one,
                                           self._on_tidy_done)
            self._agent_tab_ids = set()  # id(view) of tabs the brain opened
            self._preview_url = ""
            self._preview_popup = None
            self._preview_label = None
            # L8: one preview fetch at a time — a single worker
            # thread and a one-slot queue; a queued URL is dropped
            # when a newer hover arrives.
            self._preview_queue = queue.Queue()
            self._preview_worker_started = False
            # M11: tabs with a refresh in flight — the intercept in
            # _sync_address must not fire twice for one click.
            self._refreshing_views = set()
            self._preview_timer = QtCore.QTimer(self)
            self._preview_timer.setSingleShot(True)
            self._preview_timer.timeout.connect(self._fetch_link_preview)
            self.tidy_done.connect(self._apply_tidy_title)
            self.preview_ready.connect(self._display_preview)
            self.download_retry_progress.connect(
                self._on_download_retry_progress)
            self.download_retry_done.connect(self._on_download_retry_done)
            self.update_check_done.connect(self._on_update_check_done)
            self.update_progress.connect(self._on_update_progress)
            self.update_install_done.connect(self._on_update_install_done)
            # v0.12: update state — one check and one install at
            # a time; the progress dialog while a download runs.
            self._update_check_running = False
            self._update_busy = False
            self._update_progress_dialog = None
            self._downloads = {}  # id(download) -> record dict
            self._downloads_dialog = None
            self._vault = None  # the vault, once unlocked this session
            self._vault_hint_shown = False  # the unlock hint showed once
            self._autofill_asked = set()  # hosts already asked about
            self._autofill_done = set()  # hosts already filled
            self._refresh_ai_controls = None  # set by _attach_chat
            self._turn_count = 0  # chat turns finished (B6 readout)
            self._chat_dock = None  # set by _attach_chat (G-UX4)
            self._view_menu = None  # set by _build_menu (G-UX4)
            self._closed_tabs = []  # URLs of closed tabs (G-UX2/G-UX12)
            # R3-03: id(view) -> the URL that failed to load, for
            # views showing the failed-load page. The view's own
            # URL is a data: URL once setHtml runs, so the bar
            # needs the failed URL kept aside to show it.
            self._failed_urls = {}
            # B3: one modal dialog at a time (U18) — a second
            # request waits in line instead of stacking.
            self._dialog_active = False
            self._dialog_queue = []
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.tabCloseRequested.connect(self._close_tab)
            self.tabs.currentChanged.connect(lambda _: self._sync_address())
            # G-UX12: right-click a tab for Close, Close Others,
            # Duplicate, Reopen Closed Tab.
            self.tabs.tabBar().setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.tabs.tabBar().customContextMenuRequested.connect(
                self._on_tab_context_menu)
            # R3-33: a tab overflow menu at the bar's right end —
            # one click lists every tab by name and jumps to the
            # one picked.
            self._tabs_menu = QtWidgets.QMenu(self)
            self._tabs_menu.aboutToShow.connect(
                self._rebuild_tabs_menu)
            tabs_btn = QtWidgets.QToolButton()
            tabs_btn.setObjectName("tabs_overflow_button")
            tabs_btn.setText("▾")
            tabs_btn.setToolTip("List all tabs")
            tabs_btn.setMenu(self._tabs_menu)
            tabs_btn.setPopupMode(
                QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
            self.tabs.setCornerWidget(
                tabs_btn, QtCore.Qt.Corner.TopRightCorner)
            # Tabs above, find bar below (hidden until Ctrl+F).
            central = QtWidgets.QWidget()
            central_layout = QtWidgets.QVBoxLayout(central)
            central_layout.setContentsMargins(0, 0, 0, 0)
            central_layout.setSpacing(0)
            central_layout.addWidget(self.tabs)
            self._find_bar = QtWidgets.QWidget()
            self._find_bar.setObjectName("find_bar")
            find_layout = QtWidgets.QHBoxLayout(self._find_bar)
            find_layout.setContentsMargins(8, 4, 8, 4)
            find_layout.addWidget(QtWidgets.QLabel("Find:"))
            self._find_entry = QtWidgets.QLineEdit()
            self._find_entry.setObjectName("find_entry")
            self._find_entry.setPlaceholderText("Find in this page")
            self._find_entry.textChanged.connect(
                lambda text: self._find(text))
            self._find_entry.returnPressed.connect(
                lambda: self._find(self._find_entry.text(), forward=True))
            find_layout.addWidget(self._find_entry)
            prev_btn = QtWidgets.QPushButton("Previous")
            prev_btn.setObjectName("find_prev")
            prev_btn.clicked.connect(
                lambda: self._find(self._find_entry.text(), backward=True))
            find_layout.addWidget(prev_btn)
            next_btn = QtWidgets.QPushButton("Next")
            next_btn.setObjectName("find_next")
            next_btn.clicked.connect(
                lambda: self._find(self._find_entry.text(), forward=True))
            find_layout.addWidget(next_btn)
            self._find_label = QtWidgets.QLabel("")
            self._find_label.setObjectName("find_label")
            find_layout.addWidget(self._find_label)
            close_btn = QtWidgets.QPushButton("Close")
            close_btn.setObjectName("find_close")
            close_btn.clicked.connect(self._hide_find)
            find_layout.addWidget(close_btn)
            self._find_bar.setVisible(False)
            central_layout.addWidget(self._find_bar)
            self.setCentralWidget(central)
            QtGui.QShortcut(QtGui.QKeySequence("Escape"), self._find_entry,
                            self._hide_find)
            profile.downloadRequested.connect(self._on_download_requested)

            bar = self.addToolBar("Navigation")
            bar.setObjectName("nav_toolbar")  # saveState needs a name
            bar.setMovable(False)
            bar.addAction("←", lambda: self._view() and self._view().back())
            bar.addAction("→", lambda: self._view() and self._view().forward())
            bar.addAction("⟳", lambda: self._view() and self._view().reload())
            bar.addAction("⌂", lambda: self._view() and self._view().load(
                QtCore.QUrl(server.home_url)))
            self.address = QtWidgets.QLineEdit()
            self.address.setPlaceholderText(
                "Type an address, or words to search")
            self.address.returnPressed.connect(self._go)
            bar.addWidget(self.address)
            bar.addAction("＋ Tab", lambda: self.new_tab())
            star = bar.addAction("★", self._bookmark_current)
            star.setToolTip("Bookmark this page (Ctrl+D)")
            # R3-22: downloads are one click away, with a live
            # count of active downloads — the old code buried
            # them two menus deep.
            self._downloads_btn = QtWidgets.QToolButton()
            self._downloads_btn.setObjectName("downloads_button")
            self._downloads_btn.setText("↓ Downloads")
            self._downloads_btn.setToolButtonStyle(
                QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
            self._downloads_btn.setToolTip("Show downloads")
            self._downloads_btn.clicked.connect(
                self._open_downloads_dialog)
            bar.addWidget(self._downloads_btn)
            self._build_menu()
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), self,
                            self._show_find)
            # G-UX2: the standard browser shortcuts.
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+L"), self,
                            self._focus_address)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+R"), self,
                            lambda: self._view() and self._view().reload())
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Tab"), self,
                            self._next_tab)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+Tab"), self,
                            self._prev_tab)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+T"), self,
                            self._reopen_closed_tab)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+="), self,
                            self._zoom_in)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl++"), self,
                            self._zoom_in)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+-"), self,
                            self._zoom_out)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+0"), self,
                            self._zoom_reset)
            self.statusBar().showMessage(_TAGLINE)
            # B1: the load progress bar — hidden until a page loads.
            self._load_progress = QtWidgets.QProgressBar()
            self._load_progress.setObjectName("load_progress")
            self._load_progress.setMaximumWidth(140)
            self._load_progress.setVisible(False)
            self.statusBar().addPermanentWidget(self._load_progress)
            # B6: the live readout — RAM, model speed, turn count.
            self._live_label = QtWidgets.QLabel(live_stats_text())
            self._live_label.setObjectName("live_stats")
            self.statusBar().addPermanentWidget(self._live_label)
            # Last session's tabs reopen, unless a start URL was given.
            if start_url is not None:
                self.new_tab(start_url)
            else:
                from .session import load_session
                restored = load_session()
                if restored:
                    for tab in restored["tabs"]:
                        self.new_tab(tab["url"])
                    self.tabs.setCurrentIndex(restored["current"])
                else:
                    self.new_tab(server.home_url)

        def closeEvent(self, event):
            # R3-29: a second window leaves the kept-alive list.
            if self in _WINDOWS:
                _WINDOWS.remove(self)
            # Remember size, position, and dock layout for next launch.
            settings = QtCore.QSettings("ForgeBrowser", "Forge Browser")
            settings.setValue("window/geometry", self.saveGeometry())
            settings.setValue("window/state", self.saveState())
            # G-UX4: the chat dock's size and visibility persist too
            # — restoreState cannot carry them, because the dock is
            # created after restoreState runs.
            if self._chat_dock is not None:
                settings.setValue("chat_dock/width",
                                  self._chat_dock.width())
                settings.setValue("chat_dock/visible",
                                  self._chat_dock.isVisible())
            # And the open tabs, so they reopen next launch.
            from .session import save_session
            tabs = []
            for i in range(self.tabs.count()):
                view = self.tabs.widget(i)
                if view is not None:
                    tabs.append({"url": view.url().toString(),
                                 "title": view.title()})
            save_session(tabs, self.tabs.currentIndex())
            super().closeEvent(event)

        # --- one dialog at a time (B3, U18) -------------------------
        def _run_dialog(self, make, queue=True):
            """Run a modal dialog now, or queue it behind the one
            already open. make() builds and exec()s the dialog and
            returns its result. When a dialog is already up and
            queue is True, the request waits in line and make()
            runs when the open one closes (the caller gets None
            at once); when queue is False the caller gets None
            and nothing is shown — for questions, where a stale
            answer later would be wrong. A queued request says
            so honestly in the status bar."""
            if self._dialog_active:
                if queue:
                    self._dialog_queue.append(make)
                    self.statusBar().showMessage(
                        "A message is waiting behind the open dialog.",
                        4000)
                return None
            self._dialog_active = True
            try:
                return make()
            finally:
                self._dialog_active = False
                if self._dialog_queue:
                    nxt = self._dialog_queue.pop(0)
                    QtCore.QTimer.singleShot(
                        0, lambda: self._run_dialog(nxt))

        def _show_message(self, kind, title, text):
            """An information/warning dialog through the queue."""

            def make():
                if kind == "warning":
                    QtWidgets.QMessageBox.warning(self, title, text)
                else:
                    QtWidgets.QMessageBox.information(self, title, text)

            self._run_dialog(make)

        def _ask_yes_no(self, title, text):
            """A Yes/No question, default No (U23), through the
            queue. A question that arrives while another dialog
            is up is answered No at once — the person never sees
            a stale question later."""
            def make():
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle(title)
                box.setText(text)
                box.setStandardButtons(
                    QtWidgets.QMessageBox.StandardButton.Yes |
                    QtWidgets.QMessageBox.StandardButton.No)
                box.setDefaultButton(
                    QtWidgets.QMessageBox.StandardButton.No)
                return (box.exec()
                        == QtWidgets.QMessageBox.StandardButton.Yes)

            return self._run_dialog(make, queue=False) is True

        # --- live readout (B6) --------------------------------------
        def _update_live_stats(self):
            """Refresh the status bar's live readout: RAM in use,
            the last turn's model speed, turns finished."""
            mem = _read_mem_mb()
            session = getattr(self, "_chat_session", None)
            speed = getattr(session, "last_speed", None) \
                if session is not None else None
            used, total = mem if mem else (None, None)
            self._live_label.setText(live_stats_text(
                used, total, speed, getattr(self, "_turn_count", 0)))

        # --- tab shortcuts (G-UX2) ----------------------------------
        def _focus_address(self):
            self.address.setFocus()
            self.address.selectAll()

        def _next_tab(self):
            count = self.tabs.count()
            if count > 1:
                self.tabs.setCurrentIndex(
                    (self.tabs.currentIndex() + 1) % count)

        def _prev_tab(self):
            count = self.tabs.count()
            if count > 1:
                self.tabs.setCurrentIndex(
                    (self.tabs.currentIndex() - 1) % count)

        def _reopen_closed_tab(self):
            if self._closed_tabs:
                self.new_tab(self._closed_tabs.pop())

        # --- tab context menu (G-UX12) ------------------------------
        def _on_tab_context_menu(self, pos):
            bar = self.tabs.tabBar()
            index = bar.tabAt(pos)
            if index < 0:
                return
            menu = QtWidgets.QMenu(self)
            menu.addAction("Close", lambda: self._close_tab(index))
            menu.addAction("Close Others",
                           lambda: self._close_other_tabs(index))
            menu.addAction("Duplicate",
                           lambda: self._duplicate_tab(index))
            reopen = menu.addAction("Reopen Closed Tab",
                                    self._reopen_closed_tab)
            reopen.setEnabled(bool(self._closed_tabs))
            menu.exec(bar.mapToGlobal(pos))

        def _rebuild_tabs_menu(self):
            """R3-33: the overflow menu lists every tab by title —
            the bar scrolls with many tabs, and a tab is hard to
            find by name. Rebuilt each time the menu opens, so
            it never goes stale; the current tab is checked."""
            self._tabs_menu.clear()
            for index in range(self.tabs.count()):
                title = self.tabs.tabText(index) or "Untitled"
                action = self._tabs_menu.addAction(title)
                action.setCheckable(True)
                action.setChecked(index == self.tabs.currentIndex())
                action.triggered.connect(
                    lambda _checked=False, i=index:
                        self.tabs.setCurrentIndex(i))

        def _close_other_tabs(self, keep):
            for i in range(self.tabs.count() - 1, -1, -1):
                if i != keep and self.tabs.count() > 1:
                    self._close_tab(i)

        def _duplicate_tab(self, index):
            view = self.tabs.widget(index)
            if view is not None:
                self.new_tab(view.url().toString())

        # --- chat dock size (G-UX4) ---------------------------------
        def _restore_chat_dock(self):
            """Put the chat dock back where the person left it:
            the saved width (clamped to half the window) and
            visibility. Runs once, just after the window shows —
            resizeDocks needs a laid-out window."""
            dock = self._chat_dock
            if dock is None:
                return
            settings = QtCore.QSettings("ForgeBrowser", "Forge Browser")
            saved = settings.value("chat_dock/width", None)
            try:
                width = int(saved) if saved is not None else None
            except (TypeError, ValueError):
                width = None
            if width is None:
                width = _CHAT_DOCK_DEFAULT_WIDTH
            width = max(_CHAT_DOCK_MIN_WIDTH,
                        min(width, max(_CHAT_DOCK_MIN_WIDTH,
                                       self.width() // 2)))
            self.resizeDocks([dock], [width],
                             QtCore.Qt.Orientation.Horizontal)
            visible = settings.value("chat_dock/visible", None)
            if visible is not None and str(visible).lower() in (
                    "false", "0", "no"):
                dock.hide()

        def _view(self):
            return self.tabs.currentWidget()

        def new_tab(self, url=None, agent=False, html=None, background=False):
            view = QWebEngineView()
            view.setPage(_ForgePage(
                profile, view, lambda: self._unmark_agent(view)))
            view.urlChanged.connect(lambda *a: self._sync_address())
            view.titleChanged.connect(lambda t: self._set_tab_title(view, t))
            view.page().linkHovered.connect(
                lambda url, view=view: self._on_link_hovered(view, url))
            # B1: load progress shows in the status bar; G-UX6:
            # the ok flag reaches _on_load_finished.
            view.loadStarted.connect(
                lambda view=view: self._on_load_started(view))
            view.loadProgress.connect(
                lambda progress, view=view:
                    self._on_load_progress(view, progress))
            view.loadFinished.connect(
                lambda ok, view=view: self._on_load_finished(view, ok))
            index = self.tabs.addTab(view, "New Tab")
            if agent:
                self._agent_tab_ids.add(id(view))
                self.tabs.setTabText(index, "🤖 New Tab")
            # Q2: a background tab (a finished research report) does
            # not steal focus from what the person is reading.
            if not background:
                self.tabs.setCurrentIndex(index)
            self._sync_tab_numbers()
            if html is not None:
                # G-S4: agent-made HTML (compare table, research
                # report) gets rel="noopener" on its links before
                # it reaches the view — a clicked link must not
                # get a window.opener back into the local page.
                from .webui import _with_noopener
                view.setHtml(_with_noopener(html))
            else:
                view.load(QtCore.QUrl(url or server.home_url))
            return view

        def _agent_mark(self, view):
            return "🤖 " if id(view) in self._agent_tab_ids else ""

        def _unmark_agent(self, view):
            # P6: the person took this tab over by hand — drop the
            # 🤖 mark from the set and from the tab's current text.
            if id(view) not in self._agent_tab_ids:
                return
            self._agent_tab_ids.discard(id(view))
            index = self.tabs.indexOf(view)
            if index >= 0:
                title = self.tabs.tabText(index)
                if title.startswith("🤖 "):
                    self.tabs.setTabText(index, title[2:])

        def _sync_tab_numbers(self):
            # J8: every tab's tooltip names its number, so the
            # person can see which tab @tab2 means without
            # counting. Numbers shift when tabs close, so this
            # re-syncs on every tab add, close, and title change.
            for i in range(self.tabs.count()):
                view = self.tabs.widget(i)
                title = view.title() if view is not None else ""
                self.tabs.setTabToolTip(i, f"Tab {i + 1}: {title or 'Page'}")
            self._sync_group_colors()

        def _sync_group_colors(self):
            # N12: color tabs by their group, so grouping is
            # visible in the tab bar itself — not only in the
            # chat's tab list. Domain grouping only: no model
            # call on every tab change. One group (or none)
            # means no colors — everything stays default.
            from .titles import assign_group_colors, group_items
            items = []
            for i in range(self.tabs.count()):
                view = self.tabs.widget(i)
                items.append({"title": view.title() if view is not None else "",
                              "url": (view.url().toString()
                                      if view is not None else "")})
            groups = group_items(items)
            bar = self.tabs.tabBar()
            if len(groups) < 2:
                for i in range(self.tabs.count()):
                    bar.setTabTextColor(i, QtGui.QColor())
                return
            colors = assign_group_colors([g["name"] for g in groups])
            index_of = {id(item): i for i, item in enumerate(items)}
            for group in groups:
                for item in group["items"]:
                    bar.setTabTextColor(index_of[id(item)],
                                        QtGui.QColor(colors[group["name"]]))

        def _set_tab_title(self, view, title):
            index = self.tabs.indexOf(view)
            if index < 0:
                return
            if view is self._view():
                self._sync_window_title()
            self.tabs.setTabText(index, self._agent_mark(view)
                                 + (title or "Page")[:_TAB_TITLE_CHARS])
            self._sync_tab_numbers()
            # Long, messy titles get a short brain-written one in the
            # background; the raw title shows until it lands.
            # The tidy-titles toggle (or the master AI switch) turns
            # this off entirely.
            from .seats import seats_enabled
            session = getattr(self, "_chat_session", None)
            url = view.url().toString()
            if (session is None or not url or url.startswith("http://127.0.0.1")
                    or not seats_enabled("tidy_titles")):
                return
            if url in self._tidy_cache:
                # N7: the cached tidy is capped like the raw title.
                self.tabs.setTabText(
                    index, self._agent_mark(view)
                    + self._tidy_cache[url][:_TAB_TITLE_CHARS])
                return
            raw = (title or "").strip()
            if len(raw) <= _TAB_TITLE_CHARS and len(raw.split()) <= 5:
                return
            # N6: the worker queues it — one tidy at a time, rapid
            # retitles coalesced per tab.
            self._tidy_worker.request(view, url, raw)

        def _fast_seat_session(self):
            """The fast seat (for tidy titles and the summarize map
            step), or the brain when no fast seat is picked (same
            model, no extra swap). G-C20: background jobs get their
            OWN session, never the turn session — sharing it raced
            on _turn_tokens, _turn_seconds, last_speed, and
            _retrieve_last. A second ChatSession for the same model
            is cheap: it is only a client, not a model load."""
            from .chat import ChatSession
            from .seats import pick_fast_model
            fast = pick_fast_model()
            session = getattr(self, "_chat_session", None)
            model = fast
            if not model:
                model = session.model if session is not None else None
            if not model:
                return None
            cached = getattr(self, "_fast_session", None)
            if cached is None or cached.model != model:
                cached = ChatSession(model=model)
                self._fast_session = cached
            return cached

        def _tidy_one(self, url, raw):
            # N6: the worker's tidy call — one at a time, on the
            # fast seat when one is picked.
            from .titles import tidy_title
            return tidy_title(self._fast_seat_session(), raw, url)

        def _on_tidy_done(self, view, url, tidy):
            # N6: the worker's delivery — cache, then hand the
            # title to the GUI thread via the signal.
            self._tidy_cache[url] = tidy
            self.tidy_done.emit(view, url, tidy)

        def _apply_tidy_title(self, view, url, tidy):
            index = self.tabs.indexOf(view)
            if index >= 0 and view.url().toString() == url:
                # N7: the tidy title is capped like the raw one.
                self.tabs.setTabText(
                    index, self._agent_mark(view) + tidy[:_TAB_TITLE_CHARS])

        def _on_load_started(self, view):
            # B1: the progress bar appears while the current tab
            # loads (U16: always show activity).
            if view is self._view():
                self._load_progress.setValue(0)
                self._load_progress.setVisible(True)

        def _on_load_progress(self, view, progress):
            if view is self._view():
                self._load_progress.setValue(progress)

        def _on_load_finished(self, view, ok=True):
            self._sync_address()
            if view is self._view():
                self._load_progress.setVisible(False)
            if not ok:
                # G-UX6: a failed load gets a friendly page, not
                # Qt's technical error page — and no history
                # entry for a page never seen.
                url = view.url().toString()
                _log_error(f"Page failed to load: {url}")
                # R3-03: keep the failed URL aside before setHtml
                # replaces the view's URL with a data: URL — the
                # bar shows the failed address, not the page's
                # own giant data: URL.
                if url:
                    self._failed_urls[id(view)] = url
                view.setHtml(load_failed_html(url))
                return
            # R3-03: a real page loaded — the failed URL is stale.
            # A data: URL here is the failed-load page's own
            # loadFinished (setHtml fires one), so it must not
            # clear the entry.
            if not view.url().toString().startswith("data:"):
                self._failed_urls.pop(id(view), None)
            from .titles import record_visit
            url = view.url().toString()
            title = view.title()
            record_visit(url, title)
            self._maybe_autofill(view)
            # Keep the page's text for later recall ("that thing I
            # read...") — chunks only, no model call at visit time.
            # The memory toggle (or the master AI switch) turns
            # this off entirely.
            from .seats import seats_enabled
            if url.startswith("http://127.0.0.1") or not seats_enabled("memory"):
                return

            def got_page(page_text):
                from .memory import remember_page
                threading.Thread(
                    target=remember_page,
                    args=(url, title, page_text or ""),
                    daemon=True).start()

            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''", got_page)

        def _close_tab(self, index):
            widget = self.tabs.widget(index)
            if widget is not None:
                # G-UX2: a closed tab can come back (Ctrl+Shift+T).
                url = widget.url().toString()
                if url:
                    self._closed_tabs.append(url)
                    del self._closed_tabs[:-_CLOSED_TABS_KEEP]
                # R3-03: drop the failed-URL entry with the view,
                # so a later view reusing this id cannot inherit it.
                self._failed_urls.pop(id(widget), None)
            if self.tabs.count() > 1:
                self._agent_tab_ids.discard(id(widget))
                self.tabs.removeTab(index)
                widget.deleteLater()
                self._sync_tab_numbers()
            else:
                # G-UX11: closing the last tab opens a fresh
                # start-page tab instead of silently doing nothing.
                self._agent_tab_ids.discard(id(widget))
                self.tabs.removeTab(index)
                widget.deleteLater()
                self.new_tab(server.home_url)

        def _sync_address(self):
            view = self._view()
            if view is not None:
                url = view.url().toString()
                # M11: a click on the comparison tab's Refresh link
                # re-runs the comparison against the tabs' current
                # text instead of navigating anywhere.
                if (url == server.home_url + "compare-refresh"
                        and id(view) not in self._refreshing_views):
                    view.stop()
                    self._refresh_comparison(view)
                    return
                # R3-27: the bar stays empty on Forge Browser's own
                # local pages, by decision — the bar is for
                # addresses the person types and pages they can
                # share, and a 127.0.0.1 address is neither:
                # copying it hands someone a link to their own
                # machine. An empty bar also lets the search
                # placeholder show.
                if url.startswith("http://127.0.0.1"):
                    self.address.setText("")
                elif url.startswith("data:"):
                    # R3-03: a setHtml page has no address of its
                    # own — WebEngine reports a data: URL holding
                    # the whole page, which the old code put in
                    # the bar verbatim. A failed load shows the
                    # address that failed (kept aside in
                    # _failed_urls); any other setHtml page
                    # (compare table, research notes) leaves the
                    # bar empty, like the other local pages.
                    self.address.setText(
                        self._failed_urls.get(id(view), ""))
                elif url:
                    self.address.setText(url)
                # An empty URL (a fresh view, no load yet) leaves
                # the bar as it was.
            # A navigation ends any hover — the link is gone.
            self._preview_url = ""
            self._preview_timer.stop()
            self._hide_link_preview()
            self._sync_window_title()

        def _sync_window_title(self):
            # The window title shows the current page's title.
            view = self._view()
            title = (view.title() or "").strip() if view is not None else ""
            if title and title != "Forge Browser":
                self.setWindowTitle(f"{title} · Forge Browser")
            else:
                self.setWindowTitle("Forge Browser")

        def _on_link_hovered(self, view, url):
            # Rest on a link and, after a short dwell, a two-line
            # preview of where it goes appears by the cursor. Nothing
            # is fetched on hover alone, and never prefetched.
            if view is not self._view():
                return
            url = url or ""
            if not url:
                self._preview_url = ""
                self._preview_timer.stop()
                self._hide_link_preview()
                # L4: the status bar shows the hovered link while
                # it is hovered, like every other browser.
                self.statusBar().showMessage(_TAGLINE)
                return
            self.statusBar().showMessage(url)
            if url == self._preview_url and self._preview_popup is not None \
                    and self._preview_popup.isVisible():
                return
            self._preview_url = url
            self._hide_link_preview()
            # L9: the dwell comes from the setting, fresh each hover.
            self._preview_timer.start(dwell_ms())

        def _fetch_link_preview(self):
            url = self._preview_url
            if not url:
                return
            # L6: the popup says it is fetching at once — a slow
            # fetch no longer looks like no preview.
            self._display_preview(url, "Fetching preview…")
            if not self._preview_worker_started:
                self._preview_worker_started = True
                threading.Thread(target=self._preview_worker,
                                 daemon=True).start()
            # L8: one fetch at a time; a queued URL is dropped
            # when a newer hover arrives.
            try:
                self._preview_queue.get_nowait()
            except queue.Empty:
                pass
            self._preview_queue.put(url)

        def _preview_worker(self):
            from .preview import preview_url
            while True:
                url = self._preview_queue.get()
                try:
                    text = preview_url(url)
                except Exception:
                    text = "Couldn't fetch that link."
                self.preview_ready.emit(url, text)

        def _display_preview(self, url, text):
            if url != self._preview_url or not text:
                return
            if self._preview_popup is None:
                popup = QtWidgets.QWidget(
                    None, QtCore.Qt.WindowType.ToolTip |
                    QtCore.Qt.WindowType.FramelessWindowHint)
                popup.setObjectName("link_preview")
                label = QtWidgets.QLabel(popup)
                label.setWordWrap(True)
                label.setMaximumWidth(380)
                layout = QtWidgets.QVBoxLayout(popup)
                layout.setContentsMargins(10, 8, 10, 8)
                layout.addWidget(label)
                popup.setStyleSheet(
                    "background:#1d1d1d; color:#e8e6e1; "
                    "border:1px solid #4a4a4a; border-radius:6px;")
                self._preview_label = label
                self._preview_popup = popup
            # L4: the destination domain rides the first line, so
            # the person can judge where a link goes before clicking.
            domain = urllib.parse.urlparse(url).netloc
            self._preview_label.setText(
                f"{domain}\n{text}" if domain else text)
            self._preview_popup.adjustSize()
            # L3: clamp the popup to the available screen — near
            # the right or bottom edge it used to run off-screen.
            pos = QtGui.QCursor.pos() + QtCore.QPoint(14, 18)
            try:
                screen = (QtGui.QGuiApplication.screenAt(
                    QtGui.QCursor.pos())
                    or QtGui.QGuiApplication.primaryScreen())
                avail = screen.availableGeometry() if screen is not None else None
            except Exception:
                avail = None
            if avail is not None:
                size = self._preview_popup.size()
                pos.setX(min(max(pos.x(), avail.left()),
                             avail.right() - size.width()))
                pos.setY(min(max(pos.y(), avail.top()),
                             avail.bottom() - size.height()))
            self._preview_popup.move(pos)
            self._preview_popup.show()

        def _hide_link_preview(self):
            if self._preview_popup is not None:
                self._preview_popup.hide()

        def _refresh_comparison(self, view):
            """M11: re-run the last comparison against the tabs'
            current text and put the new table in the same tab."""
            from .compare import (compare_table_html, compare_tabs,
                                  last_comparison, set_last_comparison)
            rows, fields = last_comparison()

            def note_page(text):
                return ("<!doctype html><html><head><meta charset=\"utf-8\">"
                        "<title>Comparison</title><style>"
                        "body{font-family:sans-serif;margin:24px;"
                        "background:#101216;color:#e8e4da}</style></head>"
                        f"<body><p>{html.escape(text)}</p></body></html>")

            if not rows:
                view.setHtml(note_page(
                    "No comparison to refresh. Ask the chat to compare "
                    "some tabs first."))
                return
            session = getattr(self, "_chat_session", None)
            if session is None:
                view.setHtml(note_page(
                    "No brain is available to refresh the comparison."))
                return
            targets = {row["url"]: row["title"] for row in rows
                       if row.get("url")}
            views = []
            for i in range(self.tabs.count()):
                candidate = self.tabs.widget(i)
                if (candidate is None or candidate is view
                        or id(candidate) in self._agent_tab_ids):
                    continue
                if candidate.url().toString() in targets:
                    views.append(candidate)
            if not views:
                view.setHtml(note_page(
                    "The compared tabs are closed. Nothing to refresh."))
                return
            self._refreshing_views.add(id(view))
            view.setHtml(note_page("Refreshing the comparison…"))
            texts = {}
            started = {"done": False}

            def start_work():
                if started["done"]:
                    return
                started["done"] = True
                tabs = [{"title": targets[v.url().toString()],
                         "url": v.url().toString(),
                         "text": texts.get(id(v), "")} for v in views]

                def work():
                    # G-C5: a refresh failure used to die silently
                    # in this thread, leaving "Refreshing…" stuck
                    # forever and the view in _refreshing_views,
                    # so later refreshes were ignored too.
                    try:
                        new_rows, dropped = compare_tabs(session, tabs,
                                                         fields)
                        set_last_comparison(new_rows, fields)
                        page = compare_table_html(new_rows, fields,
                                                  dropped=dropped)
                    except Exception as exc:
                        _log_error(
                            f"Comparison refresh failed: {exc}")
                        page = note_page(
                            "Could not refresh the comparison: "
                            f"{exc}")
                    finally:
                        self._refreshing_views.discard(id(view))
                    self.compare_refresh_done.emit(view, page)

                threading.Thread(target=work, daemon=True).start()

            def collect(candidate, page_text):
                texts[id(candidate)] = page_text or ""
                if len(texts) == len(views):
                    start_work()

            for candidate in views:
                candidate.page().runJavaScript(
                    "document.body ? document.body.innerText : ''",
                    lambda page_text, v=candidate: collect(v, page_text))
            # A tab that never answers must not hang the refresh.
            QtCore.QTimer.singleShot(5000, start_work)

        def _go(self):
            kind, target = resolve_address(self.address.text())
            if kind == "search":
                target = server.search_url(target)
                _log_action(f"Searched for: {self.address.text()}")
            elif target == "/":
                target = server.home_url
            else:
                _log_action(f"Opened: {target}")
            view = self._view()
            if view is not None:
                # P6: typed in the address bar by hand — the tab is
                # the person's now (view.load reports Other, so the
                # page hook can't see this one).
                self._unmark_agent(view)
                view.load(QtCore.QUrl(target))

        # --- menu bar (E8) -----------------------------------------
        def _menu_action(self, menu, text, slot, shortcut=None, parent=None):
            action = QtGui.QAction(text, parent or self)
            if shortcut:
                action.setShortcut(QtGui.QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)
            return action

        def _build_menu(self):
            bar = self.menuBar()
            file_menu = bar.addMenu("&File")
            self._menu_action(file_menu, "New Tab",
                              lambda: self.new_tab(), "Ctrl+T")
            self._menu_action(file_menu, "New Window",
                              self._new_window, "Ctrl+N")
            self._menu_action(file_menu, "Open File…",
                              self._open_file, "Ctrl+O")
            self._menu_action(
                file_menu, "Close Tab",
                lambda: self._close_tab(self.tabs.currentIndex()), "Ctrl+W")
            self._menu_action(file_menu, "Reopen Closed Tab",
                              self._reopen_closed_tab, "Ctrl+Shift+T")
            file_menu.addSeparator()
            self._menu_action(file_menu, "Print…",
                              self._print_page, "Ctrl+P")
            self._menu_action(file_menu, "Save as PDF…",
                              self._save_page_pdf)
            file_menu.addSeparator()
            self._menu_action(file_menu, "Quit", self.close, "Ctrl+Q")
            edit_menu = bar.addMenu("&Edit")
            self._menu_action(edit_menu, "Cut",
                              lambda: self._edit_action("cut"), "Ctrl+X")
            self._menu_action(edit_menu, "Copy",
                              lambda: self._edit_action("copy"), "Ctrl+C")
            self._menu_action(edit_menu, "Paste",
                              lambda: self._edit_action("paste"), "Ctrl+V")
            self._menu_action(edit_menu, "Select All",
                              lambda: self._edit_action("selectAll"),
                              "Ctrl+A")
            edit_menu.addSeparator()
            self._menu_action(edit_menu, "Find in Page",
                              self._show_find, "Ctrl+F")
            view_menu = bar.addMenu("&View")
            self._view_menu = view_menu  # G-UX4: the Chat toggle joins here
            self._menu_action(view_menu, "Zoom In", self._zoom_in, "Ctrl+=")
            self._menu_action(view_menu, "Zoom Out", self._zoom_out, "Ctrl+-")
            self._menu_action(view_menu, "Reset Zoom", self._zoom_reset,
                              "Ctrl+0")
            self._fullscreen_action = self._menu_action(
                view_menu, "Fullscreen", self._toggle_fullscreen, "F11")
            self._fullscreen_action.setCheckable(True)
            # G-UX1: history gets a menu of its own.
            history_menu = bar.addMenu("&History")
            self._menu_action(history_menu, "Show All History",
                              self._open_history_window, "Ctrl+H")
            self._menu_action(history_menu, "Clear History…",
                              self._clear_history_native)
            self._bookmarks_menu = bar.addMenu("&Bookmarks")
            self._bookmarks_menu.aboutToShow.connect(
                self._rebuild_bookmarks_menu)
            # G-UX3: the Ctrl+D action is created once, here — the
            # old code built it on every menu open, so Ctrl+D was
            # dead until the menu opened and duplicated after.
            self._bookmark_action = QtGui.QAction("Bookmark This Page",
                                                  self)
            self._bookmark_action.setShortcut(
                QtGui.QKeySequence("Ctrl+D"))
            self._bookmark_action.triggered.connect(
                self._bookmark_current)
            tools_menu = bar.addMenu("&Tools")
            self._menu_action(tools_menu, "Downloads",
                              self._open_downloads_dialog)
            self._menu_action(tools_menu, "Vault…",
                              self._open_vault_window)
            self._menu_action(tools_menu, "Research Reports…",
                              self._open_research_window)
            self._menu_action(tools_menu, "Settings…", self._open_settings)
            help_menu = bar.addMenu("&Help")
            self._menu_action(help_menu, "How to Use…",
                              self._show_howto)
            self._menu_action(help_menu, "Debug Report…",
                              self._show_debug_report)
            self._menu_action(help_menu, "About Forge Browser",
                              self._show_about)
            # v0.12: the update check — a manual check that
            # reports its outcome, and the startup toggle
            # (on by default: the launch check is silent).
            from .updates import startup_check_enabled
            help_menu.addSeparator()
            self._menu_action(help_menu, "Check for updates",
                              self._on_check_updates)
            self._update_toggle_action = self._menu_action(
                help_menu, "Check for updates on startup",
                self._on_toggle_update_check)
            self._update_toggle_action.setCheckable(True)
            self._update_toggle_action.setChecked(
                startup_check_enabled())

        def _new_window(self):
            """R3-29: a second Forge Browser window. The
            single-instance guard is per-process (the QLockFile
            and QLocalServer held by run_app), so a window opened
            inside this process is allowed deliberately — the
            guard only stops a second PROCESS. The new window
            opens with one start-page tab (a start URL skips
            session restore), offset so it does not sit exactly
            on top of this one."""
            other = create_window(server, profile, server.home_url)
            _WINDOWS.append(other)
            other.move(self.x() + 40, self.y() + 40)
            other.show()
            return other

        def _open_file(self):
            """R3-31: open a local file in a new tab — through the
            window's file:// load path, the same any-file-type
            path the engine room uses: WebEngine renders HTML,
            text, images, and PDF natively, and downloads the
            rest instead of showing binary."""
            path, _filter = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open File")
            if not path:
                return
            self.new_tab(QtCore.QUrl.fromLocalFile(path).toString())

        def _edit_action(self, kind):
            """R3-16: Cut/Copy/Paste/Select All on whatever has
            focus — a text field in the window (address bar, chat
            entry, chat log), or the page in the current tab via
            its WebEngine page action. The menu shortcut consumes
            the key, so the field does not also act on it."""
            widget = QtWidgets.QApplication.focusWidget()
            if isinstance(widget, (QtWidgets.QLineEdit,
                                   QtWidgets.QTextEdit,
                                   QtWidgets.QPlainTextEdit)):
                getattr(widget, kind)()
                return
            view = self._view()
            if view is not None:
                action = {"cut": QWebEnginePage.WebAction.Cut,
                          "copy": QWebEnginePage.WebAction.Copy,
                          "paste": QWebEnginePage.WebAction.Paste,
                          "selectAll":
                              QWebEnginePage.WebAction.SelectAll}[kind]
                view.page().triggerAction(action)

        def _toggle_fullscreen(self):
            """R3-30: F11 toggles fullscreen; the View menu item
            follows the window's state."""
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            self._fullscreen_action.setChecked(self.isFullScreen())

        def _print_page(self):
            """R3-28: print the current page. This PySide6 build
            does not bind QWebEnginePage.print and ships no QtPdf,
            so there is no direct path to a printer — the page is
            rendered to a PDF through the WebEngine print path
            and opened in the system's PDF viewer, which prints
            it. The status line says so plainly instead of
            pretending a printer dialog ran."""
            view = self._view()
            if view is None:
                return
            path = os.path.join(tempfile.gettempdir(),
                                "forge-browser-print.pdf")

            def done(pdf_path, ok):
                if pdf_path != path:
                    return
                if ok:
                    QtGui.QDesktopServices.openUrl(
                        QtCore.QUrl.fromLocalFile(path))
                    self.statusBar().showMessage(
                        "Opened the page as a PDF to print.", 5000)
                else:
                    self.statusBar().showMessage(
                        "Could not make a PDF of this page.", 5000)

            view.page().pdfPrintingFinished.connect(
                done, QtCore.Qt.ConnectionType.SingleShotConnection)
            view.page().printToPdf(path)

        def _save_page_pdf(self):
            """R3-28: save the current page as a PDF, through the
            WebEngine print path."""
            view = self._view()
            if view is None:
                return
            path, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save Page as PDF", "",
                "PDF Files (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"

            def done(pdf_path, ok):
                if pdf_path != path:
                    return
                if ok:
                    self.statusBar().showMessage(
                        "Saved the page as "
                        f"{os.path.basename(path)}.", 5000)
                else:
                    self.statusBar().showMessage(
                        "Could not save the page as a PDF.", 5000)

            view.page().pdfPrintingFinished.connect(
                done, QtCore.Qt.ConnectionType.SingleShotConnection)
            view.page().printToPdf(path)

        def _clear_history_native(self):
            """G-UX1: clear history from the menu — a destructive
            action, so it confirms first, default No (U23)."""
            from .titles import clear_history, load_history
            count = len(load_history())
            if not count:
                self.statusBar().showMessage(
                    "History is already empty.", 3000)
                return
            if self._ask_yes_no(
                    "Clear history?",
                    f"This deletes all {count} recorded visits. "
                    "It cannot be undone.\n\nClear history?"):
                clear_history()
                _log_action("Cleared history")
                self.statusBar().showMessage("History cleared.", 3000)

        def _show_howto(self):
            """U22: the manual as an index window — sections from
            BROWSER.md's own headings; clicking one shows its text.
            R3-10b: user topics first, developer topics grouped
            under their own heading below, so a first-time user
            never meets the engine-room internals unprompted."""
            from .howto import load_sections, split_sections
            sections = load_sections()
            user_sections, dev_sections = split_sections(sections)
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("How to Use")
            dialog.resize(900, 600)
            layout = QtWidgets.QHBoxLayout(dialog)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("howto_list")
            text = QtWidgets.QTextBrowser()
            text.setObjectName("howto_text")
            text.setOpenExternalLinks(False)
            # shown[row] is the section for that list row, or None
            # for the developer heading (not selectable).
            shown = []
            for _level, title, _body in user_sections:
                listing.addItem("  " * (_level - 1) + title)
                shown.append((_level, title, _body))
            if dev_sections:
                heading = QtWidgets.QListWidgetItem("Developer topics")
                heading.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
                bold = heading.font()
                bold.setBold(True)
                heading.setFont(bold)
                listing.addItem(heading)
                shown.append(None)
                for _level, title, _body in dev_sections:
                    listing.addItem("  " * (_level - 1) + title)
                    shown.append((_level, title, _body))

            def show_section(row):
                if 0 <= row < len(shown) and shown[row] is not None:
                    text.setPlainText(shown[row][2])

            listing.currentRowChanged.connect(show_section)
            if sections:
                listing.setCurrentRow(0)
            else:
                text.setPlainText(
                    "The manual (BROWSER.md) is not installed with "
                    "this copy of Forge Browser, so there is nothing "
                    "to show here. Reinstalling puts it back.")
            layout.addWidget(listing, 1)
            layout.addWidget(text, 2)
            dialog.exec()

        def _show_debug_report(self):
            """U12: write a debug report and say where it went."""
            try:
                path = get_debug_log().generate_report()
            except Exception as exc:  # noqa: BLE001 — report it plainly
                self._show_message(
                    "warning", "Debug Report",
                    f"Could not write the debug report: {exc}")
                return
            self._show_message(
                "info", "Debug Report",
                f"Debug report written to:\n{path}\n\nIt holds the "
                "recent actions, any errors, and the log tail. "
                "Nothing in it carries passwords or keys.")

        def _show_about(self):
            from . import __version__

            def make():
                QtWidgets.QMessageBox.about(
                    self, "About Forge Browser",
                    f"Forge Browser {__version__}\n\nYour browser. "
                    "No accounts, no API keys. Nothing leaves this "
                    "machine.\n\n"
                    "A local AI model drives the browsing: search, pages, "
                    "and research, all on this machine.")

            self._run_dialog(make)

        # --- updates (v0.12) ----------------------------------------
        # The launch check is silent and on by default: it runs
        # in a background thread after the window shows, and
        # says nothing at all when the app is current or the
        # check fails. Only a newer release pops up — and only
        # the user's tap starts the download, which is checked
        # against its published checksum before it installs.
        def _maybe_check_for_updates_on_startup(self):
            """The silent launch check (run_app schedules it
            after the window shows). Off means off — no
            request is made at all."""
            from .updates import startup_check_enabled
            if not startup_check_enabled():
                return
            self._start_update_check(manual=False)

        def _on_check_updates(self):
            """Help, Check for updates: a manual check, which
            reports its outcome either way."""
            if not self._start_update_check(manual=True):
                self.statusBar().showMessage(
                    "Already checking for updates.", 4000)
                return
            self.statusBar().showMessage(
                "Checking for updates…", 8000)

        def _on_toggle_update_check(self, checked):
            """Help, Check for updates on startup: remember
            the choice at once. Turning it on does not check
            now — the check runs on the next launch, or by
            hand."""
            from .settings import set_setting
            set_setting("check_updates", "1" if checked else "0")

        def _start_update_check(self, manual):
            """Start one update check in a worker thread,
            unless one is already running. Returns False when
            a check is already running, True when one
            started."""
            if self._update_check_running:
                return False
            self._update_check_running = True
            from . import __version__

            def work():
                from . import updates
                try:
                    result = updates.check_for_update(
                        __version__)
                except Exception as exc:  # noqa: BLE001 - window stays up
                    result = {"state": "unknown", "latest": None,
                              "url": None,
                              "reason": ("the update check "
                                         "failed: %s" % exc)}
                result["manual"] = manual
                self.update_check_done.emit(result)

            threading.Thread(target=work, daemon=True).start()
            return True

        def _on_update_check_done(self, result):
            """One check finished, back on the GUI thread. A
            newer release is offered whether the check was
            manual or automatic. Up to date, or a failed
            check, is told only when the user asked — the
            silent launch check says nothing for either."""
            self._update_check_running = False
            state = result.get("state")
            manual = result.get("manual", False)
            if state == "newer":
                self._offer_update(result)
                return
            if state == "current":
                if manual:
                    from . import __version__
                    self._show_message(
                        "info", "No updates",
                        "You have the newest version (%s)."
                        % __version__)
                return
            if manual:
                self._show_message(
                    "warning", "Could not check for updates",
                    result.get("reason")
                    or "the update check failed")

        def _offer_update(self, result):
            """The update popup: a newer release is out, ask
            first. Only a Yes starts the download. A question
            that arrives while another dialog is up is
            answered No at once (see _ask_yes_no) — the
            person never sees a stale question later."""
            from . import __version__
            latest = result.get("latest") or "a newer version"
            if self._ask_yes_no(
                    "Update available",
                    "Forge Browser %s is out. You have %s.\n\n"
                    "Update now? The download is checked "
                    "against its published checksum before it "
                    "installs, and your vault, memory, and "
                    "history are never touched."
                    % (latest, __version__)):
                self._start_update_download(result)

        def _start_update_download(self, result):
            """Download, verify, and install the update in a
            worker thread, with a progress dialog. One install
            at a time — a second tap while one runs does
            nothing."""
            if self._update_busy:
                return
            self._update_busy = True
            dialog = QtWidgets.QProgressDialog(
                "Downloading the update…", None, 0, 0, self)
            dialog.setCancelButton(None)
            dialog.setWindowModality(
                QtCore.Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            self._update_progress_dialog = dialog
            dialog.show()

            def work():
                from . import updates
                work_dir = tempfile.mkdtemp(
                    prefix="forge-update-download-")
                try:
                    def progress(got, total):
                        self.update_progress.emit(
                            "Downloading the update…", got, total)

                    zip_path = updates.download_and_verify(
                        result, work_dir, on_progress=progress)
                    self.update_progress.emit(
                        "Installing the update…", -1, -1)
                    updates.install_update(zip_path)
                except Exception as exc:  # noqa: BLE001 - reported plainly
                    self.update_install_done.emit(False, str(exc))
                else:
                    self.update_install_done.emit(
                        True, result.get("latest") or "")
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

            threading.Thread(target=work, daemon=True).start()

        def _on_update_progress(self, phase, got, total):
            """A download/install progress step, back on the
            GUI thread. A negative total means the size is
            unknown — the bar runs indeterminate."""
            dialog = self._update_progress_dialog
            if dialog is None:
                return
            dialog.setLabelText(phase)
            if total and total > 0:
                dialog.setMaximum(total)
                dialog.setValue(got)
            else:
                dialog.setMaximum(0)
                dialog.setValue(0)

        def _on_update_install_done(self, ok, message):
            """The download + install finished. On success the
            restart prompt asks: Yes relaunches the new
            version, No leaves the old window running and the
            update finishes on the next launch. On failure the
            reason is shown plainly and the old version keeps
            running — nothing was installed."""
            self._update_busy = False
            dialog = self._update_progress_dialog
            self._update_progress_dialog = None
            if dialog is not None:
                dialog.close()
            if not ok:
                self._show_message(
                    "warning", "The update did not install",
                    "%s\n\nThe old version is still here."
                    % message)
                return
            version = message or "the new version"
            if self._ask_yes_no(
                    "Update installed",
                    "Forge Browser %s is installed. Restart "
                    "now to use it?\n\nIf you say no, the "
                    "update finishes the next time you open "
                    "Forge Browser." % version):
                from . import updates
                folder = updates.find_installed_app()
                if folder is not None:
                    updates.relaunch(folder)
                QtWidgets.QApplication.instance().quit()

        # --- find in page (E4) --------------------------------------
        def _show_find(self):
            self._find_bar.setVisible(True)
            self._find_entry.setFocus()
            self._find_entry.selectAll()
            if self._find_entry.text():
                self._find(self._find_entry.text())

        def _hide_find(self):
            view = self._view()
            if view is not None:
                view.page().findText("")
            self._find_label.setText("")
            self._find_bar.setVisible(False)
            if view is not None:
                view.setFocus()

        def _find(self, text, forward=False, backward=False):
            view = self._view()
            if view is None:
                return
            if not text:
                view.page().findText("")
                self._find_label.setText("")
                return
            flags = (QWebEnginePage.FindFlag.FindBackward if backward
                     else QWebEnginePage.FindFlag(0))
            view.page().findText(text, flags, self._on_find_result)

        def _on_find_result(self, result):
            # Qt 6.12 hands the callback a QWebEngineFindTextResult;
            # older versions handed it a plain bool. Both are read.
            if isinstance(result, bool):
                found = result
                self._find_label.setText("" if found else "No matches")
                return
            # G-UX14: the match count, not just found/not found.
            total = result.numberOfMatches()
            if total <= 0:
                self._find_label.setText("No matches")
                return
            active = result.activeMatch()
            if active < 1:
                active = 1
            self._find_label.setText(f"{active} of {total} matches")

        # --- zoom (E5) ----------------------------------------------
        def _zoom_in(self):
            view = self._view()
            if view is None:
                return
            factor = min(5.0, round(view.zoomFactor() + 0.1, 2))
            view.setZoomFactor(factor)
            self.statusBar().showMessage(f"Zoom {int(factor * 100)}%", 2000)

        def _zoom_out(self):
            view = self._view()
            if view is None:
                return
            factor = max(0.25, round(view.zoomFactor() - 0.1, 2))
            view.setZoomFactor(factor)
            self.statusBar().showMessage(f"Zoom {int(factor * 100)}%", 2000)

        def _zoom_reset(self):
            view = self._view()
            if view is None:
                return
            view.setZoomFactor(1.0)
            self.statusBar().showMessage("Zoom 100%", 2000)

        # --- bookmarks (E6) -----------------------------------------
        def _bookmark_current(self):
            from .bookmarks import (add_bookmark, is_bookmarked,
                                    remove_bookmark)
            view = self._view()
            if view is None:
                return
            url = view.url().toString()
            # R3-24: the refusal says why — the old "This page
            # can't be bookmarked." left the person guessing
            # whether it was a bug or a rule.
            if url.startswith(server.home_url):
                self.statusBar().showMessage(
                    "The start page can't be bookmarked.", 3000)
                return
            if not url.startswith("http"):
                self.statusBar().showMessage(
                    "Only web pages can be bookmarked.", 3000)
                return
            # G-UX16: the star toggles — bookmarked already means
            # unbookmark.
            if is_bookmarked(url):
                remove_bookmark(url)
                _log_action(f"Removed bookmark: {url}")
                self.statusBar().showMessage("Removed the bookmark.", 3000)
                return
            add_bookmark(url, view.title() or url)
            _log_action(f"Bookmarked: {url}")
            self.statusBar().showMessage(
                f"Bookmarked: {view.title() or url}", 3000)

        def _rebuild_bookmarks_menu(self):
            from .bookmarks import list_bookmarks
            menu = self._bookmarks_menu
            menu.clear()
            # G-UX3: the same Ctrl+D action every time (created
            # once in _build_menu), and the per-bookmark actions
            # are parented to the menu, so clear() deletes them
            # instead of leaking another live shortcut per open.
            menu.addAction(self._bookmark_action)
            menu.addSeparator()
            marks = list_bookmarks()[:15]
            for mark in marks:
                title = (mark.get("title") or mark["url"])[:40]
                self._menu_action(
                    menu, title,
                    lambda url=mark["url"]: self.new_tab(url),
                    parent=menu)
            if marks:
                menu.addSeparator()
            self._menu_action(menu, "All Bookmarks…",
                              self._open_bookmarks_dialog, parent=menu)

        def _open_bookmarks_dialog(self):
            from .bookmarks import list_bookmarks, remove_bookmark
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Bookmarks")
            dialog.setMinimumSize(500, 300)
            layout = QtWidgets.QVBoxLayout(dialog)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("bookmarks_list")
            layout.addWidget(listing)
            # R3-12: an empty list says what it is for and how to
            # fill it, instead of showing a blank box.
            empty = QtWidgets.QLabel(
                "No bookmarks yet. Open a web page and press "
                "Ctrl+D to add one.")
            empty.setObjectName("bookmarks_empty")
            empty.setWordWrap(True)
            layout.addWidget(empty)

            def refresh():
                listing.clear()
                empty.setVisible(len(list_bookmarks()) == 0)
                for mark in list_bookmarks():
                    # R3-17: title on one line, address on the
                    # next — the old "Title: URL" single line ran
                    # off the right edge for long entries.
                    title = mark.get("title") or mark["url"]
                    item = QtWidgets.QListWidgetItem(
                        f"{title}\n{mark['url']}")
                    item.setData(QtCore.Qt.ItemDataRole.UserRole,
                                 mark["url"])
                    item.setData(QtCore.Qt.ItemDataRole.UserRole + 1,
                                 title)
                    listing.addItem(item)

            refresh()
            buttons = QtWidgets.QHBoxLayout()
            open_btn = QtWidgets.QPushButton("Open")
            open_btn.setObjectName("bookmarks_open")
            remove_btn = QtWidgets.QPushButton("Remove")
            remove_btn.setObjectName("bookmarks_remove")
            close_btn = QtWidgets.QPushButton("Close")

            def open_selected():
                item = listing.currentItem()
                if item is not None:
                    self.new_tab(item.data(QtCore.Qt.ItemDataRole.UserRole))
                    dialog.accept()

            def remove_selected():
                item = listing.currentItem()
                if item is not None:
                    # A2: a destructive remove confirms first,
                    # default No (U23).
                    url = item.data(QtCore.Qt.ItemDataRole.UserRole)
                    if self._ask_yes_no(
                            "Remove bookmark?",
                            f"Remove this bookmark?\n\n{url}"):
                        remove_bookmark(url)
                        _log_action(f"Removed bookmark: {url}")
                        refresh()

            def rename_selected():
                # G-UX16: rename in place — remove + re-add under
                # the new title (the entry moves to the top of the
                # list, newest first).
                from .bookmarks import add_bookmark
                item = listing.currentItem()
                if item is None:
                    return
                url = item.data(QtCore.Qt.ItemDataRole.UserRole)
                # R3-17: the title rides in the item's data now —
                # the text is two lines ("title\nurl"), so the
                # old ": " split no longer finds it.
                current = (item.data(
                    QtCore.Qt.ItemDataRole.UserRole + 1)
                    or item.text().split("\n", 1)[0])
                title, ok = QtWidgets.QInputDialog.getText(
                    dialog, "Rename bookmark", "Title:", text=current)
                if ok and title.strip():
                    remove_bookmark(url)
                    add_bookmark(url, title.strip())
                    refresh()

            # R3-11: Open, Rename, and Remove need a selection —
            # they sit disabled with nothing picked instead of
            # silently doing nothing when clicked.
            def update_buttons():
                picked = listing.currentItem() is not None
                open_btn.setEnabled(picked)
                rename_btn.setEnabled(picked)
                remove_btn.setEnabled(picked)

            open_btn.clicked.connect(open_selected)
            remove_btn.clicked.connect(remove_selected)
            rename_btn = QtWidgets.QPushButton("Rename")
            rename_btn.setObjectName("bookmarks_rename")
            rename_btn.clicked.connect(rename_selected)
            listing.currentItemChanged.connect(
                lambda *a: update_buttons())
            update_buttons()
            close_btn.clicked.connect(dialog.reject)
            listing.itemDoubleClicked.connect(lambda item: open_selected())
            buttons.addWidget(open_btn)
            buttons.addWidget(rename_btn)
            buttons.addWidget(remove_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            # R3-11: Close takes the initial focus, not the list —
            # a list that takes focus on open auto-selects its
            # first row, which would enable the buttons with no
            # pick.
            close_btn.setFocus()
            dialog.exec()

        # --- history + research windows (B12) -----------------------
        def _open_history_window(self):
            """B12: history as a native window, like the Memory
            dialog — search, open, clear. The /history page stays
            for the start page's link."""
            from .titles import (_HISTORY_LIMIT, clear_history,
                                 load_history)
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("History")
            dialog.setObjectName("history_dialog")
            dialog.setMinimumSize(560, 360)
            layout = QtWidgets.QVBoxLayout(dialog)
            search = QtWidgets.QLineEdit()
            search.setObjectName("history_search")
            search.setPlaceholderText("Search history")
            layout.addWidget(search)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("history_list")
            layout.addWidget(listing)
            # R3-12: an empty list says what it is for and how to
            # fill it, instead of showing a blank box.
            empty = QtWidgets.QLabel(
                "No visits recorded yet. Browse a page and it "
                "shows up here.")
            empty.setObjectName("history_empty")
            empty.setWordWrap(True)
            layout.addWidget(empty)
            cap = QtWidgets.QLabel(
                f"History keeps the newest {_HISTORY_LIMIT} visits. "
                "Older visits drop off.")
            cap.setWordWrap(True)
            layout.addWidget(cap)

            def refresh():
                needle = search.text().strip().lower()
                listing.clear()
                for entry in load_history():
                    if not isinstance(entry, dict):
                        continue
                    url = entry.get("url")
                    if not url:
                        continue
                    title = entry.get("title") or url
                    if needle and needle not in title.lower() \
                            and needle not in url.lower():
                        continue
                    # R3-17: title on one line, address on the
                    # next, like bookmarks.
                    item = QtWidgets.QListWidgetItem(f"{title}\n{url}")
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                    listing.addItem(item)
                # R3-12: the guidance shows only when the list
                # is empty (a search with no matches counts).
                empty.setVisible(listing.count() == 0)

            search.textChanged.connect(refresh)
            refresh()
            buttons = QtWidgets.QHBoxLayout()
            open_btn = QtWidgets.QPushButton("Open")
            open_btn.setObjectName("history_open")
            clear_btn = QtWidgets.QPushButton("Clear History…")
            close_btn = QtWidgets.QPushButton("Close")

            def open_selected():
                item = listing.currentItem()
                if item is not None:
                    self.new_tab(
                        item.data(QtCore.Qt.ItemDataRole.UserRole))
                    dialog.accept()

            def clear_all():
                if self._ask_yes_no(
                        "Clear history?",
                        "This deletes all recorded visits. "
                        "It cannot be undone.\n\nClear history?"):
                    clear_history()
                    _log_action("Cleared history")
                    refresh()

            # R3-11: Open needs a selection — it sits disabled
            # with nothing picked instead of silently doing
            # nothing when clicked.
            def update_buttons():
                open_btn.setEnabled(listing.currentItem() is not None)

            open_btn.clicked.connect(open_selected)
            clear_btn.clicked.connect(clear_all)
            listing.currentItemChanged.connect(
                lambda *a: update_buttons())
            update_buttons()
            close_btn.clicked.connect(dialog.reject)
            listing.itemDoubleClicked.connect(lambda item: open_selected())
            buttons.addWidget(open_btn)
            buttons.addWidget(clear_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            dialog.exec()

        def _open_research_window(self):
            """B12: research reports as a native window — the list,
            one click to open a report in a tab."""
            from .research import list_reports
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Research Reports")
            dialog.setObjectName("research_dialog")
            dialog.setMinimumSize(560, 360)
            layout = QtWidgets.QVBoxLayout(dialog)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("research_list")
            layout.addWidget(listing)
            for report in list_reports():
                item = QtWidgets.QListWidgetItem(report["topic"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole,
                             report["name"])
                listing.addItem(item)
            # R3-14: the empty state is a wrapping label, not a
            # list item — the old single line ran off the right
            # edge with a horizontal scrollbar.
            empty = QtWidgets.QLabel(
                "No research reports saved yet. Ask the browser "
                "to research something and the report lands here.")
            empty.setObjectName("research_empty")
            empty.setWordWrap(True)
            layout.addWidget(empty)
            empty.setVisible(listing.count() == 0)
            listing.setVisible(listing.count() > 0)
            buttons = QtWidgets.QHBoxLayout()
            open_btn = QtWidgets.QPushButton("Open")
            open_btn.setObjectName("research_open")
            close_btn = QtWidgets.QPushButton("Close")

            def open_selected():
                item = listing.currentItem()
                if item is None:
                    return
                name = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if name:
                    self.new_tab(server.home_url + "research/" + name)
                    dialog.accept()

            # R3-11: Open needs a selection — it sits disabled
            # with nothing picked instead of silently doing
            # nothing when clicked.
            def update_buttons():
                item = listing.currentItem()
                open_btn.setEnabled(
                    item is not None
                    and bool(item.data(
                        QtCore.Qt.ItemDataRole.UserRole)))

            open_btn.clicked.connect(open_selected)
            listing.currentItemChanged.connect(
                lambda *a: update_buttons())
            update_buttons()
            close_btn.clicked.connect(dialog.reject)
            listing.itemDoubleClicked.connect(lambda item: open_selected())
            buttons.addWidget(open_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            # R3-11: Close takes the initial focus, not the list —
            # a list that takes focus on open auto-selects its
            # first row, which would enable Open with no pick.
            close_btn.setFocus()
            dialog.exec()

        # --- downloads (E1) -----------------------------------------
        def _on_download_requested(self, download):
            # The file downloads to a temp name in the download
            # folder (same filesystem, so the final move is the
            # atomic link in keep_download), then lands under its
            # uniquified name when it finishes.
            from .files import download_dir
            from .settings import get_setting
            name = download.downloadFileName() or "download"
            folder = download_dir()
            save_as = None
            if get_setting("download_ask") == "1":
                # R3-32: ask where to save this one — the dialog
                # suggests the download folder and name, and its
                # own overwrite prompt covers an existing file.
                # Cancelling the dialog cancels the download.
                picked, _filter = (
                    QtWidgets.QFileDialog.getSaveFileName(
                        self, "Save download",
                        os.path.join(folder, name)))
                if not picked:
                    download.cancel()
                    return
                save_as = picked
                folder = os.path.dirname(picked) or folder
                name = os.path.basename(picked)
            tmp_name = f".forge-download-{id(download)}.part"
            download.setDownloadDirectory(folder)
            download.setDownloadFileName(tmp_name)
            download.accept()
            record = {"download": download, "name": name,
                      "tmp": os.path.join(folder, tmp_name),
                      "folder": folder, "final": None,
                      "save_as": save_as,
                      "status": "downloading",
                      # G-UX10: the URL + the partial file are what
                      # a retry resumes from; "error" is why a
                      # failure failed (G-UX13).
                      "url": download.url().toString(),
                      "error": "",
                      "retry_received": 0, "retry_total": 0}
            self._downloads[id(download)] = record
            download.receivedBytesChanged.connect(
                self._refresh_downloads_list)
            download.totalBytesChanged.connect(
                self._refresh_downloads_list)
            download.stateChanged.connect(
                lambda state, key=id(download):
                    self._on_download_state(key, state))
            _log_action(f"Started downloading: {name}")
            self.statusBar().showMessage(f"Downloading {name}…", 5000)
            self._refresh_downloads_list()

        def _download_line(self, record):
            download = record["download"]
            name = record["name"]
            if record["status"] == "done":
                return f"{name}: Saved to {record['final']}"
            if record["status"] == "cancelled":
                return f"{name}: Cancelled"
            if record["status"] == "failed":
                error = record.get("error", "")
                return (f"{name}: Failed ({error})" if error
                        else f"{name}: Failed")
            if record["status"] == "retrying":
                received = record.get("retry_received", 0)
                total = record.get("retry_total", 0)
                if total > 0:
                    pct = int(received * 100 / total)
                    return (f"{name}: Retrying… {pct}% "
                            f"({_human_size(received)} of "
                            f"{_human_size(total)})")
                return f"{name}: Retrying… {_human_size(received)}"
            received = download.receivedBytes()
            total = download.totalBytes()
            if total > 0:
                pct = int(received * 100 / total)
                return (f"{name}: {pct}% ({_human_size(received)} of "
                        f"{_human_size(total)})")
            return f"{name}: {_human_size(received)}"

        def _refresh_downloads_list(self):
            # G-UX7: rows update in place instead of clearing and
            # rebuilding, so the selection survives a progress
            # tick and Cancel stays usable during a download.
            listing = getattr(self, "_downloads_list", None)
            if listing is None:
                return
            selected_key = None
            current = listing.currentItem()
            if current is not None:
                selected_key = current.data(
                    QtCore.Qt.ItemDataRole.UserRole)
            existing = {}
            for row in range(listing.count()):
                item = listing.item(row)
                existing[item.data(QtCore.Qt.ItemDataRole.UserRole)] = item
            for key, record in self._downloads.items():
                line = self._download_line(record)
                if key in existing:
                    if existing[key].text() != line:
                        existing[key].setText(line)
                else:
                    item = QtWidgets.QListWidgetItem(line)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                    listing.addItem(item)
            for row in range(listing.count() - 1, -1, -1):
                item = listing.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) \
                        not in self._downloads:
                    listing.takeItem(row)
            if selected_key is not None:
                for row in range(listing.count()):
                    item = listing.item(row)
                    if item.data(QtCore.Qt.ItemDataRole.UserRole) \
                            == selected_key:
                        listing.setCurrentRow(row)
                        break
            self._update_download_buttons()

        def _update_download_buttons(self):
            """Enable the downloads dialog's per-file buttons for
            the selected record (G-UX10/G-UX13)."""
            listing = getattr(self, "_downloads_list", None)
            buttons = getattr(self, "_download_buttons", None)
            if listing is None or buttons is None:
                return
            record = None
            item = listing.currentItem()
            if item is not None:
                record = self._downloads.get(
                    item.data(QtCore.Qt.ItemDataRole.UserRole))
            status = record["status"] if record else None
            buttons["open"].setEnabled(
                record is not None and status == "done"
                and bool(record.get("final")))
            buttons["show"].setEnabled(
                record is not None and status == "done"
                and bool(record.get("final")))
            buttons["retry"].setEnabled(
                record is not None and status == "failed"
                and bool(record.get("url")))
            buttons["cancel"].setEnabled(status == "downloading")

        def _prune_downloads(self):
            """G-P20: finished records past the keep count drop
            out, so the dict stops growing for the app's life.
            Active downloads are never pruned."""
            finished = [key for key, record in self._downloads.items()
                        if record["status"] in ("done", "cancelled",
                                                "failed")]
            if len(finished) > _DOWNLOADS_KEEP:
                for key in finished[:len(finished) - _DOWNLOADS_KEEP]:
                    del self._downloads[key]

        def _remove_download_tmp(self, record):
            try:
                os.unlink(record["tmp"])
            except OSError:
                pass

        def _finish_download_file(self, record):
            """Land a finished download at its final path (R3-32):
            the path the person picked with Save as, or the
            uniquified name in the download folder. Returns the
            final path."""
            chosen = record.get("save_as")
            if chosen:
                parent = os.path.dirname(chosen)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                os.replace(record["tmp"], chosen)
                return chosen
            from .files import keep_download
            return keep_download(record["tmp"], record["name"],
                                 record["folder"])

        def _on_download_state(self, key, state):
            record = self._downloads.get(key)
            if record is None:
                return
            states = QWebEngineDownloadRequest.DownloadState
            if state == states.DownloadCompleted:
                try:
                    record["final"] = self._finish_download_file(record)
                    record["status"] = "done"
                    _log_action(
                        f"Downloaded: {record['name']} to "
                        f"{record['final']}")
                    self.statusBar().showMessage(
                        f"Downloaded {record['name']} to {record['final']}",
                        8000)
                except (OSError, BrowserError) as exc:
                    # G-C4: keep_download's failure path raises
                    # BrowserError, not OSError — the old handler
                    # let it escape the slot and left the record
                    # stuck at "downloading" forever.
                    record["status"] = "failed"
                    record["error"] = str(exc)
                    _log_error(
                        f"Could not save {record['name']}: {exc}")
                    self._remove_download_tmp(record)
                    self.statusBar().showMessage(
                        f"Could not save {record['name']}: {exc}", 8000)
            elif state == states.DownloadCancelled:
                record["status"] = "cancelled"
                self._remove_download_tmp(record)
            elif state == states.DownloadInterrupted:
                # G-UX10: the partial file is KEPT, so Retry can
                # resume it instead of starting over.
                record["status"] = "failed"
                record["error"] = "the connection dropped"
                _log_error(f"Download failed: {record['name']}")
                self.statusBar().showMessage(
                    f"Download failed: {record['name']}", 8000)
            self._prune_downloads()
            self._refresh_downloads_list()

        def _on_download_retry_progress(self, key, received, total):
            record = self._downloads.get(key)
            if record is None:
                return
            record["retry_received"] = received
            record["retry_total"] = total
            self._refresh_downloads_list()

        def _on_download_retry_done(self, key, ok, detail):
            record = self._downloads.get(key)
            if record is None:
                return
            if ok:
                record["final"] = detail
                record["status"] = "done"
                _log_action(f"Downloaded (retried): {record['name']}")
                self.statusBar().showMessage(
                    f"Downloaded {record['name']} to {detail}", 8000)
            else:
                record["status"] = "failed"
                record["error"] = detail
                _log_error(
                    f"Download retry failed: {record['name']}: {detail}")
                self.statusBar().showMessage(
                    f"Retry failed: {record['name']}", 8000)
            self._refresh_downloads_list()

        def _retry_download(self, key):
            """G-UX10: resume a failed download from its partial
            file, through the engine room's resumable fetch
            (Range requests). The browser's cookies do not travel
            with it, so a login-gated download may still fail —
            the error says so plainly."""
            record = self._downloads.get(key)
            if record is None or record["status"] != "failed" \
                    or not record.get("url"):
                return
            record["status"] = "retrying"
            record["retry_received"] = 0
            record["retry_total"] = 0
            self._refresh_downloads_list()

            def work_retry():
                from ._http import http_get_to_file
                try:
                    _final, _headers, _size = http_get_to_file(
                        record["url"], record["tmp"], resume=True,
                        on_progress=lambda got, total:
                            self.download_retry_progress.emit(
                                key, got, total or 0))
                    final = self._finish_download_file(record)
                    self.download_retry_done.emit(key, True, final)
                except Exception as exc:
                    self.download_retry_done.emit(key, False, str(exc))

            threading.Thread(target=work_retry, daemon=True).start()

        def _on_downloads_dialog_closed(self, *args):
            self._downloads_dialog = None
            self._downloads_list = None
            self._download_buttons = None

        def _open_downloads_dialog(self):
            if self._downloads_dialog is not None:
                self._downloads_dialog.show()
                self._downloads_dialog.raise_()
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Downloads")
            dialog.setMinimumSize(500, 300)
            dialog.setObjectName("downloads_dialog")
            # A modeless dialog's close() doesn't emit finished(),
            # so the window learns it closed from destroyed().
            dialog.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
            layout = QtWidgets.QVBoxLayout(dialog)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("downloads_list")
            layout.addWidget(listing)
            self._downloads_list = listing
            # R3-08: the buttons sit on two rows, not one — six
            # buttons in a row overflowed the dialog's width and
            # the last ones were cut off. Row one holds the
            # per-file actions, row two the folder and Close.
            buttons = QtWidgets.QHBoxLayout()
            open_btn = QtWidgets.QPushButton("Open")
            open_btn.setObjectName("downloads_open")
            show_btn = QtWidgets.QPushButton("Show in folder")
            show_btn.setObjectName("downloads_show")
            retry_btn = QtWidgets.QPushButton("Retry")
            retry_btn.setObjectName("downloads_retry")
            cancel_btn = QtWidgets.QPushButton("Cancel")
            cancel_btn.setObjectName("downloads_cancel")
            folder_btn = QtWidgets.QPushButton("Open folder")
            close_btn = QtWidgets.QPushButton("Close")
            self._download_buttons = {"open": open_btn, "show": show_btn,
                                      "retry": retry_btn,
                                      "cancel": cancel_btn}

            def selected_record():
                item = listing.currentItem()
                if item is None:
                    return None
                return self._downloads.get(
                    item.data(QtCore.Qt.ItemDataRole.UserRole))

            def open_folder():
                from .files import download_dir
                QtGui.QDesktopServices.openUrl(
                    QtCore.QUrl.fromLocalFile(download_dir()))

            def open_selected():
                # G-UX13: open the finished file itself.
                record = selected_record()
                if record is not None and record.get("final"):
                    QtGui.QDesktopServices.openUrl(
                        QtCore.QUrl.fromLocalFile(record["final"]))

            def show_selected():
                # G-UX13: show the finished file in its folder.
                record = selected_record()
                if record is not None and record.get("final"):
                    QtGui.QDesktopServices.openUrl(
                        QtCore.QUrl.fromLocalFile(
                            os.path.dirname(record["final"])))

            def retry_selected():
                item = listing.currentItem()
                if item is not None:
                    self._retry_download(
                        item.data(QtCore.Qt.ItemDataRole.UserRole))

            def cancel_selected():
                record = selected_record()
                if record is not None:
                    record["download"].cancel()

            folder_btn.clicked.connect(open_folder)
            open_btn.clicked.connect(open_selected)
            show_btn.clicked.connect(show_selected)
            retry_btn.clicked.connect(retry_selected)
            cancel_btn.clicked.connect(cancel_selected)
            close_btn.clicked.connect(dialog.close)
            listing.currentItemChanged.connect(
                lambda *a: self._update_download_buttons())
            buttons.addWidget(open_btn)
            buttons.addWidget(show_btn)
            buttons.addWidget(retry_btn)
            buttons.addWidget(cancel_btn)
            buttons.addStretch(1)
            layout.addLayout(buttons)
            bottom_row = QtWidgets.QHBoxLayout()
            bottom_row.addWidget(folder_btn)
            bottom_row.addStretch(1)
            bottom_row.addWidget(close_btn)
            layout.addLayout(bottom_row)
            dialog.destroyed.connect(self._on_downloads_dialog_closed)
            self._downloads_dialog = dialog
            self._refresh_downloads_list()
            # R3-11: Close takes the initial focus, not the list —
            # a list that takes focus on open auto-selects its
            # first row, which would enable the buttons with no
            # pick.
            close_btn.setFocus()
            dialog.show()

        # --- vault autofill (E3) ------------------------------------
        def _maybe_autofill(self, view):
            # A login form on a real page: offer the vault's saved
            # login for the site — with approval, never silently.
            # Forge Browser's own pages are skipped.
            url = view.url().toString()
            if not url.startswith("http") or url.startswith(server.home_url):
                return
            host = urllib.parse.urlparse(url).hostname or ""
            if not host or host in self._autofill_asked \
                    or host in self._autofill_done:
                return
            script = (
                "(() => { const pw = document.querySelector("
                "'input[type=password]');"
                " if (!pw) return '';"
                " const form = pw.form;"
                " const user = form ? form.querySelector('input[type=text],"
                " input[type=email], input:not([type])')"
                " : document.querySelector('input[type=text],"
                " input[type=email]');"
                " return user ? 'login' : 'password-only'; })()")
            view.page().runJavaScript(
                script,
                lambda result, view=view, url=url, host=host:
                    self._on_login_form_found(view, url, host, result))

        def _on_login_form_found(self, view, url, host, result):
            # G-UX15: password-only forms get the offer too.
            if result not in ("login", "password-only"):
                return
            if host in self._autofill_asked or host in self._autofill_done:
                return
            from .vault import default_vault_path, find_entry_for_url
            if not os.path.exists(default_vault_path()):
                return  # no vault, nothing to fill
            if self._vault is None:
                # B4: the vault unlocks only from the Vault window
                # (Tools menu) — autofill never prompts for the
                # passphrase mid-browse. The host is NOT marked
                # asked, so unlocking later still offers the fill.
                if not self._vault_hint_shown:
                    self._vault_hint_shown = True
                    self.statusBar().showMessage(
                        "A login is saved for this site. Unlock the "
                        "vault from Tools, then reload to fill it.",
                        8000)
                return
            self._autofill_asked.add(host)
            found = find_entry_for_url(self._vault, url)
            if found is None:
                return  # no saved login for this site
            site, entry = found
            if result == "password-only":
                question = (f"Fill the saved password for {site}?\n\n"
                            "The password is filled in but never shown.")
            else:
                question = (f"Fill the saved login for {site}?\n\n"
                            f"Username: {entry.get('username', '')}\n"
                            "The password is filled in but never shown.")
            if not self._ask_yes_no("Fill login?", question):
                return
            # G-S10: the page may have navigated during the
            # dialogs — re-verify the tab still shows the entry's
            # site immediately before filling.
            current_host = (urllib.parse.urlparse(
                view.url().toString()).hostname or "").lower()
            if current_host != host.lower():
                self.statusBar().showMessage(
                    "The page changed before the login could be "
                    "filled.", 5000)
                return
            self._fill_login(
                view,
                entry.get("username", "") if result == "login" else "",
                entry.get("password", ""))
            self._autofill_done.add(host)
            _log_action(f"Filled the saved login for {site}")
            self.statusBar().showMessage(
                f"Filled the saved login for {site}. Check it and sign in.",
                8000)

        def _fill_login(self, view, username, password):
            # Values are set through the native setter with input +
            # change events, so pages that watch their fields notice.
            # Nothing is submitted — the person checks and signs in.
            script = (
                "(() => { const pw = document.querySelector("
                "'input[type=password]');"
                " const form = pw ? pw.form : null;"
                " const user = form ? form.querySelector('input[type=text],"
                " input[type=email], input:not([type])')"
                " : document.querySelector('input[type=text],"
                " input[type=email]');"
                " function setVal(el, v) { if (!el) return;"
                " const proto = el.tagName === 'TEXTAREA'"
                " ? HTMLTextAreaElement.prototype"
                " : HTMLInputElement.prototype;"
                " const setter = Object.getOwnPropertyDescriptor(proto,"
                " 'value').set; setter.call(el, v);"
                " el.dispatchEvent(new Event('input', {bubbles: true}));"
                " el.dispatchEvent(new Event('change', {bubbles: true})); }"
                f" setVal(user, {json.dumps(username)});"
                f" setVal(pw, {json.dumps(password)});"
                " return 'filled'; })()")
            view.page().runJavaScript(script)

        # --- vault window (B4, U1) ----------------------------------
        def _open_vault_window(self):
            """The vault in the window at last: unlock/lock, list,
            add, delete. This is the one place that asks for the
            passphrase — autofill never prompts mid-browse."""
            from .vault import Vault, default_vault_path
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Vault")
            dialog.setObjectName("vault_dialog")
            # R3-18: the minimum fits the short pages (create /
            # locked); the entry list grows the dialog when it
            # shows, so the short pages no longer sit in a tall
            # box of dead space.
            dialog.setMinimumSize(520, 220)
            layout = QtWidgets.QVBoxLayout(dialog)
            status = QtWidgets.QLabel("")
            status.setObjectName("vault_status")
            status.setWordWrap(True)
            layout.addWidget(status)
            stack = QtWidgets.QStackedWidget()
            layout.addWidget(stack, 1)

            # Page 0: locked — passphrase + Unlock.
            locked_page = QtWidgets.QWidget()
            locked_layout = QtWidgets.QVBoxLayout(locked_page)
            locked_layout.addWidget(QtWidgets.QLabel(
                "The vault is locked. Enter the passphrase to unlock it."))
            pass_entry = QtWidgets.QLineEdit()
            pass_entry.setObjectName("vault_passphrase")
            pass_entry.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            locked_layout.addWidget(pass_entry)
            unlock_btn = QtWidgets.QPushButton("Unlock")
            unlock_btn.setObjectName("vault_unlock")
            locked_layout.addWidget(unlock_btn)
            locked_layout.addStretch(1)
            stack.addWidget(locked_page)

            # Page 1: no vault yet — choose a passphrase + Create.
            create_page = QtWidgets.QWidget()
            create_layout = QtWidgets.QVBoxLayout(create_page)
            # R3-18: the status line above already says "No vault
            # yet." — the instruction does not repeat it.
            create_layout.addWidget(QtWidgets.QLabel(
                "Choose a passphrase (at least 8 characters) "
                "to create one."))
            new_pass = QtWidgets.QLineEdit()
            new_pass.setObjectName("vault_new_passphrase")
            new_pass.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            create_layout.addWidget(new_pass)
            confirm_pass = QtWidgets.QLineEdit()
            confirm_pass.setObjectName("vault_confirm_passphrase")
            confirm_pass.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            confirm_pass.setPlaceholderText("Repeat the passphrase")
            create_layout.addWidget(confirm_pass)
            create_btn = QtWidgets.QPushButton("Create vault")
            create_btn.setObjectName("vault_create")
            create_layout.addWidget(create_btn)
            create_layout.addStretch(1)
            stack.addWidget(create_page)

            # Page 2: unlocked — the entries, add/delete/lock.
            list_page = QtWidgets.QWidget()
            list_layout = QtWidgets.QVBoxLayout(list_page)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("vault_list")
            list_layout.addWidget(listing)
            entry_buttons = QtWidgets.QHBoxLayout()
            add_btn = QtWidgets.QPushButton("Add…")
            add_btn.setObjectName("vault_add")
            delete_btn = QtWidgets.QPushButton("Delete")
            delete_btn.setObjectName("vault_delete")
            lock_btn = QtWidgets.QPushButton("Lock vault")
            lock_btn.setObjectName("vault_lock")
            entry_buttons.addWidget(add_btn)
            entry_buttons.addWidget(delete_btn)
            entry_buttons.addStretch(1)
            entry_buttons.addWidget(lock_btn)
            list_layout.addLayout(entry_buttons)
            stack.addWidget(list_page)

            def refresh():
                # R3-18: the dialog fits the page it shows — the
                # entry list gets room, the short pages do not
                # carry 360 px of dead space.
                if self._vault is not None:
                    status.setText("Unlocked. Passwords are never shown.")
                    listing.clear()
                    for item in self._vault.list_entries():
                        row = QtWidgets.QListWidgetItem(
                            f"{item['site']} ({item['username']})")
                        row.setData(QtCore.Qt.ItemDataRole.UserRole,
                                    item["site"])
                        listing.addItem(row)
                    stack.setCurrentIndex(2)
                    dialog.resize(520, 360)
                elif os.path.exists(default_vault_path()):
                    status.setText("The vault is locked.")
                    stack.setCurrentIndex(0)
                    dialog.resize(520, 240)
                else:
                    status.setText("No vault yet.")
                    stack.setCurrentIndex(1)
                    dialog.resize(520, 260)
                # R3-11: refresh leaves nothing selected — the
                # page switch moves focus to the list, and a list
                # that takes focus auto-selects its first row.
                # Clearing here (after the switch) is what keeps
                # Delete disabled until the person picks an entry.
                listing.setCurrentRow(-1)
                # R3-11: Delete needs a selected entry.
                delete_btn.setEnabled(
                    listing.currentItem() is not None)

            def do_unlock():
                try:
                    self._vault = Vault.open(default_vault_path(),
                                             pass_entry.text())
                except Exception as exc:
                    _log_error(f"Vault unlock failed: {exc}")
                    status.setText(f"Could not unlock the vault: {exc}")
                    return
                pass_entry.clear()
                _log_action("Unlocked the vault")
                refresh()
                # R3-11: after unlock the list page shows — Add
                # takes the focus, not the list, or Qt
                # auto-selects the first entry and Delete enables
                # with no pick.
                add_btn.setFocus()

            def do_create():
                first = new_pass.text()
                second = confirm_pass.text()
                if len(first) < 8:
                    status.setText(
                        "The passphrase needs at least 8 characters.")
                    return
                if first != second:
                    status.setText("The two passphrases do not match.")
                    return
                try:
                    self._vault = Vault.create(default_vault_path(), first)
                except Exception as exc:
                    _log_error(f"Vault creation failed: {exc}")
                    status.setText(f"Could not create the vault: {exc}")
                    return
                new_pass.clear()
                confirm_pass.clear()
                _log_action("Created the vault")
                refresh()

            def do_lock():
                self._vault = None
                self._autofill_asked.clear()
                self._autofill_done.clear()
                _log_action("Locked the vault")
                refresh()

            def do_add():
                form = QtWidgets.QDialog(dialog)
                form.setWindowTitle("Add a login")
                form_layout = QtWidgets.QVBoxLayout(form)
                site_entry = QtWidgets.QLineEdit()
                site_entry.setPlaceholderText("Site (example.org)")
                user_entry = QtWidgets.QLineEdit()
                user_entry.setPlaceholderText("Username")
                pw_entry = QtWidgets.QLineEdit()
                pw_entry.setPlaceholderText("Password")
                pw_entry.setEchoMode(
                    QtWidgets.QLineEdit.EchoMode.Password)
                notes_entry = QtWidgets.QLineEdit()
                notes_entry.setPlaceholderText("Notes (optional)")
                for label, entry in (
                        ("Site:", site_entry), ("Username:", user_entry),
                        ("Password:", pw_entry), ("Notes:", notes_entry)):
                    form_layout.addWidget(QtWidgets.QLabel(label))
                    form_layout.addWidget(entry)
                form_buttons = QtWidgets.QHBoxLayout()
                save_btn = QtWidgets.QPushButton("Save")
                form_cancel = QtWidgets.QPushButton("Cancel")
                form_buttons.addStretch(1)
                form_buttons.addWidget(save_btn)
                form_buttons.addWidget(form_cancel)
                form_layout.addLayout(form_buttons)

                def save_entry():
                    site = site_entry.text().strip()
                    if not site:
                        return
                    self._vault.set(site, user_entry.text().strip(),
                                    pw_entry.text(), notes_entry.text())
                    _log_action(f"Added a vault entry for {site}")
                    form.accept()

                save_btn.clicked.connect(save_entry)
                form_cancel.clicked.connect(form.reject)
                if form.exec():
                    refresh()

            def do_delete():
                item = listing.currentItem()
                if item is None:
                    return
                site = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if self._ask_yes_no(
                        "Delete login?",
                        f"Delete the saved login for {site}?\n\n"
                        "It cannot be undone."):
                    self._vault.delete(site)
                    _log_action(f"Deleted a vault entry for {site}")
                    refresh()

            unlock_btn.clicked.connect(do_unlock)
            pass_entry.returnPressed.connect(do_unlock)
            create_btn.clicked.connect(do_create)
            lock_btn.clicked.connect(do_lock)
            add_btn.clicked.connect(do_add)
            delete_btn.clicked.connect(do_delete)
            listing.currentItemChanged.connect(
                lambda *a: delete_btn.setEnabled(
                    listing.currentItem() is not None))
            close_btn = QtWidgets.QPushButton("Close")
            close_btn.clicked.connect(dialog.accept)
            layout.addWidget(close_btn)
            refresh()
            # R3-11: when the vault is already unlocked the list
            # page shows at once — Add takes the initial focus,
            # not the list, or Qt auto-selects the first entry
            # and Delete enables with no pick.
            if self._vault is not None:
                add_btn.setFocus()
            dialog.exec()

        # --- settings (E9) ------------------------------------------
        def _open_settings(self):
            open_settings_dialog(self, on_applied=self._refresh_ai_controls)

    window = Window()
    _attach_chat(window, QtCore, QtGui, QtWidgets)
    return window


def _attach_chat(window, QtCore, QtGui, QtWidgets):
    """The AI part: a chat dock wired to a local-model ChatSession.

    The person types plain language; the brain searches/opens/
    researches with the engine room, opens pages in real tabs,
    and answers from what it found. No brain (Ollama down) leaves
    the browser fully usable — the dock just says so.
    """
    from .chat import ChatSession, LiveDisplay
    from .retrieve import retrieve_note

    class Signals(QtCore.QObject):
        notice = QtCore.Signal(str)
        open_url = QtCore.Signal(str)
        token = QtCore.Signal(str)
        answer = QtCore.Signal(str)
        failed = QtCore.Signal(str)
        step = QtCore.Signal(str)  # what the turn is doing right now
        voice_text = QtCore.Signal(str)
        voice_failed = QtCore.Signal(str)  # S2: transcription failure reason
        plan = QtCore.Signal(str, object)  # plan text, {"event", "approved"}
        compare_table = QtCore.Signal(str)  # comparison table HTML
        progress = QtCore.Signal(str)  # research progress line
        research_done = QtCore.Signal(object, object, object)  # report, sources, error
        brief_done = QtCore.Signal(str)  # U9: a background brief build's text
        brain_checked = QtCore.Signal(bool, str)  # answer-check: answers?, model
        brain_ready = QtCore.Signal(object, object)  # G-P1: session, models
        brain_fix_done = QtCore.Signal()  # G-UX5: Start Ollama / Get model done

    signals = Signals()
    # G-P1: the brain pick is NOT made here anymore — ChatSession()
    # blocks on Ollama (up to 5 s) and used to freeze the launch
    # with no window on screen. The window paints first with a
    # "checking…" header; the pick runs on a background thread and
    # lands through brain_ready.
    session = None
    window._chat_session = None
    # R3-04: "checking" is True while the answer-check runs — the
    # header says "(checking…)" instead of looking healthy for
    # the ~40 s the check can take.
    brain_health = {"checked": False, "answers": None, "checking": False}
    retrieval_state = {"models": []}  # fill_seat_combos keeps this fresh

    def brain_header_text():
        """The dock header from the session's pick: the model, how it
        was picked (G8), whether it answers (G7), and any
        fall-through note (G3). Ollama-down and no-models get
        different lines — the fix is different (G9). Plus the
        retrieval line (I1): on when nomic-embed-text is
        installed, off with the fix otherwise."""
        if session is None:
            return "Checking for a brain…"
        if session.brain_state == "ollama-down":
            text = ("Brain: none. Ollama is not running. Start Ollama, "
                    "then ↻. Browsing still works.")
        elif session.brain_state == "no-models":
            text = ("Brain: none. Ollama is running but no chat model "
                    "is installed. Pull a small model, then ↻. "
                    "Browsing still works.")
        elif not session.model:
            text = ("Brain: none answering. Start Ollama, then ↻. "
                    "Browsing still works.")
        else:
            text = f"Brain: {session.model}"
            if session.pick_reason and session.pick_reason != "given":
                text += f" ({session.pick_reason})"
            if brain_health["checking"]:
                text += " (checking…)"
            elif brain_health["checked"]:
                text += (" (answering)" if brain_health["answers"]
                         else " (not answering)")
            if session.pick_note:
                text += f"\n{session.pick_note}"
        models = retrieval_state.get("models") or []
        if any("nomic-embed-text" in m for m in models):
            text += "\nRetrieval: on"
        elif session.brain_state == "ollama-down":
            text += "\nRetrieval: off. Ollama is not running"
        else:
            text += "\nRetrieval: off. Pull nomic-embed-text"
        return text

    def _check_brain_in_background():
        # G7: prove the picked model answers, off the GUI thread.
        # R3-04: the header shows "(checking…)" while the check
        # runs — it used to look healthy until "(not answering)"
        # appeared up to 40 s later.
        if session is None:
            return
        model = session.model
        if not model:
            return
        brain_health["checking"] = True
        brain_health["checked"] = False
        brain_health["answers"] = None
        apply_ai_state()

        def work_check():
            from .chat import check_model_answers
            signals.brain_checked.emit(check_model_answers(model), model)

        threading.Thread(target=work_check, daemon=True).start()

    def _start_brain_pick(refresh=False):
        """G-P1: pick the brain (and read the model list) off the
        GUI thread. refresh=True re-picks the existing session
        (the ↻ button) instead of making a new one, so turn
        history survives a refresh."""
        def work_pick():
            from .chat import ChatSession, check_ollama
            if refresh and session is not None:
                session.repick()
                picked = session
            else:
                # B10: the session's log hook is the debug log —
                # failures caught inside a turn land in it.
                picked = ChatSession(on_log=_chat_log)
            _reachable, details = check_ollama()
            signals.brain_ready.emit(picked, details)

        threading.Thread(target=work_pick, daemon=True).start()

    def _build_brief_in_background():
        # The morning brief builds once a day in the background; the
        # start page shows it on its next load. A failed brain call
        # is not cached, so the next launch retries. T2: the brief
        # is a brain job — it does not build when AI/chat is off.
        # G-C20: the brief gets its own session — sharing the turn
        # session raced on its per-turn state.
        from .seats import seats_enabled
        if not seats_enabled("chat"):
            return
        from .brief import build_brief
        from .chat import ChatSession
        try:
            build_brief(ChatSession())
        except Exception:
            pass

    threading.Thread(target=_build_brief_in_background, daemon=True).start()

    dock = QtWidgets.QDockWidget("Chat: talk to your browser", window)
    dock.setObjectName("chat_dock")  # saveState needs a name
    panel = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(panel)
    header = QtWidgets.QLabel(brain_header_text())
    header.setObjectName("brain_header")
    header.setWordWrap(True)
    layout.addWidget(header)
    # G-UX5: the no-brain states get buttons that fix them, not
    # terminal instructions — Start Ollama, Get a chat model.
    # apply_ai_state shows the one that fits the state.
    fix_row = QtWidgets.QHBoxLayout()
    start_ollama_btn = QtWidgets.QPushButton("Start Ollama")
    start_ollama_btn.setObjectName("start_ollama_button")
    start_ollama_btn.setVisible(False)
    get_model_btn = QtWidgets.QPushButton("Get a chat model")
    get_model_btn.setObjectName("get_model_button")
    get_model_btn.setVisible(False)
    # R3-05: a picked brain that does not answer gets a fix button
    # too — the old code only showed fix buttons for Ollama-down
    # and no-models, so "(not answering)" was a dead end.
    retry_brain_btn = QtWidgets.QPushButton("Retry brain")
    retry_brain_btn.setObjectName("retry_brain_button")
    retry_brain_btn.setVisible(False)
    fix_row.addWidget(start_ollama_btn)
    fix_row.addWidget(get_model_btn)
    fix_row.addWidget(retry_brain_btn)
    fix_row.addStretch(1)
    layout.addLayout(fix_row)
    log = QtWidgets.QTextEdit()
    log.setObjectName("chat_log")
    log.setReadOnly(True)
    # G-UX9: the log is capped — it used to grow for the session.
    log.document().setMaximumBlockCount(_CHAT_LOG_BLOCKS)
    layout.addWidget(log)
    # G-UX9: Clear and Export for the chat log.
    log_row = QtWidgets.QHBoxLayout()
    clear_chat_btn = QtWidgets.QPushButton("Clear chat")
    clear_chat_btn.setObjectName("clear_chat_button")
    export_chat_btn = QtWidgets.QPushButton("Export chat…")
    export_chat_btn.setObjectName("export_chat_button")
    log_row.addWidget(clear_chat_btn)
    log_row.addWidget(export_chat_btn)
    log_row.addStretch(1)
    layout.addLayout(log_row)
    # The working line: what this turn is doing, and how long it
    # has taken. It ticks every second while a turn is busy, and
    # says how long the turn took when it ends.
    # V6: the label starts on "Ready" — never blank — and keeps
    # the last outcome (the done line) until the next turn.
    status_label = QtWidgets.QLabel("Ready")
    status_label.setObjectName("turn_status")
    status_label.setWordWrap(True)
    layout.addWidget(status_label)
    progress_row = QtWidgets.QHBoxLayout()
    progress_label = QtWidgets.QLabel("")
    progress_label.setObjectName("research_progress")
    progress_label.setWordWrap(True)
    progress_row.addWidget(progress_label, 1)
    # Q1: a running research job can be stopped from the dock — the
    # job outlives the turn that started it, so the turn's Stop
    # does not reach it.
    stop_research_btn = QtWidgets.QPushButton("Stop research")
    stop_research_btn.setObjectName("stop_research_button")
    stop_research_btn.setEnabled(False)
    progress_row.addWidget(stop_research_btn)
    layout.addLayout(progress_row)

    class _ChatEntry(QtWidgets.QPlainTextEdit):
        """The chat entry (G-UX9): multi-line, Enter sends,
        Shift+Enter adds a line. text()/setText() match the old
        QLineEdit's call sites."""
        send_requested = QtCore.Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setPlaceholderText(
                "Ask for a page, a search, a summary… (try @tab2)")
            self.setFixedHeight(64)

        def keyPressEvent(self, event):
            if event.key() in (QtCore.Qt.Key.Key_Return,
                               QtCore.Qt.Key.Key_Enter) \
                    and not (event.modifiers()
                             & QtCore.Qt.KeyboardModifier.ShiftModifier):
                self.send_requested.emit()
                return
            super().keyPressEvent(event)

        def text(self):
            return self.toPlainText()

        def setText(self, text):
            self.setPlainText(text)

    entry = _ChatEntry()
    entry.setObjectName("chat_entry")
    layout.addWidget(entry)
    # G-UX4 (extension): the buttons sit 2x2, not 1x4 — four
    # buttons in a row pinned the dock's minimum width past 400.
    button_grid = QtWidgets.QGridLayout()
    send = QtWidgets.QPushButton("Send")
    summarize_btn = QtWidgets.QPushButton("Summarize")
    mic_btn = QtWidgets.QPushButton("🎤 Hold to talk")
    mic_btn.setObjectName("voice_button")
    stop_btn = QtWidgets.QPushButton("Stop")
    stop_btn.setObjectName("stop_button")
    stop_btn.setEnabled(False)
    button_grid.addWidget(send, 0, 0)
    button_grid.addWidget(summarize_btn, 0, 1)
    button_grid.addWidget(mic_btn, 1, 0)
    button_grid.addWidget(stop_btn, 1, 1)
    layout.addLayout(button_grid)
    # K11: summary length — a short summary is 2-3 sentences.
    short_row = QtWidgets.QHBoxLayout()
    short_box = QtWidgets.QCheckBox("Short summary")
    short_box.setObjectName("short_summary_box")
    short_row.addWidget(short_box)
    short_row.addStretch(1)
    layout.addLayout(short_row)
    # O11: saved workflow cards, discoverable in the window — pick
    # one and run it against the current page. The entry text, if
    # any, is the card's question.
    cards_row = QtWidgets.QHBoxLayout()
    cards_row.addWidget(QtWidgets.QLabel("Card:"))
    cards_combo = QtWidgets.QComboBox()
    cards_combo.setObjectName("cards_combo")
    # R3-01a follow-up: the combo must not demand the width of
    # its longest card name — that forced the whole panel (and
    # the dock minimum) wide. It takes the spare width, and the
    # dropdown shows full names.
    cards_combo.setSizeAdjustPolicy(
        QtWidgets.QComboBox.SizeAdjustPolicy
        .AdjustToMinimumContentsLengthWithIcon)
    cards_combo.setMinimumContentsLength(12)
    cards_row.addWidget(cards_combo, 1)
    run_card_btn = QtWidgets.QPushButton("Run card")
    run_card_btn.setObjectName("run_card_button")
    cards_row.addWidget(run_card_btn)
    layout.addLayout(cards_row)
    # R11: the memory view — what the browser remembers, in the
    # window at last (facts + stored pages, with forget/remove).
    # Viewing the person's own data is not an AI feature, so this
    # button is live even with AI off.
    memory_row = QtWidgets.QHBoxLayout()
    memory_btn = QtWidgets.QPushButton("Memory…")
    memory_btn.setObjectName("memory_button")
    memory_btn.setToolTip("See what the browser remembers: facts and "
                          "stored pages, and forget things.")
    memory_row.addWidget(memory_btn)
    memory_row.addStretch(1)
    layout.addLayout(memory_row)

    def show_memory_view():
        from .memory import (_MEMORY_LIMIT, _PAGE_MEMORY_LIMIT,
                             allow_site_memory, block_site_memory,
                             clear_site_memory, forget, forget_page,
                             list_entries, list_pages, list_site_memory)
        dialog = QtWidgets.QDialog(window)
        dialog.setWindowTitle("Memory")
        dialog.setObjectName("memory_dialog")
        vbox = QtWidgets.QVBoxLayout(dialog)
        facts_list = QtWidgets.QListWidget()
        facts_list.setObjectName("memory_facts")
        pages_list = QtWidgets.QListWidget()
        pages_list.setObjectName("memory_pages")
        sites_list = QtWidgets.QListWidget()
        sites_list.setObjectName("memory_sites")

        def refresh():
            facts_list.clear()
            for entry in list_entries():
                item = QtWidgets.QListWidgetItem(entry["text"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole, entry["id"])
                facts_list.addItem(item)
            pages_list.clear()
            for page in list_pages():
                item = QtWidgets.QListWidgetItem(
                    f"{page['title']}: {page['url']}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, page["url"])
                pages_list.addItem(item)
            # R3-12: each empty list says what it is for and how
            # to fill it, instead of showing a blank box.
            facts_empty.setVisible(facts_list.count() == 0)
            pages_empty.setVisible(pages_list.count() == 0)
            sites_list.clear()
            sites = list_site_memory()
            for host in sites.get("blocked", []):
                item = QtWidgets.QListWidgetItem(
                    f"Never remembered: {host}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, host)
                sites_list.addItem(item)
            for host in sites.get("allowed", []):
                item = QtWidgets.QListWidgetItem(
                    f"Always remembered: {host}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, host)
                sites_list.addItem(item)
            sites_empty.setVisible(sites_list.count() == 0)

        def forget_fact():
            item = facts_list.currentItem()
            if item is not None:
                # A2: a destructive forget confirms first, default
                # No (U23).
                if window._ask_yes_no(
                        "Forget this fact?",
                        "Forget this fact?\n\n"
                        f"{item.text()}\n\nIt cannot be undone."):
                    try:
                        forget(item.data(QtCore.Qt.ItemDataRole.UserRole))
                    except (BrowserError, OSError) as exc:
                        # B15: the stores raise when the disk write
                        # fails — the person gets a plain message,
                        # not a traceback on stderr from a Qt slot.
                        _log_error(f"Could not forget a fact: {exc}")
                        window._show_message(
                            "warning", "Could not forget that",
                            "That fact is still remembered. The save "
                            "did not work, so nothing was changed. "
                            "Try again.")
                        return
                    _log_action("Forgot a fact")
                    refresh()

        def remove_page():
            item = pages_list.currentItem()
            if item is not None:
                # A2: a destructive remove confirms first, default
                # No (U23).
                if window._ask_yes_no(
                        "Remove this page?",
                        "Remove this stored page?\n\n"
                        f"{item.text()}\n\nIt cannot be undone."):
                    try:
                        forget_page(
                            item.data(QtCore.Qt.ItemDataRole.UserRole))
                    except (BrowserError, OSError) as exc:
                        # B15: the stores raise when the disk write
                        # fails — the person gets a plain message,
                        # not a traceback on stderr from a Qt slot.
                        _log_error(f"Could not remove a page: {exc}")
                        window._show_message(
                            "warning", "Could not remove that page",
                            "That page is still stored. The save "
                            "did not work, so nothing was changed. "
                            "Try again.")
                        return
                    _log_action("Removed a stored page")
                    refresh()

        def block_site():
            host, ok = QtWidgets.QInputDialog.getText(
                dialog, "Never remember a site",
                "Site to never remember (example.org):")
            if ok and host.strip():
                try:
                    blocked = block_site_memory(host.strip())
                except (BrowserError, OSError) as exc:
                    # B15: the stores raise when the disk write
                    # fails — the person gets a plain message,
                    # not a traceback on stderr from a Qt slot.
                    _log_error(f"Could not block memory: {exc}")
                    window._show_message(
                        "warning", "Could not block that site",
                        "That site is not blocked yet. The save "
                        "did not work, so nothing was changed. "
                        "Try again.")
                    return
                if blocked:
                    _log_action(f"Blocked memory for {blocked}")
                refresh()

        def unblock_site():
            item = sites_list.currentItem()
            if item is not None:
                try:
                    clear_site_memory(
                        item.data(QtCore.Qt.ItemDataRole.UserRole))
                except (BrowserError, OSError) as exc:
                    # B15: same guard as block_site.
                    _log_error(f"Could not remove a site rule: {exc}")
                    window._show_message(
                        "warning", "Could not remove that rule",
                        "That site rule is still there. The save "
                        "did not work, so nothing was changed. "
                        "Try again.")
                    return
                _log_action("Removed a site memory rule")
                refresh()

        vbox.addWidget(QtWidgets.QLabel("Remembered facts:"))
        vbox.addWidget(facts_list)
        facts_empty = QtWidgets.QLabel(
            "No facts remembered yet. Ask the chat to remember "
            "something.")
        facts_empty.setObjectName("memory_facts_empty")
        facts_empty.setWordWrap(True)
        vbox.addWidget(facts_empty)
        forget_btn = QtWidgets.QPushButton("Forget selected fact")
        forget_btn.setObjectName("memory_forget_button")
        forget_btn.clicked.connect(forget_fact)
        vbox.addWidget(forget_btn)
        vbox.addWidget(QtWidgets.QLabel("Stored pages:"))
        vbox.addWidget(pages_list)
        pages_empty = QtWidgets.QLabel(
            "No pages stored yet. Pages you visit are kept here "
            "so the chat can recall them.")
        pages_empty.setObjectName("memory_pages_empty")
        pages_empty.setWordWrap(True)
        vbox.addWidget(pages_empty)
        remove_btn = QtWidgets.QPushButton("Remove selected page")
        remove_btn.setObjectName("memory_remove_button")
        remove_btn.clicked.connect(remove_page)
        vbox.addWidget(remove_btn)
        # B15: the caps are disclosed where the data is shown.
        caps = QtWidgets.QLabel(
            f"Keeps the newest {_MEMORY_LIMIT} facts and "
            f"{_PAGE_MEMORY_LIMIT} pages.")
        caps.setWordWrap(True)
        vbox.addWidget(caps)
        # G-UX18: the per-site memory controls, in the window.
        vbox.addWidget(QtWidgets.QLabel(
            "Sites with their own memory rule:"))
        vbox.addWidget(sites_list)
        sites_empty = QtWidgets.QLabel(
            "No site rules yet. A rule makes one site always or "
            "never remembered.")
        sites_empty.setObjectName("memory_sites_empty")
        sites_empty.setWordWrap(True)
        vbox.addWidget(sites_empty)
        site_row = QtWidgets.QHBoxLayout()
        block_btn = QtWidgets.QPushButton("Never remember a site…")
        block_btn.setObjectName("memory_block_button")
        block_btn.clicked.connect(block_site)
        unblock_btn = QtWidgets.QPushButton("Remove selected rule")
        unblock_btn.setObjectName("memory_unblock_button")
        unblock_btn.clicked.connect(unblock_site)
        site_row.addWidget(block_btn)
        site_row.addWidget(unblock_btn)
        site_row.addStretch(1)
        vbox.addLayout(site_row)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        vbox.addWidget(close_btn)
        # R3-11: Forget, Remove, and Remove-rule need a selection
        # in their own list — they sit disabled with nothing
        # picked instead of silently doing nothing when clicked.
        def update_buttons():
            forget_btn.setEnabled(
                facts_list.currentItem() is not None)
            remove_btn.setEnabled(
                pages_list.currentItem() is not None)
            unblock_btn.setEnabled(
                sites_list.currentItem() is not None)

        facts_list.currentItemChanged.connect(
            lambda *a: update_buttons())
        pages_list.currentItemChanged.connect(
            lambda *a: update_buttons())
        sites_list.currentItemChanged.connect(
            lambda *a: update_buttons())
        update_buttons()
        refresh()
        dialog.resize(560, 560)
        # R3-11: Close takes the initial focus, not a list — a
        # list that takes focus on open auto-selects its first
        # row, which would enable its button with no pick.
        close_btn.setFocus()
        dialog.exec()

    memory_btn.clicked.connect(show_memory_view)
    # R3-01a: the panel scrolls — the dock used to demand the
    # panel's full height (793 px minimum), so the window could
    # not fit a 1366x768 screen at all. With a scroll area the
    # dock takes what height the window has and the rest scrolls.
    # Horizontal scrolling stays off: the panel is laid out for
    # the dock's width, and a sideways scroll would hide buttons.
    scroll = QtWidgets.QScrollArea()
    scroll.setObjectName("chat_scroll")
    scroll.setWidget(panel)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    # R3-01a follow-up: the panel needs its usable width for
    # the 2x2 button rows — the vertical scrollbar takes ~15 px
    # of the dock, so the scroll area's own minimum covers the
    # panel's own minimum plus the scrollbar, and the buttons
    # never clip at the narrowest dock. Measured once the
    # window has shown (see below) and again when the seats
    # fill, so the number is the laid-out one.
    def _update_dock_minimum():
        scroll.setMinimumWidth(
            max(_CHAT_DOCK_MIN_WIDTH,
                panel.minimumSizeHint().width())
            + scroll.verticalScrollBar().sizeHint().width())
        dock.updateGeometry()
    # R3-01a follow-up: the scroll area's viewport paints the
    # default (light) palette colour behind the transparent
    # panel — as the dock's direct widget the panel sat on dark,
    # but inside a scroll area the light viewport shows through
    # and the cream labels go unreadable. Paint both dark. The
    # viewport's palette does not stick (Qt's repolish resets
    # it), so the viewport carries a stylesheet instead — a
    # stylesheet survives repolish.
    palette = panel.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window,
                     QtGui.QColor("#14161a"))
    panel.setPalette(palette)
    scroll.viewport().setStyleSheet("background-color: #14161a;")
    panel.setAutoFillBackground(True)
    dock.setWidget(scroll)
    # G-UX4 (extension): the dock is freely resizable by dragging
    # its border — the old 320 minimum is now 240, and nothing in
    # the panel pins the width (buttons and toggles sit 2x2).
    dock.setMinimumWidth(_CHAT_DOCK_MIN_WIDTH)
    window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
    window._chat_dock = dock
    # R3-23: the dock can be dragged wider than half the window,
    # leaving the page a sliver. Clamp it at half the window
    # width (never below the 240 px minimum) — the page always
    # keeps half the window. The guard stops the clamp's own
    # resize from re-entering.
    dock_clamp = {"clamping": False}

    class _DockWidthFilter(QtCore.QObject):
        def eventFilter(self, obj, event):
            if (event.type() == QtCore.QEvent.Type.Resize
                    and not dock_clamp["clamping"]):
                limit = max(_CHAT_DOCK_MIN_WIDTH, window.width() // 2)
                if dock.width() > limit:
                    dock_clamp["clamping"] = True
                    try:
                        window.resizeDocks(
                            [dock], [limit],
                            QtCore.Qt.Orientation.Horizontal)
                    finally:
                        dock_clamp["clamping"] = False
            return super().eventFilter(obj, event)

    dock.installEventFilter(_DockWidthFilter(dock))
    # G-UX4: View → Chat Dock re-shows a closed dock — and tracks
    # it, so the check follows the X button too.
    if window._view_menu is not None:
        chat_action = QtGui.QAction("Chat Dock", window)
        chat_action.setObjectName("view_chat_dock")
        chat_action.setCheckable(True)
        chat_action.setChecked(True)
        chat_action.triggered.connect(
            lambda checked: dock.setVisible(checked))
        dock.visibilityChanged.connect(chat_action.setChecked)
        window._view_menu.addAction(chat_action)
    # G-UX4 (extension): the dock comes back at the size it was
    # left at — restored once the window has shown and laid out.
    QtCore.QTimer.singleShot(0, window._restore_chat_dock)
    # R3-01a: the minimum is measured once the window has shown
    # and laid out — before show the panel's hint overestimates
    # by ~18 px and the dock would sit too wide.
    QtCore.QTimer.singleShot(0, _update_dock_minimum)

    state = {"busy": False, "live": False}
    # V2: "waited" is the plan-gate time accumulated this turn
    # (never counted as work); "wait_started" is when the current
    # wait began, or None when no dialog is up.
    turn = {"step": "thinking", "started": None, "retrieve_note": "",
            "waited": 0.0, "wait_started": None}
    # Voice state lives here (not with the mic handlers below) so
    # apply_ai_state can read it — it runs before the mic section.
    voice_state = {"recorder": None, "transcribing": False,
                   "capped": False, "last_size": 0}

    def waited_so_far(now=None):
        """Seconds spent at the plan gate so far this turn,
        including the wait in progress (V2)."""
        total = turn.get("waited", 0.0)
        if turn.get("wait_started") is not None:
            now = time.monotonic() if now is None else now
            total += now - turn["wait_started"]
        return total

    def status_text(step, started):
        """The working line, with a ● writing marker while the
        answer is streaming (H3). The clock pauses during a
        plan-gate wait (V2)."""
        text = turn_status_text(step, started, waited=waited_so_far())
        if state["live"]:
            text += " ●"
        return text

    def on_step(kind):
        # The worker names the step; the label shows it at once,
        # with the seconds so far.
        turn["step"] = kind
        if state["busy"] and turn["started"] is not None:
            status_label.setText(status_text(kind, turn["started"]))

    signals.step.connect(on_step)

    def tick_turn_clock():
        # Once a second, while a turn is busy, refresh the elapsed
        # time on the working line.
        if state["busy"] and turn["started"] is not None:
            status_label.setText(
                status_text(turn["step"], turn["started"]))
        # Q1: Stop research is live only while a job runs.
        from .research import research_job_running
        stop_research_btn.setEnabled(research_job_running() is not None)
        # B6: the live readout ticks with the same timer.
        window._update_live_stats()

    def on_stop_research():
        from .research import cancel_research_job
        if cancel_research_job():
            progress_label.setText("Stopping research…")
            stop_research_btn.setEnabled(False)

    stop_research_btn.clicked.connect(on_stop_research)

    turn_timer = QtCore.QTimer(window)
    turn_timer.setInterval(1000)
    turn_timer.timeout.connect(tick_turn_clock)
    turn_timer.start()

    # --- Model picker + AI controls (task #14) -----------------
    # Three seat dropdowns fed by `ollama list`, plus Firefox-style
    # toggles: one master AI kill-switch and one per feature. Picks
    # live in ~/.forge/seats.json and apply at once.
    # B13: the seats and toggles sit under one collapsible
    # "Models and AI" header, so the dock is not one long column.
    from .chat import check_ollama, pick_chat_model_with_reason
    from .seats import load_seats, set_feature, set_seat

    models_toggle = QtWidgets.QToolButton()
    models_toggle.setObjectName("models_toggle")
    models_toggle.setText("Models and AI")
    models_toggle.setCheckable(True)
    models_toggle.setChecked(True)
    models_toggle.setToolButtonStyle(
        QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    models_toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow)
    layout.addWidget(models_toggle)
    models_box = QtWidgets.QWidget()
    models_box.setObjectName("models_box")
    models_layout = QtWidgets.QVBoxLayout(models_box)
    models_layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(models_box)

    def on_models_toggled(on):
        models_box.setVisible(on)
        models_toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if on
            else QtCore.Qt.ArrowType.RightArrow)

    models_toggle.toggled.connect(on_models_toggled)

    # B8: each seat says what it is for, under its combo.
    _SEAT_DESCRIPTIONS = {
        "brain": "Brain: answers questions and drives the browser.",
        "fast": "Fast: quick jobs, short titles and fast summaries.",
        "vision": "Vision: reads pictures and describes images.",
    }
    seat_grid = QtWidgets.QGridLayout()
    combos = {}
    desc_labels = {}
    for row, (label, key) in enumerate(
            (("Brain", "brain"), ("Fast", "fast"), ("Vision", "vision"))):
        seat_grid.addWidget(QtWidgets.QLabel(label), row * 2, 0)
        combo = QtWidgets.QComboBox()
        combo.setObjectName(f"{key}_combo")
        # R3-15 follow-up: the combo must not demand the width
        # of its longest model name — a 33-char roster entry
        # would force the whole panel (and the dock minimum)
        # wide. It takes the spare width, the tooltip names the
        # auto pick, and the dropdown shows full names.
        combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy
            .AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        combos[key] = combo
        seat_grid.addWidget(combo, row * 2, 1)
        desc = QtWidgets.QLabel(_SEAT_DESCRIPTIONS[key])
        desc.setObjectName(f"{key}_desc")
        desc.setWordWrap(True)
        # R3-01b: never shrink a description below its size hint —
        # the old layout clipped them to ~1.5 lines when the dock
        # was short. With the scroll area (R3-01a) the panel gets
        # its full height, and this keeps the text whole even so.
        desc.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Minimum)
        desc_labels[key] = desc
        seat_grid.addWidget(desc, row * 2 + 1, 0, 1, 2)
    refresh_btn = QtWidgets.QPushButton("↻")
    refresh_btn.setObjectName("refresh_seats")
    refresh_btn.setToolTip("Re-read the models Ollama has installed")
    seat_grid.addWidget(refresh_btn, 0, 2)
    # R3-15: the combo column takes the spare width, so a long
    # model name has room instead of truncating at once.
    seat_grid.setColumnStretch(1, 1)
    models_layout.addLayout(seat_grid)

    # G-UX4 (extension): the toggles sit 2x2, not 1x4 — four
    # checkboxes in a row pinned the dock's minimum width.
    toggle_grid = QtWidgets.QGridLayout()
    ai_toggle = QtWidgets.QCheckBox("AI")
    ai_toggle.setObjectName("ai_toggle")
    # T11: each toggle says what it covers — no guessing.
    ai_toggle.setToolTip(
        "Master switch: turns off every AI feature: chat, the "
        "morning brief, image describing, tidy titles, and memory.")
    chat_toggle = QtWidgets.QCheckBox("Chat")
    chat_toggle.setObjectName("chat_toggle")
    chat_toggle.setToolTip(
        "The chat brain, and every brain job: summarize, the "
        "morning brief, compare, and research.")
    tidy_toggle = QtWidgets.QCheckBox("Tidy titles")
    tidy_toggle.setObjectName("tidy_toggle")
    tidy_toggle.setToolTip("Brain-written tab titles.")
    memory_toggle = QtWidgets.QCheckBox("Memory")
    memory_toggle.setObjectName("memory_toggle")
    memory_toggle.setToolTip(
        "Remembered facts offered as context, and pages recorded "
        "for later recall.")
    toggle_grid.addWidget(ai_toggle, 0, 0)
    toggle_grid.addWidget(chat_toggle, 0, 1)
    toggle_grid.addWidget(tidy_toggle, 1, 0)
    toggle_grid.addWidget(memory_toggle, 1, 1)
    models_layout.addLayout(toggle_grid)

    def apply_ai_state():
        """Header + which controls are live, from the seats file
        and whether a brain is answering. Never re-enables the
        send controls mid-turn. The entry stays editable mid-turn
        (G-UX9) — only Send waits for the turn to finish."""
        seats = load_seats()
        ai_on = seats["ai_enabled"]
        chat_on = seats["features"]["chat"]
        if not ai_on:
            header.setText("AI is off: turn it on below to use chat, "
                           "tidy titles, and memory.")
        elif not chat_on:
            header.setText("Chat is off: turn it on below to talk "
                           "to the browser.")
        else:
            header.setText(brain_header_text())
        chat_ok = (ai_on and chat_on and session is not None
                   and bool(session.model))
        busy = state["busy"]
        entry.setEnabled(ai_on and chat_on)
        send.setEnabled(chat_ok and not busy)
        summarize_btn.setEnabled(chat_ok and not busy)
        mic_btn.setEnabled(chat_ok and not busy
                           and not voice_state["transcribing"])
        run_card_btn.setEnabled(chat_ok and not busy
                                and cards_combo.count() > 1)
        # G-UX5: the fix buttons show only for the state they fix.
        brain_state = (session.brain_state if session is not None
                       else None)
        start_ollama_btn.setVisible(
            ai_on and chat_on and brain_state == "ollama-down")
        start_ollama_btn.setEnabled(True)
        get_model_btn.setVisible(
            ai_on and chat_on and brain_state == "no-models")
        get_model_btn.setEnabled(True)
        # R3-05: Retry brain shows when a picked brain was checked
        # and did not answer — re-picking and re-checking is the
        # fix for a brain that was still loading or briefly stuck.
        retry_brain_btn.setVisible(
            ai_on and chat_on and brain_state == "ok"
            and brain_health["checked"]
            and not brain_health["answers"])
        retry_brain_btn.setEnabled(True)
        ai_toggle.setChecked(ai_on)
        chat_toggle.setChecked(chat_on)
        tidy_toggle.setChecked(seats["features"]["tidy_titles"])
        memory_toggle.setChecked(seats["features"]["memory"])
        for box in (chat_toggle, tidy_toggle, memory_toggle):
            box.setEnabled(ai_on)

    seat_sizes = {}  # name -> bytes, from the last seat fill (B7)
    # R3-15: what (auto) would pick for the Brain seat, from the
    # last seat fill — named in the description line and the combo
    # tooltip, not inside the combo label (which truncated).
    auto_pick_state = {"brain": None}

    def fill_seat_combos(details=None):
        # Each item carries the model name as its data; the label
        # shows the size (T8) so a 2 GB model reads differently
        # from an 8 GB one. Embed models are not offered as Brain
        # or Fast (T6); a saved pick that is gone shows as dead
        # (T5) instead of hiding behind "(auto)", and a saved
        # embed pick shows as an embed model. The Brain combo's
        # (auto) entry stays short (R3-15) — what auto would pick
        # is named in the description line, not the label.
        # G-P1: details arrive from the background pick — only
        # the ↻ path calls check_ollama() itself, via
        # _start_brain_pick(refresh=True), never directly.
        if details is None:
            _reachable, details = check_ollama()
        models = [d["name"] for d in details]
        sizes = {d["name"]: d["size"] for d in details}
        seat_sizes.clear()
        seat_sizes.update(sizes)
        retrieval_state["models"] = models  # the header's Retrieval line (I1)
        seats = load_seats()
        auto_pick = None
        if models:
            auto_pick, _reason, _note = pick_chat_model_with_reason(
                models=models, sizes=sizes, ignore_saved=True)
        auto_pick_state["brain"] = auto_pick
        for key, combo in combos.items():
            combo.blockSignals(True)
            combo.clear()
            # R3-15: the (auto) label is always just "(auto)" —
            # the old "(auto: qwen3.5:4b)" truncated in the narrow
            # combo. The pick is named in the description line
            # (_refresh_seat_descriptions) and the tooltip instead.
            combo.addItem("(auto)", None)
            for name in models:
                if key in ("brain", "fast") and "embed" in name.lower():
                    continue
                display = name
                if sizes.get(name):
                    display = f"{name} ({_human_size(sizes[name])})"
                combo.addItem(display, name)
                # B7: every model carries its one-line blurb as a
                # tooltip — what it does, its size, its fit.
                combo.setItemData(
                    combo.count() - 1,
                    model_blurb(name, sizes.get(name)),
                    QtCore.Qt.ItemDataRole.ToolTipRole)
            pick = seats.get(key)
            if pick and pick not in models:
                combo.addItem(f"{pick} (not installed)", pick)
                combo.setCurrentIndex(combo.count() - 1)
            elif (pick and key in ("brain", "fast")
                  and "embed" in pick.lower()):
                combo.addItem(f"{pick} (embed model)", pick)
                combo.setCurrentIndex(combo.count() - 1)
            elif pick:
                index = combo.findData(pick)
                combo.setCurrentIndex(index if index >= 0 else 0)
            else:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)
        combos["brain"].setToolTip(
            f"Auto picks {auto_pick}." if auto_pick else "")
        _refresh_seat_descriptions()
        # R3-01a: the filled combos can widen the panel's
        # minimum — re-cover it so the buttons never clip.
        _update_dock_minimum()

    def _refresh_seat_descriptions():
        """B7+B8: under each seat combo, what the seat is for plus
        the picked model's one-line blurb. With (auto) picked on
        the Brain seat, the description names what auto would
        pick (R3-15) — the label itself stays short."""
        for key, combo in combos.items():
            picked = combo.currentData()
            text = _SEAT_DESCRIPTIONS[key]
            if picked:
                text += " " + model_blurb(picked,
                                          seat_sizes.get(picked))
            elif key == "brain" and auto_pick_state["brain"]:
                text += f" Auto picks {auto_pick_state['brain']}."
            desc_labels[key].setText(text)

    def on_brain_ready(picked, details):
        """G-P1: the background pick landed — wire the session in,
        fill the seats, prove the model answers."""
        nonlocal session
        session = picked
        window._chat_session = picked  # tab titles tidy against this brain
        brain_health["checking"] = False
        brain_health["checked"] = False
        brain_health["answers"] = None
        fill_seat_combos(details)
        apply_ai_state()
        _check_brain_in_background()

    signals.brain_ready.connect(on_brain_ready)

    def on_seat_changed(key):
        def handler():
            picked = combos[key].currentData()
            set_seat(key, picked)
            _refresh_seat_descriptions()
            if key == "brain":
                if session is None:
                    return
                session.repick()
                brain_health["checking"] = False
                brain_health["checked"] = False
                brain_health["answers"] = None
                apply_ai_state()
                _check_brain_in_background()
            elif key == "fast":
                window._fast_session = None
        return handler

    def fill_cards_combo():
        # O11: the runnable cards, by name — broken cards are left
        # out (the CLI names them); a new card file appears after ↻.
        # R3-09: the first item is empty — no card is picked by
        # default, so Run card with nothing chosen says so instead
        # of silently running the first card.
        from .cards import list_cards
        current = cards_combo.currentText()
        cards_combo.blockSignals(True)
        cards_combo.clear()
        cards_combo.addItem("", None)
        cards_combo.addItems([c.name for c in list_cards() if not c.error])
        if current:
            cards_combo.setCurrentText(current)
        cards_combo.blockSignals(False)

    def on_refresh():
        # ↻ re-reads the models AND re-picks the brain, so a brain
        # that appeared since launch (Ollama just started) is picked
        # up — the header's own "then ↻" instruction works. Then the
        # answer-check proves the picked model answers (G7).
        # G-P1: the re-read runs off the GUI thread, like the
        # launch pick — a down Ollama used to freeze the window.
        fill_cards_combo()
        _start_brain_pick(refresh=True)

    def on_brain_checked(answers, model):
        # Only the current model's result counts — a re-pick since
        # the check started makes it stale.
        if session is None or model != session.model:
            return
        brain_health["checking"] = False
        brain_health["checked"] = True
        brain_health["answers"] = answers
        apply_ai_state()

    signals.brain_checked.connect(on_brain_checked)

    def on_ai_toggled(on):
        # T7: the kill-switch kills the CURRENT turn too, not just
        # the next one — flipping AI off mid-turn stops the brain.
        set_feature("ai", on)
        if not on and session is not None:
            session.cancel()
        apply_ai_state()

    for key in combos:
        combos[key].currentTextChanged.connect(on_seat_changed(key))
    refresh_btn.clicked.connect(on_refresh)
    ai_toggle.toggled.connect(on_ai_toggled)
    chat_toggle.toggled.connect(lambda on: (set_feature("chat", on), apply_ai_state()))
    tidy_toggle.toggled.connect(
        lambda on: (set_feature("tidy_titles", on), apply_ai_state()))
    memory_toggle.toggled.connect(
        lambda on: (set_feature("memory", on), apply_ai_state()))
    fill_cards_combo()
    apply_ai_state()
    # G-P1: the brain pick starts in the background — the window
    # is already painted by the time it lands.
    _start_brain_pick()

    # --- G-UX5: fix the no-brain states from the dock ------------
    def on_start_ollama():
        """Start Ollama in the background, then re-pick the brain."""
        import shutil
        import subprocess
        if shutil.which("ollama") is None:
            append("Forge Browser",
                   "Ollama is not installed. Use the Forge Browser "
                   "installer to add it.")
            return
        start_ollama_btn.setEnabled(False)
        status_label.setText("Starting Ollama…")

        def work_start():
            try:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except Exception as exc:
                signals.notice.emit(f"Could not start Ollama: {exc}")
                return
            time.sleep(3)
            signals.brain_fix_done.emit()

        threading.Thread(target=work_start, daemon=True).start()

    def on_get_model():
        """Pull a small chat model in the background, then re-pick."""
        import subprocess
        get_model_btn.setEnabled(False)
        status_label.setText(
            "Getting a chat model… this downloads about 1.4 GB.")

        def work_pull():
            try:
                proc = subprocess.run(
                    ["ollama", "pull", _SMALL_CHAT_MODEL],
                    capture_output=True, text=True, timeout=1800)
            except Exception as exc:
                signals.notice.emit(
                    f"Could not get a chat model: {exc}")
                return
            if proc.returncode == 0:
                signals.brain_fix_done.emit()
            else:
                signals.notice.emit(
                    "Could not get a chat model. Check the connection "
                    "and try again.")

        threading.Thread(target=work_pull, daemon=True).start()

    start_ollama_btn.clicked.connect(on_start_ollama)
    get_model_btn.clicked.connect(on_get_model)
    # R3-05: Retry brain re-picks and re-checks — the same path as
    # the ↻ button, which is what fixes a brain that was still
    # loading or briefly stuck when it was checked.
    retry_brain_btn.clicked.connect(on_refresh)
    signals.brain_fix_done.connect(on_refresh)

    # --- G-UX9: the chat log persists across restarts -------------
    def _save_chat_log():
        save_chat_history(log.toPlainText())

    def _load_chat_log():
        saved = load_chat_history()
        if saved:
            log.setPlainText(saved)

    def on_clear_chat():
        log.clear()
        try:
            os.unlink(chat_history_path())
        except OSError:
            pass
        _log_action("Cleared the chat log")

    def on_export_chat():
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            window, "Export chat", "forge-browser-chat.txt",
            "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(log.toPlainText())
        except OSError as exc:
            window._show_message(
                "warning", "Export chat",
                f"Could not write the export: {exc}")
            return
        _log_action("Exported the chat log")
        window.statusBar().showMessage(
            f"Chat exported to {path}", 5000)

    clear_chat_btn.clicked.connect(on_clear_chat)
    export_chat_btn.clicked.connect(on_export_chat)
    _load_chat_log()

    # F4: after an update, one line in the log saying what changed.
    from .whatsnew import check_whats_new
    _whats_new = check_whats_new()
    if _whats_new:
        log.append(f"<i>{html.escape(_whats_new)}</i>")

    def append(who, text):
        # H6: model text is escaped — a reply containing "<b>" or
        # "<div>" must print as text, never render as HTML.
        log.append(f"<b>{html.escape(who)}:</b> {html.escape(text)}")

    def finish(outcome="done"):
        state["busy"] = False
        stop_btn.setEnabled(False)
        if turn["started"] is not None:
            done = turn_done_text(
                outcome, turn["started"], speed=session.last_speed,
                waited=turn.get("waited", 0.0))
            if turn.get("retrieve_note"):
                done += f" {turn['retrieve_note']}"
            status_label.setText(done)
            turn["started"] = None
        # B6: the turn count feeds the live readout. G-UX9: the
        # chat log persists at the end of every turn.
        window._turn_count = getattr(window, "_turn_count", 0) + 1
        _save_chat_log()
        window._update_live_stats()
        apply_ai_state()
        entry.setFocus()

    def on_token(text):
        # Streamed words appear as they are generated. The first
        # token of a turn opens the "Forge Browser:" paragraph;
        # later tokens keep flowing onto the same line. Insertion
        # goes through a separate end cursor on the document —
        # never log.moveCursor(End), and the first paragraph opens
        # with insertBlock + insertHtml, not log.append (which
        # scrolls) — so a person scrolled up to read an earlier
        # answer is not yanked down on every token (H2). The
        # viewport follows to the end only when it was already
        # at the bottom.
        if not text:
            return
        bar = log.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        if not state["live"]:
            state["live"] = True
            cursor = QtGui.QTextCursor(log.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            if not log.document().isEmpty():
                cursor.insertBlock()
            cursor.insertHtml("<b>Forge Browser:</b> ")
        cursor = QtGui.QTextCursor(log.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if at_bottom:
            bar.setValue(bar.maximum())

    def on_answer(text):
        outcome = "stopped" if (text or "").strip() == "Stopped." else "done"
        if state["live"]:
            # The answer already streamed into the log line by line;
            # re-printing it would duplicate it.
            state["live"] = False
            finish(outcome)
        else:
            append("Forge Browser", text)
            finish(outcome)

    def on_failed(text):
        if state["live"]:
            # The partial answer was cut off mid-stream: mark it
            # so it never reads as finished (H4).
            cursor = QtGui.QTextCursor(log.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertText(" …(cut off)")
        state["live"] = False
        _log_error(f"Chat turn failed: {text}")
        append("Forge Browser", f"⚠ {text}")
        finish("failed")

    def on_notice(text):
        # H5: notice text is escaped — the action kind comes from
        # model-supplied JSON. A progress line landing mid-stream
        # opens a fresh block, so the resumed answer starts on its
        # own line instead of running onto the notice's.
        log.append(f"<i>{html.escape(text)}</i>")
        if state["live"]:
            cursor = QtGui.QTextCursor(log.document())
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertBlock()

    signals.notice.connect(on_notice)
    signals.open_url.connect(lambda url: window.new_tab(url, agent=True))
    # M11: the comparison tab is brain-made, so it wears the 🤖 mark.
    signals.compare_table.connect(
        lambda html: window.new_tab(html=html, agent=True))
    signals.progress.connect(progress_label.setText)

    def on_compare_refresh(view, html):
        # M11: the refreshed table goes back into the same tab.
        # G-S4: its links get rel="noopener", as in new_tab.
        from .webui import _with_noopener
        window._refreshing_views.discard(id(view))
        if view is not None and window.tabs.indexOf(view) >= 0:
            view.setHtml(_with_noopener(html))

    window.compare_refresh_done.connect(on_compare_refresh)

    def on_research_done(report, sources, error):
        if error:
            append("Forge Browser", f"⚠ Research failed: {error}")
            progress_label.setText("Research failed.")
            return
        from .research import report_to_html
        from .webui import _with_noopener
        # Q2: the report opens behind the current tab — a background
        # job's completion does not yank the person from their page.
        # G-S4: report links carry rel="noopener".
        window.new_tab(html=_with_noopener(report_to_html(report)),
                       background=True)
        append("Forge Browser",
               "Research done. The report opened in a new tab behind this one.")
        progress_label.setText("Research done.")

    signals.research_done.connect(on_research_done)

    def on_brief_done(text):
        # U9: a background brief build lands in the log when it
        # finishes — the turn that asked for it ended long ago.
        append("Forge Browser", text)
        progress_label.setText("Brief done.")

    signals.brief_done.connect(on_brief_done)
    signals.token.connect(on_token)
    signals.answer.connect(on_answer)
    signals.failed.connect(on_failed)

    def on_plan(plan_text, state):
        # Runs on the GUI thread; the worker waits on state["event"].
        # Nothing runs until the person says yes.
        # V3: the working line says what is actually happening —
        # waiting for the person, not thinking.
        turn["step"] = "waiting for approval"
        if turn["started"] is not None:
            status_label.setText(
                status_text("waiting for approval", turn["started"]))
        append("Forge Browser", f"Plan: {plan_text}")

        def make():
            dialog = QtWidgets.QMessageBox(window)
            dialog.setWindowTitle("Approve this step?")
            dialog.setText(f"The browser wants to:\n\n{plan_text}\n\nAllow it?")
            dialog.setStandardButtons(
                QtWidgets.QMessageBox.StandardButton.Yes |
                QtWidgets.QMessageBox.StandardButton.No)
            dialog.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
            # P5: a cheap kind can be always-allowed from the dialog, so
            # the same step stops asking every turn.
            always_box = None
            from .seats import ALWAYS_ALLOWABLE_KINDS
            if state.get("kind") in ALWAYS_ALLOWABLE_KINDS:
                always_box = QtWidgets.QCheckBox(
                    f"Always allow {state['kind']}")
                dialog.setCheckBox(always_box)
            timed_out = {"value": False}

            def on_timeout():
                # P2: a missed dialog cannot hang the turn forever —
                # after 5 minutes the plan is denied, not left waiting.
                timed_out["value"] = True
                dialog.done(QtWidgets.QMessageBox.StandardButton.No)

            timer = QtCore.QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(on_timeout)
            timer.start(PLAN_TIMEOUT_SECONDS * 1000)
            answer = dialog.exec()
            timer.stop()
            state["approved"] = (
                answer == QtWidgets.QMessageBox.StandardButton.Yes)
            if timed_out["value"]:
                append("Forge Browser",
                       "Plan timed out after 5 minutes. Treated as no.")
            elif (state["approved"] and always_box is not None
                    and always_box.isChecked()):
                from .seats import set_always_allow
                set_always_allow(state["kind"], True)
            state["event"].set()
            # The turn resumes with the worker — the next on_step names
            # the real step; until then the line says thinking.
            turn["step"] = "thinking"

        # B3: the plan dialog goes through the one-dialog queue —
        # if another dialog is up, it waits its turn instead of
        # stacking (the worker's 5-minute backstop still bounds
        # the wait, and the queued dialog sets the event when the
        # person answers it).
        window._run_dialog(make)

    signals.plan.connect(on_plan)

    def ask_plan(plan_text, kind):
        # Called from the worker thread; blocks it until the person
        # answers the dialog on the GUI thread. The wait is capped
        # at the same 5 minutes as the dialog timer (P2 backstop):
        # even if the dialog never appears, the turn is denied,
        # not hung.
        # V2: the wait is timed and banked — it never counts as
        # work on the status or done lines.
        turn["wait_started"] = time.monotonic()
        state = {"event": threading.Event(), "approved": False,
                 "kind": kind}
        signals.plan.emit(plan_text, state)
        if not state["event"].wait(timeout=PLAN_TIMEOUT_SECONDS):
            signals.notice.emit(
                "Plan timed out after 5 minutes. Treated as no.")
        turn["waited"] = (turn.get("waited", 0.0)
                          + (time.monotonic() - turn["wait_started"]))
        turn["wait_started"] = None
        return state["approved"]

    stop_btn.clicked.connect(
        lambda: session is not None and session.cancel())

    def on_voice_text(text):
        # Transcribed speech lands in the entry for review — the
        # person reads it and hits Send; it is never sent itself.
        voice_state["transcribing"] = False
        apply_ai_state()
        if not text:
            append("Forge Browser",
                   "⚠ Couldn't make out anything in the recording.")
            return
        current = entry.text()
        entry.setText((current + " " + text).strip() if current else text)
        entry.setFocus()

    def on_voice_failed(reason):
        # S2: each failure gets its own plain sentence — a missing
        # transcriber is not a bad recording.
        voice_state["transcribing"] = False
        apply_ai_state()
        if reason == "no-transcriber":
            # G-UX19: point at the GUI installer, not a terminal
            # command or an env var.
            message = ("⚠ No transcriber is installed. Use the Forge "
                       "Browser installer to add voice support.")
        elif reason == "failed":
            message = ("⚠ Transcription failed. The transcriber "
                       "errored on that recording.")
        else:
            message = "⚠ Couldn't make out anything in the recording."
        append("Forge Browser", message)

    signals.voice_text.connect(on_voice_text)
    signals.voice_failed.connect(on_voice_failed)

    def tick_voice_clock():
        # S3/S7: while recording, the button shows elapsed time and
        # whether audio is arriving (the WAV file growing); at the
        # 2-minute cap the recording stops itself.
        recorder = voice_state["recorder"]
        if recorder is None:
            return
        from .voice import MAX_RECORD_SECONDS
        elapsed = recorder.elapsed()
        if elapsed >= MAX_RECORD_SECONDS:
            voice_state["capped"] = True
            on_mic_released()
            return
        size = 0
        try:
            size = os.path.getsize(recorder.path) if recorder.path else 0
        except OSError:
            pass
        growing = size > voice_state["last_size"]
        voice_state["last_size"] = size
        text = f"● Listening… {int(elapsed // 60)}:{int(elapsed % 60):02d}"
        if elapsed > 2 and not growing:
            text += ": no audio yet"
        mic_btn.setText(text)

    voice_timer = QtCore.QTimer(window)
    voice_timer.setInterval(500)
    voice_timer.timeout.connect(tick_voice_clock)

    def on_mic_pressed():
        from .voice import Recorder, find_recorder
        if voice_state["recorder"] is not None:
            return
        if voice_state["transcribing"]:
            return  # S9: one transcription at a time
        if find_recorder() is None:
            append("Forge Browser",
                   "⚠ No microphone recorder found (arecord, parecord, or "
                   "ffmpeg). Voice input needs one of those installed.")
            return
        recorder = Recorder()
        try:  # S10: a Popen failure must say so, not raise silently
            started = recorder.start()
        except Exception as exc:
            append("Forge Browser",
                   f"⚠ Couldn't start the microphone ({exc}).")
            return
        if not started:
            append("Forge Browser", "⚠ Couldn't start the microphone.")
            return
        voice_state["recorder"] = recorder
        voice_state["capped"] = False
        voice_state["last_size"] = 0
        mic_btn.setText("● Listening… 0:00")
        voice_timer.start()

    def on_mic_released():
        recorder = voice_state["recorder"]
        voice_state["recorder"] = None
        voice_timer.stop()
        mic_btn.setText("🎤 Hold to talk")
        if recorder is None:
            return
        if voice_state["capped"]:
            append("Forge Browser",
                   "Recording stopped at the 2-minute cap.")
        try:  # S10: a wedged recorder must say so, not raise silently
            path = recorder.stop()
        except Exception as exc:
            append("Forge Browser",
                   f"⚠ The recording could not be stopped cleanly ({exc}).")
            return
        if path is None:
            append("Forge Browser",
                   "⚠ Nothing recorded. Check the microphone.")
            return
        voice_state["transcribing"] = True
        apply_ai_state()
        signals.notice.emit("transcribing…")

        def work_voice():
            from .voice import transcribe_recording_detailed
            try:
                text, reason = transcribe_recording_detailed(path)
            except Exception:
                text, reason = "", "failed"
            try:
                os.unlink(path)
            except OSError:
                pass
            if text:
                signals.voice_text.emit(text)
            else:
                signals.voice_failed.emit(reason or "failed")

        threading.Thread(target=work_voice, daemon=True).start()

    def _make_chunk_handler(display):
        """Batched token delivery (H7) with the 'answering' step
        (H3). on_chunk accumulates shown text and emits at most
        every ~50 ms, so a fast stream doesn't make the GUI thread
        do a cursor move + insert + layout per token; the first
        chunk still shows at once. flush(extra) pushes any pending
        text plus already-filtered text (the held remainder) out
        at once. Every emitted batch switches the status to
        'answering', token first then step, so the ● marker (which
        needs state['live']) is set by the time the status line
        is drawn."""
        pending = {"text": "", "last_emit": 0.0}

        def flush(extra=""):
            pending["text"] += extra or ""
            if pending["text"]:
                signals.token.emit(pending["text"])
                signals.step.emit("answering")
                pending["text"] = ""
                pending["last_emit"] = time.monotonic()

        def on_chunk(delta):
            shown = display.feed(delta)
            if not shown:
                return
            pending["text"] += shown
            if time.monotonic() - pending["last_emit"] >= 0.05:
                flush()

        return on_chunk, flush

    def _note_retrieval_once():
        # I7: surface the embed failure reason once per session,
        # in plain words — Ollama down and model missing need
        # different fixes. Reads what page_context recorded.
        last = getattr(session, "_retrieve_last", None) or {}
        reason = last.get("reason")
        if not reason or getattr(session, "_retrieve_notice_shown", False):
            return
        session._retrieve_notice_shown = True
        if reason == "model-missing":
            # G-UX19: point at the GUI installer, not install.sh.
            signals.notice.emit(
                "Retrieval is off: nomic-embed-text is not installed. "
                "Use the Forge Browser installer to add it")
        elif reason == "ollama-down":
            signals.notice.emit(
                "Ollama is not running. Start Ollama to search long pages")
        else:
            signals.notice.emit(
                "Retrieval is off: the embeddings model did not answer")

    def work(text, page_text, tabs=None, page_url=None):
        display = LiveDisplay()
        on_chunk, flush_tokens = _make_chunk_handler(display)

        def on_action(kind, action):
            # H5/V4: action progress lines, matching the CLI's
            # shape — plain words, no turn-relative time (the old
            # "(12s)" read as the action's duration when it was
            # the turn's age; the duration lands when the step
            # finishes, below).
            signals.notice.emit(f"… {step_words(kind)}")

        def on_step_done(step, seconds):
            # V4: the step log — each finished step notes how
            # long it took.
            signals.notice.emit(
                f"{step_words(step)} took {format_duration(seconds)}")

        try:
            answer, opens = session.ask(
                text, page_text=page_text, tabs=tabs, page_url=page_url,
                on_action=on_action,
                on_step=lambda kind: signals.step.emit(kind),
                on_step_done=on_step_done,
                on_note=lambda text: signals.notice.emit(text),
                on_chunk=on_chunk, on_plan=ask_plan,
                on_table=lambda html: signals.compare_table.emit(html),
                on_progress=lambda text: signals.progress.emit(text),
                on_research_done=lambda report, sources, error=None:
                    signals.research_done.emit(report, sources, error),
                on_brief_done=lambda text:
                    signals.brief_done.emit(text))
            # I10: what retrieval did this turn rides the done line.
            turn["retrieve_note"] = retrieve_note(
                getattr(session, "_retrieve_last", None))
            _note_retrieval_once()
            for url in opens:
                signals.open_url.emit(url)
            flush_tokens(display.flush())
            signals.answer.emit(answer or "(no answer)")
        except Exception as exc:
            # H4: the held remainder streams out first, so a
            # cut-off answer keeps its last words; on_failed
            # marks it cut off.
            flush_tokens(display.flush())
            signals.failed.emit(str(exc))

    def work_summary(page_text, url=None, length="standard"):
        display = LiveDisplay()
        on_chunk, flush_tokens = _make_chunk_handler(display)

        try:
            from .summarize import (SummarizeEmptyError, SummarizeStopped,
                                    summarize_page)
            summary = summarize_page(
                session, page_text, url=url, on_chunk=on_chunk,
                on_progress=lambda text: signals.progress.emit(text),
                map_session=window._fast_seat_session(), length=length)
            flush_tokens(display.flush())
            # K5: "" now means the page itself was empty — a blank
            # brain raises instead, so this message is honest.
            signals.answer.emit(summary or "(no summary: the page text was empty)")
        except SummarizeStopped:
            # K1: a stopped summarize ends as "Stopped.", not a
            # failure.
            flush_tokens(display.flush())
            signals.answer.emit("Stopped.")
        except SummarizeEmptyError:
            flush_tokens(display.flush())
            signals.answer.emit("(no summary: the brain produced nothing)")
        except Exception as exc:
            flush_tokens(display.flush())
            signals.failed.emit(str(exc))

    def work_card(name, question, page_text):
        # O11: a card run streams into the log like a summarize —
        # the card's steps show as progress lines, its opens as tabs.
        display = LiveDisplay()
        on_chunk, flush_tokens = _make_chunk_handler(display)

        def on_action(kind, action):
            # H5/V4: plain words, no turn-relative time — the
            # duration lands when the step finishes, below.
            signals.notice.emit(f"… {step_words(kind)}")

        def on_step_done(step, seconds):
            # V4: the step log — each finished card step notes
            # how long it took.
            signals.notice.emit(
                f"{step_words(step)} took {format_duration(seconds)}")

        try:
            from .cards import run_card
            answer, opens = run_card(
                session, name, question=question, page_text=page_text,
                on_action=on_action, on_chunk=on_chunk,
                on_progress=lambda text: signals.progress.emit(text),
                on_step_done=on_step_done)
            for url in opens:
                signals.open_url.emit(url)
            flush_tokens(display.flush())
            signals.answer.emit(answer or "(no answer)")
        except Exception as exc:
            flush_tokens(display.flush())
            signals.failed.emit(str(exc))

    def do_card():
        if state["busy"]:
            return
        name = cards_combo.currentText()
        if not name:
            # R3-09: nothing picked — say so in the log and the
            # status line instead of silently doing nothing.
            append("Forge Browser", "Pick a card first.")
            window.statusBar().showMessage("Pick a card first.", 3000)
            return
        state["busy"] = True
        state["live"] = False
        turn["step"] = "running card"
        turn["retrieve_note"] = ""
        turn["started"] = time.monotonic()
        turn["waited"] = 0.0
        turn["wait_started"] = None
        status_label.setText(status_text("running card", turn["started"]))
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        run_card_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        # G-UX9: the entry stays editable while a card runs — only
        # Send waits for the turn to finish.
        # Clear any earlier stop, or this card ends at once.
        session._stop_requested = False
        question = entry.text().strip()
        entry.clear()
        append("You", f"Run the {name} card"
               + (f": {question}" if question else ""))
        view = window.tabs.widget(window.tabs.currentIndex())

        def got_page(page_text):
            threading.Thread(target=work_card,
                             args=(name, question, page_text or None),
                             daemon=True).start()

        if view is not None:
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''", got_page)
        else:
            got_page(None)

    def do_summarize():
        if state["busy"]:
            return
        state["busy"] = True
        state["live"] = False
        turn["step"] = "summarizing"
        turn["retrieve_note"] = ""
        turn["started"] = time.monotonic()
        turn["waited"] = 0.0
        turn["wait_started"] = None
        status_label.setText(turn_status_text("summarizing", turn["started"]))
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        # K1: the Stop button works during a summarize — a long
        # one is no longer unstoppable.
        stop_btn.setEnabled(True)
        # G-UX9: the entry stays editable while a summary runs.
        # Clear any earlier stop, or this summarize ends at once.
        session._stop_requested = False
        # K8: the button follows @tab — summarize the named tab
        # when the entry has @tabN.
        target = window.tabs.currentIndex()
        refs = re.findall(r"@tab(\d+)", entry.text() or "")
        if refs:
            n = int(refs[0])
            if 1 <= n <= window.tabs.count():
                target = n - 1
            else:
                append("Forge Browser",
                       f"There is no tab {n}. Summarizing the current tab.")
        view = window.tabs.widget(target)
        url = view.url().toString() if view is not None else None
        length = "short" if short_box.isChecked() else "standard"

        def got_page(page_text):
            threading.Thread(target=work_summary,
                             args=(page_text or None, url, length),
                             daemon=True).start()

        if view is not None:
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''", got_page)
        else:
            got_page(None)

    def do_send():
        text = entry.text().strip()
        if not text or state["busy"]:
            return
        state["busy"] = True
        state["live"] = False
        # J5: the working line names the phase honestly — the dock
        # is reading tabs before the brain starts, not thinking.
        turn["step"] = "reading tabs"
        turn["retrieve_note"] = ""
        turn["started"] = time.monotonic()
        turn["waited"] = 0.0
        turn["wait_started"] = None
        status_label.setText(turn_status_text("reading tabs", turn["started"]))
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        # G-UX9: the entry stays editable while a turn runs.
        entry.clear()
        append("You", text)
        _log_action("Sent a chat message")
        # J4: the current tab's text is always needed; the rest
        # only when the message names tabs or asks to compare.
        read_all = needs_all_tab_text(text)
        count = window.tabs.count()
        current = window.tabs.currentIndex()
        # G-C21: collected text is keyed by view identity, not by
        # position — a tab closed or reordered during the 5 s
        # window used to pair text with the wrong tab.
        views = [window.tabs.widget(i) for i in range(count)]
        current_view = (views[current]
                        if 0 <= current < count else None)
        queried = [view for view in views
                   if view is not None
                   and (view is current_view or read_all)]
        texts = {}
        started = {"done": False}

        def start_work():
            if started["done"]:
                return
            started["done"] = True
            turn["step"] = "thinking"
            status_label.setText(
                turn_status_text("thinking", turn["started"]))
            live_views = [window.tabs.widget(i)
                          for i in range(window.tabs.count())]
            tabs = send_tabs_payload(live_views, texts, current_view,
                                     window._agent_tab_ids)
            page_text = (texts.get(id(current_view), "")
                         if current_view is not None else None)
            page_url = (current_view.url().toString()
                        if current_view is not None else None)
            threading.Thread(target=work, args=(text, page_text, tabs, page_url),
                             daemon=True).start()

        def collect(view, page_text):
            texts[id(view)] = page_text or ""
            if len(texts) == len(queried):
                start_work()

        if not queried:
            start_work()
            return
        for view in queried:
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''",
                lambda page_text, v=view: collect(v, page_text))
        # A tab that never answers must not hang the send.
        QtCore.QTimer.singleShot(5000, start_work)

    send.clicked.connect(do_send)
    entry.send_requested.connect(do_send)
    summarize_btn.clicked.connect(do_summarize)
    run_card_btn.clicked.connect(do_card)
    mic_btn.pressed.connect(on_mic_pressed)
    mic_btn.released.connect(on_mic_released)
    # The settings window calls this after a save, so the chat dock
    # picks the new seats and toggles without a restart. It is the
    # refresh handler: a brain seat changed in Settings must
    # re-pick the brain at once, not wait for a restart (G7/G9 —
    # the same re-pick the ↻ button does).
    window._refresh_ai_controls = on_refresh


def open_settings_dialog(parent, on_applied=None):
    """Settings window: seats, AI toggles, download folder, and the
    Ollama + SearXNG addresses — no hand-editing files or env vars.

    The Ollama and SearXNG addresses take effect when the browser
    restarts; everything else applies at once."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView, QWebEngineDownloadRequest) = _load_qt()
    from .chat import check_ollama
    from .files import download_dir
    from .seats import set_feature, set_seat, load_seats
    from .settings import get_setting, set_setting
    from .media import OLLAMA_URL

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Settings")
    dialog.setMinimumSize(480, 420)
    layout = QtWidgets.QVBoxLayout(dialog)
    seats = load_seats()
    # G-P1: the model list arrives from a background thread — the
    # old list_models() call blocked the dialog from appearing
    # for up to 5 s with Ollama down. Combos start with "(auto)"
    # only and fill in when the list lands.
    model_sizes = {}
    combos = {}
    desc_labels = {}

    # B8: each seat says what it is for, under its combo.
    _SEAT_DESCRIPTIONS = {
        "brain": "Brain: answers questions and drives the browser.",
        "fast": "Fast: quick jobs, short titles and fast summaries.",
        "vision": "Vision: reads pictures and describes images.",
    }

    def seat_row(label, key, object_name):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label))
        combo = QtWidgets.QComboBox()
        combo.setObjectName(object_name)
        combo.addItem("(auto)", None)
        saved = seats.get(key)
        if saved:
            combo.addItem(saved, saved)
            combo.setCurrentIndex(1)
        row.addWidget(combo, 1)
        layout.addLayout(row)
        desc = QtWidgets.QLabel(_SEAT_DESCRIPTIONS[key])
        desc.setWordWrap(True)
        layout.addWidget(desc)
        combos[key] = combo
        desc_labels[key] = desc
        return combo

    def refresh_seat_descriptions():
        # B7: under each combo, the seat's purpose plus the picked
        # model's one-line blurb (what it does, its size, its fit).
        for key, combo in combos.items():
            picked = combo.currentData()
            text = _SEAT_DESCRIPTIONS[key]
            if picked:
                text += " " + model_blurb(picked,
                                          model_sizes.get(picked))
            desc_labels[key].setText(text)

    brain_combo = seat_row("Brain model:", "brain", "settings_brain")
    fast_combo = seat_row("Fast model:", "fast", "settings_fast")
    vision_combo = seat_row("Vision model:", "vision", "settings_vision")
    for combo in combos.values():
        combo.currentIndexChanged.connect(
            lambda *a: refresh_seat_descriptions())

    class _SettingsSignals(QtCore.QObject):
        models_ready = QtCore.Signal(object)  # check_ollama details

    settings_signals = _SettingsSignals(dialog)

    def on_models_ready(details):
        # Fill the combos with names + sizes, keeping the saved
        # pick selected (or "(auto)").
        model_sizes.clear()
        for item in details:
            model_sizes[item["name"]] = item["size"]
        for key, combo in combos.items():
            saved = seats.get(key)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(auto)", None)
            for item in details:
                name = item["name"]
                if key in ("brain", "fast") and "embed" in name.lower():
                    continue
                display = name
                if item["size"]:
                    display = f"{name} ({_human_size(item['size'])})"
                combo.addItem(display, name)
                combo.setItemData(
                    combo.count() - 1,
                    model_blurb(name, item["size"]),
                    QtCore.Qt.ItemDataRole.ToolTipRole)
            if saved and saved not in [d["name"] for d in details]:
                combo.addItem(f"{saved} (not installed)", saved)
                combo.setCurrentIndex(combo.count() - 1)
            elif saved:
                index = combo.findData(saved)
                combo.setCurrentIndex(index if index >= 0 else 0)
            else:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)
        refresh_seat_descriptions()

    settings_signals.models_ready.connect(on_models_ready)

    def work_models():
        try:
            _reachable, details = check_ollama()
        except Exception:
            details = []
        try:
            settings_signals.models_ready.emit(details)
        except RuntimeError:
            pass  # the dialog closed before the list landed

    threading.Thread(target=work_models, daemon=True).start()

    feature_boxes = {}
    for key, label in (("ai", "Use the AI (chat, tidy titles, brief)"),
                       # G-UX4: the label says what the toggle
                       # actually toggles — the chat brain and its
                       # jobs, not the dock's visibility.
                       ("chat", "Chat and brain jobs"),
                       ("tidy_titles", "Tidy up long tab titles"),
                       ("memory", "Remember facts and pages")):
        box = QtWidgets.QCheckBox(label)
        if key == "chat":
            box.setToolTip(
                "The chat brain, and every brain job: summarize, the "
                "morning brief, compare, and research.")
        if key == "ai":
            box.setChecked(seats["ai_enabled"])
        else:
            box.setChecked(seats["features"].get(key, True))
        feature_boxes[key] = box
        layout.addWidget(box)

    download_row = QtWidgets.QHBoxLayout()
    download_row.addWidget(QtWidgets.QLabel("Download folder:"))
    download_entry = QtWidgets.QLineEdit()
    download_entry.setObjectName("settings_download_dir")
    download_entry.setText(get_setting("download_dir") or download_dir())
    download_row.addWidget(download_entry, 1)
    browse_btn = QtWidgets.QPushButton("Browse…")
    browse_btn.clicked.connect(
        lambda: download_entry.setText(
            QtWidgets.QFileDialog.getExistingDirectory(
                dialog, "Choose the download folder",
                download_entry.text() or os.path.expanduser("~")) or
            download_entry.text()))
    download_row.addWidget(browse_btn)
    layout.addLayout(download_row)

    # R3-32: ask where to save each download, instead of always
    # landing in the folder above.
    ask_box = QtWidgets.QCheckBox(
        "Ask where to save each download")
    ask_box.setObjectName("settings_download_ask")
    ask_box.setChecked(get_setting("download_ask") == "1")
    layout.addWidget(ask_box)

    # B11: the service addresses live behind an Advanced section —
    # settings that rarely change do not sit in the main list.
    # (The link-preview dwell control is gone entirely: the 600 ms
    # default stands, and the setting still reads if one was saved.)
    advanced_btn = QtWidgets.QPushButton("Advanced…")
    advanced_btn.setObjectName("settings_advanced")
    advanced_btn.setCheckable(True)
    layout.addWidget(advanced_btn)
    advanced_box = QtWidgets.QWidget()
    advanced_box.setObjectName("settings_advanced_box")
    advanced_box.setVisible(False)
    advanced_layout = QtWidgets.QVBoxLayout(advanced_box)
    advanced_layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(advanced_box)
    advanced_btn.toggled.connect(advanced_box.setVisible)

    ollama_row = QtWidgets.QHBoxLayout()
    ollama_row.addWidget(QtWidgets.QLabel("Ollama address:"))
    ollama_entry = QtWidgets.QLineEdit()
    ollama_entry.setObjectName("settings_ollama_url")
    ollama_entry.setText(get_setting("ollama_url") or OLLAMA_URL)
    ollama_row.addWidget(ollama_entry, 1)
    advanced_layout.addLayout(ollama_row)

    searxng_row = QtWidgets.QHBoxLayout()
    searxng_row.addWidget(QtWidgets.QLabel("SearXNG address:"))
    searxng_entry = QtWidgets.QLineEdit()
    searxng_entry.setObjectName("settings_searxng_url")
    searxng_entry.setText(get_setting("searxng_url") or "")
    searxng_entry.setPlaceholderText("Leave empty for DuckDuckGo / Bing")
    searxng_row.addWidget(searxng_entry, 1)
    advanced_layout.addLayout(searxng_row)

    note = QtWidgets.QLabel(
        "The Ollama and SearXNG addresses take effect when the browser "
        "restarts.")
    note.setWordWrap(True)
    advanced_layout.addWidget(note)

    # G-S3: address problems show here, in the dialog — no second
    # dialog stacked on Settings for a typo.
    error_label = QtWidgets.QLabel("")
    error_label.setObjectName("settings_error")
    error_label.setWordWrap(True)
    error_label.setStyleSheet("color: #ff8a7a;")
    layout.addWidget(error_label)

    buttons = QtWidgets.QHBoxLayout()
    save_btn = QtWidgets.QPushButton("Save")
    cancel_btn = QtWidgets.QPushButton("Cancel")
    buttons.addStretch(1)
    buttons.addWidget(save_btn)
    buttons.addWidget(cancel_btn)
    layout.addLayout(buttons)

    def save():
        # G-S3: the addresses are validated at save time. A remote
        # host needs an explicit yes — it sends prompts, page text,
        # and memory off this machine.
        for label, entry in (("Ollama", ollama_entry),
                             ("SearXNG", searxng_entry)):
            text = entry.text().strip()
            ok, remote, problem = validate_service_url(text)
            if not ok:
                error_label.setText(f"{label} address: {problem}")
                return
            if remote:
                host = urllib.parse.urlparse(text).hostname or text
                answer = QtWidgets.QMessageBox.question(
                    dialog, f"{label} address is not on this machine",
                    f"The {label} address points at {host}, not this "
                    "machine.\n\nPrompts, page text, and remembered "
                    "facts would go to that machine.\n\nSave it anyway?",
                    QtWidgets.QMessageBox.StandardButton.Yes |
                    QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No)
                if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
        for key, combo in (("brain", brain_combo), ("fast", fast_combo),
                           ("vision", vision_combo)):
            picked = combo.currentData()
            set_seat(key, picked)
        for key, box in feature_boxes.items():
            set_feature(key, box.isChecked())
        set_setting("download_dir", download_entry.text().strip())
        set_setting("download_ask",
                    "1" if ask_box.isChecked() else "0")
        set_setting("ollama_url", ollama_entry.text().strip())
        set_setting("searxng_url", searxng_entry.text().strip())
        if on_applied is not None:
            on_applied()
        dialog.accept()

    save_btn.clicked.connect(save)
    cancel_btn.clicked.connect(dialog.reject)
    dialog.exec()


def _splash_pixmap(QtGui, QtCore):
    """R3-26: the startup splash image — Forge Browser on a dark
    card, drawn in code so it needs no image file. The Qt modules
    arrive as arguments because app.py loads Qt lazily."""
    pixmap = QtGui.QPixmap(360, 160)
    pixmap.fill(QtGui.QColor("#101216"))
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(QtGui.QColor("#ffb347")))
    painter.drawRoundedRect(2, 2, 356, 156, 12, 12)
    painter.setPen(QtGui.QPen(QtGui.QColor("#e8e4da")))
    font = painter.font()
    font.setPointSize(22)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(),
                     QtCore.Qt.AlignmentFlag.AlignCenter,
                     "Forge Browser")
    painter.end()
    return pixmap


def run_app(start_url=None):
    """Open the Forge Browser window. Blocks until it closes."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView, QWebEngineDownloadRequest) = _load_qt()
    from .cdp import default_profile_dir
    from .webui import LocalPageServer

    # U12: the debug log starts with the app — a crash writes a
    # report instead of vanishing without a trace.
    get_debug_log().install_crash_handler()
    get_debug_log().action("Forge Browser started")

    app = QtWidgets.QApplication([])
    app.setApplicationName("Forge Browser")
    app.setOrganizationName("Forge Browser")

    # U27: one copy at a time — a second launch raises this one
    # and exits. The guard is held for the life of the app.
    guard = _take_single_instance()
    if guard is None:
        return 0
    _lock, local_server = guard

    # R3-26: a splash shows at once — a cold first run can take
    # the better part of a minute before the window appears, and
    # with nothing on screen the launch looks dead.
    splash = QtWidgets.QSplashScreen(_splash_pixmap(QtGui, QtCore))
    splash.show()
    splash.showMessage("Starting Forge Browser…",
                       QtCore.Qt.AlignmentFlag.AlignHCenter |
                       QtCore.Qt.AlignmentFlag.AlignBottom,
                       QtGui.QColor("#e8e4da"))
    app.processEvents()

    server = LocalPageServer().start()
    splash.showMessage("Starting the local page server…",
                       QtCore.Qt.AlignmentFlag.AlignHCenter |
                       QtCore.Qt.AlignmentFlag.AlignBottom,
                       QtGui.QColor("#e8e4da"))
    app.processEvents()
    profile = QWebEngineProfile("forge-browser")
    profile.setPersistentStoragePath(
        os.path.join(default_profile_dir(), "app-profile"))
    profile.setCachePath(os.path.join(default_profile_dir(), "app-cache"))

    window = create_window(server, profile, start_url)
    local_server.newConnection.connect(
        lambda: _handle_show_request(local_server, window))
    window.show()
    splash.finish(window)
    # v0.12: the update check runs after the window shows —
    # never before first paint, never on the UI thread.
    QtCore.QTimer.singleShot(
        0, window._maybe_check_for_updates_on_startup)
    try:
        return app.exec()
    finally:
        server.stop()
