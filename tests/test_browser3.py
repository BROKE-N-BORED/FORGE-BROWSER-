"""Tests for the AI browser's chat brain (Item: chat interface).

The brain and the engine hands are stubbed — these tests pin the
protocol: ACTION parsing, the action loop and its cap, results
fed back to the model, tab-open requests, and the no-brain error.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

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
        os.environ["FORGE_BROWSER_SEATS_PATH"] = "/tmp/fb-no-seats-here.json"
        try:
            self.assertEqual(chat.pick_chat_model(["qwen3:4b", "my-model:7b"]),
                             "my-model:7b")
        finally:
            del os.environ["FORGE_BROWSER_CHAT_MODEL"]
            del os.environ["FORGE_BROWSER_SEATS_PATH"]

    def test_14b_trap_is_dead(self):
        # G1: substring "4b" matched "14b" — tag-boundary matching
        # kills it, whichever order Ollama lists them.
        self.assertEqual(chat.pick_chat_model(["qwen3:14b", "qwen3:4b"]),
                         "qwen3:4b")
        self.assertEqual(chat.pick_chat_model(["qwen3:4b", "qwen3:14b"]),
                         "qwen3:4b")

    def test_qwen35_2b_in_preference(self):
        # G2: his actual all-rounder is on the list now, after the
        # 4B entries and before the 1.7B fast seat.
        models = ["qwen3:1.7b", "qwen3.5:2b-q4_K_M"]
        self.assertEqual(chat.pick_chat_model(models), "qwen3.5:2b-q4_K_M")
        models = ["qwen3.5:2b-q4_K_M", "qwen3:4b"]
        self.assertEqual(chat.pick_chat_model(models), "qwen3:4b")

    def test_saved_pick_gone_falls_through_to_auto(self):
        # G3: a saved pick for a deleted model falls through to auto
        # instead of naming a dead model every turn.
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(tmp, "seats.json")
            try:
                seats_module.set_seat("brain", "deleted-model:4b")
                model, reason, note = chat.pick_chat_model_with_reason(
                    ["qwen3:4b"])
                self.assertEqual(model, "qwen3:4b")
                self.assertEqual(reason, "auto")
                self.assertIn("deleted-model:4b", note)
                self.assertIn("not installed", note)
            finally:
                del os.environ["FORGE_BROWSER_SEATS_PATH"]

    def test_env_pick_gone_falls_through_to_auto(self):
        # G3: same for the env var.
        os.environ["FORGE_BROWSER_CHAT_MODEL"] = "gone-model:7b"
        os.environ["FORGE_BROWSER_SEATS_PATH"] = "/tmp/fb-no-seats-here.json"
        try:
            self.assertEqual(chat.pick_chat_model(["qwen3:4b"]), "qwen3:4b")
        finally:
            del os.environ["FORGE_BROWSER_CHAT_MODEL"]
            del os.environ["FORGE_BROWSER_SEATS_PATH"]

    def test_embed_only_returns_none(self):
        # G4: the last-resort pick never names an embed model.
        self.assertIsNone(chat.pick_chat_model(["nomic-embed-text"]))
        self.assertIsNone(chat.pick_chat_model(
            ["nomic-embed-text", "other-embed-model"]))

    def test_size_aware_skips_oversize(self):
        # G6: an auto pick skips models over the ~4.5 GB guide.
        big = 8 * 1024 ** 3
        small = 3 * 1024 ** 3
        models = ["qwen3.5:4b", "qwen3:4b"]
        sizes = {"qwen3.5:4b": big, "qwen3:4b": small}
        self.assertEqual(chat.pick_chat_model(models, sizes), "qwen3:4b")

    def test_all_oversize_picks_smallest_with_note(self):
        # G6: when every model is over the guide, the smallest wins
        # (a slow brain beats no brain) and the note says so.
        models = ["qwen3:14b", "qwen3:32b"]
        sizes = {"qwen3:14b": 8 * 1024 ** 3, "qwen3:32b": 20 * 1024 ** 3}
        model, reason, note = chat.pick_chat_model_with_reason(models, sizes)
        self.assertEqual(model, "qwen3:14b")
        self.assertIn("4.5 GB", note)

    def test_explicit_pick_ignores_size(self):
        # G6: an explicit pick is never overridden for size.
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(tmp, "seats.json")
            try:
                seats_module.set_seat("brain", "qwen3:14b")
                models = ["qwen3:14b", "qwen3:4b"]
                sizes = {"qwen3:14b": 8 * 1024 ** 3,
                         "qwen3:4b": 3 * 1024 ** 3}
                model, reason, _note = chat.pick_chat_model_with_reason(
                    models, sizes)
                self.assertEqual((model, reason), ("qwen3:14b", "your pick"))
            finally:
                del os.environ["FORGE_BROWSER_SEATS_PATH"]

    def test_pick_reasons(self):
        # G8: the pick says how it was made.
        _m, reason, _n = chat.pick_chat_model_with_reason(["qwen3.5:4b"])
        self.assertEqual(reason, "auto")
        _m, reason, _n = chat.pick_chat_model_with_reason(["llama3.2:3b"])
        self.assertEqual(reason, "fallback")
        _m, reason, _n = chat.pick_chat_model_with_reason([])
        self.assertEqual(reason, "none")

    def test_ignore_saved_answers_as_auto_would(self):
        # T12: ignore_saved answers what "(auto)" would pick — the
        # saved pick is skipped, but the env var still applies.
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(tmp, "seats.json")
            try:
                seats_module.set_seat("brain", "qwen3:1.7b")
                model, reason, note = chat.pick_chat_model_with_reason(
                    ["qwen3:4b", "qwen3:1.7b"], ignore_saved=True)
                self.assertEqual((model, reason), ("qwen3:4b", "auto"))
                self.assertNotIn("qwen3:1.7b", note)
            finally:
                del os.environ["FORGE_BROWSER_SEATS_PATH"]

    def test_seats_pick_beats_env(self):
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(tmp, "seats.json")
            os.environ["FORGE_BROWSER_CHAT_MODEL"] = "my-model:7b"
            try:
                seats_module.set_seat("brain", "qwen3:4b")
                self.assertEqual(chat.pick_chat_model(["qwen3:4b"]), "qwen3:4b")
            finally:
                del os.environ["FORGE_BROWSER_CHAT_MODEL"]
                del os.environ["FORGE_BROWSER_SEATS_PATH"]


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


class OllamaStatusTests(unittest.TestCase):
    """G7 + G9: the answer-check, and Ollama-down told apart from
    no-models."""

    def _fake_urlopen(self, behavior):
        import json as _json
        import urllib.request as _urlreq
        seen = {}

        class Resp:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps(self._payload).encode("utf-8")

        def fake(request, timeout=None):
            seen["url"] = request.full_url
            seen["data"] = _json.loads(request.data.decode("utf-8")) \
                if request.data else None
            if behavior == "down":
                raise ConnectionError("refused")
            if behavior == "no-models":
                return Resp({"models": []})
            if behavior == "answers":
                return Resp({"message": {"content": "OK."}})
            if behavior == "silent":
                return Resp({"message": {"content": "  "}})
            return Resp({"models": [{"name": "qwen3:4b", "size": 1234}]})

        return fake, seen

    def test_ollama_down_vs_no_models(self):
        import urllib.request as _urlreq
        original = _urlreq.urlopen
        try:
            fake, _seen = self._fake_urlopen("down")
            _urlreq.urlopen = fake
            model, reason, note = chat.pick_chat_model_with_reason()
            self.assertIsNone(model)
            self.assertEqual(reason, "none")
            self.assertEqual(note, "Ollama is not answering")
            fake, _seen = self._fake_urlopen("no-models")
            _urlreq.urlopen = fake
            model, reason, note = chat.pick_chat_model_with_reason()
            self.assertIsNone(model)
            self.assertEqual(note, "no chat model is installed")
        finally:
            _urlreq.urlopen = original

    def test_check_ollama_returns_sizes(self):
        import urllib.request as _urlreq
        original = _urlreq.urlopen
        try:
            fake, _seen = self._fake_urlopen("models")
            _urlreq.urlopen = fake
            reachable, models = chat.check_ollama()
            self.assertTrue(reachable)
            self.assertEqual(models, [{"name": "qwen3:4b", "size": 1234}])
        finally:
            _urlreq.urlopen = original

    def test_answer_check(self):
        import urllib.request as _urlreq
        original = _urlreq.urlopen
        try:
            fake, seen = self._fake_urlopen("answers")
            _urlreq.urlopen = fake
            self.assertTrue(chat.check_model_answers("qwen3:4b"))
            self.assertTrue(seen["url"].endswith("/api/chat"))
            self.assertIs(seen["data"]["think"], False)
            self.assertEqual(seen["data"]["options"], {"num_ctx": 512})
            fake, _seen = self._fake_urlopen("silent")
            _urlreq.urlopen = fake
            self.assertFalse(chat.check_model_answers("qwen3:4b"))
            fake, _seen = self._fake_urlopen("down")
            _urlreq.urlopen = fake
            self.assertFalse(chat.check_model_answers("qwen3:4b"))
            self.assertFalse(chat.check_model_answers(None))
        finally:
            _urlreq.urlopen = original


class BudgetTests(unittest.TestCase):
    """G5: the prompt is token-budgeted to the 8192 ctx."""

    def test_budget_trims_oldest_first(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "a" * 4000},
                    {"role": "assistant", "content": "b" * 4000},
                    {"role": "user", "content": "c" * 4000},
                    {"role": "user", "content": "the question"}]
        # 3000 tokens of history + the question; budget 1500 keeps
        # the system prompt, the question, and the newest history.
        trimmed = chat.budget_messages(messages, budget=1500)
        self.assertEqual(trimmed[0]["content"], "sys")
        self.assertEqual(trimmed[-1]["content"], "the question")
        self.assertLessEqual(
            sum(len(m["content"]) for m in trimmed) // 4, 1500)
        self.assertNotIn("a" * 4000, [m["content"] for m in trimmed])

    def test_budget_keeps_everything_when_it_fits(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "hi"}]
        self.assertEqual(chat.budget_messages(messages), messages)

    def test_ask_budgets_long_history(self):
        session = chat.ChatSession(model="fake:1b")
        session.history = [{"role": "user", "content": "old " * 1500},
                           {"role": "assistant", "content": "old " * 1500}] * 6
        seen = []

        def fake_chat(messages, on_chunk=None):
            seen.append(messages)
            return "done"

        session._chat = fake_chat
        session.ask("new question")
        sent = seen[0]
        self.assertLessEqual(
            sum(len(m["content"]) for m in sent) // 4,
            chat.PROMPT_TOKEN_BUDGET)
        self.assertEqual(sent[0]["content"], chat.SYSTEM_PROMPT)
        self.assertEqual(sent[-1]["content"], "new question")

    def test_turn_speed_recorded(self):
        # G10: the turn's tok/s is on the session when it ends.
        import json as _json
        import urllib.request as _urlreq

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps(
                    {"message": {"content": "word " * 100}}).encode("utf-8")

        original = _urlreq.urlopen
        _urlreq.urlopen = lambda request, timeout=None: Resp()
        try:
            session = chat.ChatSession(model="fake:1b")
            session.ask("hi")
        finally:
            _urlreq.urlopen = original
        self.assertIsInstance(session.last_speed, float)
        self.assertGreater(session.last_speed, 0)


class LiveDisplayTests(unittest.TestCase):
    def test_action_lines_never_shown(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed('Let me look.\nACTION {"type": "search"}\n'), "Let me look.\n")
        self.assertEqual(display.flush(), "")

    def test_partial_line_streams_at_once(self):
        # H1: partial lines show as they arrive — the old hold
        # left a long paragraph silent until its newline.
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("Hel"), "Hel")
        self.assertEqual(display.feed("lo world"), "lo world")
        self.assertEqual(display.feed("\nnext"), "\nnext")
        self.assertEqual(display.flush(), "")

    def test_action_line_split_across_chunks(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed('ACT'), "")
        self.assertEqual(display.feed('ION {"type": "open"}\nDone.'), "Done.")
        self.assertEqual(display.flush(), "")

    def test_action_prefix_holds_until_it_cannot_be_action(self):
        # H1: "ACT" holds, but "ACTually" is released the moment
        # the fourth character rules out an ACTION line.
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("ACT"), "")
        self.assertEqual(display.feed("ually, here is the thing"),
                         "ACTually, here is the thing")

    def test_action_midline_still_held(self):
        # H1: "ACTION " plus more text is still an ACTION line —
        # it must not leak just because the prefix check passed.
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("ACTION "), "")
        self.assertEqual(display.feed('{"type": "open"}\nDone.'), "Done.")
        self.assertEqual(display.flush(), "")

    def test_long_paragraph_streams_without_newline(self):
        # H1: the audit's probe — 50 chunks of one long paragraph
        # with no newline showed NOTHING before; now every chunk
        # appears as it arrives.
        display = chat.LiveDisplay()
        shown = "".join(display.feed("word ") for _ in range(50))
        self.assertEqual(shown, "word " * 50)
        self.assertEqual(display.flush(), "")

    def test_plain_text_passes_through(self):
        display = chat.LiveDisplay()
        self.assertEqual(display.feed("one\ntwo\n"), "one\ntwo\n")
        self.assertEqual(display.flush(), "")


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cache = os.environ.get("FORGE_BROWSER_SUMMARY_CACHE")
        os.environ["FORGE_BROWSER_SUMMARY_CACHE"] = os.path.join(self.tmp, "s.json")
        # No fast seat in tests: the chat summarize action routes
        # the map step to the fast seat when one is picked (K9),
        # and these tests fake only the brain session.
        self.old_seats = os.environ.get("FORGE_BROWSER_SEATS_PATH")
        os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(
            self.tmp, "seats.json")

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop("FORGE_BROWSER_SUMMARY_CACHE", None)
        else:
            os.environ["FORGE_BROWSER_SUMMARY_CACHE"] = self.old_cache
        if self.old_seats is None:
            os.environ.pop("FORGE_BROWSER_SEATS_PATH", None)
        else:
            os.environ["FORGE_BROWSER_SEATS_PATH"] = self.old_seats

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

    def test_cancel_stops_between_map_calls(self):
        from forge.browser.summarize import (SummarizeStopped,
                                             summarize_page)
        session = self.make_session()
        calls = []

        def stopping_chat(messages, on_chunk=None):
            prompt = messages[-1]["content"]
            calls.append(prompt)
            if prompt.startswith("Summarize this part"):
                session.cancel()
            return "part summary"

        session._chat = stopping_chat
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(6))
        with self.assertRaises(SummarizeStopped):
            summarize_page(session, text)
        maps = [c for c in calls if c.startswith("Summarize this part")]
        self.assertEqual(len(maps), 1)

    def test_map_work_capped_at_forty(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(100))
        self.assertEqual(summarize_page(session, text), "combined summary")
        maps = [c for c in session.calls
                if c.startswith("Summarize this part")]
        self.assertEqual(len(maps), 40)

    def test_second_reduce_level(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(20))
        self.assertEqual(summarize_page(session, text), "combined summary")
        reduces = [c for c in session.calls
                   if c.startswith("Combine these")]
        # 20 parts: two group reduces, then the final combine.
        self.assertEqual(len(reduces), 3)

    def test_per_chunk_progress(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        progress = []
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(3))
        summarize_page(session, text, on_progress=progress.append)
        self.assertEqual(progress[0], "part 1 of 3")
        self.assertEqual(progress[-1], "part 3 of 3")

    def test_chunks_overlap(self):
        from forge.browser.summarize import _chunks
        text = "\n".join(f"line {i} " + ("word " * 100) for i in range(10))
        chunks = _chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[1].startswith(chunks[0][-200:]))

    def test_blank_brain_raises_not_empty_string(self):
        from forge.browser.summarize import (SummarizeEmptyError,
                                             summarize_page)
        session = self.make_session()
        session._chat = lambda messages, on_chunk=None: ""
        with self.assertRaises(SummarizeEmptyError):
            summarize_page(session, "Some real page text here.")
        # An actually empty page still returns "".
        self.assertEqual(summarize_page(session, "   "), "")

    def test_url_in_cache_key(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        summarize_page(session, "Same text.", url="http://a.example/")
        first = len(session.calls)
        self.assertGreater(first, 0)
        # Same text, different URL: a miss, the brain runs again.
        summarize_page(session, "Same text.", url="http://b.example/")
        self.assertGreater(len(session.calls), first)
        # Same URL again: a hit, no new calls.
        second = len(session.calls)
        summarize_page(session, "Same text.", url="http://b.example/")
        self.assertEqual(len(session.calls), second)

    def test_chat_action_streams_summarize(self):
        session = chat.ChatSession(model="fake:1b")
        streamed = []

        def fake_chat(messages, on_chunk=None):
            last = messages[-1]["content"]
            if "summarize this page" in last:
                return 'ACTION {"type": "summarize"}'
            if last.startswith("Summarize this part"):
                if on_chunk is not None:
                    on_chunk("part ")
                    on_chunk("summary")
                return "part summary"
            return "Here is the summary: part summary."

        session._chat = fake_chat
        answer, _opens = session.ask("summarize this page",
                                     page_text="Some page text here.",
                                     on_chunk=streamed.append)
        self.assertEqual(streamed, ["part ", "summary"])
        self.assertIn("part summary", answer)

    def test_map_step_uses_fast_session(self):
        from forge.browser.summarize import summarize_page
        brain = self.make_session()
        fast = chat.ChatSession(model="fake-fast:1b")
        fast_calls = []

        def fast_chat(messages, on_chunk=None):
            prompt = messages[-1]["content"]
            fast_calls.append(prompt)
            return "fast part summary"

        fast._chat = fast_chat
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(3))
        self.assertEqual(
            summarize_page(brain, text, map_session=fast),
            "combined summary")
        self.assertTrue(all(c.startswith("Summarize this part")
                            for c in fast_calls))
        self.assertGreater(len(fast_calls), 1)
        brain_maps = [c for c in brain.calls
                      if c.startswith("Summarize this part")]
        self.assertEqual(brain_maps, [])
        brain_reduces = [c for c in brain.calls
                         if c.startswith("Combine these")]
        self.assertEqual(len(brain_reduces), 1)
        self.assertIn("fast part summary", brain_reduces[0])

    def test_cache_write_is_atomic(self):
        import os as _os
        from unittest import mock
        from forge.browser import summarize as summarize_module
        path = os.path.join(self.tmp, "atomic.json")
        os.environ["FORGE_BROWSER_SUMMARY_CACHE"] = path
        with mock.patch.object(_os, "replace",
                               wraps=_os.replace) as spy:
            summarize_module._save_cache({"k": "v"})
            self.assertTrue(spy.called)
            self.assertEqual(spy.call_args[0], (path + ".tmp", path))
        self.assertFalse(_os.path.exists(path + ".tmp"))
        self.assertEqual(summarize_module._load_cache(), {"k": "v"})

    def test_short_length(self):
        from forge.browser.summarize import summarize_page
        session = self.make_session()
        text = "\n".join(f"Paragraph {i} " + ("word " * 200)
                         for i in range(3))
        summarize_page(session, text, length="short")
        reduces = [c for c in session.calls
                   if c.startswith("Combine these")]
        self.assertEqual(len(reduces), 1)
        self.assertIn("2-3 sentences", reduces[0])
        maps = [c for c in session.calls
                if c.startswith("Summarize this part")]
        self.assertIn("1-2 sentences", maps[0])


class RetrieveTests(unittest.TestCase):
    def setUp(self):
        # J6: a known-missing embed model is believed for 60 s —
        # clear it so each test starts with a clean slate.
        from forge.browser import retrieve as retrieve_module
        retrieve_module.clear_embed_failure_cache()

    def tearDown(self):
        from forge.browser import retrieve as retrieve_module
        retrieve_module.clear_embed_failure_cache()

    def test_chunks_split_and_overlap(self):
        from forge.browser.retrieve import chunk_text
        text = "\n".join(f"line {i} " + ("word " * 100) for i in range(10))
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1200 + 200 + 1)
        self.assertTrue(chunks[1].startswith(chunks[0][-200:]))
        joined = "\n".join(chunks)
        for i in range(10):
            self.assertIn(f"line {i}", joined)

    def test_retrieve_ranks_by_similarity(self):
        from forge.browser.retrieve import retrieve
        query = [1.0, 0.0]
        vectors = [[0.0, 1.0], [1.0, 0.0], [0.7, 0.7]]
        self.assertEqual(retrieve(query, vectors, top_k=2), [1, 2])

    def test_embed_texts_posts_to_ollama(self):
        import json as _json
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps(
                    {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}).encode()

        original = _urlreq.urlopen

        def fake_urlopen(request, timeout=None):
            seen["data"] = _json.loads(request.data.decode("utf-8"))
            seen["url"] = request.full_url
            return Resp()

        _urlreq.urlopen = fake_urlopen
        try:
            vectors = retrieve_module.embed_texts(["a", "b"])
        finally:
            _urlreq.urlopen = original
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(seen["data"]["model"], "nomic-embed-text")
        self.assertEqual(seen["data"]["input"], ["a", "b"])
        self.assertTrue(seen["url"].endswith("/api/embed"))

    def test_page_context_short_page_goes_whole(self):
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        calls = []
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            calls.append(list(texts))
            return [[0.0]] * len(texts)

        retrieve_module.embed_texts = fake_embed
        try:
            self.assertEqual(
                retrieve_module.page_context(session, "q", "short text"),
                "short text")
        finally:
            retrieve_module.embed_texts = original
        self.assertEqual(calls, [])

    def test_page_context_long_page_retrieves_top_chunks(self):
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        paras = []
        for i in range(6):
            paras.append(f"filler{i} " * 150)
        paras[3] = "needle word " * 90 + paras[3]
        text = "\n".join(paras)
        self.assertGreater(len(text), 6000)

        def fake_embed(texts, model=None):
            vectors = []
            for t in texts:
                vectors.append([0.0, 1.0] if "needle" in t else [1.0, 0.0])
            return vectors

        original = retrieve_module.embed_texts
        retrieve_module.embed_texts = fake_embed
        try:
            context = retrieve_module.page_context(session, "needle", text)
        finally:
            retrieve_module.embed_texts = original
        self.assertIn("needle", context)
        self.assertLess(len(context), len(text))

    def test_page_context_caches_chunk_embeddings(self):
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        text = "x" * 7000
        calls = []
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            calls.append(list(texts))
            return [[0.5]] * len(texts)

        retrieve_module.embed_texts = fake_embed
        try:
            retrieve_module.page_context(session, "q1", text)
            retrieve_module.page_context(session, "q2", text)
        finally:
            retrieve_module.embed_texts = original
        chunk_calls = [c for c in calls if len(c) > 1]
        query_calls = [c for c in calls if len(c) == 1]
        self.assertEqual(len(chunk_calls), 1)
        self.assertEqual(len(query_calls), 2)

    def test_page_context_falls_back_without_embeddings(self):
        # I4: the fallback is labeled — the brain knows it only
        # saw the head of the page.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            raise BrowserError("down")

        retrieve_module.embed_texts = fake_embed
        try:
            text = "x" * 7000
            context = retrieve_module.page_context(session, "q", text)
            self.assertTrue(context.startswith(
                retrieve_module.FALLBACK_NOTE))
            self.assertTrue(context.endswith(text[:6000]))
            self.assertEqual(session._retrieve_last["mode"], "fallback")
        finally:
            retrieve_module.embed_texts = original

    def test_ties_break_to_earliest_chunk(self):
        # I3: 8 identical vectors used to return [7, 6, 5, 4].
        from forge.browser.retrieve import retrieve
        query = [1.0, 0.0]
        vectors = [[1.0, 0.0]] * 8
        self.assertEqual(retrieve(query, vectors, top_k=4), [0, 1, 2, 3])

    def test_chunks_join_in_document_order_with_labels(self):
        # I2: rank order scrambled sequences (PARA6, PARA1, ...);
        # the join is document order with position labels.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        paras = [f"PARA{i} " * 150 for i in range(8)]
        text = "\n".join(paras)
        self.assertGreater(len(text), 6000)

        def fake_embed(texts, model=None):
            vectors = []
            for t in texts:
                if len(t) < 100:  # the query
                    vectors.append([1.0, 0.0])
                    continue
                # The chunk's main paragraph is its most frequent
                # PARA marker — the overlap tail only contributes
                # a few dozen. Rank: chunk 6 best, then 1, 3, 7 —
                # deliberately out of document order.
                counts = {i: t.count(f"PARA{i}") for i in range(8)}
                main = max(counts, key=counts.get)
                rank = {6: 1.0, 1: 0.9, 3: 0.8, 7: 0.7}.get(main, 0.0)
                vectors.append([rank, 1.0 - rank] if rank else [0.0, 1.0])
            return vectors

        original = retrieve_module.embed_texts
        retrieve_module.embed_texts = fake_embed
        try:
            context = retrieve_module.page_context(session, "q", text)
        finally:
            retrieve_module.embed_texts = original
        positions = [context.index(f"PARA{i}") for i in (1, 3, 6, 7)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--- chunk 2 of", context)  # PARA1 is chunk 2
        self.assertIn("--- chunk 7 of", context)  # PARA6 is chunk 7

    def test_relevance_floor_skips_weak_chunks(self):
        # I5: below-floor chunks never reach the brain; when
        # nothing passes, the brain gets the no-match signal.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        text = "x" * 7000

        def fake_embed_weak(texts, model=None):
            # Query is [1, 0]; chunks are [0, 1] — cosine 0.
            return [[1.0, 0.0]] if len(texts) == 1 and texts[0] == "q" \
                else [[0.0, 1.0]] * len(texts)

        original = retrieve_module.embed_texts
        retrieve_module.embed_texts = fake_embed_weak
        try:
            self.assertEqual(
                retrieve_module.page_context(session, "q", text),
                retrieve_module.NO_MATCH)
            self.assertEqual(session._retrieve_last["mode"], "no-match")
        finally:
            retrieve_module.embed_texts = original

    def test_embed_texts_batches_at_64(self):
        # I6: 130 texts go out as 3 calls, vectors back in order.
        import json as _json
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        calls = []

        class Resp:
            def __init__(self, count):
                self._count = count

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps({"embeddings":
                    [[float(i)] for i in range(self._count)]}).encode()

        original = _urlreq.urlopen

        def fake_urlopen(request, timeout=None):
            data = _json.loads(request.data.decode("utf-8"))
            calls.append(data["input"])
            return Resp(len(data["input"]))

        _urlreq.urlopen = fake_urlopen
        try:
            vectors = retrieve_module.embed_texts(
                [f"t{i}" for i in range(130)])
        finally:
            _urlreq.urlopen = original
        self.assertEqual([len(c) for c in calls], [64, 64, 2])
        self.assertEqual(len(vectors), 130)
        # Vectors come back in input order across batches.
        self.assertEqual(vectors[0], [0.0])
        self.assertEqual(vectors[64], [0.0])
        self.assertEqual(vectors[129], [1.0])

    def test_embed_errors_are_classified(self):
        # I7: a 404 means the model is missing; a refused
        # connection means Ollama is down.
        import urllib.error as _urlerr
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        original = _urlreq.urlopen
        try:
            def fake_404(request, timeout=None):
                raise _urlerr.HTTPError(request.full_url, 404, "not found",
                                        {}, None)

            _urlreq.urlopen = fake_404
            with self.assertRaises(BrowserError) as caught:
                retrieve_module.embed_texts(["a"])
            self.assertEqual(caught.exception.reason, "model-missing")
            # J6 cached the 404 — clear it so the next scenario
            # is a fresh failure, not the cached one.
            retrieve_module.clear_embed_failure_cache()

            def fake_down(request, timeout=None):
                raise _urlerr.URLError("refused")

            _urlreq.urlopen = fake_down
            with self.assertRaises(BrowserError) as caught:
                retrieve_module.embed_texts(["a"])
            self.assertEqual(caught.exception.reason, "ollama-down")
        finally:
            _urlreq.urlopen = original

    def test_tab_refs_stripped_from_query(self):
        # I8: @tabN is addressing, not content.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        text = "x" * 7000
        seen = []
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            seen.append(list(texts))
            return [[0.5]] * len(texts)

        retrieve_module.embed_texts = fake_embed
        try:
            retrieve_module.page_context(
                session, "what does @tab2 say about cats?", text)
        finally:
            retrieve_module.embed_texts = original
        queries = [c for c in seen if len(c) == 1]
        self.assertEqual(len(queries), 1)
        self.assertNotIn("@tab2", queries[0][0])
        self.assertIn("cats", queries[0][0])

    def test_adjacent_overlaps_deduped(self):
        # I9: when adjacent chunks are both picked, their shared
        # 200-char overlap appears once, not twice.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        marker = "UNIQUE-OVERLAP-MARKER "
        text = (("aaa " * 600) + "\n" + marker * 40 + "\n" +
                ("bbb " * 600) + "\n" + ("ccc " * 600))
        self.assertGreater(len(text), 6000)

        def fake_embed(texts, model=None):
            return [[0.5]] * len(texts)

        original = retrieve_module.embed_texts
        retrieve_module.embed_texts = fake_embed
        try:
            context = retrieve_module.page_context(session, "q", text)
        finally:
            retrieve_module.embed_texts = original
        # The marker sits in the overlap region: with dedupe it
        # appears once per chunk at most, never doubled by the join.
        self.assertLessEqual(context.count(marker), text.count(marker))

    def test_retrieve_last_records_modes(self):
        # I10: every page_context call records what it did.
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        self.assertEqual(
            retrieve_module.page_context(session, "q", ""), "")
        self.assertEqual(session._retrieve_last["mode"], "none")
        self.assertEqual(
            retrieve_module.page_context(session, "q", "short"), "short")
        self.assertEqual(session._retrieve_last["mode"], "whole")
        self.assertEqual(
            retrieve_module.retrieve_note(session._retrieve_last), "")
        self.assertEqual(
            retrieve_module.retrieve_note(
                {"mode": "retrieval", "chunks_searched": 24}),
            "(Searched 24 chunks.)")


class EmbedFailureCacheTests(unittest.TestCase):
    """J6: a known-missing embed model is believed briefly."""

    def setUp(self):
        from forge.browser import retrieve as retrieve_module
        retrieve_module.clear_embed_failure_cache()

    def tearDown(self):
        from forge.browser import retrieve as retrieve_module
        retrieve_module.clear_embed_failure_cache()

    def test_failure_short_circuits_second_call(self):
        import urllib.error
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        from forge.browser.errors import BrowserError
        calls = []
        original = _urlreq.urlopen

        def failing(request, timeout=None):
            calls.append(request)
            raise urllib.error.URLError("connection refused")

        _urlreq.urlopen = failing
        try:
            with self.assertRaises(BrowserError):
                retrieve_module.embed_texts(["hello"])
            self.assertEqual(len(calls), 1)
            # The second call raises without another round trip.
            with self.assertRaises(BrowserError) as caught:
                retrieve_module.embed_texts(["hello again"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(caught.exception.reason, "ollama-down")
        finally:
            _urlreq.urlopen = original

    def test_clear_lets_a_later_call_through(self):
        import json as _json
        import urllib.error
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        from forge.browser.errors import BrowserError
        original = _urlreq.urlopen

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps({"embeddings": [[0.1]]}).encode()

        def failing(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        _urlreq.urlopen = failing
        try:
            with self.assertRaises(BrowserError):
                retrieve_module.embed_texts(["hello"])
        finally:
            _urlreq.urlopen = original
        retrieve_module.clear_embed_failure_cache()

        def working(request, timeout=None):
            return Resp()

        _urlreq.urlopen = working
        try:
            self.assertEqual(retrieve_module.embed_texts(["hello"]),
                             [[0.1]])
        finally:
            _urlreq.urlopen = original

    def test_unknown_failure_not_cached(self):
        import urllib.error
        import urllib.request as _urlreq
        from forge.browser import retrieve as retrieve_module
        from forge.browser.errors import BrowserError
        calls = []
        original = _urlreq.urlopen

        def odd(request, timeout=None):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 500,
                                         "server error", {}, None)

        _urlreq.urlopen = odd
        try:
            with self.assertRaises(BrowserError):
                retrieve_module.embed_texts(["hello"])
            with self.assertRaises(BrowserError):
                retrieve_module.embed_texts(["hello"])
            # A transient oddity is retried, not believed.
            self.assertEqual(len(calls), 2)
        finally:
            _urlreq.urlopen = original


class TabTextDemandTests(unittest.TestCase):
    """J4: other tabs' text is read only on demand."""

    def test_plain_send_reads_current_only(self):
        from forge.browser.app import needs_all_tab_text
        self.assertFalse(needs_all_tab_text("what is this page about"))
        self.assertFalse(needs_all_tab_text("search for gears"))

    def test_tab_ref_reads_all(self):
        from forge.browser.app import needs_all_tab_text
        self.assertTrue(needs_all_tab_text("@tab2 what does it say"))
        self.assertTrue(needs_all_tab_text("what does @tab1 say"))

    def test_compare_reads_all(self):
        from forge.browser.app import needs_all_tab_text
        self.assertTrue(needs_all_tab_text("compare these three"))
        self.assertTrue(needs_all_tab_text("Compare the prices"))


class CardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cards = os.environ.get("FORGE_BROWSER_CARDS_DIR")
        os.environ["FORGE_BROWSER_CARDS_DIR"] = self.tmp

    def tearDown(self):
        if self.old_cards is None:
            os.environ.pop("FORGE_BROWSER_CARDS_DIR", None)
        else:
            os.environ["FORGE_BROWSER_CARDS_DIR"] = self.old_cards

    def make_session(self):
        session = chat.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            last = messages[-1]["content"]
            if last.startswith("Summarize this part"):
                return "part summary"
            if last.startswith("List the key facts"):
                return "facts answer"
            if last.startswith("Present the summary"):
                return "card answer"
            return "other"

        session._chat = fake_chat
        return session

    def test_parse_card(self):
        from forge.browser.cards import parse_card
        card = parse_card("demo", "# Demo title\nDo the thing.\n## Actions\n"
                                  'ACTION {"type": "read_page"}\n')
        self.assertEqual(card.title, "Demo title")
        self.assertEqual(card.prompt, "Do the thing.")
        self.assertEqual(card.actions, [{"type": "read_page"}])

    def test_parse_card_bad_action_line(self):
        from forge.browser.cards import parse_card
        with self.assertRaises(BrowserError):
            parse_card("demo", "# Demo\nPrompt.\n## Actions\nACTION nope\n")

    def test_builtin_cards_shipped(self):
        from forge.browser.cards import list_cards
        names = [card.name for card in list_cards()]
        for expected in ("summarize-page", "tl-dr", "key-facts",
                         "explain-like-im-new", "find-price", "research-topic"):
            self.assertIn(expected, names)

    def test_user_card_wins_on_name_clash(self):
        from forge.browser.cards import list_cards
        with open(os.path.join(self.tmp, "tl-dr.md"), "w") as handle:
            handle.write("# My TL;DR\nMy own version.\n")
        found = {card.name: card for card in list_cards()}
        self.assertEqual(found["tl-dr"].title, "My TL;DR")

    def test_run_card_read_page(self):
        from forge.browser.cards import run_card
        session = self.make_session()
        answer, opens = run_card(session, "key-facts", page_text="Gears cost $42.")
        self.assertEqual(opens, [])
        self.assertEqual(answer, "facts answer")

    def test_run_card_summarize(self):
        from forge.browser.cards import run_card
        session = self.make_session()
        answer, opens = run_card(session, "summarize-page", page_text="Gears turn.")
        self.assertEqual(opens, [])
        self.assertEqual(answer, "card answer")

    def test_run_card_research_topic(self):
        from forge.browser.cards import run_card
        original = chat._run_research
        seen = []
        chat._run_research = lambda topic: seen.append(topic) or "research findings"
        session = self.make_session()

        def fake_chat(messages, on_chunk=None):
            if "Research the topic" in messages[-1]["content"]:
                return "researched answer"
            return "other"

        session._chat = fake_chat
        try:
            answer, opens = run_card(session, "research-topic",
                              question="quantum dots")
        finally:
            chat._run_research = original
        self.assertEqual(answer, "researched answer")
        self.assertEqual(seen, ["quantum dots"])

    def test_run_card_unknown_name(self):
        from forge.browser.cards import run_card
        session = self.make_session()
        with self.assertRaises(BrowserError):
            run_card(session, "no-such-card")

    def test_chat_action_runs_card(self):
        session = chat.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            last = messages[-1]["content"]
            if last.startswith("List the key facts"):
                return "facts answer"
            if "run the key-facts card" in last:
                return 'ACTION {"type": "card", "name": "key-facts"}'
            return "other"

        session._chat = fake_chat
        answer, _opens = session.ask("run the key-facts card",
                                     page_text="Gears cost $42.")
        self.assertEqual(answer, "facts answer")

    def test_cli_cards_lists_builtins(self):
        import io as _io
        from contextlib import redirect_stdout
        from forge.browser import cli as cli_module
        out = _io.StringIO()

        class Args:
            pass

        with redirect_stdout(out):
            self.assertEqual(cli_module._cmd_cards(Args()), 0)
        self.assertIn("summarize-page", out.getvalue())
        self.assertIn("research-topic", out.getvalue())


class TitlesTests(unittest.TestCase):
    def test_tidy_title_trims_brain_ramble(self):
        from forge.browser.titles import tidy_title
        session = chat.ChatSession(model="fake:1b")
        session._chat = lambda messages, on_chunk=None: (
            "The Best Gears In The Whole Wide World Today")
        # N1: the 5-word cut no longer strands "In The" at the end.
        self.assertEqual(
            tidy_title(session,
                       "The Best Gears In The Whole Wide World Today | Shop",
                       "http://x.example"),
            "The Best Gears")

    def test_tidy_title_falls_back_without_brain(self):
        from forge.browser.titles import tidy_title
        session = chat.ChatSession(model="fake:1b")

        def boom(messages, on_chunk=None):
            raise BrowserError("down")

        session._chat = boom
        long_title = "A Very Long Messy Page Title That Goes On And On | Example Site"
        self.assertEqual(tidy_title(session, long_title), long_title[:40])

    def test_group_items_by_embedding(self):
        from forge.browser.titles import group_items
        items = [
            {"title": "Gears", "url": "http://a.example/1"},
            {"title": "Wheels", "url": "http://b.example/2"},
            {"title": "Soup", "url": "http://c.example/3"},
        ]
        # N4/N8: the embed function sees titles (content-keyed —
        # grouping sorts its input, so position-based vectors
        # would be wrong).
        by_title = {"Gears": [1.0, 0.0], "Wheels": [0.9, 0.1],
                    "Soup": [0.0, 1.0]}

        def fake_embed(texts):
            return [by_title[text] for text in texts]

        groups = group_items(items, embed_fn=fake_embed)
        self.assertEqual(len(groups), 2)
        self.assertEqual([i["title"] for i in groups[0]["items"]],
                         ["Gears", "Wheels"])
        self.assertEqual([i["title"] for i in groups[1]["items"]], ["Soup"])

    def test_group_items_falls_back_to_domain(self):
        from forge.browser.titles import group_items
        items = [
            {"title": "One", "url": "http://shop.example/a"},
            {"title": "Two", "url": "http://shop.example/b"},
            {"title": "Three", "url": "http://news.example/c"},
        ]
        groups = group_items(items)
        self.assertEqual([g["name"] for g in groups],
                         ["shop.example", "news.example"])

    def test_history_records_dedups_and_groups(self):
        from forge.browser import titles as titles_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_HISTORY_PATH")
            os.environ["FORGE_BROWSER_HISTORY_PATH"] = os.path.join(tmp, "h.json")
            try:
                titles_module.record_visit("http://shop.example/a", "Shop A")
                titles_module.record_visit("http://news.example/b", "News B")
                titles_module.record_visit("http://shop.example/a", "Shop A Again")
                entries = titles_module.load_history()
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries[0]["url"], "http://shop.example/a")
                self.assertEqual(entries[0]["title"], "Shop A Again")
                groups = titles_module.grouped_history()
                self.assertEqual([g["name"] for g in groups],
                                 ["shop.example", "news.example"])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_HISTORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_HISTORY_PATH"] = old


