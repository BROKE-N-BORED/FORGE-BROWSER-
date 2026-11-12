"""One small HTTP GET helper shared by search and fetch.

Standard library only. Sends a User-Agent that names the tool, follows
redirects (urllib default), decompresses gzip when a server insists on
it, and turns every failure into a BrowserError in plain words. No
cookies are stored, no headers beyond User-Agent / Accept are sent,
and there is no key or token of any kind to send.

Download caps (task #17): nothing is ever read into memory without
a limit. In-memory bodies stop at MAX_BODY_BYTES; files saved to
disk stream there in chunks and stop at MAX_FILE_BYTES. A declared
Content-Length over the cap is refused from the headers alone, and
the cap counts decompressed bytes, so a gzip bomb cannot sneak past
a small wire size. On a refusal the partial file is removed.
"""

import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from .errors import BrowserError


def _allow_local_switch():
    """The explicit local-allow switch: FORGE_BROWSER_ALLOW_LOCAL=1."""
    return os.environ.get("FORGE_BROWSER_ALLOW_LOCAL", "").strip().lower() in (
        "1", "true", "yes", "on")


def check_fetch_url(url, allow_local=False):
    """Refuse a fetch to a non-public address, unless local fetching
    was explicitly allowed (allow_local=True, or the
    FORGE_BROWSER_ALLOW_LOCAL env switch).

    Refused: loopback (127.0.0.0/8, ::1), RFC 1918 LAN ranges,
    link-local, and the localhost / *.local names.

    DNS names are NOT resolved here, deliberately: in proxied
    environments a public name can resolve to a non-global address
    (this sandbox's resolver answers example.org with a CGNAT
    address), so resolving would refuse ordinary browsing. A name
    that points at a private address is therefore not caught by
    this fence — catching that needs the fetch to connect to the
    address the fence checked, which is a bigger change than this
    item. The fence stops the steering that matters: literal local
    addresses (the router admin page, Ollama on 127.0.0.1:11434,
    a file server on the LAN) and localhost names. The verify flow
    (which serves its own throwaway local page) passes
    allow_local=True.
    """
    if allow_local or _allow_local_switch():
        return
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise BrowserError(f"cannot fetch {url!r} — it has no host to check")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise BrowserError(
            f"Forge Browser doesn't fetch local addresses ({host}) unless "
            "local fetching is allowed — pass allow_local=True, or "
            "--allow-local on the command line, or set "
            "FORGE_BROWSER_ALLOW_LOCAL=1. The verify flow for project "
            "pages allows it on its own.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # a DNS name: not resolved here (see the docstring)
    if not address.is_global:
        raise BrowserError(
            f"Forge Browser doesn't fetch local addresses ({host}) "
            "unless local fetching is allowed — pass allow_local=True, "
            "or --allow-local on the command line, or set "
            "FORGE_BROWSER_ALLOW_LOCAL=1. The verify flow for project "
            "pages allows it on its own.")

USER_AGENT = "ForgeBrowser/0.1 (Code Forge; +https://github.com/BROKE-N-BORED/CODE-FORGE)"
DEFAULT_TIMEOUT = 20  # seconds

# 32 MiB in memory (pages, PDFs, text) and 512 MiB streamed to disk
# (images, audio, video). On an 8 GB machine the browser + model
# already hold most of the RAM; no single download may add more.
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
_CHUNK = 65536


def _refuse_too_big(url, size, cap):
    raise BrowserError(
        f"that download is too big for Forge Browser ({size} bytes, "
        f"over the {cap} byte limit) — it was not downloaded, so it "
        "cannot fill up the machine's memory")


def _precheck_length(headers, max_bytes, url):
    """Refuse from the declared Content-Length alone, before any
    body bytes are read."""
    if headers is None:
        return
    length = headers.get("Content-Length")
    if length is None:
        return
    try:
        declared = int(length)
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        _refuse_too_big(url, declared, max_bytes)


def _iter_chunks(response, max_bytes, url):
    """Yield body chunks, enforcing the cap on the bytes the caller
    keeps — decompressed bytes when the server sent gzip."""
    gzipped = response.headers.get("Content-Encoding") == "gzip"
    decoder = zlib.decompressobj(31) if gzipped else None
    total = 0
    while True:
        raw = response.read(_CHUNK)
        if not raw:
            break
        try:
            chunk = decoder.decompress(raw) if decoder else raw
        except zlib.error as exc:
            raise BrowserError(
                f"the site sent a download Forge Browser could not "
                f"decompress ({url})") from exc
        total += len(chunk)
        if total > max_bytes:
            _refuse_too_big(url, total, max_bytes)
        if chunk:
            yield chunk
    if decoder is not None:
        try:
            tail = decoder.flush()
        except zlib.error as exc:
            raise BrowserError(
                f"the site sent a download Forge Browser could not "
                f"decompress ({url})") from exc
        total += len(tail)
        if total > max_bytes:
            _refuse_too_big(url, total, max_bytes)
        if tail:
            yield tail


def _request(url):
    return urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*"},
    )


def http_get(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BODY_BYTES,
             allow_local=False):
    """GET ``url``; return (final_url, headers, body_bytes).

    The body is read in chunks and refused over max_bytes.
    Raises BrowserError with a plain-words message on any failure.
    Local addresses are refused unless allow_local (task #20).
    """
    check_fetch_url(url, allow_local=allow_local)
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            _precheck_length(response.headers, max_bytes, url)
            body = b"".join(_iter_chunks(response, max_bytes, url))
            return response.geturl(), response.headers, body
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"the site answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BrowserError(f"could not reach {url} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise BrowserError(f"timed out after {timeout}s reading {url}") from exc


def http_get_to_file(url, path, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_FILE_BYTES,
                     allow_local=False):
    """Stream ``url`` straight to ``path``; RAM stays flat no
    matter the size. Returns (final_url, headers, size_bytes).
    Over max_bytes the download is refused and the partial file
    is removed. Raises BrowserError in plain words on failure.
    Local addresses are refused unless allow_local (task #20)."""
    check_fetch_url(url, allow_local=allow_local)
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            _precheck_length(response.headers, max_bytes, url)
            size = 0
            try:
                with open(path, "wb") as handle:
                    for chunk in _iter_chunks(response, max_bytes, url):
                        handle.write(chunk)
                        size += len(chunk)
            except BrowserError:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            except OSError as exc:
                raise BrowserError(
                    f"could not save the download ({exc})") from exc
            return response.geturl(), response.headers, size
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"the site answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BrowserError(f"could not reach {url} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise BrowserError(f"timed out after {timeout}s reading {url}") from exc


def decode_body(body, headers):
    """Decode bytes using the declared charset, falling back to UTF-8."""
    charset = "utf-8"
    if headers is not None:
        try:
            declared = headers.get_content_charset()
        except Exception:
            declared = None
        if declared:
            charset = declared
    return body.decode(charset, errors="replace")
