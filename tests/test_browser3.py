"""Tests for the AI browser's chat brain (Item: chat interface).

The brain and the engine hands are stubbed — these tests pin the
protocol: ACTION parsing, the action loop and its cap, results
fed back to the model, tab-open requests, and the no-brain error.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import chat
from forge.browser.errors import BrowserError


class ParseTests(unittest.TestCase):
    def test_action_line_parsed(self):
        action = chat.parse_action('Let me look.\nACTION {"type": "search", "query": "gears"}')
        self.assertEqual(action, {"type": "search", "query": "gears"})

    def test_no_action_and_bad_json(self):
        self.assertIsNone(chat.parse_action("Just an answer."))
        self.assertIsNone(chat.parse_action("ACTION {not json"))
        self.assertIsNone(chat.parse_action('ACTION {"nope": 1}'))

    def test_visible_text_strips_actions(self):
        text = chat.visible_text('ACTION {"type": "read_page"}\nThe answer.')
        self.assertEqual(text, "The answer.")


class PickModelTests(unittest.TestCase):
    def test_preferred_name_and_embed_skip(self):
        models = ["nomic-embed-text", "qwen3:4b", "llama3.2:3b"]
        self.assertEqual(chat.pick_chat_model(models), "qwen3:4b")

    def test_qwen35_4b_wins_over_qwen3_4b(self):
        # Preference order beats list order: the brain+vision model first.
        models = ["qwen3:4b", "qwen3.5:4b", "llama3.2:3b"]
        self.assertEqual(chat.pick_chat_model(models), "qwen3.5:4b")

    def test_4b_wins_over_17b(self):
        models = ["qwen3:1.7b", "qwen3:4b"]
        self.assertEqual(chat.pick_chat_model(models), "qwen3:4b")

    def test_instruct_variant_counts_as_4b(self):
        models = ["qwen3:4b-instruct-2507", "llama3.2:3b"]
        self.assertEqual(chat.pick_chat_model(models),
                         "qwen3:4b-instruct-2507")

    def test_family_fallback_when_no_preferred_match(self):
        models = ["llama3.2:3b", "gemma3:4b"]
        self.assertEqual(chat.pick_chat_model(models), "llama3.2:3b")

    def test_none_when_empty(self):
        self.assertIsNone(chat.pick_chat_model([]))

    def test_env_wins(self):
        os.environ["FORGE_BROWSER_CHAT_MODEL"] = "my-model:7b"
        try:
            self.assertEqual(chat.pick_chat_model(["qwen3:4b"]), "my-model:7b")
        finally:
            del os.environ["FORGE_BROWSER_CHAT_MODEL"]


class ChatCallTests(unittest.TestCase):
    def test_call_disables_thinking_and_caps_context(self):
        import json as _json
        import urllib.request as _urlreq
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps(
                    {"message": {"content": "hi"}}).encode("utf-8")

        original = _urlreq.urlopen

        def fake_urlopen(request, timeout=None):
            seen["data"] = _json.loads(request.data.decode("utf-8"))
            return Resp()

        _urlreq.urlopen = fake_urlopen
        try:
            session = chat.ChatSession(model="qwen3.5:4b")
            self.assertEqual(
                session._chat([{"role": "user", "content": "hi"}]), "hi")
        finally:
            _urlreq.urlopen = original
        self.assertEqual(seen["data"]["model"], "qwen3.5:4b")
        self.assertIs(seen["data"]["stream"], False)
        self.assertIs(seen["data"]["think"], False)
        self.assertEqual(seen["data"]["options"], {"num_ctx": 8192})

    def test_streaming_delivers_chunks_in_order(self):
        import json as _json
        import urllib.request as _urlreq
        seen = {}
        lines = [
            b'{"message": {"content": "Hel"}}\n',
            b'\n',
            b'{"message": {"content": "lo "}}\n',
            b'{"message": {"content": "world"}}\n',
            b'{"done": true}\n',
            b'\n',
        ]

        class StreamResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def readline(self):
                return lines.pop(0) if lines else b""

        original = _urlreq.urlopen

        def fake_urlopen(request, timeout=None):
            seen["data"] = _json.loads(request.data.decode("utf-8"))
            return StreamResp()

        _urlreq.urlopen = fake_urlopen
        chunks = []
        try:
            session = chat.ChatSession(model="qwen3.5:4b")
            result = session._chat([{"role": "user", "content": "hi"}],
                                   on_chunk=chunks.append)
        finally:
            _urlreq.urlopen = original
        self.assertEqual(result, "Hello world")
        self.assertEqual(chunks, ["Hel", "lo ", "world"])
        self.assertIs(seen["data"]["stream"], True)
        self.assertIs(seen["data"]["think"], False)
        self.assertEqual(seen["data"]["options"], {"num_ctx": 8192})


class LiveDisplayTests(unittest.TestCase):
    def test_action_lines_never_shown(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed('Let me look.\nACTION {"type": "search"}\n'), "Let me look.\n")
        self.assertEqual(display.flush(), "")

    def test_partial_line_held_until_newline(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("Hel"), "")
        self.assertEqual(display.feed("lo world"), "")
        self.assertEqual(display.feed("\nnext"), "Hello world\n")
        self.assertEqual(display.flush(), "next")

    def test_action_line_split_across_chunks(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed('ACT'), "")
        self.assertEqual(display.feed('ION {"type": "open"}\nDone.'), "")
        self.assertEqual(display.flush(), "Done.")

    def test_plain_text_passes_through(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("one\ntwo\n"), "one\ntwo\n")
        self.assertEqual(display.flush(), "")


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cache = os.environ.get("FORGE_BROWSER_SUMMARY_CACHE")
        os.environ["FORGE_BROWSER_SUMMARY_CACHE"] = os.path.join(self.tmp, "s.json")

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop("FORGE_BROWSER_SUMMARY_CACHE", None)
        else:
            os.environ["FORGE_BROWSER_SUMMARY_CACHE"] = self.old_cache

    def make_session(self):
        session = chat.ChatSession(model="fake:1b")
        calls = []

        def fake_chat(messages, on_chunk=None):
            prompt = messages[-1]["content"]
            calls.append(prompt)
            if prompt.startswith("Combine these"):
                return "combined summary"
            if prompt.startswith("Summarize this part"):
                return "part summary"
            return "other"

        session._chat = fake_chat
        session.calls = calls
        return session

    def test_short_page_one_map_call_no_reduce(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        self.assertEqual(summarize_page(session, "Short page text."), "part summary")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0].startswith("Summarize this part"))

    def test_long_page_map_then_reduce(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        text = "\n".join(f"Paragraph {i} " + ("word " * 200) for i in range(6))
        self.assertEqual(summarize_page(session, text), "combined summary")
        maps = [c for c in session.calls if c.startswith("Summarize this part")]
        reduces = [c for c in session.calls if c.startswith("Combine these")]
        self.assertGreater(len(maps), 1)
        self.assertEqual(len(reduces), 1)
        self.assertIn("part summary", reduces[0])

    def test_cache_hit_skips_brain(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        summarize_page(session, "Same page.")
        first = len(session.calls)
        self.assertGreater(first, 0)
        self.assertEqual(summarize_page(session, "Same page."), "part summary")
        self.assertEqual(len(session.calls), first)

    def test_changed_text_misses_cache(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        summarize_page(session, "Version one.")
        first = len(session.calls)
        summarize_page(session, "Version two.")
        self.assertGreater(len(session.calls), first)

    def test_empty_page_no_calls(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        self.assertEqual(summarize_page(session, "   "), "")
        self.assertEqual(session.calls, [])

    def test_single_chunk_streams_live(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        streamed = []

        def fake_streaming_chat(messages, on_chunk=None):
            prompt = messages[-1]["content"]
            if on_chunk is not None:
                on_chunk("part ")
                on_chunk("summary")
            return "part summary"

        session._chat = fake_streaming_chat
        self.assertEqual(
            summarize_page(session, "Short page.", on_chunk=streamed.append),
            "part summary")
        self.assertEqual(streamed, ["part ", "summary"])

    def test_chat_action_summarizes_current_page(self):
        session = chat.ChatSession(model="fake:1b")
        calls = []

        def fake_chat(messages, on_chunk=None):
            last = messages[-1]["content"]
            calls.append(last)
            if last.startswith("Combine these"):
                return "combined summary"
            if last.startswith("Summarize this part"):
                return "part summary"
            if "summarize this page" in last:
                return 'ACTION {"type": "summarize"}'
            return "Here is the summary: part summary."

        session._chat = fake_chat
        answer, _opens = session.ask("summarize this page",
                                     page_text="Some page text here.")
        self.assertIn("part summary", answer)
        self.assertTrue(any(c.startswith("Summarize this part") for c in calls))

    def test_summarize_action_without_page_is_plain(self):
        session = chat.ChatSession(model="fake:1b")
        queue = ['ACTION {"type": "summarize"}', "No page is open."]

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        answer, _opens = session.ask("summarize this page")
        self.assertEqual(answer, "No page is open.")


class SessionTests(unittest.TestCase):
    def make_session(self, replies):
        session = chat.ChatSession(model="fake:1b")
        queue = list(replies)
        seen = []

        def fake_chat(messages, on_chunk=None):
            seen.append([dict(m) for m in messages])
            return queue.pop(0)

        session._chat = fake_chat
        session.seen = seen
        return session

    def test_ask_passes_on_chunk_through(self):
        session = self.make_session(["done"])
        received = []
        original_chat = session._chat

        def spy(messages, on_chunk=None):
            received.append(on_chunk)
            return original_chat(messages, on_chunk)

        session._chat = spy
        marker = lambda delta: None
        answer, _opens = session.ask("hi", on_chunk=marker)
        self.assertEqual(answer, "done")
        self.assertEqual(received, [marker])

    def test_plain_answer(self):
        session = self.make_session(["Hello yourself."])
        answer, opens = session.ask("hi")
        self.assertEqual(answer, "Hello yourself.")
        self.assertEqual(opens, [])

    def test_search_action_feeds_result_back(self):
        original = chat._run_search
        chat._run_search = lambda q: f"results for {q}"
        try:
            session = self.make_session([
                'ACTION {"type": "search", "query": "gears"}',
                "Gears are toothed wheels."])
            answer, opens = session.ask("what are gears")
        finally:
            chat._run_search = original
        self.assertEqual(answer, "Gears are toothed wheels.")
        second_call = session.seen[1]
        self.assertTrue(any("RESULT of search" in m["content"]
                            and "results for gears" in m["content"]
                            for m in second_call))

    def test_open_action_requests_a_tab(self):
        original = chat._run_open
        chat._run_open = lambda url: "page text"
        try:
            session = self.make_session([
                'ACTION {"type": "open", "url": "https://example.org"}',
                "Opened it."])
            answer, opens = session.ask("open example.org")
        finally:
            chat._run_open = original
        self.assertEqual(opens, ["https://example.org"])

    def test_action_loop_is_capped(self):
        original = chat._run_search
        chat._run_search = lambda q: "again"
        try:
            session = self.make_session(
                ['ACTION {"type": "search", "query": "x"}'] * 10)
            answer, _opens = session.ask("loop forever")
        finally:
            chat._run_search = original
        self.assertIn("ran out of steps", answer)

    def test_no_brain_is_a_plain_error(self):
        original = chat.pick_chat_model
        chat.pick_chat_model = lambda models=None: None
        try:
            session = chat.ChatSession()
        finally:
            chat.pick_chat_model = original
        with self.assertRaises(BrowserError):
            session.ask("hi")


if __name__ == "__main__":
    unittest.main()
