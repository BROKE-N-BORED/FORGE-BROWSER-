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


# G-P18: settings are re-read from disk on every call (per
# search, per chat turn) — cache the parsed values, keyed on the
# file's mtime + size, so a changed file is re-read at once.
# Callers get a copy: save_settings merges into the loaded dict,
# and must never edit the cached one.
_settings_cache = {"path": None, "key": None, "data": None}


def load_settings():
    """The stored settings over the defaults. Unknown keys are kept
    (a newer version may have written them)."""
    path = settings_path()
    try:
        stat = os.stat(path)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if (key is not None and _settings_cache["path"] == path
            and _settings_cache["key"] == key
            and _settings_cache["data"] is not None):
        return dict(_settings_cache["data"])
    values = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        _settings_cache.update({"path": path, "key": key,
                                "data": dict(values)})
        return values
    if isinstance(stored, dict):
        for stored_key, value in stored.items():
            if isinstance(value, str):
                values[stored_key] = value
    _settings_cache.update({"path": path, "key": key,
                            "data": dict(values)})
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
    _settings_cache.update({"path": None, "key": None, "data": None})
    return merged


def get_setting(key):
    return load_settings().get(key, DEFAULTS.get(key, ""))


def set_setting(key, value):
    save_settings({key: value})