class TitlesAuditTests(unittest.TestCase):
    """#8 audit fixes (N1–N12)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_history = os.environ.get("FORGE_BROWSER_HISTORY_PATH")
        os.environ["FORGE_BROWSER_HISTORY_PATH"] = os.path.join(
            self.tmp, "h.json")

    def tearDown(self):
        if self.old_history is None:
            os.environ.pop("FORGE_BROWSER_HISTORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_HISTORY_PATH"] = self.old_history

    def _write_history(self, entries):
        import json as _json
        path = os.environ["FORGE_BROWSER_HISTORY_PATH"]
        with open(path, "w", encoding="utf-8") as handle:
            _json.dump(entries, handle)

    def test_tidy_trims_trailing_stopwords(self):
        # N1: no more dangling "To The" in the tab bar.
        from forge.browser.titles import tidy_title
        session = chat.ChatSession(model="fake:1b")
        session._chat = lambda messages, on_chunk=None: (
            "The Complete Guide To The")
        self.assertEqual(tidy_title(session, "The Complete Guide To The"),
                         "The Complete Guide")
        session._chat = lambda messages, on_chunk=None: "A Short History Of"
        self.assertEqual(tidy_title(session, "A Short History Of"),
                         "A Short History")

    def test_tidy_keeps_one_word_when_all_are_stopwords(self):
        from forge.browser.titles import tidy_title
        session = chat.ChatSession(model="fake:1b")
        session._chat = lambda messages, on_chunk=None: "To The And Of In"
        self.assertEqual(tidy_title(session, "To The And Of In"), "To")

    def test_journeys_split_on_time_gaps(self):
        # N2: a 30-minute quiet gap starts a new journey.
        from forge.browser import titles as titles_module
        base = 1762900000.0
        self._write_history([
            {"url": "http://late.example/x", "title": "Late",
             "at": base + 7200},
            {"url": "http://shop.example/b", "title": "Shop B",
             "at": base + 600},
            {"url": "http://shop.example/a", "title": "Shop A", "at": base},
        ])
        found = titles_module.journeys()
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["name"], "Late")
        self.assertEqual([i["title"] for i in found[0]["items"]], ["Late"])
        self.assertEqual(found[1]["name"], "shop.example")
        self.assertEqual([i["title"] for i in found[1]["items"]],
                         ["Shop B", "Shop A"])

    def test_journeys_empty_history(self):
        from forge.browser import titles as titles_module
        self.assertEqual(titles_module.journeys(), [])

    def test_sensitive_urls_not_recorded(self):
        # N3: bank and health pages never reach history.
        from forge.browser import titles as titles_module
        titles_module.record_visit("https://mybank.example/login", "Bank")
        titles_module.record_visit("https://health.example/records", "Health")
        titles_module.record_visit("https://gears.example/", "Gears")
        entries = titles_module.load_history()
        self.assertEqual([e["url"] for e in entries],
                         ["https://gears.example/"])

    def test_clear_history(self):
        from forge.browser import titles as titles_module
        titles_module.record_visit("https://gears.example/", "Gears")
        self.assertTrue(titles_module.clear_history())
        self.assertEqual(titles_module.load_history(), [])
        self.assertFalse(os.path.exists(
            os.environ["FORGE_BROWSER_HISTORY_PATH"]))

    def test_grouping_is_order_stable(self):
        # N4: the same tabs in different orders give the same groups.
        from forge.browser.titles import group_items
        by_title = {"Gears": [1.0, 0.0], "Wheels": [0.9, 0.1],
                    "Soup": [0.0, 1.0], "Stew": [0.1, 0.9]}

        def fake_embed(texts):
            return [by_title[text] for text in texts]

        items = [{"title": "Gears", "url": "http://a.example/1"},
                 {"title": "Wheels", "url": "http://b.example/2"},
                 {"title": "Soup", "url": "http://c.example/3"},
                 {"title": "Stew", "url": "http://d.example/4"}]
        first = group_items(items, embed_fn=fake_embed)
        second = group_items(list(reversed(items)), embed_fn=fake_embed)
        names = lambda groups: sorted(
            (g["name"], tuple(sorted(i["title"] for i in g["items"])))
            for g in groups)
        self.assertEqual(names(first), names(second))

    def test_group_name_comes_from_shared_words(self):
        # N4: a group of gear pages is named for gears, not for the
        # first tab's title.
        from forge.browser.titles import group_items
        items = [{"title": "Gear Repair Guide", "url": "http://a.example/1"},
                 {"title": "Gear Maintenance Tips", "url": "http://b.example/2"},
                 {"title": "Soup Recipes", "url": "http://c.example/3"}]
        # Content-keyed: grouping sorts its input, so a
        # position-based fake would be wrong.
        by_title = {"Gear Repair Guide": [1.0, 0.0],
                    "Gear Maintenance Tips": [0.95, 0.05],
                    "Soup Recipes": [0.0, 1.0]}
        groups = group_items(
            items, embed_fn=lambda texts: [by_title[t] for t in texts])
        gear = [g for g in groups if len(g["items"]) == 2][0]
        self.assertEqual(gear["name"], "Gear")

    def test_tidy_cache_caps_at_200_fifo(self):
        # N5: the 201st tidy evicts the oldest, not the newest.
        from forge.browser.titles import TidyCache
        cache = TidyCache()
        for i in range(250):
            cache[f"http://x.example/{i}"] = f"Title {i}"
        self.assertEqual(len(cache), 200)
        self.assertNotIn("http://x.example/0", cache)
        self.assertIn("http://x.example/249", cache)
        self.assertEqual(cache["http://x.example/249"], "Title 249")

    def test_tidy_worker_coalesces_rapid_retitles(self):
        # N6: five retitles in a burst cost one brain call, on the
        # latest title.
        import threading as _threading
        from forge.browser.titles import TidyWorker
        calls = []
        done = _threading.Event()

        def tidy_fn(url, raw):
            calls.append((url, raw))
            return "tidy"

        def on_done(view, url, tidy):
            done.set()

        worker = TidyWorker(tidy_fn, on_done, debounce=0.01)
        view = object()
        for raw in ("One", "Two", "Three", "Four", "Five"):
            worker.request(view, "http://x.example/", raw)
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(calls, [("http://x.example/", "Five")])

    def test_tidy_worker_processes_each_tab(self):
        # N6: different tabs are separate requests, one at a time.
        import threading as _threading
        from forge.browser.titles import TidyWorker
        calls = []
        done = _threading.Event()

        def tidy_fn(url, raw):
            calls.append(url)
            return "tidy"

        def on_done(view, url, tidy):
            if len(calls) == 2:
                done.set()

        worker = TidyWorker(tidy_fn, on_done, debounce=0.01)
        worker.request(object(), "http://a.example/", "A")
        worker.request(object(), "http://b.example/", "B")
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(sorted(calls),
                         ["http://a.example/", "http://b.example/"])

    def test_embed_text_is_title_only(self):
        # N8: the URL no longer dilutes topic grouping.
        from forge.browser.titles import group_items
        seen = {}

        def fake_embed(texts):
            seen["texts"] = list(texts)
            return [[1.0, 0.0], [0.0, 1.0]]

        group_items([{"title": "Gears", "url": "http://shop.example/1"},
                     {"title": "Soup", "url": "http://news.example/2"}],
                    embed_fn=fake_embed)
        self.assertEqual(seen["texts"], ["Gears", "Soup"])

    def test_threshold_report_separated(self):
        # N9: the harness reports separation when same-topic pairs
        # score above 0.75 and different-topic pairs below it.
        from forge.browser.titles import _THRESHOLD_PAIRS, threshold_report
        topics = {}
        for a, b, same in _THRESHOLD_PAIRS:
            topics.setdefault(a, len(topics))
            if same:
                topics[b] = topics[a]
            else:
                topics.setdefault(b, len(topics))

        def fake_embed(texts):
            vectors = []
            for text in texts:
                vec = [0.0] * 8
                vec[topics[text] % 8] = 1.0
                vectors.append(vec)
            return vectors

        report = threshold_report(fake_embed)
        self.assertTrue(report["separated"])
        self.assertEqual(report["same"]["min"], 1.0)
        self.assertEqual(report["different"]["max"], 0.0)

    def test_threshold_report_not_separated(self):
        from forge.browser.titles import threshold_report

        def fake_embed(texts):
            return [[1.0, 0.0] for _ in texts]

        report = threshold_report(fake_embed)
        self.assertFalse(report["separated"])

    def test_history_write_leaves_no_temp_file(self):
        # N10: the atomic write leaves the file and no .tmp behind.
        from forge.browser import titles as titles_module
        titles_module.record_visit("https://gears.example/", "Gears")
        path = os.environ["FORGE_BROWSER_HISTORY_PATH"]
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(len(titles_module.load_history()), 1)

    def test_history_page_renders_and_searches(self):
        # N11: the full view shows entries with timestamps and
        # filters by query.
        from forge.browser import titles as titles_module
        from forge.browser.webui import history_page
        titles_module.record_visit("https://gears.example/", "Gear Guide")
        titles_module.record_visit("https://soup.example/", "Soup Recipes")
        page = history_page()
        self.assertIn("Gear Guide", page)
        self.assertIn("Soup Recipes", page)
        self.assertIn("Clear history", page)
        filtered = history_page(query="gear")
        self.assertIn("Gear Guide", filtered)
        self.assertNotIn("Soup Recipes", filtered)

    def test_group_colors_are_stable(self):
        # N12: the same group name always gets the same color.
        from forge.browser.titles import GROUP_COLORS, assign_group_colors
        first = assign_group_colors(["shop.example", "news.example"])
        second = assign_group_colors(["news.example", "shop.example"])
        self.assertEqual(first, second)
        for color in first.values():
            self.assertIn(color, GROUP_COLORS)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
        os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(self.tmp, "m.json")

    def tearDown(self):
        if self.old_path is None:
            os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = self.old_path

    def test_remember_dedups(self):
        from forge.browser.memory import list_entries, remember
        first = remember("I like dark mode.")
        second = remember("i like  dark mode.")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list_entries()), 1)

    def test_forget_by_text_and_id(self):
        from forge.browser.memory import forget, list_entries, remember
        entry = remember("My budget is $500.")
        remember("I like dark mode.")
        self.assertEqual(forget("budget"), 1)
        self.assertEqual(len(list_entries()), 1)
        self.assertEqual(forget(entry["id"]), 0)  # already gone
        remaining = list_entries()
        self.assertEqual(forget(remaining[0]["id"]), 1)
        self.assertEqual(list_entries(), [])

    def test_search_by_embedding(self):
        from forge.browser.memory import remember, search_entries
        remember("I like dark mode.")
        remember("My budget is $500.")

        def fake_embed(texts):
            return [[1.0, 0.0] if "dark" in t.lower() else [0.0, 1.0]
                    for t in texts]

        found = search_entries("dark mode please", embed_fn=fake_embed)
        self.assertEqual([e["text"] for e in found], ["I like dark mode."])

    def test_search_falls_back_to_keywords(self):
        from forge.browser.memory import remember, search_entries
        remember("I like dark mode.")
        remember("My budget is $500.")

        def boom(texts):
            raise BrowserError("down")

        found = search_entries("what is my budget", embed_fn=boom)
        self.assertEqual([e["text"] for e in found], ["My budget is $500."])

    def test_sensitive_urls(self):
        from forge.browser.memory import is_sensitive_url
        self.assertTrue(is_sensitive_url("https://mybank.example/login"))
        self.assertTrue(is_sensitive_url("https://checkout.example/pay"))
        self.assertFalse(is_sensitive_url("https://news.example/gears"))
        self.assertFalse(is_sensitive_url(""))
        # R8: word boundaries — "bank" must not flag riverbank,
        # "tax" must not flag syntax; a host label still matches.
        self.assertFalse(is_sensitive_url("https://riverbank.example/news"))
        self.assertFalse(is_sensitive_url("https://syntax.example/docs"))
        self.assertTrue(is_sensitive_url("https://bank.example/"))

    def test_site_block_allow_clear(self):
        # F3: per-site memory controls, beyond the sensitive list.
        from forge.browser.memory import (
            allow_site_memory, block_site_memory, clear_site_memory,
            list_site_memory, site_memory_allowed)
        old = os.environ.get("FORGE_BROWSER_MEMORY_SITES_PATH")
        os.environ["FORGE_BROWSER_MEMORY_SITES_PATH"] = os.path.join(
            self.tmp, "sites.json")
        try:
            # Default: the sensitive-pattern rule decides.
            self.assertTrue(site_memory_allowed("https://news.example/gears"))
            self.assertFalse(site_memory_allowed("https://mybank.example/login"))
            # Block a plain site: memory off there, and on subdomains.
            self.assertEqual(block_site_memory("news.example"), "news.example")
            self.assertFalse(site_memory_allowed("https://news.example/gears"))
            self.assertFalse(site_memory_allowed("https://www.news.example/x"))
            self.assertTrue(site_memory_allowed("https://other.example/x"))
            # Allow a sensitive site: memory stays on despite the pattern.
            self.assertEqual(allow_site_memory("https://mybank.example/login"),
                             "mybank.example")
            self.assertTrue(site_memory_allowed("https://mybank.example/login"))
            # Block wins over allow when re-blocked, and clear restores
            # the pattern rule.
            block_site_memory("mybank.example")
            self.assertFalse(site_memory_allowed("https://mybank.example/login"))
            self.assertEqual(list_site_memory()["blocked"],
                             ["mybank.example", "news.example"])
            clear_site_memory("news.example")
            self.assertTrue(site_memory_allowed("https://news.example/gears"))
            self.assertEqual(list_site_memory()["allowed"], [])
        finally:
            if old is None:
                os.environ.pop("FORGE_BROWSER_MEMORY_SITES_PATH", None)
            else:
                os.environ["FORGE_BROWSER_MEMORY_SITES_PATH"] = old

    def test_remember_page_skips_blocked_site(self):
        # F3: a blocked site's pages are never recorded.
        from forge.browser.memory import block_site_memory, remember_page
        old_sites = os.environ.get("FORGE_BROWSER_MEMORY_SITES_PATH")
        old_pages = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
        os.environ["FORGE_BROWSER_MEMORY_SITES_PATH"] = os.path.join(
            self.tmp, "sites.json")
        os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(
            self.tmp, "pages.json")
        try:
            block_site_memory("quiet.example")
            self.assertFalse(remember_page(
                "https://quiet.example/article", "Quiet",
                "word " * 100))
            self.assertTrue(remember_page(
                "https://loud.example/article", "Loud", "word " * 100))
        finally:
            for key, old in (("FORGE_BROWSER_MEMORY_SITES_PATH", old_sites),
                             ("FORGE_BROWSER_PAGE_MEMORY_PATH", old_pages)):
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    def test_export_writes_json(self):
        from forge.browser.memory import export_memory, remember
        remember("I like dark mode.")
        path = os.path.join(self.tmp, "out.json")
        self.assertEqual(export_memory(path), path)
        with open(path) as handle:
            data = json.load(handle)
        self.assertEqual([e["text"] for e in data["entries"]],
                         ["I like dark mode."])

    def test_page_memory_stores_chunks_as_text(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                text = ("Gears turn wheels. " * 40)
                self.assertTrue(memory_module.remember_page(
                    "http://gears.example/guide", "Gear Guide", text))
                pages = memory_module.list_pages()
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0]["url"], "http://gears.example/guide")
                # No embeddings stored — chunks are plain text.
                with open(os.path.join(tmp, "p.json")) as handle:
                    data = json.load(handle)
                self.assertNotIn("embedding", json.dumps(data))
                # Sensitive, local, and thin pages are skipped.
                self.assertFalse(memory_module.remember_page(
                    "http://mybank.example/login", "Bank", text))
                self.assertFalse(memory_module.remember_page(
                    "http://127.0.0.1:9/", "Local", text))
                self.assertFalse(memory_module.remember_page(
                    "http://x.example/", "Thin", "hi"))
                self.assertEqual(len(memory_module.list_pages()), 1)
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_search_pages_ranks_by_embedding(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)
                memory_module.remember_page(
                    "http://soup.example/", "Soup", "Soup is warm food. " * 40)

                def fake_embed(texts, model=None):
                    return [[1.0, 0.0] if "gear" in t.lower() else [0.0, 1.0]
                            for t in texts]

                found = memory_module.search_pages("gears", embed_fn=fake_embed)
                self.assertEqual([f["url"] for f in found],
                                 ["http://gears.example/"])
                self.assertIn("Gears turn wheels", found[0]["text"])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_search_pages_falls_back_to_keywords(self):
        # R3: with embeddings down, recall works by keyword overlap
        # instead of returning nothing.
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)

                def boom(texts, model=None):
                    raise BrowserError("down")

                found = memory_module.search_pages("gears", embed_fn=boom)
                self.assertEqual([f["url"] for f in found],
                                 ["http://gears.example/"])
                self.assertEqual(
                    memory_module.search_pages("zebra", embed_fn=boom), [])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_cap_keeps_newest_fact(self):
        # R1: the 201st remember used to be silently dropped.
        from forge.browser.memory import list_entries, remember
        for i in range(201):
            remember(f"fact number {i}")
        texts = [e["text"] for e in list_entries()]
        self.assertEqual(len(texts), 200)
        self.assertIn("fact number 200", texts)
        self.assertNotIn("fact number 0", texts)

    def test_forget_short_needle_removes_nothing(self):
        # R2: forget("e") used to wipe the whole store.
        from forge.browser.memory import forget, list_entries, remember
        remember("I like dark mode.")
        remember("My budget is $500.")
        self.assertEqual(forget("e"), 0)
        self.assertEqual(forget("ab"), 0)
        self.assertEqual(len(list_entries()), 2)
        self.assertEqual(forget("budget"), 1)

    def test_embedding_tie_break_goes_to_newest(self):
        # R7: identical vectors used to hand ties to the oldest fact.
        import time
        from forge.browser.memory import remember, search_entries
        remember("First fact about gears.")
        time.sleep(0.01)
        remember("Second fact about gears.")

        def fake_embed(texts):
            return [[1.0, 0.0] for _t in texts]

        found = search_entries("gears", embed_fn=fake_embed)
        self.assertEqual(found[0]["text"], "Second fact about gears.")

    def test_long_page_keeps_24_chunks(self):
        # R5: the cap was 12 — a long page kept only its head.
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                self.assertTrue(memory_module.remember_page(
                    "http://long.example/", "Long", "word " * 12000))
                pages = memory_module.page_texts()
                self.assertEqual(len(pages[0]["chunks"]), 24)
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_embed_cache_reuses_vectors_and_persists(self):
        # R4: a recall embeds only the query + new/changed chunks.
        from forge.browser.memory import EmbedCache
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            return [[float(len(t)), 0.0] for t in texts]

        path = os.path.join(self.tmp, "cache.json")
        cache = EmbedCache(path)
        first = cache.vectors_for(["aaa", "bbb"], fake_embed)
        self.assertEqual(len(calls), 1)
        second = cache.vectors_for(["aaa", "bbb", "ccc"], fake_embed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], ["ccc"])
        self.assertEqual(first[0], second[0])
        # Persisted: a fresh cache on the same path embeds nothing.
        fresh = EmbedCache(path)
        fresh.vectors_for(["aaa", "bbb", "ccc"], fake_embed)
        self.assertEqual(len(calls), 2)

    def test_search_entries_reuses_cached_fact_vectors(self):
        # R6: each turn used to re-embed all 200 facts.
        from forge.browser.memory import (EmbedCache, remember,
                                          search_entries)
        remember("I like dark mode.")
        remember("My budget is $500.")
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            return [[1.0, 0.0] if "dark" in t.lower() else [0.0, 1.0]
                    for t in texts]

        cache = EmbedCache(os.path.join(self.tmp, "cache.json"))
        search_entries("dark mode please", embed_fn=fake_embed,
                       embed_cache=cache)
        self.assertGreater(len(calls), 0)
        search_entries("dark mode again", embed_fn=fake_embed,
                       embed_cache=cache)
        self.assertEqual(calls[-1], ["dark mode again"])

    def test_forget_page_by_url_and_title(self):
        # R9: no way to remove a stored page before.
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)
                memory_module.remember_page(
                    "http://soup.example/", "Soup", "Soup is warm food. " * 40)
                self.assertEqual(
                    memory_module.forget_page("http://gears.example/"), 1)
                self.assertEqual(memory_module.forget_page("Soup"), 1)
                self.assertEqual(memory_module.forget_page("no such page"), 0)
                self.assertEqual(memory_module.forget_page("So"), 0)
                self.assertEqual(memory_module.list_pages(), [])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_cli_memory_page_forget(self):
        # R9: `memory page-forget <url or title>` from the terminal.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)
                args = mock.Mock(action="page-forget",
                                 value="http://gears.example/")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli_module._cmd_memory(args)
                self.assertEqual(code, 0)
                self.assertIn("forgot 1 stored page", out.getvalue())
                self.assertEqual(memory_module.list_pages(), [])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

    def test_saves_leave_no_tmp_behind(self):
        # R10: writes are atomic (temp + rename).
        from forge.browser.memory import remember
        remember("I like dark mode.")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "m.json.tmp")))
        with open(os.path.join(self.tmp, "m.json")) as handle:
            data = json.load(handle)
        self.assertEqual(len(data["entries"]), 1)

    def test_memory_threshold_report(self):
        # R12: the 0.5-floor validation harness.
        from forge.browser.memory import memory_threshold_report

        def fake_embed(texts):
            vectors = []
            for t in texts:
                low = t.lower()
                if "dark" in low:
                    vectors.append([1.0, 0.0, 0.0, 0.0])
                elif "budget" in low:
                    vectors.append([0.0, 1.0, 0.0, 0.0])
                elif "tea" in low:
                    vectors.append([0.0, 0.0, 1.0, 0.0])
                else:
                    vectors.append([0.0, 0.0, 0.0, 1.0])
            return vectors

        report = memory_threshold_report(fake_embed)
        self.assertTrue(report["separated"])
        self.assertEqual(report["same"]["min"], 1.0)
        self.assertEqual(report["different"]["max"], 0.0)

        def flat_embed(texts):
            return [[1.0, 0.0] for _t in texts]

        self.assertFalse(memory_threshold_report(flat_embed)["separated"])


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

    def test_chat_action_forgets_stored_page(self):
        # R9: the forget_page ACTION removes a stored page.
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)
                session = chat.ChatSession(model="fake:1b")
                queue = ['ACTION {"type": "forget_page", '
                         '"text": "http://gears.example/"}',
                         "Forgot it."]
                session._chat = lambda messages, on_chunk=None: queue.pop(0)
                answer, _opens = session.ask("forget the gears page")
                self.assertEqual(answer, "Forgot it.")
                self.assertEqual(memory_module.list_pages(), [])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old

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

    def test_tab_reference_selects_named_tab(self):
        session = self.make_session(["Tab two says hi."])
        tabs = [
            {"title": "First", "url": "http://a.example",
             "text": "first tab words", "current": True},
            {"title": "Second", "url": "http://b.example",
             "text": "second tab words", "current": False},
        ]
        answer, _opens = session.ask("@tab2 what does it say", tabs=tabs)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertTrue(any("Open tabs:" in t and "Second" in t
                            for t in system_texts))
        self.assertTrue(any("second tab words" in t for t in system_texts))
        self.assertFalse(any("first tab words" in t for t in system_texts))
        self.assertEqual(answer, "Tab two says hi.")

    def test_tab_reference_out_of_range_is_plain(self):
        session = self.make_session(["There is no tab 9."])
        tabs = [{"title": "Only", "url": "http://a.example",
                 "text": "only words", "current": True}]
        answer, _opens = session.ask("@tab9 what does it say", tabs=tabs)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertTrue(any("only 1 tab" in t for t in system_texts))
        self.assertFalse(any("only 1 tabs" in t for t in system_texts))
        self.assertEqual(answer, "There is no tab 9.")

    def test_tab_mention_without_tabs_is_plain(self):
        session = self.make_session(["No tabs here."])
        answer, _opens = session.ask("@tab2 what does it say")
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertTrue(any("has no tabs" in t for t in system_texts))
        self.assertEqual(answer, "No tabs here.")

    def test_tab_list_groups_related_tabs(self):
        from forge.browser import retrieve as retrieve_module
        session = self.make_session(["grouped answer"])
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            return [[1.0, 0.0] if "gear" in t.lower() or "wheel" in t.lower()
                    else [0.0, 1.0] for t in texts]

        retrieve_module.embed_texts = fake_embed
        try:
            tabs = [
                {"title": "Gears", "url": "http://a.example/1",
                 "text": "gears", "current": True},
                {"title": "Wheels", "url": "http://b.example/2",
                 "text": "wheels", "current": False},
                {"title": "Soup", "url": "http://c.example/3",
                 "text": "soup", "current": False},
            ]
            answer, _opens = session.ask("what is open", tabs=tabs)
        finally:
            retrieve_module.embed_texts = original
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        tab_note = [t for t in system_texts if t.startswith("Open tabs:")][0]
        # J3: visible order (numbers ascending), the group name a
        # label on each line — no grouped blocks reordering them.
        self.assertIn("[group:", tab_note)
        self.assertLess(tab_note.index("1. Gears"),
                        tab_note.index("2. Wheels"))
        self.assertLess(tab_note.index("2. Wheels"),
                        tab_note.index("3. Soup"))
        self.assertEqual(answer, "grouped answer")

    def test_multiple_tab_refs_feed_both(self):
        session = self.make_session(["Both tabs say hi."])
        tabs = [
            {"title": "First", "url": "http://a.example",
             "text": "first tab words", "current": True},
            {"title": "Second", "url": "http://b.example",
             "text": "second tab words", "current": False},
            {"title": "Third", "url": "http://c.example",
             "text": "third tab words", "current": False},
        ]
        answer, _opens = session.ask("@tab1 and @tab3, compare them",
                                     tabs=tabs)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call
                        if m["role"] == "system"]
        joined = "\n".join(system_texts)
        self.assertIn("first tab words", joined)
        self.assertIn("third tab words", joined)
        self.assertNotIn("second tab words", joined)
        self.assertIn("(@tab1)", joined)
        self.assertIn("(@tab3)", joined)
        self.assertEqual(answer, "Both tabs say hi.")

    def test_empty_named_tab_is_named(self):
        session = self.make_session(["Nothing there."])
        notes = []
        tabs = [
            {"title": "First", "url": "http://a.example",
             "text": "first tab words", "current": True},
            {"title": "Second", "url": "http://b.example",
             "text": "", "current": False},
        ]
        answer, _opens = session.ask("@tab2 what does it say", tabs=tabs,
                                     on_note=notes.append)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call
                        if m["role"] == "system"]
        joined = "\n".join(system_texts)
        self.assertIn("had no readable text", joined)
        self.assertEqual(notes, ["Tab 2 (Second) had no readable text."])
        self.assertEqual(answer, "Nothing there.")

    def test_tab_list_visible_order_with_group_labels(self):
        from forge.browser import retrieve as retrieve_module
        session = self.make_session(["ordered answer"])
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            return [[1.0, 0.0] if "gear" in t.lower()
                    else [0.0, 1.0] for t in texts]

        retrieve_module.embed_texts = fake_embed
        try:
            tabs = [
                {"title": "Gear guide", "url": "http://a.example/1",
                 "text": "gears", "current": True},
                {"title": "News", "url": "http://b.example/2",
                 "text": "news", "current": False},
                {"title": "Gear parts", "url": "http://a.example/3",
                 "text": "gears", "current": False},
            ]
            answer, _opens = session.ask("what is open", tabs=tabs)
        finally:
            retrieve_module.embed_texts = original
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call
                        if m["role"] == "system"]
        tab_note = [t for t in system_texts
                    if t.startswith("Open tabs:")][0]
        # Tabs 1 and 3 share a group, but the list still prints
        # 1, 2, 3 in visible order.
        self.assertLess(tab_note.index("1. Gear guide"),
                        tab_note.index("2. News"))
        self.assertLess(tab_note.index("2. News"),
                        tab_note.index("3. Gear parts"))
        self.assertIn("[group:", tab_note)
        self.assertEqual(answer, "ordered answer")

    def test_tab_list_caps_at_fifteen(self):
        session = self.make_session(["many tabs"])
        tabs = [{"title": f"Tab {i}", "url": f"http://x.example/{i}",
                 "text": f"words {i}", "current": i == 1}
                for i in range(1, 21)]
        answer, _opens = session.ask("what is open", tabs=tabs)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call
                        if m["role"] == "system"]
        tab_note = [t for t in system_texts
                    if t.startswith("Open tabs:")][0]
        self.assertIn("15. Tab 15", tab_note)
        self.assertNotIn("16. Tab 16", tab_note)
        self.assertIn("and 5 more tabs", tab_note)
        self.assertEqual(answer, "many tabs")

    def test_tab_reference_out_of_range_plural(self):
        session = self.make_session(["There is no tab 9."])
        tabs = [{"title": "One", "url": "http://a.example",
                 "text": "one", "current": True},
                {"title": "Two", "url": "http://b.example",
                 "text": "two", "current": False}]
        answer, _opens = session.ask("@tab9 what does it say", tabs=tabs)
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertTrue(any("only 2 tabs" in t for t in system_texts))
        self.assertEqual(answer, "There is no tab 9.")

    def test_memory_context_offered_when_relevant(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(tmp, "m.json")
            try:
                memory_module.remember("I like dark mode.")
                session = self.make_session(["Use dark mode then."])
                answer, _opens = session.ask("should I use dark mode?")
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_MEMORY_PATH"] = old
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertTrue(any("I like dark mode." in t for t in system_texts))
        self.assertEqual(answer, "Use dark mode then.")

    def test_on_step_names_thinking_then_action(self):
        session = self.make_session(
            ['ACTION {"type": "read_page"}', "Here is the page."])
        steps = []
        answer, _opens = session.ask(
            "what does the page say", page_text="page words here",
            on_step=steps.append)
        self.assertEqual(steps, ["thinking", "read_page", "thinking"])
        self.assertEqual(answer, "Here is the page.")

    def test_memory_withheld_when_feature_off(self):
        from forge.browser import memory as memory_module
        from forge.browser import seats as seats_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(tmp, "m.json")
            os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(tmp, "seats.json")
            try:
                memory_module.remember("I like dark mode.")
                seats_module.set_feature("memory", False)
                session = self.make_session(["Use dark mode then."])
                answer, _opens = session.ask("should I use dark mode?")
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_MEMORY_PATH"] = old
                del os.environ["FORGE_BROWSER_SEATS_PATH"]
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertFalse(any("I like dark mode." in t for t in system_texts))

    def test_memory_withheld_on_sensitive_page(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(tmp, "m.json")
            try:
                memory_module.remember("I like dark mode.")
                session = self.make_session(["Use dark mode then."])
                answer, _opens = session.ask(
                    "should I use dark mode?",
                    page_url="https://mybank.example/login")
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_MEMORY_PATH"] = old
        first_call = session.seen[0]
        system_texts = [m["content"] for m in first_call if m["role"] == "system"]
        self.assertFalse(any("I like dark mode." in t for t in system_texts))

    def test_remember_action_stores_fact(self):
        from forge.browser.memory import list_entries
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(tmp, "m.json")
            try:
                session = chat.ChatSession(model="fake:1b")
                queue = ['ACTION {"type": "remember", "text": "I like dark mode."}',
                         "Got it, remembered."]

                def fake_chat(messages, on_chunk=None):
                    return queue.pop(0)

                session._chat = fake_chat
                answer, _opens = session.ask("remember that I like dark mode")
                stored = [e["text"] for e in list_entries()]
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_MEMORY_PATH"] = old
        self.assertEqual(answer, "Got it, remembered.")
        self.assertEqual(stored, ["I like dark mode."])

    def test_recall_pages_action_searches_visited_pages(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gear Guide",
                    "Gears turn wheels. " * 40)
                original = memory_module.search_pages

                def fake_search(query, top_k=3, embed_fn=None):
                    return [{"url": "http://gears.example/", "title": "Gear Guide",
                             "text": "Gears turn wheels.", "score": 0.9}]

                memory_module.search_pages = fake_search
                session = chat.ChatSession(model="fake:1b")
                queue = ['ACTION {"type": "recall_pages", "query": "gears"}',
                         "You read about gears turning wheels."]

                def fake_chat(messages, on_chunk=None):
                    return queue.pop(0)

                session._chat = fake_chat
                try:
                    answer, _opens = session.ask("that thing I read about gears")
                finally:
                    memory_module.search_pages = original
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old
        self.assertEqual(answer, "You read about gears turning wheels.")

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
        original = chat.pick_chat_model_with_reason
        chat.pick_chat_model_with_reason = lambda models=None, sizes=None: (
            None, "none", "no chat model is installed")
        try:
            session = chat.ChatSession()
        finally:
            chat.pick_chat_model_with_reason = original
        with self.assertRaises(BrowserError):
            session.ask("hi")


class TurnStatusTests(unittest.TestCase):
    """#16: the dock's working line, with elapsed time."""

    def test_status_line_names_step_and_seconds(self):
        from forge.browser.app import turn_status_text
        # V1: plain words, not the raw action kind.
        self.assertEqual(turn_status_text("search", 100.0, now=112.0),
                         "working: searching… 12s")
        # V5: "<1s", not "0s".
        self.assertEqual(turn_status_text("thinking", 100.0, now=100.4),
                         "working: thinking… <1s")

    def test_step_words_plain(self):
        # V1: the working line's vocabulary, from describe_action.
        from forge.browser.chat import step_words
        self.assertEqual(step_words("search"), "searching")
        self.assertEqual(step_words("read_page"), "reading the page")
        self.assertEqual(step_words("recall_pages"),
                         "searching visited pages")
        self.assertEqual(step_words("compare"), "comparing")
        self.assertEqual(step_words("thinking"), "thinking")
        # The dock's own phases are already plain words.
        self.assertEqual(step_words("reading tabs"), "reading tabs")
        self.assertEqual(step_words("waiting for approval"),
                         "waiting for approval")

    def test_format_duration(self):
        # V5: sub-second turns say "<1s".
        from forge.browser.chat import format_duration
        self.assertEqual(format_duration(0.0), "<1s")
        self.assertEqual(format_duration(0.4), "<1s")
        self.assertEqual(format_duration(1.0), "1s")
        self.assertEqual(format_duration(12.7), "12s")

    def test_status_line_subtracts_waited(self):
        # V2: plan-gate time is not work — the clock pauses.
        from forge.browser.app import turn_status_text
        self.assertEqual(
            turn_status_text("search", 100.0, now=112.0, waited=11.0),
            "working: searching… 1s")

    def test_done_line_names_waiting(self):
        # V2: the done line names the waiting time it excluded.
        from forge.browser.app import turn_done_text
        self.assertEqual(
            turn_done_text("done", 100.0, now=123.0, waited=11.0),
            "Done in 12s (11s waiting).")
        self.assertEqual(
            turn_done_text("done", 100.0, now=112.0, speed=4.2,
                           waited=2.0),
            "Done in 10s (4.2 tok/s, 2s waiting).")

    def test_done_line_sub_second(self):
        # V5: "Done in <1s.", not "Done in 0s."
        from forge.browser.app import turn_done_text
        self.assertEqual(turn_done_text("done", 100.0, now=100.4),
                         "Done in <1s.")

    def test_ask_fires_step_done(self):
        # V4: each brain call ("thinking") and each action reports
        # its duration when it finishes.
        from forge.browser import chat
        session = chat.ChatSession(model="fake:1b")
        calls = []

        def fake_chat(messages, on_chunk=None):
            calls.append(messages)
            if len(calls) == 1:
                return 'ACTION {"type": "search", "query": "gears"}'
            return "found it"

        session._chat = fake_chat
        original_search = chat._run_search
        chat._run_search = lambda query: "a result"
        steps = []
        try:
            session.ask("find gears",
                        on_step_done=lambda kind, seconds: (
                            steps.append((kind, seconds))))
        finally:
            chat._run_search = original_search
        self.assertEqual([kind for kind, _ in steps],
                         ["thinking", "search", "thinking"])
        for _kind, seconds in steps:
            self.assertGreaterEqual(seconds, 0)
            self.assertLess(seconds, 60)

    def test_run_card_fires_step_done(self):
        # V4: a card's steps report their durations too.
        from forge.browser import chat
        from forge.browser.cards import run_card
        tmp = tempfile.mkdtemp()
        old = os.environ.get("FORGE_BROWSER_CARDS_DIR")
        os.environ["FORGE_BROWSER_CARDS_DIR"] = tmp
        try:
            with open(os.path.join(tmp, "demo.md"), "w") as handle:
                handle.write("# Demo\nPresent the summary.\n## Actions\n"
                             'ACTION {"type": "read_page"}\n')
            session = chat.ChatSession(model="fake:1b")
            session._chat = lambda messages, on_chunk=None: "card answer"
            steps = []
            answer, _opens = run_card(
                session, "demo", page_text="some page text",
                on_step_done=lambda kind, seconds: (
                    steps.append((kind, seconds))))
            self.assertEqual(answer, "card answer")
            self.assertEqual([kind for kind, _ in steps],
                             ["read_page", "thinking"])
            for _kind, seconds in steps:
                self.assertGreaterEqual(seconds, 0)
                self.assertLess(seconds, 60)
        finally:
            if old is None:
                os.environ.pop("FORGE_BROWSER_CARDS_DIR", None)
            else:
                os.environ["FORGE_BROWSER_CARDS_DIR"] = old

    def test_done_line_by_outcome(self):
        from forge.browser.app import turn_done_text
        self.assertEqual(turn_done_text("done", 100.0, now=123.0),
                         "Done in 23s.")
        self.assertEqual(turn_done_text("stopped", 100.0, now=108.0),
                         "Stopped after 8s.")
        self.assertEqual(turn_done_text("failed", 100.0, now=105.0),
                         "Failed after 5s.")

    def test_done_line_shows_speed(self):
        # G10: the per-turn tok/s readout rides the done line.
        from forge.browser.app import turn_done_text
        self.assertEqual(turn_done_text("done", 100.0, now=112.0, speed=4.2),
                         "Done in 12s (4.2 tok/s).")
        self.assertEqual(turn_done_text("done", 100.0, now=112.0, speed=None),
                         "Done in 12s.")


