"""Link previews: hover a link, get two lines about where it goes.

On demand only — nothing is fetched until the person rests on a
link (the window waits the dwell first), and never on hover alone.
No prefetching: a page full of links must not turn into a page
full of fetches on this CPU.

The preview is the destination's own words, not a model summary:
its title plus the first lines of its text. Binary destinations
(image, audio, video, unreadable files) say what they are
instead — from a HEAD request alone (L1), never a download.
Previews are cached by URL (50 entries) so a second hover on the
same link costs nothing. Failures are never cached (L2), so a
transient failure retries on the next hover.
"""

import os
import urllib.parse

from .files import fetch_any

DWELL_MS = 600
_CACHE_LIMIT = 50
_CACHE = {}

# Kinds worth fetching the body for: the preview reads their text.
# Anything else (image, audio, video, archives, ...) is named from
# its HEAD headers alone and never downloaded (L1).
_TEXT_TYPES = ("application/json", "application/pdf", "application/xml")


def dwell_ms():
    """The hover dwell before a preview fetches (L9): the env
    override wins, then the stored setting, then DWELL_MS.
    Clamped to 100–5000 ms; garbage means the default."""
    raw = os.environ.get("FORGE_BROWSER_PREVIEW_DWELL_MS", "").strip()
    if not raw:
        from .settings import get_setting
        raw = (get_setting("preview_dwell_ms") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DWELL_MS
    return max(100, min(5000, value))


def _is_text_kind(content_type):
    """True when the body is worth fetching for its text (L1)."""
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct.startswith("text/"):
        return True
    return ct in _TEXT_TYPES or ct.endswith(("+json", "+xml"))


def _media_kind(content_type):
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("video/"):
        return "video"
    return None


def _format_size(size):
    if not size:
        return ""
    if size < 1024:
        return f", {size} bytes"
    if size < 1024 * 1024:
        return f", {size // 1024} KB"
    if size < 1024 * 1024 * 1024:
        return f", {size // (1024 * 1024)} MB"
    return f", {size // (1024 * 1024 * 1024)} GB"


def _default_head(url):
    """(content_type, size_bytes) from a HEAD request, or raise."""
    from ._http import http_head
    _final, headers = http_head(url, timeout=10)
    size = 0
    length = headers.get("Content-Length") if headers is not None else None
    try:
        size = int(length) if length is not None else 0
    except (TypeError, ValueError):
        size = 0
    content_type = ""
    if headers is not None:
        try:
            content_type = headers.get_content_type() or ""
        except Exception:
            content_type = ""
    return content_type.lower(), size


_BOILERPLATE_STARTS = ("sign in", "log in", "sign up", "log out",
                       "accept cookies", "we use cookies",
                       "this site uses cookies", "cookie policy",
                       "cookie settings", "subscribe", "newsletter")


def _is_boilerplate(line):
    """A nav/boilerplate line, not content (L5): a link bar, a lone
    nav word, or a line starting with a boilerplate phrase."""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.count("|") >= 2:
        return True  # "Home | About | Contact | Cart"
    if " " not in stripped and len(stripped) <= 20:
        return True  # a lone nav word ("Shop")
    low = stripped.lower()
    return any(low.startswith(word) for word in _BOILERPLATE_STARTS)


def _first_lines(text, max_lines=2, max_chars=220):
    lines = []
    for line in (text or "").splitlines():
        line = line.strip(" #*`>").strip()
        if line:
            lines.append(line)
    # L5: skip nav/boilerplate lines and take the first real
    # paragraph. When everything is boilerplate, fall back to the
    # first lines unfiltered — some text beats none.
    content = [line for line in lines if not _is_boilerplate(line)]
    picked = (content or lines)[:max_lines]
    return " ".join(picked)[:max_chars].strip()


def _normalize_url(url):
    """Cache key (L7): lowercase scheme + host, no fragment, no
    trailing slash — the same page under two spellings fetches once."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
    elif path == "/":
        path = ""
    return urllib.parse.urlunparse(
        ((parsed.scheme or "").lower(), (parsed.netloc or "").lower(),
         path, parsed.params, parsed.query, ""))


def _store(key, text):
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = text


def clear_preview_cache():
    _CACHE.clear()


def preview_url(url, fetcher=None, head_fetcher=None):
    """Two lines about a link's destination. "" when there is
    nothing sensible to say (empty URL, anchor-only). A plain note
    when the destination could not be fetched. head_fetcher, when
    given, answers (content_type, size_bytes) for the HEAD-first
    path (L1); the default HEAD runs only when no fetcher was
    injected, so tests that stand in a fetcher stay offline."""
    url = (url or "").strip()
    if not url or url.startswith("#"):
        return ""
    key = _normalize_url(url)
    if key in _CACHE:
        return _CACHE[key]
    if head_fetcher is None and fetcher is None:
        head_fetcher = _default_head
    if head_fetcher is not None:
        try:
            content_type, size = head_fetcher(url)
        except Exception:
            content_type, size = None, 0
        if content_type is not None and not _is_text_kind(content_type):
            # L1: media and other non-text kinds are named from
            # the headers alone — the body is never downloaded.
            kind = _media_kind(content_type)
            if kind:
                text = f"{kind} file{_format_size(size)}."
            else:
                text = (f"A file — couldn't read it as text "
                        f"({content_type}).")
            _store(key, text)
            return text
    fetch = fetcher or (lambda u: fetch_any(u, describe=False, transcribe=False))
    try:
        result = fetch(url)
    except Exception:
        # L2: failures are NOT cached — a transient failure
        # retries on the next hover.
        return "Couldn't fetch that link."
    title = (result.title or "").strip()
    if result.kind in ("image", "audio", "video"):
        text = f"{title + ' — ' if title else ''}{result.kind} file{_format_size(result.size_bytes)}."
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
    _store(key, text)
    return text
