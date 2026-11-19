"""Tests for the Review 5 build (v0.14): R5-01..R5-07.

R5-01  Summarize (and every other answer path) never writes a raw
       ACTION protocol line into the chat log.
R5-02  The How to Use dialog has a Close button.
R5-03  How to Use shows plain user help, not developer docs.
R5-04  Navigation toolbar tooltips are words, not glyphs.
R5-05  Saying no at the plan gate leaves a line in the log and
       ends the turn as stopped, not done.
R5-06  Embedding models stay out of the Vision model picker.
R5-07  The vault entry actions refuse plainly while the vault
       is locked instead of crashing.

The pure tests run under plain python (no PySide6). The Qt tests
run under the offscreen platform when PySide6 is present and
skip otherwise; they share one window on a temp HOME, following
the test_browser8 / test_browser9 pattern.
"""

import hashlib
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from forge.browser import app as appmod
from forge.browser import chat as chatmod
from forge.browser import summarize as summarizemod
from forge.browser import userhelp
from forge.browser.summarize import SummarizeEmptyError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-software-rasterizer "
    "--disable-gpu-compositing")
os.environ.setdefault("QT_OPENGL", "software")

try:
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtWebEngineCore import QWebEngineProfile
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

if HAVE_QT:
    from forge.browser import seats as seatsmod
    from forge.browser import webui as webuimod


# ------------------------------------------------------------------
# R5-01: the summarize path filters protocol lines
# ------------------------------------------------------------------