class SeatsTests(unittest.TestCase):
    """#14: seat picks + AI toggles saved in the seats file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(
            self.tmp.name, "seats.json")

    def tearDown(self):
        del os.environ["FORGE_BROWSER_SEATS_PATH"]
        self.tmp.cleanup()

    def test_defaults_all_on_no_picks(self):
        from forge.browser.seats import load_seats
        seats = load_seats()
        self.assertIsNone(seats["brain"])
        self.assertIsNone(seats["fast"])
        self.assertIsNone(seats["vision"])
        self.assertTrue(seats["ai_enabled"])
        self.assertTrue(all(seats["features"].values()))

    def test_set_seat_round_trips_and_clears(self):
        from forge.browser.seats import load_seats, set_seat
        set_seat("brain", "qwen3:4b")
        self.assertEqual(load_seats()["brain"], "qwen3:4b")
        set_seat("brain", None)
        self.assertIsNone(load_seats()["brain"])
        set_seat("vision", "  ")
        self.assertIsNone(load_seats()["vision"])

    def test_master_switch_kills_every_feature(self):
        from forge.browser.seats import seats_enabled, set_feature
        set_feature("ai", False)
        for feature in ("chat", "tidy_titles", "memory"):
            self.assertFalse(seats_enabled(feature))

    def test_feature_toggle_kills_only_that_feature(self):
        from forge.browser.seats import seats_enabled, set_feature
        set_feature("tidy_titles", False)
        self.assertFalse(seats_enabled("tidy_titles"))
        self.assertTrue(seats_enabled("chat"))
        self.assertTrue(seats_enabled("memory"))

    def test_fast_pick_defaults_to_none(self):
        from forge.browser.seats import pick_fast_model, set_seat
        self.assertIsNone(pick_fast_model())
        set_seat("fast", "qwen3:1.7b")
        self.assertEqual(pick_fast_model(), "qwen3:1.7b")

    def test_vision_pick_beats_auto_choice(self):
        from unittest import mock
        from forge.browser import media, seats as seats_module
        seats_module.set_seat("vision", "moondream:1.8b")
        with mock.patch.object(media, "installed_models",
                               return_value=["qwen3.5:4b"]):
            self.assertEqual(media.pick_vision_model(), "moondream:1.8b")

    def test_set_seat_rejects_unknown_names(self):
        # T10: a bogus seat name is ignored, not stored.
        from forge.browser.seats import load_seats, set_seat
        set_seat("bogus", "qwen3:4b")
        self.assertNotIn("bogus", load_seats())
        self.assertIsNone(load_seats()["brain"])

    def test_writes_are_atomic_and_locked(self):
        # T9: no .tmp file is left behind, and two threads
        # writing different seats do not clobber each other.
        import threading
        from forge.browser.seats import load_seats, set_seat
        set_seat("brain", "qwen3:4b")
        path = os.environ["FORGE_BROWSER_SEATS_PATH"]
        self.assertFalse(os.path.exists(path + ".tmp"))

        def write_brain():
            for _ in range(30):
                set_seat("brain", "qwen3:4b")

        def write_fast():
            for _ in range(30):
                set_seat("fast", "qwen3:1.7b")

        threads = [threading.Thread(target=write_brain),
                   threading.Thread(target=write_fast)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seats = load_seats()
        self.assertEqual(seats["brain"], "qwen3:4b")
        self.assertEqual(seats["fast"], "qwen3:1.7b")

    def test_cli_brief_refuses_when_ai_off(self):
        # T2: the brief does not build when AI/chat is off.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import brief as brief_module
        from forge.browser import cli as cli_module
        from forge.browser.seats import set_feature
        set_feature("ai", False)
        with mock.patch.object(
                brief_module, "build_brief",
                side_effect=AssertionError("built while AI is off")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module._cmd_brief(mock.Mock())
        self.assertEqual(code, 0)
        self.assertIn("AI is off", out.getvalue())

    def test_cli_voice_refuses_when_ai_off(self):
        # T3: the CLI voice path honors the chat gate, like the
        # dock mic — no recording starts while AI/chat is off.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import voice as voice_module
        from forge.browser.seats import set_feature
        set_feature("chat", False)
        with mock.patch.object(
                voice_module, "find_recorder",
                side_effect=AssertionError("recorded while chat is off")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module._cmd_voice(mock.Mock(seconds=1))
        self.assertEqual(code, 0)
        self.assertIn("AI is off", out.getvalue())

    def test_cli_describe_refuses_when_ai_off(self):
        # T4: describing is an AI feature — the master switch
        # kills it.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import media as media_module
        from forge.browser.seats import set_feature
        set_feature("ai", False)
        with mock.patch.object(
                media_module, "pick_vision_model",
                side_effect=AssertionError("described while AI is off")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module._cmd_describe(
                    mock.Mock(file="/tmp/x.png"))
        self.assertEqual(code, 0)
        self.assertIn("AI is off", out.getvalue())

    def test_cli_describe_runs_when_only_chat_off(self):
        # T4: describing is gated on the master switch only —
        # the chat toggle does not kill it.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import media as media_module
        from forge.browser.seats import set_feature
        set_feature("chat", False)
        with mock.patch.object(media_module, "pick_vision_model",
                               return_value="moondream:1.8b"):
            with mock.patch.object(media_module, "describe_saved_image",
                                   return_value="a cat"):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli_module._cmd_describe(
                        mock.Mock(file="/tmp/x.png"))
        self.assertEqual(code, 0)
        self.assertIn("a cat", out.getvalue())


class ResearchJobTests(unittest.TestCase):
    """#11: research runs in the background; the turn doesn't wait."""

    def test_research_action_starts_background_job(self):
        from unittest import mock
        from forge.browser import research as research_module
        captured = {}

        def fake_start(topic, on_progress=None, on_done=None, **kwargs):
            captured["topic"] = topic
            captured["on_done"] = on_done
            return topic

        session = chat.ChatSession(model="fake:1b")
        queue = ['ACTION {"type": "research", "topic": "gears"}',
                 "Research is running, I'll show you when it's done."]

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        delivered = {}
        with mock.patch.object(research_module, "start_research_job", fake_start):
            with mock.patch.object(research_module, "research_job_running",
                                   return_value=None):
                answer, _opens = session.ask(
                    "research gears for me", on_plan=lambda plan, kind: True,
                    on_research_done=lambda r, s, e=None: delivered.setdefault("report", r))
        self.assertEqual(captured["topic"], "gears")
        self.assertEqual(answer, "Research is running, I'll show you when it's done.")
        captured["on_done"]("# Research: gears\n\ntext", [], None)
        self.assertIn("Research: gears", delivered["report"])

    def test_research_action_queues_second_job(self):
        # F1: a second ask queues behind the running job instead of
        # being refused.
        from unittest import mock
        from forge.browser import research as research_module
        session = chat.ChatSession(model="fake:1b")
        seen = []
        queue = ['ACTION {"type": "research", "topic": "gears"}', "ok, waiting"]
        started = []

        def fake_chat(messages, on_chunk=None):
            seen.append(messages)
            return queue.pop(0)

        def fake_start(topic, **kwargs):
            started.append(topic)
            return topic

        session._chat = fake_chat
        with mock.patch.object(research_module, "research_job_running",
                               return_value="bikes"):
            with mock.patch.object(research_module, "start_research_job",
                                   fake_start):
                with mock.patch.object(research_module, "research_queue",
                                       return_value=["gears"]):
                    answer, _opens = session.ask("research gears for me",
                                                 on_plan=lambda plan, kind: True)
        self.assertEqual(answer, "ok, waiting")
        self.assertEqual(started, ["gears"])
        result = [m["content"] for m in seen[1]
                  if m["content"].startswith("RESULT of research")][0]
        self.assertIn("already running", result)
        self.assertIn("bikes", result)
        self.assertIn("queued", result)
        self.assertIn("position 1", result)

    def test_second_job_queues_and_runs_in_order(self):
        # F1: the queue drains itself — job 2 starts when job 1
        # finishes, and both reports arrive in order.
        import threading as _threading
        from forge.browser import research as research_module
        research_module._CURRENT_JOB["topic"] = None
        research_module._JOB_QUEUE.clear()
        release_first = _threading.Event()
        done = []
        finished = _threading.Event()

        def search_fn(query, max_results=6):
            if query == "first":
                release_first.wait(timeout=10)
            return []

        def on_done(report, sources, error=None):
            done.append(report)
            if len(done) == 2:
                finished.set()

        try:
            self.assertEqual(
                research_module.start_research_job(
                    "first", search_fn=search_fn, on_done=on_done), "first")
            self.assertEqual(
                research_module.start_research_job(
                    "second", search_fn=search_fn, on_done=on_done), "second")
            self.assertEqual(research_module.research_job_running(), "first")
            self.assertEqual(research_module.research_queue(), ["second"])
            release_first.set()
            self.assertTrue(finished.wait(timeout=10))
            self.assertIn("Research: first", done[0])
            self.assertIn("Research: second", done[1])
            self.assertIsNone(research_module.research_job_running())
            self.assertEqual(research_module.research_queue(), [])
        finally:
            release_first.set()
            research_module._CURRENT_JOB["topic"] = None
            research_module._JOB_QUEUE.clear()

    def test_queue_is_empty_when_idle(self):
        from forge.browser import research as research_module
        research_module._CURRENT_JOB["topic"] = None
        research_module._JOB_QUEUE.clear()
        self.assertEqual(research_module.research_queue(), [])
        self.assertIsNone(research_module.research_job_running())


