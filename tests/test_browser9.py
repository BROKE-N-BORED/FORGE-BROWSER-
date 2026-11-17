"""Tests for the v0.12 build: silent update check + ask-first
auto-update (check / popup / download+verify / install /
restart prompt / Help menu / docs).

The pure tests run under plain python (no PySide6). The Qt
tests at the bottom run under the offscreen platform when
PySide6 is present and skip otherwise — they share one window
and one local stub server standing in for GitHub.
"""

import hashlib
import http.server
import io
import json
import os
import shutil
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from forge.browser import settings as settingsmod
from forge.browser import updates as updatesmod
from forge.browser.errors import BrowserError

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
    from PySide6 import QtCore, QtTest, QtWidgets
    from PySide6.QtWebEngineCore import QWebEngineProfile
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

if HAVE_QT:
    from forge.browser import app as appmod
    from forge.browser import webui as webuimod


def _tree_path(name):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), name)


# ------------------------------------------------------------------
# Version parsing + is_newer
# ------------------------------------------------------------------

class ParseVersionTests(unittest.TestCase):
    def test_plain_version(self):
        self.assertEqual(updatesmod.parse_version("0.11"), (0, 11))

    def test_leading_v(self):
        self.assertEqual(updatesmod.parse_version("v0.12"), (0, 12))

    def test_three_parts(self):
        self.assertEqual(updatesmod.parse_version("1.2.3"), (1, 2, 3))

    def test_whitespace_is_stripped(self):
        self.assertEqual(updatesmod.parse_version(" 0.11 "),
                         (0, 11))

    def test_prerelease_tag_is_not_a_version(self):
        self.assertIsNone(updatesmod.parse_version("v0.12-beta"))

    def test_empty_and_junk_are_not_versions(self):
        self.assertIsNone(updatesmod.parse_version(""))
        self.assertIsNone(updatesmod.parse_version("abc"))
        self.assertIsNone(updatesmod.parse_version("0.11a"))
        self.assertIsNone(updatesmod.parse_version(None))
        self.assertIsNone(updatesmod.parse_version(11))


class IsNewerTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(updatesmod.is_newer("0.12", "0.11"))

    def test_same_is_not_newer(self):
        self.assertFalse(updatesmod.is_newer("0.11", "0.11"))

    def test_older_is_not_newer(self):
        self.assertFalse(updatesmod.is_newer("0.10", "0.11"))

    def test_trailing_zero_compares_equal(self):
        self.assertFalse(updatesmod.is_newer("0.11.0", "0.11"))
        self.assertTrue(updatesmod.is_newer("0.11.1", "0.11"))

    def test_leading_v_on_either_side(self):
        self.assertTrue(updatesmod.is_newer("v0.12", "0.11"))
        self.assertTrue(updatesmod.is_newer("0.12", "v0.11"))

    def test_unparsable_is_never_newer(self):
        self.assertFalse(updatesmod.is_newer("v0.12-beta", "0.11"))
        self.assertFalse(updatesmod.is_newer("0.12", "dev"))
        self.assertFalse(updatesmod.is_newer(None, "0.11"))


# ------------------------------------------------------------------
# The check, with a stubbed urlopen (no network)
# ------------------------------------------------------------------

class _StubResponse:
    """A urlopen stand-in: serves fixed bytes, acts as a
    context manager, carries headers."""

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_payload(tag="v0.12", with_sidecar=True,
                     with_zip=True):
    assets = []
    if with_zip:
        assets.append({
            "name": "FORGE-BROWSER-AI-v0.12-RELEASE.zip",
            "browser_download_url":
                "https://example.invalid/dl/v012.zip"})
    if with_sidecar and with_zip:
        assets.append({
            "name": "FORGE-BROWSER-AI-v0.12-RELEASE.zip.sha256",
            "browser_download_url":
                "https://example.invalid/dl/v012.zip.sha256"})
    return {"tag_name": tag,
            "html_url": ("https://github.com/BROKE-N-BORED/"
                         "FORGE-BROWSER-/releases/tag/" + tag),
            "assets": assets}


def _json_urlopen(payload, seen=None):
    def opener(url, timeout=None):
        if seen is not None:
            seen.append(url)
        body = json.dumps(payload).encode("utf-8")
        return _StubResponse(body)
    return opener


