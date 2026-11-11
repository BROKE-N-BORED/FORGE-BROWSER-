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
import urllib.request

from .errors import BrowserError
from .media import OLLAMA_URL

CHUNK_CHARS = 1200   # ~300 tokens a chunk
CHUNK_OVERLAP = 200
TOP_K = 4            # chunks handed to the brain (~1200 tokens total)
SHORT_PAGE_CHARS = 6000  # at or under this, the whole page goes in


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


def retrieve(query_embedding, chunk_embeddings, top_k=TOP_K):
    """Indices of the top_k chunks by cosine similarity, best first."""
    scored = sorted(
        ((_cosine(query_embedding, emb), i)
         for i, emb in enumerate(chunk_embeddings)),
        reverse=True)
    return [i for _score, i in scored[:top_k]]


def embed_texts(texts, model=None):
    """Vectors for texts from the local embeddings model. Raises
    BrowserError when none is answering — the caller treats that
    as 'no retrieval', never as a chat failure."""
    model = model or embed_model()
    payload = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL + "/api/embed", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise BrowserError(
            f"no embeddings model is answering at {OLLAMA_URL} "
            f"({exc}) — retrieval is off") from exc
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts):
        raise BrowserError("the embeddings model answered oddly (wrong count)")
    return vectors


def page_context(session, question, page_text, url=None):
    """The page text to put in the brain's context for this question.
    Short page: the whole thing. Long page: the top-k chunks closest
    to the question. No embeddings model: the first SHORT_PAGE_CHARS
    chars, as before. Returns "" when there is no page text."""
    text = (page_text or "").strip()
    if not text:
        return ""
    if len(text) <= SHORT_PAGE_CHARS:
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
        except BrowserError:
            return text[:SHORT_PAGE_CHARS]
        entry = (chunks, vectors)
        cache[key] = entry
        while len(cache) > 4:  # pages change; keep the cache small
            cache.pop(next(iter(cache)))
    chunks, vectors = entry
    try:
        query_vector = embed_texts([question])[0]
    except BrowserError:
        return text[:SHORT_PAGE_CHARS]
    picked = retrieve(query_vector, vectors)
    return "\n\n---\n\n".join(chunks[i] for i in picked)
