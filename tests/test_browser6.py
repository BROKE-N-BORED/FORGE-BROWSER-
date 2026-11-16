"""Tests for the WINDOW section of Forge Browser v0.10.

The pure tests run under plain python (no PySide6). The Qt tests
at the bottom run under the offscreen platform when PySide6 is
present and skip otherwise — they share one window so the suite
stays fast.
"""

import gzip
import http.client
import json
import logging
import os
import re
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from forge.browser import app as appmod
from forge.browser import compare as comparemod
from forge.browser import debug_log as debuglog
from forge.browser import howto as howtomod
from forge.browser import preview as previewmod
from forge.browser import titles as titlesmod
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


# ------------------------------------------------------------------
# B9: the honest line — no "nothing tracked" anywhere a user reads.
# ------------------------------------------------------------------

class HonestLineTests(unittest.TestCase):
    def test_tagline_is_honest(self):
        self.assertNotIn("nothing tracked", appmod._TAGLINE.lower())
        self.assertIn("Nothing leaves this machine", appmod._TAGLINE)
        self.assertNotIn("\u2014", appmod._TAGLINE)

    def test_start_page_is_honest(self):
        page = self._start_page()
        self.assertNotIn("nothing tracked", page.lower())
        self.assertIn("Nothing leaves this machine", page)
        self.assertIn('href="/history"', page)
        self.assertIn('href="/memory"', page)

    def test_start_page_mentions_local_keeping(self):
        page = self._start_page()
        self.assertIn("kept on this PC", page)

    @staticmethod
    def _start_page():
        webuimod._STATUS_CACHE["status"] = None
        with mock.patch.object(titlesmod, "load_history", return_value=[]), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]), \
                mock.patch("forge.browser.engines.engine_status",
                           return_value={}):
            return webuimod.start_page(token="t")


# ------------------------------------------------------------------
# B7: model blurbs — role, size, and whether it fits this PC.
# ------------------------------------------------------------------

class ModelBlurbTests(unittest.TestCase):
    def test_chat_model_fits(self):
        blurb = appmod.model_blurb("qwen3:4b", 2_600_000_000, 8192)
        self.assertIn("chat model", blurb)
        self.assertIn("2.4 GB", blurb)
        self.assertIn("fits this PC", blurb)

    def test_big_model_does_not_fit(self):
        blurb = appmod.model_blurb("qwen3:32b", 20_000_000_000, 8192)
        self.assertIn("too big for this PC", blurb)

    def test_fit_boundary_is_ram_minus_2gb(self):
        # 8 GB total: a 6 GB model fits, a 6.1 GB model does not.
        self.assertIn("fits this PC",
                      appmod.model_fit_note("qwen3:4b", 6 * 1024 ** 3,
                                            8192))
        self.assertIn("too big",
                      appmod.model_fit_note("qwen3:32b",
                                            int(6.1 * 1024 ** 3), 8192))

    def test_embed_and_vision_roles(self):
        self.assertIn("finds matching text",
                      appmod.model_blurb("nomic-embed-text", 274_000_000,
                                         8192))
        self.assertIn("reads pictures",
                      appmod.model_blurb("llava:7b", None, 8192))

    def test_unknown_size_has_no_fit_note(self):
        blurb = appmod.model_blurb("llava:7b", None, 8192)
        self.assertNotIn("fits this PC", blurb)
        self.assertNotIn("too big", blurb)

    def test_role_words(self):
        self.assertEqual(appmod.model_role("qwen3:4b"),
                         "chat model (answers and drives the browser)")
        self.assertEqual(appmod.model_role("mxbai-embed-large"),
                         "finds matching text")
        self.assertEqual(appmod.model_role("llava:13b"),
                         "reads pictures")
        self.assertEqual(appmod.model_role("something-else"),
                         "chat model (answers and drives the browser)")


# ------------------------------------------------------------------
# G-S3: service addresses are validated before they are saved.
# ------------------------------------------------------------------

class ValidateServiceUrlTests(unittest.TestCase):
    def test_empty_is_ok(self):
        ok, remote, problem = appmod.validate_service_url("")
        self.assertTrue(ok)
        self.assertFalse(remote)
        self.assertEqual(problem, "")

    def test_loopback_is_local(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:8080",
                    "http://[::1]:11434", "https://127.0.0.1/"):
            ok, remote, _ = appmod.validate_service_url(url)
            self.assertTrue(ok, url)
            self.assertFalse(remote, url)

    def test_remote_is_flagged(self):
        ok, remote, _ = appmod.validate_service_url(
            "http://192.168.1.5:11434")
        self.assertTrue(ok)
        self.assertTrue(remote)
        ok, remote, _ = appmod.validate_service_url(
            "https://ollama.example.com")
        self.assertTrue(ok)
        self.assertTrue(remote)

    def test_bad_addresses_are_rejected(self):
        for url in ("example.com", "ftp://x/", "http://",
                    "http://exa mple.com"):
            ok, _, problem = appmod.validate_service_url(url)
            self.assertFalse(ok, url)
            self.assertTrue(problem, url)


# ------------------------------------------------------------------
# B6: the live status line.
# ------------------------------------------------------------------

class LiveStatsTests(unittest.TestCase):
    def test_full_line(self):
        text = appmod.live_stats_text(3200, 8192, 4.2, 12)
        self.assertEqual(text, "RAM 3.1/8.0 GB \u00b7 4.2 tok/s \u00b7 12 turns")

    def test_turns_only_when_nothing_else(self):
        self.assertEqual(appmod.live_stats_text(turns=0), "0 turns")
        self.assertEqual(appmod.live_stats_text(turns=1), "1 turn")

    def test_ram_only(self):
        self.assertEqual(appmod.live_stats_text(1024, 8192),
                         "RAM 1.0/8.0 GB \u00b7 0 turns")

    def test_meminfo_reads(self):
        mem = appmod._read_mem_mb()
        if mem is not None:
            used, total = mem
            self.assertGreater(total, 0)
            self.assertGreaterEqual(total, used)


# ------------------------------------------------------------------
# G-UX6: the failed-load page is friendly, not a blank tab.
# ------------------------------------------------------------------

class LoadFailedHtmlTests(unittest.TestCase):
    def test_friendly_and_escaped(self):
        page = appmod.load_failed_html(
            "http://example.org/<script>alert(1)</script>")
        self.assertIn("Could not reach", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("\u2014", page)

    def test_has_plain_advice(self):
        page = appmod.load_failed_html("http://example.org/")
        self.assertIn("Check the address", page)
        self.assertIn("check your connection", page)


# ------------------------------------------------------------------
# G-UX9: the chat log persists, capped.
# ------------------------------------------------------------------

class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-chat-history-")
        self.path = os.path.join(self.tmp, "chat-history.txt")
        self.patch = mock.patch.dict(
            os.environ, {"FORGE_BROWSER_CHAT_HISTORY_PATH": self.path})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_override(self):
        self.assertEqual(appmod.chat_history_path(), self.path)

    def test_round_trip(self):
        appmod.save_chat_history("hello\nworld\n")
        self.assertEqual(appmod.load_chat_history(), "hello\nworld\n")

    def test_missing_file_reads_empty(self):
        self.assertEqual(appmod.load_chat_history(), "")

    def test_save_caps_at_200kb_keeping_the_tail(self):
        text = "x" * 100_000 + "TAIL" + "y" * 150_000
        appmod.save_chat_history(text)
        saved = appmod.load_chat_history()
        self.assertEqual(len(saved), appmod._CHAT_HISTORY_MAX_BYTES)
        self.assertTrue(saved.endswith("y" * 150_000))


# ------------------------------------------------------------------
# G-C21: a send's tab text is keyed by view, not by position.
# ------------------------------------------------------------------

class _FakeUrl:
    def __init__(self, text):
        self._text = text

    def toString(self):
        return self._text


class _FakeView:
    def __init__(self, title, url):
        self._title = title
        self._url = url

    def title(self):
        return self._title

    def url(self):
        return _FakeUrl(self._url)


class SendTabsPayloadTests(unittest.TestCase):
    def test_text_follows_the_view_when_tabs_reorder(self):
        view_a = _FakeView("A", "http://a.example/")
        view_b = _FakeView("B", "http://b.example/")
        texts = {id(view_a): "text of A", id(view_b): "text of B"}
        # The tabs reordered while the text was being read.
        tabs = appmod.send_tabs_payload([view_b, view_a], texts, view_a,
                                        set())
        self.assertEqual(tabs[0]["url"], "http://b.example/")
        self.assertEqual(tabs[0]["text"], "text of B")
        self.assertEqual(tabs[1]["url"], "http://a.example/")
        self.assertEqual(tabs[1]["text"], "text of A")
        self.assertTrue(tabs[1]["current"])
        self.assertFalse(tabs[0]["current"])

    def test_closed_tab_drops_out(self):
        view_a = _FakeView("A", "http://a.example/")
        view_b = _FakeView("B", "http://b.example/")
        texts = {id(view_a): "text of A", id(view_b): "text of B"}
        tabs = appmod.send_tabs_payload([view_a], texts, view_a, set())
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["text"], "text of A")

    def test_missing_text_is_empty_and_agent_flagged(self):
        view_a = _FakeView("A", "http://a.example/")
        tabs = appmod.send_tabs_payload([view_a], {}, view_a,
                                        {id(view_a)})
        self.assertEqual(tabs[0]["text"], "")
        self.assertTrue(tabs[0]["agent"])

    def test_none_view_is_a_blank_tab(self):
        tabs = appmod.send_tabs_payload([None], {}, None, set())
        self.assertEqual(tabs[0], {"title": "", "url": "", "text": "",
                                   "current": False, "agent": False})


# ------------------------------------------------------------------
# G-S1: secrets are scrubbed from the debug log at every sink.
# ------------------------------------------------------------------

