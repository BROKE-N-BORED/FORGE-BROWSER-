"""Offline tests for the v0.10 BRAIN review items.

Covers the fixes builder #4 implemented (and the three the finisher
corrected) across the brain modules: vault atomicity (A1), the
transcription template (A3), the RAM-measured brain guide (B5), the
memory-write failure surfacing (B15), the CLI/card/memory fixes
(G-C*), the performance work (G-P*), the sensitive-page and store
hardening (G-S*), and the describe error path (G-T9).

Everything runs without the internet and without Ollama: stores live
in throwaway temp dirs (via the FORGE_BROWSER_*_PATH overrides and a
patched HOME), Ollama answers are faked, and slow paths (the vault's
key derivation, the streaming stall) are kept to a few calls.
"""

import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import BrowserError  # noqa: E402
from forge.browser import brief as brief_module  # noqa: E402
from forge.browser import cards as cards_module  # noqa: E402
from forge.browser import chat as chat_module  # noqa: E402
from forge.browser import cli as cli_module  # noqa: E402
from forge.browser import media as media_module  # noqa: E402
from forge.browser import memory as memory_module  # noqa: E402
from forge.browser import research as research_module  # noqa: E402
from forge.browser import retrieve as retrieve_module  # noqa: E402
from forge.browser import seats as seats_module  # noqa: E402
from forge.browser import session as session_module  # noqa: E402
from forge.browser import settings as settings_module  # noqa: E402
from forge.browser import titles as titles_module  # noqa: E402
from forge.browser import vault as vault_module  # noqa: E402
from forge.browser import voice as voice_module  # noqa: E402

GB = 1024 ** 3


def _mode(path):
    """The permission bits of a file (0o600, 0o700, ...)."""
    return stat.S_IMODE(os.stat(path).st_mode)


def _rewrite_same_key(path, content):
    """Rewrite a file keeping its size and mtime — the cache key
    (G-P18/G-P23) must not notice the change."""
    old = os.stat(path)
    assert len(content.encode("utf-8")) == old.st_size, \
        "test bug: replacement must be the same byte length"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))


def _bump_mtime(path):
    """Move a file's mtime a second forward, so the mtime caches
    (G-P18/G-P23) must re-read it."""
    old = os.stat(path)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 10 ** 9))


class VaultAtomicTests(unittest.TestCase):
    """A1: the vault save is atomic, keeps one .bak, and a truncated
    vault is a plain-words error, not a traceback."""

    PASSPHRASE = "correct horse battery staple"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "vault.json")
        # The key derivation (PBKDF2, 600k rounds) is pinned by the
        # existing vault tests; these tests pin file behaviour, so
        # the derivation is stubbed to keep them fast.
        self._kdf = mock.patch.object(
            vault_module, "_derive_keys",
            return_value=(b"e" * 32, b"m" * 32))
        self._kdf.start()

    def tearDown(self):
        self._kdf.stop()

    def test_save_keeps_previous_as_bak(self):
        vault = vault_module.Vault.create(self.path, self.PASSPHRASE)
        # First save: no previous vault, so no .bak yet.
        self.assertFalse(os.path.exists(self.path + ".bak"))
        # set() saves: after each set, the .bak is the vault as it
        # was before that set.
        vault.set("example.com", "user", "pw1")
        vault.set("other.com", "user2", "pw2")
        backup = vault_module.Vault.open(self.path + ".bak",
                                         self.PASSPHRASE)
        self.assertIsNotNone(backup.get("example.com"))
        self.assertIsNone(backup.get("other.com"))
        self.assertIsNotNone(vault.get("example.com"))
        self.assertIsNotNone(vault.get("other.com"))

    def test_truncated_vault_is_plain_error(self):
        vault = vault_module.Vault.create(self.path, self.PASSPHRASE)
        vault.set("example.com", "user", "pw1")  # set() saves, making .bak
        with open(self.path, "r", encoding="utf-8") as handle:
            blob = handle.read()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(blob[: len(blob) // 2])  # cut off mid-write
        with self.assertRaises(BrowserError) as caught:
            vault_module.Vault.open(self.path, self.PASSPHRASE)
        self.assertIn("damaged", str(caught.exception))
        self.assertIn(".bak", str(caught.exception))

    def test_save_leaves_no_tmp_litter(self):
        vault = vault_module.Vault.create(self.path, self.PASSPHRASE)
        vault.set("example.com", "user", "pw1")  # set() saves
        leftovers = [name for name in os.listdir(self.tmp)
                     if name.endswith(".tmp") or name.endswith(".bak.tmp")]
        self.assertEqual(leftovers, [])


class TranscribeTemplateTests(unittest.TestCase):
    """A3: the transcription command template runs as argv, never
    through a shell — a filename with spaces works, and one with
    shell metacharacters cannot execute commands."""

    def test_filename_with_spaces_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "my file.wav")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hello transcript")
            env = {"FORGE_TRANSCRIBE_CMD": "cp {file} {out_dir}/out.txt"}
            with mock.patch.dict(os.environ, env):
                text, reason = media_module.transcribe_saved_audio_detailed(
                    path)
        self.assertEqual(text, "hello transcript")
        self.assertIsNone(reason)

    def test_filename_metacharacters_do_not_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "pwned")
            # Under shell=True this would run `touch <marker>`.
            evil = "nothing; touch " + marker
            env = {"FORGE_TRANSCRIBE_CMD": "echo {file}"}
            with mock.patch.dict(os.environ, env):
                text, reason = media_module.transcribe_saved_audio_detailed(
                    evil)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual((text, reason), ("", "failed"))


