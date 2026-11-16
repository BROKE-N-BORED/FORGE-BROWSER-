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

import atexit
import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import urlparse

from .errors import BrowserError
from .retrieve import _cosine

# Caps on what the stores keep (B15): named constants, so the
# windows that show the data can disclose them. The old private
# names stay as aliases.
MEMORY_LIMIT = 200
_MEMORY_LIMIT = MEMORY_LIMIT
_SIMILARITY_FLOOR = 0.5
_MIN_FORGET_NEEDLE = 3  # a shorter text needle never matches (R2)
EMBED_CACHE_LIMIT = 1500  # cached vectors kept on disk (R4)
_EMBED_CACHE_LIMIT = EMBED_CACHE_LIMIT
# Page-memory save delay (G-P14): rapid navigations coalesce into
# one disk write; any read flushes first, so readers never wait.
_PAGE_SAVE_DELAY = 2.0
# Embed-cache journal lines before a full rewrite (G-P15).
_EMBED_JOURNAL_COMPACT = 200

# URL fragments that mark a page sensitive: no memory context there.
SENSITIVE_PATTERNS = (
    "bank", "paypal", "stripe", "login", "signin", "sign-in", "account",
    "checkout", "password", "health", "medical", "patient", "tax", "irs",
    "wallet", "payment", "billing",
)
# G-S2: well-known sensitive hosts — a bank or clinic on a neutral
# domain has no keyword in its URL, so keyword matching alone
# records it as full text. A starter list, matched as the host or
# a parent domain (mail.chase.com matches chase.com); extend it
# as new ones are found. Government sites are covered by suffix
# (.gov/.mil) instead of by name.
SENSITIVE_HOSTS = (
    # Banks and payments
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com",
    "citibank.com", "usbank.com", "capitalone.com", "americanexpress.com",
    "discover.com", "pnc.com", "td.com", "citizensbank.com",
    "fifththird.com", "huntington.com", "regions.com", "key.com",
    "ally.com", "marcus.com", "sofi.com", "paypal.com", "venmo.com",
    "cash.app", "zellepay.com",
    # Health and insurance
    "kaiserpermanente.org", "anthem.com", "unitedhealthcare.com",
    "aetna.com", "cigna.com", "humana.com", "bcbs.com",
    # Government services
    "medicare.gov", "medicaid.gov", "healthcare.gov", "ssa.gov",
    "va.gov", "irs.gov",
)
SENSITIVE_HOST_SUFFIXES = (".gov", ".mil")
# G-S2: phrases in a page's visible text that mark it sensitive
# even when the URL is neutral — an account or checkout page on
# any domain. Over-flagging is safe here: it only means the page
# is not kept for recall.
_SENSITIVE_TEXT_PATTERNS = (
    "account balance", "available balance", "routing number",
    "account number", "credit card", "debit card", "card number",
    "checkout", "place your order", "payment method",
    "billing address", "order summary", "order total",
    "patient portal", "medical record", "health record",
    "prescription", "lab results", "test results",
    "social security", "one-time code", "verification code",
    "tax return", "sign in to your account",
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
    low = (url or "").lower()
    if _SENSITIVE_RE.search(low):
        return True
    # G-S2: a sensitive host on a neutral URL — a known bank,
    # health, or government domain, or any .gov/.mil site.
    host = _site_host(low)
    if not host:
        return False
    if host.endswith(SENSITIVE_HOST_SUFFIXES):
        return True
    return any(host == listed or host.endswith("." + listed)
               for listed in SENSITIVE_HOSTS)


def is_sensitive_text(text):
    """Does this page's visible text look like an account,
    checkout, or health page (G-S2)? Only the first 4,000
    characters are read — enough for the page's own framing,
    cheap on every navigation."""
    if not text:
        return False
    sample = text[:4000].lower()
    return any(phrase in sample for phrase in _SENSITIVE_TEXT_PATTERNS)


# ----------------------------------------------------------------------
# Store plumbing: locking (G-C17) and private writes (G-S7).
# ----------------------------------------------------------------------

_stores_lock = threading.RLock()  # one in-process writer per store
_thread_state = threading.local()  # paths this thread already flocks


def _secure_store_dir(path):
    """Make the store's folder, 0700 when it is ours to set: a
    folder we just created, or the ~/.forge folder itself (G-S7).
    Other existing folders are left alone — a store path under a
    shared directory must not change that directory's mode."""
    folder = os.path.dirname(os.path.abspath(path))
    if not folder:
        return
    created = not os.path.isdir(folder)
    os.makedirs(folder, exist_ok=True)
    home_forge = os.path.join(os.path.expanduser("~"), ".forge")
    if created or folder == home_forge or folder.startswith(home_forge + os.sep):
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass


@contextlib.contextmanager
def _store_lock(path):
    """One writer at a time for a store file (G-C17): the
    in-process lock plus an flock on a sidecar lock file, the same
    pattern as seats._locked_update — the window, the CLI, and the
    per-navigation remember thread can no longer interleave
    load-modify-save and lose entries. Re-entrant in one thread:
    a nested lock on a path this thread already holds skips the
    flock (a second flock on another fd would block forever)."""
    with _stores_lock:
        held = getattr(_thread_state, "flocks", None)
        if held is None:
            held = _thread_state.flocks = {}
        if path in held:
            yield
            return
        try:
            _secure_store_dir(path)
            handle = open(path + ".lock", "w", encoding="utf-8")
        except OSError:
            # No lock file possible — the thread lock above still
            # guards in-process races.
            yield
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            held[path] = handle
            try:
                yield
            finally:
                del held[path]
        finally:
            handle.close()


def _write_json_atomic(path, payload, indent=None):
    """Write JSON to path atomically (tmp + replace), 0600, in a
    0700 folder (G-S7) — these files hold personal facts and full
    page text, like the vault. Raises OSError when the write
    fails; the caller decides whether that is fatal (B15: the
    facts store reports it, the recall stores stay silent)."""
    _secure_store_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=indent)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


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