class _FakeBrain:
    """A chat session stand-in whose brain answers from a script."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._stop_requested = False
        self.last_speed = None
        self.calls = 0

    def _chat(self, messages, on_chunk=None):
        self.calls += 1
        if self._replies:
            return self._replies.pop(0)
        return ""


class SummarizeProtocolTests(unittest.TestCase):
    PAGE = ("Gears turn and wheels roll. The old mill by the "
            "river grinds corn for the village every morning.")

    def _summarize(self, brain):
        with mock.patch.object(summarizemod, "_load_cache",
                               return_value={}), \
                mock.patch.object(summarizemod, "_save_cache",
                                  lambda cache: None):
            return summarizemod.summarize_page(brain, self.PAGE,
                                               url="http://x.test/")

    def test_protocol_only_reply_counts_as_no_summary(self):
        # The R5-01 bug: the brain answered the summarize prompt
        # with a raw ACTION line and it was shown (and saved) as
        # the summary. Now it counts as no output at all.
        brain = _FakeBrain(['ACTION {"type": "summarize"}'])
        with self.assertRaises(SummarizeEmptyError):
            self._summarize(brain)

    def test_protocol_line_is_stripped_from_a_real_summary(self):
        brain = _FakeBrain([
            'ACTION {"type": "summarize"}\n'
            "The page describes the old mill by the river."])
        result = self._summarize(brain)
        self.assertNotIn("ACTION", result)
        self.assertIn("old mill by the river", result)

    def test_plain_summary_passes_through(self):
        brain = _FakeBrain(["The mill grinds corn every morning."])
        result = self._summarize(brain)
        self.assertEqual(result, "The mill grinds corn every morning.")

    def test_cache_key_retires_pre_fix_entries(self):
        # Entries cached before the filter can hold a raw ACTION
        # line as the "summary"; the salted key must not collide
        # with the old formula.
        legacy = hashlib.sha256(
            ("http://x.test/" + "\n" + "standard" + "\n" + self.PAGE
             ).encode("utf-8")).hexdigest()
        self.assertNotEqual(
            summarizemod._cache_key("http://x.test/", self.PAGE),
            legacy)


# ------------------------------------------------------------------
# R5-05: the denial wording follows the action that was denied
# ------------------------------------------------------------------

class PlanDenialWordsTests(unittest.TestCase):
    def test_search_denial_reads_like_the_review_line(self):
        self.assertEqual(appmod.plan_denial_words("search"), "search")

    def test_other_kinds_name_their_action(self):
        self.assertEqual(appmod.plan_denial_words("open"),
                         "open that page")
        self.assertEqual(appmod.plan_denial_words("research"),
                         "start that research")
        self.assertEqual(appmod.plan_denial_words("read_page"),
                         "read the page")
        self.assertEqual(appmod.plan_denial_words("summarize"),
                         "summarize the page")
        self.assertEqual(appmod.plan_denial_words("card"),
                         "run that card")

    def test_unknown_kind_falls_back_plainly(self):
        self.assertEqual(appmod.plan_denial_words("teleport"),
                         "do that")
        self.assertEqual(appmod.plan_denial_words(None), "do that")


# ------------------------------------------------------------------
# R5-03: the user help covers the required subjects, cleanly
# ------------------------------------------------------------------

class UserHelpTests(unittest.TestCase):
    def setUp(self):
        self.topics = userhelp.user_help_topics()
        self.all_text = "\n".join(
            title + "\n" + body for title, body in self.topics)

    def test_topics_cover_the_required_subjects(self):
        text = self.all_text.lower()
        for needle in ("tab", "address bar", "search", "chat",
                       "download", "bookmark", "vault", "doorway",
                       "fyrn", "update", ".forge folder"):
            self.assertIn(needle, text, needle)

    def test_no_developer_documentation_leaks_in(self):
        for needle in ("#65", "Developer topics", "BROWSER.md",
                       "forge/browser", "audit"):
            self.assertNotIn(needle, self.all_text, needle)

    def test_no_em_dashes(self):
        self.assertNotIn("\u2014", self.all_text)

    def test_every_topic_has_a_title_and_a_body(self):
        self.assertGreaterEqual(len(self.topics), 8)
        for title, body in self.topics:
            self.assertTrue(title.strip())
            self.assertTrue(body.strip())


# ------------------------------------------------------------------
# Qt drive helpers (the test_browser8 pattern)
# ------------------------------------------------------------------

if HAVE_QT:

    class _DialogDriver:
        """Runs steps against modal dialogs from a timer, the way
        test_browser8 drives them: a step returns True when it is
        done; errors are recorded, never raised into the dialog."""

        def __init__(self, steps, timeout=20.0):
            self.steps = list(steps)
            self.index = 0
            self.errors = []
            self.deadline = time.time() + timeout
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._tick)

        def start(self):
            self.timer.start(50)

        def stop(self):
            self.timer.stop()

        def _tick(self):
            if self.index >= len(self.steps):
                # Steps are done: the dialog must be gone too. If
                # it is still up, the opener would block forever —
                # reject it and record the failure instead.
                widget = QtWidgets.QApplication.activeModalWidget()
                if widget is not None:
                    self.errors.append(
                        "dialog still open after the last step")
                    if hasattr(widget, "reject"):
                        widget.reject()
                    else:
                        widget.close()
                self.timer.stop()
                return
            if time.time() > self.deadline:
                self.errors.append(
                    "timed out at step %d of %d"
                    % (self.index, len(self.steps)))
                widget = QtWidgets.QApplication.activeModalWidget()
                if widget is not None:
                    if hasattr(widget, "reject"):
                        widget.reject()
                    else:
                        widget.close()
                self.timer.stop()
                return
            try:
                if self.steps[self.index]():
                    self.index += 1
            except Exception as exc:  # noqa: BLE001 - recorded
                self.errors.append(
                    "step %d: %r" % (self.index, exc))
                self.index += 1

    def _modal():
        return QtWidgets.QApplication.activeModalWidget()

    def _button(parent, text):
        for button in parent.findChildren(QtWidgets.QPushButton):
            if button.text().replace("&", "") == text:
                return button
        return None

    def _named(parent, cls, name):
        for widget in parent.findChildren(cls):
            if widget.objectName() == name:
                return widget
        return None


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtReview5Tests(unittest.TestCase):
    window = None
    server = None
    app = None
    home = None
    _home_patch = None

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="forge-qt-home-r5-")
        cls._home_patch = mock.patch.dict(os.environ,
                                          {"HOME": cls.home})
        cls._home_patch.start()
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication([])
        cls.app = app
        cls.server = webuimod.LocalPageServer().start()
        profile = QWebEngineProfile("forge-qt-tests-r5")
        cls.window = appmod.create_window(cls.server, profile,
                                          cls.server.home_url)
        cls.window.resize(1200, 800)
        cls.window.show()
        cls._pump(1.0)

    @classmethod
    def tearDownClass(cls):
        if cls.window is not None:
            cls.window.close()
        if cls.server is not None:
            cls.server.stop()
        cls._home_patch.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def _pump(cls, seconds):
        end = time.time() + seconds
        while time.time() < end:
            cls.app.processEvents()
            time.sleep(0.05)

    @classmethod
    def _wait_for(cls, predicate, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            cls.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        cls.app.processEvents()
        return predicate()

    def _drive_modal(self, opener, steps, timeout=20.0):
        driver = _DialogDriver(steps, timeout=timeout)
        driver.start()
        try:
            opener()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertEqual(driver.index, len(driver.steps),
                         "not all dialog steps ran")

    # -- shared setup: a live session with a stub roster ---------

    ROSTER = [
        {"name": "qwen3.5:4b", "size": 4_000_000_000},
        {"name": "qwen3-vl:2b-instruct", "size": 2_000_000_000},
        {"name": "nomic-embed-text", "size": 300_000_000},
    ]

    def _refresh_with_roster(self):
        refresh = _named(self.window, QtWidgets.QPushButton,
                         "refresh_seats")
        self.assertIsNotNone(refresh)
        with mock.patch.object(chatmod, "check_ollama",
                               return_value=(True, self.ROSTER)):
            refresh.click()
            ok = self._wait_for(
                lambda: self.window._chat_session is not None
                and bool(self.window._chat_session.model))
        self.assertTrue(ok, "the brain pick never landed")

    def _combo_data(self, name):
        combo = _named(self.window, QtWidgets.QComboBox, name)
        self.assertIsNotNone(combo, name)
        return combo, [combo.itemData(i) for i in range(combo.count())]

    def _log_text(self):
        log = _named(self.window, QtWidgets.QTextEdit, "chat_log")
        if log is None:
            log = self.window.findChild(QtWidgets.QPlainTextEdit,
                                        "chat_log")
        self.assertIsNotNone(log)
        return log.toPlainText()

    # -- R5-04 ----------------------------------------------------

    def test_nav_toolbar_tooltips_are_words(self):
        toolbar = self.window.findChild(QtWidgets.QToolBar,
                                        "nav_toolbar")
        self.assertIsNotNone(toolbar)
        tips = {}
        for action in toolbar.actions():
            tips[action.text()] = action.toolTip()
        self.assertEqual(tips.get("←"), "Back")
        self.assertEqual(tips.get("→"), "Forward")
        self.assertEqual(tips.get("⟳"), "Reload")
        self.assertEqual(tips.get("⌂"), "Home")
        self.assertEqual(tips.get("＋ Tab"), "New tab")
        for text, tip in tips.items():
            if text:
                self.assertTrue(
                    tip and tip != text,
                    "glyph-only tooltip left on %r" % text)

    # -- R5-02 + R5-03 ---------------------------------------------

    def test_howto_dialog_user_help_and_close_button(self):
        seen = {}

        def read_dialog():
            dialog = _modal()
            if dialog is None:
                return False
            listing = _named(dialog, QtWidgets.QListWidget,
                             "howto_list")
            text = _named(dialog, QtWidgets.QTextBrowser,
                          "howto_text")
            if listing is None or text is None:
                return False
            seen["items"] = [listing.item(i).text()
                             for i in range(listing.count())]
            seen["first_body"] = text.toPlainText()
            seen["close"] = _named(dialog, QtWidgets.QPushButton,
                                   "howto_close")
            return True

        def click_close():
            if seen.get("close") is None:
                return False
            seen["close"].click()
            return True

        self._drive_modal(self.window._show_howto,
                          [read_dialog, click_close])
        items = seen["items"]
        # The user help comes first, in order.
        self.assertEqual(items[0], "Welcome to Forge Browser")
        self.assertIn("The doorway", items)
        self.assertIn("The vault", items)
        # The developer grouping is still there, below the help.
        self.assertIn("Developer topics", items)
        self.assertGreater(items.index("Developer topics"),
                           items.index("The doorway"))
        # The first topic's text is user help, not developer docs.
        self.assertIn("Forge Browser", seen["first_body"])
        self.assertNotIn("#65", seen["first_body"])
        self.assertNotIn("BROWSER.md", seen["first_body"])
        # R5-02: the Close button existed and closed the dialog
        # (the drive only completes if the exec returned).
        self.assertIsNotNone(seen["close"])

    # -- R5-06 ------------------------------------------------------

    def test_vision_combo_keeps_embedding_models_out(self):
        self._refresh_with_roster()
        _combo, vision = self._combo_data("vision_combo")
        self.assertIn("qwen3-vl:2b-instruct", vision)
        self.assertNotIn("nomic-embed-text", vision)
        _b, brain = self._combo_data("brain_combo")
        _f, fast = self._combo_data("fast_combo")
        self.assertNotIn("nomic-embed-text", brain)
        self.assertNotIn("nomic-embed-text", fast)

    def test_saved_embed_vision_pick_is_labelled_not_offered(self):
        seatsmod.set_seat("vision", "nomic-embed-text")
        try:
            self._refresh_with_roster()
            combo, vision = self._combo_data("vision_combo")
            hits = [i for i in range(combo.count())
                    if combo.itemData(i) == "nomic-embed-text"]
            self.assertEqual(len(hits), 1)
            self.assertIn("(embed model)",
                          combo.itemText(hits[0]))
        finally:
            seatsmod.set_seat("vision", None)
            self._refresh_with_roster()

    # -- R5-01 (the button itself) ----------------------------------

    def test_summarize_button_never_logs_a_protocol_line(self):
        self._refresh_with_roster()
        button = _button(self.window, "Summarize")
        self.assertIsNotNone(button)
        self.assertTrue(
            self._wait_for(lambda: button.isEnabled()),
            "Summarize never became enabled")
        # The page must hold real text before the click, so the
        # turn reaches the brain (an unloaded start page would
        # take the empty-page path and prove nothing).
        view = self.window.tabs.widget(
            self.window.tabs.currentIndex())
        holder = {"text": ""}

        def read_page_text():
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''",
                lambda text: holder.__setitem__("text", text or ""))
            return bool(holder["text"])

        self.assertTrue(
            self._wait_for(read_page_text, timeout=20.0),
            "the start page never produced text")
        # The brain is stubbed at the class level, not on one
        # session object: a brain pick landing mid-test can swap
        # the session object the dock holds, and the stub must
        # hold for whichever object runs the turn.
        stub = lambda self, messages, on_chunk=None: \
            'ACTION {"type": "summarize"}'  # noqa: E731
        with mock.patch.object(chatmod.ChatSession, "_chat", stub):
            button.click()
            done = self._wait_for(
                lambda: "the brain produced nothing"
                in self._log_text(),
                timeout=20.0)
        self.assertTrue(done, "the summarize turn never landed")
        log = self._log_text()
        self.assertIn("Summarizing the page", log)
        self.assertNotIn("ACTION {", log)

    # -- R5-05 (the gate itself) ------------------------------------

    def test_plan_denial_leaves_a_line_and_stops_the_turn(self):
        self._refresh_with_roster()
        replies = ['ACTION {"type": "search", "query": "gears"}',
                   "All right, I will not search."]

        def stub(self, messages, on_chunk=None):
            if replies:
                return replies.pop(0)
            return "All right, I will not search."

        entry = _named(self.window, QtWidgets.QPlainTextEdit,
                       "chat_entry")
        entry.setText("search for gears")

        def deny_plan():
            dialog = _modal()
            if dialog is None:
                return False
            no = _button(dialog, "No")
            if no is None:
                return False
            no.click()
            return True

        driver = _DialogDriver([deny_plan], timeout=30.0)
        driver.start()
        try:
            send = _button(self.window, "Send")
            self.assertTrue(
                self._wait_for(lambda: send.isEnabled()),
                "Send never became enabled")
            # Class-level stub, as in the summarize test: the
            # session object can be swapped by a late brain pick.
            with mock.patch.object(chatmod.ChatSession, "_chat",
                                   stub):
                send.click()
                status = _named(self.window, QtWidgets.QLabel,
                                "turn_status")
                stopped = self._wait_for(
                    lambda: status.text().startswith("Stopped after"),
                    timeout=30.0)
        finally:
            driver.stop()
            leftover = _modal()
            if leftover is not None:
                leftover.reject()
        self.assertEqual(driver.errors, [])
        self.assertEqual(driver.index, 1, "the plan gate never came up")
        self.assertTrue(stopped,
                        "the turn did not end as stopped: %r"
                        % status.text())
        self.assertIn("You said no, so I did not search.",
                      self._log_text())

    # -- R5-07 ------------------------------------------------------

    def _vault_dialog(self):
        dialog = _modal()
        if dialog is None:
            return None
        if _named(dialog, QtWidgets.QLabel, "vault_status") is None:
            return None
        return dialog

    def test_vault_actions_refuse_plainly_while_locked(self):
        window = self.window
        state = {}

        # Session 1: create a vault, open the add form, lock the
        # vault behind the form's back, then Save. The save must
        # refuse with the plain line, not crash.
        def create_vault():
            dialog = self._vault_dialog()
            if dialog is None:
                return False
            new = _named(dialog, QtWidgets.QLineEdit,
                         "vault_new_passphrase")
            confirm = _named(dialog, QtWidgets.QLineEdit,
                             "vault_confirm_passphrase")
            if new is None or confirm is None:
                return False
            new.setText("test-pass-123")
            confirm.setText("test-pass-123")
            _button(dialog, "Create vault").click()
            return True

        def open_form():
            dialog = self._vault_dialog()
            if dialog is None:
                return False
            add = _named(dialog, QtWidgets.QPushButton, "vault_add")
            if add is None or not add.isEnabled():
                return False
            # Deferred: do_add opens the form with exec(), and a
            # click delivered from inside this driver's own tick
            # would nest that exec inside the tick and starve the
            # timer. Through the event loop the form opens the
            # way it does for a real click.
            QtCore.QTimer.singleShot(0, add.click)
            return True

        def fill_and_lock():
            form = _modal()
            if form is None or form is self._vault_dialog():
                return False
            edits = form.findChildren(QtWidgets.QLineEdit)
            if len(edits) < 3:
                return False
            edits[0].setText("example.org")
            edits[1].setText("joshua")
            edits[2].setText("hunter2")
            state["form"] = form
            window._vault = None  # locked while the form is open
            return True

        def save_while_locked():
            save = _button(state["form"], "Save")
            if save is None:
                return False
            save.click()
            return True

        def check_save_refused():
            dialog = self._vault_dialog()
            if dialog is None or _modal() is not dialog:
                return False
            status = _named(dialog, QtWidgets.QLabel, "vault_status")
            state["save_status"] = status.text()
            return "The vault is locked." in status.text()

        def add_while_locked():
            dialog = self._vault_dialog()
            add = _named(dialog, QtWidgets.QPushButton, "vault_add")
            add.click()
            # No form may open: the vault dialog stays the modal.
            return _modal() is dialog

        def check_add_refused():
            dialog = self._vault_dialog()
            status = _named(dialog, QtWidgets.QLabel, "vault_status")
            state["add_status"] = status.text()
            return "adding a login" in status.text()

        def close_dialog():
            dialog = self._vault_dialog()
            if dialog is None:
                return False
            close = _button(dialog, "Close")
            if close is None:
                return False
            close.click()
            return True

        self._drive_modal(
            window._open_vault_window,
            [create_vault, open_form, fill_and_lock,
             save_while_locked, check_save_refused,
             add_while_locked, check_add_refused, close_dialog],
            timeout=30.0)
        self.assertIn("adding a login", state["save_status"])
        self.assertIn("adding a login", state["add_status"])

        # Session 2: unlock, add an entry for real, lock again,
        # then Delete. The delete must refuse plainly too, and
        # the entry must survive.
        def unlock():
            dialog = self._vault_dialog()
            if dialog is None:
                return False
            pw = _named(dialog, QtWidgets.QLineEdit,
                        "vault_passphrase")
            if pw is None:
                return False
            pw.setText("test-pass-123")
            _named(dialog, QtWidgets.QPushButton,
                   "vault_unlock").click()
            return True

        def open_form_again():
            dialog = self._vault_dialog()
            if dialog is None:
                return False
            add = _named(dialog, QtWidgets.QPushButton, "vault_add")
            if add is None or not add.isEnabled():
                return False
            QtCore.QTimer.singleShot(0, add.click)
            return True

        def fill_and_save():
            dialog = self._vault_dialog()
            form = _modal()
            if form is None or form is dialog:
                return False
            edits = form.findChildren(QtWidgets.QLineEdit)
            edits[0].setText("example.org")
            edits[1].setText("joshua")
            edits[2].setText("hunter2")
            _button(form, "Save").click()
            return True

        def lock_and_delete():
            dialog = self._vault_dialog()
            if dialog is None or _modal() is not dialog:
                return False
            listing = _named(dialog, QtWidgets.QListWidget,
                             "vault_list")
            if listing is None or listing.count() != 1:
                return False
            listing.setCurrentRow(0)
            window._vault = None  # locked behind the dialog's back
            delete = _named(dialog, QtWidgets.QPushButton,
                            "vault_delete")
            # Deferred like the Add clicks: if the guard ever
            # regressed, the confirm dialog would open through
            # the event loop and the next step would see it.
            QtCore.QTimer.singleShot(0, delete.click)
            return True

        def check_no_confirm():
            dialog = self._vault_dialog()
            # No confirm dialog may appear over the refusal.
            return dialog is not None and _modal() is dialog

        def check_delete_refused():
            dialog = self._vault_dialog()
            status = _named(dialog, QtWidgets.QLabel, "vault_status")
            listing = _named(dialog, QtWidgets.QListWidget,
                             "vault_list")
            state["delete_status"] = status.text()
            return ("changing logins" in status.text()
                    and listing.count() == 1)

        self._drive_modal(
            window._open_vault_window,
            [unlock, open_form_again, fill_and_save,
             lock_and_delete, check_no_confirm,
             check_delete_refused, close_dialog],
            timeout=30.0)
        self.assertIn("changing logins", state["delete_status"])


if __name__ == "__main__":
    unittest.main()