class ScrubTests(unittest.TestCase):
    def test_json_quoted_pairs(self):
        text = debuglog.scrub_text(
            '{"password": "hunter2", "token": "abc123"}')
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc123", text)
        self.assertEqual(text.count("[REDACTED]"), 2)

    def test_json_unquoted_value(self):
        text = debuglog.scrub_text('{"api_key": abc123}')
        self.assertNotIn("abc123", text)
        self.assertIn("[REDACTED]", text)

    def test_bare_token_key_value(self):
        self.assertNotIn("abc123", debuglog.scrub_text("token=abc123"))
        self.assertNotIn("abc123",
                         debuglog.scrub_text("token: abc123"))

    def test_similar_words_are_untouched(self):
        self.assertIn("tokens=5", debuglog.scrub_text("tokens=5"))
        self.assertIn("tokenizer=x",
                      debuglog.scrub_text("tokenizer=x"))

    def test_traceback_is_scrubbed_at_every_sink(self):
        tmp = tempfile.mkdtemp(prefix="forge-scrub-")
        try:
            logger = debuglog.DebugLogger("scrub-test", "0.9",
                                          log_dir=tmp)
            try:
                raise ValueError("login failed, token=hunter2")
            except ValueError:
                logger.exception("a failure")
            ring = "\n".join(logger._ring.records)
            errors = "\n".join(logger._errors.errors)
            self.assertNotIn("hunter2", ring)
            self.assertNotIn("hunter2", errors)
            self.assertIn("[REDACTED]", ring)
            with open(logger.log_path, encoding="utf-8") as handle:
                self.assertNotIn("hunter2", handle.read())
            report = logger.build_report()
            self.assertNotIn("hunter2", report)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_report_rescrubs_stale_records(self):
        tmp = tempfile.mkdtemp(prefix="forge-scrub-")
        try:
            logger = debuglog.DebugLogger("scrub-test-2", "0.9",
                                          log_dir=tmp)
            # A record that reached the ring before scrubbing
            # existed (e.g. from an older run's buffer).
            logger._ring.records.append("leaked password=hunter2")
            report = logger.build_report()
            self.assertNotIn("hunter2", report)
            self.assertIn("[REDACTED]", report)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------
# G-P17: previews are capped at 256 KB — a hover never downloads
# a whole 10 MB page.
# ------------------------------------------------------------------

class _FakeHeadResponse:
    """A stand-in for _urlopen's response, recording what was read."""

    def __init__(self, body, headers=None, url="http://example.org/x"):
        self._body = body
        self.headers = headers or {}
        self._url = url
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PreviewCapTests(unittest.TestCase):
    def setUp(self):
        previewmod.clear_preview_cache()

    def tearDown(self):
        previewmod.clear_preview_cache()

    def _head(self, content_type, size):
        return lambda url: (content_type, size)

    def test_big_html_uses_only_the_head(self):
        body = (b"<html><head><title>Big Page</title></head><body>"
                b"<p>First paragraph here.</p>"
                + b"x" * (2 * 1024 * 1024) + b"</body></html>")
        seen = {}

        def fake_urlopen(request, timeout=None, allow_local=False):
            seen["range"] = request.get_header("Range")
            return _FakeHeadResponse(
                body, {"Content-Type": "text/html; charset=utf-8"},
                "http://example.org/big.html")

        with mock.patch("forge.browser._http._urlopen", fake_urlopen), \
                mock.patch("forge.browser._http.check_fetch_url",
                           lambda url: None):
            text = previewmod.preview_url(
                "http://example.org/big.html",
                head_fetcher=self._head("text/html", 2 * 1024 * 1024))
        self.assertEqual(seen["range"],
                         "bytes=0-%d" % (previewmod.PREVIEW_MAX_BYTES - 1))
        self.assertIn("Big Page", text)
        self.assertIn("First paragraph here.", text)

    def test_big_pdf_is_named_without_any_fetch(self):
        def boom(url):
            raise AssertionError("a big PDF must not be fetched")

        text = previewmod.preview_url(
            "http://example.org/big.pdf",
            fetcher=boom,
            head_fetcher=self._head("application/pdf", 12 * 1024 * 1024))
        self.assertIn("PDF file", text)
        self.assertIn("12 MB", text)
        self.assertIn("Too big to preview", text)

    def test_small_html_still_uses_the_fetcher(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return types.SimpleNamespace(
                title="Small", text="small page text", kind="text",
                size_bytes=1024, content_type="text/html")

        text = previewmod.preview_url(
            "http://example.org/small.html", fetcher=fetcher,
            head_fetcher=self._head("text/html", 1024))
        self.assertEqual(calls, ["http://example.org/small.html"])
        self.assertIn("small page text", text)

    def test_unknown_size_without_fetcher_uses_the_head(self):
        body = (b"<html><head><title>Chunked</title></head><body>"
                b"<p>Chunked page text.</p></body></html>")

        def fake_urlopen(request, timeout=None, allow_local=False):
            return _FakeHeadResponse(
                body, {"Content-Type": "text/html; charset=utf-8"},
                "http://example.org/chunked")

        with mock.patch("forge.browser._http._urlopen", fake_urlopen), \
                mock.patch("forge.browser._http.check_fetch_url",
                           lambda url: None):
            text = previewmod.preview_url(
                "http://example.org/chunked",
                head_fetcher=self._head("text/html", 0))
        self.assertIn("Chunked", text)
        self.assertIn("Chunked page text.", text)

    def test_unknown_size_with_fetcher_still_calls_it(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return types.SimpleNamespace(
                title="T", text="fetcher text", kind="text",
                size_bytes=0, content_type="text/html")

        previewmod.preview_url(
            "http://example.org/unknown.html", fetcher=fetcher,
            head_fetcher=self._head("text/html", 0))
        self.assertEqual(calls, ["http://example.org/unknown.html"])

    def test_gzipped_head_is_decompressed_with_a_bound(self):
        raw = (b"<html><head><title>Gzipped</title></head><body>"
               b"<p>Compressed text.</p></body></html>")
        body = gzip.compress(raw)

        def fake_urlopen(request, timeout=None, allow_local=False):
            return _FakeHeadResponse(
                body, {"Content-Type": "text/html; charset=utf-8",
                       "Content-Encoding": "gzip"},
                "http://example.org/gz.html")

        with mock.patch("forge.browser._http._urlopen", fake_urlopen), \
                mock.patch("forge.browser._http.check_fetch_url",
                           lambda url: None):
            text = previewmod.preview_url(
                "http://example.org/gz.html",
                head_fetcher=self._head("text/html", 2 * 1024 * 1024))
        self.assertIn("Gzipped", text)
        self.assertIn("Compressed text.", text)

    def test_head_fetch_failure_is_a_plain_note(self):
        def failing_urlopen(request, timeout=None, allow_local=False):
            raise OSError("network down")

        with mock.patch("forge.browser._http._urlopen", failing_urlopen), \
                mock.patch("forge.browser._http.check_fetch_url",
                           lambda url: None):
            text = previewmod.preview_url(
                "http://example.org/down.html",
                head_fetcher=self._head("text/html", 2 * 1024 * 1024))
        self.assertEqual(text, "Couldn't fetch that link.")


# ------------------------------------------------------------------
# G-C36 + B15: the history page survives malformed entries and
# discloses the cap.
# ------------------------------------------------------------------

class HistoryPageTests(unittest.TestCase):
    ENTRIES = [
        {"title": "No url here"},
        "a bare string",
        42,
        None,
        {"url": "http://example.org/", "title": "Good Page",
         "at": 1700000000},
    ]

    def test_malformed_entries_are_skipped_not_a_500(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=list(self.ENTRIES)):
            page = webuimod.history_page()
        self.assertIn("Good Page", page)
        self.assertNotIn("No url here", page)
        self.assertNotIn("a bare string", page)

    def test_cap_is_disclosed(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=list(self.ENTRIES)):
            page = webuimod.history_page()
        self.assertIn("History keeps the newest 200 visits", page)
        self.assertIn("Older visits drop off", page)

    def test_search_filters_without_crashing_on_bad_entries(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=list(self.ENTRIES)):
            page = webuimod.history_page(query="good")
        self.assertIn("Good Page", page)


# ------------------------------------------------------------------
# B14 + G-P12 + G-P13 + G-T6: the start page.
# ------------------------------------------------------------------

class StartPageTests(unittest.TestCase):
    def setUp(self):
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0
        self.tmp = tempfile.mkdtemp(prefix="forge-start-")
        self.home = mock.patch.dict(os.environ, {"HOME": self.tmp})
        self.home.start()

    def tearDown(self):
        self.home.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0

    def _page(self, entries):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=entries), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]), \
                mock.patch("forge.browser.engines.engine_status",
                           return_value={}):
            return webuimod.start_page(token="t")

    def test_no_engine_dump_on_the_page(self):
        page = self._page([])
        self.assertNotIn("Chromium engine", page)
        self.assertNotIn("kitesurf", page.lower())
        self.assertIn("No page engine is installed", page)
        self.assertIn("How to fix it", page)

    def test_ready_line_when_an_engine_exists(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=[]), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]), \
                mock.patch("forge.browser.engines.engine_status",
                           return_value={"chromium": "/usr/bin/x"}):
            page = webuimod.start_page(token="t")
        self.assertIn("Everything is ready.", page)

    def test_history_is_loaded_once_per_page(self):
        calls = []

        def counting():
            calls.append(1)
            return []

        with mock.patch.object(titlesmod, "load_history",
                               side_effect=counting), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]), \
                mock.patch("forge.browser.engines.engine_status",
                           return_value={}):
            webuimod.start_page(token="t")
        self.assertEqual(len(calls), 1)

    def test_engine_probe_is_cached_across_pages(self):
        calls = []

        def counting():
            calls.append(1)
            return {}

        with mock.patch.object(titlesmod, "load_history",
                               return_value=[]), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]), \
                mock.patch("forge.browser.engines.engine_status",
                           side_effect=counting):
            webuimod.start_page(token="t")
            webuimod.start_page(token="t")
        self.assertEqual(len(calls), 1)

    def test_no_howto_link_once_history_exists(self):
        entries = [{"url": "http://example.org/", "title": "Example",
                    "at": time.time()}]
        page = self._page(entries)
        self.assertNotIn("First time here?", page)
        self.assertNotIn("How to use Forge Browser", page)

    def test_howto_link_on_first_run(self):
        page = self._page([])
        self.assertIn("First time here?", page)
        self.assertIn("How to use Forge Browser", page)

    def test_brief_refresh_link_carries_the_token(self):
        page = self._page([])
        self.assertIn("/brief-refresh?token=t", page)