class BrainGuideTests(unittest.TestCase):
    """B5: the brain size guide is measured from real RAM, not a
    hardcoded 4.5 GB — and the live pick re-checks under load."""

    def test_guide_scales_with_ram(self):
        self.assertEqual(chat_module.brain_size_guide(8 * GB),
                         int(4.5 * GB))
        self.assertEqual(chat_module.brain_size_guide(16 * GB),
                         9 * GB)
        # A small machine still gets a usable 1 GB floor.
        self.assertEqual(chat_module.brain_size_guide(1 * GB), 1 * GB)

    def test_total_ram_matches_meminfo(self):
        if not os.path.exists("/proc/meminfo"):
            self.skipTest("no /proc/meminfo on this machine")
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    expected = int(line.split()[1]) * 1024
        self.assertEqual(chat_module.total_ram_bytes(), expected)
        self.assertGreater(chat_module.free_ram_bytes(), 0)

    def _clean_pick_env(self, tmp):
        return mock.patch.dict(os.environ, {
            "FORGE_BROWSER_CHAT_MODEL": "",
            "FORGE_BROWSER_SEATS_PATH": os.path.join(tmp, "seats.json")})

    def test_live_pick_drops_guide_under_load(self):
        # Under 1 GB free, the live pick drops the guide to 1 GB.
        with tempfile.TemporaryDirectory() as tmp:
            with self._clean_pick_env(tmp):
                with mock.patch.object(
                        chat_module, "check_ollama",
                        return_value=(True, [{"name": "big:8b",
                                              "size": 5 * GB}])):
                    with mock.patch.object(
                            chat_module, "free_ram_bytes",
                            return_value=512 * 1024 ** 2):
                        _model, _reason, note = (
                            chat_module.pick_chat_model_with_reason())
        self.assertIn("1 GB size guide", note)

    def test_live_pick_remeasures_the_guide(self):
        # The live pick uses the freshly measured guide, not the
        # import-time one.
        with tempfile.TemporaryDirectory() as tmp:
            with self._clean_pick_env(tmp):
                with mock.patch.object(
                        chat_module, "check_ollama",
                        return_value=(True, [{"name": "big:8b",
                                              "size": 5 * GB}])):
                    with mock.patch.object(
                            chat_module, "brain_size_guide",
                            return_value=2 * GB):
                        with mock.patch.object(
                                chat_module, "free_ram_bytes",
                                return_value=8 * GB):
                            _m, _r, note = (
                                chat_module.pick_chat_model_with_reason())
        self.assertIn("2 GB size guide", note)

    def test_explicit_pick_keeps_startup_guide(self):
        # An explicit models/sizes call stays a pure function of its
        # arguments: it uses the startup guide, not a re-measure.
        with tempfile.TemporaryDirectory() as tmp:
            with self._clean_pick_env(tmp):
                with mock.patch.object(chat_module, "MAX_BRAIN_BYTES",
                                       2 * GB):
                    _m, _r, note = (
                        chat_module.pick_chat_model_with_reason(
                            models=["big:8b"], sizes={"big:8b": 5 * GB}))
        self.assertIn("2 GB size guide", note)


