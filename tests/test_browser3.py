"""Tests for the AI browser's chat brain (Item: chat interface).

The brain and the engine hands are stubbed — these tests pin the
protocol: ACTION parsing, the action loop and its cap, results
fed back to the model, tab-open requests, and the no-brain error.
"""

import json
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


class RetrieveTests(unittest.TestCase):
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
        from forge.browser import retrieve as retrieve_module
        session = chat.ChatSession(model="fake:1b")
        original = retrieve_module.embed_texts

        def fake_embed(texts, model=None):
            raise BrowserError("down")

        retrieve_module.embed_texts = fake_embed
        try:
            text = "x" * 7000
            self.assertEqual(
                retrieve_module.page_context(session, "q", text), text[:6000])
        finally:
            retrieve_module.embed_texts = original


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
        answer = run_card(session, "key-facts", page_text="Gears cost $42.")
        self.assertEqual(answer, "facts answer")

    def test_run_card_summarize(self):
        from forge.browser.cards import run_card
        session = self.make_session()
        answer = run_card(session, "summarize-page", page_text="Gears turn.")
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
            answer = run_card(session, "research-topic",
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
        self.assertEqual(
            tidy_title(session,
                       "The Best Gears In The Whole Wide World Today | Shop",
                       "http://x.example"),
            "The Best Gears In The")

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

        def fake_embed(texts):
            return [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]

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

    def test_search_pages_empty_without_embeddings(self):
        from forge.browser import memory as memory_module
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = os.path.join(tmp, "p.json")
            try:
                memory_module.remember_page(
                    "http://gears.example/", "Gears", "Gears turn wheels. " * 40)

                def boom(texts, model=None):
                    raise BrowserError("down")

                self.assertEqual(memory_module.search_pages("gears", embed_fn=boom), [])
            finally:
                if old is None:
                    os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
                else:
                    os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = old


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
        self.assertTrue(any("only 1 tabs" in t for t in system_texts))
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
        self.assertIn("Group:", tab_note)
        self.assertIn("1. Gears", tab_note)
        self.assertIn("2. Wheels", tab_note)
        self.assertIn("3. Soup", tab_note)
        self.assertEqual(answer, "grouped answer")

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
        original = chat.pick_chat_model
        chat.pick_chat_model = lambda models=None: None
        try:
            session = chat.ChatSession()
        finally:
            chat.pick_chat_model = original
        with self.assertRaises(BrowserError):
            session.ask("hi")


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
                                             on_plan=lambda plan: True)
        self.assertEqual(answer, "here's your brief")
        # The brief's page list and summary reached the brain as the
        # ACTION result (chunk text itself is not in the brief text).
        result = [m["content"] for m in seen[2]
                  if m["content"].startswith("RESULT of brief")][0]
        self.assertIn("Gear Guide", result)
        self.assertIn("You were reading about gears.", result)


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
                on_plan=lambda plan: plans.append(plan) or True)
        run.assert_called_once_with("gears")
        self.assertEqual(plans, ['Search the web for "gears"'])
        self.assertEqual(answer, "done")

    def test_plan_gate_denies_first_action(self):
        from unittest import mock
        session = self._session_with_queue(
            ['ACTION {"type": "search", "query": "gears"}', "done"])
        with mock.patch.object(chat, "_run_search",
                               return_value="results") as run:
            answer, _opens = session.ask("find gears", on_plan=lambda plan: False)
        run.assert_not_called()
        self.assertEqual(answer, "Okay — I didn't do that.")

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

                def gate(plan):
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
                                         on_plan=lambda plan: True)
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
                with mock.patch.object(voice_module, "transcribe_recording",
                                       return_value="hello from the mic"):
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


if __name__ == "__main__":
    unittest.main()
