"""Tidy titles, tab grouping, and history (task #8).

Raw page titles are long and messy, so the brain writes a 2–5 word
title for each tab (the raw title shows until the tidy one lands,
and stays when no brain is answering). Related tabs are grouped
by embedding similarity — with plain domain grouping as the
fallback when no embeddings model is answering. Visits are
recorded in ~/.forge/history.json and the start page shows them
ordered by group (a group is a site there: fast, no model call
on every new tab).
"""

import json
import os
import re
import time
from urllib.parse import urlparse

TIDY_WORDS = 5
GROUP_SIMILARITY = 0.75
_HISTORY_LIMIT = 200

_TIDY_PROMPT = ("Give this web page a short tab title of 2 to 5 words. "
                "Reply with only the title, nothing else.\n"
                "Page title: {title}\nURL: {url}")


def tidy_title(session, title, url=None):
    """A 2–5 word title from the brain. Falls back to the raw title
    (truncated) when the brain is missing or answers junk."""
    raw = (title or "").strip()
    fallback = (raw[:40] or "Page")
    try:
        reply = session._chat([{"role": "user",
                                "content": _TIDY_PROMPT.format(
                                    title=raw, url=url or "")}])
    except Exception:
        return fallback
    cleaned = (reply or "").strip().strip("\"'").replace("\n", " ")
    words = [w for w in re.split(r"\s+", cleaned) if w][:TIDY_WORDS]
    if not words:
        return fallback
    return " ".join(words)


def _domain(item):
    try:
        return urlparse(item.get("url") or "").netloc or "other"
    except Exception:
        return "other"


def _centroid(vectors):
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def group_items(items, embed_fn=None):
    """Group [{title, url}] by relatedness. With embed_fn (texts ->
    vectors) the groups come from embedding similarity; without it,
    from the site (domain). Returns [{"name": str, "items": [...]}]
    in first-seen order."""
    items = list(items)
    if not items:
        return []
    if embed_fn is not None:
        try:
            texts = [f"{i.get('title') or ''} {i.get('url') or ''}".strip()
                     for i in items]
            vectors = embed_fn(texts)
        except Exception:
            vectors = None
        if vectors and len(vectors) == len(items):
            groups = []  # each: {"name", "items", "vectors"}
            for item, vector in zip(items, vectors):
                placed = False
                for group in groups:
                    if _cosine(vector, _centroid(group["vectors"])) >= GROUP_SIMILARITY:
                        group["items"].append(item)
                        group["vectors"].append(vector)
                        placed = True
                        break
                if not placed:
                    groups.append({"name": (item.get("title") or _domain(item))[:40],
                                   "items": [item], "vectors": [vector]})
            return [{"name": g["name"], "items": g["items"]} for g in groups]
    groups = {}
    order = []
    for item in items:
        key = _domain(item)
        if key not in groups:
            groups[key] = {"name": key, "items": []}
            order.append(key)
        groups[key]["items"].append(item)
    return [groups[key] for key in order]


def history_path():
    override = os.environ.get("FORGE_BROWSER_HISTORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "history.json")


def record_visit(url, title):
    """Append a visit (deduped by URL, newest first). Never raises —
    history is a convenience, not a promise."""
    url = (url or "").strip()
    if not url or url.startswith("http://127.0.0.1"):
        return
    try:
        entries = load_history()
        entries = [e for e in entries if e.get("url") != url]
        entries.insert(0, {"url": url, "title": (title or url).strip(),
                           "at": time.time()})
        entries = entries[:_HISTORY_LIMIT]
        path = history_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
    except OSError:
        pass


def load_history():
    try:
        with open(history_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def grouped_history():
    """History ordered by group (a group is a site), newest group
    first, newest visit first inside each group."""
    entries = load_history()
    groups = {}
    order = []
    for entry in entries:
        key = _domain(entry)
        if key not in groups:
            groups[key] = {"name": key, "items": []}
            order.append(key)
        groups[key]["items"].append(entry)
    return [groups[key] for key in order]