class MemoryWriteFailureTests(unittest.TestCase):
    """B15 (stores half): a failed memory write is reported, not
    swallowed — and the caps are public constants the windows that
    show the data can disclose."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
        os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(
            self.tmp, "memory.json")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = self.old

    def test_remember_raises_when_save_fails(self):
        with mock.patch.object(
                memory_module, "_write_json_atomic",
                side_effect=OSError("disk full")):
            with self.assertRaises(BrowserError) as caught:
                memory_module.remember("a fact that cannot be saved")
        self.assertIn("could not save", str(caught.exception))

    def test_forget_raises_when_save_fails(self):
        memory_module.remember("a fact to forget later")
        with mock.patch.object(
                memory_module, "_write_json_atomic",
                side_effect=OSError("disk full")):
            with self.assertRaises(BrowserError):
                memory_module.forget("forget later")

    def test_cap_constants_are_public(self):
        self.assertEqual(memory_module.MEMORY_LIMIT, 200)
        self.assertEqual(memory_module.EMBED_CACHE_LIMIT, 1500)
        self.assertEqual(memory_module.PAGE_MEMORY_LIMIT, 100)
        self.assertEqual(memory_module.PAGE_CHUNKS_LIMIT, 24)
        self.assertEqual(titles_module.HISTORY_LIMIT, 200)
        self.assertEqual(session_module.MAX_TABS, 50)


class CliMemoryAddTests(unittest.TestCase):
    """G-C1: `memory add` no longer crashes with AttributeError —
    the vault add/remove site check is scoped to the vault."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
        os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(
            self.tmp, "memory.json")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_MEMORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_MEMORY_PATH"] = self.old

    def test_memory_add_does_not_crash(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_module.main(["memory", "add", "hello there"])
        self.assertEqual(code, 0)
        self.assertIn("remembered: hello there", out.getvalue())
        self.assertEqual(len(memory_module.list_entries()), 1)

    def test_vault_add_without_site_still_refused(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_module.main(["vault", "add"])
        self.assertEqual(code, 2)
        self.assertIn("vault add/remove need a SITE", err.getvalue())


class _StreamResp:
    """A canned streaming urlopen result."""

    def __init__(self, readline_fn):
        self._readline = readline_fn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def readline(self):
        return self._readline()


class StreamStallTests(unittest.TestCase):
    """G-C3: a stalled streaming chat ends after 240 s of no tokens
    instead of hanging the turn forever. The stall is patched short
    for the test; the production value is untouched."""

    def _chat_with(self, readline_fn, stall=0.05):
        session = chat_module.ChatSession(model="fake:1b")
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return _StreamResp(readline_fn)

        with mock.patch.object(chat_module, "STREAM_STALL_SECONDS", stall):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                return session._chat([{"role": "user", "content": "hi"}],
                                     on_chunk=lambda _c: None)

    def test_timeouts_raise_after_stall(self):
        def always_timeout():
            raise socket.timeout("timed out")

        with self.assertRaises(BrowserError) as caught:
            self._chat_with(always_timeout)
        self.assertIn("stopped answering", str(caught.exception))
        self.assertIn("no new words", str(caught.exception))

    def test_blank_lines_do_not_hold_stream_open(self):
        # A server dribbling blank lines is stalled too: only a line
        # with content resets the clock.
        seen = {"n": 0}

        def blanks_forever():
            seen["n"] += 1
            if seen["n"] > 100000:  # safety net, never reached
                return b""
            return b"\n"

        with self.assertRaises(BrowserError) as caught:
            self._chat_with(blanks_forever)
        self.assertIn("stopped answering", str(caught.exception))

    def test_tokens_keep_stream_alive(self):
        # Tokens arriving inside the stall window — even when the
        # whole reply takes longer than the stall — never trip it.
        lines = [b'{"message": {"content": "word "}}\n'] * 20
        lines.append(b"")

        def slow_tokens():
            time.sleep(0.005)
            return lines.pop(0) if lines else b""

        result = self._chat_with(slow_tokens, stall=0.05)
        self.assertEqual(result, "word " * 20)


class TidyWorkerTests(unittest.TestCase):
    """G-C13: one exception in the tidy callback no longer kills the
    tidy worker, and a dead worker is restarted on the next
    request."""

    def _wait_for(self, seen, want, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if want in seen:
                return True
            time.sleep(0.01)
        return want in seen

    def test_callback_exception_does_not_kill_worker(self):
        seen = []

        def on_done(view, url, tidy):
            if url == "https://bad.example/":
                raise RuntimeError("boom in the callback")
            seen.append(url)

        worker = titles_module.TidyWorker(
            lambda url, raw: "tidy:" + raw, on_done, debounce=0)
        worker.request(object(), "https://bad.example/", "Bad")
        worker.request(object(), "https://good.example/", "Good")
        self.assertTrue(self._wait_for(seen, "https://good.example/"))
        self.assertEqual(seen, ["https://good.example/"])
        self.assertTrue(worker._thread.is_alive())

    def test_dead_worker_is_restarted(self):
        seen = []

        def on_done(view, url, tidy):
            seen.append(url)

        worker = titles_module.TidyWorker(
            lambda url, raw: "tidy:" + raw, on_done, debounce=0)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.assertFalse(dead.is_alive())
        worker._thread = dead
        worker.request(object(), "https://example.org/", "Example")
        self.assertIsNot(worker._thread, dead)
        self.assertTrue(worker._thread.is_alive())
        self.assertTrue(self._wait_for(seen, "https://example.org/"))


class NestedCardTests(unittest.TestCase):
    """G-C14: the plan promises nested card steps, and the runner
    now runs them — with a depth cap, so a self-referential card
    stops instead of recursing forever."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_CARDS_DIR")
        os.environ["FORGE_BROWSER_CARDS_DIR"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_CARDS_DIR", None)
        else:
            os.environ["FORGE_BROWSER_CARDS_DIR"] = self.old

    def _write_card(self, name, prompt, actions):
        lines = [f"# {name}", prompt, "## Actions"]
        lines.extend(f"ACTION {json.dumps(a)}" for a in actions)
        with open(os.path.join(self.tmp, name + ".md"),
                  "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def _session(self, prompts):
        session = chat_module.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            content = messages[-1]["content"]
            prompts.append(content)
            if "Inner prompt" in content:
                return "INNER-ANSWER"
            if "Outer prompt" in content:
                return "OUTER-ANSWER"
            if "Loop prompt" in content:
                return "LOOP-ANSWER"
            return "OTHER"

        session._chat = fake_chat
        return session

    def test_nested_card_runs(self):
        self._write_card("inner", "Inner prompt.",
                         [{"type": "read_page"}])
        self._write_card("outer", "Outer prompt.",
                         [{"type": "card", "name": "inner"}])
        prompts = []
        answer, opens = cards_module.run_card(
            self._session(prompts), "outer")
        self.assertEqual(answer, "OUTER-ANSWER")
        self.assertEqual(opens, [])
        # The inner card's answer reached the outer card's prompt
        # as the nested step's result.
        outer = [p for p in prompts if "Outer prompt" in p]
        self.assertEqual(len(outer), 1)
        self.assertIn("INNER-ANSWER", outer[0])

    def test_self_referential_card_stops_at_depth_cap(self):
        self._write_card("loop", "Loop prompt.",
                         [{"type": "card", "name": "loop"}])
        prompts = []
        answer, _opens = cards_module.run_card(
            self._session(prompts), "loop")
        self.assertEqual(answer, "LOOP-ANSWER")
        # One brain call per depth level (0..MAX_CARD_DEPTH), no
        # RecursionError — and the deepest level says it stopped.
        self.assertEqual(len(prompts), cards_module.MAX_CARD_DEPTH + 1)
        self.assertTrue(any("nests too deep" in p for p in prompts))


class RecorderStopTests(unittest.TestCase):
    """G-C16: Recorder.stop() returns None when the recording file
    is already gone, instead of raising on getsize."""

    def _recorder_with_fake_proc(self, path):
        recorder = voice_module.Recorder()
        proc = mock.Mock()
        proc.terminate.return_value = None
        proc.wait.return_value = None
        recorder._proc = proc
        recorder.path = path
        recorder.started = time.monotonic()
        return recorder

    def test_stop_with_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "gone.wav")
            recorder = self._recorder_with_fake_proc(missing)
            self.assertIsNone(recorder.stop())

    def test_stop_with_no_path_returns_none(self):
        recorder = self._recorder_with_fake_proc(None)
        self.assertIsNone(recorder.stop())


class StoreLockingTests(unittest.TestCase):
    """G-C17: the memory stores are locked like the seats file, so
    concurrent load-modify-save from several threads loses no
    entries."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_mem = os.environ.get("FORGE_BROWSER_MEMORY_PATH")
        self.old_sites = os.environ.get("FORGE_BROWSER_MEMORY_SITES_PATH")
        os.environ["FORGE_BROWSER_MEMORY_PATH"] = os.path.join(
            self.tmp, "memory.json")
        os.environ["FORGE_BROWSER_MEMORY_SITES_PATH"] = os.path.join(
            self.tmp, "sites.json")

    def tearDown(self):
        for key, old in (("FORGE_BROWSER_MEMORY_PATH", self.old_mem),
                         ("FORGE_BROWSER_MEMORY_SITES_PATH", self.old_sites)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_concurrent_remember_loses_nothing(self):
        barrier = threading.Barrier(4)

        def worker(n):
            barrier.wait()
            for i in range(5):
                memory_module.remember(f"fact {n}-{i} from worker {n}")

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(memory_module.list_entries()), 20)

    def test_concurrent_site_blocks_lose_nothing(self):
        barrier = threading.Barrier(4)

        def worker(n):
            barrier.wait()
            memory_module.block_site_memory(f"site{n}.example")

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sites = memory_module.list_site_memory()
        self.assertEqual(sorted(sites["blocked"]),
                         ["site0.example", "site1.example",
                          "site2.example", "site3.example"])


class EmbedCacheModelTests(unittest.TestCase):
    """G-C18: the embedding cache is keyed by model — stale vectors
    from a replaced embeddings model are never reused, and a legacy
    flat cache (no model) is dropped, not trusted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "embed.json")

    def test_vectors_keyed_by_model(self):
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

        cache = memory_module.EmbedCache(self.path)
        cache.vectors_for(["hello"], fake_embed, model="model-a")
        self.assertEqual(len(calls), 1)
        cache.vectors_for(["hello"], fake_embed, model="model-a")
        self.assertEqual(len(calls), 1)  # same model: a cache hit
        cache.vectors_for(["hello"], fake_embed, model="model-b")
        self.assertEqual(len(calls), 2)  # another model: a miss

    def test_legacy_flat_cache_is_dropped(self):
        import hashlib
        key = hashlib.sha256(b"hello").hexdigest()
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({key: [9.9, 9.9]}, handle)

        def fake_embed(texts):
            return [[1.0, 0.0] for _ in texts]

        cache = memory_module.EmbedCache(self.path)
        vectors = cache.vectors_for(["hello"], fake_embed,
                                    model="model-a")
        self.assertEqual(vectors, [[1.0, 0.0]])  # re-embedded, not 9.9

    def test_on_disk_file_has_model_sections(self):
        def fake_embed(texts):
            return [[1.0, 0.0] for _ in texts]

        cache = memory_module.EmbedCache(self.path)
        cache.vectors_for(["hello"], fake_embed, model="model-a")
        cache._save()
        with open(self.path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertIn("model-a", stored)
        self.assertEqual(list(stored["model-a"].values()), [[1.0, 0.0]])


class CliVoiceClampTests(unittest.TestCase):
    """G-C23: `voice` with negative seconds no longer reaches
    time.sleep(-5) and crashes — seconds clamp to 0 (and to the
    max)."""

    def _run_voice(self, seconds):
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = os.path.join(tmp, "fake.wav")

            class FakeRecorder:
                def start(self):
                    return True

                def stop(self):
                    return fake_path

            env = {"FORGE_BROWSER_SEATS_PATH": os.path.join(tmp, "s.json")}
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(voice_module, "find_recorder",
                                       return_value="arecord"):
                    with mock.patch.object(voice_module, "Recorder",
                                           FakeRecorder):
                        with mock.patch.object(
                                voice_module,
                                "transcribe_recording_detailed",
                                return_value=("hello", None)):
                            with mock.patch("time.sleep",
                                           side_effect=sleeps.append):
                                out = io.StringIO()
                                with redirect_stdout(out):
                                    code = cli_module._cmd_voice(
                                        SimpleNamespace(seconds=seconds))
        return code, sleeps, out.getvalue()

    def test_negative_seconds_clamped_to_zero(self):
        code, sleeps, out = self._run_voice(-5)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [0])
        self.assertIn("hello", out)

    def test_huge_seconds_clamped_to_max(self):
        code, sleeps, _out = self._run_voice(10 ** 9)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [voice_module.MAX_RECORD_SECONDS])


class CliMemoryBlankTests(unittest.TestCase):
    """G-C24: `memory add` with whitespace-only text says the input
    was blank instead of crashing on entry['text']."""


    def test_whitespace_add_reports_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_MEMORY_PATH": os.path.join(tmp, "m.json")}
            with mock.patch.dict(os.environ, env):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli_module._cmd_memory(
                        SimpleNamespace(action="add", value="   "))
        self.assertEqual(code, 1)
        self.assertIn("nothing to remember", out.getvalue())


class CliFileErrorTests(unittest.TestCase):
    """G-C25: `research --out` and `history export` file errors are
    plain BrowserErrors, not tracebacks — main() only catches
    BrowserError."""

    def test_research_out_error_is_browser_error(self):
        args = SimpleNamespace(topic="gears", queries="", max_pages=6,
                               yes=True,
                               out=os.path.join("/no-such-dir-xyz",
                                                "report.md"))
        with mock.patch.object(research_module, "run_research",
                               return_value=("REPORT", [])):
            with self.assertRaises(BrowserError) as caught:
                cli_module._cmd_research(args)
        self.assertIn("could not write the report", str(caught.exception))

    def test_history_export_error_is_browser_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_HISTORY_PATH":
                   os.path.join(tmp, "history.json")}
            args = SimpleNamespace(
                action="export",
                value=os.path.join("/no-such-dir-xyz", "h.json"))
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(BrowserError) as caught:
                    cli_module._cmd_history(args)
        self.assertIn("could not write the history export",
                      str(caught.exception))


class CardCheckNewTests(unittest.TestCase):
    """G-C26: cards named "check" or "new" can be run now — check
    and new are flags, and the legacy `card check <name>` form still
    validates when a name follows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_CARDS_DIR")
        os.environ["FORGE_BROWSER_CARDS_DIR"] = self.tmp
        self._write_card("demo", "Demo prompt.",
                         [{"type": "read_page"}])
        self._write_card("check", "Check prompt.",
                         [{"type": "read_page"}])
        self._write_card("new", "New prompt.",
                         [{"type": "read_page"}])

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_CARDS_DIR", None)
        else:
            os.environ["FORGE_BROWSER_CARDS_DIR"] = self.old

    def _write_card(self, name, prompt, actions):
        lines = [f"# {name}", prompt, "## Actions"]
        lines.extend(f"ACTION {json.dumps(a)}" for a in actions)
        with open(os.path.join(self.tmp, name + ".md"),
                  "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def _run_cmd(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_module._cmd_card(args)
        return code, out.getvalue()

    def test_check_flag_validates(self):
        args = SimpleNamespace(name="demo", question="",
                               check=True, new=False)
        code, out = self._run_cmd(args)
        self.assertEqual(code, 0)
        self.assertIn("is fine", out)

    def test_new_flag_scaffolds(self):
        args = SimpleNamespace(name="fresh-card", question="",
                               check=False, new=True)
        code, out = self._run_cmd(args)
        self.assertEqual(code, 0)
        self.assertIn("New card:", out)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "fresh-card.md")))

    def test_legacy_check_with_name_still_validates(self):
        args = SimpleNamespace(name="check", question="demo",
                               check=False, new=False)
        with mock.patch.object(chat_module, "ChatSession"):
            code, out = self._run_cmd(args)
        self.assertEqual(code, 0)
        self.assertIn("is fine", out)

    def test_card_named_check_runs(self):
        ran = []

        def fake_run(session, name, question="", **kwargs):
            ran.append(name)
            return ("ran it", [])

        args = SimpleNamespace(name="check", question="",
                               check=False, new=False)
        with mock.patch.object(chat_module, "ChatSession",
                               return_value=mock.Mock()):
            with mock.patch.object(cards_module, "run_card",
                                   side_effect=fake_run):
                code, _out = self._run_cmd(args)
        self.assertEqual(code, 0)
        self.assertEqual(ran, ["check"])

    def test_card_named_new_runs(self):
        ran = []

        def fake_run(session, name, question="", **kwargs):
            ran.append(name)
            return ("ran it", [])

        args = SimpleNamespace(name="new", question="",
                               check=False, new=False)
        with mock.patch.object(chat_module, "ChatSession",
                               return_value=mock.Mock()):
            with mock.patch.object(cards_module, "run_card",
                                   side_effect=fake_run):
                code, _out = self._run_cmd(args)
        self.assertEqual(code, 0)
        self.assertEqual(ran, ["new"])


class MakePlanClampTests(unittest.TestCase):
    """G-C27: make_plan clamps max_pages to 1-20, as the chat action
    does — a negative value no longer yields an empty report."""

    def test_max_pages_clamped(self):
        plan = research_module.make_plan("gears", max_pages=-3)
        self.assertEqual(plan["max_pages"], 1)
        self.assertIn("Read up to 1 sources", plan["steps"][-1])
        self.assertEqual(
            research_module.make_plan("gears", max_pages=99)["max_pages"],
            20)
        self.assertEqual(
            research_module.make_plan("gears", max_pages=0)["max_pages"],
            1)
        self.assertEqual(
            research_module.make_plan("gears", max_pages=None)["max_pages"],
            6)
        self.assertEqual(
            research_module.make_plan("gears",
                                      max_pages="abc")["max_pages"], 6)
        self.assertEqual(
            research_module.make_plan("gears", max_pages=5)["max_pages"],
            5)


class SmallestModelTests(unittest.TestCase):
    """G-C30: the "smallest model" fallback. The sweep's premise
    does not hold as written: the fallback branch (chat.py:291)
    runs only when every model is over the guide, and the oversize
    filter (chat.py:288) requires a known size — so an unknown-size
    model can never reach the min() key the finding faults. What
    the code really does, pinned here: with all known sizes over
    the guide it takes the smallest known size, and an unknown-size
    model is a candidate (it is not provably oversize)."""

    def _pick(self, models, sizes):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_CHAT_MODEL": "",
                   "FORGE_BROWSER_SEATS_PATH":
                       os.path.join(tmp, "seats.json")}
            with mock.patch.dict(os.environ, env):
                return chat_module.pick_chat_model_with_reason(
                    models=models, sizes=sizes)

    def test_smallest_known_size_picked_when_all_over_guide(self):
        model, _reason, note = self._pick(
            ["huge:70b", "big:8b"],
            {"huge:70b": 10 ** 12, "big:8b": 10 ** 11})
        self.assertEqual(model, "big:8b")
        self.assertIn("using the smallest (big:8b)", note)

    def test_unknown_size_model_is_not_filtered_as_oversize(self):
        # "mystery" has no known size, so the oversize filter cannot
        # exclude it — it is the only candidate.
        model, _reason, _note = self._pick(
            ["big:8b", "mystery"], {"big:8b": 10 ** 11})
        self.assertEqual(model, "mystery")


