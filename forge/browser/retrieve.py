"""Chunk-and-retrieve page feeding (task #3).

A whole page never fits a small model's working context, and stuffing
the first 6000 chars into the prompt answers the wrong question when
the answer lives at the bottom. So a long page is split into
~1200-char chunks with overlap, embedded once with the local
nomic-embed-text model, and only the chunks closest to the question
go to the brain. Short pages skip all of this and go in whole.
When no embeddings model is answering, retrieval degrades silently
to the old first-6000-chars behaviour — chat never breaks because
the memory seat is missing.
"""

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

from .errors import BrowserError
from .media import OLLAMA_URL

CHUNK_CHARS = 1200   # ~300 tokens a chunk
CHUNK_OVERLAP = 200
TOP_K = 4            # chunks handed to the brain (~1200 tokens total)
SHORT_PAGE_CHARS = 6000  # at or under this, the whole page goes in
RELEVANCE_FLOOR = 0.3  # chunks below this similarity never reach the brain (I5)
EMBED_BATCH = 64     # texts per /api/embed call (I6)
NO_MATCH = "[No part of this page closely matched the question.]"
FALLBACK_NOTE = ("[Only the first 6000 characters of this page were "
                 "searched — the rest was not seen.]")
EMBED_FAILURE_TTL = 60.0  # seconds a known-missing embed model is
# believed without another round trip (J6)
_embed_failures = {}  # model -> (until, reason, message)


def clear_embed_failure_cache():
    """Forget known-missing embed models (tests, and any caller
    that just installed one)."""
    _embed_failures.clear()


def embed_model():
    return os.environ.get("FORGE_BROWSER_EMBED_MODEL", "").strip() or "nomic-embed-text"


def chunk_text(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Split text into ~size-char chunks at line boundaries, each
    (after the first) starting with the previous chunk's tail so
    an answer split across a boundary is still found whole."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    pieces = []
    for line in lines:
        while len(line) > size:
            pieces.append(line[:size])
            line = line[size:]
        if line:
            pieces.append(line)
    raw, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > size:
            raw.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        raw.append(current)
    chunks = []
    for i, chunk in enumerate(raw):
        if i > 0 and overlap:
            chunks.append(raw[i - 1][-overlap:] + "\n" + chunk)
        else:
            chunks.append(chunk)
    return chunks


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def retrieve_scored(query_embedding, chunk_embeddings, top_k=TOP_K):
    """[(index, score)] of the top_k chunks by cosine similarity,
    best first. Ties go to the earliest chunk (I3): sorted by
    (-score, i), not reverse-tuple order, which handed ties to
    the END of the page."""
    scored = sorted(
        ((_cosine(query_embedding, emb), i)
         for i, emb in enumerate(chunk_embeddings)),
        key=lambda p: (-p[0], p[1]))
    return [(i, score) for score, i in scored[:top_k]]


def retrieve(query_embedding, chunk_embeddings, top_k=TOP_K):
    """Indices of the top_k chunks by cosine similarity, best first."""
    return [i for i, _score in retrieve_scored(
        query_embedding, chunk_embeddings, top_k)]


def _embed_batch(texts, model):
    """One /api/embed call. The BrowserError it raises carries a
    .reason (I7): 'model-missing' when Ollama answers 404 (the
    embeddings model is not installed), 'ollama-down' when nothing
    answers at all, 'unknown' otherwise."""
    payload = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL + "/api/embed", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            err = BrowserError(
                f"the embeddings model {model} is not installed "
                "(Ollama answered 404) — run install.sh to pull it")
            err.reason = "model-missing"
        else:
            err = BrowserError(
                f"the embeddings model answered oddly (HTTP {exc.code})")
            err.reason = "unknown"
        _note_embed_failure(model, err)
        raise err from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError,
            OSError) as exc:
        err = BrowserError(
            f"no embeddings model is answering at {OLLAMA_URL} "
            f"({exc}) — retrieval is off")
        err.reason = "ollama-down"
        _note_embed_failure(model, err)
        raise err from exc
    except Exception as exc:
        err = BrowserError(
            f"the embeddings model did not answer ({exc})")
        err.reason = "unknown"
        raise err from exc
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts):
        err = BrowserError(
            "the embeddings model answered oddly (wrong count)")
        err.reason = "unknown"
        raise err
    return vectors


def _note_embed_failure(model, err):
    """Remember a known-missing embed model briefly (J6): a
    'model-missing' or 'ollama-down' failure is believed for
    EMBED_FAILURE_TTL so every send does not pay another failed
    round trip. Anything else ('unknown') is not cached — a
    transient oddity must not look like a missing model."""
    reason = getattr(err, "reason", "unknown")
    if reason in ("model-missing", "ollama-down"):
        _embed_failures[model] = (
            time.monotonic() + EMBED_FAILURE_TTL, reason, str(err))


