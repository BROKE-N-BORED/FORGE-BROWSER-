"""Silent update check + ask-first auto-update (v0.12).

On launch, the app quietly asks GitHub's public releases
endpoint for the latest Forge Browser release and compares its
tag against the running version. The request is a plain GET to
a public endpoint — it sends no identifying information and
needs no token. When the running version is current, or the
check itself fails, nothing happens at all: no popup, no
noise. When a newer release is out, the app asks first, and
only the user's tap starts the download.

The download is never installed blind. Every release from
v0.12 on carries a checksum sidecar next to the release zip:
an asset named <zip>.sha256 holding the zip's SHA-256 in
sha256sum format ("<hex>  <zip basename>"). The updater finds
both assets in the release's asset list, downloads both, and
verifies the zip against the sidecar before anything is
installed. A missing sidecar, or a hash that does not match,
refuses the update in plain words and keeps the old version.

The install reuses the double-click installer's safe swap:
the verified zip is extracted, the new tree is moved into
~/.local/share/forge-browser-app with the old tree moved
aside (not deleted) until the new install is verified, the
old venv is carried across when it still imports, and the new
tree's install.sh runs to refresh everything. A failure at
any step restores the old tree. User data under ~/.forge/ is
never touched. When no installed app exists at the expected
path (a dev tree), the install refuses instead of guessing.

One honest limit: the running process is the old code until
it exits. After an install with no restart yet, the old
window keeps working, and the new version runs on the next
launch. A module the old process has not loaded yet would
resolve to the new tree after the swap, so restarting soon
is the clean path — the restart prompt says so.

FORGE_BROWSER_UPDATE_URL overrides the releases endpoint
(for testing against a local stub server; the app never
sets it).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from .errors import BrowserError

LATEST_RELEASE_URL = ("https://api.github.com/repos/BROKE-N-BORED/"
                      "FORGE-BROWSER-/releases/latest")
RELEASES_PAGE_URL = ("https://github.com/BROKE-N-BORED/"
                     "FORGE-BROWSER-/releases")
CHECK_TIMEOUT_S = 8
DOWNLOAD_TIMEOUT_S = 30
INSTALL_TIMEOUT_S = 3600
APP_DIR_SUFFIX = (".local", "share", "forge-browser-app")

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_version(text):
    """A version string as a tuple of ints — "0.11" becomes
    (0, 11), "v0.11" the same — or None when it is not a
    version at all. Anything but an optional "v" followed by
    numbers and dots is refused, so a tag like "v0.12-beta"
    reads as no usable version rather than a wrong one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current):
    """True when the latest version is newer than the running
    one. Either side unparsable means no — an unknown version
    is never reported as an update. Shorter tuples are padded
    with zeros, so 0.11 and 0.11.0 compare equal."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    width = max(len(latest_parsed), len(current_parsed))
    latest_padded = latest_parsed + (0,) * (width - len(latest_parsed))
    current_padded = current_parsed + (0,) * (width - len(current_parsed))
    return latest_padded > current_padded


def latest_release_url():
    """The releases endpoint: the override when set, else the
    real GitHub one."""
    override = os.environ.get("FORGE_BROWSER_UPDATE_URL", "").strip()
    return override or LATEST_RELEASE_URL


def _asset_urls(payload):
    """The release zip + checksum sidecar URLs from a release
    payload's asset list: (zip_url, zip_name, sha256_url).
    The zip is the first asset whose name ends in .zip; the
    sidecar is the asset named <zip name>.sha256. A missing
    zip raises; a missing sidecar comes back as None — the
    download step refuses it, never the check."""
    assets = payload.get("assets") or []
    zip_url = None
    zip_name = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name") or ""
        if name.endswith(".zip") and not name.endswith(".sha256"):
            zip_url = asset.get("browser_download_url") or None
            zip_name = name
            break
    if not zip_url or not zip_name:
        raise BrowserError(
            "the latest release has no download file")
    sha256_url = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if asset.get("name") == zip_name + ".sha256":
            sha256_url = asset.get("browser_download_url") or None
            break
    return zip_url, zip_name, sha256_url


def fetch_latest_release(urlopen=None, latest_url=None):
    """The latest release from GitHub's public endpoint, as
    {"version", "url", "zip_url", "zip_name", "sha256_url"}.
    Raises BrowserError with a plain reason when the network
    fails, the answer is not JSON, it carries no tag, or it
    has no download file: the caller turns every one of those
    into "unknown", never a crash. urlopen is a test seam —
    pass a stub and no network is touched."""
    opener = urlopen or urllib.request.urlopen
    url = latest_url or latest_release_url()
    try:
        with opener(url, timeout=CHECK_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise BrowserError("the update check timed out")
        raise BrowserError(
            "could not reach GitHub: %s" % reason)
    except TimeoutError:
        raise BrowserError("the update check timed out")
    except (ValueError, UnicodeDecodeError):
        raise BrowserError(
            "the releases page did not answer in a form "
            "Forge Browser understands")
    if not isinstance(payload, dict):
        raise BrowserError(
            "the releases page did not answer in a form "
            "Forge Browser understands")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise BrowserError(
            "the releases page did not name a version")
    version = parse_version(tag)
    if version is None:
        raise BrowserError(
            "the latest release tag did not look like a version: %s"
            % tag)
    zip_url, zip_name, sha256_url = _asset_urls(payload)
    url = payload.get("html_url") or RELEASES_PAGE_URL
    return {"version": ".".join(str(part) for part in version),
            "url": url,
            "zip_url": zip_url,
            "zip_name": zip_name,
            "sha256_url": sha256_url}


def check_for_update(current_version, urlopen=None, latest_url=None):
    """One call, three outcomes, never an exception for a
    network or parsing problem:

    {"state": "newer", "latest": "0.12", "url": "<release
     page>", "zip_url": ..., "zip_name": ..., "sha256_url":
     ...} — a newer release is out, with its download.
    {"state": "current", ...} — the running version is the
     newest (or newer than the latest release, a dev tree).
    {"state": "unknown", "latest": None, "url": None,
     "reason": "<plain words>"} — the check could not run.
    """
    try:
        release = fetch_latest_release(urlopen=urlopen,
                                       latest_url=latest_url)
    except BrowserError as exc:
        return {"state": "unknown", "latest": None, "url": None,
                "reason": str(exc)}
    result = {"latest": release["version"],
              "url": release["url"],
              "zip_url": release["zip_url"],
              "zip_name": release["zip_name"],
              "sha256_url": release["sha256_url"]}
    if is_newer(release["version"], current_version):
        result["state"] = "newer"
    else:
        result["state"] = "current"
    return result


def startup_check_enabled():
    """The Help menu toggle: on by default, off only when the
    saved setting says "0"."""
    from .settings import get_setting
    return get_setting("check_updates") != "0"


def download_file(url, dest, urlopen=None, on_progress=None,
                  timeout=DOWNLOAD_TIMEOUT_S):
    """Download a URL to a file. on_progress(got, total) is
    called after each chunk; total is -1 when the server did
    not say how big the file is. Raises BrowserError with a
    plain reason on failure, and removes a partial file."""
    opener = urlopen or urllib.request.urlopen
    try:
        response = opener(url, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise BrowserError("the download timed out")
        raise BrowserError("could not reach GitHub: %s" % reason)
    except TimeoutError:
        raise BrowserError("the download timed out")
    total = -1
    try:
        total = int(response.headers.get("Content-Length", -1))
    except (AttributeError, TypeError, ValueError):
        total = -1
    got = 0
    try:
        with response:
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    got += len(chunk)
                    if on_progress is not None:
                        on_progress(got, total)
    except (OSError, urllib.error.URLError) as exc:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise BrowserError("the download failed: %s" % exc)
    return dest


def parse_sidecar(text):
    """A sha256sum sidecar as (hex digest, file name) — the
    first non-empty line, split on whitespace — or None when
    it does not parse. The digest must be 64 hex characters;
    the name is whatever follows it (empty when absent)."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        digest = parts[0].lower()
        if not _SHA256_RE.match(digest):
            return None
        name = parts[1].strip() if len(parts) > 1 else ""
        # sha256sum writes binary-mode names with a leading *.
        if name.startswith("*"):
            name = name[1:]
        return digest, name
    return None


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_zip(zip_path, sidecar_path):
    """Check a downloaded zip against its checksum sidecar.
    Returns True on a match. Raises BrowserError — plain
    words, nothing installed — when the sidecar does not
    parse, names a different file, or the hash does not
    match."""
    try:
        with open(sidecar_path, encoding="utf-8") as handle:
            parsed = parse_sidecar(handle.read())
    except OSError as exc:
        raise BrowserError(
            "the checksum file could not be read: %s" % exc)
    if parsed is None:
        raise BrowserError(
            "the checksum file did not look like a checksum")
    digest, name = parsed
    if name and name != os.path.basename(zip_path):
        raise BrowserError(
            "the checksum file names %s, not this download"
            % name)
    try:
        actual = sha256_of_file(zip_path)
    except OSError as exc:
        raise BrowserError(
            "the download could not be read back: %s" % exc)
    if actual != digest:
        raise BrowserError(
            "the download did not match its published "
            "checksum, so it was not installed")
    return True


