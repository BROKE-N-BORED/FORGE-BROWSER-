"""Tidy titles, tab grouping, and history (task #8).

Raw page titles are long and messy, so the brain writes a 2–5 word
title for each tab (the raw title shows until the tidy one lands,
and stays when no brain is answering). Related tabs are grouped
by embedding similarity — with plain domain grouping as the
fallback when no embeddings model is answering. Visits are
recorded in ~/.forge/history.json (sensitive URLs excluded) and
the start page shows them ordered by group (a group is a site
there: fast, no model call on every new tab) and by journey —
runs of visits split by quiet gaps, resumable from the start page.
"""

import json
import os
import queue
import re
import threading
import time
import zlib
from urllib.parse import urlparse

from .memory import is_sensitive_url

TIDY_WORDS = 5
GROUP_SIMILARITY = 0.75
_HISTORY_LIMIT = 200
TIDY_CACHE_LIMIT = 200  # N5: the tidy cache holds this many URLs, FIFO
JOURNEY_GAP_SECONDS = 30 * 60  # N2: a new journey after 30 quiet minutes
# N12: tab-bar colors, one per group — light enough to read on dark.
GROUP_COLORS = ("#ffb347", "#7fae7a", "#6ea8d8",
                "#d87fd0", "#d8c56e", "#7fd0c5")

_TIDY_PROMPT = ("Give this web page a short tab title of 2 to 5 words. "
                "End on a content word, not 'the', 'of', or 'to'. "
                "Reply with only the title, nothing else.\n"
                "Page title: {title}\nURL: {url}")

# N1: words a tidy title must not end on after the 5-word cut.
_TRAILING_STOPWORDS = {"the", "a", "of", "to", "in", "and"}

# N4: words that never name a group on their own.
_TOPIC_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on",
                    "for", "with", "how", "what", "is", "are", "by", "at",
                    "from", "home", "page", "site"}


def _trim_trailing_stopwords(words):
    """Drop trailing stop-words after the 5-word cut — 'The Complete
    Guide To The' becomes 'The Complete Guide' (N1). Keeps at
    least one word."""
    words = list(words)
    while (len(words) > 1
           and words[-1].lower().strip(".,;:!?") in _TRAILING_STOPWORDS):
        words.pop()
    return words


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
    return " ".join(_trim_trailing_stopwords(words))


class TidyCache(dict):
    """URL -> tidy title, capped at TIDY_CACHE_LIMIT entries, FIFO
    (N5): the oldest entry goes when a new one arrives past the cap,
    so the cache never grows with every distinct URL for the life
    of the window."""
    def __setitem__(self, key, value):
        if key not in self and len(self) >= TIDY_CACHE_LIMIT:
            self.pop(next(iter(self)))
        super().__setitem__(key, value)


class TidyWorker:
    """One tidy at a time, coalescing rapid titleChanged events per
    tab (N6). request() records the latest (view, url, raw) for a
    tab; the worker thread processes each tab's latest request once,
    after a short debounce, so an SPA retitling five times in a
    second costs one brain call, not five."""
    def __init__(self, tidy_fn, on_done, debounce=0.2):
        self._tidy_fn = tidy_fn  # (url, raw) -> tidy title
        self._on_done = on_done  # (view, url, tidy) -> None
        self._debounce = debounce
        self._pending = {}
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None

    def request(self, view, url, raw):
        key = id(view)
        with self._lock:
            self._pending[key] = (view, url, raw)
            if key not in self._queued:
                self._queued.add(key)
                self._queue.put(key)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run,
                                                daemon=True)
                self._thread.start()

    def _run(self):
        while True:
            key = self._queue.get()
            if self._debounce:
                time.sleep(self._debounce)
            with self._lock:
                entry = self._pending.pop(key, None)
                self._queued.discard(key)
            if entry is None:
                continue
            view, url, raw = entry
            try:
                tidy = self._tidy_fn(url, raw)
            except Exception:
                continue
            self._on_done(view, url, tidy)


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