# G-P18: the per-site list is re-read from disk on every call
# (per tab, per page load, per chat turn) — cache it, keyed on
# the file's mtime + size, so a changed file is re-read at once.
_sites_cache = {"path": None, "key": None, "data": None}


def _load_sites():
    path = memory_sites_path()
    try:
        stat = os.stat(path)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if (key is not None and _sites_cache["path"] == path
            and _sites_cache["key"] == key
            and _sites_cache["data"] is not None):
        data = _sites_cache["data"]
        return {"blocked": list(data["blocked"]),
                "allowed": list(data["allowed"])}
    sites = {"blocked": [], "allowed": []}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            sites = {
                "blocked": [h for h in data.get("blocked", [])
                            if isinstance(h, str) and h],
                "allowed": [h for h in data.get("allowed", [])
                            if isinstance(h, str) and h],
            }
    except (OSError, ValueError):
        pass
    _sites_cache.update({"path": path, "key": key,
                         "data": {"blocked": list(sites["blocked"]),
                                  "allowed": list(sites["allowed"])}})
    return sites


def _save_sites(sites):
    try:
        _write_json_atomic(
            memory_sites_path(),
            {"blocked": sorted(set(sites["blocked"])),
             "allowed": sorted(set(sites["allowed"]))}, indent=2)
    except OSError:
        pass
    _sites_cache.update({"path": None, "key": None, "data": None})


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
    with _store_lock(memory_sites_path()):
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
    with _store_lock(memory_sites_path()):
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
    with _store_lock(memory_sites_path()):
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
    """Write the facts store. Raises BrowserError when the disk
    write fails (B15) — remember() used to report success while
    the write silently failed, so the fact was never kept."""
    try:
        # R1: keep the NEWEST 200 — entries are stored in
        # insertion order, so slicing from the front dropped
        # the fact just remembered. R10: atomic write.
        _write_json_atomic(memory_path(),
                           {"entries": entries[-MEMORY_LIMIT:]}, indent=2)
    except OSError as exc:
        raise BrowserError(
            f"could not save what was remembered ({exc})") from exc


def list_entries():
    """All remembered entries, newest first."""
    return sorted(_load(), key=lambda e: e.get("at", 0), reverse=True)


def remember(text, source="chat"):
    """Store a fact. Returns the entry (the existing one when the
    same text is already remembered). Raises BrowserError when the
    disk write fails (B15) — a lost write is reported, not
    silently called a success."""
    text = (text or "").strip()
    if not text:
        return None
    with _store_lock(memory_path()):
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
    with _store_lock(memory_path()):
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


