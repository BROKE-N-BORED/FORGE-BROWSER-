"""Local memory store (task #12).

Facts and preferences the person asks the browser to remember
("I like dark mode", "my budget is $500"), kept in one plain
JSON file under ~/.forge that they can read, edit, and export
with any text editor. Nothing is ever recorded on its own —
only an explicit "remember ..." (chat ACTION or CLI) writes
here, never page contents. And the brain is never offered
memory context while the current page looks sensitive
(banking, login, checkout, health...), so a remembered fact
cannot leak into the wrong tab.

Search is by embedding similarity when a model is answering,
keyword overlap when it is not.
"""

import hashlib
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse

_MEMORY_LIMIT = 200
_SIMILARITY_FLOOR = 0.5
_MIN_FORGET_NEEDLE = 3  # a shorter text needle never matches (R2)
_EMBED_CACHE_LIMIT = 1500  # cached vectors kept on disk (R4)

# URL fragments that mark a page sensitive: no memory context there.
SENSITIVE_PATTERNS = (
    "bank", "paypal", "stripe", "login", "signin", "sign-in", "account",
    "checkout", "password", "health", "medical", "patient", "tax", "irs",
    "wallet", "payment", "billing",
)
_STOPWORDS = {"the", "a", "an", "is", "it", "i", "my", "to", "of", "and",
              "in", "on", "for", "me", "you", "do", "does", "what", "how"}


def memory_path():
    override = os.environ.get("FORGE_BROWSER_MEMORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "memory.json")


# R8: sensitive patterns match at word boundaries, not as raw
# substrings — "bank" must not flag riverbank pages, "tax" must
# not flag syntax pages. A host label or path segment still
# matches ("bank.example", "/login", "checkout.example").
_SENSITIVE_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in SENSITIVE_PATTERNS) + r")\b")


def is_sensitive_url(url):
    return bool(_SENSITIVE_RE.search((url or "").lower()))


# ----------------------------------------------------------------------
# Per-site memory controls (task F3) — the person's own list, beyond
# the sensitive-URL patterns above. A blocked site gets no memory
# context and no pages recorded; an allowed site keeps memory even
# when its URL matches a sensitive pattern. Blocking a host covers
# its subdomains (blocking example.com covers www.example.com).
# ----------------------------------------------------------------------

def memory_sites_path():
    override = os.environ.get("FORGE_BROWSER_MEMORY_SITES_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "memory-sites.json")


def _load_sites():
    try:
        with open(memory_sites_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {
                "blocked": [h for h in data.get("blocked", [])
                            if isinstance(h, str) and h],
                "allowed": [h for h in data.get("allowed", [])
                            if isinstance(h, str) and h],
            }
    except (OSError, ValueError):
        pass
    return {"blocked": [], "allowed": []}


def _save_sites(sites):
    try:
        path = memory_sites_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"blocked": sorted(set(sites["blocked"])),
                       "allowed": sorted(set(sites["allowed"]))},
                      handle, indent=2)
        os.replace(tmp, path)  # atomic: a crash never leaves half a file (R10)
    except OSError:
        pass


def _site_host(value):
    """The host of a bare host or a full URL, lowercased. "" when
    there is no host to read."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return urlparse(value).hostname or ""


def _host_matches(host, listed):
    """True when host is listed, or is a subdomain of a listed host."""
    return bool(host) and (host == listed or host.endswith("." + listed))


def site_memory_allowed(url):
    """May memory be used on this page — facts offered as context,
    pages recorded for recall? The person's per-site list wins:
    blocked means no, allowed means yes even on a sensitive-looking
    URL; otherwise the sensitive-pattern rule decides."""
    host = _site_host(url)
    sites = _load_sites()
    if any(_host_matches(host, listed) for listed in sites["blocked"]):
        return False
    if any(_host_matches(host, listed) for listed in sites["allowed"]):
        return True
    return not is_sensitive_url(url)


def list_site_memory():
    """{"blocked": [...], "allowed": [...]} — the person's list."""
    return _load_sites()


def block_site_memory(host_or_url):
    """No memory on this site (and its subdomains). Returns the host,
    or "" when none could be read."""
    host = _site_host(host_or_url)
    if not host:
        return ""
    sites = _load_sites()
    sites["allowed"] = [h for h in sites["allowed"] if h != host]
    if host not in sites["blocked"]:
        sites["blocked"].append(host)
    _save_sites(sites)
    return host


def allow_site_memory(host_or_url):
    """Memory stays on for this site even when its URL looks
    sensitive. Returns the host, or "" when none could be read."""
    host = _site_host(host_or_url)
    if not host:
        return ""
    sites = _load_sites()
    sites["blocked"] = [h for h in sites["blocked"] if h != host]
    if host not in sites["allowed"]:
        sites["allowed"].append(host)
    _save_sites(sites)
    return host