class CheckOutcomesTests(unittest.TestCase):
    def test_newer_release_carries_the_download(self):
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(_release_payload("v0.12")))
        self.assertEqual(result["state"], "newer")
        self.assertEqual(result["latest"], "0.12")
        self.assertEqual(result["zip_name"],
                         "FORGE-BROWSER-AI-v0.12-RELEASE.zip")
        self.assertTrue(result["zip_url"].endswith("v012.zip"))
        self.assertTrue(
            result["sha256_url"].endswith("v012.zip.sha256"))
        self.assertIn("releases/tag/v0.12", result["url"])

    def test_current_release(self):
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(_release_payload("v0.11")))
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["latest"], "0.11")

    def test_local_version_ahead_is_current(self):
        result = updatesmod.check_for_update(
            "0.12",
            urlopen=_json_urlopen(_release_payload("v0.11")))
        self.assertEqual(result["state"], "current")

    def test_network_down_is_unknown_with_a_plain_reason(self):
        import urllib.error

        def failing(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        result = updatesmod.check_for_update("0.11",
                                             urlopen=failing)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertIn("could not reach GitHub",
                      result["reason"])

    def test_timeout_is_unknown(self):
        def timing_out(url, timeout=None):
            raise TimeoutError("timed out")

        result = updatesmod.check_for_update("0.11",
                                             urlopen=timing_out)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("timed out", result["reason"])

    def test_bad_json_is_unknown(self):
        def bad(url, timeout=None):
            return _StubResponse(b"not json{")

        result = updatesmod.check_for_update("0.11",
                                             urlopen=bad)
        self.assertEqual(result["state"], "unknown")

    def test_missing_tag_is_unknown(self):
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen({"assets": []}))
        self.assertEqual(result["state"], "unknown")
        self.assertIn("did not name a version",
                      result["reason"])

    def test_unparsable_tag_is_unknown(self):
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(
                _release_payload("v0.12-beta")))
        self.assertEqual(result["state"], "unknown")
        self.assertIn("did not look like a version",
                      result["reason"])

    def test_no_zip_asset_is_unknown(self):
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(
                _release_payload("v0.12", with_zip=False)))
        self.assertEqual(result["state"], "unknown")
        self.assertIn("no download file", result["reason"])

    def test_missing_sidecar_still_reports_newer(self):
        # The check succeeds; the DOWNLOAD refuses a release
        # with no sidecar (see DownloadVerifyTests).
        result = updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(
                _release_payload("v0.12", with_sidecar=False)))
        self.assertEqual(result["state"], "newer")
        self.assertIsNone(result["sha256_url"])

    def test_latest_url_override_is_used(self):
        seen = []
        updatesmod.check_for_update(
            "0.11",
            urlopen=_json_urlopen(_release_payload(), seen),
            latest_url="http://127.0.0.1:9/fake")
        self.assertEqual(seen, ["http://127.0.0.1:9/fake"])

    def test_env_override_is_used(self):
        seen = []
        with mock.patch.dict(os.environ, {
                "FORGE_BROWSER_UPDATE_URL":
                    "http://127.0.0.1:9/from-env"}):
            updatesmod.check_for_update(
                "0.11",
                urlopen=_json_urlopen(_release_payload(),
                                      seen))
        self.assertEqual(seen, ["http://127.0.0.1:9/from-env"])

    def test_real_endpoint_is_the_forge_browser_repo(self):
        self.assertIn("BROKE-N-BORED/FORGE-BROWSER-",
                      updatesmod.LATEST_RELEASE_URL)
        self.assertTrue(updatesmod.LATEST_RELEASE_URL.endswith(
            "/releases/latest"))


# ------------------------------------------------------------------
# The checksum sidecar
# ------------------------------------------------------------------

class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-update-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.tmp, name)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(data)
        return path

    def test_parse_sidecar(self):
        digest = "a" * 64
        parsed = updatesmod.parse_sidecar(
            "%s  FORGE-BROWSER-AI-v0.12-RELEASE.zip\n" % digest)
        self.assertEqual(parsed, (digest,
                                  "FORGE-BROWSER-AI-v0.12-RELEASE.zip"))

    def test_parse_sidecar_binary_mode_star(self):
        digest = "b" * 64
        parsed = updatesmod.parse_sidecar(
            "%s *file.zip\n" % digest)
        self.assertEqual(parsed, (digest, "file.zip"))

    def test_parse_sidecar_skips_blank_lines(self):
        digest = "c" * 64
        parsed = updatesmod.parse_sidecar(
            "\n\n%s  file.zip\n" % digest)
        self.assertEqual(parsed, (digest, "file.zip"))

    def test_parse_sidecar_rejects_bad_digest(self):
        self.assertIsNone(updatesmod.parse_sidecar(
            "not-a-hash  file.zip\n"))
        self.assertIsNone(updatesmod.parse_sidecar(""))
        self.assertIsNone(updatesmod.parse_sidecar(None))
        self.assertIsNone(updatesmod.parse_sidecar(
            "a" * 63 + "  file.zip\n"))

    def test_verify_match(self):
        data = b"fake zip bytes"
        zip_path = self._write("update.zip", data)
        digest = hashlib.sha256(data).hexdigest()
        sidecar = self._write("update.zip.sha256",
                              "%s  update.zip\n" % digest)
        self.assertTrue(updatesmod.verify_zip(zip_path, sidecar))

    def test_verify_mismatch_refuses(self):
        zip_path = self._write("update.zip", b"tampered bytes")
        sidecar = self._write(
            "update.zip.sha256",
            "%s  update.zip\n" % ("0" * 64))
        with self.assertRaises(BrowserError) as caught:
            updatesmod.verify_zip(zip_path, sidecar)
        self.assertIn("did not match its published checksum",
                      str(caught.exception))

    def test_verify_wrong_name_refuses(self):
        data = b"fake zip bytes"
        zip_path = self._write("update.zip", data)
        digest = hashlib.sha256(data).hexdigest()
        sidecar = self._write("update.zip.sha256",
                              "%s  other.zip\n" % digest)
        with self.assertRaises(BrowserError) as caught:
            updatesmod.verify_zip(zip_path, sidecar)
        self.assertIn("names other.zip", str(caught.exception))

    def test_verify_unparsable_sidecar_refuses(self):
        zip_path = self._write("update.zip", b"fake zip bytes")
        sidecar = self._write("update.zip.sha256", "junk\n")
        with self.assertRaises(BrowserError):
            updatesmod.verify_zip(zip_path, sidecar)


