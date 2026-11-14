"""One small HTTP GET helper shared by search and fetch.

Standard library only. Sends a User-Agent that names the tool, follows
redirects (urllib default), decompresses gzip when a server insists on
it, and turns every failure into a BrowserError in plain words. No
cookies are stored, no headers beyond User-Agent / Accept are sent,
and there is no key or token of any kind to send.

Download limits (task #17, revised 2026-11-11 at Joshua's order —
downloads need no cap, his machine has plenty of storage): files
saved to disk stream there in chunks with NO size limit; his disk
is the only limit. The one limit that stays is in-memory bodies,
capped at MAX_BODY_BYTES — that is a RAM guard on an 8 GB machine,
not a download cap: a big text file is saved to disk and reported
as saved, never refused. A declared Content-Length over the
in-memory cap is refused from the headers alone, and the cap
counts decompressed bytes, so a gzip bomb cannot sneak past a
small wire size. An explicit max_bytes passed to http_get_to_file
still refuses over that size and removes the partial file.

Downloads (audit #17): a failed download removes its partial file
and says plainly whether the network dropped or the save failed;
resume=True restarts from the saved part (Range requests) instead
of from zero; on_progress reports bytes so far + the total when
known; a full-disk guard stops cleanly when free space runs low
(not a size cap — the disk is still the only limit); and stall
detection aborts a connection that dribbles slower than 1 KB in
60 s (a dead connection, not a slow one).
"""

import http.client
import ipaddress
import json
import os
import shutil
import socket
import time
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
    link-local, and the localhost / *.local names. IP literals in
    non-dotted forms (2130706433, 0x7f.0.0.1, 0177.0.0.1, 127.1)
    are normalized before the check (Z1) — the socket layer
    accepts them even though ipaddress rejects them, so an
    un-normalized fence would wave them through.

    DNS names ARE resolved (Z3): a name that points at the local
    machine or the LAN is refused. The resolved-address test is
    narrower than the literal test on purpose: CGNAT (100.64/10)
    and other non-global-but-not-local ranges are allowed, because
    in proxied environments a public name can resolve there (this
    sandbox's resolver answers example.org with a CGNAT address)
    and those addresses are ISP space, not this machine or LAN. A
    name that does not resolve at all passes the fence — the fetch
    itself then fails with "could not reach". There is no TOCTOU
    between this check and the connection (G1): the pinned
    handlers resolve the request host once more at open time,
    check that answer the same way, and connect to it — the
    address that is checked is the address that is connected to,
    so a hostile DNS cannot flip the answer in between. This
    up-front check is fail-fast (a plain refusal before any
    socket work); the connection-time check is authoritative.

    Redirects are re-fenced on every hop (Z2): the redirect
    handler runs this check on each new URL before following it,
    so a public URL that 302s to 127.0.0.1 is refused, not
    followed.

    The env switch is all-or-nothing for the whole process (Z4):
    FORGE_BROWSER_ALLOW_LOCAL=1 opens local fetching for every
    fetch, including brain-triggered ones. The scoped alternative
    is allow_local=True on one call. The verify flow (which serves
    its own throwaway local page) passes allow_local=True.

    The browser window's own navigation is NOT fenced, by design
    (Z6): the person driving it can open anything, including the
    start page it serves locally. The fence guards the fetches
    the browser makes on a page's or a prompt's say-so, where the
    person is not choosing the address.
    """
    if allow_local or _allow_local_switch():
        return
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        raise BrowserError(f"cannot fetch {url!r} — it has no host to check")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        _refuse_local(host, "localhost name")
    address = _parse_ip_literal(host)
    if address is not None:
        if not address.is_global:
            _refuse_local(host, f"non-global IP literal {address}")
        return
    _resolve_checked_host(host)  # raises on a local resolution


def _parse_ip_literal(host):
    """An ipaddress for a literal host, or None for a DNS name.

    Z1: ipaddress rejects non-dotted IPv4 forms (2130706433,
    0x7f.0.0.1, 0177.0.0.1, 127.1), but the socket layer accepts
    them — inet_aton normalizes them to dotted quads, so the
    fence checks the address the fetch would actually reach."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.ip_address(socket.inet_ntoa(packed))


# Z3: the resolved-address test refuses only addresses that are
# the local machine or the LAN. CGNAT (100.64/10) and the
# documentation ranges are deliberately NOT here — a public name
# can resolve to CGNAT behind a proxy, and those addresses are
# not this machine.
_LOCAL_RESOLVED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
)


