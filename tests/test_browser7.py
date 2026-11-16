"""Ship-area behavioral tests (review task list 2026-11-15, section D/G).

The v0.9 suite pinned the installer and CI mostly by grepping script
text (InstallerTests, CiTests) — D5/D6 are the proof that grep pins
pass while the real path is broken. These tests EXECUTE the scripts:
install.sh --check in a fake home, the GUI installer end to end, the
free CI against a failing suite, run.sh check against a stub Ollama,
and the Qt single-instance guard under offscreen Qt. Each class names
the review item it covers.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def _write_stub(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(cmd, cwd, env, timeout=120):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def _fake_home_env(tmp, extra=None):
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = home
    env["TMPDIR"] = tmp
    env.pop("FORGE_OLLAMA_URL", None)
    env.pop("FORGE_BROWSER_SETTINGS_PATH", None)
    if extra:
        env.update(extra)
    return home, env


# ---------------------------------------------------------------------
# G-T3: DocsTests cannot catch stale counts.


class DocsFreshnessTests(unittest.TestCase):
    """G-T3: the '50/50' and '493/493' strings passed the suite
    because DocsTests only asserts the CURRENT count appears. These
    tests reject stale counts outright."""

    def _live_count(self):
        loader = unittest.TestLoader()
        suite = loader.discover(
            start_dir=os.path.join(ROOT, "tests"))
        return suite.countTestCases()

    def test_no_stale_nn_pass_strings(self):
        for rel in ("BROWSER.md", "README.md"):
            self.assertIsNone(
                re.search(r"\d+/\d+\s*pass", _read(rel)),
                f"{rel} carries a stale NN/NN pass string")

    def test_no_stale_check_counts(self):
        count = self._live_count()
        for rel in ("BROWSER.md", "README.md"):
            for found in re.findall(r"(\d+) checks", _read(rel)):
                self.assertEqual(
                    int(found), count,
                    f"{rel} says {found} checks, the suite has {count}")

    def test_no_known_old_counts(self):
        for rel in ("BROWSER.md", "README.md"):
            text = _read(rel)
            self.assertNotIn("50/50", text)
            self.assertNotIn("493/493", text)


# ---------------------------------------------------------------------
# D5/D6/D9/D10/D16/D17/D18/D19 + G-T8: install.sh, executed.


class InstallCheckHarnessTests(unittest.TestCase):
    """D6: install.sh behavior, executed in a fake home — the
    --check harness (exit 0, no mode changes, no suite run), the
    preflights, the PySide6 failure path, the whisper hash check,
    and the Ollama/chat-model install (D10)."""

    def _fixture(self, tmp, whisper_pin=None, venv_stub="ok"):
        """A minimal tree install.sh can run in: the real script
        plus its pins and manifest, a stub venv, and one stub test
        so the self-check has something to run."""
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(tree, "tests"))
        os.makedirs(os.path.join(tree, ".venv", "bin"))
        for name in ("install.sh", "engine-pins.txt", "EXECUTABLES.txt",
                     "pyside-requirements.txt"):
            shutil.copy(os.path.join(ROOT, name),
                        os.path.join(tree, name))
        if whisper_pin is not None:
            pins_path = os.path.join(tree, "engine-pins.txt")
            pins = _read("engine-pins.txt")
            pins = re.sub(r"^whisper-base\.en .*$", whisper_pin, pins,
                          flags=re.MULTILINE)
            with open(pins_path, "w", encoding="utf-8") as handle:
                handle.write(pins)
        if venv_stub == "ok":
            stub = "#!/bin/sh\nexit 0\n"
        elif venv_stub == "fail":
            stub = "#!/bin/sh\nexit 1\n"
        else:  # pip-ok-import-fail (D18): pip succeeds, import fails
            stub = ("#!/bin/sh\n"
                    "case \"$*\" in\n"
                    "  *import*PySide6*) exit 1 ;;\n"
                    "  *) exit 0 ;;\n"
                    "esac\n")
        _write_stub(os.path.join(tree, ".venv", "bin", "python"), stub)
        with open(os.path.join(tree, "tests", "test_browser_stub.py"),
                  "w", encoding="utf-8") as handle:
            handle.write("import unittest\n\n\n"
                         "class StubTests(unittest.TestCase):\n"
                         "    def test_stub_passes(self):\n"
                         "        self.assertTrue(True)\n")
        # The D6 premise: a fresh clone carries no exec bits.
        for name in ("install.sh", "engine-pins.txt", "EXECUTABLES.txt"):
            os.chmod(os.path.join(tree, name), 0o644)
        return tree

    def _bin(self, tmp, curl="fail", ollama=False, df_free_kb=None):
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        if curl == "fail":
            _write_stub(os.path.join(bindir, "curl"),
                        "#!/bin/sh\nexit 1\n")
        elif curl == "whisper":
            _write_stub(os.path.join(bindir, "curl"),
                        "#!/bin/sh\n"
                        "# Serves canned bytes for the whisper URL;\n"
                        "# every other download fails.\n"
                        "out=\"\"; prev=\"\"; want=\"\"\n"
                        "for a in \"$@\"; do\n"
                        "    if [ \"$prev\" = \"-o\" ]; then out=\"$a\"; fi\n"
                        "    prev=\"$a\"\n"
                        "    case \"$a\" in *huggingface.co*) want=1 ;;\n"
                        "    esac\n"
                        "done\n"
                        "if [ -n \"$want\" ] && [ -n \"$out\" ]; then\n"
                        "    printf '%s' \"$CANNED_WHISPER_BYTES\" > \"$out\"\n"
                        "    exit 0\n"
                        "fi\n"
                        "exit 1\n")
        if ollama:
            _write_stub(os.path.join(bindir, "ollama"),
                        "#!/bin/sh\n"
                        "# A server that answers with no models.\n"
                        "if [ \"$1\" = \"list\" ]; then exit 0; fi\n"
                        "if [ \"$1\" = \"pull\" ]; then\n"
                        "    echo \"$2\" >> \"$OLLAMA_PULL_LOG\"\n"
                        "    exit 0\n"
                        "fi\n"
                        "if [ \"$1\" = \"--version\" ]; then\n"
                        "    echo \"ollama version 0.40.16\"\n"
                        "    exit 0\n"
                        "fi\n"
                        "exit 0\n")
        if df_free_kb is not None:
            _write_stub(os.path.join(bindir, "df"),
                        "#!/bin/sh\n"
                        "echo \"Filesystem  1K-blocks  Used Available "
                        "Use% Mounted on\"\n"
                        f"echo \"/dev/fake   10000000 9900000   "
                        f"{df_free_kb}  99% /home\"\n")
        return bindir

    def _run_install(self, tree, env, args):
        return _run(["sh", "install.sh"] + args, cwd=tree, env=env,
                    timeout=180)

    def test_check_exits_zero_and_changes_nothing(self):
        # D6: the --check harness — exit 0, no mode changes, and no
        # suite run (D5: --check is documented as change-free).
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            _home, env = _fake_home_env(tmp)
            before = {}
            for dirpath, _dirnames, filenames in os.walk(tree):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    before[os.path.relpath(path, tree)] = (
                        os.stat(path).st_mode, os.path.getmtime(path))
            result = self._run_install(tree, env, ["--check"])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Self-check: running", result.stdout)
            self.assertNotIn("Self-check: all tests pass", result.stdout)
            after = {}
            for dirpath, _dirnames, filenames in os.walk(tree):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    after[os.path.relpath(path, tree)] = (
                        os.stat(path).st_mode, os.path.getmtime(path))
            self.assertEqual(before, after,
                             "--check changed files in the tree")
            self.assertFalse(os.path.exists(
                os.path.join(tree, "__pycache__")),
                "--check ran Python that wrote bytecode")

    def test_check_reports_missing_engines(self):
        # D5: --check reports instead of installing.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            _home, env = _fake_home_env(tmp)
            result = self._run_install(tree, env, ["--check"])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Lightpanda NOT installed", result.stdout)
            self.assertIn("Chromium NOT installed", result.stdout)
            self.assertIn("app venv ready", result.stdout)

    def _minimal_path_farm(self, tmp, names):
        """A PATH holding only the named commands (symlinked) — so
        a missing-tool preflight can be proven without hiding the
        tools the script needs to reach it."""
        bindir = os.path.join(tmp, "farm")
        os.makedirs(bindir, exist_ok=True)
        for name in names:
            for directory in ("/usr/bin", "/bin"):
                src = os.path.join(directory, name)
                if os.path.isfile(src):
                    os.symlink(src, os.path.join(bindir, name))
                    break
        return bindir

    def test_install_names_missing_curl(self):
        # D16: curl is pre-flighted and named when missing.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            _home, env = _fake_home_env(tmp)
            farm = self._minimal_path_farm(
                tmp, ("dirname", "python3", "uname", "grep", "awk",
                      "sha256sum", "df"))
            env["PATH"] = farm
            result = _run(["/bin/sh", "install.sh", "--check"],
                          cwd=tree, env=env, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("curl", result.stdout)

    def test_install_names_missing_sha256sum(self):
        # D16: sha256sum is pre-flighted and named when missing.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            _home, env = _fake_home_env(tmp)
            farm = self._minimal_path_farm(
                tmp, ("dirname", "python3", "uname", "grep", "awk",
                      "curl", "df"))
            env["PATH"] = farm
            result = _run(["/bin/sh", "install.sh", "--check"],
                          cwd=tree, env=env, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sha256sum", result.stdout)

    def test_install_refuses_when_disk_full(self):
        # D17: under ~2 GB free the install refuses early, in plain
        # words, instead of downloading a gigabyte and dying.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            bindir = self._bin(tmp, df_free_kb=100000)
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            result = self._run_install(tree, env, ["--check"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("free space", result.stdout)

    def test_pyside_failure_fails_install(self):
        # D9: a failed venv/PySide6 build fails the install and says
        # the window will not open — no more exit 0 + "Install done"
        # with no working window.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp, venv_stub="fail")
            bindir = self._bin(tmp)
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            result = self._run_install(tree, env, [])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("PySide6 install FAILED", result.stdout)
            self.assertIn("will not open", result.stdout)
            self.assertNotIn("Install done.", result.stdout)

    def test_pyside_import_rechecked_after_install(self):
        # D18: pip succeeding is not proof — the import is re-run
        # after the install, and a missing system Qt lib fails it.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp, venv_stub="pip-ok-import-fail")
            bindir = self._bin(tmp)
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            result = self._run_install(tree, env, [])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("does not import", result.stdout)
            self.assertIn("will not open", result.stdout)

    def test_whisper_download_verifies_hash(self):
        # D19: the model file is verified against the pinned
        # SHA-256 before it is installed.
        with tempfile.TemporaryDirectory() as tmp:
            canned = b"fake-whisper-model-bytes-for-the-hash-test"
            digest = hashlib.sha256(canned).hexdigest()
            commit = re.search(
                r"^whisper-base\.en (\S+) \S+$",
                _read("engine-pins.txt"), re.MULTILINE).group(1)
            pin = f"whisper-base.en {commit} {digest}"
            tree = self._fixture(tmp, whisper_pin=pin)
            bindir = self._bin(tmp, curl="whisper")
            home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            env["CANNED_WHISPER_BYTES"] = canned.decode()
            result = self._run_install(tree, env, ["--with-whisper"])
            self.assertEqual(result.returncode, 0, result.stdout)
            model = os.path.join(home, ".forge", "whisper", "base.en")
            self.assertTrue(os.path.isfile(model))
            with open(model, "rb") as handle:
                self.assertEqual(handle.read(), canned)
            self.assertFalse(os.path.exists(model + ".tmp"))

    def test_whisper_hash_mismatch_refuses(self):
        # D19: bytes that do not match the pin are refused and the
        # partial file is removed — never installed.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)  # the real pin, wrong bytes
            bindir = self._bin(tmp, curl="whisper")
            home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            env["CANNED_WHISPER_BYTES"] = "bytes-that-do-not-match"
            result = self._run_install(tree, env, ["--with-whisper"])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("hash check", result.stdout)
            model = os.path.join(home, ".forge", "whisper", "base.en")
            self.assertFalse(os.path.exists(model))
            self.assertFalse(os.path.exists(model + ".tmp"))

    def test_install_pulls_chat_model_when_ollama_present(self):
        # D10: with Ollama answering, the install pulls a small chat
        # model (and the embeddings model) so chat works at first
        # launch.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            bindir = self._bin(tmp, ollama=True)
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            pull_log = os.path.join(tmp, "pulls.log")
            env["OLLAMA_PULL_LOG"] = pull_log
            result = self._run_install(tree, env, [])
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(pull_log, encoding="utf-8") as handle:
                pulls = handle.read()
            self.assertIn("qwen3:0.6b", pulls)
            self.assertIn("nomic-embed-text", pulls)

    def test_install_without_ollama_reports_chat_unavailable(self):
        # D10: with no Ollama and no working download, the install
        # says plainly that chat will not work — best-effort, so
        # the rest of the install still succeeds.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            bindir = self._bin(tmp)
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            result = self._run_install(tree, env, [])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Ollama", result.stdout)
            self.assertIn("chat will not work", result.stdout.lower())


# ---------------------------------------------------------------------
# D1/D2/D7/D25: ci/check.sh and ci/build-zip.py, executed.


class BuildZipTests(unittest.TestCase):
    """D7: build() matches executables by full relative path while
    verify() matched by basename — a nested same-basename file
    shipped 755 unnoticed. D1/D2/D25: the free CI, executed against
    a fixture suite instead of the real one."""

    def _zip_tree(self, tmp):
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(tree, "sub"))
        with open(os.path.join(tree, "install.sh"), "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(tree, "sub", "install.sh"), "w") as handle:
            handle.write("#!/bin/sh\necho nested\n")
        with open(os.path.join(tree, "README.md"), "w") as handle:
            handle.write("hi\n")
        return tree

    def test_build_and_verify_with_nested_same_basename(self):
        # D7: the nested copy of install.sh must ship 644 even
        # though the root one ships 755.
        import zipfile
        module = _load_module(
            "build_zip", os.path.join(ROOT, "ci", "build-zip.py"))
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._zip_tree(tmp)
            out = os.path.join(tmp, "release.zip")
            module.build(tree, out, {"install.sh"})
            module.verify(out, {"install.sh"})  # must not raise
            with zipfile.ZipFile(out) as zf:
                modes = {info.filename: info.external_attr >> 16
                         for info in zf.infolist()}
            self.assertEqual(modes["forge-browser-ai/install.sh"],
                             0o100755)
            self.assertEqual(modes["forge-browser-ai/sub/install.sh"],
                             0o100644)

    def test_verify_rejects_wrong_mode(self):
        # D7: verify() must actually catch a bad mode, not pass
        # whatever build() wrote.
        import zipfile
        module = _load_module(
            "build_zip", os.path.join(ROOT, "ci", "build-zip.py"))
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._zip_tree(tmp)
            out = os.path.join(tmp, "release.zip")
            module.build(tree, out, {"install.sh"})
            with zipfile.ZipFile(out) as zf:
                items = [(info, zf.read(info.filename))
                         for info in zf.infolist()]
            with zipfile.ZipFile(out, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                for info, data in items:
                    if info.filename == "forge-browser-ai/install.sh":
                        info.external_attr = 0o100644 << 16
                    zf.writestr(info, data)
            with self.assertRaises(SystemExit):
                module.verify(out, {"install.sh"})

    def _ci_fixture(self, tmp, failing):
        tree = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(tree, "ci"))
        os.makedirs(os.path.join(tree, "tests"))
        for name in ("check.sh", "build-zip.py"):
            shutil.copy(os.path.join(ROOT, "ci", name),
                        os.path.join(tree, "ci", name))
        with open(os.path.join(tree, "EXECUTABLES.txt"), "w") as handle:
            handle.write("install.sh\n")
        with open(os.path.join(tree, "install.sh"), "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(tree, "README.md"), "w") as handle:
            handle.write("# Fixture\n\n2 checks\n")
        with open(os.path.join(tree, "tests", "test_browser_ci.py"),
                  "w") as handle:
            handle.write(
                "import os, re, unittest\n"
                "\n\n"
                "class CiStatusPresentTests(unittest.TestCase):\n"
                "    def test_ci_status_present_during_suite(self):\n"
                "        root = os.path.dirname(\n"
                "            os.path.dirname(os.path.abspath(__file__)))\n"
                "        with open(os.path.join(root, 'CI-STATUS.md'),\n"
                "                  encoding='utf-8') as handle:\n"
                "            status = handle.read()\n"
                "        with open(os.path.join(root, 'README.md'),\n"
                "                  encoding='utf-8') as handle:\n"
                "            readme = handle.read()\n"
                "        readme_count = re.search(r'(\\d+) checks',\n"
                "                                 readme).group(1)\n"
                "        status_count = re.search(r'(\\d+) checks',\n"
                "                                 status).group(1)\n"
                "        self.assertEqual(status_count, readme_count)\n"
                "        self.assertIn('Release zip:', status)\n")
        with open(os.path.join(tree, "tests", "test_browser_other.py"),
                  "w") as handle:
            if failing:
                handle.write(
                    "import unittest\n\n\n"
                    "class DeliberateFailureTests(unittest.TestCase):\n"
                    "    def test_deliberate_failure(self):\n"
                    "        self.fail('deliberate failure')\n")
            else:
                handle.write(
                    "import unittest\n\n\n"
                    "class PassingTests(unittest.TestCase):\n"
                    "    def test_passes(self):\n"
                    "        self.assertTrue(True)\n")
        return tree

    def test_check_sh_against_failing_suite(self):
        # D7/D1/D2: a deliberately failing fixture suite must fail
        # check.sh, name the failing test, and NOT name the
        # CI-STATUS test (the D1 provisional file is in place
        # before the suite runs). The commit comes from the env.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._ci_fixture(tmp, failing=True)
            _home, env = _fake_home_env(tmp)
            env["FORGE_BROWSER_CI_TMP"] = os.path.join(tmp, "ci-tmp")
            env["FORGE_BROWSER_CI_COMMIT"] = "testcommit123"
            result = _run(["sh", "ci/check.sh"], cwd=tree, env=env,
                          timeout=180)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("test_deliberate_failure", result.stdout)
            self.assertNotIn("test_ci_status_present_during_suite",
                             result.stdout)
            with open(os.path.join(tree, "CI-STATUS.md"),
                      encoding="utf-8") as handle:
                status = handle.read()
            self.assertIn("testcommit123", status)
            self.assertIn("FAIL", status)

    def test_check_sh_passes_and_stamps_commit_arg(self):
        # D2: --commit stamps the verdict; D25: with no override
        # the work directory lives under TMPDIR.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._ci_fixture(tmp, failing=False)
            _home, env = _fake_home_env(tmp)
            env.pop("FORGE_BROWSER_CI_TMP", None)
            env.pop("FORGE_BROWSER_CI_COMMIT", None)
            env["TMPDIR"] = os.path.join(tmp, "tmpdir")
            os.makedirs(env["TMPDIR"])
            result = _run(["sh", "ci/check.sh", "--commit",
                           "deadbee99"], cwd=tree, env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(os.path.join(tree, "CI-STATUS.md"),
                      encoding="utf-8") as handle:
                status = handle.read()
            self.assertIn("deadbee99", status)
            self.assertIn("PASS", status)

    def test_check_sh_fails_when_tmpdir_missing(self):
        # D25: TMPDIR points at a directory that does not exist —
        # check.sh must fail early and clearly, not halfway through.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._ci_fixture(tmp, failing=False)
            _home, env = _fake_home_env(tmp)
            env.pop("FORGE_BROWSER_CI_TMP", None)
            env["TMPDIR"] = os.path.join(tmp, "no-such-dir")
            result = _run(["sh", "ci/check.sh"], cwd=tree, env=env,
                          timeout=120)
            self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------
# G-T4/D8/D13/D28: run.sh, executed.


class RunShTests(unittest.TestCase):
    """G-T4: run.sh is untested. These tests execute it in a
    fixture tree: `check` against a stub Ollama server, the demo
    and guided-test routing, URL forwarding (D13), the failed
    launch dialog (D8), and the settings-file Ollama URL (D28)."""

    def _fixture(self, tmp, real_forge=False):
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(tree, "tests"))
        shutil.copy(os.path.join(ROOT, "run.sh"),
                    os.path.join(tree, "run.sh"))
        if real_forge:
            os.symlink(os.path.join(ROOT, "forge"),
                       os.path.join(tree, "forge"))
            shutil.copy(os.path.join(ROOT, "engine-pins.txt"),
                        os.path.join(tree, "engine-pins.txt"))
        else:
            forge = os.path.join(tree, "forge", "browser")
            os.makedirs(forge)
            for name in ("__init__.py",):
                open(os.path.join(tree, "forge", name), "w").close()
                open(os.path.join(forge, name), "w").close()
            with open(os.path.join(forge, "__main__.py"), "w") as h:
                h.write("import os, sys\n"
                        "with open(os.environ['FORGE_CALL_LOG'], 'a') "
                        "as fh:\n"
                        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n")
        return tree

    def _validators(self, tree, threshold=0, memory=0):
        # The stub validators exit with the given code, so a test
        # can make one of them fail on purpose (threshold=1).
        for name, code in (("validate_threshold.py", threshold),
                           ("validate_memory_threshold.py", memory)):
            with open(os.path.join(tree, "tests", name), "w") as h:
                h.write(f"import sys\nsys.exit({int(code)})\n")

    def _start_stub_ollama(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/tags":
                    body = b'{"models": []}'
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/json")
                    self.send_header("Content-Length",
                                     str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        return server, thread

    def _check_env(self, tmp, ollama_url=None, settings=None):
        home, env = _fake_home_env(tmp)
        if ollama_url is not None:
            env["FORGE_OLLAMA_URL"] = ollama_url
        if settings is not None:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(settings, handle)
            env["FORGE_BROWSER_SETTINGS_PATH"] = path
        return home, env

    def test_check_with_ollama_pass(self):
        server, _thread = self._start_stub_ollama()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tree = self._fixture(tmp, real_forge=True)
                self._validators(tree)
                url = (f"http://127.0.0.1:"
                       f"{server.server_address[1]}")
                _home, env = self._check_env(tmp, ollama_url=url)
                result = _run(["sh", "run.sh", "check"], cwd=tree,
                              env=env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("not run: no Ollama answering",
                                 result.stdout)
                self.assertIn("validate_threshold: PASS", result.stdout)
                self.assertIn("validate_memory_threshold: PASS",
                              result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_check_with_ollama_validator_fail(self):
        # A failing validator fails `run.sh check` (exit 1) even
        # though Ollama is up.
        server, _thread = self._start_stub_ollama()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tree = self._fixture(tmp, real_forge=True)
                self._validators(tree, threshold=1)
                url = (f"http://127.0.0.1:"
                       f"{server.server_address[1]}")
                _home, env = self._check_env(tmp, ollama_url=url)
                result = _run(["sh", "run.sh", "check"], cwd=tree,
                              env=env, timeout=120)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("validate_threshold: FAIL", result.stdout)
                self.assertIn("validate_memory_threshold: PASS",
                              result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_check_uses_settings_ollama_url(self):
        # D28: the probe honors the Ollama URL from the settings
        # file, not only the env var and the default.
        server, _thread = self._start_stub_ollama()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tree = self._fixture(tmp, real_forge=True)
                self._validators(tree)
                url = (f"http://127.0.0.1:"
                       f"{server.server_address[1]}")
                _home, env = self._check_env(
                    tmp, settings={"ollama_url": url})
                result = _run(["sh", "run.sh", "check"], cwd=tree,
                              env=env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("not run: no Ollama answering",
                                 result.stdout)
                self.assertIn("validate_threshold: PASS", result.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_demo_routing(self):
        # G-T4: `run.sh demo` runs engines, a search, and a fetch,
        # in that order.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            log = os.path.join(tmp, "calls.log")
            _home, env = _fake_home_env(tmp)
            env["FORGE_CALL_LOG"] = log
            result = _run(["sh", "run.sh", "demo"], cwd=tree,
                          env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(log, encoding="utf-8") as handle:
                calls = handle.read()
            self.assertEqual(
                calls.splitlines(),
                ["engines",
                 "search ollama local llm --max 3",
                 "fetch https://example.org --max-chars 400"])

    def test_test_routing(self):
        # G-T4: `run.sh test` hands off to the guided test script.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            _write_stub(os.path.join(tree, "test-forge-browser.sh"),
                        "#!/bin/sh\necho GUIDED-TEST-STUB-RAN\n")
            _home, env = _fake_home_env(tmp)
            result = _run(["sh", "run.sh", "test"], cwd=tree,
                          env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("GUIDED-TEST-STUB-RAN", result.stdout)

    def _py_stub(self, tree, exit_code=0):
        venv_bin = os.path.join(tree, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        _write_stub(os.path.join(venv_bin, "python"),
                    "#!/bin/sh\n"
                    "echo \"$@\" >> \"$PY_CALL_LOG\"\n"
                    "echo \"Traceback (most recent call last)\" >&2\n"
                    "echo \"ImportError: No module named PySide6\" >&2\n"
                    f"exit {exit_code}\n")

    def test_url_forwarding(self):
        # D13: a bare URL launches the program on that URL.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            self._py_stub(tree)
            log = os.path.join(tmp, "py.log")
            _home, env = _fake_home_env(tmp)
            env["PY_CALL_LOG"] = log
            result = _run(["sh", "run.sh",
                           "https://example.org/page"],
                          cwd=tree, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(log, encoding="utf-8") as handle:
                self.assertEqual(
                    handle.read().strip(),
                    "-m forge.browser app https://example.org/page")

    def test_passthrough_unchanged(self):
        # Other commands still pass through to the engine room.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            self._py_stub(tree)
            log = os.path.join(tmp, "py.log")
            _home, env = _fake_home_env(tmp)
            env["PY_CALL_LOG"] = log
            result = _run(["sh", "run.sh", "search", "foo"],
                          cwd=tree, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(log, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(),
                                 "-m forge.browser search foo")

    def test_bare_launch(self):
        # No arguments launches the program window.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            self._py_stub(tree)
            log = os.path.join(tmp, "py.log")
            _home, env = _fake_home_env(tmp)
            env["PY_CALL_LOG"] = log
            result = _run(["sh", "run.sh"], cwd=tree, env=env,
                          timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(log, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(),
                                 "-m forge.browser app")

    def test_failed_launch_shows_error_dialog(self):
        # D8: a launch that dies must not fail silently — the
        # dialog names the failure and shows the last error lines.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._fixture(tmp)
            self._py_stub(tree, exit_code=1)
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            _write_stub(os.path.join(bindir, "zenity"),
                        "#!/bin/sh\n"
                        "echo \"$@\" >> \"$ZENITY_LOG\"\n"
                        "exit 0\n")
            zenity_log = os.path.join(tmp, "zenity.log")
            py_log = os.path.join(tmp, "py.log")
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            env["ZENITY_LOG"] = zenity_log
            env["PY_CALL_LOG"] = py_log
            result = _run(["sh", "run.sh"], cwd=tree, env=env,
                          timeout=60)
            self.assertNotEqual(result.returncode, 0)
            with open(zenity_log, encoding="utf-8") as handle:
                calls = handle.read()
            self.assertIn("--error", calls)
            self.assertIn("did not start", calls)
            self.assertIn("PySide6", calls)


# ---------------------------------------------------------------------
# G-T1: the Qt single-instance guard, genuinely exercised.


try:
    import PySide6  # noqa: F401
    _QT_IMPORTED = True
except ImportError:
    _QT_IMPORTED = False


class SingleInstanceTests(unittest.TestCase):
    """G-T1: the single-instance guard exists only in a comment and
    a docstring. This test drives it for real under offscreen Qt:
    the first launch takes the instance, the second is refused and
    the running window is raised."""

    def test_second_launch_pings_first(self):
        if not _QT_IMPORTED:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6 import QtWidgets
            app = (QtWidgets.QApplication.instance()
                   or QtWidgets.QApplication([]))
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"Qt offscreen unavailable: {exc}")
        self.assertIsNotNone(app)
        from forge.browser.app import (
            _handle_show_request, _take_single_instance)
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            with mock.patch.dict(os.environ, {"HOME": home}):
                first = _take_single_instance()
                self.assertIsNotNone(
                    first, "first launch should take the instance")
                lock, server = first
                try:
                    second = _take_single_instance()
                    self.assertIsNone(
                        second, "second launch must not take it")
                    import time
                    from PySide6 import QtCore
                    for _ in range(100):
                        QtCore.QCoreApplication.processEvents()
                        if server.hasPendingConnections():
                            break
                        time.sleep(0.02)
                    self.assertTrue(
                        server.hasPendingConnections(),
                        "the ping never reached the server")
                    window = QtWidgets.QMainWindow()
                    _handle_show_request(server, window)
                    self.assertTrue(
                        window.isVisible(),
                        "the running window was not raised")
                finally:
                    server.close()
                    from PySide6.QtNetwork import QLocalServer
                    QLocalServer.removeServer("forge-browser")
                    lock.unlock()


# ---------------------------------------------------------------------
# G-T8/D11/D12/D13/D21/D22/D23: the GUI installer, executed.


class InstallerBehaviorTests(unittest.TestCase):
    """G-T8: InstallerTests greps the installer; these tests RUN
    it in a fake home with a stub zenity, a stub install.sh, and a
    stub timeout — the behavioral partners for D11/D12/D13/D21/
    D22/D23."""

    def _harness(self, tmp, src_venv=False, old_venv=None,
                 with_icon=True, install_sh=None, timeout_stub=False,
                 zenity_progress="wait"):
        """A fake release dir + fake home. old_venv: None (no old
        install), "ok" (working .venv with a marker), or "broken"
        (a .venv whose python always exits 1)."""
        src = os.path.join(tmp, "src")
        home = os.path.join(tmp, "home")
        bindir = os.path.join(tmp, "bin")
        os.makedirs(src)
        os.makedirs(home)
        os.makedirs(bindir)
        for name in ("Install Forge Browser", "run.sh", "install.sh"):
            shutil.copy(os.path.join(ROOT, name),
                        os.path.join(src, name))
        if with_icon:
            shutil.copy(os.path.join(ROOT, "forge-browser.svg"),
                        os.path.join(src, "forge-browser.svg"))
        with open(os.path.join(src, "new-file.txt"), "w") as handle:
            handle.write("new\n")
        if src_venv:
            venv_bin = os.path.join(src, ".venv", "bin")
            os.makedirs(venv_bin)
            _write_stub(os.path.join(venv_bin, "python"),
                        "#!/bin/sh\nexit 0\n")
            with open(os.path.join(src, ".venv", "SRC-VENV"), "w") \
                    as handle:
                handle.write("source venv — must not be copied\n")
            pycache = os.path.join(src, "forge", "__pycache__")
            os.makedirs(pycache)
            open(os.path.join(pycache, "junk.pyc"), "w").close()
        app_dir = os.path.join(home, ".local", "share",
                               "forge-browser-app")
        if old_venv is not None:
            os.makedirs(os.path.join(app_dir, ".venv", "bin"))
            with open(os.path.join(app_dir, "OLD-MARKER"), "w") \
                    as handle:
                handle.write("old\n")
            if old_venv == "ok":
                _write_stub(os.path.join(app_dir, ".venv", "bin",
                                         "python"),
                            "#!/bin/sh\nexit 0\n")
            else:
                _write_stub(os.path.join(app_dir, ".venv", "bin",
                                         "python"),
                            "#!/bin/sh\nexit 1\n")
            with open(os.path.join(app_dir, ".venv", "OLD-VENV"),
                      "w") as handle:
                handle.write("old venv\n")
        if install_sh is None:
            install_sh = ("#!/bin/sh\n"
                          "mkdir -p .venv/bin\n"
                          "printf '#!/bin/sh\\nexit 0\\n' "
                          "> .venv/bin/python\n"
                          "chmod +x .venv/bin/python\n"
                          "echo ok\n")
        with open(os.path.join(src, "install.sh"), "w") as handle:
            handle.write(install_sh)
        zenity_log = os.path.join(tmp, "zenity.log")
        if zenity_progress == "wait":
            progress = (
                "if [ \"$1\" = \"--progress\" ]; then\n"
                "    while [ ! -f \"$HOME/.local/share/"
                "forge-browser-app.install-done\" ]; do\n"
                "        sleep 0.2\n"
                "    done\n"
                "    cat >/dev/null\n"
                "    exit 0\n"
                "fi\n")
        else:  # cancel: the user presses Cancel at once
            progress = ("if [ \"$1\" = \"--progress\" ]; then\n"
                        "    exit 1\n"
                        "fi\n")
        _write_stub(os.path.join(bindir, "zenity"),
                    "#!/bin/sh\n"
                    "echo \"$@\" >> \"$ZENITY_LOG\"\n"
                    "if [ \"$1\" = \"--question\" ]; then\n"
                    "    f=\"$0-count\"\n"
                    "    n=$(cat \"$f\" 2>/dev/null || echo 0)\n"
                    "    n=$((n + 1)); echo \"$n\" > \"$f\"\n"
                    "    if [ \"$n\" = \"1\" ]; then exit 0; fi\n"
                    "    exit 1\n"
                    "fi\n" + progress + "exit 0\n")
        if timeout_stub:
            _write_stub(os.path.join(bindir, "timeout"),
                        "#!/bin/sh\nexit 124\n")
        env = dict(os.environ)
        env["HOME"] = home
        env["TMPDIR"] = tmp
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        env["ZENITY_LOG"] = zenity_log
        return src, home, app_dir, env, zenity_log

    def _run_installer(self, src, env):
        return _run(["sh", "Install Forge Browser"], cwd=src,
                    env=env, timeout=120)

    def test_gui_installer_excludes_source_venv(self):
        # D11: a .venv (and __pycache__) in the release tree is
        # excluded from the copy — the old tree's absolute paths
        # must never reach the installed program.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, app_dir, env, _log = self._harness(
                tmp, src_venv=True)
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(os.path.join(
                app_dir, ".venv", "SRC-VENV")))
            self.assertFalse(os.path.exists(os.path.join(
                app_dir, "forge", "__pycache__")))

    def test_gui_installer_carries_working_old_venv(self):
        # D12: an old install whose venv still imports PySide6 is
        # carried into the new tree instead of rebuilt.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, app_dir, env, _log = self._harness(
                tmp, src_venv=True, old_venv="ok",
                install_sh="#!/bin/sh\necho ok\n")
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(os.path.exists(os.path.join(
                app_dir, ".venv", "OLD-VENV")))
            self.assertFalse(os.path.exists(os.path.join(
                app_dir, ".venv", "SRC-VENV")))

    def test_gui_installer_does_not_carry_broken_old_venv(self):
        # D12: a broken old venv is not carried — the install
        # rebuilds it.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, app_dir, env, _log = self._harness(
                tmp, old_venv="broken")
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(os.path.join(
                app_dir, ".venv", "OLD-VENV")))

    def test_desktop_entry_supports_default_browser(self):
        # D13: the menu entry handles links and reports the right
        # window class, so it can be the default browser.
        with tempfile.TemporaryDirectory() as tmp:
            src, home, app_dir, env, _log = self._harness(tmp)
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            desktop = os.path.join(
                home, ".local", "share", "applications",
                "forge-browser.desktop")
            with open(desktop, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn(
                "MimeType=text/html;x-scheme-handler/http;"
                "x-scheme-handler/https;", text)
            self.assertIn(f"Exec=sh {app_dir}/run.sh %u", text)
            self.assertIn("StartupWMClass=Forge Browser", text)

    def test_installer_warns_when_icon_missing(self):
        # D23: a release without the icon file says so in the final
        # dialog instead of silently shipping a generic icon.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, _app_dir, env, zenity_log = self._harness(
                tmp, with_icon=False)
            result = self._run_installer(src, env)
            self.assertEqual(result.returncode, 0, result.stdout)
            with open(zenity_log, encoding="utf-8") as handle:
                self.assertIn("generic icon", handle.read())

    def test_installer_names_one_hour_timeout(self):
        # D22: a timeout (exit 124) is named as the 1-hour limit,
        # not a generic failure.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, _app_dir, env, zenity_log = self._harness(
                tmp, timeout_stub=True)
            result = self._run_installer(src, env)
            self.assertNotEqual(result.returncode, 0)
            with open(zenity_log, encoding="utf-8") as handle:
                calls = handle.read()
            self.assertIn("--error", calls)
            self.assertIn("1-hour", calls)

    def test_installer_cancel_stops_install(self):
        # D21: pressing Cancel stops the install instead of
        # leaving it running detached.
        with tempfile.TemporaryDirectory() as tmp:
            src, _home, _app_dir, env, zenity_log = self._harness(
                tmp, zenity_progress="cancel",
                install_sh="#!/bin/sh\nsleep 30\necho ok\n")
            result = self._run_installer(src, env)
            self.assertNotEqual(result.returncode, 0)
            with open(zenity_log, encoding="utf-8") as handle:
                calls = handle.read()
            self.assertIn("--error", calls)
            self.assertIn("cancelled", calls.lower())


# ---------------------------------------------------------------------
# A6/D24/G-T10: browser-setup.sh, executed.


class BrowserSetupTests(unittest.TestCase):
    """A6/D24: the SearXNG setup pins an exact commit, generates a
    random secret key, and keeps an existing key. G-T10: the setup
    script's usage and no-pin errors."""

    def _git_stub(self, bindir):
        _write_stub(os.path.join(bindir, "git"),
                    "#!/bin/sh\n"
                    "# Stub git: answers rev-parse from a marker\n"
                    "# file, and 'fetches' the pinned commit by\n"
                    "# writing that marker.\n"
                    "dir=\"\"; prev=\"\"\n"
                    "for a in \"$@\"; do\n"
                    "    if [ \"$prev\" = \"-C\" ]; then dir=\"$a\"; fi\n"
                    "    prev=\"$a\"\n"
                    "done\n"
                    "case \"$*\" in\n"
                    "  *init*) mkdir -p \"$dir/.git\" ;;\n"
                    "  *fetch*)\n"
                    "    mkdir -p \"$dir/.git\"\n"
                    "    : > \"$dir/requirements.txt\"\n"
                    "    echo \"$SEARXNG_PIN\" > \"$dir/.git/HEAD_SHA\"\n"
                    "    ;;\n"
                    "  *rev-parse*)\n"
                    "    cat \"$dir/.git/HEAD_SHA\" 2>/dev/null ||\n"
                    "      echo 0000000000000000000000000000000000000000\n"
                    "    ;;\n"
                    "esac\n"
                    "exit 0\n")

    def _python_stub(self, bindir):
        # A python3 that fakes `-m venv` (a venv dir with stub
        # python/pip) and passes everything else to the real
        # interpreter (via $REAL_PYTHON3). Real venv creation takes
        # ~2 minutes in this sandbox (CPU-starved ensurepip); the
        # venv mechanics are not what A6/D24 test — the pin, the
        # key, and the settings are — so the harness fakes them.
        _write_stub(os.path.join(bindir, "python3"),
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"venv\" ]; then\n"
                    "    [ -n \"$VENV_LOG\" ] && echo \"venv $3\" >> "
                    "\"$VENV_LOG\"\n"
                    "    mkdir -p \"$3/bin\"\n"
                    "    printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
                    "    printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/pip\"\n"
                    "    chmod +x \"$3/bin/python\" \"$3/bin/pip\"\n"
                    "    exit 0\n"
                    "fi\n"
                    "exec \"$REAL_PYTHON3\" \"$@\"\n")

    def _secret_key(self, path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        match = re.search(r'secret_key: "([0-9a-f]+)"', text)
        self.assertIsNotNone(match, f"no secret key in {path}")
        return match.group(1)

    def test_searxng_setup_pins_commit_and_randomizes_secret(self):
        # A6: the secret key is random per install, kept across
        # re-runs, and replaced when it is the old shared literal.
        # D24: the source is pinned to one commit and a stale tree
        # is re-fetched, never silently reused.
        script = _read("browser-setup.sh")
        pin = re.search(r'SEARXNG_PIN="([0-9a-f]{40})"',
                        script).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            shutil.copy(os.path.join(ROOT, "browser-setup.sh"),
                        os.path.join(tree, "browser-setup.sh"))
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            self._git_stub(bindir)
            self._python_stub(bindir)
            home, env = _fake_home_env(tmp)
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            env["REAL_PYTHON3"] = sys.executable
            env["SEARXNG_PIN"] = pin
            venv_log = os.path.join(tmp, "venv.log")
            env["VENV_LOG"] = venv_log
            base = os.path.join(home, ".local", "share",
                                "forge-browser")
            settings = os.path.join(base, "searxng", "config",
                                    "settings.yml")
            src = os.path.join(base, "searxng", "src")

            # Fresh setup: random 64-hex key, pinned commit.
            result = _run(["sh", "browser-setup.sh", "searxng"],
                          cwd=tree, env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout)
            key1 = self._secret_key(settings)
            self.assertEqual(len(key1), 64)
            self.assertNotEqual(
                key1, "forge-browser-local-only-change-if-you-like")
            with open(os.path.join(src, ".git", "HEAD_SHA")) as h:
                self.assertEqual(h.read().strip(), pin)

            # Re-run: the key is kept, the pinned tree is reused.
            result = _run(["sh", "browser-setup.sh", "searxng"],
                          cwd=tree, env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(self._secret_key(settings), key1)

            # A settings file carrying the old literal gets a new
            # random key.
            with open(settings, encoding="utf-8") as handle:
                text = handle.read()
            with open(settings, "w", encoding="utf-8") as handle:
                handle.write(text.replace(
                    key1, "forge-browser-local-only-change-if-you-like"))
            result = _run(["sh", "browser-setup.sh", "searxng"],
                          cwd=tree, env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout)
            key2 = self._secret_key(settings)
            self.assertEqual(len(key2), 64)
            self.assertNotEqual(
                key2, "forge-browser-local-only-change-if-you-like")

            # A stale source tree (wrong commit) is removed and
            # re-fetched, never silently reused.
            with open(os.path.join(src, ".git", "HEAD_SHA"), "w") \
                    as handle:
                handle.write("f" * 40)
            stale = os.path.join(src, "STALE-MARKER")
            with open(stale, "w") as handle:
                handle.write("stale\n")
            result = _run(["sh", "browser-setup.sh", "searxng"],
                          cwd=tree, env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(os.path.exists(stale))
            with open(os.path.join(src, ".git", "HEAD_SHA")) as h:
                self.assertEqual(h.read().strip(), pin)

            # A working venv is reused, not rebuilt: four setup
            # runs cost one venv creation.
            with open(venv_log, encoding="utf-8") as handle:
                creations = [line for line in handle.read().splitlines()
                             if line.startswith("venv ")]
            self.assertEqual(len(creations), 1,
                             "setup rebuilt the venv on a re-run")

    def test_searxng_setup_refuses_without_git(self):
        # D24: no git, no clone — a clear refusal, not a crash.
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            shutil.copy(os.path.join(ROOT, "browser-setup.sh"),
                        os.path.join(tree, "browser-setup.sh"))
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            os.symlink("/bin/mkdir", os.path.join(bindir, "mkdir"))
            os.symlink("/usr/bin/dirname",
                       os.path.join(bindir, "dirname"))
            _home, env = _fake_home_env(tmp)
            env["PATH"] = bindir
            result = _run(["/bin/sh", "browser-setup.sh", "searxng"],
                          cwd=tree, env=env, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("git", result.stdout.lower())

    def test_lightpanda_setup_refuses_without_pin(self):
        # G-T10: the Lightpanda setup refuses to fetch an
        # unpinned engine.
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            shutil.copy(os.path.join(ROOT, "browser-setup.sh"),
                        os.path.join(tree, "browser-setup.sh"))
            _home, env = _fake_home_env(tmp)
            result = _run(["sh", "browser-setup.sh", "lightpanda"],
                          cwd=tree, env=env, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No pin", result.stdout)

    def test_browser_setup_usage_error(self):
        # G-T10: an unknown argument prints usage and exits 2.
        with tempfile.TemporaryDirectory() as tmp:
            _home, env = _fake_home_env(tmp)
            result = _run(["sh", "browser-setup.sh", "bogus"],
                          cwd=ROOT, env=env, timeout=60)
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage", result.stdout.lower())


# ---------------------------------------------------------------------
# G-T10/D21: the guided test script and the GUI installer dialog.


class GuidedTestTests(unittest.TestCase):
    """G-T10: test-forge-browser.sh gains --step selection, tested
    in a fixture tree with a stub suite (never the real tree — a
    nested full-suite run would recurse). D21: install-gui.py's
    Cancel kills the install's process group."""

    def _guided_fixture(self, tmp, failing=False):
        tree = os.path.join(tmp, "tree")
        os.makedirs(os.path.join(tree, "tests"))
        shutil.copy(os.path.join(ROOT, "test-forge-browser.sh"),
                    os.path.join(tree, "test-forge-browser.sh"))
        with open(os.path.join(tree, "tests", "test_browser_stub.py"),
                  "w") as handle:
            if failing:
                handle.write("import unittest\n\n\n"
                             "class StubTests(unittest.TestCase):\n"
                             "    def test_stub_fails(self):\n"
                             "        self.fail('stub failure')\n")
            else:
                handle.write("import unittest\n\n\n"
                             "class StubTests(unittest.TestCase):\n"
                             "    def test_stub_passes(self):\n"
                             "        self.assertTrue(True)\n")
        return tree

    def test_step_one_runs_suite_in_isolation(self):
        # G-T10: --step 1 runs only the unit tests and reports them.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._guided_fixture(tmp)
            _home, env = _fake_home_env(tmp)
            result = _run(["sh", "test-forge-browser.sh",
                           "--step", "1"], cwd=tree, env=env,
                          timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("PASS: unit tests", result.stdout)
            self.assertNotIn("2. What this machine has", result.stdout)

    def test_step_one_reports_failure(self):
        # G-T10: a failing step-1 suite is reported as FAIL.
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._guided_fixture(tmp, failing=True)
            _home, env = _fake_home_env(tmp)
            result = _run(["sh", "test-forge-browser.sh",
                           "--step", "1"], cwd=tree, env=env,
                          timeout=120)
            self.assertIn("FAIL: unit tests", result.stdout)

    def test_install_gui_usage_errors(self):
        # G-T10: install-gui.py rejects bad invocations with exit 2.
        gui = os.path.join(ROOT, "install-gui.py")
        with tempfile.TemporaryDirectory() as tmp:
            _home, env = _fake_home_env(tmp)
            result = _run([sys.executable, gui], cwd=tmp, env=env,
                          timeout=60)
            self.assertEqual(result.returncode, 2)
            result = _run([sys.executable, gui, "bogus", "t", "x"],
                          cwd=tmp, env=env, timeout=60)
            self.assertEqual(result.returncode, 2)

    def test_install_gui_cancel_kills_process_group(self):
        # D21: the factored cancel kills the install's whole
        # process group (not just one process).
        gui = _load_module("install_gui",
                           os.path.join(ROOT, "install-gui.py"))
        proc = subprocess.Popen(["sleep", "30"],
                                start_new_session=True)
        try:
            self.assertTrue(
                gui.cancel_install(os.getpgid(proc.pid)))
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------
# A4/C1-C14/D14/D20: the docs say what the code does.
#
# Prose wraps mid-sentence, so these tests read the docs flattened
# (whitespace collapsed) and assert the corrected wording is there
# and the old wrong wording is gone.


def _flat(rel):
    # Collapse whitespace and drop backticks (markdown
    # formatting, not content) so prose assertions survive
    # line wraps and inline code.
    return " ".join(_read(rel).split()).replace("`", "")


class DocsContentTests(unittest.TestCase):
    """The C-item rewordings and the A4 debugging-port section."""

    def test_debugging_port_section_present(self):
        # A4: the engine section documents the debugging port,
        # verbatim from the ENGINE builder.
        text = _flat("BROWSER.md")
        for sentence in (
                "The browser engine's debugging port",
                "listens on 127.0.0.1 only",
                "picked at random for each launch",
                "FORGE_BROWSER_PORT still wins when set",
                "DevToolsActivePort file",
                "The debugging protocol has no login",
                "Do not run programs you do not trust alongside it",
                "falls back to --no-sandbox when a launch fails"):
            self.assertIn(sentence, text)

    def test_no_fixed_9222_port_claim(self):
        # A4 consistency: the port is random per launch, so no
        # part of the manual may still say "port 9222".
        self.assertNotIn("port 9222", _read("BROWSER.md"))

    def test_commands_list_every_subcommand(self):
        # C4: the Commands section names every subcommand cli.py
        # defines — parsed live, so a new subcommand cannot
        # silently go undocumented.
        cli = _read("forge/browser/cli.py")
        names = re.findall(r'add_parser\("([^"]+)"', cli)
        self.assertGreater(len(names), 20,
                           "cli.py lost its subcommands?")
        text = _flat("BROWSER.md")
        for name in names:
            self.assertTrue(
                re.search(r"forge\.browser " + re.escape(name) +
                          r"(?![a-z-])", text),
                f"subcommand {name!r} is not in BROWSER.md")

    def test_stop_interrupts_in_flight(self):
        # C5: cancel() interrupts a streaming generation in
        # flight (chat.py:656-657) — it does not wait for it.
        text = _flat("BROWSER.md")
        self.assertIn("interrupts a brain call already in flight",
                      text)
        self.assertNotIn("finishes first, then it stops", text)

    def test_pdf_bullet_names_ocr(self):
        # C6: scanned PDFs are read with OCR when tesseract is
        # installed (files.py:502-507).
        self.assertIn("read with OCR when tesseract is installed",
                      _flat("BROWSER.md"))

    def test_auto_chain_names_kitesurf_third(self):
        # C7: the auto chain is FORGE_CDP_URL,
        # FORGE_LIGHTPANDA_URL, FORGE_KITESURF_URL, then Chromium
        # (cdp.py:346-347, 380-382).
        text = _flat("BROWSER.md")
        self.assertTrue(re.search(
            r"FORGE_CDP_URL.*FORGE_LIGHTPANDA_URL.*"
            r"FORGE_KITESURF_URL.*Chromium-family", text),
            "the auto chain does not name Kitesurf third")

    def test_reverification_is_install_time(self):
        # C8: the chrome binary is re-verified on every
        # install.sh run, not on every program run.
        for rel in ("BROWSER.md", "engine-pins.txt", "install.sh"):
            text = _flat(rel)
            self.assertNotIn("re-verified by hash on every run",
                             text, rel)
        self.assertIn("on every install.sh run (--check included)",
                      _flat("BROWSER.md"))
        # engine-pins.txt wraps the sentence across comment
        # lines, so the "#" lands between "on" and "every".
        self.assertIn("every install.sh run (--check included)",
                      _flat("engine-pins.txt"))

    def test_research_queues(self):
        # C10: a second research ask queues with a position; it
        # is not turned away.
        text = _flat("BROWSER.md")
        self.assertIn("queued with a position", text)
        self.assertNotIn("is told to wait", text)

    def test_chat_names_all_action_kinds(self):
        # C11: chat has 13 action kinds (chat.py:9-21), not the
        # five the manual used to name.
        text = _flat("BROWSER.md")
        for kind in ("memory (remember, forget, recall",
                     "cards (run a saved workflow card)",
                     "morning brief",
                     "compare (pull the same fields"):
            self.assertIn(kind, text)

    def test_no_kitesurf_port_knob(self):
        # C12: KITESURF_PORT does not exist (cdp.py:56 mentions
        # it only in a comment); FORGE_KITESURF_URL is the knob.
        text = _flat("BROWSER.md")
        self.assertNotIn("KITESURF_PORT", text)
        self.assertIn("FORGE_KITESURF_URL points Forge Browser at it",
                      text)

    def test_improvements_brand_rewordings(self):
        # C2: the listed brand mentions in IMPROVEMENTS.md are
        # reworded to generic descriptions.
        text = _flat("docs/IMPROVEMENTS.md")
        for old in ("Edge/Firefox pattern", "Fellou/Chrome",
                    "Firefox-style", "blog.google", "neowin.net"):
            self.assertNotIn(old, text)
        self.assertNotIn("Fellou", text)
        for new in ("plan-approval safety shape", "browser-style",
                    "Auto Browse (vendor blog)",
                    "GenTabs (vendor blog)",
                    "browser assistant changes (tech press)",
                    "on-device AI (tech press)"):
            self.assertIn(new, text)

    def test_improvements_notes_all_built(self):
        # C9: the proposal file says plainly that all 22 items
        # were built 2026-11-11, and points at TASK-LIST.md.
        text = _flat("docs/IMPROVEMENTS.md")
        self.assertIn("all 22 items below were built 2026-11-11",
                      text)
        self.assertIn("TASK-LIST.md", text)

    def test_deep_audit_names_ollama_size(self):
        # C14: the deep-audit list's install size includes the
        # Ollama download.
        self.assertIn("≈1.2 GB with Ollama present",
                      _flat("docs/DEEP-AUDIT-TASK-LIST.md"))

    def test_clone_path_documented(self):
        # D14: a fresh clone arrives without exec bits, so the
        # README-first step documents the recovery path.
        for rel in ("README.md", "BROWSER.md"):
            text = _flat(rel)
            self.assertIn("restores the exec bits", text, rel)
            self.assertIn('sh "Install Forge Browser"', text, rel)

    def test_c1_features_documented(self):
        # C1: the v0.9 features are in the manual, not only in
        # the changelog.
        text = _flat("BROWSER.md")
        for feature in ("How to Use", "Debug Report",
                        "single instance", "run.sh check",
                        "free CI"):
            self.assertIn(feature, text)

    def test_c2_browser_rewordings(self):
        # C2: BROWSER.md's brand mentions are reworded.
        text = _flat("BROWSER.md")
        self.assertIn("snapshot publisher", text)
        self.assertIn("keyless search backends", text)
        self.assertNotIn("Google publishes no SHA-256", text)
        self.assertNotIn("DuckDuckGo HTML, then Bing HTML", text)

    def test_tests_section_names_current_verification(self):
        # C3: the Tests section carries the current verification
        # date, not the stale 50/50 line. (The count itself is
        # pinned by test_no_stale_check_counts.)
        text = _flat("BROWSER.md")
        self.assertIn("Verified in the sandbox on 2026-11-15", text)
        self.assertNotIn("50/50", text)

    def test_install_header_names_pip_window(self):
        # D20: the engine room is stdlib-only, but the window
        # needs PySide6 from pip — the header says both.
        text = _read("install.sh")
        self.assertIn("the window needs PySide6", text)
        self.assertNotIn("no pip dependencies", text.lower())


# ---------------------------------------------------------------------
# D15/D27: the release verification script, pinned.
#
# verify-install.sh lives outside the tree (it verifies the
# release zip, not the source) and a full run takes ~50 minutes
# with real downloads, so these are static pins on the script's
# contents, not behavioral runs.


class VerifyInstallTests(unittest.TestCase):
    """D15/D27: the release verifier unzips fresh, launches the
    app headless, and kills the installer's process group on
    timeout."""

    RELEASE = os.path.join(os.path.dirname(ROOT),
                           "forge-browser-ai-release",
                           "verify-install.sh")

    def _script(self):
        if not os.path.exists(self.RELEASE):
            self.skipTest("no release dir beside this tree")
        with open(self.RELEASE, encoding="utf-8") as handle:
            return handle.read()

    def test_verify_install_extracts_fresh_zip(self):
        # D15: the script unzips a fresh copy (named by
        # RELEASE_ZIP) and checks the installer is executable.
        text = self._script()
        self.assertIn("RELEASE_ZIP", text)
        self.assertIn('rm -rf "$EXTRACT"', text)
        self.assertIn('unzip -q "$RELEASE_ZIP"', text)
        self.assertIn("not executable after unzip", text)

    def test_verify_install_launches_app_headless(self):
        # D15: after installing, the script launches the app
        # offscreen and waits for its started line.
        text = self._script()
        self.assertIn("QT_QPA_PLATFORM=offscreen", text)
        self.assertIn("Forge Browser started", text)
        self.assertIn("Traceback", text)

    def test_verify_install_kills_process_group_on_timeout(self):
        # D27: the installer runs under setsid, and the timeout
        # path kills the whole group, not just the shell.
        text = self._script()
        self.assertIn("setsid", text)
        self.assertIn("kill -TERM -- -$INSTALLER_PID", text)

    def test_verify_install_exits_nonzero_on_failure(self):
        text = self._script()
        self.assertIn("VERIFY_FAIL", text)
        self.assertIn("VERIFY PASS", text)
        self.assertIn("exit 1", text)


# ---------------------------------------------------------------------
# D26: the guided script kills the Lightpanda it started, even
# when interrupted.


class GuidedTrapTests(unittest.TestCase):
    """D26: interrupting test-forge-browser.sh mid-step-8 must
    not orphan the background Lightpanda."""

    def test_guided_script_names_trap(self):
        text = _read("test-forge-browser.sh")
        self.assertIn("trap cleanup_lightpanda EXIT INT TERM", text)

    def test_interrupt_kills_started_lightpanda(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "tree")
            os.makedirs(tree)
            shutil.copy(os.path.join(ROOT, "test-forge-browser.sh"),
                        os.path.join(tree, "test-forge-browser.sh"))
            home, env = _fake_home_env(tmp)
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            lp_pid_file = os.path.join(tmp, "lp.pid")
            py_pid_file = os.path.join(tmp, "py.pid")
            curl_count = os.path.join(tmp, "curl.count")
            env["PATH"] = bindir + os.pathsep + env["PATH"]
            env["LP_PID_FILE"] = lp_pid_file
            env["PY_PID_FILE"] = py_pid_file
            env["CURL_COUNT"] = curl_count
            lp_dir = os.path.join(home, ".local", "share",
                                  "forge-browser")
            os.makedirs(lp_dir)
            _write_stub(os.path.join(lp_dir, "lightpanda"),
                        "#!/bin/sh\n"
                        "echo $$ > \"$LP_PID_FILE\"\n"
                        "exec sleep 60\n")
            # First probe fails (no server yet, so the script
            # starts one); later probes succeed.
            _write_stub(os.path.join(bindir, "curl"),
                        "#!/bin/sh\n"
                        "n=$(cat \"$CURL_COUNT\" 2>/dev/null || "
                        "echo 0)\n"
                        "n=$((n + 1)); echo \"$n\" > \"$CURL_COUNT\"\n"
                        "[ \"$n\" -ge 2 ]\n")
            # The snapshot call blocks, so the script is
            # mid-step when interrupted. (dash runs a trap only
            # once the foreground command finishes, so the
            # block is short — the trap still fires before the
            # script exits.)
            _write_stub(os.path.join(bindir, "python3"),
                        "#!/bin/sh\n"
                        "echo $$ > \"$PY_PID_FILE\"\n"
                        "sleep 5\n")
            proc = subprocess.Popen(
                ["sh", "test-forge-browser.sh", "--step", "8"],
                cwd=tree, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.time() + 30
                while not os.path.exists(py_pid_file):
                    if proc.poll() is not None:
                        out = proc.stdout.read() if proc.stdout \
                            else ""
                        self.fail("guided script exited before "
                                  "the snapshot step:\n" + out)
                    if time.time() > deadline:
                        self.fail("guided script never reached "
                                  "the snapshot step")
                    time.sleep(0.2)
                with open(lp_pid_file, encoding="utf-8") as handle:
                    lp_pid = int(handle.read().strip())
                proc.terminate()  # SIGTERM — the trap must fire
                proc.wait(timeout=30)
                # The trap killed the Lightpanda it started.
                # Poll: a killed process can linger as a zombie
                # until its parent reaps it, and kill -0 still
                # answers for a zombie.
                deadline = time.time() + 10
                while True:
                    try:
                        os.kill(lp_pid, 0)
                    except ProcessLookupError:
                        break
                    if time.time() > deadline:
                        self.fail("the interrupted script left "
                                  "its Lightpanda running")
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for pid_file in (py_pid_file, lp_pid_file):
                    if os.path.exists(pid_file):
                        with open(pid_file,
                                  encoding="utf-8") as handle:
                            try:
                                pid = int(handle.read().strip())
                            except ValueError:
                                continue
                        try:
                            os.kill(pid, 9)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