class EngineStatusLineTests(unittest.TestCase):
    def test_ready_when_any_engine_found(self):
        for status in ({"chromium": "/x"}, {"lightpanda_binary": "/x"},
                       {"kitesurf_serving": True},
                       {"cdp_url": "http://127.0.0.1:9222"}):
            self.assertEqual(webuimod.engine_status_line(status),
                             "Everything is ready.")

    def test_missing_line_names_the_fix(self):
        line = webuimod.engine_status_line({})
        self.assertIn("No page engine is installed", line)
        self.assertIn("Browsing still works", line)
        self.assertIn('href="/howto"', line)
        self.assertNotIn("\u2014", line)


class GroupingEquivalenceTests(unittest.TestCase):
    """G-P13: the start page's grouping mirrors titles.py exactly,
    from already-loaded entries."""

    ENTRIES = [
        {"url": "http://example.org/a", "title": "A", "at": 1700003600},
        {"url": "http://example.org/b", "title": "B", "at": 1700003500},
        {"url": "http://other.net/x", "title": "X", "at": 1700003400},
        {"url": "http://example.org/c", "title": "C", "at": 1699990000},
        {"url": "http://third.io/y", "title": "Y", "at": 1699989000},
    ]

    def test_grouping_matches(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=list(self.ENTRIES)):
            expected = titlesmod.grouped_history()
        got = webuimod._group_entries(list(self.ENTRIES))
        self.assertEqual(got, expected)

    def test_journeys_match(self):
        with mock.patch.object(titlesmod, "load_history",
                               return_value=list(self.ENTRIES)):
            expected = titlesmod.journeys()
        got = webuimod._journey_entries(list(self.ENTRIES))
        self.assertEqual(got, expected)

    def test_empty_history(self):
        self.assertEqual(webuimod._group_entries([]), [])
        self.assertEqual(webuimod._journey_entries([]), [])


class TodayPagesMetaTests(unittest.TestCase):
    """G-P13: today's list is metadata-only — no chunk text parsed."""

    def test_window_dedup_and_sensitive_exclusion(self):
        now = time.time()
        today = now - 3600
        yesterday = now - 90000
        pages = [
            {"url": "http://example.org/a", "title": "A", "at": today},
            {"url": "http://example.org/old", "title": "Old",
             "at": yesterday},
            {"url": "https://bank.example.com/login", "title": "Bank",
             "at": today},
        ]
        entries = [
            {"url": "http://example.org/a", "title": "A dup",
             "at": today},
            {"url": "http://example.org/b", "title": "B", "at": today},
        ]
        with mock.patch("forge.browser.memory.list_pages",
                        return_value=pages), \
                mock.patch("forge.browser.memory.page_texts",
                           side_effect=AssertionError(
                               "chunk text must not be parsed")):
            got = webuimod._today_pages_meta(entries, now=now)
        urls = [page["url"] for page in got]
        self.assertEqual(urls, ["http://example.org/a",
                                "http://example.org/b"])
        self.assertEqual(got[0]["title"], "A")

    def test_newest_first(self):
        now = time.time()
        entries = [
            {"url": "http://a.example/", "title": "A", "at": now - 100},
            {"url": "http://b.example/", "title": "B", "at": now - 50},
        ]
        with mock.patch("forge.browser.memory.list_pages",
                        return_value=[]):
            got = webuimod._today_pages_meta(entries, now=now)
        self.assertEqual([p["url"] for p in got],
                         ["http://b.example/", "http://a.example/"])


# ------------------------------------------------------------------
# G-UX8: local addresses get http, the web gets https.
# ------------------------------------------------------------------

class ResolveAddressTests(unittest.TestCase):
    def test_local_hosts_use_http(self):
        for text, expected in (
                ("localhost:8888", "http://localhost:8888"),
                ("localhost/x", "http://localhost/x"),
                ("127.0.0.1:9222", "http://127.0.0.1:9222"),
                ("127.0.0.1", "http://127.0.0.1"),
                ("[::1]:8080", "http://[::1]:8080"),
                ("::1", "http://[::1]"),
                ("printer.local", "http://printer.local")):
            kind, target = webuimod.resolve_address(text)
            self.assertEqual((kind, target), ("url", expected), text)

    def test_normal_domains_use_https(self):
        self.assertEqual(webuimod.resolve_address("example.org"),
                         ("url", "https://example.org"))
        self.assertEqual(
            webuimod.resolve_address("example.org:8080/x"),
            ("url", "https://example.org:8080/x"))

    def test_words_are_still_search(self):
        self.assertEqual(webuimod.resolve_address("how to bake bread"),
                         ("search", "how to bake bread"))
        # Empty input goes home.
        self.assertEqual(webuimod.resolve_address(""), ("url", "/"))


# ------------------------------------------------------------------
# G-S4: report links get rel=noopener.
# ------------------------------------------------------------------

class NoopenerTests(unittest.TestCase):
    def test_rel_added(self):
        page = webuimod._with_noopener('<a href="http://x/">x</a>')
        self.assertIn('rel="noopener"', page)

    def test_existing_rel_kept(self):
        page = webuimod._with_noopener(
            '<a rel="nofollow" href="http://x/">x</a>')
        self.assertIn('rel="nofollow"', page)
        self.assertNotIn("noopener", page)

    def test_report_page_links_are_hardened(self):
        report = {"name": "some-report", "path": "/nonexistent/r.md",
                  "topic": "Topic", "at": 1700000000}
        with mock.patch("forge.browser.research.list_reports",
                        return_value=[report]), \
                mock.patch("forge.browser.research.report_to_html",
                           return_value='<a href="http://x/">x</a>'), \
                mock.patch("builtins.open",
                           mock.mock_open(read_data="# Topic\n")):
            page = webuimod.research_report_page("some-report")
        self.assertIn('rel="noopener"', page)


# ------------------------------------------------------------------
# G-S4: the choke point — every page the local server serves, and
# every non-HTML body it must leave alone.
# ------------------------------------------------------------------

class ServedNoopenerTests(unittest.TestCase):
    _EXTERNAL_LINK = re.compile(
        r'<a\s+[^>]*href="https?://[^"]*"[^>]*>')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-noopener-")
        history_path = os.path.join(self.tmp, "history.json")
        with open(history_path, "w", encoding="utf-8") as handle:
            json.dump([{"url": "http://example.org/", "title": "E",
                        "at": 1700000000}], handle)
        pages_path = os.path.join(self.tmp, "pages.json")
        with open(pages_path, "w", encoding="utf-8") as handle:
            json.dump({"pages": [{"url": "http://example.org/kept",
                                  "title": "Kept", "at": 1700000000,
                                  "chunks": ["some kept text"]}]}, handle)
        self.env = mock.patch.dict(os.environ, {
            "HOME": self.tmp,
            "FORGE_BROWSER_HISTORY_PATH": history_path,
            "FORGE_BROWSER_PAGE_MEMORY_PATH": pages_path,
            "FORGE_BROWSER_MEMORY_PATH":
                os.path.join(self.tmp, "no-memory.json"),
            "FORGE_BROWSER_BOOKMARKS_PATH":
                os.path.join(self.tmp, "no-bookmarks.json"),
        })
        self.env.start()
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0
        self.server = webuimod.LocalPageServer().start()

    def tearDown(self):
        self.server.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0
        comparemod.set_last_comparison([], [])
        comparemod.set_server_base("")

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=10)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        conn.close()
        return response.status, body

    def _assert_external_links_hardened(self, body):
        """Every http(s) link in the served page carries a rel —
        and the caller asserts the count, so the check is never
        vacuous on a page with no links at all."""
        links = self._EXTERNAL_LINK.findall(body)
        for link in links:
            self.assertIn("rel=", link,
                          "external link without a rel: %s" % link)
        return len(links)

    def test_served_pages_harden_external_links(self):
        # The start page, history, memory, and search results
        # all link out to visited sites — through the server,
        # every one of those links carries rel="noopener".
        fake_results = [types.SimpleNamespace(
            url="http://example.org/result", title="Result",
            snippet="a snippet")]
        with mock.patch.object(webuimod, "keyless_search",
                               return_value=fake_results):
            status, search_body = self._get("/search?q=gears")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(
            self._assert_external_links_hardened(search_body), 1)
        for path, minimum in (("/", 1), ("/history", 1),
                              ("/memory", 1), ("/howto", 0)):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertGreaterEqual(
                self._assert_external_links_hardened(body), minimum,
                path)

    def test_non_html_bodies_are_not_rewritten(self):
        # A comparison field can contain text that merely looks
        # like a link — the CSV and Markdown exports must carry
        # it verbatim, never with a rel injected.
        rows = [{"title": "Tab", "url": "http://example.org/",
                 "fields": {"Note": '<a href="http://x/">x</a>'}}]
        comparemod.set_last_comparison(rows, ["Note"])
        for path in ("/compare.csv", "/compare.md"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn('rel="noopener"', body, path)
            self.assertIn("http://x/", body, path)

    def test_compare_refresh_links_are_hardened(self):
        rows = [{"title": "Tab", "url": "http://example.org/",
                 "fields": {"Note": "n"}}]
        comparemod.set_last_comparison(rows, ["Note"])
        comparemod.set_server_base(self.server.home_url)
        status, body = self._get("/compare-refresh")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(
            self._assert_external_links_hardened(body), 1)


# ------------------------------------------------------------------
# B9/B15: the /memory page.
# ------------------------------------------------------------------

class MemoryPageTests(unittest.TestCase):
    def test_facts_pages_and_caps(self):
        facts = [{"text": "Joshua likes tea", "at": 1700000000}]
        pages = [{"url": "http://example.org/", "title": "Example",
                  "at": 1700000000, "chunks": 3}]
        with mock.patch("forge.browser.memory.list_entries",
                        return_value=facts), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=pages):
            page = webuimod.memory_page()
        self.assertIn("Joshua likes tea", page)
        self.assertIn("Example", page)
        self.assertIn("Keeps the newest 200 facts", page)
        self.assertIn("Keeps the newest 100 pages", page)
        self.assertIn("Kept on this PC only", page)

    def test_empty_memory(self):
        with mock.patch("forge.browser.memory.list_entries",
                        return_value=[]), \
                mock.patch("forge.browser.memory.list_pages",
                           return_value=[]):
            page = webuimod.memory_page()
        self.assertIn("No facts remembered yet", page)
        self.assertIn("No pages stored yet", page)


