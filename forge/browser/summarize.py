"""Summarize a page with the local chat brain (task #5).

Map-reduce, because a whole page never fits a small model's working
context: the page is split into ~1500-char chunks, each chunk is
summarized on its own, then the chunk summaries are combined into
one short summary. Results are cached by URL + content hash, so
asking twice about the same page costs nothing — and a changed page
is never served a stale summary, because the hash changed with it.

The cache is a convenience file under ~/.forge, not an archive: it
keeps the newest 50 entries, and any cache failure is ignored
silently — summarization never depends on it.
"""

import hashlib
import json
import os

CHUNK_CHARS = 1500  # ~375 tokens a chunk — inside a small model's comfort
_CACHE_LIMIT = 50

_MAP_PROMPT = ("Summarize this part of a web page in 2-3 sentences, "
               "keeping names, numbers, and facts. Do not add anything "
               "the part does not say.\n\n{chunk}")
_REDUCE_PROMPT = ("Combine these part-summaries into one short summary "
                  "of the whole page (5-8 sentences), keeping the key "
                  "facts and numbers. Do not add anything the summaries "
                  "do not say.\n\n{parts}")


def cache_path():
    override = os.environ.get("FORGE_BROWSER_SUMMARY_CACHE", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "summaries.json")


def _cache_key(url, text):
    return hashlib.sha256(((url or "") + "\n" + text).encode("utf-8")).hexdigest()


def _load_cache():
    try:
        with open(cache_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        path = cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        while len(cache) > _CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle)
    except OSError:
        pass


def _chunks(text, size=CHUNK_CHARS):
    """Split text into ~size-char chunks at line boundaries; a single
    over-long line is hard-split so no chunk ever exceeds the size."""
    parts = [p.strip() for p in (text or "").split("\n") if p.strip()]
    pieces = []
    for part in parts:
        while len(part) > size:
            pieces.append(part[:size])
            part = part[size:]
        if part:
            pieces.append(part)
    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > size:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def summarize_page(session, page_text, url=None, on_chunk=None):
    """Summary of page_text with the brain in session. Cached by
    URL + content hash. on_chunk, when given, streams the final
    summary as it is generated (the part-summaries stay silent)."""
    text = (page_text or "").strip()
    if not text:
        return ""
    key = _cache_key(url, text)
    cache = _load_cache()
    if key in cache:
        return cache[key]
    chunks = _chunks(text)
    # One chunk means the map call IS the summary — stream it live.
    # With several chunks the map calls stay silent and only the
    # combine step streams, so the person never sees scratch work.
    stream_map = on_chunk if len(chunks) == 1 else None
    parts = []
    for chunk in chunks:
        reply = session._chat([{"role": "user",
                                "content": _MAP_PROMPT.format(chunk=chunk)}],
                              stream_map)
        if reply.strip():
            parts.append(reply.strip())
    if not parts:
        return ""
    if len(parts) == 1:
        summary = parts[0]
    else:
        summary = session._chat(
            [{"role": "user",
              "content": _REDUCE_PROMPT.format(parts="\n\n".join(parts))}],
            on_chunk)
    summary = summary.strip()
    if summary:
        cache[key] = summary
        _save_cache(cache)
    return summary