def _is_local_address(address):
    """True when a RESOLVED address is the local machine or the LAN
    (Z3). An IPv6-mapped IPv4 address is judged by its IPv4 part."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _LOCAL_RESOLVED_NETWORKS)


def _resolve_checked_host(host):
    """Z3 + G1: resolve a DNS name ONCE, refuse it if ANY answer
    points at a local address, and return the checked addresses
    (deduplicated, resolver order) for the connection to use.
    The fence and the connection share this one resolution — the
    address that is checked is the address that is connected to,
    so a hostile DNS cannot flip the answer in between. A name
    that does not resolve returns [] — the fetch fails on its
    own with "could not reach"."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    addresses = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_local_address(address):
            _log_fence_refusal(host, f"resolves to local address {address}")
            raise BrowserError(
                f"Forge Browser doesn't fetch {host} — it resolves to "
                f"a local address ({address}), so fetching it would "
                "reach this machine or the LAN. If that is what you "
                "meant, pass allow_local=True, or --allow-local on "
                "the command line, or set FORGE_BROWSER_ALLOW_LOCAL=1.")
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    return addresses


def _refuse_local(host, reason):
    """The refusal for a local literal or localhost name: logged
    (Z5) so steering attempts are visible afterwards, then raised
    in plain words."""
    _log_fence_refusal(host, reason)
    raise BrowserError(
        f"Forge Browser doesn't fetch local addresses ({host}) "
        "unless local fetching is allowed — pass allow_local=True, "
        "or --allow-local on the command line, or set "
        "FORGE_BROWSER_ALLOW_LOCAL=1. The verify flow for project "
        "pages allows it on its own.")


# Z5: fence refusals are recorded (URL + reason) in a bounded log
# under ~/.forge, so steering attempts are visible afterwards.
# The log is best-effort — a failure to write it must never break
# the fence itself.
_FENCE_LOG_LIMIT = 200


def _fence_log_path():
    override = os.environ.get("FORGE_BROWSER_FENCE_LOG_PATH", "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".forge", "fence-log.json")


def _log_fence_refusal(host, reason):
    try:
        path = _fence_log_path()
        entries = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": host,
            "reason": reason,
        })
        entries = entries[-_FENCE_LOG_LIMIT:]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=1)
        os.replace(tmp_path, path)
    except Exception:
        pass  # the fence stands even if the log cannot be written