def clear_site_memory(host_or_url):
    """Drop a site from both lists — the sensitive-pattern rule
    decides again. Returns the host, or "" when none could be read."""
    host = _site_host(host_or_url)
    if not host:
        return ""
    sites = _load_sites()
    sites["blocked"] = [h for h in sites["blocked"] if h != host]
    sites["allowed"] = [h for h in sites["allowed"] if h != host]
    _save_sites(sites)
    return host


def _load():
    try:
        with open(memory_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        entries = data.get("entries", []) if isinstance(data, dict) else []
        return [e for e in entries if isinstance(e, dict) and e.get("text")]
    except (OSError, ValueError):
        return []


def _save(entries):
    try:
        path = memory_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            # R1: keep the NEWEST 200 — entries are stored in
            # insertion order, so slicing from the front dropped
            # the fact just remembered. R10: atomic write.
            json.dump({"entries": entries[-_MEMORY_LIMIT:]}, handle, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def list_entries():
    """All remembered entries, newest first."""
    return sorted(_load(), key=lambda e: e.get("at", 0), reverse=True)


def remember(text, source="chat"):
    """Store a fact. Returns the entry (the existing one when the
    same text is already remembered)."""
    text = (text or "").strip()
    if not text:
        return None
    entries = _load()
    normalized = " ".join(text.lower().split())
    for entry in entries:
        if " ".join(entry["text"].lower().split()) == normalized:
            return entry
    entry = {"id": uuid.uuid4().hex[:8], "text": text,
             "source": source, "at": time.time()}
    entries.append(entry)
    _save(entries)
    return entry


def forget(text_or_id):
    """Remove entries whose id matches, or whose text contains the
    given words. Returns how many were removed. A text needle
    shorter than 3 chars never matches — a one-letter forget
    ("e") wiped the whole store (R2); forget a short fact by its
    id instead (ids always match, whatever their length)."""
    needle = (text_or_id or "").strip()
    if not needle:
        return 0
    entries = _load()
    kept = []
    removed = 0
    for entry in entries:
        if entry.get("id") == needle:
            removed += 1
        elif (len(needle) >= _MIN_FORGET_NEEDLE
                and needle.lower() in entry["text"].lower()):
            removed += 1
        else:
            kept.append(entry)
    if removed:
        _save(kept)
    return removed


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ----------------------------------------------------------------------
# Embedding cache (R4/R6) — text hash -> vector, in memory for the
# process and on disk in a sidecar file. A recall embeds only the
# query + new/changed chunks, and each chat turn does not re-embed
# all 200 facts. The sidecar is a cache, never the store: the page
# store keeps plain text (and stays readable), and deleting the
# cache only costs re-embedding. Keyed by text hash — the vector
# depends only on the text, so identical texts share one entry.
# ----------------------------------------------------------------------

def embed_cache_path():
    override = os.environ.get("FORGE_BROWSER_EMBED_CACHE_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge",
                        "embeddings-cache.json")


class EmbedCache:
    """Persistent embedding cache. Created cheaply (the file loads
    lazily, on first use); vectors_for embeds only what is
    missing and saves when it added anything."""

    def __init__(self, path=None):
        self.path = path or embed_cache_path()
        self._data = None

    def _load(self):
        if self._data is not None:
            return self._data
        self._data = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                for key, vector in data.items():
                    if isinstance(key, str) and isinstance(vector, list):
                        self._data[key] = [float(v) for v in vector]
        except (OSError, ValueError):
            pass
        return self._data

    def _save(self):
        data = self._data or {}
        if len(data) > _EMBED_CACHE_LIMIT:
            # Keep the newest entries (dicts keep insertion order).
            data = dict(list(data.items())[-_EMBED_CACHE_LIMIT:])
            self._data = data
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({key: [round(v, 4) for v in vector]
                           for key, vector in data.items()}, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def vectors_for(self, texts, embed_fn):
        """Vectors for texts, in order — cached ones reused, missing
        ones embedded in one call and saved. Raises whatever
        embed_fn raises when nothing is answering."""
        data = self._load()
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest()
                for t in texts]
        missing = [t for t, key in zip(texts, keys) if key not in data]
        if missing:
            vectors = embed_fn(missing)
            if len(vectors) != len(missing):
                raise ValueError(
                    "the embeddings model answered oddly (wrong count)")
            for text, vector in zip(missing, vectors):
                data[hashlib.sha256(text.encode("utf-8")).hexdigest()] = (
                    list(vector))
            self._save()
        return [data[key] for key in keys]


def _words(text):
    return {w for w in re.findall(r"\w+", (text or "").lower())
            if w not in _STOPWORDS}


def search_entries(query, top_k=3, embed_fn=None, embed_cache=None):
    """Entries most relevant to the query, best first. Embedding
    similarity when embed_fn is given and works; keyword overlap
    otherwise. Returns [] when nothing is relevant. embed_cache
    (R6) reuses stored fact vectors so each turn embeds only the
    query; without it every call embeds all entries."""
    entries = list_entries()
    if not entries or not (query or "").strip():
        return []
    if embed_fn is not None:
        try:
            if embed_cache is not None:
                vectors = embed_cache.vectors_for(
                    [query] + [e["text"] for e in entries], embed_fn)
            else:
                vectors = embed_fn([query] + [e["text"] for e in entries])
            query_vector, vectors = vectors[0], vectors[1:]
            # R7: ties go to the NEWEST entry — entries are
            # newest-first, so sort by (-score, i), not reverse
            # tuple order (which handed ties to the oldest).
            scored = sorted(
                ((_cosine(query_vector, v), i)
                 for i, v in enumerate(vectors)),
                key=lambda pair: (-pair[0], pair[1]))
            return [entries[i] for score, i in scored[:top_k]
                    if score >= _SIMILARITY_FLOOR]
        except Exception:
            pass
    query_words = _words(query)
    scored = []
    for entry in entries:
        overlap = len(query_words & _words(entry["text"]))
        if overlap:
            scored.append((overlap, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _overlap, entry in scored[:top_k]]


def export_memory(path):
    """Write all entries as JSON to path. Returns the path."""
    entries = list_entries()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"entries": entries}, handle, indent=2)
    return path


# ----------------------------------------------------------------------
# Page memory — "that thing I read last week"
#
# Visited pages are kept as text chunks (never on sensitive pages).
# Chunks are stored as TEXT, not vectors: embedding happens only
# when the person actually asks about something they read, in one
# batched call. Embedding at visit time would evict the chat brain
# from the one-model slot on every navigation — a stall the person
# would feel on every page load. Vectors, once embedded, are kept
# in the EmbedCache sidecar (R4) — never in this file.
# ----------------------------------------------------------------------

_PAGE_MEMORY_LIMIT = 100
_PAGE_CHUNKS_LIMIT = 24  # R5: was 12 — a long page kept only its head
_EMBED_BATCH = 200


def page_memory_path():
    override = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "page-memory.json")


