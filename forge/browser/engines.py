"""Engine status — what browser power this machine actually has.

One plain report used by the CLI and the setup script's checks:
which engines exist, which search backends are configured, whether
the vision seat and a transcription backend are available, and
where the vault lives. Read-only; reports absence plainly instead
of guessing.
"""

import os
import shutil

from .cdp import find_chromium, find_lightpanda
from .vault import default_vault_path


def _engine_home():
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "forge-browser")


def _read_version_file(name):
    """First field of a version file install.sh wrote, or None."""
    try:
        with open(os.path.join(_engine_home(), name), encoding="utf-8") as handle:
            return (handle.read().strip().split() or [None])[0]
    except OSError:
        return None


def transcription_backends():
    """Every transcription backend installed on this machine, in
    the order transcribe_saved_audio tries them (S8) — not just
    the `whisper` CLI. A whisper.cpp `main` counts only when its
    path says whisper, the same rule the transcriber uses."""
    import importlib.util
    found = []
    if os.environ.get("FORGE_TRANSCRIBE_CMD"):
        found.append("FORGE_TRANSCRIBE_CMD")
    if shutil.which("whisper"):
        found.append("whisper CLI")
    for binary in ("whisper-cli", "main"):
        exe = shutil.which(binary)
        if exe and "whisper" in (exe + binary):
            found.append(f"whisper.cpp ({binary})")
    if importlib.util.find_spec("faster_whisper") is not None:
        found.append("faster-whisper")
    return found


def engine_status():
    from . import media
    lightpanda = find_lightpanda()
    vision_model = None
    try:
        vision_model = media.pick_vision_model()
    except Exception:
        vision_model = None
    backends = transcription_backends()
    return {
        "chromium": find_chromium(),
        "chromium_revision": _read_version_file("chromium/REVISION"),
        "lightpanda_binary": lightpanda,
        "lightpanda_version": _read_version_file("lightpanda.version"),
        "lightpanda_serving": os.environ.get("FORGE_LIGHTPANDA_URL", "") or None,
        "cdp_url": os.environ.get("FORGE_CDP_URL", "") or None,
        "searxng_url": os.environ.get("FORGE_SEARXNG_URL", "") or None,
        "search_default": "searxng" if os.environ.get("FORGE_SEARXNG_URL")
                          else "keyless chain (DuckDuckGo Lite/HTML, Bing)",
        "vision_seat_model": vision_model,
        "transcription": ", ".join(backends) if backends else None,
        "vault_path": default_vault_path(),
        "vault_exists": os.path.exists(default_vault_path()),
    }


def format_status(status):
    def show(value):
        return value if value else "not found / not configured"
    return "\n".join([
        f"Chromium engine: {show(status['chromium'])}",
        f"Chromium revision: {show(status['chromium_revision'])}",
        f"Lightpanda binary: {show(status['lightpanda_binary'])}",
        f"Lightpanda version: {show(status['lightpanda_version'])}",
        f"Lightpanda serving: {show(status['lightpanda_serving'])}",
        f"External CDP endpoint: {show(status['cdp_url'])}",
        f"Search: {status['search_default']}"
        + (f" ({status['searxng_url']})" if status['searxng_url'] else ""),
        f"Vision seat (local Ollama): {show(status['vision_seat_model'])}",
        f"Transcription: {show(status['transcription'])}",
        f"Vault: {status['vault_path']} ({'exists' if status['vault_exists'] else 'not created yet'})",
    ])
