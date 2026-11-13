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
import threading
import time
import urllib.parse

from .chat import format_duration, step_words
from .errors import BrowserError
from .preview import dwell_ms

_TAGLINE = "Forge Browser — no accounts, no API keys, nothing tracked."

# N7: tab titles — raw and tidy alike — are capped at this many
# characters on display, so five long words can't make a wide tab.
_TAB_TITLE_CHARS = 28

# P2: a plan the person never answers is denied after 5 minutes —
# a missed dialog cannot hang a turn forever.
PLAN_TIMEOUT_SECONDS = 300

_QT = None


def needs_all_tab_text(text):
    """Whether a send must read every tab's text (J4): the message
    names tabs (@tabN), or asks to compare. Anything else reads
    the current tab only — every send used to wait for all tabs
    (or the 5 s guard) for text nobody asked for."""
    lowered = (text or "").lower()
    return "@tab" in lowered or "compare" in lowered


def turn_status_text(step, started, now=None, waited=0):
    """The dock's working line: 'working: searching… — 12s'.
    The step is named in plain words (V1), not the raw action
    kind ("recall_pages"). waited seconds — time spent at the
    plan gate — do not count as work (V2), so the clock pauses
    while the person decides. Under a second the line says
    "<1s", not "0s" (V5)."""
    now = time.monotonic() if now is None else now
    elapsed = max(0.0, now - started - waited)
    return f"working: {step_words(step)}… — {format_duration(elapsed)}"


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
            "this Python. Run install.sh — it builds the app venv — "
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

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Forge Browser")
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
            self._downloads = {}  # id(download) -> record dict
            self._downloads_dialog = None
            self._vault = None  # the vault, once unlocked this session
            self._vault_declined = False  # the person said no to unlocking
            self._autofill_asked = set()  # hosts already asked about
            self._autofill_done = set()  # hosts already filled
            self._refresh_ai_controls = None  # set by _attach_chat
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.tabCloseRequested.connect(self._close_tab)
            self.tabs.currentChanged.connect(lambda _: self._sync_address())
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
                "Type an address, or words to search — Forge Browser searches keyless")
            self.address.returnPressed.connect(self._go)
            bar.addWidget(self.address)
            bar.addAction("＋ Tab", lambda: self.new_tab())
            star = bar.addAction("★", self._bookmark_current)
            star.setToolTip("Bookmark this page (Ctrl+D)")
            self._build_menu()
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+F"), self,
                            self._show_find)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+="), self,
                            self._zoom_in)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl++"), self,
                            self._zoom_in)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+-"), self,
                            self._zoom_out)
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+0"), self,
                            self._zoom_reset)
            self.statusBar().showMessage(_TAGLINE)
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
            # Remember size, position, and dock layout for next launch.
            settings = QtCore.QSettings("ForgeBrowser", "Forge Browser")
            settings.setValue("window/geometry", self.saveGeometry())
            settings.setValue("window/state", self.saveState())
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
            view.loadFinished.connect(
                lambda *a, view=view: self._on_load_finished(view))
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
                view.setHtml(html)
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
            model, no extra swap)."""
            from .chat import ChatSession
            from .seats import pick_fast_model
            fast = pick_fast_model()
            session = getattr(self, "_chat_session", None)
            if not fast or session is None or fast == session.model:
                return session
            cached = getattr(self, "_fast_session", None)
            if cached is None or cached.model != fast:
                cached = ChatSession(model=fast)
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

        def _on_load_finished(self, view):
            self._sync_address()
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
            if self.tabs.count() > 1:
                widget = self.tabs.widget(index)
                self._agent_tab_ids.discard(id(widget))
                self.tabs.removeTab(index)
                widget.deleteLater()
                self._sync_tab_numbers()

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
                # Forge Browser's own local pages aren't addresses
                # the person typed — keep the bar clean on those.
                self.address.setText("" if url.startswith("http://127.0.0.1") else url)
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
                self.setWindowTitle(f"{title} — Forge Browser")
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
                    "No comparison to refresh — ask the chat to compare "
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
                    "The compared tabs are closed — nothing to refresh."))
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
                    new_rows, dropped = compare_tabs(session, tabs, fields)
                    set_last_comparison(new_rows, fields)
                    page = compare_table_html(new_rows, fields,
                                              dropped=dropped)
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
            elif target == "/":
                target = server.home_url
            view = self._view()
            if view is not None:
                # P6: typed in the address bar by hand — the tab is
                # the person's now (view.load reports Other, so the
                # page hook can't see this one).
                self._unmark_agent(view)
                view.load(QtCore.QUrl(target))

        # --- menu bar (E8) -----------------------------------------
        def _menu_action(self, menu, text, slot, shortcut=None):
            action = QtGui.QAction(text, self)
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
            self._menu_action(
                file_menu, "Close Tab",
                lambda: self._close_tab(self.tabs.currentIndex()), "Ctrl+W")
            file_menu.addSeparator()
            self._menu_action(file_menu, "Downloads",
                              self._open_downloads_dialog)
            file_menu.addSeparator()
            self._menu_action(file_menu, "Quit", self.close, "Ctrl+Q")
            edit_menu = bar.addMenu("&Edit")
            self._menu_action(edit_menu, "Find in Page",
                              self._show_find, "Ctrl+F")
            view_menu = bar.addMenu("&View")
            self._menu_action(view_menu, "Zoom In", self._zoom_in, "Ctrl+=")
            self._menu_action(view_menu, "Zoom Out", self._zoom_out, "Ctrl+-")
            self._menu_action(view_menu, "Reset Zoom", self._zoom_reset,
                              "Ctrl+0")
            self._bookmarks_menu = bar.addMenu("&Bookmarks")
            self._bookmarks_menu.aboutToShow.connect(
                self._rebuild_bookmarks_menu)
            tools_menu = bar.addMenu("&Tools")
            self._menu_action(tools_menu, "Downloads",
                              self._open_downloads_dialog)
            self._menu_action(tools_menu, "Settings…", self._open_settings)
            help_menu = bar.addMenu("&Help")
            self._menu_action(help_menu, "About Forge Browser",
                              self._show_about)

        def _show_about(self):
            from . import __version__
            QtWidgets.QMessageBox.about(
                self, "About Forge Browser",
                f"Forge Browser {__version__}\n\nYour browser. "
                "No accounts, no API keys, nothing tracked.\n\n"
                "A local AI model drives the browsing — search, pages, "
                "and research, all on this machine.")

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
            else:
                found = result.numberOfMatches() > 0
            self._find_label.setText("" if found else "No matches")

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
            from .bookmarks import add_bookmark
            view = self._view()
            if view is None:
                return
            url = view.url().toString()
            if not url.startswith("http") or url.startswith(server.home_url):
                self.statusBar().showMessage(
                    "This page can't be bookmarked.", 3000)
                return
            add_bookmark(url, view.title() or url)
            self.statusBar().showMessage(
                f"Bookmarked: {view.title() or url}", 3000)

        def _rebuild_bookmarks_menu(self):
            from .bookmarks import list_bookmarks
            menu = self._bookmarks_menu
            menu.clear()
            self._menu_action(menu, "Bookmark This Page",
                              self._bookmark_current, "Ctrl+D")
            menu.addSeparator()
            marks = list_bookmarks()[:15]
            for mark in marks:
                title = (mark.get("title") or mark["url"])[:40]
                self._menu_action(
                    menu, title,
                    lambda url=mark["url"]: self.new_tab(url))
            if marks:
                menu.addSeparator()
            self._menu_action(menu, "All Bookmarks…",
                              self._open_bookmarks_dialog)

        def _open_bookmarks_dialog(self):
            from .bookmarks import list_bookmarks, remove_bookmark
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Forge Browser — Bookmarks")
            dialog.setMinimumSize(500, 300)
            layout = QtWidgets.QVBoxLayout(dialog)
            listing = QtWidgets.QListWidget()
            listing.setObjectName("bookmarks_list")
            layout.addWidget(listing)

            def refresh():
                listing.clear()
                for mark in list_bookmarks():
                    item = QtWidgets.QListWidgetItem(
                        f"{mark.get('title') or mark['url']} — {mark['url']}")
                    item.setData(QtCore.Qt.ItemDataRole.UserRole,
                                 mark["url"])
                    listing.addItem(item)

            refresh()
            buttons = QtWidgets.QHBoxLayout()
            open_btn = QtWidgets.QPushButton("Open")
            remove_btn = QtWidgets.QPushButton("Remove")
            close_btn = QtWidgets.QPushButton("Close")

            def open_selected():
                item = listing.currentItem()
                if item is not None:
                    self.new_tab(item.data(QtCore.Qt.ItemDataRole.UserRole))
                    dialog.accept()

            def remove_selected():
                item = listing.currentItem()
                if item is not None:
                    remove_bookmark(
                        item.data(QtCore.Qt.ItemDataRole.UserRole))
                    refresh()

            open_btn.clicked.connect(open_selected)
            remove_btn.clicked.connect(remove_selected)
            close_btn.clicked.connect(dialog.reject)
            listing.itemDoubleClicked.connect(lambda item: open_selected())
            buttons.addWidget(open_btn)
            buttons.addWidget(remove_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            dialog.exec()

        # --- downloads (E1) -----------------------------------------
        def _on_download_requested(self, download):
            # The file downloads to a temp name in the download
            # folder (same filesystem, so the final move is the
            # atomic link in keep_download), then lands under its
            # uniquified name when it finishes.
            from .files import download_dir
            name = download.downloadFileName() or "download"
            folder = download_dir()
            tmp_name = f".forge-download-{id(download)}.part"
            download.setDownloadDirectory(folder)
            download.setDownloadFileName(tmp_name)
            download.accept()
            record = {"download": download, "name": name,
                      "tmp": os.path.join(folder, tmp_name),
                      "folder": folder, "final": None,
                      "status": "downloading"}
            self._downloads[id(download)] = record
            download.receivedBytesChanged.connect(
                self._refresh_downloads_list)
            download.totalBytesChanged.connect(
                self._refresh_downloads_list)
            download.stateChanged.connect(
                lambda state, key=id(download):
                    self._on_download_state(key, state))
            self.statusBar().showMessage(f"Downloading {name}…", 5000)
            self._refresh_downloads_list()

        def _download_line(self, record):
            download = record["download"]
            name = record["name"]
            if record["status"] == "done":
                return f"{name} — Saved to {record['final']}"
            if record["status"] == "cancelled":
                return f"{name} — Cancelled"
            if record["status"] == "failed":
                return f"{name} — Failed"
            received = download.receivedBytes()
            total = download.totalBytes()
            if total > 0:
                pct = int(received * 100 / total)
                return (f"{name} — {pct}% ({_human_size(received)} of "
                        f"{_human_size(total)})")
            return f"{name} — {_human_size(received)}"

        def _refresh_downloads_list(self):
            listing = getattr(self, "_downloads_list", None)
            if listing is None:
                return
            listing.clear()
            for record in self._downloads.values():
                listing.addItem(self._download_line(record))

        def _remove_download_tmp(self, record):
            try:
                os.unlink(record["tmp"])
            except OSError:
                pass

        def _on_download_state(self, key, state):
            record = self._downloads.get(key)
            if record is None:
                return
            states = QWebEngineDownloadRequest.DownloadState
            if state == states.DownloadCompleted:
                from .files import keep_download
                try:
                    record["final"] = keep_download(
                        record["tmp"], record["name"], record["folder"])
                    record["status"] = "done"
                    self.statusBar().showMessage(
                        f"Downloaded {record['name']} to {record['final']}",
                        8000)
                except OSError as exc:
                    record["status"] = "failed"
                    self._remove_download_tmp(record)
                    self.statusBar().showMessage(
                        f"Could not save {record['name']}: {exc}", 8000)
            elif state == states.DownloadCancelled:
                record["status"] = "cancelled"
                self._remove_download_tmp(record)
            elif state == states.DownloadInterrupted:
                record["status"] = "failed"
                self._remove_download_tmp(record)
                self.statusBar().showMessage(
                    f"Download failed: {record['name']}", 8000)
            self._refresh_downloads_list()

        def _on_downloads_dialog_closed(self, *args):
            self._downloads_dialog = None
            self._downloads_list = None

        def _open_downloads_dialog(self):
            if self._downloads_dialog is not None:
                self._downloads_dialog.show()
                self._downloads_dialog.raise_()
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Forge Browser — Downloads")
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
            buttons = QtWidgets.QHBoxLayout()
            folder_btn = QtWidgets.QPushButton("Open download folder")
            cancel_btn = QtWidgets.QPushButton("Cancel download")
            close_btn = QtWidgets.QPushButton("Close")

            def open_folder():
                from .files import download_dir
                QtGui.QDesktopServices.openUrl(
                    QtCore.QUrl.fromLocalFile(download_dir()))

            def cancel_selected():
                row = listing.currentRow()
                records = list(self._downloads.values())
                if 0 <= row < len(records):
                    records[row]["download"].cancel()

            folder_btn.clicked.connect(open_folder)
            cancel_btn.clicked.connect(cancel_selected)
            close_btn.clicked.connect(dialog.close)
            buttons.addWidget(folder_btn)
            buttons.addWidget(cancel_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            dialog.destroyed.connect(self._on_downloads_dialog_closed)
            self._downloads_dialog = dialog
            self._refresh_downloads_list()
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
            if result != "login":
                return
            if host in self._autofill_asked or host in self._autofill_done:
                return
            self._autofill_asked.add(host)
            from .vault import Vault, default_vault_path, find_entry_for_url
            if not os.path.exists(default_vault_path()):
                return  # no vault, nothing to fill
            if self._vault is None:
                if self._vault_declined:
                    return
                self._vault = self._unlock_vault()
                if self._vault is None:
                    self._vault_declined = True
                    return
            found = find_entry_for_url(self._vault, url)
            if found is None:
                return  # no saved login for this site
            site, entry = found
            answer = QtWidgets.QMessageBox.question(
                self, "Forge Browser — fill login?",
                f"Fill the saved login for {site}?\n\n"
                f"Username: {entry.get('username', '')}\n"
                "The password is filled in but never shown.",
                QtWidgets.QMessageBox.StandardButton.Yes |
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self._fill_login(view, entry.get("username", ""),
                             entry.get("password", ""))
            self._autofill_done.add(host)
            self.statusBar().showMessage(
                f"Filled the saved login for {site} — check it and sign in.",
                8000)

        def _unlock_vault(self):
            from .vault import Vault, default_vault_path
            passphrase, ok = QtWidgets.QInputDialog.getText(
                self, "Forge Browser — vault",
                "Vault passphrase (to check for a saved login):",
                QtWidgets.QLineEdit.EchoMode.Password)
            if not ok or not passphrase:
                return None
            try:
                return Vault.open(default_vault_path(), passphrase)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Forge Browser — vault", str(exc))
                return None

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

    signals = Signals()
    session = ChatSession()  # picks the brain, with reason + state
    window._chat_session = session  # tab titles tidy against this brain
    brain_health = {"checked": False, "answers": None}
    retrieval_state = {"models": []}  # fill_seat_combos keeps this fresh

    def brain_header_text():
        """The dock header from the session's pick: the model, how it
        was picked (G8), whether it answers (G7), and any
        fall-through note (G3). Ollama-down and no-models get
        different lines — the fix is different (G9). Plus the
        retrieval line (I1): on when nomic-embed-text is
        installed, off with the fix otherwise."""
        if session.brain_state == "ollama-down":
            text = ("Brain: none — Ollama is not running. Start Ollama, "
                    "then ↻. Browsing still works.")
        elif session.brain_state == "no-models":
            text = ("Brain: none — Ollama is running but no chat model "
                    "is installed. Pull a small model, then ↻. "
                    "Browsing still works.")
        elif not session.model:
            text = ("Brain: none answering — start Ollama, then ↻. "
                    "Browsing still works.")
        else:
            text = f"Brain: {session.model}"
            if session.pick_reason and session.pick_reason != "given":
                text += f" ({session.pick_reason})"
            if brain_health["checked"]:
                text += (" — answering" if brain_health["answers"]
                         else " — not answering")
            if session.pick_note:
                text += f"\n{session.pick_note}"
        models = retrieval_state.get("models") or []
        if any("nomic-embed-text" in m for m in models):
            text += "\nRetrieval: on"
        elif session.brain_state == "ollama-down":
            text += "\nRetrieval: off — Ollama is not running"
        else:
            text += "\nRetrieval: off — pull nomic-embed-text"
        return text

    def _check_brain_in_background():
        # G7: prove the picked model answers, off the GUI thread.
        model = session.model
        if not model:
            return

        def work_check():
            from .chat import check_model_answers
            signals.brain_checked.emit(check_model_answers(model), model)

        threading.Thread(target=work_check, daemon=True).start()

    def _build_brief_in_background():
        # The morning brief builds once a day in the background; the
        # start page shows it on its next load. A failed brain call
        # is not cached, so the next launch retries. T2: the brief
        # is a brain job — it does not build when AI/chat is off.
        from .seats import seats_enabled
        if not seats_enabled("chat"):
            return
        from .brief import build_brief
        try:
            build_brief(session)
        except Exception:
            pass

    threading.Thread(target=_build_brief_in_background, daemon=True).start()

    dock = QtWidgets.QDockWidget("Chat — talk to your browser", window)
    dock.setObjectName("chat_dock")  # saveState needs a name
    panel = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(panel)
    header = QtWidgets.QLabel(brain_header_text())
    header.setWordWrap(True)
    layout.addWidget(header)
    log = QtWidgets.QTextEdit()
    log.setReadOnly(True)
    layout.addWidget(log)
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
    row = QtWidgets.QHBoxLayout()
    entry = QtWidgets.QLineEdit()
    entry.setPlaceholderText("Ask for a page, a search, a summary… (try @tab2)")
    send = QtWidgets.QPushButton("Send")
    summarize_btn = QtWidgets.QPushButton("Summarize")
    mic_btn = QtWidgets.QPushButton("🎤 Hold to talk")
    mic_btn.setObjectName("voice_button")
    stop_btn = QtWidgets.QPushButton("Stop")
    stop_btn.setObjectName("stop_button")
    stop_btn.setEnabled(False)
    row.addWidget(entry)
    row.addWidget(send)
    row.addWidget(summarize_btn)
    row.addWidget(mic_btn)
    row.addWidget(stop_btn)
    layout.addLayout(row)
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
    memory_btn.setToolTip("See what the browser remembers — facts and "
                          "stored pages — and forget things.")
    memory_row.addWidget(memory_btn)
    memory_row.addStretch(1)
    layout.addLayout(memory_row)

    def show_memory_view():
        from .memory import (forget, forget_page, list_entries, list_pages)
        dialog = QtWidgets.QDialog(window)
        dialog.setWindowTitle("Forge Browser — memory")
        dialog.setObjectName("memory_dialog")
        vbox = QtWidgets.QVBoxLayout(dialog)
        facts_list = QtWidgets.QListWidget()
        facts_list.setObjectName("memory_facts")
        pages_list = QtWidgets.QListWidget()
        pages_list.setObjectName("memory_pages")

        def refresh():
            facts_list.clear()
            for entry in list_entries():
                item = QtWidgets.QListWidgetItem(entry["text"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole, entry["id"])
                facts_list.addItem(item)
            pages_list.clear()
            for page in list_pages():
                item = QtWidgets.QListWidgetItem(
                    f"{page['title']} — {page['url']}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, page["url"])
                pages_list.addItem(item)

        def forget_fact():
            item = facts_list.currentItem()
            if item is not None:
                forget(item.data(QtCore.Qt.ItemDataRole.UserRole))
                refresh()

        def remove_page():
            item = pages_list.currentItem()
            if item is not None:
                forget_page(item.data(QtCore.Qt.ItemDataRole.UserRole))
                refresh()

        vbox.addWidget(QtWidgets.QLabel("Remembered facts:"))
        vbox.addWidget(facts_list)
        forget_btn = QtWidgets.QPushButton("Forget selected fact")
        forget_btn.setObjectName("memory_forget_button")
        forget_btn.clicked.connect(forget_fact)
        vbox.addWidget(forget_btn)
        vbox.addWidget(QtWidgets.QLabel("Stored pages:"))
        vbox.addWidget(pages_list)
        remove_btn = QtWidgets.QPushButton("Remove selected page")
        remove_btn.setObjectName("memory_remove_button")
        remove_btn.clicked.connect(remove_page)
        vbox.addWidget(remove_btn)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        vbox.addWidget(close_btn)
        refresh()
        dialog.resize(560, 480)
        dialog.exec()

    memory_btn.clicked.connect(show_memory_view)
    dock.setWidget(panel)
    dock.setMinimumWidth(320)
    window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

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
    from .chat import check_ollama, pick_chat_model_with_reason
    from .seats import load_seats, set_feature, set_seat

    seat_row = QtWidgets.QHBoxLayout()
    combos = {}
    for label, key in (("Brain", "brain"), ("Fast", "fast"), ("Vision", "vision")):
        seat_row.addWidget(QtWidgets.QLabel(label))
        combo = QtWidgets.QComboBox()
        combo.setObjectName(f"{key}_combo")
        combos[key] = combo
        seat_row.addWidget(combo)
    refresh_btn = QtWidgets.QPushButton("↻")
    refresh_btn.setObjectName("refresh_seats")
    refresh_btn.setToolTip("Re-read the models Ollama has installed")
    seat_row.addWidget(refresh_btn)
    layout.addLayout(seat_row)

    toggle_row = QtWidgets.QHBoxLayout()
    ai_toggle = QtWidgets.QCheckBox("AI")
    ai_toggle.setObjectName("ai_toggle")
    # T11: each toggle says what it covers — no guessing.
    ai_toggle.setToolTip(
        "Master switch — turns off every AI feature: chat, the "
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
    for box in (ai_toggle, chat_toggle, tidy_toggle, memory_toggle):
        toggle_row.addWidget(box)
    layout.addLayout(toggle_row)

    def apply_ai_state():
        """Header + which controls are live, from the seats file
        and whether a brain is answering. Never re-enables the
        send controls mid-turn."""
        seats = load_seats()
        ai_on = seats["ai_enabled"]
        chat_on = seats["features"]["chat"]
        if not ai_on:
            header.setText("AI is off — turn it on below to use chat, "
                           "tidy titles, and memory.")
        elif not chat_on:
            header.setText("Chat is off — turn it on below to talk "
                           "to the browser.")
        else:
            header.setText(brain_header_text())
        chat_ok = ai_on and chat_on and bool(session.model)
        busy = state["busy"]
        entry.setEnabled(chat_ok and not busy)
        send.setEnabled(chat_ok and not busy)
        summarize_btn.setEnabled(chat_ok and not busy)
        mic_btn.setEnabled(chat_ok and not busy
                           and not voice_state["transcribing"])
        run_card_btn.setEnabled(chat_ok and not busy
                                and cards_combo.count() > 0)
        ai_toggle.setChecked(ai_on)
        chat_toggle.setChecked(chat_on)
        tidy_toggle.setChecked(seats["features"]["tidy_titles"])
        memory_toggle.setChecked(seats["features"]["memory"])
        for box in (chat_toggle, tidy_toggle, memory_toggle):
            box.setEnabled(ai_on)

    def fill_seat_combos():
        # Each item carries the model name as its data; the label
        # shows the size (T8) so a 2 GB model reads differently
        # from an 8 GB one. Embed models are not offered as Brain
        # or Fast (T6); a saved pick that is gone shows as dead
        # (T5) instead of hiding behind "(auto)", and a saved
        # embed pick shows as an embed model. The Brain combo's
        # (auto) entry names what auto would pick (T12).
        _reachable, details = check_ollama()
        models = [d["name"] for d in details]
        sizes = {d["name"]: d["size"] for d in details}
        retrieval_state["models"] = models  # the header's Retrieval line (I1)
        seats = load_seats()
        auto_pick = None
        if models:
            auto_pick, _reason, _note = pick_chat_model_with_reason(
                models=models, sizes=sizes, ignore_saved=True)
        for key, combo in combos.items():
            combo.blockSignals(True)
            combo.clear()
            label = "(auto)"
            if key == "brain" and auto_pick:
                label = f"(auto: {auto_pick})"
            combo.addItem(label, None)
            for name in models:
                if key in ("brain", "fast") and "embed" in name.lower():
                    continue
                display = name
                if sizes.get(name):
                    display = f"{name} ({_human_size(sizes[name])})"
                combo.addItem(display, name)
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

    def on_seat_changed(key):
        def handler():
            picked = combos[key].currentData()
            set_seat(key, picked)
            if key == "brain":
                session.repick()
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
        from .cards import list_cards
        current = cards_combo.currentText()
        cards_combo.blockSignals(True)
        cards_combo.clear()
        cards_combo.addItems([c.name for c in list_cards() if not c.error])
        if current:
            cards_combo.setCurrentText(current)
        cards_combo.blockSignals(False)

    def on_refresh():
        # ↻ re-reads the models AND re-picks the brain, so a brain
        # that appeared since launch (Ollama just started) is picked
        # up — the header's own "then ↻" instruction works. Then the
        # answer-check proves the picked model answers (G7).
        fill_seat_combos()
        fill_cards_combo()
        session.repick()
        brain_health["checked"] = False
        brain_health["answers"] = None
        apply_ai_state()
        _check_brain_in_background()

    def on_brain_checked(answers, model):
        # Only the current model's result counts — a re-pick since
        # the check started makes it stale.
        if model != session.model:
            return
        brain_health["checked"] = True
        brain_health["answers"] = answers
        apply_ai_state()

    signals.brain_checked.connect(on_brain_checked)

    def on_ai_toggled(on):
        # T7: the kill-switch kills the CURRENT turn too, not just
        # the next one — flipping AI off mid-turn stops the brain.
        set_feature("ai", on)
        if not on:
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
    fill_seat_combos()
    fill_cards_combo()
    apply_ai_state()
    _check_brain_in_background()

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
        window._refreshing_views.discard(id(view))
        if view is not None and window.tabs.indexOf(view) >= 0:
            view.setHtml(html)

    window.compare_refresh_done.connect(on_compare_refresh)

    def on_research_done(report, sources, error):
        if error:
            append("Forge Browser", f"⚠ Research failed: {error}")
            progress_label.setText("Research failed.")
            return
        from .research import report_to_html
        # Q2: the report opens behind the current tab — a background
        # job's completion does not yank the person from their page.
        window.new_tab(html=report_to_html(report), background=True)
        append("Forge Browser",
               "Research done — the report opened in a new tab behind this one.")
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
        dialog = QtWidgets.QMessageBox(window)
        dialog.setWindowTitle("Forge Browser — approve this step?")
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
                   "Plan timed out after 5 minutes — treated as no.")
        elif (state["approved"] and always_box is not None
                and always_box.isChecked()):
            from .seats import set_always_allow
            set_always_allow(state["kind"], True)
        state["event"].set()
        # The turn resumes with the worker — the next on_step names
        # the real step; until then the line says thinking.
        turn["step"] = "thinking"

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
                "Plan timed out after 5 minutes — treated as no.")
        turn["waited"] = (turn.get("waited", 0.0)
                          + (time.monotonic() - turn["wait_started"]))
        turn["wait_started"] = None
        return state["approved"]

    stop_btn.clicked.connect(lambda: session.cancel())

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
            message = ("⚠ No transcriber is installed — install whisper "
                       "or set FORGE_TRANSCRIBE_CMD.")
        elif reason == "failed":
            message = ("⚠ Transcription failed — the transcriber "
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
            text += " — no audio yet"
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
                   "⚠ Nothing recorded — check the microphone.")
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
            signals.notice.emit(
                "Retrieval is off: nomic-embed-text is not installed — "
                "run install.sh to pull it")
        elif reason == "ollama-down":
            signals.notice.emit(
                "Ollama is not running — start Ollama to search long pages")
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
            signals.answer.emit(summary or "(no summary — the page text was empty)")
        except SummarizeStopped:
            # K1: a stopped summarize ends as "Stopped.", not a
            # failure.
            flush_tokens(display.flush())
            signals.answer.emit("Stopped.")
        except SummarizeEmptyError:
            flush_tokens(display.flush())
            signals.answer.emit("(no summary — the brain produced nothing)")
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
        entry.setEnabled(False)
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
        entry.setEnabled(False)
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
                       f"There is no tab {n} — summarizing the current tab.")
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
        entry.setEnabled(False)
        entry.clear()
        append("You", text)
        # J4: the current tab's text is always needed; the rest
        # only when the message names tabs or asks to compare.
        read_all = needs_all_tab_text(text)
        count = window.tabs.count()
        current = window.tabs.currentIndex()
        texts = [None] * count
        started = {"done": False}

        def start_work():
            if started["done"]:
                return
            started["done"] = True
            turn["step"] = "thinking"
            status_label.setText(
                turn_status_text("thinking", turn["started"]))
            tabs = []
            for i in range(count):
                view = window.tabs.widget(i)
                tabs.append({"title": view.title() if view is not None else "",
                             "url": view.url().toString() if view is not None else "",
                             "text": texts[i] or "",
                             "current": i == current,
                             "agent": (view is not None and
                                       id(view) in window._agent_tab_ids)})
            page_text = texts[current] if 0 <= current < count else None
            page_url = tabs[current]["url"] if 0 <= current < count else None
            threading.Thread(target=work, args=(text, page_text, tabs, page_url),
                             daemon=True).start()

        def collect(i, page_text):
            texts[i] = page_text or ""
            if all(t is not None for t in texts):
                start_work()

        if count == 0:
            start_work()
            return
        for i in range(count):
            view = window.tabs.widget(i)
            if view is not None and (i == current or read_all):
                view.page().runJavaScript(
                    "document.body ? document.body.innerText : ''",
                    lambda page_text, i=i: collect(i, page_text))
            else:
                collect(i, "")
        # A tab that never answers must not hang the send.
        QtCore.QTimer.singleShot(5000, start_work)

    send.clicked.connect(do_send)
    entry.returnPressed.connect(do_send)
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
    from .chat import list_models
    from .files import download_dir
    from .seats import set_feature, set_seat, load_seats
    from .settings import get_setting, set_setting
    from .media import OLLAMA_URL

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Forge Browser — Settings")
    dialog.setMinimumSize(480, 420)
    layout = QtWidgets.QVBoxLayout(dialog)
    models = list_models()
    seat_choices = ["(auto)"] + models
    seats = load_seats()

    def seat_row(label, key, object_name):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label))
        combo = QtWidgets.QComboBox()
        combo.setObjectName(object_name)
        combo.addItems(seat_choices)
        saved = seats.get(key)
        combo.setCurrentText(saved if saved in seat_choices else "(auto)")
        row.addWidget(combo, 1)
        layout.addLayout(row)
        return combo

    brain_combo = seat_row("Brain model:", "brain", "settings_brain")
    fast_combo = seat_row("Fast model:", "fast", "settings_fast")
    vision_combo = seat_row("Vision model:", "vision", "settings_vision")

    feature_boxes = {}
    for key, label in (("ai", "Use the AI (chat, tidy titles, brief)"),
                       ("chat", "Show the chat dock"),
                       ("tidy_titles", "Tidy up long tab titles"),
                       ("memory", "Remember facts and pages")):
        box = QtWidgets.QCheckBox(label)
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

    ollama_row = QtWidgets.QHBoxLayout()
    ollama_row.addWidget(QtWidgets.QLabel("Ollama address:"))
    ollama_entry = QtWidgets.QLineEdit()
    ollama_entry.setObjectName("settings_ollama_url")
    ollama_entry.setText(get_setting("ollama_url") or OLLAMA_URL)
    ollama_row.addWidget(ollama_entry, 1)
    layout.addLayout(ollama_row)

    searxng_row = QtWidgets.QHBoxLayout()
    searxng_row.addWidget(QtWidgets.QLabel("SearXNG address:"))
    searxng_entry = QtWidgets.QLineEdit()
    searxng_entry.setObjectName("settings_searxng_url")
    searxng_entry.setText(get_setting("searxng_url") or "")
    searxng_entry.setPlaceholderText("Leave empty for DuckDuckGo / Bing")
    searxng_row.addWidget(searxng_entry, 1)
    layout.addLayout(searxng_row)

    # L9: how long to rest on a link before its preview fetches.
    dwell_row = QtWidgets.QHBoxLayout()
    dwell_row.addWidget(QtWidgets.QLabel("Link preview dwell (ms):"))
    dwell_spin = QtWidgets.QSpinBox()
    dwell_spin.setObjectName("settings_preview_dwell_ms")
    dwell_spin.setRange(100, 5000)
    dwell_spin.setSingleStep(100)
    dwell_spin.setSuffix(" ms")
    try:
        dwell_spin.setValue(int(get_setting("preview_dwell_ms") or "600"))
    except ValueError:
        dwell_spin.setValue(600)
    dwell_row.addWidget(dwell_spin)
    dwell_row.addStretch(1)
    layout.addLayout(dwell_row)

    note = QtWidgets.QLabel(
        "The Ollama and SearXNG addresses take effect when the browser "
        "restarts.")
    note.setWordWrap(True)
    layout.addWidget(note)

    buttons = QtWidgets.QHBoxLayout()
    save_btn = QtWidgets.QPushButton("Save")
    cancel_btn = QtWidgets.QPushButton("Cancel")
    buttons.addStretch(1)
    buttons.addWidget(save_btn)
    buttons.addWidget(cancel_btn)
    layout.addLayout(buttons)

    def save():
        for key, combo in (("brain", brain_combo), ("fast", fast_combo),
                           ("vision", vision_combo)):
            text = combo.currentText()
            set_seat(key, None if text == "(auto)" else text)
        for key, box in feature_boxes.items():
            set_feature(key, box.isChecked())
        set_setting("download_dir", download_entry.text().strip())
        set_setting("ollama_url", ollama_entry.text().strip())
        set_setting("searxng_url", searxng_entry.text().strip())
        set_setting("preview_dwell_ms", str(dwell_spin.value()))
        if on_applied is not None:
            on_applied()
        dialog.accept()

    save_btn.clicked.connect(save)
    cancel_btn.clicked.connect(dialog.reject)
    dialog.exec()


def run_app(start_url=None):
    """Open the Forge Browser window. Blocks until it closes."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView, QWebEngineDownloadRequest) = _load_qt()
    from .cdp import default_profile_dir
    from .webui import LocalPageServer

    server = LocalPageServer().start()
    app = QtWidgets.QApplication([])
    app.setApplicationName("Forge Browser")
    app.setOrganizationName("Forge Browser")

    profile = QWebEngineProfile("forge-browser")
    profile.setPersistentStoragePath(
        os.path.join(default_profile_dir(), "app-profile"))
    profile.setCachePath(os.path.join(default_profile_dir(), "app-cache"))

    window = create_window(server, profile, start_url)
    window.show()
    try:
        return app.exec()
    finally:
        server.stop()