class CompareTests(unittest.TestCase):
    """#7: cross-tab compare — one tiny call per tab, table in the window."""

    def setUp(self):
        from forge.browser.compare import clear_extraction_cache
        clear_extraction_cache()

    def tearDown(self):
        from forge.browser.compare import clear_extraction_cache
        clear_extraction_cache()

    def _session(self, replies):
        session = chat.ChatSession(model="fake:1b")
        queue = list(replies)

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        return session

    def test_extract_fields_parses_json(self):
        from forge.browser.compare import extract_fields
        session = self._session(['{"price": "$42", "specs": "steel"}'])
        out = extract_fields(session, "Gear A", "The Gear A costs $42, steel.",
                             ["price", "specs"])
        self.assertEqual(out, {"price": "$42", "specs": "steel"})

    def test_extract_fields_missing_keys(self):
        from forge.browser.compare import extract_fields
        session = self._session(['{"price": "$42"}'])
        out = extract_fields(session, "Gear A", "text", ["price", "specs"])
        self.assertEqual(out, {"price": "$42", "specs": "not listed"})

    def test_extract_fields_bad_json(self):
        from forge.browser.compare import extract_fields
        session = self._session(['no json here'])
        out = extract_fields(session, "Gear A", "text", ["price"])
        self.assertEqual(out, {"price": "couldn't extract"})

    def test_extract_fields_fenced_json(self):
        from forge.browser.compare import extract_fields
        session = self._session(['```json\n{"price": "$42"}\n```'])
        out = extract_fields(session, "Gear A", "text", ["price"])
        self.assertEqual(out, {"price": "$42"})

    def test_compare_tabs_one_call_per_tab(self):
        from forge.browser.compare import compare_tabs
        session = self._session(['{"price": "$42"}', '{"price": "$55"}'])
        tabs = [{"title": "Gear A", "url": "http://a.example/",
                 "text": "Gear A costs $42."},
                {"title": "Gear B", "url": "http://b.example/",
                 "text": "Gear B costs $55."}]
        rows, dropped = compare_tabs(session, tabs, ["price"])
        self.assertEqual(dropped, 0)
        self.assertEqual([(r["title"], r["fields"]["price"]) for r in rows],
                         [("Gear A", "$42"), ("Gear B", "$55")])

    def test_compare_table_text(self):
        from forge.browser.compare import compare_table_text
        rows = [{"title": "Gear A", "url": "", "fields": {"price": "$42"}},
                {"title": "Gear B", "url": "", "fields": {"price": "not listed"}}]
        text = compare_table_text(rows, ["price"])
        self.assertIn("| Gear A | Gear B |", text)
        self.assertIn("| price | $42 | not listed |", text)

    def test_compare_table_html_escapes(self):
        from forge.browser.compare import compare_table_html
        rows = [{"title": "<Gear & Co>", "url": "",
                 "fields": {"price": "$42"}}]
        page = compare_table_html(rows, ["price"])
        self.assertIn("<table>", page)
        self.assertIn("&lt;Gear &amp; Co&gt;", page)
        self.assertNotIn("<Gear", page)

    def test_compare_action_via_chat(self):
        queue = ['ACTION {"type": "compare", "tabs": [1, 2], "fields": ["price"]}',
                 '{"price": "$42"}', '{"price": "$55"}', "here's the comparison"]
        session = chat.ChatSession(model="fake:1b")
        seen_tables = []

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        tabs = [{"title": "Gear A", "url": "http://a.example/",
                 "text": "Gear A costs $42.", "current": True},
                {"title": "Gear B", "url": "http://b.example/",
                 "text": "Gear B costs $55.", "current": False}]
        answer, _opens = session.ask("compare @tab1 @tab2 on price", tabs=tabs,
                                     on_plan=lambda plan, kind: True,
                                     on_table=seen_tables.append)
        self.assertEqual(answer, "here's the comparison")
        self.assertEqual(len(seen_tables), 1)
        self.assertIn("$42", seen_tables[0])
        self.assertIn("$55", seen_tables[0])
        self.assertIn("<table>", seen_tables[0])

    def test_extract_fields_preamble(self):
        from forge.browser.compare import extract_fields
        session = self._session(['Here is what I found:\n{"price": "$42"}'])
        out = extract_fields(session, "Gear A", "text", ["price"])
        self.assertEqual(out, {"price": "$42"})

    def test_extract_fields_case_insensitive_keys(self):
        from forge.browser.compare import extract_fields
        session = self._session(['{"Price": "$42", "SPECS": "steel"}'])
        out = extract_fields(session, "Gear A", "text", ["price", "specs"])
        self.assertEqual(out, {"price": "$42", "specs": "steel"})

    def test_compare_tabs_reports_dropped(self):
        from forge.browser.compare import compare_tabs
        session = self._session(['{"price": "$1"}'] * 8)
        tabs = [{"title": f"Tab {i}", "url": f"http://t{i}.example/",
                 "text": f"tab {i} text"} for i in range(8)]
        rows, dropped = compare_tabs(session, tabs, ["price"])
        self.assertEqual(len(rows), 6)
        self.assertEqual(dropped, 2)

    def test_compare_table_shows_domain(self):
        from forge.browser.compare import (compare_table_html,
                                           compare_table_text)
        rows = [{"title": "Amazon", "url": "http://a.example/item",
                 "fields": {"price": "$42"}},
                {"title": "Amazon", "url": "http://b.example/item",
                 "fields": {"price": "$55"}}]
        text = compare_table_text(rows, ["price"])
        self.assertIn("| Amazon (a.example) | Amazon (b.example) |", text)
        page = compare_table_html(rows, ["price"])
        self.assertIn("<small>a.example</small>", page)
        self.assertIn("<small>b.example</small>", page)

    def test_compare_tabs_honors_cancel(self):
        from forge.browser.compare import compare_tabs
        session = chat.ChatSession(model="fake:1b")
        calls = []

        def fake_chat(messages, on_chunk=None):
            calls.append(1)
            if len(calls) == 1:
                session._stop_requested = True
            return '{"price": "$42"}'

        session._chat = fake_chat
        tabs = [{"title": f"Tab {i}", "url": f"http://t{i}.example/",
                 "text": f"tab {i} text"} for i in range(4)]
        rows, _dropped = compare_tabs(session, tabs, ["price"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(calls), 1)

    def test_compare_tabs_progress(self):
        from forge.browser.compare import compare_tabs
        session = self._session(['{"price": "$42"}', '{"price": "$55"}'])
        seen = []
        tabs = [{"title": "Gear A", "url": "http://a.example/", "text": "A"},
                {"title": "Gear B", "url": "http://b.example/", "text": "B"}]
        compare_tabs(session, tabs, ["price"],
                     on_progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_compare_table_dark_theme(self):
        from forge.browser.compare import compare_table_html
        rows = [{"title": "Gear A", "url": "", "fields": {"price": "$42"}}]
        page = compare_table_html(rows, ["price"])
        self.assertIn("#101216", page)
        self.assertIn("#171b22", page)
        self.assertNotIn("#f0ede6", page)

    def test_compare_table_text_fits_cap(self):
        from forge.browser.compare import compare_table_text
        fields = [f"field{i}" for i in range(12)]
        rows = [{"title": "Gear A", "url": "",
                 "fields": {f: "x" * 40 for f in fields}},
                {"title": "Gear B", "url": "",
                 "fields": {f: "y" * 40 for f in fields}}]
        text = compare_table_text(rows, fields, max_chars=600)
        self.assertLessEqual(len(text), 600)
        self.assertIn("left out to fit", text)
        self.assertIn("field0", text)

    def test_extraction_cache(self):
        from forge.browser.compare import extract_fields
        calls = []

        def fake_chat(messages, on_chunk=None):
            calls.append(1)
            return '{"price": "$42"}'

        session = chat.ChatSession(model="fake:1b")
        session._chat = fake_chat
        out1 = extract_fields(session, "Gear A", "same text", ["price"])
        out2 = extract_fields(session, "Gear A", "same text", ["price"])
        self.assertEqual(out1, out2)
        self.assertEqual(len(calls), 1)

    def test_parse_fields_from_text(self):
        from forge.browser.compare import parse_fields_from_text
        self.assertEqual(
            parse_fields_from_text("compare @tab1 @tab2 on price and warranty"),
            ["price", "warranty"])
        self.assertEqual(
            parse_fields_from_text("compare tabs 1 and 2 by price, battery"),
            ["price", "battery"])
        self.assertEqual(parse_fields_from_text("compare these"), [])
        self.assertEqual(parse_fields_from_text("compare on price"), ["price"])

    def test_compare_action_coerces_string_tabs(self):
        queue = ['ACTION {"type": "compare", "tabs": ["1", "2"], '
                 '"fields": ["price"]}',
                 '{"price": "$42"}', '{"price": "$55"}',
                 "here's the comparison"]
        session = chat.ChatSession(model="fake:1b")
        seen_tables = []

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        tabs = [{"title": "Gear A", "url": "http://a.example/",
                 "text": "Gear A costs $42.", "current": True},
                {"title": "Gear B", "url": "http://b.example/",
                 "text": "Gear B costs $55.", "current": False},
                {"title": "Gear C", "url": "http://c.example/",
                 "text": "Gear C costs $66.", "current": False}]
        answer, _opens = session.ask("compare tabs 1 and 2 on price", tabs=tabs,
                                     on_plan=lambda plan, kind: True,
                                     on_table=seen_tables.append)
        self.assertEqual(answer, "here's the comparison")
        self.assertEqual(len(seen_tables), 1)
        self.assertIn("$42", seen_tables[0])
        self.assertIn("$55", seen_tables[0])
        self.assertNotIn("$66", seen_tables[0])

    def test_compare_action_fields_from_words(self):
        queue = ['ACTION {"type": "compare", "tabs": [1, 2]}',
                 '{"warranty": "2 years"}', '{"warranty": "1 year"}',
                 "here's the comparison"]
        session = chat.ChatSession(model="fake:1b")
        seen_prompts = []

        def fake_chat(messages, on_chunk=None):
            seen_prompts.append(messages[-1]["content"])
            return queue.pop(0)

        session._chat = fake_chat
        tabs = [{"title": "Gear A", "url": "http://a.example/",
                 "text": "Gear A warranty 2 years.", "current": True},
                {"title": "Gear B", "url": "http://b.example/",
                 "text": "Gear B warranty 1 year.", "current": False}]
        answer, _opens = session.ask("compare @tab1 @tab2 on warranty", tabs=tabs,
                                     on_plan=lambda plan, kind: True)
        self.assertEqual(answer, "here's the comparison")
        extraction_prompts = [p for p in seen_prompts if "extract ONLY" in p]
        self.assertEqual(len(extraction_prompts), 2)
        for prompt in extraction_prompts:
            self.assertIn('"warranty"', prompt)

    def test_compare_csv(self):
        from forge.browser.compare import compare_table_csv
        rows = [{"title": "Gear A", "url": "", "fields": {"price": "$42"}},
                {"title": "Gear B", "url": "", "fields": {"price": "$55"}}]
        csv_text = compare_table_csv(rows, ["price"])
        self.assertIn("Field,Gear A,Gear B", csv_text)
        self.assertIn("price,$42,$55", csv_text)

    def test_last_comparison_roundtrip(self):
        from forge.browser.compare import (last_comparison,
                                           set_last_comparison)
        rows = [{"title": "Gear A", "url": "", "fields": {"price": "$42"}}]
        set_last_comparison(rows, ["price"])
        stored_rows, stored_fields = last_comparison()
        self.assertEqual(stored_rows, rows)
        self.assertEqual(stored_fields, ["price"])


class PreviewTests(unittest.TestCase):
    """#6: link previews — two lines, on demand, cached."""

    def setUp(self):
        from forge.browser import preview as preview_module
        preview_module.clear_preview_cache()

    def _result(self, kind="html", title="Gear Guide",
                text="Gears turn wheels.\nMore about gears.", size=1234):
        from forge.browser.files import FileResult
        return FileResult(url="http://gears.example/",
                          final_url="http://gears.example/",
                          kind=kind, title=title, text=text, size_bytes=size)

    def test_preview_two_lines_and_cached(self):
        from forge.browser import preview as preview_module
        calls = []

        def fake(url):
            calls.append(url)
            return self._result()

        # Title line + one snippet line (the first two source lines,
        # joined). The second hover on the same link costs no fetch.
        self.assertEqual(
            preview_module.preview_url("http://gears.example/", fetcher=fake),
            "Gear Guide\nGears turn wheels. More about gears.")
        self.assertEqual(
            preview_module.preview_url("http://gears.example/", fetcher=fake),
            "Gear Guide\nGears turn wheels. More about gears.")
        self.assertEqual(len(calls), 1)

    def test_preview_empty_and_anchor(self):
        from forge.browser import preview as preview_module
        self.assertEqual(preview_module.preview_url(""), "")
        self.assertEqual(preview_module.preview_url("#section"), "")

    def test_preview_fetch_failure(self):
        from forge.browser import preview as preview_module

        def boom(url):
            raise Exception("down")

        self.assertEqual(
            preview_module.preview_url("http://x.example/", fetcher=boom),
            "Couldn't fetch that link.")

    def test_preview_binary_kinds(self):
        from forge.browser import preview as preview_module
        self.assertEqual(
            preview_module.preview_url(
                "http://x.example/pic.png",
                fetcher=lambda u: self._result(kind="image", title="",
                                               text="", size=42000)),
            "image file, 41 KB.")
        self.assertEqual(
            preview_module.preview_url(
                "http://x.example/f.bin",
                fetcher=lambda u: self._result(kind="binary", title="Data",
                                               text="", size=10)),
            "Data — couldn't read it as text (unknown type).")

    def test_preview_no_readable_text(self):
        from forge.browser import preview as preview_module
        self.assertEqual(
            preview_module.preview_url(
                "http://x.example/empty",
                fetcher=lambda u: self._result(title="", text="   \n\n")),
            "That page has no readable text.")

    def test_cli_preview(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import preview as preview_module
        with mock.patch.object(preview_module, "preview_url",
                               return_value="Gear Guide\nGears turn wheels."):
            args = mock.Mock(url="http://gears.example/")
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module._cmd_preview(args)
        self.assertEqual(code, 0)
        self.assertIn("Gear Guide", out.getvalue())

    def test_head_first_names_media_without_fetch(self):
        from forge.browser import preview as preview_module

        def boom(url):
            raise AssertionError("the body must not be fetched")

        self.assertEqual(
            preview_module.preview_url(
                "http://x.example/clip.mp4", fetcher=boom,
                head_fetcher=lambda u: ("video/mp4", 2048 * 1024 * 1024)),
            "video file, 2 GB.")

    def test_head_first_text_kind_fetches_body(self):
        from forge.browser import preview as preview_module
        self.assertEqual(
            preview_module.preview_url(
                "http://gears.example/", fetcher=lambda u: self._result(),
                head_fetcher=lambda u: ("text/html", 0)),
            "Gear Guide\nGears turn wheels. More about gears.")

    def test_head_failure_falls_back_to_fetch(self):
        from forge.browser import preview as preview_module

        def no_head(url):
            raise Exception("HEAD refused")

        self.assertEqual(
            preview_module.preview_url(
                "http://gears.example/", fetcher=lambda u: self._result(),
                head_fetcher=no_head),
            "Gear Guide\nGears turn wheels. More about gears.")

    def test_failure_not_cached(self):
        from forge.browser import preview as preview_module
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) == 1:
                raise Exception("down")
            return self._result()

        self.assertEqual(
            preview_module.preview_url("http://x.example/", fetcher=flaky),
            "Couldn't fetch that link.")
        self.assertEqual(
            preview_module.preview_url("http://x.example/", fetcher=flaky),
            "Gear Guide\nGears turn wheels. More about gears.")
        self.assertEqual(len(calls), 2)

    def test_snippet_skips_nav_junk(self):
        from forge.browser import preview as preview_module
        text = ("Shop\nHome | About | Contact | Cart Sign in\n"
                "Real content paragraph here.\nSecond real line.")
        self.assertEqual(
            preview_module.preview_url(
                "http://x.example/shop",
                fetcher=lambda u: self._result(title="Shop", text=text)),
            "Shop\nReal content paragraph here. Second real line.")

    def test_url_normalized_for_cache(self):
        from forge.browser import preview as preview_module
        calls = []

        def fake(url):
            calls.append(url)
            return self._result()

        preview_module.preview_url("http://x.example/a/", fetcher=fake)
        preview_module.preview_url("http://X.EXAMPLE/a", fetcher=fake)
        preview_module.preview_url("http://x.example/a#frag", fetcher=fake)
        self.assertEqual(len(calls), 1)

    def test_dwell_ms(self):
        from forge.browser import preview as preview_module
        old_env = os.environ.get("FORGE_BROWSER_PREVIEW_DWELL_MS")
        old_settings = os.environ.get("FORGE_BROWSER_SETTINGS_PATH")
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "settings.json")
        try:
            os.environ["FORGE_BROWSER_SETTINGS_PATH"] = path
            os.environ.pop("FORGE_BROWSER_PREVIEW_DWELL_MS", None)
            self.assertEqual(preview_module.dwell_ms(), 600)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"preview_dwell_ms": "1200"}, handle)
            self.assertEqual(preview_module.dwell_ms(), 1200)
            os.environ["FORGE_BROWSER_PREVIEW_DWELL_MS"] = "250"
            self.assertEqual(preview_module.dwell_ms(), 250)
            os.environ["FORGE_BROWSER_PREVIEW_DWELL_MS"] = "nonsense"
            self.assertEqual(preview_module.dwell_ms(), 600)
            os.environ["FORGE_BROWSER_PREVIEW_DWELL_MS"] = "50"
            self.assertEqual(preview_module.dwell_ms(), 100)
            os.environ["FORGE_BROWSER_PREVIEW_DWELL_MS"] = "99999"
            self.assertEqual(preview_module.dwell_ms(), 5000)
        finally:
            if old_env is None:
                os.environ.pop("FORGE_BROWSER_PREVIEW_DWELL_MS", None)
            else:
                os.environ["FORGE_BROWSER_PREVIEW_DWELL_MS"] = old_env
            if old_settings is None:
                os.environ.pop("FORGE_BROWSER_SETTINGS_PATH", None)
            else:
                os.environ["FORGE_BROWSER_SETTINGS_PATH"] = old_settings