def download_and_verify(result, work_dir, urlopen=None,
                        on_progress=None):
    """Download the release zip + its checksum sidecar and
    verify the zip against the sidecar. Returns the zip path.
    A release with no sidecar asset refuses before anything
    is downloaded; a hash mismatch refuses after. Raises
    BrowserError with a plain reason either way."""
    zip_url = result.get("zip_url")
    zip_name = result.get("zip_name") or "forge-browser-update.zip"
    sha256_url = result.get("sha256_url")
    if not zip_url:
        raise BrowserError("this release has no download file")
    if not sha256_url:
        raise BrowserError(
            "no checksum was published for this release, so "
            "the download cannot be checked. Nothing was "
            "installed")
    os.makedirs(work_dir, exist_ok=True)
    zip_path = os.path.join(work_dir, zip_name)
    sidecar_path = os.path.join(work_dir, zip_name + ".sha256")
    download_file(zip_url, zip_path, urlopen=urlopen,
                  on_progress=on_progress)
    download_file(sha256_url, sidecar_path, urlopen=urlopen)
    verify_zip(zip_path, sidecar_path)
    return zip_path


def app_dir(home=None):
    """The installed app's folder."""
    base = home or os.path.expanduser("~")
    return os.path.join(base, *APP_DIR_SUFFIX)