class BriefAtomicTests(unittest.TestCase):
    """G-C31: the brief cache write is tmp + replace — a crash
    mid-write leaves the old brief, never a half-written one."""

    NOW = time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))

    def test_failed_replace_leaves_old_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                path = brief_module.brief_path(
                    brief_module.yesterday_date(self.NOW))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                old = {"date": "2026-11-10", "pages": [],
                       "summary": "the old brief"}
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(old, handle)
                with mock.patch("os.replace",
                                side_effect=OSError("disk full")):
                    brief_module.build_brief(session=None, now=self.NOW,
                                             force=True)
                with open(path, encoding="utf-8") as handle:
                    kept = json.load(handle)
        # The old direct open(path, "w") would have truncated this.
        self.assertEqual(kept, old)

    def test_successful_save_is_valid_json_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                brief = brief_module.build_brief(session=None,
                                                 now=self.NOW, force=True)
                path = brief_module.brief_path(
                    brief_module.yesterday_date(self.NOW))
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)
                leftovers = [name for name in os.listdir(
                    os.path.dirname(path)) if name.endswith(".tmp")]
        self.assertEqual(stored, brief)
        self.assertEqual(leftovers, [])


class SessionRestoreTests(unittest.TestCase):
    """G-C32: a restored `current` index is clamped against the
    sliced tab list, so it can never exceed the returned tabs."""

    def test_current_clamped_to_sliced_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.json")
            tabs = [{"url": f"https://example.org/{i}",
                     "title": f"Tab {i}"} for i in range(60)]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tabs": tabs, "current": 55}, handle)
            env = {"FORGE_BROWSER_SESSION_PATH": path}
            with mock.patch.dict(os.environ, env):
                restored = session_module.load_session()
        self.assertEqual(len(restored["tabs"]), session_module.MAX_TABS)
        self.assertEqual(restored["current"], 49)
        self.assertLess(restored["current"], len(restored["tabs"]))