class BriefTests(unittest.TestCase):
    """#15: the morning brief, from the local memory store."""

    def _seed(self, tmp, now):
        import datetime
        import time as _time
        yesterday = datetime.date.fromtimestamp(now) - datetime.timedelta(days=1)
        yday_start = _time.mktime(yesterday.timetuple())
        today_start = _time.mktime(
            datetime.date.fromtimestamp(now).timetuple())
        page_path = os.path.join(tmp, "p.json")
        with open(page_path, "w") as handle:
            json.dump({"pages": [
                {"url": "http://gears.example/", "title": "Gear Guide",
                 "at": yday_start + 36000, "chunks": ["Gears turn wheels."]},
                {"url": "http://today.example/", "title": "Today Page",
                 "at": today_start + 36000, "chunks": ["Today stuff."]}]}, handle)
        history_path = os.path.join(tmp, "h.json")
        with open(history_path, "w") as handle:
            json.dump([
                {"url": "http://soup.example/", "title": "Soup",
                 "at": yday_start + 72000},
                {"url": "http://gears.example/", "title": "Gear Guide",
                 "at": yday_start + 1000}], handle)
        return page_path, history_path, yday_start

    def _env(self, tmp, page_path, history_path):
        from unittest import mock
        return mock.patch.dict(os.environ, {
            "HOME": tmp,
            "FORGE_BROWSER_PAGE_MEMORY_PATH": page_path,
            "FORGE_BROWSER_HISTORY_PATH": history_path})

    def test_yesterday_pages_filters_by_date(self):
        import time as _time
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                pages = brief_module.yesterday_pages(now=now)
        self.assertEqual([p["url"] for p in pages],
                         ["http://soup.example/", "http://gears.example/"])
        self.assertEqual(pages[1]["text"], "Gears turn wheels.")

    def test_build_brief_summarizes_and_caches(self):
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "You were reading about gears."
                first = brief_module.build_brief(session, now=now)
                self.assertEqual(first["summary"],
                                 "You were reading about gears.")
                self.assertEqual(len(first["pages"]), 2)
                session.ask_once.side_effect = Exception("brain down")
                second = brief_module.build_brief(session, now=now)
                self.assertEqual(second, first)
                self.assertEqual(session.ask_once.call_count, 1)

    def test_build_brief_no_brain_lists_pages(self):
        import time as _time
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                brief = brief_module.build_brief(None, now=now)
            self.assertIsNone(brief["summary"])
            self.assertEqual(len(brief["pages"]), 2)
            self.assertTrue(os.path.exists(
                os.path.join(tmp, ".forge", "brief-2026-11-10.json")))

    def test_build_brief_failed_brain_not_cached(self):
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                session = mock.Mock(model="fake:1b")
                session.ask_once.side_effect = Exception("brain down")
                first = brief_module.build_brief(session, now=now)
                self.assertIsNone(first["summary"])
                self.assertFalse(os.path.exists(
                    os.path.join(tmp, ".forge", "brief-2026-11-10.json")))
                second = brief_module.build_brief(session, now=now)
                self.assertEqual(session.ask_once.call_count, 2)

    def test_brief_text_empty_day(self):
        from forge.browser import brief as brief_module
        self.assertIn("Nothing to brief",
                      brief_module.brief_text({"date": "2026-11-10",
                                               "pages": [], "summary": None}))

    def test_chat_brief_action(self):
        import datetime
        import time as _time
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        at = _time.mktime(yesterday.timetuple()) + 36000
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "p.json")
            with open(page_path, "w") as handle:
                json.dump({"pages": [
                    {"url": "http://gears.example/", "title": "Gear Guide",
                     "at": at, "chunks": ["Gears turn wheels."]}]}, handle)
            history_path = os.path.join(tmp, "h.json")
            with open(history_path, "w") as handle:
                json.dump([], handle)
            with self._env(tmp, page_path, history_path):
                seen = []
                queue = ['ACTION {"type": "brief"}',
                         "You were reading about gears.",
                         "here's your brief"]
                session = chat.ChatSession(model="fake:1b")

                def fake_chat(messages, on_chunk=None):
                    seen.append(messages)
                    return queue.pop(0)

                session._chat = fake_chat
                answer, _opens = session.ask("give me my morning brief",
                                             on_plan=lambda plan, kind: True)
        self.assertEqual(answer, "here's your brief")
        # The brief's page list and summary reached the brain as the
        # ACTION result (chunk text itself is not in the brief text).
        result = [m["content"] for m in seen[2]
                  if m["content"].startswith("RESULT of brief")][0]
        self.assertIn("Gear Guide", result)
        self.assertIn("You were reading about gears.", result)

    def test_cached_summary_less_brief_rebuilds_when_brain_appears(self):
        # U1: a no-brain launch caches the plain list, but a later
        # brain fills the summary in — the day is not poisoned.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                first = brief_module.build_brief(None, now=now)
                self.assertIsNone(first["summary"])
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "You were reading about gears."
                second = brief_module.build_brief(session, now=now)
                self.assertEqual(second["summary"],
                                 "You were reading about gears.")
                self.assertEqual(session.ask_once.call_count, 1)
                third = brief_module.build_brief(session, now=now)
                self.assertEqual(third["summary"],
                                 "You were reading about gears.")
                self.assertEqual(session.ask_once.call_count, 1)

    def test_sensitive_pages_excluded_from_brief(self):
        # U2: a bank visit in history never reaches the brief —
        # not the list, and not the summary prompt.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, yday_start = self._seed(tmp, now)
            with open(history_path, "w") as handle:
                json.dump([
                    {"url": "http://soup.example/", "title": "Soup",
                     "at": yday_start + 72000},
                    {"url": "http://bank.example/", "title": "My Bank",
                     "at": yday_start + 73000}], handle)
            with self._env(tmp, page_path, history_path):
                urls = [p["url"] for p in
                        brief_module.yesterday_pages(now=now)]
                self.assertNotIn("http://bank.example/", urls)
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "Summary."
                brief_module.build_brief(session, now=now)
                prompt = session.ask_once.call_args[0][0]
                self.assertNotIn("bank.example", prompt)
                self.assertIn("soup.example", prompt)

    def test_summary_samples_across_the_day(self):
        # U3: the summary prompt covers the whole day — the
        # oldest page is in it, not just the 12 newest.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, yday_start = self._seed(tmp, now)
            pages = [{"url": f"http://p{i}.example/", "title": f"Page {i}",
                      "at": yday_start + i * 3600, "chunks": [f"Text {i}."]}
                     for i in range(20)]
            with open(page_path, "w") as handle:
                json.dump({"pages": pages}, handle)
            with open(history_path, "w") as handle:
                json.dump([], handle)
            with self._env(tmp, page_path, history_path):
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "Summary."
                brief_module.build_brief(session, now=now)
                prompt = session.ask_once.call_args[0][0]
        self.assertIn("Page 0", prompt)   # the oldest page
        self.assertIn("Page 19", prompt)  # the newest page
        self.assertEqual(prompt.count("http://p"), 12)

    def test_summary_gets_600_chars_per_page(self):
        # U4: each page contributes 600 chars, not 300.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, yday_start = self._seed(tmp, now)
            marker = "MARKER-NEAR-THE-END"
            text = "x" * 500 + marker
            with open(page_path, "w") as handle:
                json.dump({"pages": [
                    {"url": "http://gears.example/", "title": "Gear Guide",
                     "at": yday_start + 36000, "chunks": [text]}]}, handle)
            with open(history_path, "w") as handle:
                json.dump([], handle)
            with self._env(tmp, page_path, history_path):
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "Summary."
                brief_module.build_brief(session, now=now)
                prompt = session.ask_once.call_args[0][0]
        self.assertIn(marker, prompt)

    def test_today_pages_filters_to_today(self):
        # U6: today_pages covers today so far, not yesterday.
        import time as _time
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                pages = brief_module.today_pages(now=now)
        self.assertEqual([p["url"] for p in pages],
                         ["http://today.example/"])

    def test_build_today_brief_is_not_cached(self):
        # U6: today's brief builds fresh every time — today keeps
        # changing, so it is never cached.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            with self._env(tmp, page_path, history_path):
                session = mock.Mock(model="fake:1b")
                session.ask_once.return_value = "Today so far, gears."
                first = brief_module.build_today_brief(session, now=now)
                self.assertEqual(first["summary"], "Today so far, gears.")
                self.assertEqual(first["date"], "2026-11-11")
                self.assertFalse(os.path.exists(
                    os.path.join(tmp, ".forge", "brief-2026-11-11.json")))
                prompt = session.ask_once.call_args[0][0]
                self.assertIn("today so far", prompt)
                self.assertIn("Today Page", prompt)

    def test_cli_brief_today(self):
        # U6: `brief today` briefs today so far, not yesterday.
        import io
        import time as _time
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import brief as brief_module
        from forge.browser import cli as cli_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            page_path, history_path, _start = self._seed(tmp, now)
            seats_path = os.path.join(tmp, "seats.json")
            with open(seats_path, "w") as handle:
                json.dump({"ai_enabled": True,
                           "features": {"chat": True}}, handle)
            with self._env(tmp, page_path, history_path):
                with mock.patch.dict(os.environ, {
                        "FORGE_BROWSER_SEATS_PATH": seats_path}):
                    session = mock.Mock(model=None)
                    today_brief = {
                        "date": "2026-11-11",
                        "pages": [{"url": "http://today.example/",
                                   "title": "Today Page"}],
                        "summary": None}
                    with mock.patch.object(
                            brief_module, "build_today_brief",
                            return_value=today_brief) as built:
                        with mock.patch(
                                "forge.browser.chat.ChatSession",
                                return_value=session):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                code = cli_module._cmd_brief(
                                    mock.Mock(when="today"))
                    self.assertEqual(code, 0)
                    self.assertEqual(built.call_count, 1)
        self.assertIn("Today so far", out.getvalue())
        self.assertIn("Today Page", out.getvalue())

    def test_prune_briefs_keeps_30_days(self):
        # U8: brief files older than 30 days are deleted; recent
        # ones and unparseable names stay.
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        with tempfile.TemporaryDirectory() as tmp:
            forge_dir = os.path.join(tmp, ".forge")
            os.makedirs(forge_dir)
            for name in ("brief-2026-09-01.json",   # 71 days old
                         "brief-2026-10-13.json",   # 29 days old
                         "brief-2026-11-10.json",   # yesterday
                         "brief-not-a-date.json",
                         "memory.json"):
                with open(os.path.join(forge_dir, name), "w") as handle:
                    handle.write("{}")
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                removed = brief_module.prune_briefs(now=now)
            remaining = sorted(os.listdir(forge_dir))
        self.assertEqual(removed, 1)
        self.assertNotIn("brief-2026-09-01.json", remaining)
        self.assertIn("brief-2026-10-13.json", remaining)
        self.assertIn("brief-2026-11-10.json", remaining)
        self.assertIn("brief-not-a-date.json", remaining)

    def test_chat_brief_action_runs_in_background(self):
        # U9: with no cached brief and a callback, the ACTION
        # returns at once and the brief arrives via on_brief_done.
        import datetime
        import threading
        import time as _time
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        at = _time.mktime(yesterday.timetuple()) + 36000
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "p.json")
            with open(page_path, "w") as handle:
                json.dump({"pages": [
                    {"url": "http://gears.example/", "title": "Gear Guide",
                     "at": at, "chunks": ["Gears turn wheels."]}]}, handle)
            history_path = os.path.join(tmp, "h.json")
            with open(history_path, "w") as handle:
                json.dump([], handle)
            with self._env(tmp, page_path, history_path):
                done = []
                event = threading.Event()
                seen = []
                queue = ['ACTION {"type": "brief"}',
                         "Your brief is building."]
                session = chat.ChatSession(model="fake:1b")

                def fake_chat(messages, on_chunk=None):
                    seen.append(messages)
                    return queue.pop(0)

                session._chat = fake_chat
                answer, _opens = session.ask(
                    "give me my morning brief",
                    on_plan=lambda plan, kind: True,
                    on_brief_done=lambda text: (done.append(text),
                                                event.set()))
                # The wait stays inside the env block — the job
                # reads the seeded stores through it.
                self.assertTrue(event.wait(15),
                                "the background brief never arrived")
        self.assertEqual(answer, "Your brief is building.")
        result = [m["content"] for m in seen[1]
                  if m["content"].startswith("RESULT of brief")][0]
        self.assertIn("background", result)
        self.assertIn("Gear Guide", done[0])

    def test_chat_brief_action_second_ask_waits(self):
        # U9: a second brief ask while one builds is told it is
        # already building — no second job starts.
        import datetime
        import threading
        import time as _time
        from unittest import mock
        from forge.browser import brief as brief_module
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        at = _time.mktime(yesterday.timetuple()) + 36000
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "p.json")
            with open(page_path, "w") as handle:
                json.dump({"pages": [
                    {"url": "http://gears.example/", "title": "Gear Guide",
                     "at": at, "chunks": ["Gears turn wheels."]}]}, handle)
            history_path = os.path.join(tmp, "h.json")
            with open(history_path, "w") as handle:
                json.dump([], handle)
            with self._env(tmp, page_path, history_path):
                started = threading.Event()
                release = threading.Event()
                finished = threading.Event()
                calls = []

                def blocking_build(session, now=None, force=False):
                    calls.append(1)
                    started.set()
                    release.wait(10)
                    return {"date": "2026-11-10", "pages": [],
                            "summary": None}

                with mock.patch.object(brief_module, "build_brief",
                                       side_effect=blocking_build):
                    session = chat.ChatSession(model="fake:1b")
                    queue = ['ACTION {"type": "brief"}',
                             "building",
                             'ACTION {"type": "brief"}',
                             "already building"]
                    session._chat = (lambda messages, on_chunk=None:
                                     queue.pop(0))
                    first, _ = session.ask(
                        "give me my morning brief",
                        on_plan=lambda plan, kind: True,
                        on_brief_done=lambda text: finished.set())
                    self.assertEqual(first, "building")
                    self.assertTrue(started.wait(10),
                                    "the first job never started")
                    second, _ = session.ask(
                        "give me my morning brief",
                        on_plan=lambda plan, kind: True,
                        on_brief_done=lambda text: finished.set())
                    self.assertEqual(second, "already building")
                    self.assertEqual(len(calls), 1)
                    release.set()
                    self.assertTrue(finished.wait(10),
                                    "the job never finished")

    def test_day_bounds_dst(self):
        # U10: on a DST transition day the window is 23 (or 25)
        # hours, not a flat 86400.
        import time as _time
        from forge.browser import brief as brief_module
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        _time.tzset()
        try:
            # Spring forward 2026-03-08: yesterday of 2026-03-09
            # is the 23-hour day.
            now = _time.mktime((2026, 3, 9, 12, 0, 0, 0, 0, -1))
            start, end = brief_module._day_bounds(now)
            self.assertEqual(end - start, 23 * 3600)
            # Fall back 2026-11-01: yesterday of 2026-11-02 is
            # the 25-hour day.
            now = _time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
            start, end = brief_module._day_bounds(now)
            self.assertEqual(end - start, 25 * 3600)
            # An ordinary day is still 24 hours.
            now = _time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
            start, end = brief_module._day_bounds(now)
            self.assertEqual(end - start, 24 * 3600)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            _time.tzset()