# ------------------------------------------------------------------
# Download + verify
# ------------------------------------------------------------------

class DownloadVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-update-test-")
        self.zip_bytes = b"PK fake zip content"
        self.digest = hashlib.sha256(self.zip_bytes).hexdigest()
        self.sidecar_bytes = ("%s  v012.zip\n"
                              % self.digest).encode("utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _urlopen(self, files):
        def opener(url, timeout=None):
            if url not in files:
                import urllib.error
                raise urllib.error.URLError("not found: " + url)
            body = files[url]
            return _StubResponse(
                body,
                {"Content-Length": str(len(body))})
        return opener

    def _result(self, sha256_url="https://x.invalid/v012.sha256"):
        return {"state": "newer", "latest": "0.12",
                "zip_url": "https://x.invalid/v012.zip",
                "zip_name": "v012.zip",
                "sha256_url": sha256_url}

    def test_download_and_verify_ok(self):
        files = {"https://x.invalid/v012.zip": self.zip_bytes,
                 "https://x.invalid/v012.sha256": self.sidecar_bytes}
        seen = []
        path = updatesmod.download_and_verify(
            self._result(), self.tmp,
            urlopen=self._urlopen(files),
            on_progress=lambda got, total: seen.append((got, total)))
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), self.zip_bytes)
        self.assertTrue(seen)
        self.assertEqual(seen[-1],
                         (len(self.zip_bytes), len(self.zip_bytes)))

    def test_missing_sidecar_refuses_before_downloading(self):
        files = {"https://x.invalid/v012.zip": self.zip_bytes}
        with self.assertRaises(BrowserError) as caught:
            updatesmod.download_and_verify(
                self._result(sha256_url=None), self.tmp,
                urlopen=self._urlopen(files))
        self.assertIn("no checksum was published",
                      str(caught.exception))
        # Nothing was downloaded at all.
        self.assertEqual(os.listdir(self.tmp), [])

    def test_mismatch_refuses(self):
        files = {"https://x.invalid/v012.zip": b"tampered",
                 "https://x.invalid/v012.sha256": self.sidecar_bytes}
        with self.assertRaises(BrowserError) as caught:
            updatesmod.download_and_verify(
                self._result(), self.tmp,
                urlopen=self._urlopen(files))
        self.assertIn("did not match", str(caught.exception))

    def test_download_failure_removes_the_partial_file(self):
        class FlakyResponse(_StubResponse):
            def read(self, size=-1):
                raise IOError("connection reset")

        def opener(url, timeout=None):
            return FlakyResponse(b"xxx")

        dest = os.path.join(self.tmp, "partial.zip")
        with self.assertRaises(BrowserError):
            updatesmod.download_file("https://x.invalid/f.zip",
                                     dest, urlopen=opener)
        self.assertFalse(os.path.exists(dest))


# ------------------------------------------------------------------
# Install into a fake home
# ------------------------------------------------------------------

def _fake_install_sh(marker, exit_code=0):
    """A stand-in install.sh: writes a marker file in its own
    folder and exits with the given code."""
    return ("#!/bin/sh\n"
            "echo installed > \"%s\"\n"
            "exit %d\n" % (marker, exit_code))