def _group_topic_name(items):
    """A group's name from the words its titles share (N4) — the
    most frequent content words, not the first tab's title (which
    could be 'Home' for a mixed group). Falls back to the first
    title when nothing is shared."""
    counts = {}
    for item in items:
        seen = set()
        for word in re.split(r"\s+", item.get("title") or ""):
            word = word.lower().strip(".,;:!?()\"'")
            if len(word) > 2 and word not in _TOPIC_STOPWORDS:
                seen.add(word)
        for word in seen:
            counts[word] = counts.get(word, 0) + 1
    shared = sorted(((word, n) for word, n in counts.items() if n >= 2),
                    key=lambda kv: (-kv[1], kv[0]))
    if not shared:
        return (items[0].get("title") or _domain(items[0]))[:40]
    words = [word for word, _n in shared[:2]]
    return " ".join(word.capitalize() for word in words)[:40]


def group_items(items, embed_fn=None):
    """Group [{title, url}] by relatedness. With embed_fn (texts ->
    vectors) the groups come from embedding similarity; without it,
    from the site (domain). Returns [{"name": str, "items": [...]}].

    Order-stable (N4): a sorted copy is grouped, so the same tabs
    always give the same groups no matter what order they arrive
    in — then visible order is restored inside each group (J3) for
    display. Embedding groups are named from the words their
    titles share, not the first tab's title. The embedded text is
    the title alone (N8) — the URL used to dilute topic grouping
    with site identity; a title-less item embeds its domain
    instead."""
    items = list(items)
    if not items:
        return []
    indexed = sorted(enumerate(items),
                     key=lambda p: ((p[1].get("title") or "").lower(),
                                    (p[1].get("url") or "").lower()))
    if embed_fn is not None:
        try:
            texts = [(item.get("title") or _domain(item)).strip()
                     for _i, item in indexed]
            vectors = embed_fn(texts)
        except Exception:
            vectors = None
        if vectors and len(vectors) == len(indexed):
            groups = []  # each: {"items": [(index, item)], "vectors"}
            for (index, item), vector in zip(indexed, vectors):
                placed = False
                for group in groups:
                    if _cosine(vector, _centroid(group["vectors"])) >= GROUP_SIMILARITY:
                        group["items"].append((index, item))
                        group["vectors"].append(vector)
                        placed = True
                        break
                if not placed:
                    groups.append({"items": [(index, item)],
                                   "vectors": [vector]})
            return [{"name": _group_topic_name(
                        [item for _i, item in group["items"]]),
                     "items": [item for _i, item in
                               sorted(group["items"], key=lambda p: p[0])]}
                    for group in groups]
    groups = {}
    order = []
    for index, item in indexed:
        key = _domain(item)
        if key not in groups:
            groups[key] = {"name": key, "items": []}
            order.append(key)
        groups[key]["items"].append((index, item))
    return [{"name": groups[key]["name"],
             "items": [item for _i, item in
                       sorted(groups[key]["items"], key=lambda p: p[0])]}
            for key in order]


def assign_group_colors(group_names):
    """A stable color per group name (N12): the same name always
    gets the same color, across tab changes and restarts — the
    color comes from a CRC of the name, not its position."""
    return {name: GROUP_COLORS[zlib.crc32(name.encode("utf-8"))
                                % len(GROUP_COLORS)]
            for name in group_names}


# N9: title pairs for checking the 0.75 grouping threshold against
# real embeddings — same-topic pairs should score above it,
# different-topic pairs below it. Run tests/validate_threshold.py
# on a machine with nomic-embed-text installed.
_THRESHOLD_PAIRS = (
    ("How to Sharpen a Chainsaw", "Chainsaw Maintenance Guide", True),
    ("Best Running Shoes for Beginners",
     "Choosing Your First Running Shoes", True),
    ("Chocolate Cake Recipe", "Easy Chocolate Desserts", True),
    ("Python Dictionary Tutorial", "Python Lists and Dictionaries", True),
    ("How to Sharpen a Chainsaw", "Chocolate Cake Recipe", False),
    ("Best Running Shoes for Beginners", "Python Dictionary Tutorial", False),
    ("Chocolate Cake Recipe", "How to Change a Car Tire", False),
    ("Python Dictionary Tutorial", "Best Running Shoes for Beginners", False),
)