class CosineDedupTests(unittest.TestCase):
    """G-C33: _cosine lives in retrieve.py alone now — memory.py and
    titles.py import the one copy."""

    def test_one_cosine_shared(self):
        self.assertIs(memory_module._cosine, retrieve_module._cosine)
        self.assertIs(titles_module._cosine, retrieve_module._cosine)

    def test_cosine_values(self):
        cosine = retrieve_module._cosine
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)
        self.assertEqual(cosine([0.0, 0.0], [1.0, 0.0]), 0.0)


class SaveSeatsTests(unittest.TestCase):
    """G-C34: save_seats is gone — it had no callers and its
    replace-the-whole-dict write would discard concurrent edits.
    The locked merge path (set_seat) still works."""

    def test_save_seats_is_gone(self):
        self.assertFalse(hasattr(seats_module, "save_seats"))

    def test_set_seat_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_SEATS_PATH": os.path.join(tmp, "s.json")}
            with mock.patch.dict(os.environ, env):
                seats_module.set_seat("brain", "model-a")
                seats_module.set_seat("fast", "model-b")
                seats = seats_module.load_seats()
        self.assertEqual(seats["brain"], "model-a")
        self.assertEqual(seats["fast"], "model-b")


class CliPagesOnceTests(unittest.TestCase):
    """G-C35: `memory pages` reads the page list once — the empty
    check no longer re-reads and re-parses the file."""

    def test_pages_list_loaded_once(self):
        calls = []

        def counting():
            calls.append(1)
            return []

        args = SimpleNamespace(action="pages", value="")
        with mock.patch.object(memory_module, "list_pages",
                               side_effect=counting):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_module._cmd_memory(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("no pages remembered yet", out.getvalue())


class ChatHistoryTrimTests(unittest.TestCase):
    """G-P7: chat history is trimmed to HISTORY_CAP after every
    turn instead of growing for the session's life."""

    def test_history_trimmed_to_cap(self):
        session = chat_module.ChatSession(model="fake:1b")

        def fake_chat(messages, on_chunk=None):
            return "a plain reply, no action"

        session._chat = fake_chat
        for turn in range(10):
            session.ask(f"question {turn}")
        self.assertEqual(len(session.history), chat_module.HISTORY_CAP)
        # The oldest kept message is from turn 4: ten turns of two
        # messages, trimmed to the last twelve.
        self.assertEqual(session.history[0]["content"], "question 4")
        self.assertEqual(session.history[-1]["content"],
                         "a plain reply, no action")


class VisionPickCacheTests(unittest.TestCase):
    """G-P11: the vision-seat pick is cached briefly instead of
    re-querying Ollama (/api/tags plus an /api/show per model) on
    every describe and status check."""

    def setUp(self):
        media_module.clear_vision_pick_cache()
        self.tmp = tempfile.mkdtemp()
        self.env = {"FORGE_BROWSER_SEATS_PATH": os.path.join(
                        self.tmp, "seats.json"),
                    "FORGE_VISION_MODEL": ""}

    def tearDown(self):
        media_module.clear_vision_pick_cache()

    def _pick_with(self, models, calls):
        def fake_show(path, payload=None, timeout=120):
            calls.append((path, payload))
            if path == "/api/show":
                return {"capabilities": ["vision"]}
            return {}

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(media_module, "installed_models",
                                   return_value=list(models)):
                with mock.patch.object(media_module, "_ollama_json",
                                       side_effect=fake_show):
                    return media_module.pick_vision_model()

    def test_pick_cached_within_ttl(self):
        calls = []
        self.assertEqual(self._pick_with(["zzz-plain"], calls),
                         "zzz-plain")
        self.assertEqual(len(calls), 1)  # one /api/show, the last resort
        self.assertEqual(self._pick_with(["zzz-plain"], calls),
                         "zzz-plain")
        self.assertEqual(len(calls), 1)  # cached: no second query

    def test_clear_cache_forces_repick(self):
        calls = []
        self._pick_with(["zzz-plain"], calls)
        self.assertEqual(len(calls), 1)
        media_module.clear_vision_pick_cache()
        self._pick_with(["zzz-plain"], calls)
        self.assertEqual(len(calls), 2)

    def test_changed_model_list_invalidates(self):
        calls = []
        self._pick_with(["zzz-plain"], calls)
        self.assertEqual(len(calls), 1)
        self._pick_with(["zzz-plain", "zzz-other"], calls)
        self.assertEqual(len(calls), 2)  # a new model list re-picks


class PageSaveCoalesceTests(unittest.TestCase):
    """G-P14: rapid navigations coalesce into one page-memory write
    instead of rewriting megabytes of JSON per page — and any read
    flushes first, so readers never wait."""

    TEXT = ("gears and sprockets and the machines they drive, " * 10)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH")
        self.path = os.path.join(self.tmp, "pages.json")
        os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = self.path

    def tearDown(self):
        memory_module.flush_page_memory()
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_PAGE_MEMORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = self.old

    def _counting_writer(self, calls):
        original = memory_module._write_json_atomic

        def counting(path, payload, indent=None):
            calls.append(path)
            return original(path, payload, indent=indent)

        return mock.patch.object(memory_module, "_write_json_atomic",
                                 side_effect=counting)

    def test_burst_writes_once(self):
        calls = []
        with self._counting_writer(calls):
            memory_module.remember_page("https://a.example/1", "One",
                                        self.TEXT)
            memory_module.remember_page("https://b.example/2", "Two",
                                        self.TEXT)
            self.assertEqual(calls, [])  # nothing written yet
            memory_module.flush_page_memory()
        self.assertEqual(len(calls), 1)  # one write for both pages
        self.assertEqual(
            [p["url"] for p in memory_module.list_pages()],
            ["https://b.example/2", "https://a.example/1"])

    def test_read_flushes_first(self):
        memory_module.remember_page("https://a.example/1", "One",
                                    self.TEXT)
        self.assertFalse(os.path.exists(self.path))
        pages = memory_module.list_pages()  # the read flushes
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual([p["url"] for p in pages],
                         ["https://a.example/1"])

    def test_flush_writes_to_pending_path_not_current_home(self):
        # A flush after the home moved lands in the file the pages
        # were remembered for — not the new home's file.
        path_b = os.path.join(self.tmp, "other-pages.json")
        memory_module.remember_page("https://a.example/1", "One",
                                    self.TEXT)
        os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = path_b
        try:
            memory_module.flush_page_memory()
        finally:
            os.environ["FORGE_BROWSER_PAGE_MEMORY_PATH"] = self.path
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(path_b))