class PlanGateTests(unittest.TestCase):
    """#10: the plan gate, the submit re-ask, and Stop."""

    def _session_with_queue(self, queue):
        session = chat.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        return session

    def test_describe_action_plain_words(self):
        self.assertEqual(
            chat.describe_action("search", {"query": "gears"}),
            'Search the web for "gears"')
        self.assertEqual(
            chat.describe_action("open", {"url": "http://x.example/"}),
            "Open http://x.example/ in a new tab")
        self.assertEqual(
            chat.describe_action("remember", {"text": "I like dark mode."}),
            'Remember: "I like dark mode."')
        self.assertEqual(chat.describe_action("mystery", {}),
                         "Run the mystery action")

    def test_plan_gate_approves_first_action(self):
        from unittest import mock
        plans = []
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}', "done"])
        with mock.patch.object(chat, "_run_search",
                               return_value="results") as run:
            answer, _opens = session.ask(
                "find gears",
                on_plan=lambda plan, kind: plans.append(plan) or True)
        run.assert_called_once_with("gears")
        self.assertEqual(plans, ['Search the web for "gears"'])
        self.assertEqual(answer, "done")

    def test_plan_gate_denies_first_action(self):
        # P4: a first-action denial feeds back to the model like a
        # submit denial — the turn continues, so it can propose
        # something else, instead of ending on the spot.
        from unittest import mock
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}', "done"])
        with mock.patch.object(chat, "_run_search",
                               return_value="results") as run:
            answer, _opens = session.ask("find gears", on_plan=lambda plan, kind: False)
        run.assert_not_called()
        self.assertEqual(answer, "done")

    def test_submit_action_reasks_inside_approved_turn(self):
        from unittest import mock
        plans = []
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(tmp, "m.json")
            try:
                session = self._session_with_queue(
                    ['ACTION {"type": "search", "query": "gears"}',
                     'ACTION {"type": "remember", "text": "I like dark mode."}',
                     "all set"])

                def gate(plan, kind):
                    plans.append(plan)
                    return len(plans) == 1  # approve the search, refuse the remember

                with mock.patch.object(chat, "_run_search",
                                       return_value="results"):
                    answer, _opens = session.ask("find gears, then remember it",
                                                 on_plan=gate)
                from forge.browser.memory import list_entries
                stored = list_entries()
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_MEMORY_PATH"] = old
        self.assertEqual(len(plans), 2)
        self.assertIn("changes what I remember", plans[1])
        self.assertEqual(stored, [])
        self.assertEqual(answer, "all set")

    def test_cancel_stops_between_steps(self):
        from unittest import mock
        calls = []
        queue = ['ACTION {"type": "search", "query": "gears"}', "never reached"]
        session = chat.ChatSession(model="fake:1b")

        def counting_chat(messages, on_chunk=None):
            calls.append(1)
            return queue.pop(0)

        session._chat = counting_chat

        def on_action(kind, action):
            if kind == "search":
                session.cancel()

        with mock.patch.object(chat, "_run_search", return_value="results"):
            answer, _opens = session.ask("find gears", on_action=on_action,
                                         on_plan=lambda plan, kind: True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(answer, "Stopped.")

    def test_tab_note_marks_agent_tabs(self):
        seen = {}

        def fake_chat(messages, on_chunk=None):
            seen["messages"] = messages
            return "ok"

        session = chat.ChatSession(model="fake:1b")
        session._chat = fake_chat
        session.ask("hi", tabs=[
            {"title": "Gear Guide", "url": "http://g.example/",
             "text": "gears", "current": True, "agent": True},
            {"title": "Soup", "url": "http://s.example/",
             "text": "soup", "current": False, "agent": False}])
        note = [m["content"] for m in seen["messages"]
                if m["content"].startswith("Open tabs:")][0]
        self.assertIn("🤖 Gear Guide", note)
        self.assertNotIn("🤖 Soup", note)


class VoiceTests(unittest.TestCase):
    """Push-to-talk recording against a fake arecord on PATH."""

    def _fake_bin(self, tmp, write_bytes=True):
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        script = os.path.join(bindir, "arecord")
        with open(script, "w") as handle:
            handle.write("#!/bin/sh\n"
                         "for last; do :; done\n"
                         + ('head -c 100 /dev/zero > "$last"\n' if write_bytes else "")
                         + "sleep 30\n")
        os.chmod(script, 0o755)
        return bindir

    def test_find_recorder_none_without_any(self):
        from unittest import mock
        from forge.browser.voice import find_recorder
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                self.assertIsNone(find_recorder())

    def test_find_recorder_finds_fake_arecord(self):
        from unittest import mock
        from forge.browser.voice import find_recorder
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._fake_bin(tmp)
            with mock.patch.dict(
                    os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
                self.assertEqual(find_recorder(), "arecord")

    def test_recorder_records_and_stops(self):
        import time
        from unittest import mock
        from forge.browser.voice import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._fake_bin(tmp)
            with mock.patch.dict(
                    os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
                recorder = Recorder()
                self.assertTrue(recorder.start())
                time.sleep(0.3)
                path = recorder.stop()
                self.assertIsNotNone(path)
                self.assertEqual(os.path.getsize(path), 100)
                os.unlink(path)

    def test_recorder_nothing_recorded_returns_none(self):
        import time
        from unittest import mock
        from forge.browser.voice import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._fake_bin(tmp, write_bytes=False)
            with mock.patch.dict(
                    os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
                recorder = Recorder()
                self.assertTrue(recorder.start())
                time.sleep(0.3)
                self.assertIsNone(recorder.stop())

    def test_transcribe_recording_delegates(self):
        from unittest import mock
        from forge.browser import voice as voice_module
        with mock.patch.object(voice_module, "transcribe_saved_audio",
                               return_value="hello") as patched:
            self.assertEqual(voice_module.transcribe_recording("/tmp/x.wav"),
                             "hello")
            patched.assert_called_once_with("/tmp/x.wav")

    def test_cli_voice_records_and_prints(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import voice as voice_module
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._fake_bin(tmp)
            with mock.patch.dict(
                    os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
                with mock.patch.object(
                        voice_module, "transcribe_recording_detailed",
                        return_value=("hello from the mic", None)):
                    args = mock.Mock(seconds=1)
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = cli_module._cmd_voice(args)
        self.assertEqual(code, 0)
        self.assertIn("hello from the mic", out.getvalue())

    def test_cli_voice_no_recorder(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                args = mock.Mock(seconds=1)
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli_module._cmd_voice(args)
        self.assertEqual(code, 1)
        self.assertIn("no microphone recorder", out.getvalue())

    def test_recorder_falls_back_when_first_dies_at_once(self):
        # S4: a recorder that exits the moment it starts is
        # skipped for the next one — the mic still works.
        import time
        from unittest import mock
        from forge.browser.voice import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            dead = os.path.join(bindir, "arecord")
            with open(dead, "w") as handle:
                handle.write("#!/bin/sh\nexit 1\n")
            os.chmod(dead, 0o755)
            alive = os.path.join(bindir, "parecord")
            with open(alive, "w") as handle:
                handle.write("#!/bin/sh\n"
                             "for last; do :; done\n"
                             "head -c 100 /dev/zero > \"$last\"\n"
                             "sleep 30\n")
            os.chmod(alive, 0o755)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "FORGE_BROWSER_RECORDER": ""}
            with mock.patch.dict(os.environ, env):
                recorder = Recorder()
                self.assertTrue(recorder.start())
                self.assertEqual(recorder.name, "parecord")
                time.sleep(0.3)
                path = recorder.stop()
                self.assertIsNotNone(path)
                os.unlink(path)

    def test_forced_recorder(self):
        # S11: FORGE_BROWSER_RECORDER pins one recorder — and an
        # uninstalled forced recorder means none, not a silent
        # fallback to another one.
        from unittest import mock
        from forge.browser.voice import available_recorders
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._fake_bin(tmp)
            base = bindir + os.pathsep + os.environ["PATH"]
            with mock.patch.dict(os.environ, {
                    "PATH": base, "FORGE_BROWSER_RECORDER": "arecord"}):
                self.assertEqual(available_recorders(), ["arecord"])
            with mock.patch.dict(os.environ, {
                    "PATH": base,
                    "FORGE_BROWSER_RECORDER": "no-such-recorder"}):
                self.assertEqual(available_recorders(), [])

    def test_mic_device_passed_to_recorder(self):
        # S11: FORGE_BROWSER_MIC_DEVICE reaches the recorder as
        # its own device flag; unset, ffmpeg records "default".
        from unittest import mock
        from forge.browser.voice import _command_for
        with mock.patch.dict(os.environ,
                             {"FORGE_BROWSER_MIC_DEVICE": "hw:1,0"}):
            self.assertIn("hw:1,0", _command_for("arecord", "/tmp/x.wav"))
            self.assertIn("-D", _command_for("arecord", "/tmp/x.wav"))
            self.assertIn("--device=hw:1,0",
                          _command_for("parecord", "/tmp/x.wav"))
            self.assertIn("hw:1,0", _command_for("ffmpeg", "/tmp/x.wav"))
        with mock.patch.dict(os.environ, {"FORGE_BROWSER_MIC_DEVICE": ""}):
            self.assertNotIn("-D", _command_for("arecord", "/tmp/x.wav"))
            self.assertIn("default", _command_for("ffmpeg", "/tmp/x.wav"))

    def test_cli_voice_clamps_seconds_to_cap(self):
        # S3: the CLI clamps a long record to the 2-minute cap.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from forge.browser import cli as cli_module
        from forge.browser import voice as voice_module
        slept = []
        fake_recorder = mock.Mock()
        fake_recorder.start.return_value = True
        fake_recorder.stop.return_value = "/tmp/x.wav"
        with mock.patch.object(voice_module, "find_recorder",
                               return_value="arecord"):
            with mock.patch.object(voice_module, "Recorder",
                                   return_value=fake_recorder):
                with mock.patch.object(
                        voice_module, "transcribe_recording_detailed",
                        return_value=("hi", None)):
                    with mock.patch("time.sleep",
                                    side_effect=slept.append):
                        args = mock.Mock(seconds=9999)
                        out = io.StringIO()
                        with redirect_stdout(out):
                            code = cli_module._cmd_voice(args)
        self.assertEqual(code, 0)
        self.assertEqual(slept, [120])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_SETTINGS_PATH")
        os.environ["FORGE_BROWSER_SETTINGS_PATH"] = os.path.join(
            self.tmp, "settings.json")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_SETTINGS_PATH", None)
        else:
            os.environ["FORGE_BROWSER_SETTINGS_PATH"] = self.old

    def test_defaults_when_no_file(self):
        from forge.browser.settings import load_settings
        values = load_settings()
        self.assertEqual(values["download_dir"], "")
        self.assertEqual(values["ollama_url"], "")
        self.assertEqual(values["searxng_url"], "")

    def test_save_and_load_round_trip(self):
        from forge.browser.settings import get_setting, save_settings
        save_settings({"download_dir": "/tmp/dl",
                       "ollama_url": "http://127.0.0.1:11434"})
        self.assertEqual(get_setting("download_dir"), "/tmp/dl")
        self.assertEqual(get_setting("ollama_url"), "http://127.0.0.1:11434")
        self.assertEqual(get_setting("searxng_url"), "")

    def test_bad_json_falls_back_to_defaults(self):
        from forge.browser.settings import load_settings
        with open(os.environ["FORGE_BROWSER_SETTINGS_PATH"], "w") as handle:
            handle.write("{not json")
        self.assertEqual(load_settings()["download_dir"], "")


class SessionRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_SESSION_PATH")
        os.environ["FORGE_BROWSER_SESSION_PATH"] = os.path.join(
            self.tmp, "session.json")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_SESSION_PATH", None)
        else:
            os.environ["FORGE_BROWSER_SESSION_PATH"] = self.old

    def test_save_and_load_round_trip(self):
        from forge.browser.session import load_session, save_session
        tabs = [{"url": "https://example.org/a", "title": "A"},
                {"url": "https://example.org/b", "title": "B"}]
        self.assertEqual(save_session(tabs, current=1), 2)
        saved = load_session()
        self.assertEqual(saved["current"], 1)
        self.assertEqual([t["url"] for t in saved["tabs"]],
                         ["https://example.org/a", "https://example.org/b"])

    def test_local_pages_are_not_saved(self):
        from forge.browser.session import load_session, save_session
        tabs = [{"url": "http://127.0.0.1:1234/", "title": "Start"},
                {"url": "https://example.org/", "title": "Real"}]
        self.assertEqual(save_session(tabs), 1)
        self.assertEqual([t["url"] for t in load_session()["tabs"]],
                         ["https://example.org/"])

    def test_no_tabs_means_no_session(self):
        from forge.browser.session import load_session, save_session
        save_session([{"url": "http://127.0.0.1:1234/", "title": "Start"}])
        self.assertIsNone(load_session())
        self.assertFalse(os.path.exists(
            os.environ["FORGE_BROWSER_SESSION_PATH"]))

    def test_current_index_is_clamped(self):
        from forge.browser.session import load_session, save_session
        save_session([{"url": "https://example.org/", "title": "A"}],
                     current=9)
        self.assertEqual(load_session()["current"], 0)


class BookmarksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_BOOKMARKS_PATH")
        os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = os.path.join(
            self.tmp, "bookmarks.json")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_BOOKMARKS_PATH", None)
        else:
            os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = self.old

    def test_add_list_remove(self):
        from forge.browser.bookmarks import (
            add_bookmark, is_bookmarked, list_bookmarks, remove_bookmark)
        self.assertEqual(list_bookmarks(), [])
        add_bookmark("https://example.org/", "Example")
        self.assertTrue(is_bookmarked("https://example.org/"))
        self.assertEqual(
            [(b["url"], b["title"]) for b in list_bookmarks()],
            [("https://example.org/", "Example")])
        self.assertTrue(remove_bookmark("https://example.org/"))
        self.assertFalse(is_bookmarked("https://example.org/"))
        self.assertFalse(remove_bookmark("https://example.org/"))

    def test_re_adding_updates_title_without_doubling(self):
        from forge.browser.bookmarks import add_bookmark, list_bookmarks
        add_bookmark("https://example.org/", "First")
        add_bookmark("https://example.org/", "Second")
        self.assertEqual(len(list_bookmarks()), 1)
        self.assertEqual(list_bookmarks()[0]["title"], "Second")

    def test_blank_url_adds_nothing(self):
        from forge.browser.bookmarks import add_bookmark, list_bookmarks
        self.assertIsNone(add_bookmark("  "))
        self.assertEqual(list_bookmarks(), [])


class VaultMatchTests(unittest.TestCase):
    def test_find_entry_for_url(self):
        from forge.browser.vault import Vault, find_entry_for_url
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vault.json")
            vault = Vault.create(path, "test-passphrase")
            vault.set("example.org", "josh", "secret1")
            vault.set("https://mail.example.com/login", "josh2", "secret2")
            site, entry = find_entry_for_url(
                vault, "https://example.org/inbox")
            self.assertEqual(site, "example.org")
            self.assertEqual(entry["username"], "josh")
            # A subdomain matches the bare domain entry.
            site, _entry = find_entry_for_url(
                vault, "https://www.example.org/")
            self.assertEqual(site, "example.org")
            # The longer (more specific) key wins.
            site, entry = find_entry_for_url(
                vault, "https://mail.example.com/")
            self.assertEqual(entry["username"], "josh2")
            self.assertIsNone(find_entry_for_url(
                vault, "https://other.org/"))
            self.assertIsNone(find_entry_for_url(vault, ""))


class KeepDownloadTests(unittest.TestCase):
    def test_keep_download_uniquifies(self):
        from forge.browser.files import keep_download
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "dl")
            paths = []
            for i in range(3):
                src = os.path.join(tmp, f"tmp{i}")
                with open(src, "w") as handle:
                    handle.write("data")
                paths.append(keep_download(src, "report.pdf",
                                           directory=target))
            self.assertEqual([os.path.basename(p) for p in paths],
                             ["report.pdf", "report-1.pdf", "report-2.pdf"])
            for path in paths:
                self.assertTrue(os.path.isfile(path))

    def test_download_dir_env_override(self):
        from forge.browser.files import download_dir
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_DOWNLOAD_DIR")
            os.environ["FORGE_BROWSER_DOWNLOAD_DIR"] = os.path.join(tmp, "d")
            try:
                self.assertEqual(download_dir(), os.path.join(tmp, "d"))
                self.assertTrue(os.path.isdir(os.path.join(tmp, "d")))
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_DOWNLOAD_DIR", None)
                else:
                    os.environ["FORGE_BROWSER_DOWNLOAD_DIR"] = old


class WindowGeometryTests(unittest.TestCase):
    def test_default_fits_1366x768(self):
        from forge.browser.app import default_window_size
        self.assertEqual(default_window_size(1366, 768), (1240, 720))

    def test_default_shrinks_for_small_screens(self):
        from forge.browser.app import default_window_size
        self.assertEqual(default_window_size(800, 600), (800, 600))
        self.assertEqual(default_window_size(1024, 600), (1024, 600))

    def test_default_caps_for_large_screens(self):
        from forge.browser.app import default_window_size
        self.assertEqual(default_window_size(1920, 1080), (1240, 720))


class StartPageQuickLinksTests(unittest.TestCase):
    def test_quick_links_show_bookmarks(self):
        from forge.browser.bookmarks import add_bookmark
        from forge.browser.webui import start_page
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_BOOKMARKS_PATH")
            os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = os.path.join(
                tmp, "bookmarks.json")
            try:
                add_bookmark("https://example.org/news", "Example News")
                html = start_page()
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_BOOKMARKS_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = old
            self.assertIn("Quick links", html)
            self.assertIn("https://example.org/news", html)
            self.assertIn("Example News", html)

    def test_no_quick_links_without_bookmarks(self):
        from forge.browser.webui import start_page
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_BOOKMARKS_PATH")
            os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = os.path.join(
                tmp, "bookmarks.json")
            try:
                html = start_page()
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_BOOKMARKS_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_BOOKMARKS_PATH"] = old
            self.assertNotIn("Quick links", html)


class OllamaUrlTests(unittest.TestCase):
    def _set_env(self, name, value):
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        return old

    def _restore_env(self, name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def test_env_beats_settings(self):
        from forge.browser.media import ollama_url
        from forge.browser.settings import set_setting
        with tempfile.TemporaryDirectory() as tmp:
            old_env = self._set_env("FORGE_OLLAMA_URL",
                                    "http://env.example:11434")
            old_path = self._set_env(
                "FORGE_BROWSER_SETTINGS_PATH",
                os.path.join(tmp, "settings.json"))
            try:
                set_setting("ollama_url", "http://settings.example:11434")
                self.assertEqual(ollama_url(), "http://env.example:11434")
            finally:
                self._restore_env("FORGE_OLLAMA_URL", old_env)
                self._restore_env("FORGE_BROWSER_SETTINGS_PATH", old_path)

    def test_settings_beats_default(self):
        from forge.browser.media import ollama_url
        from forge.browser.settings import set_setting
        with tempfile.TemporaryDirectory() as tmp:
            old_env = self._set_env("FORGE_OLLAMA_URL", None)
            old_path = self._set_env(
                "FORGE_BROWSER_SETTINGS_PATH",
                os.path.join(tmp, "settings.json"))
            try:
                set_setting("ollama_url", "http://settings.example:11434/")
                self.assertEqual(ollama_url(),
                                 "http://settings.example:11434")
            finally:
                self._restore_env("FORGE_OLLAMA_URL", old_env)
                self._restore_env("FORGE_BROWSER_SETTINGS_PATH", old_path)

    def test_default_when_nothing_set(self):
        from forge.browser.media import ollama_url
        with tempfile.TemporaryDirectory() as tmp:
            old_env = self._set_env("FORGE_OLLAMA_URL", None)
            old_path = self._set_env(
                "FORGE_BROWSER_SETTINGS_PATH",
                os.path.join(tmp, "settings.json"))
            try:
                self.assertEqual(ollama_url(), "http://127.0.0.1:11434")
            finally:
                self._restore_env("FORGE_OLLAMA_URL", old_env)
                self._restore_env("FORGE_BROWSER_SETTINGS_PATH", old_path)


class WhatsNewTests(unittest.TestCase):
    """F4: a "what changed" line on launch after an update."""

    def _changelog(self, tmp):
        path = os.path.join(tmp, "CHANGELOG.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Changelog\n\n"
                         "## 0.3 — 2026-11-12\n\n"
                         "Audit batch 2, built from the audits.\n\n"
                         "- F1: the research queue\n\n"
                         "## 0.2 — 2026-11-12\n\n"
                         "Audit batch 1.\n")
        return path

    def test_section_reading(self):
        from forge.browser.whatsnew import read_changelog_section
        with tempfile.TemporaryDirectory() as tmp:
            path = self._changelog(tmp)
            body = [ln for ln in read_changelog_section("0.3", path)
                    if ln.strip()]
            self.assertIn("Audit batch 2, built from the audits.", body)
            self.assertNotIn("Audit batch 1.", body)
            self.assertEqual(read_changelog_section("9.9", path), [])

    def test_whats_new_line(self):
        from forge.browser.whatsnew import whats_new_line
        with tempfile.TemporaryDirectory() as tmp:
            path = self._changelog(tmp)
            line = whats_new_line("0.3", path)
            self.assertTrue(line.startswith("What's new in 0.3: "))
            self.assertIn("Audit batch 2, built from the audits.", line)
            self.assertEqual(whats_new_line("9.9", path),
                             "Forge Browser updated to 9.9.")

    def test_first_launch_records_and_shows_nothing(self):
        from forge.browser import __version__
        from forge.browser.whatsnew import check_whats_new
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_VERSION_SEEN_PATH")
            os.environ["FORGE_BROWSER_VERSION_SEEN_PATH"] = os.path.join(
                tmp, "version-seen")
            try:
                self.assertIsNone(check_whats_new())
                with open(os.path.join(tmp, "version-seen")) as handle:
                    self.assertEqual(handle.read(), __version__)
                # Second launch, same version: still nothing.
                self.assertIsNone(check_whats_new())
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_VERSION_SEEN_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_VERSION_SEEN_PATH"] = old

    def test_update_shows_the_line_once(self):
        from forge.browser import __version__
        from forge.browser.whatsnew import check_whats_new
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_VERSION_SEEN_PATH")
            seen = os.path.join(tmp, "version-seen")
            os.environ["FORGE_BROWSER_VERSION_SEEN_PATH"] = seen
            try:
                with open(seen, "w") as handle:
                    handle.write("0.2")
                line = check_whats_new()
                self.assertIsNotNone(line)
                self.assertIn(__version__, line)
                # Now recorded — the next launch shows nothing.
                self.assertIsNone(check_whats_new())
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_VERSION_SEEN_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_VERSION_SEEN_PATH"] = old


class CardsAuditTests(unittest.TestCase):
    """Audit #9 follow-ups (O1–O12, batch 4)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["FORGE_BROWSER_CARDS_DIR"] = self.tmp
        self.addCleanup(
            lambda: os.environ.pop("FORGE_BROWSER_CARDS_DIR", None))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name + ".md"),
                  "w", encoding="utf-8") as handle:
            handle.write(text)

    def _session(self, reply="final answer", seen=None):
        session = chat.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            if seen is not None:
                seen["messages"] = messages
            return reply

        session._chat = fake_chat
        return session

    def test_broken_card_listed_with_error(self):
        from forge.browser.cards import list_cards
        self._write("busted", "# Busted\n\nprompt\n\n## Actions\nACTION {bad\n")
        cards = {c.name: c for c in list_cards()}
        self.assertIn("busted", cards)
        self.assertIn("bad action line", cards["busted"].error)

    def test_load_broken_card_names_the_break(self):
        from forge.browser.cards import load_card
        self._write("busted", "# Busted\n\nprompt\n\n## Actions\nACTION {bad\n")
        with self.assertRaises(BrowserError) as caught:
            load_card("busted")
        self.assertIn("broken", str(caught.exception))
        self.assertIn("bad action line", str(caught.exception))

    def test_cards_command_shows_broken(self):
        from forge.browser import cli as cli_module
        self._write("busted", "# Busted\n\nprompt\n\n## Actions\nACTION {bad\n")

        class Args:
            pass

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli_module._cmd_cards(Args()), 0)
        self.assertIn("busted", out.getvalue())
        self.assertIn("BROKEN", out.getvalue())

    def test_ask_lists_cards_for_the_brain(self):
        seen = {}
        session = self._session(seen=seen)
        session.ask("hello")
        systems = [m["content"] for m in seen["messages"]
                   if m["role"] == "system"]
        joined = "\n".join(systems)
        self.assertIn("Saved workflow cards", joined)
        self.assertIn("research-topic", joined)
        # O2: a broken card is not offered to the brain.
        self._write("busted", "# Busted\n\nprompt\n\n## Actions\nACTION {bad\n")
        session.ask("hello again")
        joined = "\n".join(m["content"] for m in seen["messages"]
                           if m["role"] == "system")
        self.assertNotIn("busted", joined)

    def test_describe_card_lists_steps(self):
        text = chat.describe_action("card", {"name": "research-topic"})
        self.assertTrue(text.startswith(
            'Run the "research-topic" workflow card:'))
        self.assertIn("1. Research", text)

    def test_describe_unknown_card_name_only(self):
        self.assertEqual(
            chat.describe_action("card", {"name": "no-such-card"}),
            'Run the "no-such-card" workflow card')

    def test_describe_self_referential_card_no_recursion(self):
        self._write("selfy", '# Self\n\nprompt\n\n## Actions\n'
                    'ACTION {"type": "card", "name": "selfy"}\n')
        text = chat.describe_action("card", {"name": "selfy"})
        # Outer line + one name-only nested line, no expansion.
        self.assertEqual(text.count("selfy"), 2)

    def test_run_card_stops_between_actions(self):
        from forge.browser.cards import run_card
        self._write("twice", "# Twice\n\nprompt\n\n## Actions\n"
                    'ACTION {"type": "search", "query": "one"}\n'
                    'ACTION {"type": "search", "query": "two"}\n')
        calls = []
        real_search = chat._run_search

        def fake_search(query):
            calls.append(query)
            return "result"

        chat._run_search = fake_search
        try:
            session = self._session()
            session._stop_requested = False

            def stop_after_first(kind, action):
                session._stop_requested = True

            answer, opens = run_card(session, "twice",
                                     on_action=stop_after_first)
        finally:
            chat._run_search = real_search
        self.assertEqual(answer, "Stopped.")
        self.assertEqual(calls, ["one"])

    def test_run_card_progress_reports_each_step(self):
        from forge.browser.cards import run_card
        self._write("twice", "# Twice\n\nprompt\n\n## Actions\n"
                    'ACTION {"type": "search", "query": "one"}\n'
                    'ACTION {"type": "search", "query": "two"}\n')
        real_search = chat._run_search
        chat._run_search = lambda query: "result"
        try:
            steps = []
            run_card(self._session(), "twice", on_progress=steps.append)
        finally:
            chat._run_search = real_search
        self.assertEqual(steps, ["Step 1 of 2: search",
                                 "Step 2 of 2: search"])

    def test_page_placeholder_uses_retrieval(self):
        from forge.browser import retrieve
        from forge.browser.cards import run_card
        self._write("needle", "# Needle\n\nAnswer from the page: {page}\n")
        # The needle sits past 6000 chars, where the old head-cut
        # never looked.
        page = ("filler word " * 600) + "needleword here" + (" tail" * 100)
        self.assertGreater(page.index("needleword"), 6000)
        real_embed = retrieve.embed_texts

        def fake_embed(texts, model=None):
            return [[1.0, 0.0] if "needleword" in t else [0.0, 1.0]
                    for t in texts]

        retrieve.embed_texts = fake_embed
        try:
            seen = {}
            run_card(self._session(seen=seen), "needle",
                     question="needleword", page_text=page)
            prompt = seen["messages"][-1]["content"]
            self.assertIn("needleword", prompt)
            # O5: with no question there is no retrieval query, so
            # the head is used and the needle is not in it.
            run_card(self._session(seen=seen), "needle",
                     question="", page_text=page)
            prompt = seen["messages"][-1]["content"]
            self.assertNotIn("needleword", prompt)
        finally:
            retrieve.embed_texts = real_embed

    def test_actions_header_variants(self):
        from forge.browser.cards import parse_card
        for header in ("## actions", "##  Actions", "##Actions"):
            card = parse_card("t", "# T\n\nprompt\n\n" + header + "\n"
                              'ACTION {"type": "read_page"}\n')
            self.assertEqual(len(card.actions), 1, header)

    def test_title_after_blank_lines(self):
        from forge.browser.cards import parse_card
        card = parse_card("t", "\n\n# My Title\n\nsome prompt")
        self.assertEqual(card.title, "My Title")
        # A "# " line after the prompt has started is prompt text.
        card = parse_card("t", "prompt text\n# Not the title")
        self.assertEqual(card.title, "t")

    def test_final_prompt_budget_trims_oldest(self):
        from forge.browser.cards import FINAL_PROMPT_CHARS, run_card
        actions = "\n".join(
            'ACTION {"type": "search", "query": "q%d"}' % i
            for i in range(1, 7))
        self._write("many", "# Many\n\nprompt\n\n## Actions\n" + actions + "\n")
        real_search = chat._run_search
        chat._run_search = lambda q: ("RESULT-" + q + " ") * 2000
        try:
            seen = {}
            run_card(self._session(seen=seen), "many")
            prompt = seen["messages"][-1]["content"]
        finally:
            chat._run_search = real_search
        self.assertLessEqual(len(prompt), FINAL_PROMPT_CHARS)
        self.assertNotIn("RESULT-q1", prompt)
        self.assertIn("RESULT-q2", prompt)
        self.assertIn("RESULT-q6", prompt)

    def test_run_card_returns_opens(self):
        from forge.browser.cards import run_card
        self._write("opener", "# Opener\n\nprompt\n\n## Actions\n"
                    'ACTION {"type": "open", "url": "https://example.com/x"}\n')
        real_open = chat._run_open
        chat._run_open = lambda url: "page text"
        try:
            _answer, opens = run_card(self._session(), "opener")
        finally:
            chat._run_open = real_open
        self.assertEqual(opens, ["https://example.com/x"])

    def test_card_check_command(self):
        from forge.browser import cli as cli_module
        self._write("busted", "# Busted\n\nprompt\n\n## Actions\nACTION {bad\n")

        class Args:
            def __init__(self, name, question):
                self.name = name
                self.question = question

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("check", "key-facts")), 0)
        self.assertIn("is fine", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("check", "busted")), 1)
        self.assertIn("broken", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("check", "no-such-card")), 1)
        self.assertIn("no card", out.getvalue())

    def test_card_new_command(self):
        from forge.browser import cli as cli_module

        class Args:
            def __init__(self, name, question):
                self.name = name
                self.question = question

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("new", "my-card")), 0)
        path = os.path.join(self.tmp, "my-card.md")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# my-card", text)
        self.assertIn("## Actions", text)
        self.assertIn("{question}", text)
        self.assertIn("{page}", text)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("new", "my-card")), 1)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli_module._cmd_card(Args("new", "a/b")), 1)

    def test_load_card_reads_list_once(self):
        import forge.browser.cards as cards_module
        real = cards_module.list_cards
        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return real()

        cards_module.list_cards = counting
        try:
            with self.assertRaises(BrowserError):
                cards_module.load_card("no-such-card")
        finally:
            cards_module.list_cards = real
        self.assertEqual(calls["n"], 1)


class PlanGateAuditTests(unittest.TestCase):
    """Audit #10 follow-ups (P1–P8, batch 4)."""

    def _session_with_queue(self, queue):
        session = chat.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            return queue.pop(0)

        session._chat = fake_chat
        return session

    def _seats_env(self, tmp):
        old = os.environ.get("FORGE_BROWSER_SEATS_PATH")
        os.environ["FORGE_BROWSER_SEATS_PATH"] = os.path.join(
            tmp, "seats.json")
        self.addCleanup(
            lambda: (os.environ.pop("FORGE_BROWSER_SEATS_PATH", None)
                     if old is None
                     else os.environ.__setitem__(
                         "FORGE_BROWSER_SEATS_PATH", old)))

    def test_research_gated_every_time(self):
        from unittest import mock
        from forge.browser import research as research_module
        plans = []
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}',
             'ACTION {"type": "research", "topic": "gears"}',
             "done"])

        def gate(plan, kind):
            plans.append((plan, kind))
            return kind == "search"  # approve the search, refuse the research

        with mock.patch.object(chat, "_run_search", return_value="results"), \
                mock.patch.object(research_module, "start_research_job") as start:
            answer, _opens = session.ask("find gears, then research them",
                                         on_plan=gate)
        start.assert_not_called()
        self.assertEqual([kind for _plan, kind in plans],
                         ["search", "research"])
        self.assertEqual(answer, "done")

    def test_card_gated_every_time(self):
        from unittest import mock
        import forge.browser.cards as cards_module
        plans = []
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}',
             'ACTION {"type": "card", "name": "key-facts"}',
             "done"])

        def gate(plan, kind):
            plans.append(kind)
            return kind == "search"

        with mock.patch.object(chat, "_run_search", return_value="results"), \
                mock.patch.object(cards_module, "run_card") as run:
            answer, _opens = session.ask("find gears, then run the card",
                                         on_plan=gate)
        run.assert_not_called()
        self.assertEqual(plans, ["search", "card"])
        self.assertEqual(answer, "done")

    def test_second_cheap_action_not_gated(self):
        from unittest import mock
        plans = []
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}',
             'ACTION {"type": "search", "query": "wheels"}',
             "done"])
        with mock.patch.object(chat, "_run_search",
                               return_value="results") as run:
            answer, _opens = session.ask(
                "find gears then wheels",
                on_plan=lambda plan, kind: plans.append(plan) or True)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(plans), 1)
        self.assertEqual(answer, "done")

    def test_stopped_generation_runs_no_action(self):
        from unittest import mock
        session = chat.ChatSession(model="fake:1b")

        def stopping_chat(messages, on_chunk=None):
            session._stop_requested = True
            return 'ACTION {"type": "search", "query": "gears"}'

        session._chat = stopping_chat
        with mock.patch.object(chat, "_run_search",
                               return_value="results") as run:
            answer, _opens = session.ask("find gears",
                                         on_plan=lambda plan, kind: True)
        run.assert_not_called()
        self.assertEqual(answer, "Stopped.")

    def test_streaming_read_stops_on_cancel(self):
        import socket
        from unittest import mock
        session = chat.ChatSession(model="fake:1b")

        class FakeResponse:
            def __init__(self):
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def readline(self):
                self.calls += 1
                if self.calls == 1:
                    return b'{"message": {"content": "hello "}}\n'
                # The person hits Stop mid-generation; the next
                # read stalls, then the loop sees the flag.
                session._stop_requested = True
                raise socket.timeout("slow")

        fake = FakeResponse()
        chunks = []
        with mock.patch("urllib.request.urlopen", return_value=fake):
            text = session._chat([{"role": "user", "content": "hi"}],
                                 on_chunk=chunks.append)
        self.assertEqual(text, "hello ")
        self.assertEqual(chunks, ["hello "])
        self.assertEqual(fake.calls, 2)

    def test_first_action_denial_feeds_back_to_model(self):
        seen = []
        session = chat.ChatSession(model="fake:1b")
        queue = ['ACTION {"type": "search", "query": "gears"}', "done"]

        def fake_chat(messages, on_chunk=None):
            seen.append([dict(m) for m in messages])
            return queue.pop(0)

        session._chat = fake_chat
        answer, _opens = session.ask(
            "find gears", on_plan=lambda plan, kind: False)
        self.assertEqual(answer, "done")
        contents = [m["content"] for m in seen[1]]
        self.assertIn("RESULT of search:\nThe person said no to that.",
                      contents)

    def test_always_allow_skips_gate_for_cheap_kind(self):
        from unittest import mock
        from forge.browser.seats import set_always_allow
        with tempfile.TemporaryDirectory() as tmp:
            self._seats_env(tmp)
            set_always_allow("search", True)
            plans = []
            session = self._session_with_queue(
                ['ACTION {"type": "search", "query": "gears"}', "done"])
            with mock.patch.object(chat, "_run_search",
                                   return_value="results") as run:
                answer, _opens = session.ask(
                    "find gears",
                    on_plan=lambda plan, kind: plans.append(plan) or True)
            run.assert_called_once_with("gears")
            self.assertEqual(plans, [])
            self.assertEqual(answer, "done")

    def test_always_allow_never_for_submit_or_expensive(self):
        from forge.browser.seats import always_allowed, set_always_allow
        with tempfile.TemporaryDirectory() as tmp:
            self._seats_env(tmp)
            set_always_allow("remember", True)
            set_always_allow("research", True)
            set_always_allow("card", True)
            self.assertFalse(always_allowed("remember"))
            self.assertFalse(always_allowed("research"))
            self.assertFalse(always_allowed("card"))
            # And a cheap kind can be allowed and cleared again.
            set_always_allow("search", True)
            self.assertTrue(always_allowed("search"))
            set_always_allow("search", False)
            self.assertFalse(always_allowed("search"))

    def test_always_allow_still_gates_expensive(self):
        from unittest import mock
        from forge.browser import research as research_module
        from forge.browser.seats import set_always_allow
        with tempfile.TemporaryDirectory() as tmp:
            self._seats_env(tmp)
            set_always_allow("search", True)
            plans = []
            session = self._session_with_queue(
                ['ACTION {"type": "search", "query": "gears"}',
                 'ACTION {"type": "research", "topic": "gears"}',
                 "done"])
            with mock.patch.object(chat, "_run_search",
                                   return_value="results"), \
                    mock.patch.object(research_module,
                                      "start_research_job"), \
                    mock.patch.object(research_module,
                                      "research_job_running",
                                      return_value=None):
                answer, _opens = session.ask(
                    "find gears, then research them",
                    on_plan=lambda plan, kind: plans.append(kind) or True)
            # The always-allowed search asked nothing; the research
            # still asked.
            self.assertEqual(plans, ["research"])
            self.assertEqual(answer, "done")

    def test_compare_describe_copy(self):
        self.assertEqual(
            chat.describe_action("compare", {}),
            "Compare the open tabs on price and key specs")
        self.assertEqual(
            chat.describe_action("compare",
                                 {"tabs": [1, 2], "fields": ["price"]}),
            "Compare tabs 1, 2 on price")