class _FencedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that re-runs the fence on every hop (Z2):
    a public URL that 302s to a local address is refused, not
    followed. The hop cap is urllib's own (10). build_opener
    skips the default redirect handler when a subclass is passed,
    so this is the only redirect handler on the opener."""

    def __init__(self, allow_local=False):
        super().__init__()
        self._allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The Location value may be relative ("/page") — resolve it
        # against the request URL first, so the fence checks the
        # address the fetch would actually reach. The parent
        # resolves it again (idempotent for an absolute URL).
        resolved = urllib.parse.urljoin(req.get_full_url(), newurl)
        check_fetch_url(resolved, allow_local=self._allow_local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# G1: DNS pinning — resolve once, connect to the checked address.
# The pinned handlers resolve each request's host at open time
# (initial request and every redirect hop), check that answer
# with the fence, and hand the checked addresses to the
# connection, which tries them in resolver order. The socket is
# opened to a checked address, never to a fresh re-resolution —
# the TOCTOU the 0.7 changelog named is closed. For HTTPS the
# TLS handshake still uses the DNS name for SNI and certificate
# verification (the name, not the address, is what the
# certificate is issued for); only the TCP socket goes to the
# pinned address.

def _pinned_addrs_for(req, allow_local):
    """The checked addresses a request may connect to (G1), or
    None for no pin: local fetching allowed, an IP literal (the
    literal IS the address — nothing to re-resolve), a localhost
    name, or a name that does not resolve (the fetch then fails
    with "could not reach", as before). The fence still refuses
    literals and localhost names up front in check_fetch_url and
    on redirect hops; this function only decides the pin."""
    if allow_local or _allow_local_switch():
        return None
    host = (urllib.parse.urlparse(req.get_full_url()).hostname
            or "").lower().rstrip(".")
    if not host:
        return None
    # A proxied request is not pinned: the socket goes to the
    # proxy, not the origin, so pinning to the origin's address
    # would connect to the wrong host entirely. ProxyHandler
    # rewrites req.host to the proxy before http_open runs — a
    # request host that is not the URL's host means proxied.
    req_host = urllib.parse.urlparse(
        "//" + (req.host or "")).hostname or ""
    if req_host.lower().rstrip(".") != host:
        return None
    if host == "localhost" or host.endswith(".localhost") \
            or host.endswith(".local"):
        return None
    if _parse_ip_literal(host) is not None:
        return None
    return _resolve_checked_host(host)


class _PinnedConnectionMixin:
    """A connection that opens its socket to the address the
    fence already checked. http.client keeps the socket factory
    as an instance attribute (_create_connection) precisely so
    it can be replaced; the pinned subclasses point it at this
    method. With no pin, the behaviour is exactly the default.
    With a pin, each checked address is tried in resolver order
    — the same fall-through create_connection does on its own
    when a host has several answers and the first is dead."""

    _pinned_addrs = None

    def _pinned_create_connection(self, address, timeout,
                                  source_address=None):
        if not self._pinned_addrs:
            return socket.create_connection(address, timeout,
                                            source_address)
        _host, port = address
        last_error = None
        for ip in self._pinned_addrs:
            try:
                return socket.create_connection(
                    (ip, port), timeout, source_address)
            except OSError as exc:
                last_error = exc
        raise last_error


class _PinnedHTTPConnection(_PinnedConnectionMixin,
                            http.client.HTTPConnection):
    def __init__(self, *args, pinned_addrs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_addrs = pinned_addrs or []
        self._create_connection = self._pinned_create_connection


class _PinnedHTTPSConnection(_PinnedConnectionMixin,
                             http.client.HTTPSConnection):
    def __init__(self, *args, pinned_addrs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_addrs = pinned_addrs or []
        self._create_connection = self._pinned_create_connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler that pins the connection to the address the
    fence checked (G1). do_open passes extra keywords to the
    connection constructor, so the pin rides in as one."""

    def __init__(self, allow_local=False, debuglevel=None):
        super().__init__(debuglevel)
        self._allow_local = allow_local

    def http_open(self, req):
        return self.do_open(
            _PinnedHTTPConnection, req,
            pinned_addrs=_pinned_addrs_for(req, self._allow_local))


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler with the same pinning (G1). The context is
    passed through exactly as the parent does; only the TCP
    socket is pinned, never the TLS name."""

    def __init__(self, allow_local=False, debuglevel=0, context=None):
        super().__init__(debuglevel, context)
        self._allow_local = allow_local

    def https_open(self, req):
        return self.do_open(
            _PinnedHTTPSConnection, req, context=self._context,
            pinned_addrs=_pinned_addrs_for(req, self._allow_local))


def _urlopen(request, timeout, allow_local):
    """urlopen with the fence on redirects (Z2) and DNS pinning
    (G1): a fresh opener per call, so the handlers' allow_local
    matches this fetch. Separated from the fetch functions so
    tests can stand in a fake transport."""
    opener = urllib.request.build_opener(
        _FencedRedirectHandler(allow_local=allow_local),
        _PinnedHTTPHandler(allow_local=allow_local),
        _PinnedHTTPSHandler(allow_local=allow_local))
    return opener.open(request, timeout=timeout)

# W6: the browser names its own repo (the old string said Code
# Forge's). The version tracks the package — bump it with the
# version bump.
USER_AGENT = "ForgeBrowser/0.9 (Forge Browser; +https://github.com/BROKE-N-BORED/FORGE-BROWSER-)"
DEFAULT_TIMEOUT = 20  # seconds

# W5: the full-disk guard — a download stops cleanly when free
# space on the destination filesystem drops below this floor.
# NOT a size cap (Joshua's no-cap order stands): it stops the
# machine filling its disk, not the download being big.
MIN_FREE_DISK_BYTES = 256 * 1024 * 1024

# W8: stall detection — a download that moves slower than this
# over a whole window is a dead connection, not a slow one. The
# per-read socket timeout already kills a fully stalled read; the
# window catches the dribble (1 byte every 19 s, forever).
STALL_WINDOW_SECONDS = 60.0
STALL_MIN_BYTES = 1024

# 32 MiB in memory (pages, PDFs, text). On an 8 GB machine the
# browser + model already hold most of the RAM; no single read into
# memory may add more. Downloads to disk have NO cap (Joshua's
# call, 2026-11-11) — the disk is the limit, not the browser.
MAX_BODY_BYTES = 32 * 1024 * 1024
_CHUNK = 65536


def _free_bytes(path):
    """Free bytes on the filesystem holding path (W5)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    return shutil.disk_usage(directory).free