class EmbedJournalTests(unittest.TestCase):
    """G-P15: new embedding vectors are appended to a journal
    instead of re-writing the whole vector cache file on every call
    that adds entries."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "embed.json")
        self.journal = self.path + ".journal"

    def _fake_embed(self, texts):
        return [[float(len(t)), 0.0] for t in texts]

    def test_new_vectors_go_to_journal_not_full_rewrite(self):
        cache = memory_module.EmbedCache(self.path)
        cache.vectors_for(["aa", "bbb"], self._fake_embed, model="m")
        self.assertFalse(os.path.exists(self.path))  # no full rewrite
        self.assertTrue(os.path.exists(self.journal))
        with open(self.journal, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)

    def test_fresh_cache_reads_journal(self):
        cache = memory_module.EmbedCache(self.path)
        cache.vectors_for(["aa", "bbb"], self._fake_embed, model="m")
        fresh = memory_module.EmbedCache(self.path)

        def boom(texts):
            raise AssertionError("must not re-embed journaled vectors")

        vectors = fresh.vectors_for(["aa", "bbb"], boom, model="m")
        self.assertEqual(vectors, [[2.0, 0.0], [3.0, 0.0]])

    def test_journal_compacts_at_threshold(self):
        cache = memory_module.EmbedCache(self.path)
        with mock.patch.object(memory_module, "_EMBED_JOURNAL_COMPACT", 2):
            cache.vectors_for(["aa", "bbb", "cccc"], self._fake_embed,
                              model="m")
        self.assertTrue(os.path.exists(self.path))  # compacted
        self.assertFalse(os.path.exists(self.journal))
        fresh = memory_module.EmbedCache(self.path)

        def boom(texts):
            raise AssertionError("must not re-embed compacted vectors")

        vectors = fresh.vectors_for(["aa", "bbb", "cccc"], boom,
                                    model="m")
        self.assertEqual(vectors, [[2.0, 0.0], [3.0, 0.0], [4.0, 0.0]])

    def test_journal_creates_missing_folder(self):
        # A fresh install has no store folder yet: the journal write
        # makes its folder instead of failing silently and losing
        # the vectors (the old _save made the folder; the first
        # journal version did not).
        nested = os.path.join(self.tmp, "new", "deep", "embed.json")
        cache = memory_module.EmbedCache(nested)
        cache.vectors_for(["aa"], self._fake_embed, model="m")
        self.assertTrue(os.path.exists(nested + ".journal"))
        fresh = memory_module.EmbedCache(nested)

        def boom(texts):
            raise AssertionError("must not re-embed journaled vectors")

        vectors = fresh.vectors_for(["aa"], boom, model="m")
        self.assertEqual(vectors, [[2.0, 0.0]])


class CosineScoresTests(unittest.TestCase):
    """G-P16: page recall scores all chunk vectors with the query
    norm computed once, not once per vector."""

    def test_scores_correct(self):
        scores = retrieve_module.cosine_scores(
            [1.0, 0.0], [[1.0, 0.0], [0.0, 1.0], [0.0, 0.0],
                         [-1.0, 0.0]])
        self.assertEqual(scores, [1.0, 0.0, 0.0, -1.0])

    def test_zero_query_scores_zero(self):
        scores = retrieve_module.cosine_scores(
            [0.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(scores, [0.0, 0.0])

    def test_recall_pages_uses_scored_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_PAGE_MEMORY_PATH":
                   os.path.join(tmp, "pages.json"),
                   "FORGE_BROWSER_EMBED_CACHE_PATH":
                   os.path.join(tmp, "embed.json")}
            with mock.patch.dict(os.environ, env):
                text = ("gears and sprockets, " * 20)
                self.assertTrue(memory_module.remember_page(
                    "https://gears.example/", "Gears", text))

                def fake_embed(texts):
                    return [[1.0, 0.0] if "gear" in t.lower()
                            else [0.0, 1.0] for t in texts]

                cache = memory_module.EmbedCache()
                found = memory_module.search_pages(
                    "gears", embed_fn=fake_embed, embed_cache=cache)
            memory_module.flush_page_memory()
        self.assertEqual([f["url"] for f in found],
                         ["https://gears.example/"])


class StoreCacheTests(unittest.TestCase):
    """G-P18: settings, seats, and the per-site list are cached with
    mtime-based invalidation instead of re-read from disk on every
    call."""

    def test_settings_cached_until_mtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            env = {"FORGE_BROWSER_SETTINGS_PATH": path}
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"memo": "111"}')
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    settings_module.load_settings()["memo"], "111")
                _rewrite_same_key(path, '{"memo": "222"}')
                self.assertEqual(
                    settings_module.load_settings()["memo"], "111")
                _bump_mtime(path)
                self.assertEqual(
                    settings_module.load_settings()["memo"], "222")

    def test_seats_cached_until_mtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seats.json")
            env = {"FORGE_BROWSER_SEATS_PATH": path}
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"brain": "aaa"}')
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    seats_module.load_seats()["brain"], "aaa")
                _rewrite_same_key(path, '{"brain": "bbb"}')
                self.assertEqual(
                    seats_module.load_seats()["brain"], "aaa")
                _bump_mtime(path)
                self.assertEqual(
                    seats_module.load_seats()["brain"], "bbb")

    def test_sites_cached_until_mtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sites.json")
            env = {"FORGE_BROWSER_MEMORY_SITES_PATH": path}
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"blocked": ["a.example"], "allowed": []}')
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    memory_module._load_sites()["blocked"], ["a.example"])
                _rewrite_same_key(
                    path, '{"blocked": ["b.example"], "allowed": []}')
                self.assertEqual(
                    memory_module._load_sites()["blocked"], ["a.example"])
                _bump_mtime(path)
                self.assertEqual(
                    memory_module._load_sites()["blocked"], ["b.example"])


class HistoryCoalesceTests(unittest.TestCase):
    """G-P21: history saves coalesce like page-memory saves —
    rapid page loads share one disk write, and any read flushes
    first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_HISTORY_PATH")
        self.path = os.path.join(self.tmp, "history.json")
        os.environ["FORGE_BROWSER_HISTORY_PATH"] = self.path

    def tearDown(self):
        titles_module.flush_history()
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_HISTORY_PATH", None)
        else:
            os.environ["FORGE_BROWSER_HISTORY_PATH"] = self.old

    def _counting_writer(self, calls):
        original = titles_module._write_json_atomic

        def counting(path, payload, indent=None):
            calls.append(path)
            return original(path, payload, indent=indent)

        return mock.patch.object(titles_module, "_write_json_atomic",
                                 side_effect=counting)

    def test_burst_writes_once(self):
        calls = []
        with self._counting_writer(calls):
            titles_module.record_visit("https://a.example/1", "One")
            titles_module.record_visit("https://b.example/2", "Two")
            self.assertEqual(calls, [])  # nothing written yet
            titles_module.flush_history()
        self.assertEqual(len(calls), 1)  # one write for both visits
        self.assertEqual(
            [e["url"] for e in titles_module.load_history()],
            ["https://b.example/2", "https://a.example/1"])

    def test_read_flushes_first(self):
        titles_module.record_visit("https://a.example/1", "One")
        self.assertFalse(os.path.exists(self.path))
        entries = titles_module.load_history()  # the read flushes
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual([e["url"] for e in entries],
                         ["https://a.example/1"])

    def test_flush_writes_to_pending_path_not_current_home(self):
        path_b = os.path.join(self.tmp, "other-history.json")
        titles_module.record_visit("https://a.example/1", "One")
        os.environ["FORGE_BROWSER_HISTORY_PATH"] = path_b
        try:
            titles_module.flush_history()
        finally:
            os.environ["FORGE_BROWSER_HISTORY_PATH"] = self.path
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(path_b))


