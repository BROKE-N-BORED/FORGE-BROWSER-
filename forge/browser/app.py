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

import json
import os
import threading
import time
import urllib.parse

from .errors import BrowserError
from .preview import DWELL_MS

_QT = None


def turn_status_text(step, started, now=None):
    """The dock's working line: 'working: search… — 12s'."""
    elapsed = int((time.monotonic() if now is None else now) - started)
    return f"working: {step}… — {elapsed}s"


def turn_done_text(outcome, started, now=None):
    """The dock's finished line: 'Done in 12s.' / 'Stopped after 12s.'
    / 'Failed after 12s.'"""
    elapsed = int((time.monotonic() if now is None else now) - started)
    if outcome == "stopped":
        return f"Stopped after {elapsed}s."
    if outcome == "failed":
        return f"Failed after {elapsed}s."
    return f"Done in {elapsed}s."


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

    class Window(QtWidgets.QMainWindow):
        tidy_done = QtCore.Signal(object, str, str)  # view, url, tidy title
        preview_ready = QtCore.Signal(str, str)  # url, preview text

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
            self._tidy_cache = {}  # url -> tidy title, so a revisit never re-asks
            self._agent_tab_ids = set()  # id(view) of tabs the brain opened
            self._preview_url = ""
            self._preview_popup = None
            self._preview_label = None
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
            self.statusBar().showMessage(
                "Forge Browser — no accounts, no API keys, nothing tracked.")
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

        def new_tab(self, url=None, agent=False, html=None):
            view = QWebEngineView()
            view.setPage(QWebEnginePage(profile, view))
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
            self.tabs.setCurrentIndex(index)
            if html is not None:
                view.setHtml(html)
            else:
                view.load(QtCore.QUrl(url or server.home_url))
            return view

        def _agent_mark(self, view):
            return "🤖 " if id(view) in self._agent_tab_ids else ""

        def _set_tab_title(self, view, title):
            index = self.tabs.indexOf(view)
            if index < 0:
                return
            if view is self._view():
                self._sync_window_title()
            self.tabs.setTabText(index, self._agent_mark(view) + (title or "Page")[:28])
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
                self.tabs.setTabText(index, self._agent_mark(view) + self._tidy_cache[url])
                return
            raw = (title or "").strip()
            if len(raw) <= 28 and len(raw.split()) <= 5:
                return
            threading.Thread(target=self._tidy_in_background,
                             args=(view, url, raw), daemon=True).start()

        def _tidy_session(self):
            """The fast seat for tidy titles, or the brain when no
            fast seat is picked (same model, no extra swap)."""
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

        def _tidy_in_background(self, view, url, raw):
            from .titles import tidy_title
            tidy = tidy_title(self._tidy_session(), raw, url)
            self._tidy_cache[url] = tidy
            self.tidy_done.emit(view, url, tidy)

        def _apply_tidy_title(self, view, url, tidy):
            index = self.tabs.indexOf(view)
            if index >= 0 and view.url().toString() == url:
                self.tabs.setTabText(index, self._agent_mark(view) + tidy)

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

        def _sync_address(self):
            view = self._view()
            if view is not None:
                url = view.url().toString()
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
                return
            if url == self._preview_url and self._preview_popup is not None \
                    and self._preview_popup.isVisible():
                return
            self._preview_url = url
            self._hide_link_preview()
            self._preview_timer.start(DWELL_MS)

        def _fetch_link_preview(self):
            url = self._preview_url
            if not url:
                return

            def work():
                from .preview import preview_url
                text = preview_url(url)
                self.preview_ready.emit(url, text)

            threading.Thread(target=work, daemon=True).start()

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
            self._preview_label.setText(text)
            self._preview_popup.adjustSize()
            self._preview_popup.move(
                QtGui.QCursor.pos() + QtCore.QPoint(14, 18))
            self._preview_popup.show()

        def _hide_link_preview(self):
            if self._preview_popup is not None:
                self._preview_popup.hide()

        def _go(self):
            kind, target = resolve_address(self.address.text())
            if kind == "search":
                target = server.search_url(target)
            elif target == "/":
                target = server.home_url
            view = self._view()
            if view is not None:
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
    from .chat import ChatSession, LiveDisplay, pick_chat_model

    class Signals(QtCore.QObject):
        notice = QtCore.Signal(str)
        open_url = QtCore.Signal(str)
        token = QtCore.Signal(str)
        answer = QtCore.Signal(str)
        failed = QtCore.Signal(str)
        step = QtCore.Signal(str)  # what the turn is doing right now
        voice_text = QtCore.Signal(str)
        plan = QtCore.Signal(str, object)  # plan text, {"event", "approved"}
        compare_table = QtCore.Signal(str)  # comparison table HTML
        progress = QtCore.Signal(str)  # research progress line
        research_done = QtCore.Signal(object, object, object)  # report, sources, error

    signals = Signals()
    model = pick_chat_model()
    session = ChatSession(model=model)
    window._chat_session = session  # tab titles tidy against this brain

    def _build_brief_in_background():
        # The morning brief builds once a day in the background; the
        # start page shows it on its next load. A failed brain call
        # is not cached, so the next launch retries.
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
    header = QtWidgets.QLabel(
        f"Brain: {model} (local, no key, no account)" if model else
        "Brain: none answering — start Ollama and reopen. Browsing still works.")
    header.setWordWrap(True)
    layout.addWidget(header)
    log = QtWidgets.QTextEdit()
    log.setReadOnly(True)
    layout.addWidget(log)
    # The working line: what this turn is doing, and how long it
    # has taken. It ticks every second while a turn is busy, and
    # says how long the turn took when it ends.
    status_label = QtWidgets.QLabel("")
    status_label.setObjectName("turn_status")
    status_label.setWordWrap(True)
    layout.addWidget(status_label)
    progress_label = QtWidgets.QLabel("")
    progress_label.setObjectName("research_progress")
    progress_label.setWordWrap(True)
    layout.addWidget(progress_label)
    row = QtWidgets.QHBoxLayout()
    entry = QtWidgets.QLineEdit()
    entry.setPlaceholderText("Ask for a page, a search, a summary…")
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
    dock.setWidget(panel)
    dock.setMinimumWidth(320)
    window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    state = {"busy": False, "live": False}
    turn = {"step": "thinking", "started": None}

    def on_step(kind):
        # The worker names the step; the label shows it at once,
        # with the seconds so far.
        turn["step"] = kind
        if state["busy"] and turn["started"] is not None:
            status_label.setText(turn_status_text(kind, turn["started"]))

    signals.step.connect(on_step)

    def tick_turn_clock():
        # Once a second, while a turn is busy, refresh the elapsed
        # time on the working line.
        if state["busy"] and turn["started"] is not None:
            status_label.setText(
                turn_status_text(turn["step"], turn["started"]))

    turn_timer = QtCore.QTimer(window)
    turn_timer.setInterval(1000)
    turn_timer.timeout.connect(tick_turn_clock)
    turn_timer.start()

    # --- Model picker + AI controls (task #14) -----------------
    # Three seat dropdowns fed by `ollama list`, plus Firefox-style
    # toggles: one master AI kill-switch and one per feature. Picks
    # live in ~/.forge/seats.json and apply at once.
    from .chat import list_models as _list_models
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
    chat_toggle = QtWidgets.QCheckBox("Chat")
    chat_toggle.setObjectName("chat_toggle")
    tidy_toggle = QtWidgets.QCheckBox("Tidy titles")
    tidy_toggle.setObjectName("tidy_toggle")
    memory_toggle = QtWidgets.QCheckBox("Memory")
    memory_toggle.setObjectName("memory_toggle")
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
        elif not session.model:
            header.setText("Brain: none answering — start Ollama, "
                           "then ↻. Browsing still works.")
        else:
            header.setText(f"Brain: {session.model} (local, no key, no account)")
        chat_ok = ai_on and chat_on and bool(session.model)
        busy = state["busy"]
        entry.setEnabled(chat_ok and not busy)
        send.setEnabled(chat_ok and not busy)
        summarize_btn.setEnabled(chat_ok and not busy)
        mic_btn.setEnabled(chat_ok and not busy)
        ai_toggle.setChecked(ai_on)
        chat_toggle.setChecked(chat_on)
        tidy_toggle.setChecked(seats["features"]["tidy_titles"])
        memory_toggle.setChecked(seats["features"]["memory"])
        for box in (chat_toggle, tidy_toggle, memory_toggle):
            box.setEnabled(ai_on)

    def fill_seat_combos():
        models = _list_models()
        seats = load_seats()
        for key, combo in combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(auto)")
            combo.addItems(models)
            pick = seats.get(key)
            combo.setCurrentText(pick if pick in models else "(auto)")
            combo.blockSignals(False)

    def on_seat_changed(key):
        def handler():
            text = combos[key].currentText()
            set_seat(key, None if text == "(auto)" else text)
            if key == "brain":
                from .chat import pick_chat_model
                session.model = pick_chat_model()
                apply_ai_state()
            elif key == "fast":
                window._fast_session = None
        return handler

    for key in combos:
        combos[key].currentTextChanged.connect(on_seat_changed(key))
    refresh_btn.clicked.connect(lambda: (fill_seat_combos(), apply_ai_state()))
    ai_toggle.toggled.connect(lambda on: (set_feature("ai", on), apply_ai_state()))
    chat_toggle.toggled.connect(lambda on: (set_feature("chat", on), apply_ai_state()))
    tidy_toggle.toggled.connect(
        lambda on: (set_feature("tidy_titles", on), apply_ai_state()))
    memory_toggle.toggled.connect(
        lambda on: (set_feature("memory", on), apply_ai_state()))
    fill_seat_combos()
    apply_ai_state()

    def append(who, text):
        log.append(f"<b>{who}:</b> {text}")

    def finish(outcome="done"):
        state["busy"] = False
        stop_btn.setEnabled(False)
        if turn["started"] is not None:
            status_label.setText(turn_done_text(outcome, turn["started"]))
            turn["started"] = None
        apply_ai_state()
        entry.setFocus()

    def on_token(text):
        # Streamed words appear as they are generated. The first
        # token of a turn opens the "Forge Browser:" paragraph;
        # later tokens keep flowing onto the same line.
        if not text:
            return
        if not state["live"]:
            state["live"] = True
            log.append("<b>Forge Browser:</b> ")
        log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        log.insertPlainText(text)

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
        state["live"] = False
        append("Forge Browser", f"⚠ {text}")
        finish("failed")

    signals.notice.connect(lambda text: log.append(f"<i>{text}</i>"))
    signals.open_url.connect(lambda url: window.new_tab(url, agent=True))
    signals.compare_table.connect(lambda html: window.new_tab(html=html))
    signals.progress.connect(progress_label.setText)

    def on_research_done(report, sources, error):
        if error:
            append("Forge Browser", f"⚠ Research failed: {error}")
            progress_label.setText("Research failed.")
            return
        from .research import report_to_html
        window.new_tab(html=report_to_html(report))
        append("Forge Browser", "Research done — the report opened in a new tab.")
        progress_label.setText("Research done.")

    signals.research_done.connect(on_research_done)
    signals.token.connect(on_token)
    signals.answer.connect(on_answer)
    signals.failed.connect(on_failed)

    def on_plan(plan_text, state):
        # Runs on the GUI thread; the worker waits on state["event"].
        # Nothing runs until the person says yes.
        append("Forge Browser", f"Plan: {plan_text}")
        answer = QtWidgets.QMessageBox.question(
            window, "Forge Browser — approve this step?",
            f"The browser wants to:\n\n{plan_text}\n\nAllow it?",
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No)
        state["approved"] = (
            answer == QtWidgets.QMessageBox.StandardButton.Yes)
        state["event"].set()

    signals.plan.connect(on_plan)

    def ask_plan(plan_text):
        # Called from the worker thread; blocks it until the person
        # answers the dialog on the GUI thread.
        state = {"event": threading.Event(), "approved": False}
        signals.plan.emit(plan_text, state)
        state["event"].wait()
        return state["approved"]

    stop_btn.clicked.connect(lambda: session.cancel())

    def on_voice_text(text):
        # Transcribed speech lands in the entry for review — the
        # person reads it and hits Send; it is never sent itself.
        if not text:
            append("Forge Browser",
                   "⚠ Couldn't make out anything in the recording.")
            return
        current = entry.text()
        entry.setText((current + " " + text).strip() if current else text)
        entry.setFocus()

    signals.voice_text.connect(on_voice_text)

    voice_state = {"recorder": None}

    def on_mic_pressed():
        from .voice import Recorder, find_recorder
        if voice_state["recorder"] is not None:
            return
        if find_recorder() is None:
            append("Forge Browser",
                   "⚠ No microphone recorder found (arecord, parecord, or "
                   "ffmpeg). Voice input needs one of those installed.")
            return
        recorder = Recorder()
        if not recorder.start():
            append("Forge Browser", "⚠ Couldn't start the microphone.")
            return
        voice_state["recorder"] = recorder
        mic_btn.setText("● Listening…")

    def on_mic_released():
        recorder = voice_state["recorder"]
        voice_state["recorder"] = None
        mic_btn.setText("🎤 Hold to talk")
        if recorder is None:
            return
        path = recorder.stop()
        if path is None:
            append("Forge Browser",
                   "⚠ Nothing recorded — check the microphone.")
            return
        signals.notice.emit("transcribing…")

        def work_voice():
            from .voice import transcribe_recording
            text = transcribe_recording(path)
            try:
                os.unlink(path)
            except OSError:
                pass
            signals.voice_text.emit(text)

        threading.Thread(target=work_voice, daemon=True).start()

    def work(text, page_text, tabs=None, page_url=None):
        display = LiveDisplay()

        def on_chunk(delta):
            shown = display.feed(delta)
            if shown:
                signals.token.emit(shown)

        try:
            answer, opens = session.ask(
                text, page_text=page_text, tabs=tabs, page_url=page_url,
                on_step=lambda kind: signals.step.emit(kind),
                on_chunk=on_chunk, on_plan=ask_plan,
                on_table=lambda html: signals.compare_table.emit(html),
                on_progress=lambda text: signals.progress.emit(text),
                on_research_done=lambda report, sources, error=None:
                    signals.research_done.emit(report, sources, error))
            for url in opens:
                signals.open_url.emit(url)
            rest = display.flush()
            if rest:
                signals.token.emit(rest)
            signals.answer.emit(answer or "(no answer)")
        except Exception as exc:
            signals.failed.emit(str(exc))

    def work_summary(page_text):
        display = LiveDisplay()

        def on_chunk(delta):
            shown = display.feed(delta)
            if shown:
                signals.token.emit(shown)

        try:
            from .summarize import summarize_page
            summary = summarize_page(session, page_text, on_chunk=on_chunk)
            rest = display.flush()
            if rest:
                signals.token.emit(rest)
            signals.answer.emit(summary or "(no summary — the page text was empty)")
        except Exception as exc:
            signals.failed.emit(str(exc))

    def do_summarize():
        if state["busy"]:
            return
        state["busy"] = True
        state["live"] = False
        turn["step"] = "summarizing"
        turn["started"] = time.monotonic()
        status_label.setText(turn_status_text("summarizing", turn["started"]))
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        entry.setEnabled(False)
        view = window.tabs.currentWidget()

        def got_page(page_text):
            threading.Thread(target=work_summary, args=(page_text or None,),
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
        turn["step"] = "thinking"
        turn["started"] = time.monotonic()
        status_label.setText(turn_status_text("thinking", turn["started"]))
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        entry.setEnabled(False)
        entry.clear()
        append("You", text)
        # Read every tab's text, so @tabN can name any of them.
        count = window.tabs.count()
        current = window.tabs.currentIndex()
        texts = [None] * count
        started = {"done": False}

        def start_work():
            if started["done"]:
                return
            started["done"] = True
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
            if view is not None:
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
    mic_btn.pressed.connect(on_mic_pressed)
    mic_btn.released.connect(on_mic_released)
    # The settings window calls this after a save, so the chat dock
    # picks the new seats and toggles without a restart.
    window._refresh_ai_controls = lambda: (fill_seat_combos(),
                                           apply_ai_state())


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