def _load_pages():
    try:
        with open(page_memory_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        pages = data.get("pages", []) if isinstance(data, dict) else []
        return [p for p in pages
                if isinstance(p, dict) and p.get("url") and p.get("chunks")]
    except (OSError, ValueError):
        return []


def _save_pages(pages):
    try:
        path = page_memory_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"pages": pages[:_PAGE_MEMORY_LIMIT]}, handle)
        os.replace(tmp, path)  # atomic (R10)
    except OSError:
        pass


def remember_page(url, title, text):
    """Keep a visited page's chunks for later recall. Returns True
    when stored. Silently skips sensitive pages, local pages, and
    pages too thin to be worth remembering."""
    from .retrieve import chunk_text

    url = (url or "").strip()
    text = (text or "").strip()
    if not url.startswith(("http://", "https://")):
        return False  # about:blank, data:, and other non-web pages
    if not site_memory_allowed(url) or url.startswith("http://127.0.0.1"):
        return False
    if len(text) < 200:
        return False
    chunks = chunk_text(text)[:_PAGE_CHUNKS_LIMIT]
    if not chunks:
        return False
    pages = [p for p in _load_pages() if p.get("url") != url]
    pages.insert(0, {"url": url, "title": (title or url).strip(),
                     "at": time.time(), "chunks": chunks})
    _save_pages(pages)
    return True


def _search_pages_keywords(query, pages, top_k):
    """Best chunk per page by keyword overlap — the recall path
    when no embeddings model is answering (R3), so "that thing I
    read last week" works without nomic installed."""
    query_words = _words(query)
    if not query_words:
        return []
    best = {}
    for page in pages:
        for chunk in page["chunks"]:
            overlap = len(query_words & _words(chunk))
            if not overlap:
                continue
            current = best.get(page["url"])
            if current is None or overlap > current["score"]:
                best[page["url"]] = {"url": page["url"], "title": page["title"],
                                     "text": chunk, "score": overlap}
    ranked = sorted(best.values(), key=lambda entry: -entry["score"])
    return ranked[:top_k]