class GroupingCentroidTests(unittest.TestCase):
    """G-P22: tab grouping keeps a running centroid per group
    instead of rebuilding it from scratch per comparison — the
    groups it produces are unchanged."""

    def test_embedding_groups_match(self):
        items = [{"title": "Gears and sprockets",
                  "url": "https://a.example/1"},
                 {"title": "Gears for bikes",
                  "url": "https://b.example/2"},
                 {"title": "Soup recipes",
                  "url": "https://c.example/3"}]

        def fake_embed(texts):
            return [[1.0, 0.0] if "gear" in t.lower() else [0.0, 1.0]
                    for t in texts]

        groups = titles_module.group_items(items, embed_fn=fake_embed)
        self.assertEqual(len(groups), 2)
        sizes = sorted(len(g["items"]) for g in groups)
        self.assertEqual(sizes, [1, 2])

    def test_running_centroid_groups_incrementally(self):
        vectors = {"Alpha one": [1.0, 0.0],
                   "Alpha two": [0.98, 0.2],
                   "Alpha three": [0.9, 0.44],
                   "Beta one": [0.0, 1.0]}
        items = [{"title": title,
                  "url": f"https://example.org/{title.replace(' ', '-')}"}
                 for title in vectors]

        def fake_embed(texts):
            return [vectors[t] for t in texts]

        groups = titles_module.group_items(items, embed_fn=fake_embed)
        self.assertEqual(len(groups), 2)
        sizes = sorted(len(g["items"]) for g in groups)
        self.assertEqual(sizes, [1, 3])