def _min_free_bytes():
    """The W5 floor, overridable for tests (a huge floor trips
    the guard on any download)."""
    try:
        return int(os.environ.get("FORGE_BROWSER_MIN_FREE_BYTES", "")
                   or MIN_FREE_DISK_BYTES)
    except ValueError:
        return MIN_FREE_DISK_BYTES


def _check_free_space(path):
    """The W5 full-disk guard: refuse (before the next write)
    when free space on the destination filesystem is under the
    floor. Raises BrowserError; the caller removes the partial."""
    floor = _min_free_bytes()
    free = _free_bytes(path)
    if free < floor:
        raise BrowserError(
            f"stopped the download — the disk is nearly full "
            f"({free // (1024 * 1024)} MB free, keeping "
            f"{floor // (1024 * 1024)} MB in reserve so the machine "
            "keeps working)")


def _remove_partial(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _refuse_too_big(url, size, cap, kind="download"):
    # W7: a page fetch says "page", not "download" — the old
    # message told the person a page fetch was a download.
    if kind == "page":
        raise BrowserError(
            f"that page is too big to read into memory ({size} bytes, "
            f"over the {cap} byte limit) — it was not read, so it "
            "cannot fill up the machine's memory")
    raise BrowserError(
        f"that download is too big for Forge Browser ({size} bytes, "
        f"over the {cap} byte limit) — it was not downloaded, so it "
        "cannot fill up the machine's memory")


def _precheck_length(headers, max_bytes, url, kind="download"):
    """Refuse from the declared Content-Length alone, before any
    body bytes are read. max_bytes None means no cap — nothing
    to refuse."""
    if headers is None or max_bytes is None:
        return
    length = headers.get("Content-Length")
    if length is None:
        return
    try:
        declared = int(length)
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        _refuse_too_big(url, declared, max_bytes, kind)


def _iter_chunks(response, max_bytes, url, kind="download",
                 stall_window=STALL_WINDOW_SECONDS,
                 stall_min_bytes=STALL_MIN_BYTES):
    """Yield body chunks, enforcing the cap on the bytes the caller
    keeps — decompressed bytes when the server sent gzip. max_bytes
    None means no cap: every chunk is yielded. A window that moves
    slower than stall_min_bytes in stall_window seconds is a dead
    connection, not a slow one (W8) — the download aborts instead
    of dribbling forever. stall_window None disables the check."""
    gzipped = response.headers.get("Content-Encoding") == "gzip"
    decoder = zlib.decompressobj(31) if gzipped else None
    total = 0
    window_start = time.monotonic()
    window_bytes = 0
    while True:
        raw = response.read(_CHUNK)
        if not raw:
            break
        try:
            chunk = decoder.decompress(raw) if decoder else raw
        except zlib.error as exc:
            raise BrowserError(
                f"the site sent a {kind} Forge Browser could not "
                f"decompress ({url})") from exc
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            _refuse_too_big(url, total, max_bytes, kind)
        window_bytes += len(chunk)
        if (stall_window is not None
                and time.monotonic() - window_start >= stall_window):
            if window_bytes < stall_min_bytes:
                raise BrowserError(
                    f"the {kind} stalled — only {window_bytes} bytes "
                    f"arrived in {int(stall_window)}s, so the "
                    "connection looks dead, not slow")
            window_start = time.monotonic()
            window_bytes = 0
        if chunk:
            yield chunk
    if decoder is not None:
        try:
            tail = decoder.flush()
        except zlib.error as exc:
            raise BrowserError(
                f"the site sent a {kind} Forge Browser could not "
                f"decompress ({url})") from exc
        total += len(tail)
        if max_bytes is not None and total > max_bytes:
            _refuse_too_big(url, total, max_bytes, kind)
        if tail:
            yield tail


def _request(url):
    return urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*"},
    )