# ------------------------------------------------------------------
# G-S6: the local server's token gate + security headers.
# ------------------------------------------------------------------

class LocalServerTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-server-")
        self.history_path = os.path.join(self.tmp, "history.json")
        with open(self.history_path, "w", encoding="utf-8") as handle:
            json.dump([{"url": "http://example.org/", "title": "E",
                        "at": 1700000000}], handle)
        self.env = mock.patch.dict(os.environ, {
            "HOME": self.tmp,
            "FORGE_BROWSER_HISTORY_PATH": self.history_path,
            "FORGE_BROWSER_PAGE_MEMORY_PATH":
                os.path.join(self.tmp, "no-pages.json"),
            "FORGE_BROWSER_MEMORY_PATH":
                os.path.join(self.tmp, "no-memory.json"),
            "FORGE_BROWSER_BOOKMARKS_PATH":
                os.path.join(self.tmp, "no-bookmarks.json"),
        })
        self.env.start()
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0
        self.server = webuimod.LocalPageServer().start()

    def tearDown(self):
        self.server.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        webuimod._STATUS_CACHE["status"] = None
        webuimod._STATUS_CACHE["at"] = 0.0

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=10)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        headers = {k.lower(): v for k, v in response.getheaders()}
        conn.close()
        return response.status, headers, body

    def test_start_page_has_security_headers(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-content-type-options"),
                         "nosniff")
        self.assertIn("default-src 'self'",
                      headers.get("content-security-policy", ""))
        self.assertIn("Nothing leaves this machine", body)
        self.assertNotIn("nothing tracked", body.lower())

    def test_wipe_without_token_is_refused(self):
        status, _headers, body = self._get("/history-clear?confirm=1")
        self.assertEqual(status, 403)
        self.assertIn("History was not cleared", body)
        self.assertEqual(len(titlesmod.load_history()), 1)

    def test_wipe_with_token_works(self):
        status, _headers, body = self._get(
            "/history-clear?confirm=1&token=" + self.server.token)
        self.assertEqual(status, 200)
        self.assertIn("History cleared", body)
        self.assertEqual(titlesmod.load_history(), [])

    def test_confirm_page_carries_the_token(self):
        _status, _headers, body = self._get("/history-clear")
        self.assertIn("token=" + self.server.token, body)

    def test_brief_refresh_without_token_is_refused(self):
        status, _headers, body = self._get("/brief-refresh")
        self.assertEqual(status, 403)
        self.assertIn("Brief was not refreshed", body)

    def test_brief_refresh_with_token_reaches_the_page(self):
        with mock.patch("forge.browser.seats.seats_enabled",
                        return_value=False):
            status, _headers, body = self._get(
                "/brief-refresh?token=" + self.server.token)
        self.assertEqual(status, 200)
        self.assertIn("AI is off", body)

    def test_memory_page_serves(self):
        status, _headers, body = self._get("/memory")
        self.assertEqual(status, 200)
        self.assertIn("Memory", body)

    def test_error_page_has_security_headers(self):
        with mock.patch.object(webuimod, "start_page",
                               side_effect=RuntimeError("boom")):
            status, headers, _body = self._get("/")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("x-content-type-options"),
                         "nosniff")
        self.assertIn("default-src 'self'",
                      headers.get("content-security-policy", ""))

    def test_tokens_differ_per_launch(self):
        other = webuimod.LocalPageServer().start()
        try:
            self.assertNotEqual(other.token, self.server.token)
            self.assertEqual(len(self.server.token), 32)
        finally:
            other.stop()


# ------------------------------------------------------------------
# G-T6: the manual is found (or honestly missing).
# ------------------------------------------------------------------

class FindManualTests(unittest.TestCase):
    def test_app_root_candidate_wins(self):
        here = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(howtomod.__file__))))
        expected = os.path.join(here, "BROWSER.md")
        if not os.path.isfile(expected):
            self.skipTest("BROWSER.md is not at the app root here")
        self.assertEqual(howtomod.find_manual(), expected)

    def test_falls_back_to_cwd(self):
        tmp = tempfile.mkdtemp(prefix="forge-manual-")
        try:
            manual = os.path.join(tmp, "BROWSER.md")
            with open(manual, "w", encoding="utf-8") as handle:
                handle.write("# Manual\n")
            real_isfile = os.path.isfile

            def fake_isfile(path):
                if path == manual:
                    return True
                if path.endswith("BROWSER.md"):
                    return False
                return real_isfile(path)

            with mock.patch("os.path.isfile",
                            side_effect=fake_isfile), \
                    mock.patch("os.getcwd", return_value=tmp):
                self.assertEqual(howtomod.find_manual(), manual)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_none_when_nowhere(self):
        with mock.patch("os.path.isfile", return_value=False):
            self.assertIsNone(howtomod.find_manual())


# ------------------------------------------------------------------
# G-T7: the debug log is wired for the real app.
# ------------------------------------------------------------------

class DebugLogWiringTests(unittest.TestCase):
    def test_get_debug_log_wiring(self):
        from forge.browser import __version__
        tmp = tempfile.mkdtemp(prefix="forge-logwiring-")
        logger = logging.getLogger("debug_log.forge-browser")
        before = list(logger.handlers)
        old = appmod._DEBUG_LOG
        appmod._DEBUG_LOG = None
        try:
            with mock.patch.object(os.path, "expanduser",
                                   return_value=tmp):
                log = appmod.get_debug_log()
            self.assertEqual(log.app_name, "forge-browser")
            self.assertEqual(log.version, __version__)
            self.assertEqual(log.log_dir,
                             os.path.join(tmp, ".forge", "logs"))
            self.assertIn("ollama", log.services)
            self.assertIn("PySide6", log.dependencies)
            self.assertIn("PySide6-WebEngine", log.dependencies)
            log.action("wiring check")
            self.assertTrue(os.path.isfile(log.log_path))
        finally:
            added = [h for h in logger.handlers if h not in before]
            for handler in added:
                logger.removeHandler(handler)
                handler.close()
            appmod._DEBUG_LOG = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_debug_log_is_a_singleton(self):
        old = appmod._DEBUG_LOG
        appmod._DEBUG_LOG = None
        tmp = tempfile.mkdtemp(prefix="forge-logwiring-")
        logger = logging.getLogger("debug_log.forge-browser")
        before = list(logger.handlers)
        try:
            with mock.patch.object(os.path, "expanduser",
                                   return_value=tmp):
                first = appmod.get_debug_log()
                second = appmod.get_debug_log()
            self.assertIs(first, second)
        finally:
            added = [h for h in logger.handlers if h not in before]
            for handler in added:
                logger.removeHandler(handler)
                handler.close()
            appmod._DEBUG_LOG = old
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------
# G-T1: the offscreen smoke script exists and is runnable.
# ------------------------------------------------------------------