def embed_texts(texts, model=None):
    """Vectors for texts from the local embeddings model, in
    batches of EMBED_BATCH per /api/embed call (I6) — one call
    for a whole big page could exceed the 60 s timeout and fail
    retrieval on exactly the long pages it exists for. Raises
    BrowserError when none is answering — the caller treats that
    as 'no retrieval', never as a chat failure. A model known
    missing (J6) raises at once, without another round trip."""
    model = model or embed_model()
    cached = _embed_failures.get(model)
    if cached is not None:
        until, reason, message = cached
        if time.monotonic() < until:
            err = BrowserError(message)
            err.reason = reason
            raise err
        del _embed_failures[model]
    texts = list(texts)
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        vectors.extend(_embed_batch(texts[start:start + EMBED_BATCH], model))
    return vectors


def _dedupe_overlap(previous, chunk):
    """Strip the overlap between two adjacent picked chunks (I9):
    the longest suffix of the previous chunk matching this one's
    prefix, capped at CHUNK_OVERLAP + 1 chars (the overlap plus
    the joining newline), plus one leading newline if present."""
    max_n = CHUNK_OVERLAP + 1
    best = 0
    for n in range(min(len(previous), len(chunk), max_n), 0, -1):
        if previous.endswith(chunk[:n]):
            best = n
            break
    rest = chunk[best:]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


def _join_chunks(chunks, picked):
    """Picked chunks in document order (I2), each labeled with its
    position, adjacent overlaps deduped (I9)."""
    parts = []
    previous_index = None
    previous_text = None
    for i in sorted(picked):
        text = chunks[i]
        if previous_index is not None and i == previous_index + 1:
            text = _dedupe_overlap(previous_text, text)
        parts.append(f"--- chunk {i + 1} of {len(chunks)} ---\n{text}")
        previous_index = i
        previous_text = chunks[i]  # the FULL text, for the next match
    return "\n\n".join(parts)


def retrieve_note(last):
    """The plain-words note for a _retrieve_last record (I10),
    or "" when there is nothing to say."""
    if not last:
        return ""
    mode = last.get("mode")
    if mode == "retrieval":
        return f"(Searched {last.get('chunks_searched', 0)} chunks.)"
    if mode == "fallback":
        return "(Retrieval off — searched the head of the page only.)"
    if mode == "no-match":
        return "(No part of the page closely matched.)"
    return ""


def _record(session, mode, chunks_searched=0, chunks_used=0, reason=None):
    """What the last page_context call did (I10): the dock and the
    CLI read it for the done-line note, and the dock reads the
    reason for its once-per-session notice (I7)."""
    session._retrieve_last = {
        "mode": mode,
        "chunks_searched": chunks_searched,
        "chunks_used": chunks_used,
        "reason": reason,
    }


def page_context(session, question, page_text, url=None):
    """The page text to put in the brain's context for this question.
    Short page: the whole thing. Long page: the top-k chunks closest
    to the question, in document order with position labels (I2),
    skipping anything below the relevance floor (I5). No embeddings
    model: the first SHORT_PAGE_CHARS chars, labeled so the brain
    knows it only saw the head (I4). Returns "" when there is no
    page text. Records what happened in session._retrieve_last (I10)."""
    text = (page_text or "").strip()
    if not text:
        _record(session, "none")
        return ""
    if len(text) <= SHORT_PAGE_CHARS:
        _record(session, "whole")
        return text
    cache = getattr(session, "_retrieve_cache", None)
    if cache is None:
        cache = session._retrieve_cache = {}
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    entry = cache.get(key)
    if entry is None:
        chunks = chunk_text(text)
        try:
            vectors = embed_texts(chunks)
        except BrowserError as exc:
            _record(session, "fallback",
                    reason=getattr(exc, "reason", "unknown"))
            return FALLBACK_NOTE + "\n\n" + text[:SHORT_PAGE_CHARS]
        entry = (chunks, vectors)
        cache[key] = entry
        while len(cache) > 4:  # pages change; keep the cache small
            cache.pop(next(iter(cache)))
    chunks, vectors = entry
    # I8: @tabN refs are addressing, not content — strip them
    # from the query before embedding (fall back to the raw
    # question if nothing is left).
    query = re.sub(r"@tab\d+", "", question or "").strip()
    if not query:
        query = question
    try:
        query_vector = embed_texts([query])[0]
    except BrowserError as exc:
        _record(session, "fallback",
                reason=getattr(exc, "reason", "unknown"))
        return FALLBACK_NOTE + "\n\n" + text[:SHORT_PAGE_CHARS]
    scored = retrieve_scored(query_vector, vectors)
    picked = [i for i, score in scored if score >= RELEVANCE_FLOOR]
    if not picked:
        _record(session, "no-match", chunks_searched=len(chunks))
        return NO_MATCH
    _record(session, "retrieval", chunks_searched=len(chunks),
            chunks_used=len(picked))
    return _join_chunks(chunks, picked)