def threshold_report(embed_fn):
    """Embed the fixed pair set and report same-topic vs
    different-topic cosine ranges, so the 0.75 grouping threshold
    can be checked against real embeddings (N9). Returns
    {"same": {"min", "max", "mean"}, "different": {...},
    "separated": bool} — separated is True when every same-topic
    pair scores at or above GROUP_SIMILARITY and every
    different-topic pair below it."""
    texts = [title for pair in _THRESHOLD_PAIRS for title in pair[:2]]
    vectors = embed_fn(texts)
    if len(vectors) != len(texts):
        raise ValueError("the embeddings model answered oddly (wrong count)")
    same, different = [], []
    for i, (_a, _b, is_same) in enumerate(_THRESHOLD_PAIRS):
        score = _cosine(vectors[2 * i], vectors[2 * i + 1])
        (same if is_same else different).append(score)

    def stats(values):
        return {"min": min(values), "max": max(values),
                "mean": sum(values) / len(values)}

    return {"same": stats(same), "different": stats(different),
            "separated": (min(same) >= GROUP_SIMILARITY
                          and max(different) < GROUP_SIMILARITY)}


def history_path():
    override = os.environ.get("FORGE_BROWSER_HISTORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "history.json")


def _save_history(entries):
    """Atomic history write (N10): temp file + rename, so a crash
    mid-write never leaves a half-written history file."""
    path = history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle)
    os.replace(tmp, path)


def record_visit(url, title):
    """Append a visit (deduped by URL, newest first). Never raises —
    history is a convenience, not a promise. Sensitive URLs (bank,
    health, login — memory.py's patterns, N3) are never recorded."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return  # about:blank, data:, and other non-web pages are not history
    if url.startswith("http://127.0.0.1"):
        return
    if is_sensitive_url(url):
        return
    try:
        entries = load_history()
        entries = [e for e in entries if e.get("url") != url]
        entries.insert(0, {"url": url, "title": (title or url).strip(),
                           "at": time.time()})
        entries = entries[:_HISTORY_LIMIT]
        _save_history(entries)
    except OSError:
        pass


def clear_history():
    """Delete all recorded history (N3). Returns True when the file
    was removed."""
    try:
        os.unlink(history_path())
        return True
    except OSError:
        return False


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


def journeys():
    """History grouped into resumable journeys (N2): runs of visits
    with less than JOURNEY_GAP_SECONDS between consecutive visits.
    Returns [{"name", "started", "ended", "items": [...]}] newest
    journey first, newest visit first inside each journey. The name
    comes from the journey's most-visited site, or its newest
    entry's title when no site repeats."""
    entries = load_history()
    if not entries:
        return []
    ordered = list(reversed(entries))  # oldest first
    runs = [[ordered[0]]]
    for entry in ordered[1:]:
        gap = entry.get("at", 0) - runs[-1][-1].get("at", 0)
        if gap > JOURNEY_GAP_SECONDS:
            runs.append([entry])
        else:
            runs[-1].append(entry)
    out = []
    for run in reversed(runs):  # newest journey first
        items = list(reversed(run))  # newest visit first
        out.append({"name": _journey_name(items),
                    "started": run[0].get("at", 0),
                    "ended": run[-1].get("at", 0),
                    "items": items})
    return out


def _journey_name(items):
    counts = {}
    for item in items:
        domain = _domain(item)
        counts[domain] = counts.get(domain, 0) + 1
    best, count = max(counts.items(), key=lambda kv: kv[1])
    if count >= 2:
        return best
    return (items[0].get("title") or items[0].get("url") or "Journey")[:40]