class SmokeScriptTests(unittest.TestCase):
    def test_smoke_script_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "smoke_offscreen.py")
        self.assertTrue(os.path.isfile(path), path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("create_window", text)
        self.assertIn("_take_single_instance", text)
        self.assertIn("offscreen", text)


# ------------------------------------------------------------------
# B3/U23 (pure): a destructive question defaults to No.
# ------------------------------------------------------------------

class AskYesNoDefaultTests(unittest.TestCase):
    def test_ask_yes_no_defaults_to_no(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "forge", "browser", "app.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("def _ask_yes_no")
        block = source[start:start + 1200]
        self.assertIn("setDefaultButton", block)
        self.assertIn("StandardButton.No", block)
        self.assertIn("queue=False", block)


# ------------------------------------------------------------------
# B2 (pure): the ember identity is flat and cheap.
# ------------------------------------------------------------------

class EmberStylesheetTests(unittest.TestCase):
    def test_ember_colors_present_no_blur(self):
        sheet = appmod.EMBER_STYLESHEET
        self.assertIn("#ffb347", sheet)
        self.assertNotIn("blur", sheet.lower())
        self.assertNotIn("gradient", sheet.lower())


# ------------------------------------------------------------------
# Qt-offscreen tests: the real window, driven under offscreen.
# They share one window so the suite stays fast; each test resets
# tabs and download state in setUp.
# ------------------------------------------------------------------

if HAVE_QT:
    class _DialogDriver:
        """Drives a modal dialog from a timer while exec() runs its
        own event loop. Steps run in order; each returns True when
        its action is done (False to retry next tick).

        A step must never click a button that opens ANOTHER modal
        dialog: a QTimer does not fire again while its own timeout
        slot is on the stack, so the driver would stall forever
        inside the nested exec (verified with a probe: the
        settings dialog hung the whole Qt class this way). Such a
        click goes through QtCore.QTimer.singleShot(0, btn.click)
        so the driver keeps ticking during the nested exec; the
        next step answers the nested box. Clicks that only CLOSE
        a box (Yes/No/OK/Close) are safe to do directly.
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

    class _FakeDownload:
        def __init__(self, received=0, total=0):
            self._received = received
            self._total = total
            self.cancelled = False

        def receivedBytes(self):
            return self._received

        def totalBytes(self):
            return self._total

        def cancel(self):
            self.cancelled = True


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed in this Python")
class QtWindowTests(unittest.TestCase):
    window = None
    server = None
    app = None
    home = None
    _home_patch = None

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="forge-qt-home-")
        cls._home_patch = mock.patch.dict(os.environ, {"HOME": cls.home})
        cls._home_patch.start()
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication([])
        cls.app = app
        cls.server = webuimod.LocalPageServer().start()
        profile = QWebEngineProfile("forge-qt-tests")
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
        window._autofill_asked.clear()
        window._autofill_done.clear()
        while window.tabs.count() > 1:
            window._close_tab(window.tabs.count() - 1)
        window._closed_tabs.clear()
        window.tabs.setCurrentIndex(0)
        self._pump(0.2)

    def _load_url(self, view, url, timeout=20.0):
        done = {"fired": False, "ok": None}

        def finished(ok):
            done["fired"] = True
            done["ok"] = ok

        view.loadFinished.connect(finished)
        try:
            view.load(QtCore.QUrl(url))
            self.assertTrue(
                self._wait_for(lambda: done["fired"], timeout),
                "loadFinished never fired for: %s" % url)
            self.assertTrue(
                done["ok"],
                "page failed to load: %s" % url)
        finally:
            try:
                view.loadFinished.disconnect(finished)
            except (TypeError, RuntimeError):
                pass

    def _wait_for_url(self, view, url, timeout=15.0):
        self.assertTrue(
            self._wait_for(
                lambda: view.url().toString() == url, timeout),
            "tab URL is %r, expected %r"
            % (view.url().toString(), url))

    # -- B1/B2: activity + identity ---------------------------------

    def test_ember_stylesheet_applied(self):
        sheet = self.window.styleSheet()
        self.assertIn("#ffb347", sheet)
        self.assertNotIn("blur", sheet.lower())

    def test_load_progress_bar(self):
        window = self.window
        bar = window.findChild(QtWidgets.QProgressBar, "load_progress")
        self.assertIsNotNone(bar)
        view = window._view()
        # Start from a settled state: the first tab may still be
        # loading its start page when this test runs.
        window._on_load_finished(view, ok=True)
        self.assertFalse(bar.isVisible())
        window._on_load_started(view)
        self.assertTrue(bar.isVisible())
        window._on_load_progress(view, 42)
        self.assertEqual(bar.value(), 42)
        window._on_load_finished(view, ok=True)
        self.assertFalse(bar.isVisible())

    def test_live_stats_label_exists(self):
        label = self.window.findChild(QtWidgets.QLabel, "live_stats")
        self.assertIsNotNone(label)
        self.assertIn("turns", label.text())

    # -- G-P1: the window paints before the brain pick lands ---------

    def test_brain_pick_is_async(self):
        import forge.browser.chat as chatmod
        real_check = chatmod.check_ollama

        def slow_check(*args, **kwargs):
            # The pick needs an Ollama round trip; slow it down so
            # the test is deterministic even when Ollama answers
            # instantly (it is up in some sandboxes).
            time.sleep(2.0)
            return real_check(*args, **kwargs)

        profile = QWebEngineProfile("forge-qt-p1")
        with mock.patch.object(chatmod, "check_ollama",
                               side_effect=slow_check):
            window = appmod.create_window(self.server, profile,
                                          self.server.home_url)
            try:
                window.show()
                self._pump(0.5)
                # The window is up before the (slowed) brain pick
                # could land.
                self.assertIsNone(window._chat_session)
                header = window._chat_dock.findChild(
                    QtWidgets.QLabel, "brain_header")
                self.assertIsNotNone(header)
                self.assertEqual(header.text(),
                                 "Checking for a brain\u2026")
                landed = self._wait_for(
                    lambda: window._chat_session is not None,
                    timeout=15.0)
                self.assertTrue(landed, "the brain pick never landed")
            finally:
                window.close()
                self._pump(0.2)

    # -- G-UX4: the chat dock closes, reopens, resizes, persists -----

    def test_chat_dock_close_reopen(self):
        window = self.window
        dock = window._chat_dock
        actions = [a for a in window._view_menu.actions()
                   if a.objectName() == "view_chat_dock"]
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertTrue(action.isCheckable())
        self.assertTrue(dock.isVisible())
        dock.close()
        self._pump(0.2)
        self.assertFalse(dock.isVisible())
        self.assertFalse(action.isChecked())
        action.trigger()
        self._pump(0.2)
        self.assertTrue(dock.isVisible())
        self.assertTrue(action.isChecked())

    def test_chat_dock_resize_and_persist(self):
        window = self.window
        dock = window._chat_dock
        window.resize(1200, 800)
        window.show()
        self._pump(0.5)
        # A sane width: never more than half the window.
        self.assertLessEqual(dock.width(), window.width() // 2)
        # Narrower.
        window.resizeDocks([dock], [260],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.3)
        self.assertLessEqual(dock.width(), 280)
        # Wider.
        window.resizeDocks([dock], [480],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.3)
        self.assertGreaterEqual(dock.width(), 460)
        # The floor holds: it cannot shrink below the minimum.
        window.resizeDocks([dock], [100],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.3)
        self.assertGreaterEqual(dock.width(),
                                appmod._CHAT_DOCK_MIN_WIDTH)
        # The size persists across a close and a restore.
        window.resizeDocks([dock], [400],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.3)
        self.assertAlmostEqual(dock.width(), 400, delta=2)
        window.closeEvent(QtGui.QCloseEvent())
        settings = QtCore.QSettings("ForgeBrowser", "Forge Browser")
        self.assertEqual(int(settings.value("chat_dock/width")), 400)
        window.resizeDocks([dock], [260],
                           QtCore.Qt.Orientation.Horizontal)
        self._pump(0.3)
        window._restore_chat_dock()
        self._pump(0.3)
        self.assertAlmostEqual(dock.width(), 400, delta=2)

    def test_chat_dock_has_no_width_pin(self):
        dock = self.window._chat_dock
        self.assertEqual(dock.minimumWidth(),
                         appmod._CHAT_DOCK_MIN_WIDTH)
        panel = dock.widget()
        self.assertEqual(panel.minimumWidth(), 0)
        # No maximum width on the panel either (16777215 is Qt's
        # "no maximum" sentinel).
        self.assertEqual(panel.maximumWidth(), 16777215)

    # -- G-UX1/G-UX2/G-UX3/G-UX11/G-UX12: menus, shortcuts, tabs -----

    def test_history_menu(self):
        bar = self.window.menuBar()
        menus = {action.text(): action.menu()
                 for action in bar.actions() if action.menu()}
        self.assertIn("&History", menus)
        items = {a.text(): a
                 for a in menus["&History"].actions()}
        self.assertIn("Show All History", items)
        self.assertEqual(
            items["Show All History"].shortcut().toString(), "Ctrl+H")
        self.assertIn("Clear History\u2026", items)

    def test_shortcuts(self):
        wanted = {"Ctrl+L", "Ctrl+R", "Ctrl+Tab", "Ctrl+Shift+Tab",
                  "Ctrl+Shift+T"}
        found = {shortcut.key().toString()
                 for shortcut in
                 self.window.findChildren(QtGui.QShortcut)}
        self.assertTrue(wanted <= found, "missing: %s" % (wanted - found))

    def test_single_bookmark_action(self):
        window = self.window
        self.assertIsNotNone(window._bookmark_action)
        window._rebuild_bookmarks_menu()
        window._rebuild_bookmarks_menu()
        bar = window.menuBar()
        menus = {a.text(): a.menu() for a in bar.actions()
                 if a.menu()}
        marks = [a for a in menus["&Bookmarks"].actions()
                 if a.text() == "Bookmark This Page"]
        self.assertEqual(len(marks), 1)
        self.assertIs(marks[0], window._bookmark_action)
        ctrl_d = [s for s in window.findChildren(QtGui.QShortcut)
                  if s.key().toString() == "Ctrl+D"]
        self.assertEqual(len(ctrl_d), 0)  # the action owns Ctrl+D

    def test_next_prev_tab_cycle(self):
        window = self.window
        for _ in range(2):
            window.new_tab()
        self.assertEqual(window.tabs.count(), 3)
        window.tabs.setCurrentIndex(0)
        window._next_tab()
        self.assertEqual(window.tabs.currentIndex(), 1)
        window._next_tab()
        window._next_tab()
        self.assertEqual(window.tabs.currentIndex(), 0)
        window._prev_tab()
        self.assertEqual(window.tabs.currentIndex(), 2)

    def test_reopen_closed_tab(self):
        window = self.window
        url = self.server.home_url + "x"
        view = window.new_tab(url)
        self._wait_for_url(view, url)
        self.assertEqual(window.tabs.count(), 2)
        window._close_tab(1)
        self.assertEqual(window.tabs.count(), 1)
        window._reopen_closed_tab()
        self.assertEqual(window.tabs.count(), 2)
        self._wait_for_url(window.tabs.widget(1), url)

    def test_close_last_tab_opens_start_tab(self):
        window = self.window
        self.assertEqual(window.tabs.count(), 1)
        window._close_tab(0)
        self.assertEqual(window.tabs.count(), 1)
        self._wait_for_url(window.tabs.widget(0),
                           self.server.home_url)

    def test_duplicate_and_close_other_tabs(self):
        window = self.window
        self.assertEqual(
            window.tabs.tabBar().contextMenuPolicy(),
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        window.new_tab(self.server.home_url + "a")
        window.new_tab(self.server.home_url + "b")
        self.assertEqual(window.tabs.count(), 3)
        self._wait_for_url(window.tabs.widget(1),
                           self.server.home_url + "a")
        window.tabs.setCurrentIndex(1)
        window._duplicate_tab(1)
        self.assertEqual(window.tabs.count(), 4)
        # The duplicate lands at the end of the tab strip.
        self._wait_for_url(window.tabs.widget(3),
                           self.server.home_url + "a")
        window._close_other_tabs(1)
        self.assertEqual(window.tabs.count(), 1)
        self.assertEqual(window.tabs.widget(0).url().toString(),
                         self.server.home_url + "a")

    # -- B3: one dialog at a time ------------------------------------

    def test_dialog_queue(self):
        window = self.window
        ran = []
        window._dialog_active = True
        try:
            result = window._run_dialog(lambda: ran.append("queued"))
            self.assertIsNone(result)
            self.assertEqual(ran, [])
            self.assertEqual(len(window._dialog_queue), 1)
            self.assertIn("waiting behind",
                          window.statusBar().currentMessage())
            self.assertFalse(window._ask_yes_no("Q?", "Do it?"))
        finally:
            window._dialog_active = False
        window._run_dialog(lambda: ran.append("first"))
        self.assertEqual(ran, ["first"])
        self.assertTrue(self._wait_for(lambda: ran == ["first", "queued"],
                                       timeout=2.0))
        self.assertEqual(window._dialog_queue, [])

    # -- G-UX6: a failed load is a friendly page, not history --------

    def test_failed_load_records_no_history(self):
        window = self.window
        view = window._view()
        calls = []
        with mock.patch("forge.browser.titles.record_visit",
                        side_effect=lambda *a: calls.append(a)):
            window._on_load_finished(view, ok=False)
            self.assertEqual(calls, [])
            window._on_load_finished(view, ok=True)
            self.assertEqual(len(calls), 1)

    # -- B10: actions are logged -------------------------------------

    def test_go_logs_action(self):
        window = self.window
        log = appmod.get_debug_log()
        before = len(log._ring.records)
        window.address.setText(self.server.home_url + "logged")
        window._go()
        lines = list(log._ring.records)[before:]
        self.assertTrue(any("Opened:" in line for line in lines),
                        "no 'Opened:' line in %r" % lines)

    # -- G-C20: background jobs never take the turn session ----------

    def test_fast_seat_session_is_not_the_turn_session(self):
        from forge.browser.chat import ChatSession
        window = self.window
        fake = types.SimpleNamespace(model="qwen3:1.7b")
        original = window._chat_session
        window._chat_session = fake
        try:
            seat = window._fast_seat_session()
            self.assertIsNotNone(seat)
            self.assertIsNot(seat, fake)
            self.assertIsInstance(seat, ChatSession)
        finally:
            # Only restore when the pick has not landed meanwhile.
            if window._chat_session is fake:
                window._chat_session = original

    # -- G-UX14: find shows the match count --------------------------

    def test_find_match_count(self):
        window = self.window
        label = window._find_label
        result = types.SimpleNamespace(
            numberOfMatches=lambda: 5, activeMatch=lambda: 2)
        window._on_find_result(result)
        self.assertEqual(label.text(), "2 of 5 matches")
        result = types.SimpleNamespace(
            numberOfMatches=lambda: 0, activeMatch=lambda: 0)
        window._on_find_result(result)
        self.assertEqual(label.text(), "No matches")
        window._on_find_result(True)
        self.assertEqual(label.text(), "")
        window._on_find_result(False)
        self.assertEqual(label.text(), "No matches")

    # -- G-UX9: the chat entry is multi-line -------------------------

    def test_entry_is_multiline_and_sends_on_enter(self):
        window = self.window
        entry = window._chat_dock.findChild(QtWidgets.QPlainTextEdit,
                                            "chat_entry")
        self.assertIsNotNone(entry)
        fired = []
        entry.send_requested.connect(lambda: fired.append(True))
        entry.setText("")
        QtTest.QTest.keyClick(entry, QtCore.Qt.Key.Key_Return)
        self.assertEqual(fired, [True])
        # Shift+Enter is a newline, not a send.
        QtTest.QTest.keyClick(entry, QtCore.Qt.Key.Key_Return,
                              QtCore.Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(fired, [True])
        self.assertIn("\n", entry.text())

    # -- B8/B13: seat descriptions + collapsible models --------------

    def test_seat_descriptions(self):
        dock = self.window._chat_dock
        desc = dock.findChild(QtWidgets.QLabel, "brain_desc")
        self.assertIsNotNone(desc)
        self.assertIn("Brain", desc.text())
        self.assertIn("answers questions", desc.text())

    def test_models_collapsible(self):
        dock = self.window._chat_dock
        toggle = dock.findChild(QtWidgets.QToolButton, "models_toggle")
        box = dock.findChild(QtWidgets.QWidget, "models_box")
        self.assertIsNotNone(toggle)
        self.assertIsNotNone(box)
        self.assertTrue(box.isVisible())
        toggle.click()
        self.assertFalse(box.isVisible())
        toggle.click()
        self.assertTrue(box.isVisible())

    # -- G-UX5: fix buttons appear when the brain is down ------------

    def test_start_ollama_button_when_brain_down(self):
        import forge.browser.chat as chatmod
        window = self.window
        self.assertTrue(
            self._wait_for(lambda: window._chat_session is not None,
                           timeout=25.0),
            "the background brain pick never landed")
        # Ollama may be up in this sandbox, so drive the brain
        # down deterministically: re-pick with the Ollama check
        # patched to fail, via the dock's refresh button.
        dock = window._chat_dock
        refresh = dock.findChild(QtWidgets.QPushButton,
                                 "refresh_seats")
        self.assertIsNotNone(refresh)
        with mock.patch.object(chatmod, "check_ollama",
                               return_value=(False, [])):
            refresh.click()
            down = self._wait_for(
                lambda: window._chat_session.brain_state
                == "ollama-down",
                timeout=25.0)
            self.assertTrue(down, "the brain never went down")
        self._pump(0.5)
        self.assertEqual(window._chat_session.brain_state, "ollama-down")
        start_btn = dock.findChild(QtWidgets.QPushButton,
                                   "start_ollama_button")
        self.assertIsNotNone(start_btn)
        self.assertTrue(start_btn.isVisible())
        get_btn = dock.findChild(QtWidgets.QPushButton,
                                 "get_model_button")
        self.assertIsNotNone(get_btn)
        self.assertFalse(get_btn.isVisible())
        # The brain stays down for the rest of the class; no later
        # test reads its state.

    # -- G-UX7/G-P20/G-C4/G-UX10: downloads --------------------------

    def _record(self, name, status="downloading", url="",
                tmp="", folder=""):
        return {"download": _FakeDownload(10, 100), "name": name,
                "folder": folder or self.home, "tmp": tmp,
                "status": status, "url": url, "error": "",
                "retry_received": 0, "retry_total": 0}

    def test_download_list_updates_in_place(self):
        window = self.window
        window._open_downloads_dialog()
        self._pump(0.3)
        listing = window._downloads_list
        self.assertIsNotNone(listing)
        window._downloads["a"] = self._record("a.bin")
        window._downloads["b"] = self._record("b.bin")
        window._refresh_downloads_list()
        self.assertEqual(listing.count(), 2)
        first_item = listing.item(0)
        listing.setCurrentRow(0)
        window._downloads["a"]["download"]._received = 50
        window._refresh_downloads_list()
        self.assertIs(listing.item(0), first_item)
        self.assertEqual(listing.currentRow(), 0)
        self.assertIn("50%", first_item.text())
        window._downloads["a"]["download"]._received = 100
        window._refresh_downloads_list()
        self.assertIs(listing.item(0), first_item)
        self.assertIn("100%", first_item.text())

    def test_download_prune(self):
        window = self.window
        for i in range(60):
            window._downloads["done-%d" % i] = self._record(
                "f%d.bin" % i, status="done")
        window._downloads["active-1"] = self._record("live1.bin")
        window._downloads["active-2"] = self._record("live2.bin")
        window._prune_downloads()
        finished = [r for r in window._downloads.values()
                    if r["status"] == "done"]
        self.assertEqual(len(finished), appmod._DOWNLOADS_KEEP)
        self.assertIn("active-1", window._downloads)
        self.assertIn("active-2", window._downloads)

    def test_download_save_failure_is_not_stuck(self):
        from forge.browser.errors import BrowserError
        window = self.window
        tmp = os.path.join(self.home, "partial.bin.part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        window._downloads["k"] = self._record("f.bin", tmp=tmp)
        from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
        with mock.patch("forge.browser.files.keep_download",
                        side_effect=BrowserError("disk full")):
            window._on_download_state(
                "k", QWebEngineDownloadRequest.DownloadState
                .DownloadCompleted)
        record = window._downloads["k"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("disk full", record["error"])
        self.assertFalse(os.path.exists(tmp))

    def test_download_interrupt_keeps_the_partial_file(self):
        window = self.window
        tmp = os.path.join(self.home, "partial2.bin.part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        window._downloads["k"] = self._record(
            "g.bin", tmp=tmp, url="http://example.org/g.bin")
        from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
        window._on_download_state(
            "k", QWebEngineDownloadRequest.DownloadState
            .DownloadInterrupted)
        record = window._downloads["k"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "the connection dropped")
        self.assertTrue(os.path.exists(tmp))

    def test_download_retry_resumes(self):
        window = self.window
        tmp = os.path.join(self.home, "partial3.bin.part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        final = os.path.join(self.home, "h.bin")
        # The retry signals carry the record key as an int, like
        # the real app (id(download)); a str key never converts
        # and the done slot never fires.
        key = 4242
        window._downloads[key] = self._record(
            "h.bin", status="failed", tmp=tmp,
            url="http://example.org/h.bin")

        def fake_fetch(url, path, resume=True, on_progress=None):
            self.assertTrue(resume)
            if on_progress:
                on_progress(100, 100)
            return url, {}, 100

        with mock.patch("forge.browser._http.http_get_to_file",
                        side_effect=fake_fetch), \
                mock.patch("forge.browser.files.keep_download",
                           return_value=final):
            window._retry_download(key)
            self.assertEqual(window._downloads[key]["status"],
                             "retrying")
            self.assertTrue(
                self._wait_for(
                    lambda: window._downloads[key]["status"] == "done",
                    timeout=5.0))
        self.assertEqual(window._downloads[key]["final"], final)

    def test_download_buttons_enablement(self):
        window = self.window
        window._open_downloads_dialog()
        self._pump(0.3)
        done = self._record("done.bin", status="done")
        done["final"] = os.path.join(self.home, "done.bin")
        failed = self._record("failed.bin", status="failed",
                              url="http://example.org/failed.bin")
        window._downloads["done"] = done
        window._downloads["failed"] = failed
        window._downloads["live"] = self._record("live.bin")
        window._refresh_downloads_list()
        listing = window._downloads_list
        buttons = window._download_buttons
        self.assertEqual(
            {b.objectName() for b in buttons.values()},
            {"downloads_open", "downloads_show", "downloads_retry",
             "downloads_cancel"})
        # A finished file can be opened or shown, not retried.
        listing.setCurrentRow(0)
        window._update_download_buttons()
        self.assertTrue(buttons["open"].isEnabled())
        self.assertTrue(buttons["show"].isEnabled())
        self.assertFalse(buttons["retry"].isEnabled())
        self.assertFalse(buttons["cancel"].isEnabled())
        # A failed file can be retried, nothing else.
        listing.setCurrentRow(1)
        window._update_download_buttons()
        self.assertFalse(buttons["open"].isEnabled())
        self.assertFalse(buttons["show"].isEnabled())
        self.assertTrue(buttons["retry"].isEnabled())
        self.assertFalse(buttons["cancel"].isEnabled())
        # A live download can only be cancelled.
        listing.setCurrentRow(2)
        window._update_download_buttons()
        self.assertFalse(buttons["open"].isEnabled())
        self.assertFalse(buttons["retry"].isEnabled())
        self.assertTrue(buttons["cancel"].isEnabled())

    # -- G-C5: a failed comparison refresh does not stick -------------

    def test_comparison_refresh_failure_clears(self):
        window = self.window
        home_url = self.server.home_url
        # The first tab already points at the start page; wait for
        # its URL to commit rather than re-loading it.
        if window._view().url().toString() != home_url:
            self._load_url(window._view(), home_url)
        else:
            self._wait_for_url(window._view(), home_url)
        rows = [{"url": home_url, "title": "Home"}]
        fields = ["title", "url"]
        compare_patcher = mock.patch(
            "forge.browser.compare.compare_tabs",
            side_effect=RuntimeError("compare boom"))
        last_patcher = mock.patch(
            "forge.browser.compare.last_comparison",
            return_value=(rows, fields))
        original_session = window._chat_session
        dummy = object()
        window._chat_session = dummy
        view = window.new_tab()
        try:
            compare_patcher.start()
            last_patcher.start()
            window._refresh_comparison(view)
            self.assertTrue(
                self._wait_for(
                    lambda: id(view) not in window._refreshing_views,
                    timeout=10.0),
                "the refresh never cleared _refreshing_views")
            # The error page lands via a queued signal after the
            # discard, so poll the tab's HTML until it appears.
            latest = {"html": ""}

            def fetch_error_page():
                view.page().toHtml(
                    lambda html: latest.__setitem__("html", html))
                return ("Could not refresh the comparison"
                        in latest["html"])

            self.assertTrue(
                self._wait_for(fetch_error_page, timeout=10.0),
                "the error page never appeared; last HTML: %r"
                % latest["html"][:200])
        finally:
            compare_patcher.stop()
            last_patcher.stop()
            if window._chat_session is dummy:
                window._chat_session = original_session

    # -- G-S4: agent-made HTML in a tab ------------------------------

    def test_new_tab_html_links_are_hardened(self):
        # A tab opened with raw HTML (a compare table, a research
        # report) gets rel="noopener" on its links before the
        # view renders it.
        window = self.window
        view = window.new_tab(
            html='<a href="http://example.org/">Example</a>',
            agent=True)
        latest = {"html": ""}

        def fetch():
            view.page().toHtml(
                lambda html: latest.__setitem__("html", html))
            return 'rel="noopener"' in latest["html"]

        self.assertTrue(
            self._wait_for(fetch, timeout=10.0),
            "rel=noopener never appeared in the tab HTML; last: %r"
            % latest["html"][:200])

    # -- B12/B15: the native history + research windows --------------

    def test_history_window(self):
        window = self.window
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["caps"] = _label_with(
                dialog, "History keeps the newest 200 visits") is not None
            seen["list"] = dialog.findChild(
                QtWidgets.QListWidget, "history_list") is not None
            seen["search"] = dialog.findChild(
                QtWidgets.QLineEdit, "history_search") is not None
            return True

        def close():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            button = _button(dialog, "Close")
            if button is None:
                return False
            button.click()
            return True

        driver = _DialogDriver([check, close])
        driver.start()
        try:
            window._open_history_window()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertTrue(seen.get("caps"), "cap label missing: %r" % seen)
        self.assertTrue(seen.get("list"), "list missing: %r" % seen)
        self.assertTrue(seen.get("search"), "search missing: %r" % seen)

    def test_research_window(self):
        window = self.window
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["title"] = dialog.windowTitle()
            seen["list"] = dialog.findChild(
                QtWidgets.QListWidget, "research_list") is not None
            return True

        def close():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            button = _button(dialog, "Close")
            if button is None:
                return False
            button.click()
            return True

        driver = _DialogDriver([check, close])
        driver.start()
        try:
            window._open_research_window()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertEqual(seen.get("title"), "Research Reports")
        self.assertTrue(seen.get("list"))

    # -- B15/G-UX18: the memory dialog -------------------------------

    def test_memory_dialog(self):
        window = self.window
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["caps"] = _label_with(
                dialog, "Keeps the newest 200 facts") is not None
            seen["sites"] = dialog.findChild(
                QtWidgets.QListWidget, "memory_sites") is not None
            seen["block"] = dialog.findChild(
                QtWidgets.QPushButton,
                "memory_block_button") is not None
            seen["unblock"] = dialog.findChild(
                QtWidgets.QPushButton,
                "memory_unblock_button") is not None
            return True

        def close():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            button = _button(dialog, "Close")
            if button is None:
                return False
            button.click()
            return True

        driver = _DialogDriver([check, close])
        driver.start()
        try:
            button = window._chat_dock.findChild(
                QtWidgets.QPushButton, "memory_button")
            self.assertIsNotNone(button)
            button.click()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertTrue(seen.get("caps"), "caps missing: %r" % seen)
        self.assertTrue(seen.get("sites"), "sites missing: %r" % seen)
        self.assertTrue(seen.get("block"), "block missing: %r" % seen)
        self.assertTrue(seen.get("unblock"), "unblock missing: %r" % seen)

    # -- B15: a failed forget shows a plain message, not a trace ---

    def test_forget_fact_failure_shows_message(self):
        from forge.browser import memory
        from forge.browser.errors import BrowserError
        window = self.window
        fact = "A fact the disk refuses to forget"
        memory.remember(fact)
        seen = {}
        try:
            def select_and_forget():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                facts = dialog.findChild(QtWidgets.QListWidget,
                                         "memory_facts")
                if facts is None:
                    return False
                for row in range(facts.count()):
                    if fact in facts.item(row).text():
                        facts.setCurrentRow(row)
                        break
                else:
                    return False
                button = dialog.findChild(
                    QtWidgets.QPushButton, "memory_forget_button")
                if button is None:
                    return False
                # Forget opens a confirm box: click via singleShot
                # (see _DialogDriver).
                QtCore.QTimer.singleShot(0, button.click)
                return True

            def answer_yes():
                box = _modal_dialog()
                if box is None or not isinstance(
                        box, QtWidgets.QMessageBox):
                    return False
                yes = box.button(
                    QtWidgets.QMessageBox.StandardButton.Yes)
                if yes is None:
                    return False
                # Yes runs the forget, which fails and opens the
                # warning box: click via singleShot too.
                QtCore.QTimer.singleShot(0, yes.click)
                return True

            def record_warning():
                box = _modal_dialog()
                if box is None or not isinstance(
                        box, QtWidgets.QMessageBox):
                    return False
                if "still remembered" not in box.text():
                    return False
                seen["warning"] = box.text()
                ok = box.button(
                    QtWidgets.QMessageBox.StandardButton.Ok)
                if ok is None:
                    return False
                ok.click()
                return True

            def close():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                button = _button(dialog, "Close")
                if button is None:
                    return False
                button.click()
                return True

            driver = _DialogDriver([select_and_forget, answer_yes,
                                    record_warning, close])
            driver.start()
            try:
                # BRAIN's B15 stores raise BrowserError when the
                # disk write fails; the wrap in forget_fact must
                # turn that into a plain message.
                with mock.patch.object(
                        memory, "forget",
                        side_effect=BrowserError("disk full")):
                    button = window._chat_dock.findChild(
                        QtWidgets.QPushButton, "memory_button")
                    self.assertIsNotNone(button)
                    button.click()
            finally:
                driver.stop()
            self.assertEqual(driver.errors, [])
            self.assertIn("still remembered",
                          seen.get("warning", ""))
            self.assertTrue(
                any(fact in entry.get("text", "")
                    for entry in memory.list_entries()),
                "the fact should still be remembered")
        finally:
            memory.forget(fact)

    def test_remove_page_failure_shows_message(self):
        from forge.browser import memory
        from forge.browser.errors import BrowserError
        window = self.window
        url = "https://example.org/kept-page"
        title = "Kept Page"
        stored = memory.remember_page(
            url, title, "A page kept in memory. " * 20)
        self.assertTrue(stored, "the page should be stored")
        seen = {}
        try:
            def select_and_remove():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                pages = dialog.findChild(QtWidgets.QListWidget,
                                         "memory_pages")
                if pages is None:
                    return False
                for row in range(pages.count()):
                    if title in pages.item(row).text():
                        pages.setCurrentRow(row)
                        break
                else:
                    return False
                button = dialog.findChild(
                    QtWidgets.QPushButton, "memory_remove_button")
                if button is None:
                    return False
                # Remove opens a confirm box: click via singleShot
                # (see _DialogDriver).
                QtCore.QTimer.singleShot(0, button.click)
                return True

            def answer_yes():
                box = _modal_dialog()
                if box is None or not isinstance(
                        box, QtWidgets.QMessageBox):
                    return False
                yes = box.button(
                    QtWidgets.QMessageBox.StandardButton.Yes)
                if yes is None:
                    return False
                # Yes runs the remove, which fails and opens the
                # warning box: click via singleShot too.
                QtCore.QTimer.singleShot(0, yes.click)
                return True

            def record_warning():
                box = _modal_dialog()
                if box is None or not isinstance(
                        box, QtWidgets.QMessageBox):
                    return False
                if "still stored" not in box.text():
                    return False
                seen["warning"] = box.text()
                ok = box.button(
                    QtWidgets.QMessageBox.StandardButton.Ok)
                if ok is None:
                    return False
                ok.click()
                return True

            def close():
                dialog = _modal_dialog()
                if dialog is None:
                    return False
                button = _button(dialog, "Close")
                if button is None:
                    return False
                button.click()
                return True

            driver = _DialogDriver([select_and_remove, answer_yes,
                                    record_warning, close])
            driver.start()
            try:
                with mock.patch.object(
                        memory, "forget_page",
                        side_effect=BrowserError("disk full")):
                    button = window._chat_dock.findChild(
                        QtWidgets.QPushButton, "memory_button")
                    self.assertIsNotNone(button)
                    button.click()
            finally:
                driver.stop()
            self.assertEqual(driver.errors, [])
            self.assertIn("still stored", seen.get("warning", ""))
            self.assertTrue(
                any(page.get("url") == url
                    for page in memory.list_pages()),
                "the page should still be stored")
        finally:
            memory.forget_page(url)

    # -- B4: the vault window (create, lock, unlock) -----------------

    def test_vault_window_create_lock_unlock(self):
        from forge.browser.vault import default_vault_path
        window = self.window
        vault_path = default_vault_path()
        if os.path.exists(vault_path):
            self.skipTest("a vault already exists in this HOME")
        seen = {}
        passphrase = "test-passphrase-1"

        def create():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            new = dialog.findChild(QtWidgets.QLineEdit,
                                   "vault_new_passphrase")
            confirm = dialog.findChild(QtWidgets.QLineEdit,
                                       "vault_confirm_passphrase")
            if new is None or confirm is None:
                return False
            new.setText(passphrase)
            confirm.setText(passphrase)
            button = _button(dialog, "Create vault")
            if button is None:
                return False
            button.click()
            return True

        def lock():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["created"] = os.path.exists(vault_path)
            seen["vault_open"] = window._vault is not None
            button = _button(dialog, "Lock vault")
            if button is None:
                return False
            button.click()
            return True

        def unlock():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["locked"] = window._vault is None
            field = dialog.findChild(QtWidgets.QLineEdit,
                                     "vault_passphrase")
            if field is None:
                return False
            field.setText(passphrase)
            button = _button(dialog, "Unlock")
            if button is None:
                return False
            button.click()
            return True

        def close():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["reopened"] = window._vault is not None
            button = _button(dialog, "Close")
            if button is None:
                return False
            button.click()
            return True

        # The vault's PBKDF2 takes ~9s to create and ~13s to open
        # on this slow sandbox CPU, and both run on the GUI thread
        # inside the driver's clicks — the default 12s timeout
        # expires mid-unlock. 60s covers both with margin.
        driver = _DialogDriver([create, lock, unlock, close],
                               timeout=60.0)
        driver.start()
        try:
            window._open_vault_window()
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertTrue(seen.get("created"), "vault file: %r" % seen)
        self.assertTrue(seen.get("vault_open"), "vault open: %r" % seen)
        self.assertTrue(seen.get("locked"), "lock: %r" % seen)
        self.assertTrue(seen.get("reopened"), "reopen: %r" % seen)
        # Leave the vault locked for the other tests.
        window._vault = None

    # -- B11/G-S3/G-UX4: the settings dialog -------------------------

    def test_settings_dialog(self):
        from forge.browser.settings import get_setting
        window = self.window
        seen = {}

        def check():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["no_dwell"] = dialog.findChild(
                QtWidgets.QWidget,
                "settings_preview_dwell_ms") is None
            seen["chat_label"] = _checkbox_with(
                dialog, "Chat and brain jobs") is not None
            box = dialog.findChild(QtWidgets.QWidget,
                                   "settings_advanced_box")
            seen["advanced_hidden"] = box is not None and (
                not box.isVisible())
            return True

        def show_advanced():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            button = _button(dialog, "Advanced\u2026")
            if button is None:
                return False
            button.click()
            box = dialog.findChild(QtWidgets.QWidget,
                                   "settings_advanced_box")
            seen["advanced_shown"] = box.isVisible()
            return True

        def bad_address():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            field = dialog.findChild(QtWidgets.QLineEdit,
                                     "settings_ollama_url")
            field.setText("not a url")
            _button(dialog, "Save").click()
            error = dialog.findChild(QtWidgets.QLabel,
                                     "settings_error")
            # "not a url" is rejected for its spaces, so the label
            # carries an explanation rather than a scheme hint.
            seen["bad_rejected"] = (
                error is not None and error.text() != "")
            seen["still_open"] = _modal_dialog() is dialog
            return True

        def remote_address():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            field = dialog.findChild(QtWidgets.QLineEdit,
                                     "settings_ollama_url")
            field.setText("http://192.168.1.9:11434")
            # The Save click opens a nested question box. A QTimer
            # never fires again while its own timeout slot is on
            # the stack, so clicking here would stall the driver
            # forever (the question would never be answered). The
            # click goes through a singleShot instead: the driver's
            # timer keeps firing during the nested exec and the
            # answer_no step can close the question.
            save = _button(dialog, "Save")
            QtCore.QTimer.singleShot(0, save.click)
            return True

        def answer_no():
            box = _modal_dialog()
            if box is None or not isinstance(
                    box, QtWidgets.QMessageBox):
                return False
            seen["remote_asked"] = "not this machine" in box.text()
            no = box.button(QtWidgets.QMessageBox.StandardButton.No)
            self.assertIsNotNone(no)
            no.click()
            return True

        def good_address():
            dialog = _modal_dialog()
            if dialog is None:
                return False
            seen["not_saved"] = get_setting(
                "ollama_url") != "http://192.168.1.9:11434"
            field = dialog.findChild(QtWidgets.QLineEdit,
                                     "settings_ollama_url")
            field.setText("http://127.0.0.1:11434")
            _button(dialog, "Save").click()
            return True

        driver = _DialogDriver([check, show_advanced, bad_address,
                                remote_address, answer_no,
                                good_address])
        driver.start()
        try:
            appmod.open_settings_dialog(window)
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertTrue(seen.get("no_dwell"), "dwell: %r" % seen)
        self.assertTrue(seen.get("chat_label"), "chat label: %r" % seen)
        self.assertTrue(seen.get("advanced_hidden"),
                        "advanced hidden: %r" % seen)
        self.assertTrue(seen.get("advanced_shown"),
                        "advanced shown: %r" % seen)
        self.assertTrue(seen.get("bad_rejected"), "bad: %r" % seen)
        self.assertTrue(seen.get("still_open"), "still open: %r" % seen)
        self.assertTrue(seen.get("remote_asked"), "asked: %r" % seen)
        self.assertTrue(seen.get("not_saved"), "not saved: %r" % seen)
        self.assertEqual(get_setting("ollama_url"),
                         "http://127.0.0.1:11434")

    # -- G-S10: autofill re-verifies the tab before filling ----------

    def test_autofill_reverifies_before_filling(self):
        from forge.browser.vault import Vault, default_vault_path
        window = self.window
        vault_path = os.path.join(self.home, "autofill-vault.json")
        env = mock.patch.dict(
            os.environ, {"FORGE_BROWSER_VAULT": vault_path})
        env.start()
        try:
            self.assertEqual(default_vault_path(), vault_path)
            Vault.create(vault_path, "test-passphrase-1")
            vault = Vault.open(vault_path, "test-passphrase-1")
            vault.set("example.org", "josh", "secret123")
            window._vault = vault
            view = window._view()
            self._load_url(view, self.server.home_url)
            # The tab shows the local server, not example.org.

            def answer_yes():
                box = _modal_dialog()
                if box is None or not isinstance(
                        box, QtWidgets.QMessageBox):
                    return False
                yes = box.button(
                    QtWidgets.QMessageBox.StandardButton.Yes)
                if yes is None:
                    return False
                yes.click()
                return True

            driver = _DialogDriver([answer_yes])
            driver.start()
            try:
                window._on_login_form_found(
                    view, "https://example.org/login", "example.org",
                    "login")
            finally:
                driver.stop()
            self.assertEqual(driver.errors, [])
            self.assertIn("changed before the login",
                          window.statusBar().currentMessage())
            self.assertNotIn("example.org", window._autofill_done)
        finally:
            window._vault = None
            env.stop()
            try:
                os.unlink(vault_path)
            except OSError:
                pass

    # -- G-UX15: password-only forms get the offer too ---------------

    def test_password_only_autofill_offer(self):
        from forge.browser.vault import Vault
        window = self.window
        vault_path = os.path.join(self.home, "pwonly-vault.json")
        env = mock.patch.dict(
            os.environ, {"FORGE_BROWSER_VAULT": vault_path})
        env.start()
        try:
            Vault.create(vault_path, "test-passphrase-1")
            vault = Vault.open(vault_path, "test-passphrase-1")
            vault.set("example.org", "josh", "secret123")
            window._vault = vault
            view = window._view()
            spy = mock.Mock(return_value=False)
            with mock.patch.object(window, "_ask_yes_no", spy):
                window._on_login_form_found(
                    view, "https://example.org/login", "example.org",
                    "password-only")
                self.assertEqual(spy.call_count, 1)
                title, text = spy.call_args[0]
                self.assertIn("Fill the saved password", text)
                self.assertNotIn("Username:", text)
                window._autofill_asked.discard("example.org")
                window._on_login_form_found(
                    view, "https://example.org/login", "example.org",
                    "login")
                self.assertEqual(spy.call_count, 2)
                _title, text = spy.call_args[0]
                self.assertIn("Fill the saved login", text)
                self.assertIn("Username: josh", text)
                # An unknown form kind gets no offer at all.
                window._autofill_asked.discard("example.org")
                window._on_login_form_found(
                    view, "https://example.org/login", "example.org",
                    "search-box")
                self.assertEqual(spy.call_count, 2)
        finally:
            window._vault = None
            env.stop()
            try:
                os.unlink(vault_path)
            except OSError:
                pass