def search_pages(query, top_k=3, embed_fn=None, embed_cache=None):
    """Pages whose stored chunks best match the query. Returns
    [{url, title, text, score}] — one entry per page, best chunk.
    Embedding similarity when a model answers (reusing the
    embed_cache, R4, so a recall embeds only the query + new or
    changed chunks); keyword overlap otherwise (R3). [] when
    nothing matches."""
    from .retrieve import embed_texts

    embed = embed_fn or embed_texts
    pages = _load_pages()
    if not pages or not (query or "").strip():
        return []
    flat = [(page, chunk) for page in pages for chunk in page["chunks"]]
    try:
        if embed_cache is not None:
            vectors = embed_cache.vectors_for(
                [query] + [chunk for _page, chunk in flat], embed)
            query_vector, vectors = vectors[0], vectors[1:]
        else:
            query_vector = embed([query])[0]
            vectors = []
            for start in range(0, len(flat), _EMBED_BATCH):
                vectors.extend(embed([chunk for _page, chunk in flat[start:start + _EMBED_BATCH]]))
    except Exception:
        return _search_pages_keywords(query, pages, top_k)
    best = {}
    for (page, chunk), vector in zip(flat, vectors):
        score = _cosine(query_vector, vector)
        if score < _SIMILARITY_FLOOR:
            continue
        current = best.get(page["url"])
        if current is None or score > current["score"]:
            best[page["url"]] = {"url": page["url"], "title": page["title"],
                                 "text": chunk, "score": score}
    ranked = sorted(best.values(), key=lambda entry: -entry["score"])
    return ranked[:top_k]


def forget_page(url_or_title):
    """Remove stored pages whose URL matches exactly, or whose
    title contains the given words. Returns how many were
    removed. Title matching needs 3+ chars (the R2 guard, same
    as forget); a URL always matches exactly."""
    needle = (url_or_title or "").strip()
    if not needle:
        return 0
    pages = _load_pages()
    kept = []
    removed = 0
    for page in pages:
        if page.get("url", "").lower() == needle.lower():
            removed += 1
        elif (len(needle) >= _MIN_FORGET_NEEDLE
                and needle.lower() in (page.get("title") or "").lower()):
            removed += 1
        else:
            kept.append(page)
    if removed:
        _save_pages(kept)
    return removed


def list_pages():
    """Stored pages, newest first: url, title, chunk count."""
    return [{"url": p["url"], "title": p["title"], "at": p.get("at", 0),
             "chunks": len(p["chunks"])} for p in _load_pages()]


def page_texts():
    """Stored pages with their chunks (for the morning brief)."""
    return [{"url": p["url"], "title": p["title"], "at": p.get("at", 0),
             "chunks": list(p["chunks"])} for p in _load_pages()]


# ----------------------------------------------------------------------
# Threshold validation (R12) — is the 0.5 similarity floor right?
# Same shape as titles.threshold_report (N9): fixed same-topic and
# different-topic fact/query pairs, embedded by the real model.
# Run on a machine with nomic-embed-text installed:
#     python3 tests/validate_memory_threshold.py
# ----------------------------------------------------------------------

_MEMORY_THRESHOLD_PAIRS = (
    # (fact, query, same-topic?)
    ("I like dark mode.", "do I like dark mode", True),
    ("My budget is $500.", "what is my budget", True),
    ("I prefer tea over coffee.", "do I prefer tea or coffee", True),
    ("My dog is called Biscuit.", "what is my dog called", True),
    ("I like dark mode.", "what is my budget", False),
    ("My budget is $500.", "do I prefer tea or coffee", False),
    ("I prefer tea over coffee.", "what is my dog called", False),
    ("My dog is called Biscuit.", "do I like dark mode", False),
)


def memory_threshold_report(embed_fn):
    """Embed the fixed pair set and report same-topic vs
    different-topic cosine ranges, so the 0.5 floor can be checked
    against real embeddings (R12). Returns {"same": {"min", "max",
    "mean"}, "different": {...}, "separated": bool} — separated is
    True when every same-topic pair scores at or above
    _SIMILARITY_FLOOR and every different-topic pair below it."""
    texts = [text for pair in _MEMORY_THRESHOLD_PAIRS for text in pair[:2]]
    vectors = embed_fn(texts)
    if len(vectors) != len(texts):
        raise ValueError("the embeddings model answered oddly (wrong count)")
    same, different = [], []
    for i, (_a, _b, is_same) in enumerate(_MEMORY_THRESHOLD_PAIRS):
        score = _cosine(vectors[2 * i], vectors[2 * i + 1])
        (same if is_same else different).append(score)

    def stats(values):
        return {"min": min(values), "max": max(values),
                "mean": sum(values) / len(values)}

    return {"same": stats(same), "different": stats(different),
            "separated": (min(same) >= _SIMILARITY_FLOOR
                          and max(different) < _SIMILARITY_FLOOR)}
