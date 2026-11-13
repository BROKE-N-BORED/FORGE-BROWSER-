"""Bookmarks (task E6): saved pages, one JSON file under ~/.forge/.

A bookmark is a URL + a title + when it was added. Adding the same
URL again updates its title instead of doubling it. The start page
shows bookmarks as quick links (task E10).
"""

import json
import os
import time


def bookmarks_path():
    override = os.environ.get("FORGE_BROWSER_BOOKMARKS_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "bookmarks.json")


def _load():
    try:
        with open(bookmarks_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("bookmarks", [])
    return [e for e in entries
            if isinstance(e, dict) and e.get("url")]


def _save(entries):
    path = bookmarks_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"bookmarks": entries}, handle, indent=2)
    os.replace(tmp, path)


def list_bookmarks():
    """All bookmarks, newest first."""
    return sorted(_load(), key=lambda e: e.get("added", 0), reverse=True)


def is_bookmarked(url):
    return any(e["url"] == url for e in _load())


def add_bookmark(url, title=""):
    """Save a page. Returns the entry, or None for a blank URL.
    Re-adding updates the title and keeps the original position."""
    url = (url or "").strip()
    if not url:
        return None
    entries = _load()
    for entry in entries:
        if entry["url"] == url:
            if title:
                entry["title"] = title
            _save(entries)
            return entry
    entry = {"url": url, "title": title or url,
             "added": time.time()}
    entries.append(entry)
    _save(entries)
    return entry


def remove_bookmark(url):
    """Remove a bookmark. Returns True when one was removed."""
    entries = [e for e in _load() if e["url"] != url]
    if len(entries) == len(_load()):
        return False
    _save(entries)
    return True
