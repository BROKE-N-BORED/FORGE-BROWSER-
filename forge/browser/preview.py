"""Link previews: hover a link, get two lines about where it goes.

On demand only — nothing is fetched until the person rests on a
link (the window waits DWELL_MS first), and never on hover alone.
No prefetching: a page full of links must not turn into a page
full of fetches on this CPU.

The preview is the destination's own words, not a model summary:
its title plus the first lines of its text. Binary destinations
(image, audio, video, unreadable files) say what they are
instead. Previews are cached by URL (50 entries) so a second
hover on the same link costs nothing.
"""

from .files import fetch_any

DWELL_MS = 600
_CACHE_LIMIT = 50
_CACHE = {}


def _first_lines(text, max_lines=2, max_chars=220):
    lines = []
    for line in (text or "").splitlines():
        line = line.strip(" #*`>").strip()
        if line:
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return " ".join(lines)[:max_chars].strip()


def _store(url, text):
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[url] = text


def clear_preview_cache():
    _CACHE.clear()


def preview_url(url, fetcher=None):
    """Two lines about a link's destination. "" when there is
    nothing sensible to say (empty URL, anchor-only). A plain note
    when the destination could not be fetched."""
    url = (url or "").strip()
    if not url or url.startswith("#"):
        return ""
    if url in _CACHE:
        return _CACHE[url]
    fetch = fetcher or (lambda u: fetch_any(u, describe=False, transcribe=False))
    try:
        result = fetch(url)
    except Exception:
        text = "Couldn't fetch that link."
        _store(url, text)
        return text
    title = (result.title or "").strip()
    if result.kind in ("image", "audio", "video"):
        if result.size_bytes >= 1024:
            size = f", {result.size_bytes // 1024} KB"
        else:
            size = f", {result.size_bytes} bytes" if result.size_bytes else ""
        text = f"{title + ' — ' if title else ''}{result.kind} file{size}."
    elif result.kind == "binary":
        kind = result.content_type or "unknown type"
        text = f"{title or 'A file'} — couldn't read it as text ({kind})."
    else:
        snippet = _first_lines(result.text)
        if title and snippet:
            text = f"{title}\n{snippet}"
        elif title:
            text = title
        elif snippet:
            text = snippet
        else:
            text = "That page has no readable text."
    _store(url, text)
    return text