def _make_fake_app(folder, version="0.11", install_exit=0):
    """A fake installed app: run.sh + install.sh + a version
    marker. Returns the folder."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "run.sh"), "w") as handle:
        handle.write("#!/bin/sh\necho run %s\n" % version)
    with open(os.path.join(folder, "install.sh"), "w") as handle:
        handle.write(_fake_install_sh("install-marker.txt",
                                      install_exit))
    with open(os.path.join(folder, "version.txt"), "w") as handle:
        handle.write(version)
    return folder


def _make_fake_zip(path, version="0.12", install_exit=0,
                   top="forge-browser-ai"):
    """A fake release zip holding a Forge Browser tree."""
    with zipfile.ZipFile(path, "w") as archive:
        prefix = (top + "/") if top else ""
        archive.writestr(prefix + "run.sh",
                         "#!/bin/sh\necho run %s\n" % version)
        archive.writestr(prefix + "install.sh",
                         _fake_install_sh("install-marker.txt",
                                          install_exit))
        archive.writestr(prefix + "version.txt", version)
    return path


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="forge-update-home-")
        self.app = updatesmod.app_dir(home=self.home)
        self.work = tempfile.mkdtemp(prefix="forge-update-work-")
        # User data that must never be touched.
        self.forge_dir = os.path.join(self.home, ".forge")
        os.makedirs(self.forge_dir, exist_ok=True)
        with open(os.path.join(self.forge_dir,
                               "browser-vault.json"), "w") as handle:
            handle.write("vault-bytes")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    def _vault_intact(self):
        with open(os.path.join(self.forge_dir,
                               "browser-vault.json")) as handle:
            return handle.read() == "vault-bytes"

    def _version(self):
        with open(os.path.join(self.app, "version.txt")) as handle:
            return handle.read()

    def test_install_swaps_in_the_new_tree(self):
        _make_fake_app(self.app, version="0.11")
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12")
        result = updatesmod.install_update(zip_path,
                                           home=self.home)
        self.assertEqual(result, self.app)
        self.assertEqual(self._version(), "0.12")
        # install.sh ran in the new tree.
        self.assertTrue(os.path.isfile(
            os.path.join(self.app, "install-marker.txt")))
        # No .new/.old leftovers.
        self.assertFalse(os.path.exists(self.app + ".new"))
        self.assertFalse(os.path.exists(self.app + ".old"))
        self.assertTrue(self._vault_intact())

    def test_install_without_install_sh_skips_the_run(self):
        _make_fake_app(self.app, version="0.11")
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12")
        updatesmod.install_update(zip_path, home=self.home,
                                  run_install=False)
        self.assertEqual(self._version(), "0.12")
        self.assertFalse(os.path.isfile(
            os.path.join(self.app, "install-marker.txt")))

    def test_dev_tree_refuses_plainly(self):
        # No installed app at the expected path.
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12")
        with self.assertRaises(BrowserError) as caught:
            updatesmod.install_update(zip_path, home=self.home)
        self.assertIn("not installed", str(caught.exception))
        self.assertFalse(os.path.exists(self.app))

    def test_failed_install_restores_the_old_tree(self):
        _make_fake_app(self.app, version="0.11")
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12",
            install_exit=1)
        with self.assertRaises(BrowserError) as caught:
            updatesmod.install_update(zip_path, home=self.home)
        self.assertIn("did not finish cleanly",
                      str(caught.exception))
        self.assertIn("old version was restored",
                      str(caught.exception))
        # The old tree is back, whole.
        self.assertEqual(self._version(), "0.11")
        self.assertTrue(os.path.isfile(
            os.path.join(self.app, "run.sh")))
        self.assertFalse(os.path.exists(self.app + ".old"))
        self.assertTrue(self._vault_intact())

    def test_bad_zip_leaves_the_old_tree_alone(self):
        _make_fake_app(self.app, version="0.11")
        bad = os.path.join(self.work, "bad.zip")
        with open(bad, "wb") as handle:
            handle.write(b"this is not a zip")
        with self.assertRaises(BrowserError):
            updatesmod.install_update(bad, home=self.home)
        self.assertEqual(self._version(), "0.11")
        self.assertTrue(self._vault_intact())

    def test_zip_without_a_program_refuses(self):
        _make_fake_app(self.app, version="0.11")
        empty = os.path.join(self.work, "empty.zip")
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("forge-browser-ai/readme.txt",
                             "no program here")
        with self.assertRaises(BrowserError) as caught:
            updatesmod.install_update(empty, home=self.home)
        self.assertIn("did not hold a Forge Browser program",
                      str(caught.exception))
        self.assertEqual(self._version(), "0.11")

    def test_flat_zip_without_a_top_folder_installs(self):
        _make_fake_app(self.app, version="0.11")
        zip_path = _make_fake_zip(
            os.path.join(self.work, "flat.zip"), version="0.12",
            top="")
        updatesmod.install_update(zip_path, home=self.home)
        self.assertEqual(self._version(), "0.12")

    def test_working_venv_is_carried_across(self):
        _make_fake_app(self.app, version="0.11")
        venv_bin = os.path.join(self.app, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        fake_python = os.path.join(venv_bin, "python")
        with open(fake_python, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake_python, 0o755)
        with open(os.path.join(self.app, ".venv",
                               "venv-marker.txt"), "w") as handle:
            handle.write("old venv")
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12")
        updatesmod.install_update(zip_path, home=self.home)
        self.assertTrue(os.path.isfile(
            os.path.join(self.app, ".venv", "venv-marker.txt")))

    def test_broken_venv_is_left_behind(self):
        _make_fake_app(self.app, version="0.11")
        venv_bin = os.path.join(self.app, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        fake_python = os.path.join(venv_bin, "python")
        with open(fake_python, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake_python, 0o755)
        zip_path = _make_fake_zip(
            os.path.join(self.work, "v012.zip"), version="0.12")
        updatesmod.install_update(zip_path, home=self.home)
        self.assertFalse(os.path.exists(
            os.path.join(self.app, ".venv")))


# ------------------------------------------------------------------
# Relaunch
# ------------------------------------------------------------------

class RelaunchTests(unittest.TestCase):
    def test_relaunch_command_waits_for_the_old_pid(self):
        cmd = updatesmod.relaunch_command("/home/u/.local/share/"
                                          "forge-browser-app",
                                          pid=1234)
        self.assertEqual(cmd[0], "sh")
        self.assertEqual(cmd[1], "-c")
        self.assertIn("kill -0 1234", cmd[2])
        self.assertIn("exec sh ", cmd[2])
        self.assertIn("forge-browser-app/run.sh", cmd[2])

    def test_relaunch_command_quotes_the_path(self):
        cmd = updatesmod.relaunch_command("/home/u/my app",
                                          pid=99)
        self.assertIn("'/home/u/my app/run.sh'", cmd[2])

    def test_relaunch_spawns_detached(self):
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return "proc"

        result = updatesmod.relaunch("/app", pid=4242,
                                     _popen=fake_popen)
        self.assertEqual(result, "proc")
        self.assertTrue(seen["kwargs"]["start_new_session"])
        self.assertEqual(seen["kwargs"]["stdout"],
                         subprocess.DEVNULL)

    def test_relaunch_waits_then_runs(self):
        # A real subprocess harness: the old process is a
        # short sleep; the relaunch must not run the new
        # run.sh until it has exited.
        tmp = tempfile.mkdtemp(prefix="forge-relaunch-")
        try:
            app = os.path.join(tmp, "forge-browser-app")
            os.makedirs(app, exist_ok=True)
            marker = os.path.join(tmp, "relaunched.txt")
            with open(os.path.join(app, "run.sh"), "w") as handle:
                handle.write("#!/bin/sh\necho new > %s\n"
                             % marker)
            old = subprocess.Popen(["sleep", "2"])
            updatesmod.relaunch(app, pid=old.pid)
            # While the old process lives, nothing relaunched.
            time.sleep(0.5)
            self.assertFalse(os.path.exists(marker))
            old.wait(timeout=5)
            end = time.time() + 5
            while not os.path.exists(marker) \
                    and time.time() < end:
                time.sleep(0.1)
            self.assertTrue(os.path.exists(marker),
                            "run.sh did not run after the old "
                            "process exited")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------
# The startup toggle setting
# ------------------------------------------------------------------

class StartupToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-settings-")
        self.path = os.path.join(self.tmp, "settings.json")
        self._patch = mock.patch.dict(os.environ, {
            "FORGE_BROWSER_SETTINGS_PATH": self.path})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_on(self):
        self.assertTrue(updatesmod.startup_check_enabled())
        self.assertEqual(settingsmod.get_setting("check_updates"),
                         "1")

    def test_off_means_off(self):
        settingsmod.set_setting("check_updates", "0")
        self.assertFalse(updatesmod.startup_check_enabled())

    def test_on_again(self):
        settingsmod.set_setting("check_updates", "0")
        settingsmod.set_setting("check_updates", "1")
        self.assertTrue(updatesmod.startup_check_enabled())


# ------------------------------------------------------------------
# Docs + wiring (source-text pins)
# ------------------------------------------------------------------

class DocsAndWiringTests(unittest.TestCase):
    def test_browser_md_documents_the_update_flow(self):
        with open(_tree_path("BROWSER.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("## Updates", text)
        for needle in ("silent", "ask", "checksum",
                       "restart", "Check for updates"):
            self.assertIn(needle, text)

    def test_run_app_schedules_the_launch_check(self):
        with open(_tree_path("forge/browser/app.py"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("_maybe_check_for_updates_on_startup",
                      text)
        self.assertIn("singleShot", text)

    def test_no_em_dashes_in_user_visible_update_text(self):
        # Docstrings and comments carry em-dashes all over this
        # tree (the U33 rule is about text the user sees), so
        # this pins the surfaces the user actually reads: the
        # Updates section of the manual (How to Use renders
        # it), and every error message the updater can show.
        with open(_tree_path("BROWSER.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("## Updates")
        end = text.index("## ", start + 1)
        self.assertNotIn("—", text[start:end],
                         "the Updates section carries an em-dash")
        messages = []
        tmp = tempfile.mkdtemp(prefix="forge-update-emdash-")
        try:
            zip_path = os.path.join(tmp, "u.zip")
            with open(zip_path, "wb") as handle:
                handle.write(b"tampered")
            sidecar = os.path.join(tmp, "u.zip.sha256")
            with open(sidecar, "w") as handle:
                handle.write("%s  u.zip\n" % ("0" * 64))
            for call in (
                    lambda: updatesmod.verify_zip(zip_path,
                                                  sidecar),
                    lambda: updatesmod.download_and_verify(
                        {"zip_url": "https://x.invalid/u.zip",
                         "zip_name": "u.zip",
                         "sha256_url": None}, tmp),
                    lambda: updatesmod.install_update(
                        zip_path, home=tmp)):
                try:
                    call()
                except BrowserError as exc:
                    messages.append(str(exc))
                else:
                    self.fail("expected BrowserError")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(messages), 3)
        for message in messages:
            self.assertNotIn("—", message,
                             "updater error carries an em-dash: "
                             "%s" % message)


# ------------------------------------------------------------------
# Qt: the real window against a local stub GitHub
# ------------------------------------------------------------------

if HAVE_QT:
    _STUB = {
        "tag": "v0.12",
        "zip_bytes": b"",
        "sidecar_bytes": b"",
        "with_sidecar": True,
        "fail_latest": False,
        "zip_name": "FORGE-BROWSER-AI-v0.12-RELEASE.zip",
    }

    def _stub_zip_bytes(version="0.12", install_exit=0):
        """A fake release zip as bytes: a Forge Browser tree
        with a stub install.sh."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("forge-browser-ai/run.sh",
                             "#!/bin/sh\necho run %s\n" % version)
            archive.writestr(
                "forge-browser-ai/install.sh",
                _fake_install_sh("install-marker.txt",
                                 install_exit))
            archive.writestr("forge-browser-ai/version.txt",
                             version)
        return buf.getvalue()

    def _stub_reset(tag="v0.12", install_exit=0, tamper=False,
                    with_sidecar=True, fail_latest=False):
        zip_bytes = _stub_zip_bytes("0.12", install_exit)
        digest = hashlib.sha256(zip_bytes).hexdigest()
        if tamper:
            zip_bytes = zip_bytes[:-4] + b"XXXX"
        _STUB.update({
            "tag": tag,
            "zip_bytes": zip_bytes,
            "sidecar_bytes": ("%s  %s\n"
                              % (digest,
                                 _STUB["zip_name"])).encode(),
            "with_sidecar": with_sidecar,
            "fail_latest": fail_latest,
        })

    class _StubGitHubHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/releases/latest":
                if _STUB["fail_latest"]:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                base = "http://127.0.0.1:%d" % self.server.port
                assets = [{
                    "name": _STUB["zip_name"],
                    "browser_download_url":
                        base + "/dl/" + _STUB["zip_name"]}]
                if _STUB["with_sidecar"]:
                    assets.append({
                        "name": _STUB["zip_name"] + ".sha256",
                        "browser_download_url":
                            base + "/dl/" + _STUB["zip_name"]
                            + ".sha256"})
                body = json.dumps({
                    "tag_name": _STUB["tag"],
                    "html_url": ("https://github.com/"
                                 "BROKE-N-BORED/FORGE-BROWSER-/"
                                 "releases/tag/" + _STUB["tag"]),
                    "assets": assets}).encode("utf-8")
                self._send(body, "application/json")
                return
            if self.path == "/dl/" + _STUB["zip_name"]:
                self._send(_STUB["zip_bytes"],
                           "application/zip")
                return
            if self.path == "/dl/" + _STUB["zip_name"] + ".sha256":
                self._send(_STUB["sidecar_bytes"], "text/plain")
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class _StubServer(socketserver.ThreadingMixIn,
                      http.server.HTTPServer):
        daemon_threads = True

    def _modal_dialog():
        return QtWidgets.QApplication.activeModalWidget()

    def _button(parent, text):
        for button in parent.findChildren(QtWidgets.QPushButton):
            if button.text().replace("&", "") == text:
                return button
        return None

    def _message_box_with(title):
        dialog = _modal_dialog()
        if isinstance(dialog, QtWidgets.QMessageBox) \
                and dialog.windowTitle() == title:
            return dialog
        return None

    class _DialogDriver:
        """A QTimer that runs steps against a modal dialog
        while the test blocks in exec() (same shape as the
        v0.11 Qt tests). Each step returns True when done."""

        def __init__(self, steps, timeout=20.0):
            self.steps = steps
            self.index = 0
            self.errors = []
            self.deadline = time.time() + timeout
            self.timer = QtCore.QTimer()
            self.timer.setInterval(100)
            self.timer.timeout.connect(self._tick)

        def start(self):
            self.timer.start()

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
                widget = _modal_dialog()
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
                self.errors.append("step %d: %r"
                                   % (self.index, exc))
                self.index += 1


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class QtUpdateTests(unittest.TestCase):
    window = None
    server = None
    app = None
    home = None
    stub = None
    stub_thread = None
    _patches = []

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="forge-qt-home-v12-")
        cls._patches = [
            mock.patch.dict(os.environ, {"HOME": cls.home}),
        ]
        for patch in cls._patches:
            patch.start()
        # The stub GitHub: the window's check reads the
        # endpoint override, so no real network is touched.
        cls.stub = _StubServer(("127.0.0.1", 0),
                               _StubGitHubHandler)
        cls.stub.port = cls.stub.server_address[1]
        cls.stub_thread = threading.Thread(
            target=cls.stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        cls._env_patch = mock.patch.dict(os.environ, {
            "FORGE_BROWSER_UPDATE_URL":
                "http://127.0.0.1:%d/releases/latest"
                % cls.stub.port})
        cls._env_patch.start()
        _stub_reset()
        cls._install_fake_app()
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication([])
        cls.app = app
        cls.server = webuimod.LocalPageServer().start()
        profile = QWebEngineProfile("forge-qt-tests-v12")
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
        if cls.stub is not None:
            cls.stub.shutdown()
            cls.stub.server_close()
        cls._env_patch.stop()
        for patch in cls._patches:
            patch.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def _install_fake_app(cls):
        """The fake installed app at 0.11 in the fake home."""
        folder = updatesmod.app_dir(home=cls.home)
        shutil.rmtree(folder, ignore_errors=True)
        _make_fake_app(folder, version="0.11")

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
        _stub_reset()
        self._install_fake_app()
        settingsmod.set_setting("check_updates", "1")
        if self.window._update_toggle_action is not None:
            self.window._update_toggle_action.setChecked(True)
        self.window._update_busy = False
        self.window._update_check_running = False
        self._pump(0.2)

    def _drive(self, opener, steps, timeout=20.0):
        """Run an opener under a driver, pumping the event
        loop until every step has run. The opener only blocks
        while a modal dialog is up — the update flow returns
        after starting the download, and the restart prompt
        arrives later via the worker thread, so the pump must
        outlive the opener."""
        driver = _DialogDriver(steps, timeout=timeout)
        driver.start()
        try:
            opener()
            end = time.time() + timeout
            while driver.index < len(driver.steps) \
                    and not driver.errors \
                    and time.time() < end:
                self.app.processEvents()
                time.sleep(0.05)
        finally:
            driver.stop()
        self.assertEqual(driver.errors, [])
        self.assertEqual(driver.index, len(driver.steps),
                         "not all driver steps ran")

    def _help_menu(self):
        for action in self.window.menuBar().actions():
            if action.text().replace("&", "") == "Help":
                return action.menu()
        return None

    def _help_action(self, text):
        menu = self._help_menu()
        self.assertIsNotNone(menu)
        for action in menu.actions():
            if action.text().replace("&", "") == text:
                return action
        return None

    def _installed_version(self):
        path = os.path.join(
            updatesmod.app_dir(home=self.home), "version.txt")
        with open(path) as handle:
            return handle.read()

    def _click_button(self, title, text):
        """A driver step: click a button on the titled modal
        once it is up. Returns True when clicked."""
        def step():
            dialog = _message_box_with(title)
            if dialog is None:
                return False
            button = _button(dialog, text)
            if button is None:
                return False
            button.click()
            return True
        return step

    def _modal_text_is(self, title, needle):
        """A driver step: the titled modal is up and its text
        carries the needle."""
        def step():
            dialog = _message_box_with(title)
            if dialog is None:
                return False
            return needle in dialog.text()
        return step

    # -- Help menu ------------------------------------------------

    def test_help_menu_has_update_actions(self):
        check = self._help_action("Check for updates")
        self.assertIsNotNone(check)
        self.assertFalse(check.isCheckable())
        toggle = self._help_action("Check for updates on startup")
        self.assertIsNotNone(toggle)
        self.assertTrue(toggle.isCheckable())
        self.assertTrue(toggle.isChecked())

    def test_toggle_flips_the_setting(self):
        toggle = self._help_action("Check for updates on startup")
        toggle.trigger()
        self.assertEqual(settingsmod.get_setting("check_updates"),
                         "0")
        self.assertFalse(toggle.isChecked())
        toggle.trigger()
        self.assertEqual(settingsmod.get_setting("check_updates"),
                         "1")
        self.assertTrue(toggle.isChecked())

    # -- Manual check ---------------------------------------------

    def test_manual_check_current_reports_up_to_date(self):
        _stub_reset(tag="v0.11")
        self._drive(
            self.window._on_check_updates,
            [self._modal_text_is(
                "No updates", "You have the newest version"),
             self._click_button("No updates", "OK")])

    def test_manual_check_unknown_reports_the_reason(self):
        _stub_reset(fail_latest=True)
        self._drive(
            self.window._on_check_updates,
            [self._modal_text_is(
                "Could not check for updates",
                "could not reach GitHub"),
             self._click_button("Could not check for updates",
                                "OK")])

    def test_manual_check_newer_offers_the_update(self):
        self._drive(
            self.window._on_check_updates,
            [self._modal_text_is(
                "Update available",
                "Forge Browser 0.12 is out. You have 0.11."),
             self._click_button("Update available", "No")])
        self.assertFalse(self.window._update_busy)
        self.assertIsNone(self.window._update_progress_dialog)

    # -- Silent launch check --------------------------------------

    def test_silent_check_current_shows_nothing(self):
        _stub_reset(tag="v0.11")
        self.window._maybe_check_for_updates_on_startup()
        self.assertTrue(self._wait_for(
            lambda: not self.window._update_check_running,
            timeout=10.0))
        self._pump(0.5)
        self.assertIsNone(_modal_dialog())

    def test_silent_check_unknown_shows_nothing(self):
        _stub_reset(fail_latest=True)
        self.window._maybe_check_for_updates_on_startup()
        self.assertTrue(self._wait_for(
            lambda: not self.window._update_check_running,
            timeout=10.0))
        self._pump(0.5)
        self.assertIsNone(_modal_dialog())

    def test_silent_check_off_makes_no_request(self):
        settingsmod.set_setting("check_updates", "0")
        with mock.patch.object(
                updatesmod, "check_for_update") as mocked:
            self.window._maybe_check_for_updates_on_startup()
            self._pump(0.5)
            mocked.assert_not_called()

    # -- The full update flow -------------------------------------

    def _check_result(self):
        from forge.browser import __version__
        return updatesmod.check_for_update(__version__)

    def test_update_yes_installs_and_restart_no_keeps_running(self):
        result = self._check_result()
        self.assertEqual(result["state"], "newer")
        self._drive(
            lambda: self.window._offer_update(result),
            [self._click_button("Update available", "Yes"),
             self._modal_text_is(
                 "Update installed",
                 "Forge Browser 0.12 is installed."),
             self._click_button("Update installed", "No")],
            timeout=30.0)
        self.assertEqual(self._installed_version(), "0.12")
        self.assertTrue(os.path.isfile(os.path.join(
            updatesmod.app_dir(home=self.home),
            "install-marker.txt")))
        # No restart: the old window is still up.
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._update_busy)

    def test_update_yes_restart_yes_relaunches(self):
        result = self._check_result()
        relaunched = {}
        mock_app = mock.Mock()

        def fake_relaunch(folder):
            relaunched["folder"] = folder

        with mock.patch.object(updatesmod, "relaunch",
                               side_effect=fake_relaunch), \
                mock.patch.object(QtWidgets.QApplication,
                                  "instance",
                                  return_value=mock_app):
            self._drive(
                lambda: self.window._offer_update(result),
                [self._click_button("Update available", "Yes"),
                 self._click_button("Update installed", "Yes")],
                timeout=30.0)
        self.assertEqual(self._installed_version(), "0.12")
        self.assertEqual(relaunched.get("folder"),
                         updatesmod.app_dir(home=self.home))
        mock_app.quit.assert_called_once()

    def test_hash_mismatch_refuses_and_keeps_old_version(self):
        _stub_reset(tamper=True)
        result = self._check_result()
        self.assertEqual(result["state"], "newer")
        self._drive(
            lambda: self.window._offer_update(result),
            [self._click_button("Update available", "Yes"),
             self._modal_text_is(
                 "The update did not install",
                 "did not match its published checksum"),
             self._click_button("The update did not install",
                                "OK")],
            timeout=30.0)
        self.assertEqual(self._installed_version(), "0.11")
        self.assertFalse(self.window._update_busy)

    def test_missing_sidecar_refuses(self):
        _stub_reset(with_sidecar=False)
        result = self._check_result()
        self.assertEqual(result["state"], "newer")
        self.assertIsNone(result["sha256_url"])
        self._drive(
            lambda: self.window._offer_update(result),
            [self._click_button("Update available", "Yes"),
             self._modal_text_is(
                 "The update did not install",
                 "no checksum was published"),
             self._click_button("The update did not install",
                                "OK")],
            timeout=30.0)
        self.assertEqual(self._installed_version(), "0.11")

    def test_dev_tree_refuses(self):
        folder = updatesmod.app_dir(home=self.home)
        shutil.rmtree(folder)
        result = self._check_result()
        self._drive(
            lambda: self.window._offer_update(result),
            [self._click_button("Update available", "Yes"),
             self._modal_text_is(
                 "The update did not install",
                 "not installed"),
             self._click_button("The update did not install",
                                "OK")],
            timeout=30.0)
        self.assertFalse(os.path.exists(folder))

    def test_failed_install_restores_and_reports(self):
        _stub_reset(install_exit=1)
        result = self._check_result()
        self._drive(
            lambda: self.window._offer_update(result),
            [self._click_button("Update available", "Yes"),
             self._modal_text_is(
                 "The update did not install",
                 "did not finish cleanly"),
             self._click_button("The update did not install",
                                "OK")],
            timeout=30.0)
        self.assertEqual(self._installed_version(), "0.11")
