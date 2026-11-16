"""Session restore (task E2): the tabs open when the window closes
reopen on the next launch.

The session is one JSON file under ~/.forge/. Forge Browser's own
local pages (127.0.0.1) are never saved — their port changes every
launch, so a saved one would be dead. A session with no tabs left
after that filter is no session at all.
"""

import json
import os

from .memory import _write_json_atomic

MAX_TABS = 50  # a runaway tab count must not make launch crawl


def session_path():
    override = os.environ.get("FORGE_BROWSER_SESSION_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "session.json")


def _saveable(url):
    url = (url or "").strip()
    if not url:
        return False
    lowered = url.lower()
    if lowered.startswith("http://127.0.0.1") or \
            lowered.startswith("http://localhost") or \
            lowered.startswith("https://localhost"):
        return False
    return lowered.startswith("http://") or lowered.startswith("https://")


def save_session(tabs, current=0):
    """tabs: [{"url": ..., "title": ...}] in tab order. Returns the
    number of tabs saved."""
    kept = [{"url": t.get("url", ""), "title": t.get("title", "")}
            for t in (tabs or []) if _saveable(t.get("url", ""))]
    kept = kept[:MAX_TABS]
    path = session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not kept:
        try:
            os.unlink(path)
        except OSError:
            pass
        return 0
    current = max(0, min(int(current or 0), len(kept) - 1))
    # Atomic + 0600 (G-S7): the session lists the person's open tabs.
    _write_json_atomic(path, {"tabs": kept, "current": current}, indent=2)
    return len(kept)


def load_session():
    """{"tabs": [...], "current": n}, or None when there is no
    session to restore."""
    try:
        with open(session_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tabs = [t for t in data.get("tabs", [])
            if isinstance(t, dict) and _saveable(t.get("url", ""))]
    if not tabs:
        return None
    # G-C32: slice first, then clamp — the old code clamped
    # `current` against the pre-slice list, so a restored index
    # could exceed the returned tab list.
    tabs = tabs[:MAX_TABS]
    current = data.get("current", 0)
    if not isinstance(current, int):
        current = 0
    return {"tabs": tabs,
            "current": max(0, min(current, len(tabs) - 1))}
