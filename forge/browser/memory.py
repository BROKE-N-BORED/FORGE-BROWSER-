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

import json
import os
import re
import time
import uuid

_MEMORY_LIMIT = 200
_SIMILARITY_FLOOR = 0.5

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


def is_sensitive_url(url):
    lowered = (url or "").lower()
    return any(pattern in lowered for pattern in SENSITIVE_PATTERNS)


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
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"entries": entries[:_MEMORY_LIMIT]}, handle, indent=2)
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
    given words. Returns how many were removed."""
    needle = (text_or_id or "").strip()
    if not needle:
        return 0
    entries = _load()
    kept = []
    removed = 0
    for entry in entries:
        if entry.get("id") == needle or needle.lower() in entry["text"].lower():
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


def _words(text):
    return {w for w in re.findall(r"\w+", (text or "").lower())
            if w not in _STOPWORDS}


def search_entries(query, top_k=3, embed_fn=None):
    """Entries most relevant to the query, best first. Embedding
    similarity when embed_fn is given and works; keyword overlap
    otherwise. Returns [] when nothing is relevant."""
    entries = list_entries()
    if not entries or not (query or "").strip():
        return []
    if embed_fn is not None:
        try:
            vectors = embed_fn([query] + [e["text"] for e in entries])
            query_vector, vectors = vectors[0], vectors[1:]
            scored = sorted(
                ((_cosine(query_vector, v), i)
                 for i, v in enumerate(vectors)), reverse=True)
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
# would feel on every page load.
# ----------------------------------------------------------------------

_PAGE_MEMORY_LIMIT = 100
_PAGE_CHUNKS_LIMIT = 12
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
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pages": pages[:_PAGE_MEMORY_LIMIT]}, handle)
    except OSError:
        pass


def remember_page(url, title, text):
    """Keep a visited page's chunks for later recall. Returns True
    when stored. Silently skips sensitive pages, local pages, and
    pages too thin to be worth remembering."""
    from .retrieve import chunk_text

    url = (url or "").strip()
    text = (text or "").strip()
    if not url or is_sensitive_url(url) or url.startswith("http://127.0.0.1"):
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


def search_pages(query, top_k=3, embed_fn=None):
    """Pages whose stored chunks best match the query. Returns
    [{url, title, text, score}] — one entry per page, best chunk.
    [] when nothing matches or no embeddings model is answering."""
    from .retrieve import embed_texts

    embed = embed_fn or embed_texts
    pages = _load_pages()
    if not pages or not (query or "").strip():
        return []
    flat = [(page, chunk) for page in pages for chunk in page["chunks"]]
    try:
        query_vector = embed([query])[0]
        vectors = []
        for start in range(0, len(flat), _EMBED_BATCH):
            vectors.extend(embed([chunk for _page, chunk in flat[start:start + _EMBED_BATCH]]))
    except Exception:
        return []
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


def list_pages():
    """Stored pages, newest first: url, title, chunk count."""
    return [{"url": p["url"], "title": p["title"], "at": p.get("at", 0),
             "chunks": len(p["chunks"])} for p in _load_pages()]
