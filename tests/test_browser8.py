"""Tests for the v0.11 build: the 2026-11-16 user review list (R3-01..33).

The pure tests run under plain python (no PySide6). The Qt tests at
the bottom run under the offscreen platform when PySide6 is present
and skip otherwise — they share one window so the suite stays fast.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from forge.browser import app as appmod
from forge.browser import cards as cardsmod
from forge.browser import chat as chatmod
from forge.browser import debug_log as debuglog
from forge.browser import howto as howtomod
from forge.browser import summarize as summarizemod
from forge.browser import webui as webuimod

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
# The offscreen platform has no EGL display: without these flags
# QtWebEngine's GPU process aborts the test process on teardown
# (SIGABRT after a green run). Software rendering keeps the exit
# code honest; no test depends on the GPU.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-software-rasterizer "
    "--disable-gpu-compositing")
os.environ.setdefault("QT_OPENGL", "software")

try:
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


def _tree_path(name):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), name)


# ------------------------------------------------------------------
# R3-07a: the first brain read gets a long allowance; later reads
# keep the short stall timeout.
# ------------------------------------------------------------------

class FirstReadTimeoutTests(unittest.TestCase):
    def test_first_read_is_longer_than_the_old_ten_seconds(self):
        self.assertGreater(chatmod.FIRST_READ_SECONDS, 10)

    def test_later_reads_keep_the_short_timeout(self):
        self.assertEqual(chatmod.READ_TIMEOUT_SECONDS, 10)

    def test_tighten_sets_the_socket_timeout(self):
        class FakeSock:
            def __init__(self):
                self.timeout = None

            def settimeout(self, seconds):
                self.timeout = seconds

        class FakeRaw:
            def __init__(self, sock):
                self._sock = sock

        class FakeResponse:
            def __init__(self, sock):
                self.raw = FakeRaw(sock)

        sock = FakeSock()
        self.assertTrue(chatmod._tighten_response_timeout(
            FakeResponse(sock), 10))
        self.assertEqual(sock.timeout, 10)

    def test_tighten_misses_gracefully_without_a_socket(self):
        class NoSocket:
            raw = None
            fp = None

        self.assertFalse(chatmod._tighten_response_timeout(
            NoSocket(), 10))

    def test_open_request_uses_the_first_read_allowance(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            raise RuntimeError("no network in this test")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError):
                session._open_request(mock.Mock())
        self.assertEqual(seen["timeout"], chatmod.FIRST_READ_SECONDS)


# ------------------------------------------------------------------
# R3-07b: a non-streaming call is capped, and Stop interrupts a
# stalled read instead of waiting for the call to return.
# ------------------------------------------------------------------

class InterruptibleCallTests(unittest.TestCase):
    def test_non_stream_cap_is_shorter_than_the_old_four_minutes(self):
        self.assertLess(chatmod.NON_STREAM_CAP_SECONDS, 240)

    def test_run_interruptible_returns_the_result(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        self.assertEqual(
            session._run_interruptible(lambda: 42, 5, "too slow"), 42)

    def test_run_interruptible_reraises_the_call_error(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        with self.assertRaises(ValueError):
            session._run_interruptible(
                lambda: (_ for _ in ()).throw(ValueError("boom")),
                5, "too slow")

    def test_stop_interrupts_a_blocked_call(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(30)
            return "never"

        def stop_soon():
            started.wait(timeout=5)
            time.sleep(0.3)
            session.cancel()

        threading.Thread(target=stop_soon, daemon=True).start()
        begun = time.monotonic()
        with self.assertRaises(chatmod.ChatStopped):
            session._run_interruptible(slow, 30, "too slow")
        self.assertLess(time.monotonic() - begun, 10)

    def test_cap_raises_a_plain_timeout(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        with self.assertRaises(chatmod.BrowserError) as caught:
            session._run_interruptible(lambda: time.sleep(30), 0.5,
                                       "the brain did not answer")
        self.assertIn("did not answer", str(caught.exception))

    def test_whole_reply_cap_counts_from_the_call_start(self):
        session = chatmod.ChatSession(model="qwen3:4b")
        started = time.monotonic() - chatmod.NON_STREAM_CAP_SECONDS - 1
        with self.assertRaises(chatmod.BrowserError):
            session._read_whole_reply(mock.Mock(), started)

    def test_summarize_stop_inside_a_call_is_stopped_not_failed(self):
        session = mock.Mock()
        session._stop_requested = False
        session._chat.side_effect = chatmod.ChatStopped("stopped")
        session.last_speed = None
        with mock.patch.object(summarizemod, "_load_cache",
                               return_value={}):
            with self.assertRaises(summarizemod.SummarizeStopped):
                summarizemod.summarize_page(session, "some page text")

    def test_card_stop_inside_a_call_is_stopped_not_failed(self):
        session = mock.Mock()
        session._stop_requested = False
        session._chat.side_effect = chatmod.ChatStopped("stopped")
        session.model = "qwen3:4b"
        session.last_speed = None
        card = cardsmod.Card(name="test-card", title="Test",
                             prompt="say hello", actions=[])
        with mock.patch.object(cardsmod, "load_card",
                               return_value=card):
            reply, _opens = cardsmod.run_card(session, "test-card")
        self.assertEqual(reply, "Stopped.")


# ------------------------------------------------------------------
# R3-06: a low-RAM re-pick says so (which model, and that RAM was
# the reason) instead of silently showing a different brain.
# ------------------------------------------------------------------

class LowRamRepickNoteTests(unittest.TestCase):
    def test_low_ram_repick_names_both_models_and_the_reason(self):
        details = [{"name": "qwen3:4b", "size": 2_600_000_000},
                   {"name": "qwen3:0.6b", "size": 500_000_000}]
        with mock.patch.object(chatmod, "check_ollama",
                               return_value=(True, details)), \
                mock.patch.object(chatmod, "free_ram_bytes",
                                  return_value=512 * 1024 ** 2), \
                mock.patch.object(chatmod, "brain_size_guide",
                                  return_value=int(4.5 * 1024 ** 3)), \
                mock.patch("forge.browser.seats.load_seats",
                           return_value={}), \
                mock.patch.dict(os.environ, {},
                                clear=True):
            model, _reason, note = \
                chatmod.pick_chat_model_with_reason(models=None)
        self.assertEqual(model, "qwen3:0.6b")
        self.assertIn("qwen3:0.6b", note)
        self.assertIn("qwen3:4b", note)
        self.assertIn("RAM", note)

    def test_no_note_when_ram_is_fine(self):
        details = [{"name": "qwen3:4b", "size": 2_600_000_000},
                   {"name": "qwen3:0.6b", "size": 500_000_000}]
        with mock.patch.object(chatmod, "check_ollama",
                               return_value=(True, details)), \
                mock.patch.object(chatmod, "free_ram_bytes",
                                  return_value=4 * 1024 ** 3), \
                mock.patch.object(chatmod, "brain_size_guide",
                                  return_value=int(4.5 * 1024 ** 3)), \
                mock.patch("forge.browser.seats.load_seats",
                           return_value={}), \
                mock.patch.dict(os.environ, {},
                                clear=True):
            model, _reason, note = \
                chatmod.pick_chat_model_with_reason(models=None)
        self.assertEqual(model, "qwen3:4b")
        self.assertEqual(note, "")


# ------------------------------------------------------------------
# R3-10a/b/c: the manual is U33-clean, How to Use splits user and
# developer topics, and the stale counts are gone.
# ------------------------------------------------------------------

class ManualCleanupTests(unittest.TestCase):
    def _manual(self):
        with open(_tree_path("BROWSER.md"), encoding="utf-8") as handle:
            return handle.read()

    def test_manual_has_no_em_dashes(self):
        self.assertNotIn("\u2014", self._manual())

    def test_manual_has_no_stale_22_improvements(self):
        self.assertNotIn("22 improvements", self._manual())

    def test_split_keeps_developer_sections_out_of_user_topics(self):
        sections = howtomod.load_sections(_tree_path("BROWSER.md"))
        user, developer = howtomod.split_sections(sections)
        user_titles = [title for _level, title, _body in user]
        dev_titles = [title for _level, title, _body in developer]
        self.assertIn("Verify (the #65 contract)", dev_titles)
        self.assertIn("Research (Item 3)", dev_titles)
        self.assertNotIn("Verify (the #65 contract)", user_titles)
        self.assertNotIn("Research (Item 3)", user_titles)

    def test_split_covers_every_section_exactly_once(self):
        sections = howtomod.load_sections(_tree_path("BROWSER.md"))
        user, developer = howtomod.split_sections(sections)
        self.assertEqual(len(user) + len(developer), len(sections))
        self.assertEqual(
            [entry[1] for entry in user + developer
             if entry[1] not in howtomod.DEVELOPER_SECTIONS]
            + [entry[1] for entry in developer],
            [entry[1] for entry in user]
            + [entry[1] for entry in developer])
        self.assertTrue(user)
        self.assertTrue(developer)

    def test_howto_page_groups_developer_topics_last(self):
        page = webuimod.howto_page()
        dev_at = page.find("Developer topics")
        self.assertGreater(dev_at, 0)
        # The developer sections come after the heading, not before it.
        self.assertGreater(page.find("Verify (the #65 contract)"), dev_at)
        self.assertGreater(page.find("Research (Item 3)"), dev_at)


# ------------------------------------------------------------------
# R3-19: the "part 1 of 1" line only speaks when it carries
# information (several parts); one part of one stays quiet.
# ------------------------------------------------------------------

class PartProgressTests(unittest.TestCase):
    def _run(self, text):
        session = mock.Mock()
        session._stop_requested = False
        session._chat.return_value = "summary text"
        session.last_speed = None
        heard = []
        with mock.patch.object(summarizemod, "_load_cache",
                               return_value={}):
            summarizemod.summarize_page(
                session, text, on_progress=heard.append)
        return heard

    def test_single_part_says_nothing(self):
        self.assertEqual(self._run("a short page"), [])

    def test_several_parts_still_report_progress(self):
        text = "word " * 5000
        heard = self._run(text)
        self.assertTrue(heard)
        self.assertTrue(all("part " in line for line in heard))


# ------------------------------------------------------------------
# R3-20: the start page search placeholder fits narrow windows.
# ------------------------------------------------------------------

class StartPagePlaceholderTests(unittest.TestCase):
    def test_placeholder_is_the_short_text(self):
        page = webuimod.start_page(token="test")
        self.assertIn("Search the web or type an address", page)
        self.assertNotIn("type an address up top", page)


# ------------------------------------------------------------------
# R3-21: the debug report's dependency probe knows PySide6 6.12
# split into sub-packages.
# ------------------------------------------------------------------

class DependencyProbeTests(unittest.TestCase):
    def test_pyside6_found_through_its_essentials_alias(self):
        import importlib.metadata as md

        def fake_version(name):
            if name == "pyside6_essentials":
                return "6.12.0"
            raise md.PackageNotFoundError(name)

        with mock.patch.object(debuglog.importlib_metadata, "version",
                               side_effect=fake_version):
            self.assertEqual(debuglog._package_version("PySide6"),
                             "6.12.0")

    def test_webengine_found_through_its_alias(self):
        import importlib.metadata as md

        def fake_version(name):
            if name == "pyside6_webengine":
                return "6.12.0"
            raise md.PackageNotFoundError(name)

        with mock.patch.object(debuglog.importlib_metadata, "version",
                               side_effect=fake_version):
            self.assertEqual(
                debuglog._package_version("PySide6-WebEngine"), "6.12.0")

    def test_truly_missing_package_still_reports_not_installed(self):
        import importlib.metadata as md

        def fake_version(name):
            raise md.PackageNotFoundError(name)

        with mock.patch.object(debuglog.importlib_metadata, "version",
                               side_effect=fake_version):
            self.assertEqual(
                debuglog._package_version("no-such-package-xyz"),
                "not installed")


# ------------------------------------------------------------------
# Qt tests — the window, driven offscreen. They share one window
# so the suite stays fast, like tests/test_browser6.py.
# ------------------------------------------------------------------

if HAVE_QT:
    class _DialogDriver:
        """Drive a modal dialog from a QTimer while exec() blocks.

        A step must never click a button that opens ANOTHER modal
        dialog: a QTimer does not fire again while its own timeout
        slot is on the stack, so the driver would stall forever
        inside the nested exec. Such a click goes through
        QtCore.QTimer.singleShot(0, btn.click) so the driver keeps
        ticking during the nested exec; the next step answers the
        nested box. Clicks that only CLOSE a box (Yes/No/OK/Close)
        are safe to do directly.
        """

        def __init__(self, steps, timeout=12.0):
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
                self.timer.stop()
                return
            if time.time() > self.deadline:
                self.errors.append(
                    "timed out at step %d of %d"
                    % (self.index, len(self.steps)))
                # Never leave a modal dialog open: the exec() call
                # would hang the suite. Reject whatever is up.
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
                self.errors.append("step %d: %r" % (self.index, exc))
                self.index += 1

    def _modal_dialog():
        return QtWidgets.QApplication.activeModalWidget()

    def _button(parent, text):
        for button in parent.findChildren(QtWidgets.QPushButton):
            if button.text().replace("&", "") == text:
                return button
        return None

    def _label_with(parent, needle):
        for label in parent.findChildren(QtWidgets.QLabel):
            if needle in label.text():
                return label
        return None

    def _checkbox_with(parent, text):
        for box in parent.findChildren(QtWidgets.QCheckBox):
            if box.text().replace("&", "") == text:
                return box
        return None

    def _close_step():
        """A driver step that closes whatever modal dialog is up —
        by clicking its Close (or Cancel) button when it has one.
        That is the path a person takes, and it is the only clean
        one: calling reject() from the driver timer crashes the
        interpreter on teardown for some dialogs (SIGBUS / heap
        corruption, intermittent — the memory dialog aborted 2
        of 3 probe runs that way, 0 of 3 via the button). The
        buttonless How to Use dialog still falls back to
        reject(), which is clean for it."""
        dialog = _modal_dialog()
        if dialog is None:
            return False
        button = _button(dialog, "Close")
        if button is None:
            button = _button(dialog, "Cancel")
        if button is not None:
            button.click()
        else:
            dialog.reject()
        return True


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtReviewTests(unittest.TestCase):
    window = None
    server = None
    app = None
    home = None
    _home_patch = None

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="forge-qt-home-r3-")
        cls._home_patch = mock.patch.dict(os.environ, {"HOME": cls.home})
        cls._home_patch.start()
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication([])
        cls.app = app
        cls.server = webuimod.LocalPageServer().start()
        profile = QWebEngineProfile("forge-qt-tests-r3")
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
    def _wait_for(cls, predicate, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            cls.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        cls.app.processEvents()
        return predicate()

    def setUp(self):
        window = self.window
        if window._downloads_dialog is not None:
            window._downloads_dialog.close()
            self._pump(0.2)
        window._downloads.clear()
        window._closed_tabs.clear()
        while window.tabs.count() > 1:
            window._close_tab(window.tabs.count() - 1)
        window._closed_tabs.clear()
        window.tabs.setCurrentIndex(0)
        window.resize(1200, 800)
        self._pump(0.2)

    def _drive_modal(self, opener, steps, timeout=12.0):
        """Run a modal dialog opener under a driver; assert no
        driver errors. The opener blocks in exec()."""
        driver = _DialogDriver(steps, timeout=timeout)
        driver.start()
        try:
            opener()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])

    def _menu_actions(self):
        """{(menu title, action text)} across the whole menu bar."""
        found = set()
        for menu in self.window.menuBar().findChildren(
                QtWidgets.QMenu):
            title = menu.title().replace("&", "")
            for action in menu.actions():
                text = action.text().replace("&", "")
                if text:
                    found.add((title, text))
        return found

    # -- R3-01a: the dock scrolls; the window fits 1366x768 --------

    def test_window_fits_720_tall(self):
        window = self.window
        window.resize(1240, 720)
        self._pump(0.5)
        self.assertEqual(window.size().height(), 720)
        self.assertLess(window.minimumSizeHint().height(), 768)
        scroll = window.findChild(QtWidgets.QScrollArea, "chat_scroll")
        self.assertIsNotNone(scroll, "chat dock has no scroll area")
        # At 720 tall the dock content overflows, so the bar shows.
        self.assertTrue(scroll.verticalScrollBar().isVisible())
        window.resize(1200, 800)
        self._pump(0.3)

    def test_dock_panel_stays_dark_with_scroll_area(self):
        # The scroll area's viewport used to paint the default
        # light palette colour behind the panel, turning the dock
        # cream and the text unreadable (R3-01a regression).
        window = self.window
        scroll = window.findChild(QtWidgets.QScrollArea, "chat_scroll")
        panel = scroll.widget()
        dark = QtGui.QColor("#14161a")
        self.assertEqual(
            panel.palette().color(QtGui.QPalette.ColorRole.Window),
            dark)
        # The viewport carries its dark background as a
        # stylesheet: its palette does not stick (Qt's repolish
        # resets it), so the palette is the wrong thing to read.
        self.assertIn("#14161a", scroll.viewport().styleSheet())

    def test_dock_minimum_covers_panel_plus_scrollbar(self):
        # At the narrowest dock the 2x2 button rows must fit: the
        # scroll area's minimum covers the panel's own minimum
        # plus the scrollbar width (verified visually too).
        window = self.window
        scroll = window.findChild(QtWidgets.QScrollArea, "chat_scroll")
        panel = scroll.widget()
        needed = (panel.minimumSizeHint().width()
                  + scroll.verticalScrollBar().sizeHint().width())
        self.assertGreaterEqual(scroll.minimumWidth(), needed)

    # -- R3-01b: seat descriptions are not clipped -----------------

    def test_seat_descriptions_not_clipped(self):
        window = self.window
        for key in ("brain", "fast", "vision"):
            desc = window.findChild(
                QtWidgets.QLabel, "%s_desc" % key)
            self.assertIsNotNone(desc, "%s description missing" % key)
            self.assertTrue(desc.wordWrap())
            self.assertEqual(
                desc.sizePolicy().verticalPolicy(),
                QtWidgets.QSizePolicy.Policy.Minimum)
            # heightForWidth at the label's real width is the
            # height the text needs — sizeHint's height is
            # computed at the hint width, not the laid-out one.
            self.assertGreaterEqual(
                desc.height(), desc.heightForWidth(desc.width()),
                "%s description is clipped: %d px tall, needs %d"
                % (key, desc.height(),
                   desc.heightForWidth(desc.width())))

    # -- R3-02: "Models and AI" is readable, checked or not --------

    def test_models_toggle_has_checked_stylesheet(self):
        # The button was white-on-white: the checked state had no
        # stylesheet rule. (Readability itself was verified by
        # screenshot; this pins the rules that make it so.)
        sheet = self.window.styleSheet()
        self.assertIn("QToolButton#models_toggle", sheet)
        self.assertIn("QToolButton#models_toggle:checked", sheet)
        self.assertIn("#23272f", sheet)
        self.assertIn("#2c313a", sheet)
        button = self.window.findChild(
            QtWidgets.QToolButton, "models_toggle")
        self.assertIsNotNone(button)
        self.assertTrue(button.isCheckable())

    # -- R3-23: the dock clamps to half the window ------------------

    def test_dock_clamps_to_half_window(self):
        window = self.window
        window.resize(640, 600)
        self._pump(0.5)
        window.resizeDocks([window._chat_dock], [500],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.5)
        self.assertLessEqual(window._chat_dock.width(), 320)
        window.resize(1200, 800)
        self._pump(0.3)

    # -- R3-27: the address bar stays empty on local pages ----------

    def test_address_bar_stays_empty_on_local_pages(self):
        window = self.window
        view = window._view()
        done = {"fired": False}

        def finished(_ok):
            done["fired"] = True

        view.loadFinished.connect(finished)
        try:
            view.load(QtCore.QUrl(self.server.home_url))
            self.assertTrue(self._wait_for(lambda: done["fired"],
                                           timeout=15.0))
        finally:
            view.loadFinished.disconnect(finished)
        self._pump(0.3)
        self.assertEqual(window.address.text(), "")

    def test_address_bar_decision_is_written_in_the_code(self):
        with open(_tree_path("forge/browser/app.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        # The decision, in the comment above _sync_address: the
        # bar stays empty on local pages by decision, because a
        # 127.0.0.1 address is neither typed nor shareable.
        self.assertIn(
            "the bar stays empty on Forge Browser's own", source)
        self.assertIn("local pages, by decision", source)
        self.assertIn("a 127.0.0.1 address is neither", source)

    # -- R3-15: seat combos stay short; auto names its pick --------

    def test_seat_combos_show_auto_short(self):
        window = self.window
        combos = {}
        for key in ("brain", "fast", "vision"):
            combo = window.findChild(
                QtWidgets.QComboBox, "%s_combo" % key)
            self.assertIsNotNone(combo, "%s combo missing" % key)
            combos[key] = combo
        # Wait for the seat fill (async, off the GUI thread).
        filled = self._wait_for(
            lambda: combos["brain"].count() > 1, timeout=20.0)
        for key, combo in combos.items():
            text = combo.currentText()
            self.assertNotIn("(auto:", text,
                             "%s combo shows the long auto label" % key)
            self.assertLessEqual(len(text), 12,
                                 "%s combo label is too long: %r"
                                 % (key, text))
        if filled:
            # The auto pick is named in the description line and
            # the tooltip instead of in the combo itself.
            brain = combos["brain"]
            if brain.currentText() == "(auto)":
                self.assertIn("Auto picks", brain.toolTip())
                desc = window.findChild(QtWidgets.QLabel, "brain_desc")
                self.assertIn("Auto picks", desc.text())


    # -- R3-03 + R3-25: a failed load keeps its URL and renders -----

    def _load_bad_url(self, url):
        """Open a tab on an unresolvable URL; return its body text
        once the error page has rendered."""
        window = self.window
        window.new_tab(url)
        view = window._view()
        seen = {}

        def read_body():
            view.page().runJavaScript(
                "document.body ? document.body.innerText : ''",
                lambda text: seen.__setitem__("text", text or ""))
            return ("Could not reach this page"
                    in seen.get("text", ""))

        self.assertTrue(
            self._wait_for(read_body, timeout=20.0),
            "error page never rendered for %s (last text: %r)"
            % (url, seen.get("text", "")[:200]))
        return seen["text"]

    def test_failed_load_keeps_url_in_address_bar(self):
        # R3-03: the bar used to show the error page's data: URL.
        self._load_bad_url("http://nonexistent.invalid/")
        self.assertEqual(self.window.address.text(),
                         "http://nonexistent.invalid/")

    def test_bad_url_error_page_renders(self):
        # R3-25: the review caught one blank capture, suspected
        # timing — the page renders its text; this guards it.
        text = self._load_bad_url(
            "http://this-domain-does-not-exist-xyz123.com/")
        self.assertIn("Could not reach this page", text)
        self.assertIn("Check the address", text)

    # -- R3-24: the bookmark refusal says why -----------------------

    def test_bookmark_refusal_names_the_reason(self):
        window = self.window
        # The first tab is the start page.
        window._bookmark_current()
        self.assertEqual(window.statusBar().currentMessage(),
                         "The start page can't be bookmarked.")
        # A local file is not a web page.
        path = os.path.join(self.home, "note.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("<html><body>note</body></html>")
        window.new_tab(QtCore.QUrl.fromLocalFile(path).toString())
        self._pump(1.0)
        window._bookmark_current()
        self.assertEqual(window.statusBar().currentMessage(),
                         "Only web pages can be bookmarked.")

    # -- R3-04 + R3-05: the brain health check is visible -----------

    def _wait_for_session(self, timeout=30.0):
        if self._wait_for(lambda: self.window._chat_session is not None,
                          timeout=timeout):
            return True
        self.skipTest("no chat session (Ollama did not answer)")

    def test_brain_header_shows_checking_while_check_runs(self):
        window = self.window
        self._wait_for_session()
        header = window._chat_dock.findChild(
            QtWidgets.QLabel, "brain_header")
        gate = threading.Event()
        result = {"value": True}

        def slow_check(*args, **kwargs):
            gate.wait(timeout=30)
            return result["value"]

        try:
            with mock.patch.object(chatmod, "check_model_answers",
                                   side_effect=slow_check):
                window._refresh_ai_controls()
                self.assertTrue(
                    self._wait_for(
                        lambda: "(checking\u2026)" in header.text(),
                        timeout=20.0),
                    "header never showed (checking…): %r"
                    % header.text())
                gate.set()
                self.assertTrue(
                    self._wait_for(
                        lambda: "(answering)" in header.text(),
                        timeout=20.0),
                    "header never showed (answering): %r"
                    % header.text())
        finally:
            gate.set()

    def test_retry_brain_button_appears_when_not_answering(self):
        window = self.window
        self._wait_for_session()
        header = window._chat_dock.findChild(
            QtWidgets.QLabel, "brain_header")
        retry = window._chat_dock.findChild(
            QtWidgets.QPushButton, "retry_brain_button")
        self.assertIsNotNone(retry)

        def never_answers(*args, **kwargs):
            return False

        with mock.patch.object(chatmod, "check_model_answers",
                               side_effect=never_answers):
            window._refresh_ai_controls()
            self.assertTrue(
                self._wait_for(
                    lambda: "(not answering)" in header.text(),
                    timeout=20.0),
                "header never showed (not answering): %r"
                % header.text())
            self.assertTrue(retry.isVisible(),
                            "no Retry brain button for (not answering)")
        # Leave the brain healthy for later tests.
        window._refresh_ai_controls()
        self._wait_for(lambda: "(answering)" in header.text(),
                       timeout=30.0)

    # -- R3-06: the header shows the low-RAM re-pick note -----------

    def test_brain_header_shows_pick_note(self):
        # The note itself is covered by the pure
        # LowRamRepickNoteTests; this pins that the header
        # actually displays it.
        with open(_tree_path("forge/browser/app.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("session.pick_note", source)
        self.assertIn("text += f\"\\n{session.pick_note}\"", source)

    # -- R3-09: Run card with nothing picked says so ----------------

    def test_run_card_with_nothing_picked_says_pick_first(self):
        window = self.window
        combo = window.findChild(QtWidgets.QComboBox, "cards_combo")
        self.assertIsNotNone(combo)
        # The first item is the empty one (select-nothing-default).
        self.assertEqual(combo.itemText(0), "")
        self.assertIsNone(combo.itemData(0))
        combo.setCurrentIndex(0)
        log = window.findChild(QtWidgets.QTextEdit, "chat_log")
        before = log.toPlainText()
        run = window.findChild(QtWidgets.QPushButton, "run_card_button")
        self.assertIsNotNone(run)
        run.click()
        self._pump(0.5)
        after = log.toPlainText()
        self.assertIn("Pick a card first.", after[len(before):])

    # -- R3-22: the Downloads button on the toolbar -----------------

    def test_downloads_toolbar_button_opens_downloads(self):
        window = self.window
        button = window.findChild(
            QtWidgets.QToolButton, "downloads_button")
        self.assertIsNotNone(button)
        self.assertEqual(button.text(), "\u2193 Downloads")
        button.click()
        self._pump(0.5)
        self.assertIsNotNone(window._downloads_dialog)
        self.assertTrue(window._downloads_dialog.isVisible())


    # -- R3-10b: How to Use splits user from developer topics ------

    def test_howto_groups_developer_topics(self):
        window = self.window
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            listing = dialog.findChild(
                QtWidgets.QListWidget, "howto_list")
            if listing is None:
                return False
            seen["items"] = [listing.item(i).text().strip()
                             for i in range(listing.count())]
            seen["heading"] = None
            for i in range(listing.count()):
                if listing.item(i).text().strip() == "Developer topics":
                    seen["heading"] = i
                    seen["heading_selectable"] = bool(
                        listing.item(i).flags()
                        & QtCore.Qt.ItemFlag.ItemIsSelectable)
            return True

        self._drive_modal(window._show_howto, [check, _close_step])
        items = seen.get("items", [])
        heading = seen.get("heading")
        self.assertIsNotNone(heading,
                             "no Developer topics heading: %r" % items)
        self.assertFalse(seen.get("heading_selectable"),
                         "the heading row must not be selectable")
        for internal in ("Verify (the #65 contract)",
                         "Research (Item 3)"):
            self.assertIn(internal, items,
                          "%s is gone entirely, not grouped" % internal)
            self.assertGreater(items.index(internal), heading,
                               "%s sits above the heading" % internal)

    # -- R3-13 + R3-16: the menus -----------------------------------

    def test_downloads_in_tools_menu_only(self):
        menus = self._menu_actions()
        downloads = [menu for menu, text in menus
                     if text == "Downloads"]
        self.assertEqual(downloads, ["Tools"],
                         "Downloads appears in: %r" % downloads)

    def test_edit_menu_has_clipboard_actions(self):
        menus = self._menu_actions()
        edit = {text for menu, text in menus if menu == "Edit"}
        for wanted in ("Cut", "Copy", "Paste", "Select All",
                       "Find in Page"):
            self.assertIn(wanted, edit,
                          "Edit menu is missing %s: %r" % (wanted, edit))

    # -- R3-08: the Downloads dialog buttons all fit ----------------

    def test_downloads_buttons_all_fit(self):
        window = self.window
        window._open_downloads_dialog()
        self._pump(0.5)
        try:
            dialog = window._downloads_dialog
            self.assertIsNotNone(dialog)
            rows = {}
            for text in ("Open", "Show in folder", "Retry", "Cancel",
                         "Open folder", "Close"):
                button = _button(dialog, text)
                self.assertIsNotNone(button, "%s button missing" % text)
                geo = button.geometry()
                self.assertLessEqual(
                    geo.right(), dialog.width(),
                    "%s runs off the dialog's right edge" % text)
                self.assertLessEqual(
                    geo.bottom(), dialog.height(),
                    "%s runs off the dialog's bottom edge" % text)
                rows[text] = geo.y()
            # Two rows: Open folder and Close sit below the first
            # four, not squeezed beside them.
            self.assertGreater(rows["Open folder"], rows["Open"])
            self.assertEqual(rows["Open folder"], rows["Close"])
        finally:
            if window._downloads_dialog is not None:
                window._downloads_dialog.close()
                self._pump(0.2)

    # -- R3-11: buttons stay disabled with no selection -------------

    def test_bookmarks_buttons_disabled_without_selection(self):
        from forge.browser import bookmarks as marksmod
        marksmod.add_bookmark("https://example.com/r3-11",
                              "R3-11 Bookmark")
        try:
            window = self.window
            seen = {}

            def check_disabled():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                for name in ("bookmarks_open", "bookmarks_rename",
                             "bookmarks_remove"):
                    button = dialog.findChild(QtWidgets.QPushButton,
                                              name)
                    seen[name] = (button is not None
                                  and button.isEnabled())
                return True

            def select_and_check():
                dialog = _modal_dialog()
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "bookmarks_list")
                listing.setCurrentRow(0)
                self._pump(0.2)
                for name in ("bookmarks_open", "bookmarks_rename",
                             "bookmarks_remove"):
                    button = dialog.findChild(QtWidgets.QPushButton,
                                              name)
                    seen[name + "_selected"] = button.isEnabled()
                return True

            self._drive_modal(window._open_bookmarks_dialog,
                              [check_disabled, select_and_check,
                               _close_step])
            for name in ("bookmarks_open", "bookmarks_rename",
                         "bookmarks_remove"):
                self.assertFalse(seen.get(name),
                                 "%s enabled with no selection" % name)
                self.assertTrue(seen.get(name + "_selected"),
                                "%s stays disabled after a pick" % name)
        finally:
            marksmod.remove_bookmark("https://example.com/r3-11")

    def test_history_button_disabled_without_selection(self):
        from forge.browser import titles as titlesmod
        titlesmod.record_visit("https://example.com/r3-11",
                               "R3-11 Visit")
        try:
            window = self.window
            seen = {}

            def check_disabled():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                button = dialog.findChild(QtWidgets.QPushButton,
                                          "history_open")
                seen["enabled"] = (button is not None
                                   and button.isEnabled())
                return True

            def select_and_check():
                dialog = _modal_dialog()
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "history_list")
                listing.setCurrentRow(0)
                self._pump(0.2)
                button = dialog.findChild(QtWidgets.QPushButton,
                                          "history_open")
                seen["enabled_selected"] = button.isEnabled()
                return True

            self._drive_modal(window._open_history_window,
                              [check_disabled, select_and_check,
                               _close_step])
            self.assertFalse(seen.get("enabled"),
                             "history Open enabled with no selection")
            self.assertTrue(seen.get("enabled_selected"),
                            "history Open stays disabled after a pick")
        finally:
            titlesmod.clear_history()

    def test_research_button_disabled_without_selection(self):
        from forge.browser import research as researchmod
        folder = researchmod.research_dir()
        os.makedirs(folder, exist_ok=True)
        report = os.path.join(folder, "r3-11-test.md")
        with open(report, "w", encoding="utf-8") as handle:
            handle.write("# Research: R3-11 test\n\nNothing here.\n")
        try:
            window = self.window
            seen = {}

            def check_disabled():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                button = dialog.findChild(QtWidgets.QPushButton,
                                          "research_open")
                seen["enabled"] = (button is not None
                                   and button.isEnabled())
                return True

            def select_and_check():
                dialog = _modal_dialog()
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "research_list")
                listing.setCurrentRow(0)
                self._pump(0.2)
                button = dialog.findChild(QtWidgets.QPushButton,
                                          "research_open")
                seen["enabled_selected"] = button.isEnabled()
                return True

            self._drive_modal(window._open_research_window,
                              [check_disabled, select_and_check,
                               _close_step])
            self.assertFalse(seen.get("enabled"),
                             "research Open enabled with no selection")
            self.assertTrue(seen.get("enabled_selected"),
                            "research Open stays disabled after a pick")
        finally:
            try:
                os.remove(report)
            except OSError:
                pass

    def _open_memory_dialog(self):
        button = self.window.findChild(
            QtWidgets.QPushButton, "memory_button")
        button.click()

    def test_memory_buttons_disabled_without_selection(self):
        from forge.browser import memory as memorymod
        entry = memorymod.remember("R3-11 test fact")
        # remember_page skips pages under 200 chars of text.
        memorymod.remember_page(
            "https://example.com/r3-11", "R3-11 Page",
            "A page worth remembering, with enough words in it "
            "to pass the thin-page floor: it describes a test "
            "fixture in far more detail than it deserves, so "
            "the memory store keeps it instead of skipping it. "
            "A few more words here to clear two hundred.")
        memorymod.block_site_memory("example.com")
        try:
            seen = {}

            def check_disabled():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                for name in ("memory_forget_button",
                             "memory_remove_button",
                             "memory_unblock_button"):
                    button = dialog.findChild(QtWidgets.QPushButton,
                                              name)
                    seen[name] = (button is not None
                                  and button.isEnabled())
                return True

            def select_and_check():
                dialog = _modal_dialog()
                facts = dialog.findChild(
                    QtWidgets.QListWidget, "memory_facts")
                facts.setCurrentRow(0)
                pages = dialog.findChild(
                    QtWidgets.QListWidget, "memory_pages")
                pages.setCurrentRow(0)
                sites = dialog.findChild(
                    QtWidgets.QListWidget, "memory_sites")
                sites.setCurrentRow(0)
                self._pump(0.2)
                for name in ("memory_forget_button",
                             "memory_remove_button",
                             "memory_unblock_button"):
                    button = dialog.findChild(QtWidgets.QPushButton,
                                              name)
                    seen[name + "_selected"] = button.isEnabled()
                return True

            self._drive_modal(self._open_memory_dialog,
                              [check_disabled, select_and_check,
                               _close_step])
            for name in ("memory_forget_button", "memory_remove_button",
                         "memory_unblock_button"):
                self.assertFalse(seen.get(name),
                                 "%s enabled with no selection" % name)
                self.assertTrue(seen.get(name + "_selected"),
                                "%s stays disabled after a pick" % name)
        finally:
            if entry is not None:
                memorymod.forget(entry["id"])
            memorymod.forget_page("https://example.com/r3-11")
            memorymod.clear_site_memory("example.com")

    def test_vault_delete_disabled_without_selection(self):
        from forge.browser.vault import Vault, default_vault_path
        vault = Vault.create(default_vault_path(),
                             "test-passphrase-123")
        vault.set("example.com", "user", "pw")
        try:
            seen = {}

            def unlock():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                entry = dialog.findChild(QtWidgets.QLineEdit,
                                         "vault_passphrase")
                if entry is None:
                    return False
                entry.setText("test-passphrase-123")
                _button(dialog, "Unlock").click()
                return True

            def check_disabled():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                button = _button(dialog, "Delete")
                if button is None:
                    return False
                seen["enabled"] = button.isEnabled()
                return True

            def select_and_check():
                dialog = _modal_dialog()
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "vault_list")
                listing.setCurrentRow(0)
                self._pump(0.2)
                seen["enabled_selected"] = _button(
                    dialog, "Delete").isEnabled()
                return True

            self._drive_modal(self.window._open_vault_window,
                              [unlock, check_disabled,
                               select_and_check, _close_step])
            self.assertFalse(seen.get("enabled"),
                             "vault Delete enabled with no selection")
            self.assertTrue(seen.get("enabled_selected"),
                            "vault Delete stays disabled after a pick")
        finally:
            self.window._vault = None
            try:
                os.remove(default_vault_path())
            except OSError:
                pass

    def test_downloads_buttons_disabled_when_empty(self):
        window = self.window
        window._open_downloads_dialog()
        self._pump(0.5)
        try:
            dialog = window._downloads_dialog
            for text in ("Open", "Show in folder", "Retry", "Cancel"):
                button = _button(dialog, text)
                self.assertIsNotNone(button, "%s button missing" % text)
                self.assertFalse(button.isEnabled(),
                                 "%s enabled with an empty list" % text)
        finally:
            if window._downloads_dialog is not None:
                window._downloads_dialog.close()
                self._pump(0.2)


    # -- R3-12: empty lists say what to do next ----------------------

    def test_bookmarks_empty_guidance(self):
        from forge.browser import bookmarks as marksmod
        for mark in marksmod.list_bookmarks():
            marksmod.remove_bookmark(mark["url"])
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            label = dialog.findChild(QtWidgets.QLabel, "bookmarks_empty")
            seen["visible"] = (label is not None
                               and label.isVisible())
            seen["text"] = label.text() if label is not None else ""
            return True

        self._drive_modal(self.window._open_bookmarks_dialog,
                          [check, _close_step])
        self.assertTrue(seen.get("visible"),
                        "no bookmarks empty-state label")
        self.assertIn("Ctrl+D", seen.get("text", ""))

    def test_history_empty_guidance(self):
        from forge.browser import titles as titlesmod
        titlesmod.clear_history()
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            label = dialog.findChild(QtWidgets.QLabel, "history_empty")
            seen["visible"] = (label is not None
                               and label.isVisible())
            seen["text"] = label.text() if label is not None else ""
            return True

        self._drive_modal(self.window._open_history_window,
                          [check, _close_step])
        self.assertTrue(seen.get("visible"),
                        "no history empty-state label")
        self.assertIn("Browse a page", seen.get("text", ""))

    def test_memory_empty_guidance(self):
        from forge.browser import memory as memorymod
        for entry in memorymod.list_entries():
            memorymod.forget(entry["id"])
        for page in memorymod.list_pages():
            memorymod.forget_page(page["url"])
        sites = memorymod.list_site_memory()
        for host in sites.get("blocked", []) + sites.get("allowed", []):
            memorymod.clear_site_memory(host)
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            for name in ("memory_facts_empty", "memory_pages_empty",
                         "memory_sites_empty"):
                label = dialog.findChild(QtWidgets.QLabel, name)
                seen[name] = (label is not None
                              and label.isVisible())
            return True

        self._drive_modal(self._open_memory_dialog,
                          [check, _close_step])
        for name in ("memory_facts_empty", "memory_pages_empty",
                     "memory_sites_empty"):
            self.assertTrue(seen.get(name), "no %s label" % name)

    def test_research_empty_guidance(self):
        from forge.browser import research as researchmod
        folder = researchmod.research_dir()
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if name.endswith(".md"):
                os.remove(os.path.join(folder, name))
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            label = dialog.findChild(QtWidgets.QLabel, "research_empty")
            seen["visible"] = (label is not None
                               and label.isVisible())
            seen["text"] = label.text() if label is not None else ""
            return True

        self._drive_modal(self.window._open_research_window,
                          [check, _close_step])
        self.assertTrue(seen.get("visible"),
                        "no research empty-state label")
        self.assertIn("No research reports saved yet",
                      seen.get("text", ""))

    # -- R3-14: the research empty state wraps -----------------------

    def test_research_empty_state_wraps(self):
        from forge.browser import research as researchmod
        folder = researchmod.research_dir()
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if name.endswith(".md"):
                os.remove(os.path.join(folder, name))
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            label = dialog.findChild(QtWidgets.QLabel, "research_empty")
            listing = dialog.findChild(
                QtWidgets.QListWidget, "research_list")
            seen["is_label"] = isinstance(
                label, QtWidgets.QLabel)
            seen["wraps"] = label.wordWrap() if label else False
            seen["list_hidden"] = (listing is not None
                                   and listing.isHidden())
            return True

        self._drive_modal(self.window._open_research_window,
                          [check, _close_step])
        self.assertTrue(seen.get("is_label"),
                        "research empty state is not a label")
        self.assertTrue(seen.get("wraps"),
                        "research empty state does not wrap")
        self.assertTrue(seen.get("list_hidden"),
                        "the empty research list still shows")

    # -- R3-17: bookmark + history items show title and URL ---------

    def test_bookmark_items_show_title_and_url(self):
        from forge.browser import bookmarks as marksmod
        marksmod.add_bookmark("https://example.com/r3-17",
                              "R3-17 Title")
        try:
            seen = {}

            def check():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "bookmarks_list")
                if listing is None or listing.count() == 0:
                    return False
                seen["text"] = listing.item(0).text()
                return True

            self._drive_modal(self.window._open_bookmarks_dialog,
                              [check, _close_step])
            self.assertEqual(seen.get("text"),
                             "R3-17 Title\nhttps://example.com/r3-17")
        finally:
            marksmod.remove_bookmark("https://example.com/r3-17")

    def test_history_items_show_title_and_url(self):
        from forge.browser import titles as titlesmod
        titlesmod.clear_history()
        titlesmod.record_visit("https://example.com/r3-17",
                               "R3-17 Visit")
        try:
            seen = {}

            def check():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                listing = dialog.findChild(
                    QtWidgets.QListWidget, "history_list")
                if listing is None or listing.count() == 0:
                    return False
                seen["text"] = listing.item(0).text()
                return True

            self._drive_modal(self.window._open_history_window,
                              [check, _close_step])
            self.assertEqual(seen.get("text"),
                             "R3-17 Visit\nhttps://example.com/r3-17")
        finally:
            titlesmod.clear_history()

    # -- R3-18: the vault create page is short and plain -------------

    def test_vault_create_page_is_short_and_plain(self):
        from forge.browser.vault import default_vault_path
        if os.path.exists(default_vault_path()):
            self.skipTest("a vault already exists in this HOME")
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["height"] = dialog.height()
            texts = [label.text()
                     for label in dialog.findChildren(QtWidgets.QLabel)]
            seen["no_vault_yet_count"] = sum(
                text.count("No vault yet.") for text in texts)
            seen["instruction"] = any(
                "Choose a passphrase" in text for text in texts)
            return True

        self._drive_modal(self.window._open_vault_window,
                          [check, _close_step])
        self.assertEqual(seen.get("no_vault_yet_count"), 1,
                         "'No vault yet.' appears %r times"
                         % seen.get("no_vault_yet_count"))
        self.assertTrue(seen.get("instruction"),
                        "no create-a-passphrase instruction")
        self.assertLess(seen.get("height", 9999), 360,
                        "create page is still a tall box: %r px"
                        % seen.get("height"))


    # -- R3-26: a splash while the window starts ---------------------

    def test_splash_pixmap_is_a_dark_card(self):
        pix = appmod._splash_pixmap(QtGui, QtCore)
        self.assertFalse(pix.isNull())
        self.assertEqual(pix.width(), 360)
        self.assertEqual(pix.height(), 160)
        image = pix.toImage()
        corner = image.pixelColor(4, 4)
        self.assertLess(corner.red(), 40)
        self.assertLess(corner.green(), 40)
        self.assertLess(corner.blue(), 40)

    def test_run_app_shows_a_splash(self):
        with open(_tree_path("forge/browser/app.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("QSplashScreen", source)
        self.assertIn("splash.finish(window)", source)

    # -- R3-28: print and save as PDF --------------------------------

    def test_print_and_save_pdf_menu_items(self):
        menus = self._menu_actions()
        file_menu = {text for menu, text in menus if menu == "File"}
        self.assertIn("Print\u2026", file_menu)
        self.assertIn("Save as PDF\u2026", file_menu)

    def _wait_for_pdf(self, path, timeout=30.0):
        return self._wait_for(
            lambda: (os.path.exists(path)
                     and os.path.getsize(path) > 0),
            timeout=timeout)

    def test_save_page_as_pdf_writes_a_pdf(self):
        window = self.window
        path = os.path.join(self.home, "page.pdf")
        if os.path.exists(path):
            os.remove(path)
        with mock.patch.object(
                QtWidgets.QFileDialog, "getSaveFileName",
                return_value=(path, "")):
            window._save_page_pdf()
        self.assertTrue(
            self._wait_for_pdf(path),
            "no PDF appeared at %s" % path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(4), b"%PDF")

    def test_print_page_renders_a_pdf(self):
        window = self.window
        path = os.path.join(tempfile.gettempdir(),
                            "forge-browser-print.pdf")
        if os.path.exists(path):
            os.remove(path)
        with mock.patch.object(
                QtGui.QDesktopServices, "openUrl",
                return_value=True):
            window._print_page()
        self.assertTrue(
            self._wait_for_pdf(path),
            "no print PDF appeared at %s" % path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(4), b"%PDF")

    # -- R3-29: a second window --------------------------------------

    def test_new_window_opens_a_second_window(self):
        menus = self._menu_actions()
        file_menu = {text for menu, text in menus if menu == "File"}
        self.assertIn("New Window", file_menu)
        before = len(appmod._WINDOWS)
        other = self.window._new_window()
        try:
            self.assertEqual(len(appmod._WINDOWS), before + 1)
            self.assertTrue(other.isVisible())
            self.assertEqual(other.tabs.count(), 1)
        finally:
            other.close()
            self._pump(0.5)
        self.assertEqual(len(appmod._WINDOWS), before)

    # -- R3-30: F11 toggles fullscreen -------------------------------

    def test_f11_toggles_fullscreen(self):
        window = self.window
        action = window._fullscreen_action
        self.assertTrue(action.isCheckable())
        self.assertEqual(action.shortcut().toString(), "F11")
        self.assertFalse(window.isFullScreen())
        window._toggle_fullscreen()
        self._pump(0.3)
        self.assertTrue(window.isFullScreen())
        self.assertTrue(action.isChecked())
        window._toggle_fullscreen()
        self._pump(0.3)
        self.assertFalse(window.isFullScreen())
        self.assertFalse(action.isChecked())

    # -- R3-31: open a local file ------------------------------------

    def test_open_file_menu_item_and_load(self):
        menus = self._menu_actions()
        file_menu = {text for menu, text in menus if menu == "File"}
        self.assertIn("Open File\u2026", file_menu)
        window = self.window
        path = os.path.join(self.home, "open-me.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("<html><body>open me</body></html>")
        before = window.tabs.count()
        with mock.patch.object(
                QtWidgets.QFileDialog, "getOpenFileName",
                return_value=(path, "")):
            window._open_file()
        self.assertEqual(window.tabs.count(), before + 1)
        # The view's url() stays empty until the load commits,
        # so wait for the file URL instead of reading it at once.
        self.assertTrue(
            self._wait_for(
                lambda: window._view().url().toString().startswith(
                    "file://"),
                timeout=15.0),
            "new tab never landed on the file: %r"
            % window._view().url().toString())

    # -- R3-32: Save As for downloads --------------------------------

    def test_download_ask_defaults_off(self):
        from forge.browser.settings import get_setting
        self.assertEqual(get_setting("download_ask"), "0")

    def test_settings_has_download_ask_checkbox(self):
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            box = dialog.findChild(QtWidgets.QCheckBox,
                                   "settings_download_ask")
            seen["exists"] = box is not None
            seen["checked"] = box.isChecked() if box else None
            seen["text"] = box.text() if box else ""
            return True

        self._drive_modal(
            lambda: appmod.open_settings_dialog(self.window),
            [check, _close_step])
        self.assertTrue(seen.get("exists"),
                        "no Ask-where-to-save checkbox in Settings")
        self.assertFalse(seen.get("checked"),
                         "Ask where to save is on by default")
        self.assertIn("Ask where to save", seen.get("text", ""))

    def test_save_as_lands_at_the_chosen_path(self):
        window = self.window
        folder = os.path.join(self.home, "dl")
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, "incomplete.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("download bytes")
        chosen = os.path.join(folder, "picked", "named.txt")
        record = {"tmp": tmp, "save_as": chosen,
                  "name": "named.txt", "folder": folder}
        final = window._finish_download_file(record)
        self.assertEqual(final, chosen)
        self.assertTrue(os.path.exists(chosen))
        self.assertFalse(os.path.exists(tmp))
        with open(chosen, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "download bytes")

    def test_finish_download_without_save_as_uniquifies(self):
        window = self.window
        folder = os.path.join(self.home, "dl2")
        os.makedirs(folder, exist_ok=True)
        taken = os.path.join(folder, "file.txt")
        with open(taken, "w", encoding="utf-8") as handle:
            handle.write("existing")
        tmp = os.path.join(folder, "incomplete.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("new bytes")
        record = {"tmp": tmp, "name": "file.txt", "folder": folder}
        final = window._finish_download_file(record)
        self.assertNotEqual(final, taken)
        self.assertTrue(final.startswith(folder))
        self.assertTrue(os.path.exists(final))

    # -- R3-33: the tab overflow menu --------------------------------

    def test_tab_overflow_menu_lists_tabs(self):
        window = self.window
        button = window.findChild(
            QtWidgets.QToolButton, "tabs_overflow_button")
        self.assertIsNotNone(button, "no tab overflow button")
        window.new_tab(self.server.home_url)
        window.new_tab(self.server.home_url)
        window.tabs.setTabText(0, "Tab One")
        window.tabs.setTabText(1, "Tab Two")
        window.tabs.setTabText(2, "Tab Three")
        window._rebuild_tabs_menu()
        menu = window._tabs_menu
        texts = [action.text() for action in menu.actions()]
        self.assertEqual(texts, ["Tab One", "Tab Two", "Tab Three"])
        window.tabs.setCurrentIndex(2)
        menu.actions()[0].trigger()
        self._pump(0.2)
        self.assertEqual(window.tabs.currentIndex(), 0)