def find_installed_app(home=None):
    """The installed app's folder when one is really there
    (run.sh + install.sh present), else None. A folder
    without those files is not an install — the updater
    never guesses."""
    folder = app_dir(home=home)
    if (os.path.isdir(folder)
            and os.path.isfile(os.path.join(folder, "run.sh"))
            and os.path.isfile(os.path.join(folder, "install.sh"))):
        return folder
    return None


def _tree_root(extracted):
    """The extracted release tree: the single top-level
    folder when the zip has one (forge-browser-ai), else the
    extraction folder itself."""
    entries = [entry for entry in os.listdir(extracted)
               if not entry.startswith(".")]
    if len(entries) == 1:
        only = os.path.join(extracted, entries[0])
        if os.path.isdir(only):
            return only
    return extracted


def _venv_imports(venv_python, timeout=60):
    """True when the given venv python imports the window
    kit. A missing binary, a timeout, or a failed import all
    mean no — the installer rebuilds it instead."""
    if not os.path.isfile(venv_python):
        return False
    try:
        proc = subprocess.run(
            [venv_python, "-c",
             "import PySide6.QtWebEngineWidgets"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _restore_old(app, old):
    """Put the old tree back after a failed update. Returns
    True when the old install is back in place."""
    try:
        shutil.rmtree(app, ignore_errors=True)
        if os.path.isdir(old):
            os.rename(old, app)
            return True
    except OSError:
        pass
    return False


def install_update(zip_path, home=None, run_install=True,
                   install_timeout=INSTALL_TIMEOUT_S):
    """Install a verified release zip over the installed app.
    Returns the installed app's folder. The swap keeps the
    old tree aside until the new install is verified, and
    restores it on any failure. Raises BrowserError with a
    plain reason when there is no installed app, the zip
    does not hold a Forge Browser tree, the install fails,
    or the new window part does not verify."""
    folder = find_installed_app(home=home)
    if folder is None:
        raise BrowserError(
            "this copy of Forge Browser is not installed "
            "under ~/.local/share/forge-browser-app, so the "
            "update has nowhere to install. Install it from "
            "a release zip first, or update by hand")
    new = folder + ".new"
    old = folder + ".old"
    work = tempfile.mkdtemp(prefix="forge-update-")
    try:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(work)
        except (OSError, zipfile.BadZipFile) as exc:
            raise BrowserError(
                "the downloaded update did not open: %s" % exc)
        root = _tree_root(work)
        if (not os.path.isfile(os.path.join(root, "run.sh"))
                or not os.path.isfile(
                    os.path.join(root, "install.sh"))):
            raise BrowserError(
                "the downloaded update did not hold a Forge "
                "Browser program")
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        try:
            shutil.copytree(root, new)
        except OSError as exc:
            raise BrowserError(
                "the update could not be staged: %s" % exc)
        # The swap: the old tree moves aside (not deleted),
        # the new one moves in. A failed move-in restores
        # the old tree at once.
        try:
            os.rename(folder, old)
            try:
                os.rename(new, folder)
            except OSError:
                os.rename(old, folder)
                raise BrowserError(
                    "the update could not be moved into "
                    "place. The old version was restored")
        except BrowserError:
            raise
        except OSError as exc:
            raise BrowserError(
                "the update could not be moved into place: %s"
                % exc)
        # Carry the old venv across when it still imports —
        # it returns to the exact path it was built for, so
        # its absolute paths are right again, and install.sh
        # then skips the rebuild. A venv that no longer
        # imports is left behind and rebuilt.
        old_venv = os.path.join(old, ".venv", "bin", "python")
        if _venv_imports(old_venv):
            try:
                shutil.copytree(os.path.join(old, ".venv"),
                                os.path.join(folder, ".venv"))
            except OSError:
                pass  # install.sh rebuilds it
        if run_install:
            try:
                proc = subprocess.run(
                    ["sh", "install.sh"], cwd=folder,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=install_timeout)
            except subprocess.TimeoutExpired:
                _restore_old(folder, old)
                raise BrowserError(
                    "the update install ran past its time "
                    "limit and was stopped. The old version "
                    "was restored")
            except OSError as exc:
                _restore_old(folder, old)
                raise BrowserError(
                    "the update install could not start (%s) "
                    ". The old version was restored" % exc)
            if proc.returncode != 0:
                _restore_old(folder, old)
                tail = ""
                try:
                    lines = proc.stdout.decode(
                        "utf-8", "replace").splitlines()
                    tail = lines[-1].strip() if lines else ""
                except (AttributeError, UnicodeDecodeError):
                    tail = ""
                detail = (": %s" % tail) if tail else ""
                raise BrowserError(
                    "the update install did not finish "
                    "cleanly%s. The old version was restored"
                    % detail)
        # Verify: the new tree must hold the program, and a
        # venv that exists must import the window kit.
        venv_python = os.path.join(folder, ".venv", "bin",
                                   "python")
        if (not os.path.isfile(os.path.join(folder, "run.sh"))
                or not os.path.isfile(
                    os.path.join(folder, "install.sh"))
                or (os.path.isfile(venv_python)
                    and not _venv_imports(venv_python))):
            _restore_old(folder, old)
            raise BrowserError(
                "the updated program did not check out. "
                "the old version was restored")
        shutil.rmtree(old, ignore_errors=True)
        return folder
    finally:
        shutil.rmtree(work, ignore_errors=True)


def relaunch_command(folder, pid=None):
    """The command that restarts the installed app once the
    current process has exited: a small shell that waits for
    the old pid to disappear, then runs the installed
    run.sh. The wait matters, because the single-instance
    guard would otherwise hand the new launch to the
    still-running old window and exit."""
    if pid is None:
        pid = os.getpid()
    run_sh = os.path.join(folder, "run.sh")
    script = ("while kill -0 %d 2>/dev/null; do sleep 0.2; "
              "done; exec sh %s" % (pid, _shell_quote(run_sh)))
    return ["sh", "-c", script]


def _shell_quote(text):
    """A single-quoted shell word."""
    return "'" + text.replace("'", "'\\''") + "'"


def relaunch(folder, pid=None, _popen=None):
    """Start the installed app again, detached, once the
    current process has exited. The caller quits after this
    returns. Returns the spawned process object."""
    popen = _popen or subprocess.Popen
    return popen(relaunch_command(folder, pid=pid),
                 stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL,
                 start_new_session=True)