class ResearchAuditTests(unittest.TestCase):
    """Audit #11 follow-ups (Q1–Q10, batch 4)."""

    def _reset_jobs(self):
        from forge.browser import research as research_module
        research_module._CURRENT_JOB["topic"] = None
        research_module._CURRENT_JOB["cancel"] = None
        research_module._JOB_QUEUE.clear()

    def _research_env(self, tmp):
        old = os.environ.get("FORGE_BROWSER_RESEARCH_DIR")
        os.environ["FORGE_BROWSER_RESEARCH_DIR"] = tmp
        self.addCleanup(
            lambda: (os.environ.pop("FORGE_BROWSER_RESEARCH_DIR", None)
                     if old is None
                     else os.environ.__setitem__(
                         "FORGE_BROWSER_RESEARCH_DIR", old)))

    def test_cancel_before_run_delivers_partial_report(self):
        import threading as _threading
        from forge.browser.research import make_plan, run_research
        searches = []
        cancel = _threading.Event()
        cancel.set()
        report, sources = run_research(
            make_plan("gears"), approved=True, cancel=cancel,
            search_fn=lambda q, max_results=6: searches.append(q) or [],
            fetch_fn=lambda url: None)
        self.assertIn("Stopped early", report)
        self.assertEqual(sources, [])
        self.assertEqual(searches, [])

    def test_cancel_running_job_delivers_partial_report(self):
        import threading as _threading
        from forge.browser import research as research_module
        self._reset_jobs()
        release = _threading.Event()
        finished = _threading.Event()
        delivered = {}

        def search_fn(query, max_results=6):
            release.wait(timeout=10)
            return []

        def on_done(report, sources, error=None):
            delivered["report"] = report
            delivered["error"] = error
            finished.set()

        try:
            research_module.start_research_job(
                "gears", search_fn=search_fn, on_done=on_done)
            self.assertTrue(research_module.cancel_research_job())
            release.set()
            self.assertTrue(finished.wait(timeout=10))
            self.assertIn("Stopped early", delivered["report"])
            self.assertIsNone(delivered["error"])
            self.assertIsNone(research_module.research_job_running())
            self.assertFalse(research_module.cancel_research_job())
        finally:
            release.set()
            self._reset_jobs()

    def test_cancel_leaves_queued_job_alone(self):
        import threading as _threading
        from forge.browser import research as research_module
        self._reset_jobs()
        release = _threading.Event()
        finished = _threading.Event()
        delivered = {}

        def search_fn(query, max_results=6):
            if query == "first":
                release.wait(timeout=10)
            return []

        def on_done(report, sources, error=None):
            delivered[report.splitlines()[0]] = report
            if len(delivered) == 2:
                finished.set()

        try:
            research_module.start_research_job(
                "first", search_fn=search_fn, on_done=on_done)
            research_module.start_research_job(
                "second", search_fn=search_fn, on_done=on_done)
            self.assertTrue(research_module.cancel_research_job())
            release.set()
            self.assertTrue(finished.wait(timeout=10))
            self.assertIn("Stopped early", delivered["# Research: first"])
            self.assertNotIn("Stopped early", delivered["# Research: second"])
        finally:
            release.set()
            self._reset_jobs()

    def test_report_links_bare_urls(self):
        from forge.browser.research import report_to_html
        report = ("# Research: gears\n\n## What the sources say\n\n"
                  "### [1] Gear Guide\n\n"
                  "Source: http://example.com/x (html)\n\n"
                  "## Sources\n\n[1] http://example.com/x\n")
        page = report_to_html(report)
        self.assertIn('<a href="http://example.com/x">http://example.com/x</a>',
                      page)

    def test_markdown_link_not_double_linked(self):
        from forge.browser.research import report_to_html
        report = ("# Research: gears\n\n"
                  "See [the guide](http://g.example/).\n")
        page = report_to_html(report)
        self.assertEqual(page.count('<a href="http://g.example/">'), 1)
        self.assertIn('<a href="http://g.example/">the guide</a>', page)

    def test_save_and_list_reports(self):
        from forge.browser.research import list_reports, save_report
        with tempfile.TemporaryDirectory() as tmp:
            self._research_env(tmp)
            first = save_report("Gears", "# Research: Gears\n\ntext one")
            second = save_report("Gears", "# Research: Gears\n\ntext two")
            self.assertNotEqual(first, second)
            save_report("Bikes", "# Research: Bikes\n\ntext three")
            reports = list_reports()
            self.assertEqual(len(reports), 3)
            self.assertEqual(reports[0]["topic"], "Bikes")
            self.assertEqual({r["topic"] for r in reports},
                             {"Gears", "Bikes"})

    def test_job_saves_its_report(self):
        import threading as _threading
        from forge.browser import research as research_module
        from forge.browser.research import list_reports
        self._reset_jobs()
        finished = _threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            self._research_env(tmp)
            try:
                research_module.start_research_job(
                    "gears",
                    search_fn=lambda q, max_results=6: [],
                    on_done=lambda r, s, e=None: finished.set())
                self.assertTrue(finished.wait(timeout=10))
            finally:
                self._reset_jobs()
            reports = list_reports()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["topic"], "gears")

    def test_report_page_uses_shared_dark_style(self):
        from forge.browser.compare import compare_table_html
        from forge.browser.research import report_to_html
        from forge.browser.webui import PAGE_STYLE
        page = report_to_html("# Research: gears\n\ntext\n")
        self.assertIn(PAGE_STYLE, page)
        self.assertIn("background:#101216", page)
        self.assertIn("color:#ffb347", page)
        self.assertNotIn("color:#222", page)
        self.assertNotIn("#1a5fb4", page)
        compare = compare_table_html(
            [{"title": "A", "url": "http://a.test/",
              "fields": {"price": "$1"}}], ["price"])
        self.assertIn(PAGE_STYLE, compare)

    def test_progress_announces_fetch_and_elapsed(self):
        import re as _re
        from forge.browser.research import make_plan, run_research
        from forge.browser.search import SearchResult

        class Found:
            kind = "html"
            title = "Src"
            text = "The source says the thing."
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/a", snippet=""),
                    SearchResult(title="B", url="http://x.test/b", snippet="")]

        progress = []
        _report, sources = run_research(
            make_plan("gears"), approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found(), on_progress=progress.append,
            excerpt_fn=lambda q, t, m: t[:m])
        self.assertEqual(len(sources), 2)
        fetching = [i for i, line in enumerate(progress)
                    if line.startswith("Fetching http://x.test/a")]
        reading = [i for i, line in enumerate(progress)
                   if line.startswith("Reading 1 of")]
        self.assertEqual(len(fetching), 1)
        self.assertEqual(len(reading), 1)
        self.assertLess(fetching[0], reading[0])
        for line in progress:
            self.assertTrue(_re.search(r"\(\d+s\)$", line), line)

    def test_chat_research_action_depth_values(self):
        from unittest import mock
        from forge.browser import research as research_module
        captured = []

        def fake_start(topic, on_progress=None, on_done=None, **kwargs):
            captured.append(kwargs)
            return topic

        for action, pages, chars in (
                ('{"type": "research", "topic": "gears", '
                 '"max_pages": 2, "detail": "brief"}', 2, 2000),
                ('{"type": "research", "topic": "gears", '
                 '"max_pages": 99, "detail": "full"}', 20, 8000),
                ('{"type": "research", "topic": "gears", '
                 '"detail": "bogus"}', 6, 4000),
                ('{"type": "research", "topic": "gears", '
                 '"max_pages": 0}', 1, 4000)):
            session = chat.ChatSession(model="fake:1b")
            queue = [f"ACTION {action}", "done"]
            session._chat = lambda messages, on_chunk=None: queue.pop(0)
            with mock.patch.object(research_module, "start_research_job",
                                   fake_start), \
                    mock.patch.object(research_module,
                                      "research_job_running",
                                      return_value=None):
                session.ask("research gears",
                            on_plan=lambda plan, kind: True)
        self.assertEqual([(c["max_pages"], c["max_chars_per_source"])
                          for c in captured],
                         [(2, 2000), (20, 8000), (6, 4000), (1, 4000)])

    def test_job_honors_max_pages(self):
        import threading as _threading
        from forge.browser import research as research_module
        from forge.browser.search import SearchResult
        self._reset_jobs()
        finished = _threading.Event()
        delivered = {}

        class Found:
            kind = "html"
            title = "Src"
            text = "body"
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/a", snippet=""),
                    SearchResult(title="B", url="http://x.test/b", snippet="")]

        def on_done(report, sources, error=None):
            delivered["sources"] = sources
            finished.set()

        try:
            research_module.start_research_job(
                "gears", search_fn=fake_search,
                fetch_fn=lambda url: Found(), max_pages=1,
                on_done=on_done)
            self.assertTrue(finished.wait(timeout=10))
        finally:
            self._reset_jobs()
        self.assertEqual(len(delivered["sources"]), 1)

    def test_dedup_normalizes_urls(self):
        from forge.browser.research import make_plan, run_research
        from forge.browser.search import SearchResult

        class Found:
            kind = "html"
            title = "Src"
            text = "body"
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/page",
                                 snippet=""),
                    SearchResult(title="B", url="https://x.test/page/",
                                 snippet=""),
                    SearchResult(title="C",
                                 url="http://x.test/page?utm_source=news",
                                 snippet="")]

        fetched = []
        _report, sources = run_research(
            make_plan("gears"), approved=True, search_fn=fake_search,
            fetch_fn=lambda url: fetched.append(url) or Found(),
            excerpt_fn=lambda q, t, m: t[:m])
        self.assertEqual(len(sources), 1)
        self.assertEqual(fetched, ["http://x.test/page"])

    def test_excerpt_fn_overrides_head_cut(self):
        from forge.browser.research import make_plan, run_research
        from forge.browser.search import SearchResult

        class Found:
            kind = "html"
            title = "Src"
            text = "filler " * 2000
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/a",
                                 snippet="")]

        _report, sources = run_research(
            make_plan("gears"), approved=True, search_fn=fake_search,
            fetch_fn=lambda url: Found(),
            excerpt_fn=lambda q, t, m: "RELEVANT")
        self.assertEqual(sources[0]["text"], "RELEVANT")

    def test_retrieval_excerpt_finds_relevant_part(self):
        from forge.browser import retrieve
        from forge.browser.research import make_plan, run_research
        from forge.browser.search import SearchResult

        class Found:
            kind = "html"
            title = "Src"
            # The needle sits past 6000 chars, where the old
            # head-cut never looked.
            text = ("filler word " * 600) + "needleword here"
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/a",
                                 snippet="")]

        real_embed = retrieve.embed_texts

        def fake_embed(texts, model=None):
            return [[1.0, 0.0] if "needleword" in t else [0.0, 1.0]
                    for t in texts]

        retrieve.embed_texts = fake_embed
        try:
            _report, sources = run_research(
                make_plan("needleword"), approved=True,
                search_fn=fake_search, fetch_fn=lambda url: Found())
        finally:
            retrieve.embed_texts = real_embed
        self.assertIn("needleword", sources[0]["text"])

    def test_retrieval_excerpt_falls_back_to_head(self):
        from forge.browser import retrieve
        from forge.browser.errors import BrowserError
        from forge.browser.research import make_plan, run_research
        from forge.browser.search import SearchResult

        class Found:
            kind = "html"
            title = "Src"
            text = "head text " * 1000
            barriers = []

        def fake_search(query, max_results=6):
            return [SearchResult(title="A", url="http://x.test/a",
                                 snippet="")]

        real_embed = retrieve.embed_texts

        def failing_embed(texts, model=None):
            raise BrowserError("no embed model")

        retrieve.embed_texts = failing_embed
        try:
            _report, sources = run_research(
                make_plan("gears"), approved=True, search_fn=fake_search,
                fetch_fn=lambda url: Found())
        finally:
            retrieve.embed_texts = real_embed
        self.assertTrue(sources[0]["text"].startswith("head text"))
        self.assertLessEqual(len(sources[0]["text"]), 4000)


if __name__ == "__main__":
    unittest.main()
