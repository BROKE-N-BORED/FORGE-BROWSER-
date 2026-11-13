"""Small persistent settings for the browser window (task E9).

What used to live only in environment variables and hand-edited
files — the download folder, the Ollama address, the SearXNG
address — lives here, in one JSON file under ~/.forge/, edited
from the Settings window. Model seats and feature toggles live in
seats.json (seats.py), not here.

Nothing in this file is secret (a folder and two local addresses),
so it is plain JSON, like history.json.
"""

import json
import os

DEFAULTS = {
    "download_dir": "",   # empty = ~/Downloads
    "ollama_url": "",     # empty = http://127.0.0.1:11434
    "searxng_url": "",    # empty = no local SearXNG
    "preview_dwell_ms": "600",  # hover dwell before a link preview (L9)
}


def settings_path():
    override = os.environ.get("FORGE_BROWSER_SETTINGS_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge",
                        "browser-settings.json")


def load_settings():
    """The stored settings over the defaults. Unknown keys are kept
    (a newer version may have written them)."""
    values = dict(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return values
    if isinstance(stored, dict):
        for key, value in stored.items():
            if isinstance(value, str):
                values[key] = value
    return values


def save_settings(values):
    """Write the given settings (merged over the stored ones)."""
    merged = load_settings()
    for key, value in (values or {}).items():
        if isinstance(value, str):
            merged[key] = value
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
    os.replace(tmp, path)
    return merged


def get_setting(key):
    return load_settings().get(key, DEFAULTS.get(key, ""))


def set_setting(key, value):
    save_settings({key: value})