# _cosine lives in retrieve.py (G-C33) and is imported above —
# this module, retrieve.py, and titles.py each had their own copy.


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
    missing and saves when it added anything.

    Vectors are keyed by model AND text (G-C18): the on-disk file
    holds one section per model name, so stale vectors from a
    replaced embeddings model are never reused. A legacy cache
    file (bare text hashes, no model) is dropped on load — its
    vectors belong to an unknown model.

    New vectors are appended to a journal file (G-P15), one JSON
    line each, instead of re-writing the whole cache (up to 1500
    vectors) on every call that adds entries. The journal is the
    persistence — a fresh cache on the same path reads base +
    journal — and a full rewrite happens only when the journal
    grows past _EMBED_JOURNAL_COMPACT lines."""

    def __init__(self, path=None):
        self.path = path or embed_cache_path()
        self._data = None
        self._journal_lines = 0

    @property
    def _journal_path(self):
        return self.path + ".journal"

    def _load(self):
        if self._data is not None:
            return self._data
        self._data = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            for model, section in data.items():
                # A section is model -> {hash: vector}. A bare
                # list value is a legacy flat entry (hash ->
                # vector, no model) — dropped, not trusted.
                if isinstance(model, str) and isinstance(section, dict):
                    kept = {}
                    for key, vector in section.items():
                        if isinstance(key, str) and isinstance(vector, list):
                            try:
                                kept[key] = [float(v) for v in vector]
                            except (TypeError, ValueError):
                                continue
                    if kept:
                        self._data[model] = kept
        try:
            with open(self._journal_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                        model = entry["m"]
                        key = entry["k"]
                        vector = [float(v) for v in entry["v"]]
                    except (ValueError, KeyError, TypeError):
                        continue  # a torn last line costs a re-embed
                    self._journal_lines += 1
                    self._data.setdefault(model, {})[key] = vector
        except OSError:
            pass
        return self._data

    def _save(self):
        data = self._data or {}
        trimmed = {}
        for model, section in data.items():
            if len(section) > EMBED_CACHE_LIMIT:
                # Keep the newest entries (dicts keep insertion order).
                section = dict(list(section.items())[-EMBED_CACHE_LIMIT:])
            trimmed[model] = section
        self._data = trimmed
        try:
            _write_json_atomic(self.path, {model: {key: [round(v, 4) for v in vector]
                                                   for key, vector in section.items()}
                                           for model, section in trimmed.items()})
        except OSError:
            return
        try:
            os.unlink(self._journal_path)
        except OSError:
            pass
        self._journal_lines = 0

    def _append_journal(self, model, added):
        """Append new (hash, vector) pairs to the journal, so a
        fresh cache sees them without a full rewrite (G-P15)."""
        if not added:
            return
        try:
            # The folder may not exist yet on a fresh install — the
            # old _save made it, and without it this append fails
            # silently and the vectors are never persisted.
            _secure_store_dir(self._journal_path)
            with open(self._journal_path, "a", encoding="utf-8") as handle:
                for key, vector in added:
                    handle.write(json.dumps(
                        {"m": model, "k": key,
                         "v": [round(v, 4) for v in vector]}) + "\n")
                    self._journal_lines += 1
            try:
                os.chmod(self._journal_path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def vectors_for(self, texts, embed_fn, model=None):
        """Vectors for texts, in order — cached ones reused, missing
        ones embedded in one call and saved. Raises whatever
        embed_fn raises when nothing is answering. model names the
        embeddings model the vectors belong to (G-C18); the default
        is the configured embed model, which is what embed_texts
        uses when no model is named."""
        if model is None:
            from .retrieve import embed_model
            model = embed_model()
        data = self._load()
        section = data.setdefault(model, {})
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest()
                for t in texts]
        missing = [t for t, key in zip(texts, keys) if key not in section]
        if missing:
            vectors = embed_fn(missing)
            if len(vectors) != len(missing):
                raise ValueError(
                    "the embeddings model answered oddly (wrong count)")
            added = []
            for text, vector in zip(missing, vectors):
                key = hashlib.sha256(text.encode("utf-8")).hexdigest()
                section[key] = list(vector)
                added.append((key, list(vector)))
            self._append_journal(model, added)
            if self._journal_lines >= _EMBED_JOURNAL_COMPACT:
                self._save()
        return [section[key] for key in keys]


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

PAGE_MEMORY_LIMIT = 100
_PAGE_MEMORY_LIMIT = PAGE_MEMORY_LIMIT
PAGE_CHUNKS_LIMIT = 24  # R5: was 12 — a long page kept only its head
_PAGE_CHUNKS_LIMIT = PAGE_CHUNKS_LIMIT
_EMBED_BATCH = 200


def page_memory_path():
    override = os.environ.get("FORGE_BROWSER_PAGE_MEMORY_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "page-memory.json")


# G-P14: pending page saves, per store path — remember_page
# records the new list here and a timer writes it a moment
# later, so rapid navigations coalesce into one disk write
# instead of rewriting megabytes of JSON per page. Any read
# flushes first (see _load_pages), so readers never see stale
# data and a direct file read after any list/search call finds
# the write already done. A clean exit flushes too (atexit).
_pages_pending = {}  # path -> {"pages": [...], "timer": Timer|None}
_pages_pending_lock = threading.Lock()


def _flush_pages(path):
    """Write one path's pending pages now. Skips the write when
    the folder is gone (a test's temp dir cleaned up before the
    timer fired) instead of recreating it. Holds the store lock
    across pop + write, so a concurrent remember_page builds on
    either the pending list or the flushed file — never on a
    file the flush has popped but not yet written."""
    with _store_lock(path):
        with _pages_pending_lock:
            pending = _pages_pending.pop(path, None)
        if pending is None:
            return
        if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
            return
        _save_pages(pending["pages"], path=path)


def _flush_all_pages():
    """Write every pending page list (clean exit)."""
    with _pages_pending_lock:
        paths = list(_pages_pending)
    for path in paths:
        _flush_pages(path)


atexit.register(_flush_all_pages)


def flush_page_memory():
    """Write any pending page saves now (tests and shutdown paths
    that need the file on disk at once)."""
    _flush_all_pages()


def _load_pages():
    # A pending save lands first, so this read — and any direct
    # file read after it — sees the latest pages.
    _flush_pages(page_memory_path())
    try:
        with open(page_memory_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        pages = data.get("pages", []) if isinstance(data, dict) else []
        return [p for p in pages
                if isinstance(p, dict) and p.get("url") and p.get("chunks")]
    except (OSError, ValueError):
        return []


def _load_pages_merged():
    """Pages from disk plus any pending save, without flushing —
    remember_page's view of the store, so a burst of navigations
    does not pay a full write per page."""
    with _pages_pending_lock:
        pending = _pages_pending.get(page_memory_path())
        if pending is not None:
            return [dict(p, chunks=list(p["chunks"]))
                    for p in pending["pages"]]
    return _load_pages()


def _save_pages(pages, path=None):
    """Write the page store. path is the store file to write —
    _flush_pages passes the pending path, so a flush never lands
    in a different home's file when the home moved between the
    remember and the flush (G-P14)."""
    try:
        _write_json_atomic(path or page_memory_path(),
                           {"pages": pages[:PAGE_MEMORY_LIMIT]})
    except OSError:
        pass


def remember_page(url, title, text):
    """Keep a visited page's chunks for later recall. Returns True
    when stored. Silently skips sensitive pages, local pages, and
    pages too thin to be worth remembering. The disk write is
    coalesced (G-P14): the page is kept at once and the file
    written a moment later, or on the next read — a crash inside
    that window loses the page, not the facts store."""
    from .retrieve import chunk_text

    url = (url or "").strip()
    text = (text or "").strip()
    if not url.startswith(("http://", "https://")):
        return False  # about:blank, data:, and other non-web pages
    if not site_memory_allowed(url) or url.startswith("http://127.0.0.1"):
        return False
    if is_sensitive_text(text):
        return False  # G-S2: an account/checkout page on any domain
    if len(text) < 200:
        return False
    chunks = chunk_text(text)[:PAGE_CHUNKS_LIMIT]
    if not chunks:
        return False
    with _store_lock(page_memory_path()):
        pages = [p for p in _load_pages_merged() if p.get("url") != url]
        pages.insert(0, {"url": url, "title": (title or url).strip(),
                         "at": time.time(), "chunks": chunks})
        pages = pages[:PAGE_MEMORY_LIMIT]
        with _pages_pending_lock:
            old = _pages_pending.get(page_memory_path())
            if old is not None and old.get("timer") is not None:
                old["timer"].cancel()
            path = page_memory_path()
            timer = threading.Timer(_PAGE_SAVE_DELAY, _flush_pages,
                                    args=[path])
            timer.daemon = True
            _pages_pending[path] = {"pages": pages, "timer": timer}
            timer.start()
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
    from .retrieve import cosine_scores, embed_texts

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
    # G-P16: one cosine call scores every chunk with the query
    # norm computed once — the old loop recomputed it per chunk.
    for (page, chunk), score in zip(
            flat, cosine_scores(query_vector, vectors)):
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
    with _store_lock(page_memory_path()):
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
