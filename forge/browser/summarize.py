"""Summarize a page with the local chat brain (task #5).

Map-reduce, because a whole page never fits a small model's working
context: the page is split into ~1500-char chunks with overlap, each
chunk is summarized on its own, then the chunk summaries are combined
into one short summary. Results are cached by URL + content hash +
length, so asking twice about the same page costs nothing — and a
changed page is never served a stale summary, because the hash
changed with it.

The cache is a convenience file under ~/.forge, not an archive: it
keeps the newest 50 entries, and any cache failure is ignored
silently — summarization never depends on it.

A long summarize is stoppable (K1): cancel() is honored between
map calls, and the map step can ride the fast seat (K9) while the
reduce stays on the brain. Map work is capped (K2) so a huge page
cannot mean hundreds of brain calls.
"""

import hashlib
import json
import os

from .errors import BrowserError
from .retrieve import chunk_text

CHUNK_CHARS = 1500  # ~375 tokens a chunk — inside a small model's comfort
CHUNK_OVERLAP = 200  # K4: like #3, so a fact split across a boundary
# is still seen whole by one map call
MAX_MAP_CHUNKS = 40  # K2: cap the map work on huge pages
REDUCE_GROUP = 12    # K2: above this many parts, reduce in groups
# first (a second reduce level) so the reduce prompt fits the ctx
_CACHE_LIMIT = 50

_MAP_PROMPT = ("Summarize this part of a web page in 2-3 sentences, "
               "keeping names, numbers, and facts. Do not add anything "
               "the part does not say.\n\n{chunk}")
_MAP_PROMPT_SHORT = ("Summarize this part of a web page in 1-2 sentences, "
                     "keeping names, numbers, and facts. Do not add anything "
                     "the part does not say.\n\n{chunk}")
_REDUCE_PROMPT = ("Combine these part-summaries into one short summary "
                  "of the whole page (5-8 sentences), keeping the key "
                  "facts and numbers. Do not add anything the summaries "
                  "do not say.\n\n{parts}")
_REDUCE_PROMPT_SHORT = ("Combine these part-summaries into one short summary "
                        "of the whole page (2-3 sentences), keeping the key "
                        "facts and numbers. Do not add anything the summaries "
                        "do not say.\n\n{parts}")


class SummarizeStopped(Exception):
    """A summarize was stopped by cancel() between brain calls (K1)."""


class SummarizeEmptyError(BrowserError):
    """The page had text but the brain produced no summary (K5) —
    distinct from an empty page, which returns \"\"."""


def cache_path():
    override = os.environ.get("FORGE_BROWSER_SUMMARY_CACHE", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "summaries.json")


def _cache_key(url, text, length="standard"):
    return hashlib.sha256(
        ((url or "") + "\n" + length + "\n" + text).encode("utf-8")).hexdigest()


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
        # K10: atomic write — a crash mid-write must not corrupt
        # the cache file.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def _chunks(text, size=CHUNK_CHARS):
    """Split text into ~size-char chunks at line boundaries with
    overlap (K4), via #3's chunker — same shape, shared code."""
    return chunk_text(text, size=size, overlap=CHUNK_OVERLAP)


def _sample_chunks(chunks, limit=MAX_MAP_CHUNKS):
    """Cap the map work (K2): at most `limit` chunks, evenly spaced
    across the page so the summary covers the whole page, not just
    its head."""
    if len(chunks) <= limit:
        return chunks
    step = len(chunks) / limit
    return [chunks[int(i * step)] for i in range(limit)]


def summarize_page(session, page_text, url=None, on_chunk=None,
                   on_progress=None, map_session=None, length="standard"):
    """Summary of page_text with the brain in session. Cached by
    URL + content hash + length. on_chunk, when given, streams the
    final summary as it is generated (the part-summaries stay
    silent). on_progress, when given, hears "part N of M" before
    each map call (K3). map_session, when given, runs the map step
    (K9); the reduce always stays on the brain. length is
    "standard" (5-8 sentences) or "short" (2-3) (K11).

    Returns "" only when the page itself is empty. A page with
    text whose brain produced nothing raises SummarizeEmptyError
    (K5). A stop requested via cancel() raises SummarizeStopped
    between brain calls (K1)."""
    # Not a chat turn: clear the last-turn speed so a summary's done
    # line can't show a stale one (G10). A mid-turn summarize action
    # is unaffected — ask() recomputes the speed when the turn ends.
    session.last_speed = None
    text = (page_text or "").strip()
    if not text:
        return ""
    length = "short" if length == "short" else "standard"
    key = _cache_key(url, text, length)
    cache = _load_cache()
    if key in cache:
        return cache[key]
    chunks = _sample_chunks(_chunks(text))
    map_prompt = _MAP_PROMPT_SHORT if length == "short" else _MAP_PROMPT
    reduce_prompt = (_REDUCE_PROMPT_SHORT if length == "short"
                     else _REDUCE_PROMPT)
    mapper = map_session if map_session is not None else session
    # One chunk means the map call IS the summary — stream it live.
    # With several chunks the map calls stay silent and only the
    # combine step streams, so the person never sees scratch work.
    stream_map = on_chunk if len(chunks) == 1 else None
    parts = []
    for i, chunk in enumerate(chunks):
        if getattr(session, "_stop_requested", False):
            raise SummarizeStopped("summarize stopped")
        if on_progress is not None:
            on_progress(f"part {i + 1} of {len(chunks)}")
        reply = mapper._chat([{"role": "user",
                               "content": map_prompt.format(chunk=chunk)}],
                             stream_map)
        if reply.strip():
            parts.append(reply.strip())
    if not parts:
        raise SummarizeEmptyError(
            "the page has text, but the brain produced no summary")
    if len(parts) > 1:
        # K2: a second reduce level — reduce in groups first, so
        # the final reduce prompt stays small whatever the page.
        while len(parts) > REDUCE_GROUP:
            grouped = []
            for start in range(0, len(parts), REDUCE_GROUP):
                if getattr(session, "_stop_requested", False):
                    raise SummarizeStopped("summarize stopped")
                group = parts[start:start + REDUCE_GROUP]
                reply = session._chat(
                    [{"role": "user",
                      "content": reduce_prompt.format(
                          parts="\n\n".join(group))}])
                if reply.strip():
                    grouped.append(reply.strip())
            if not grouped:
                raise SummarizeEmptyError(
                    "the page has text, but the brain produced no summary")
            parts = grouped
    if len(parts) == 1:
        summary = parts[0]
    else:
        if getattr(session, "_stop_requested", False):
            raise SummarizeStopped("summarize stopped")
        summary = session._chat(
            [{"role": "user",
              "content": reduce_prompt.format(parts="\n\n".join(parts))}],
            on_chunk)
    summary = summary.strip()
    if not summary:
        raise SummarizeEmptyError(
            "the page has text, but the brain produced no summary")
    cache[key] = summary
    _save_cache(cache)
    return summary