def http_head(url, timeout=DEFAULT_TIMEOUT, allow_local=False):
    """HEAD ``url``; return (final_url, headers). No body is read —
    for callers that only need the kind and size (link previews,
    task L1: a media link is named from its headers, never
    downloaded). Raises BrowserError in plain words on failure.
    Local addresses are refused unless allow_local (task #20)."""
    check_fetch_url(url, allow_local=allow_local)
    request = urllib.request.Request(
        url, method="HEAD",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _urlopen(request, timeout, allow_local) as response:
            return response.geturl(), response.headers
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"the site answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BrowserError(f"could not reach {url} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise BrowserError(f"timed out after {timeout}s reading {url}") from exc


def http_get(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BODY_BYTES,
             allow_local=False, stall_window=STALL_WINDOW_SECONDS,
             stall_min_bytes=STALL_MIN_BYTES):
    """GET ``url``; return (final_url, headers, body_bytes).

    The body is read in chunks and refused over max_bytes.
    Raises BrowserError with a plain-words message on any failure.
    Local addresses are refused unless allow_local (task #20).
    """
    check_fetch_url(url, allow_local=allow_local)
    try:
        with _urlopen(_request(url), timeout, allow_local) as response:
            _precheck_length(response.headers, max_bytes, url, kind="page")
            body = b"".join(_iter_chunks(
                response, max_bytes, url, kind="page",
                stall_window=stall_window, stall_min_bytes=stall_min_bytes))
            return response.geturl(), response.headers, body
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"the site answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BrowserError(f"could not reach {url} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise BrowserError(f"timed out after {timeout}s reading {url}") from exc
    except (http.client.HTTPException, ConnectionError) as exc:
        # A connection that drops mid-read surfaces raw (a reset
        # is RemoteDisconnected, a short body is IncompleteRead) —
        # still a network failure, in plain words (W1's class).
        raise BrowserError(
            f"the connection dropped while reading {url} ({exc})") from exc


def http_get_to_file(url, path, timeout=DEFAULT_TIMEOUT, max_bytes=None,
                     allow_local=False, resume=False, on_progress=None,
                     stall_window=STALL_WINDOW_SECONDS,
                     stall_min_bytes=STALL_MIN_BYTES):
    """Stream ``url`` straight to ``path``; RAM stays flat no
    matter the size. Returns (final_url, headers, size_bytes).
    No size limit by default — the disk is the limit (Joshua's
    call, 2026-11-11). An explicit max_bytes still refuses over
    that size and removes the partial file. Raises BrowserError
    in plain words on failure. Local addresses are refused
    unless allow_local (task #20).

    resume (W3): when path already holds part of the file, ask
    the server for the rest (Range) and append — a failed
    download restarts from where it stopped, not from zero. A
    server that ignores Range restarts the file from zero. A
    failed resume keeps its partial file, so the next attempt
    can pick up again; a failed fresh download removes it (W2).
    on_progress (W4), when given, hears (bytes_so_far, total)
    after each chunk — total is None when the server does not
    declare a length. A network failure mid-download says the
    connection dropped, never that the save failed (W1). The
    full-disk guard (W5) stops cleanly when free space runs low,
    and stall detection (W8) aborts a connection that dribbles
    slower than stall_min_bytes in stall_window seconds."""
    check_fetch_url(url, allow_local=allow_local)
    existing = 0
    if resume:
        try:
            existing = os.path.getsize(path)
        except OSError:
            existing = 0
    request = _request(url)
    if existing:
        request.add_header("Range", f"bytes={existing}-")
    try:
        with _urlopen(request, timeout, allow_local) as response:
            status = getattr(response, "status", 200)
            # A 206 whose Content-Range starts where the saved
            # part ends is a real resume; anything else restarts
            # from zero (a server that ignores Range, or a range
            # that does not match the saved part).
            content_range = response.headers.get("Content-Range") or ""
            range_start = None
            if content_range.startswith("bytes "):
                try:
                    range_start = int(content_range[6:].split("-", 1)[0])
                except (TypeError, ValueError):
                    range_start = None
            if existing and status == 206 and range_start == existing:
                if response.headers.get("Content-Encoding") == "gzip":
                    raise BrowserError(
                        "cannot resume a download the server compresses "
                        "(the saved part would not match the rest)")
                mode = "ab"
            else:
                mode = "wb"
                existing = 0
            remaining_cap = (None if max_bytes is None
                             else max(0, max_bytes - existing))
            _precheck_length(response.headers, remaining_cap, url)
            total = None
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    total = existing + int(declared)
                except (TypeError, ValueError):
                    total = None
            size = existing
            try:
                with open(path, mode) as handle:
                    if on_progress is not None:
                        on_progress(size, total)
                    chunks_since_check = 0
                    for chunk in _iter_chunks(
                            response, remaining_cap, url,
                            stall_window=stall_window,
                            stall_min_bytes=stall_min_bytes):
                        # W5: the full-disk guard — every ~1 MiB
                        # (16 chunks), not every chunk.
                        if chunks_since_check == 0:
                            _check_free_space(path)
                        handle.write(chunk)
                        size += len(chunk)
                        chunks_since_check = (chunks_since_check + 1) % 16
                        if on_progress is not None:
                            on_progress(size, total)
            except BrowserError:
                # The cap refusal, the disk guard, and the stall
                # detector all arrive here. A failed resume keeps
                # its partial (the next attempt resumes from it);
                # a failed fresh download removes it (W2).
                if not resume:
                    _remove_partial(path)
                raise
            except (urllib.error.URLError, TimeoutError,
                    http.client.HTTPException, ConnectionError) as exc:
                # W1: URLError and ConnectionError are OSError
                # subclasses — caught before OSError so a dropped
                # connection is reported as a dropped connection,
                # never as a failed save. A reset mid-read is
                # RemoteDisconnected, a short body IncompleteRead:
                # both are network failures too.
                if not resume:
                    _remove_partial(path)
                raise BrowserError(
                    f"the download stopped partway — the connection "
                    f"dropped ({exc})") from exc
            except OSError as exc:
                if not resume:
                    _remove_partial(path)
                raise BrowserError(
                    f"could not save the download ({exc})") from exc
            return response.geturl(), response.headers, size
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and resume:
            raise BrowserError(
                "the server refused to resume this download — the "
                "saved part does not match the file on the server "
                "(delete the partial file to start over)") from exc
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
