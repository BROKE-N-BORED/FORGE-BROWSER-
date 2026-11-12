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
import time

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
        tidy_done = QtCore.Signal(object, str, str)  # view, url, tidy title
        preview_ready = QtCore.Signal(str, str)  # url, preview text

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Forge Browser")
            self.resize(1240, 820)
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
