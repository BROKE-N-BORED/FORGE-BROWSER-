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

import os
import threading

from .errors import BrowserError

_QT = None


def _load_qt():
    global _QT
    if _QT is not None:
        return _QT
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        raise BrowserError(
            "Forge Browser's window needs PySide6, which is not in "
            "this Python. Run install.sh — it builds the app venv — "
            "then start it with run.sh.") from exc
    _QT = (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
           QWebEngineView)
    return _QT


def create_window(server, profile, start_url=None):
    """Build the Forge Browser window around the given page server
    and WebEngine profile. Used by run_app and by smoke tests."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView) = _load_qt()
    from .webui import resolve_address

    class Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Forge Browser")
            self.resize(1240, 820)
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.tabCloseRequested.connect(self._close_tab)
            self.tabs.currentChanged.connect(lambda _: self._sync_address())
            self.setCentralWidget(self.tabs)

            bar = self.addToolBar("Navigation")
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
            self.statusBar().showMessage(
                "Forge Browser — no accounts, no API keys, nothing tracked.")
            self.new_tab(start_url or server.home_url)

        def _view(self):
            return self.tabs.currentWidget()

        def new_tab(self, url=None):
            view = QWebEngineView()
            view.setPage(QWebEnginePage(profile, view))
            view.urlChanged.connect(lambda *a: self._sync_address())
            view.titleChanged.connect(lambda t: self._set_tab_title(view, t))
            view.loadFinished.connect(lambda *a: self._sync_address())
            index = self.tabs.addTab(view, "New Tab")
            self.tabs.setCurrentIndex(index)
            view.load(QtCore.QUrl(url or server.home_url))
            return view

        def _set_tab_title(self, view, title):
            index = self.tabs.indexOf(view)
            if index >= 0:
                self.tabs.setTabText(index, (title or "Page")[:28])

        def _close_tab(self, index):
            if self.tabs.count() > 1:
                widget = self.tabs.widget(index)
                self.tabs.removeTab(index)
                widget.deleteLater()

        def _sync_address(self):
            view = self._view()
            if view is not None:
                url = view.url().toString()
                # Forge Browser's own local pages aren't addresses
                # the person typed — keep the bar clean on those.
                self.address.setText("" if url.startswith("http://127.0.0.1") else url)

        def _go(self):
            kind, target = resolve_address(self.address.text())
            if kind == "search":
                target = server.search_url(target)
            elif target == "/":
                target = server.home_url
            view = self._view()
            if view is not None:
                view.load(QtCore.QUrl(target))

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

    signals = Signals()
    model = pick_chat_model()
    session = ChatSession(model=model)

    dock = QtWidgets.QDockWidget("Chat — talk to your browser", window)
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
    row = QtWidgets.QHBoxLayout()
    entry = QtWidgets.QLineEdit()
    entry.setPlaceholderText("Ask for a page, a search, a summary…")
    send = QtWidgets.QPushButton("Send")
    summarize_btn = QtWidgets.QPushButton("Summarize")
    row.addWidget(entry)
    row.addWidget(send)
    row.addWidget(summarize_btn)
    layout.addLayout(row)
    dock.setWidget(panel)
    dock.setMinimumWidth(320)
    window.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
    if not model:
        entry.setEnabled(False)
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        return

    state = {"busy": False, "live": False}

    def append(who, text):
        log.append(f"<b>{who}:</b> {text}")

    def finish():
        state["busy"] = False
        send.setEnabled(True)
        summarize_btn.setEnabled(True)
        entry.setEnabled(True)
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
        if state["live"]:
            # The answer already streamed into the log line by line;
            # re-printing it would duplicate it.
            state["live"] = False
            finish()
        else:
            append("Forge Browser", text)
            finish()

    def on_failed(text):
        state["live"] = False
        append("Forge Browser", f"⚠ {text}")
        finish()

    signals.notice.connect(lambda text: log.append(f"<i>{text}</i>"))
    signals.open_url.connect(lambda url: window.new_tab(url))
    signals.token.connect(on_token)
    signals.answer.connect(on_answer)
    signals.failed.connect(on_failed)

    def work(text, page_text):
        display = LiveDisplay()

        def on_chunk(delta):
            shown = display.feed(delta)
            if shown:
                signals.token.emit(shown)

        try:
            answer, opens = session.ask(
                text, page_text=page_text,
                on_action=lambda kind, a: signals.notice.emit(f"working: {kind}…"),
                on_chunk=on_chunk)
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
            signals.notice.emit("working: summarizing…")
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
        send.setEnabled(False)
        summarize_btn.setEnabled(False)
        entry.setEnabled(False)
        entry.clear()
        append("You", text)
        view = window.tabs.currentWidget()

        def got_page(page_text):
            threading.Thread(target=work, args=(text, page_text or None),
                             daemon=True).start()

        if view is not None:
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''", got_page)
        else:
            got_page(None)

    send.clicked.connect(do_send)
    entry.returnPressed.connect(do_send)
    summarize_btn.clicked.connect(do_summarize)


def run_app(start_url=None):
    """Open the Forge Browser window. Blocks until it closes."""
    (QtCore, QtGui, QtWidgets, QWebEnginePage, QWebEngineProfile,
     QWebEngineView) = _load_qt()
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