class CardListCacheTests(unittest.TestCase):
    """G-P23: the parsed card list is cached with mtime
    invalidation instead of re-read and re-parsed on every chat
    turn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("FORGE_BROWSER_CARDS_DIR")
        os.environ["FORGE_BROWSER_CARDS_DIR"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FORGE_BROWSER_CARDS_DIR", None)
        else:
            os.environ["FORGE_BROWSER_CARDS_DIR"] = self.old

    def _titles(self):
        return {c.name: c.title for c in cards_module.list_cards()}

    def test_list_cached_until_file_changes(self):
        path = os.path.join(self.tmp, "alpha.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Alpha One\nPrompt here.\n")
        self.assertEqual(self._titles()["alpha"], "Alpha One")
        _rewrite_same_key(path, "# Alpha Two\nPrompt here.\n")
        self.assertEqual(self._titles()["alpha"], "Alpha One")  # cached
        _bump_mtime(path)
        self.assertEqual(self._titles()["alpha"], "Alpha Two")

    def test_new_card_file_invalidates(self):
        path = os.path.join(self.tmp, "alpha.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Alpha One\nPrompt here.\n")
        self.assertIn("alpha", self._titles())
        self.assertNotIn("beta", self._titles())
        with open(os.path.join(self.tmp, "beta.md"),
                  "w", encoding="utf-8") as handle:
            handle.write("# Beta One\nPrompt here.\n")
        self.assertEqual(self._titles()["beta"], "Beta One")


class ImageSizeCapTests(unittest.TestCase):
    """G-P24: an image over the size cap is refused before it is
    read or base64-inflated into a POST body."""

    def test_oversize_bytes_refused(self):
        big = b"x" * (media_module.MAX_DESCRIBE_IMAGE_BYTES + 1)
        with self.assertRaises(BrowserError) as caught:
            media_module.describe_image_bytes(big)
        self.assertIn("too big", str(caught.exception))

    def test_oversize_file_refused_without_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.png")
            with open(path, "wb") as handle:
                handle.seek(media_module.MAX_DESCRIBE_IMAGE_BYTES + 1)
                handle.write(b"x")  # sparse: big on paper, tiny on disk
            with mock.patch("builtins.open",
                            side_effect=AssertionError("read attempted")):
                with self.assertRaises(BrowserError) as caught:
                    media_module.describe_saved_image(path)
        self.assertIn("too big", str(caught.exception))

    def test_small_image_still_describes(self):
        with mock.patch.object(media_module, "pick_vision_model",
                               return_value="fake-vision"):
            with mock.patch.object(
                    media_module, "_ollama_json",
                    return_value={"response": "A gear."}):
                self.assertEqual(
                    media_module.describe_image_bytes(b"small"),
                    "A gear.")


class SensitivePageTests(unittest.TestCase):
    """G-S2: sensitive-page detection is no longer URL-keyword
    matching alone — known bank/health/government hosts and
    account/checkout page text are treated as sensitive too, so
    real banking visits are not recorded as full text."""

    def test_known_sensitive_hosts(self):
        sensitive = memory_module.is_sensitive_url
        self.assertTrue(sensitive("https://chase.com/login"))
        self.assertTrue(sensitive("https://mail.chase.com/"))
        self.assertTrue(sensitive("https://www.irs.gov/"))
        self.assertTrue(sensitive("https://example.gov/"))
        self.assertTrue(sensitive("https://portal.example.mil/x"))
        self.assertTrue(sensitive("https://paypal.com/"))
        # Substring lookalikes are not flagged: the match is on the
        # host or a parent domain, not a fragment.
        self.assertFalse(sensitive("https://notchase.com/"))
        self.assertFalse(sensitive("https://chase.example/"))
        self.assertFalse(sensitive("https://news.example/gears"))

    def test_sensitive_text_patterns(self):
        sensitive = memory_module.is_sensitive_text
        self.assertTrue(sensitive("Your account balance is $1,234.56"))
        self.assertTrue(sensitive("Enter the verification code we sent"))
        self.assertTrue(sensitive("Review your order summary below"))
        self.assertFalse(sensitive("A story about gears and sprockets"))
        self.assertFalse(sensitive(""))
        self.assertFalse(sensitive(None))

    def test_remember_page_skips_sensitive_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_PAGE_MEMORY_PATH":
                   os.path.join(tmp, "pages.json")}
            with mock.patch.dict(os.environ, env):
                text = "Your account balance is $5. " * 10
                self.assertFalse(memory_module.remember_page(
                    "https://neutral.example/account", "Account", text))
                self.assertEqual(memory_module.list_pages(), [])
            memory_module.flush_page_memory()

    def test_record_visit_skips_sensitive_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_HISTORY_PATH":
                   os.path.join(tmp, "history.json")}
            with mock.patch.dict(os.environ, env):
                titles_module.record_visit(
                    "https://neutral.example/x",
                    "Account Balance - Example Bank")
                self.assertEqual(titles_module.load_history(), [])
                titles_module.record_visit("https://news.example/x",
                                           "Gears news")
                self.assertEqual(len(titles_module.load_history()), 1)
            titles_module.flush_history()


class ReportLinkTests(unittest.TestCase):
    """G-S4 (research half): report links keep only http(s) hrefs —
    a prompt-injected javascript:/data: link renders as plain text,
    never as script in the privileged local-server origin."""

    def test_non_http_schemes_render_as_text(self):
        inline = research_module._inline
        self.assertNotIn("<a", inline("[click](javascript:alert(1))"))
        self.assertIn("click", inline("[click](javascript:alert(1))"))
        self.assertNotIn(
            "<a", inline("[x](data:text/html;base64,PHNjcmlwdD4=)"))
        # A control character smuggled into the scheme does not pass.
        self.assertNotIn("<a", inline("[x](java\tscript:alert(1))"))

    def test_http_links_still_linkified(self):
        inline = research_module._inline
        self.assertIn('<a href="https://example.org/">x</a>',
                      inline("[x](https://example.org/)"))
        self.assertIn('<a href="https://example.org/">',
                      inline("see https://example.org/ now"))


class SnapshotRedactTests(unittest.TestCase):
    """G-S5 (cli half): the snapshot command never prints
    password-field or hidden-input values to the terminal."""

    ELEMENTS = [
        {"i": 0, "tag": "input", "type": "password", "text": "hunter2"},
        {"i": 1, "tag": "input", "type": "text", "text": "hello"},
        {"i": 2, "tag": "input", "type": "hidden", "text": "tok123"},
        {"i": 3, "tag": "button", "type": "", "text": "Go"},
    ]

    def _data(self):
        return {"title": "T", "url": "https://example.org",
                "engine": "fake", "snapshot": "SNAP",
                "elements": [dict(e) for e in self.ELEMENTS]}

    def test_redact_blanks_password_and_hidden(self):
        data = self._data()
        redacted = cli_module._redact_snapshot_elements(data)
        texts = [e["text"] for e in redacted["elements"]]
        self.assertEqual(texts, ["", "hello", "", "Go"])
        # The library result is untouched.
        self.assertEqual(data["elements"][0]["text"], "hunter2")

    def test_cmd_snapshot_never_prints_passwords(self):
        # importlib, not `from forge.browser import render`: the
        # package __init__ shadows the render submodule with the
        # render() function.
        import importlib
        render_module = importlib.import_module("forge.browser.render")
        for as_json in (True, False):
            args = SimpleNamespace(url="https://example.org",
                                   engine=None, allow_local=False,
                                   json=as_json)
            with mock.patch.object(render_module, "snapshot",
                                   return_value=self._data()):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli_module._cmd_snapshot(args)
            self.assertEqual(code, 0)
            self.assertNotIn("hunter2", out.getvalue())
            self.assertNotIn("tok123", out.getvalue())
            self.assertIn("hello", out.getvalue())


class StorePermissionsTests(unittest.TestCase):
    """G-S7: history, memory, page memory, and session are written
    0600 in a 0700 folder, like the vault — not plaintext 0644."""

    def test_memory_file_and_folder_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                memory_module.remember("a private fact")
                path = os.path.join(tmp, ".forge", "memory.json")
                self.assertEqual(_mode(path), 0o600)
                self.assertEqual(_mode(os.path.join(tmp, ".forge")),
                                 0o700)

    def test_session_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_SESSION_PATH":
                   os.path.join(tmp, "session.json")}
            with mock.patch.dict(os.environ, env):
                session_module.save_session(
                    [{"url": "https://example.org/", "title": "T"}])
                self.assertEqual(
                    _mode(os.path.join(tmp, "session.json")), 0o600)

    def test_history_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_HISTORY_PATH":
                   os.path.join(tmp, "history.json")}
            with mock.patch.dict(os.environ, env):
                titles_module.record_visit("https://example.org/",
                                           "Example")
                titles_module.flush_history()
                self.assertEqual(
                    _mode(os.path.join(tmp, "history.json")), 0o600)

    def test_page_memory_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_BROWSER_PAGE_MEMORY_PATH":
                   os.path.join(tmp, "pages.json")}
            with mock.patch.dict(os.environ, env):
                memory_module.remember_page(
                    "https://example.org/", "Example",
                    "some page text here, " * 20)
                memory_module.flush_page_memory()
                self.assertEqual(
                    _mode(os.path.join(tmp, "pages.json")), 0o600)


class DescribeErrorTests(unittest.TestCase):
    """G-T9: describe_image_bytes returns "" when Ollama errors on
    /api/generate instead of surfacing the failure."""

    def test_ollama_error_returns_empty(self):
        with mock.patch.object(media_module, "pick_vision_model",
                               return_value="fake-vision"):
            with mock.patch.object(
                    media_module, "_ollama_json",
                    side_effect=Exception("Ollama is down")):
                self.assertEqual(
                    media_module.describe_image_bytes(b"tiny"), "")


if __name__ == "__main__":
    unittest.main()
