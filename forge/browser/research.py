"""Item 3 — Research wiring: plan gate, search/read loop, cited report.

Follows the Research-mode shape Joshua ordered on 2026-11-09
(modelled on how Gemini's Deep Research works, rebuilt for Forge):

1. PLAN FIRST. make_plan(topic, queries) drafts the research plan —
   the topic, the exact searches that will run, how many pages will
   be read. Nothing touches the web until a plan exists.
2. THE GATE. run_research refuses a plan that was not approved —
   approval is a human act (the CLI asks; a Forge tool caller sets
   approved=True only after the user says go), matching Forge's
   plan-gate rule everywhere else.
3. THE LOOP. Each query is searched with the keyless backends; the
   top results are read with fetch_any, so a source can be a web
   page, a PDF, or another file type. Sources are deduplicated by
   normalized URL and numbered ONCE — that number is the citation,
   stable across the whole report.
4. THE REPORT. A Markdown document: the questions, what each source
   actually said (an excerpt of its real text, under its citation
   number, with its URL), barriers found on a source (login /
   paywall / captcha are reported, never silently read past), and
   the full source list. The report contains only what the sources
   said — synthesis is the reading model's job, so nothing here
   invents findings.
"""

import html
import os
import re
import threading
import time
import urllib.parse
from types import SimpleNamespace

from .errors import BrowserError
from .files import fetch_any
from .search import search

# Q7: detail levels for the chat ACTION — excerpt length per source.
DETAIL_CHARS = {"brief": 2000, "standard": 4000, "full": 8000}

# Q8: tracking params dropped from a URL before dedup.
_TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "_ga", "mc_cid"}

_JOB_LOCK = threading.Lock()
_CURRENT_JOB = {"topic": None, "cancel": None}
_JOB_QUEUE = []  # jobs waiting their turn, in the order asked


def _normalize_url(url):
    """The dedup key for a URL (Q8): scheme dropped, host
    lowercased, trailing slash stripped, tracking params dropped
    — the same page at http vs https, or with ?utm_source=...,
    is read once. The stored source keeps its original URL."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url
    host = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(
        parsed.query, keep_blank_values=True)
        if not k.startswith("utm_") and k not in _TRACKING_PARAMS]
    key = host + path
    if kept:
        key += "?" + urllib.parse.urlencode(kept)
    return key


def _retrieve_excerpt(query, text, max_chars):
    """The relevant part of a source for this query (Q10) — the
    retrieval excerpt, not the head-cut. Falls back to the head
    when no embeddings model answers (page_context's own
    fallback, with its note stripped — the note is about a page
    search, not a source excerpt)."""
    from .retrieve import FALLBACK_NOTE, page_context
    holder = SimpleNamespace(_retrieve_cache={})
    try:
        excerpt = page_context(holder, query, text)
    except Exception:
        return text[:max_chars]
    if excerpt.startswith(FALLBACK_NOTE):
        excerpt = excerpt[len(FALLBACK_NOTE):].lstrip("\n")
    return excerpt[:max_chars]


def make_plan(topic, queries=None, max_pages=6):
    """Draft a research plan. Pure data — runs no searches."""
    if not topic or not topic.strip():
        raise BrowserError("a research plan needs a topic")
    searches = [q.strip() for q in (queries or []) if q and q.strip()]
    if not searches:
        searches = [topic.strip()]
    return {
        "topic": topic.strip(),
        "queries": searches,
        "max_pages": max_pages,
        "steps": [
            f"Search: {query}" for query in searches
        ] + [f"Read up to {max_pages} sources (pages, PDFs, other files) "
             "and excerpt what each one says, with citations"],
    }


def format_plan(plan):
    lines = [f"Research plan: {plan['topic']}", ""]
    for index, step in enumerate(plan["steps"], 1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines)


def run_research(plan, approved=False, max_chars_per_source=4000,
                 search_fn=search, fetch_fn=fetch_any, on_progress=None,
                 cancel=None, excerpt_fn=None):
    """Run an approved plan; return (report_markdown, sources).

    Sources: [{n, title, url, kind, text, barriers}] — text is the
    source's own words, cut to max_chars_per_source.
    on_progress(text) is called as the run moves — searching,
    fetching each source, reading each source, done — each line
    carrying the elapsed seconds, so a person watching knows
    where it is and how long it has taken. Research takes
    minutes, not seconds.
    cancel is a threading.Event checked before each search and
    each fetch (Q1): when set, the run stops and delivers a
    partial report with a "Stopped early" note. excerpt_fn(query,
    text, max_chars) overrides the per-source excerpt (Q10's
    default is the retrieval excerpt).
    """
    if not approved:
        raise BrowserError(
            "this research plan has not been approved — show the plan "
            "first and run it only after the go (the plan gate)")
    from .barriers import detect_barriers

    started = time.monotonic()
    stopped = False

    def progress(text):
        if on_progress is not None:
            elapsed = int(time.monotonic() - started)
            on_progress(f"{text} ({elapsed}s)")

    def cancelled():
        return cancel is not None and cancel.is_set()

    progress(f"Researching '{plan['topic']}' — up to {plan['max_pages']} sources")
    sources = []
    seen_urls = set()
    failures = []
    for query in plan["queries"]:
        if cancelled():
            stopped = True
            break
        if len(sources) >= plan["max_pages"]:
            break
        try:
            results = search_fn(query, max_results=plan["max_pages"])
        except BrowserError as exc:
            failures.append(f"search {query!r} failed: {exc}")
            continue
        progress(f"Searched '{query}' — {len(results)} result(s)")
        for result in results:
            if cancelled():
                stopped = True
                break
            if len(sources) >= plan["max_pages"]:
                break
            key = _normalize_url(result.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            # Q6: the fetch is announced before it starts — during
            # a slow fetch the label shows this step, not the
            # previous one.
            progress(f"Fetching {result.url}...")
            try:
                found = fetch_fn(result.url)
            except BrowserError as exc:
                failures.append(f"could not read {result.url}: {exc}")
                continue
            text = getattr(found, "text", "") or ""
            # Barriers the fetcher found in the raw HTML (markup
            # markers) plus anything the extracted text still shows
            # (visible phrases) — one kind each, no duplicates. A
            # fetch that never ran detection leaves barriers empty
            # and the text check still catches what it can.
            barriers = list(getattr(found, "barriers", []) or [])
            if found.kind == "html":
                seen_kinds = {b.get("kind") for b in barriers}
                for barrier in detect_barriers(text, result.url):
                    if barrier.get("kind") not in seen_kinds:
                        seen_kinds.add(barrier.get("kind"))
                        barriers.append(barrier)
            if excerpt_fn is not None:
                excerpt = excerpt_fn(query, text, max_chars_per_source)
            else:
                excerpt = _retrieve_excerpt(query, text,
                                            max_chars_per_source)
            sources.append({
                "n": len(sources) + 1,
                "title": getattr(found, "title", "") or result.title,
                "url": result.url,
                "kind": found.kind,
                "text": excerpt,
                "truncated": len(text) > max_chars_per_source,
                "barriers": barriers,
                "query": query,
            })
            progress(f"Reading {len(sources)} of {plan['max_pages']}: "
                     f"{sources[-1]['title'] or sources[-1]['url']}")
        if stopped:
            break
    # A cancel that landed during the last search or fetch is
    # still a stop — the report says so instead of reading done.
    if cancelled():
        stopped = True
    if stopped:
        progress(f"Stopped early — {len(sources)} source(s) read")
    else:
        progress(f"Done — {len(sources)} source(s) read" if sources
                 else "Done — no sources could be read")
    return build_report(plan, sources, failures, stopped=stopped), sources


def research_job_running():
    """Topic of the running research job, or None."""
    with _JOB_LOCK:
        return _CURRENT_JOB["topic"]


def research_queue():
    """Topics waiting in the queue, in the order asked. [] when none."""
    with _JOB_LOCK:
        return [job["topic"] for job in _JOB_QUEUE]


def cancel_research_job():
    """Stop the running research job (Q1) — it delivers a partial
    report with a "Stopped early" note instead of running to
    completion. Queued jobs are untouched; the next one starts
    when the cancelled job winds down. Returns True when a job
    was running."""
    with _JOB_LOCK:
        if _CURRENT_JOB["topic"] is None:
            return False
        event = _CURRENT_JOB.get("cancel")
    if event is None:
        return False
    event.set()
    return True


def _run_job(job):
    """One job on its own thread; starts the next queued job when
    it finishes, so the queue drains itself in order."""

    def work():
        try:
            plan = make_plan(job["topic"], queries=job["queries"],
                             max_pages=job["max_pages"])
            report, sources = run_research(
                plan, approved=True, on_progress=job["on_progress"],
                search_fn=job["search_fn"] or search,
                fetch_fn=job["fetch_fn"] or fetch_any,
                max_chars_per_source=job["max_chars_per_source"],
                cancel=job["cancel"])
            # Q4: the report is saved, so closing its tab does not
            # lose it. A failed save never loses the delivery.
            try:
                save_report(job["topic"], report)
            except OSError:
                pass
            if job["on_done"] is not None:
                job["on_done"](report, sources, None)
        except Exception as exc:
            if job["on_done"] is not None:
                job["on_done"](None, [], str(exc))
        finally:
            with _JOB_LOCK:
                _CURRENT_JOB["topic"] = None
                _CURRENT_JOB["cancel"] = None
                next_job = _JOB_QUEUE.pop(0) if _JOB_QUEUE else None
                if next_job is not None:
                    _CURRENT_JOB["topic"] = next_job["topic"]
                    _CURRENT_JOB["cancel"] = next_job["cancel"]
            if next_job is not None:
                _run_job(next_job)

    threading.Thread(target=work, daemon=True).start()


def start_research_job(topic, queries=None, on_progress=None, on_done=None,
                       search_fn=None, fetch_fn=None, max_pages=6,
                       max_chars_per_source=4000):
    """Start a research job in the background. Returns the topic.
    When a job is already running, the new one waits in the queue
    and starts when the running job finishes — strictly sequential,
    one at a time, no parallel agents. on_done(report, sources,
    error) runs when the job finishes or fails. max_pages caps the
    sources read; max_chars_per_source caps each excerpt (Q7)."""
    job = {"topic": topic, "queries": queries, "on_progress": on_progress,
           "on_done": on_done, "search_fn": search_fn, "fetch_fn": fetch_fn,
           "max_pages": max_pages,
           "max_chars_per_source": max_chars_per_source,
           "cancel": threading.Event()}
    with _JOB_LOCK:
        if _CURRENT_JOB["topic"] is not None:
            _JOB_QUEUE.append(job)
            return topic
        _CURRENT_JOB["topic"] = topic
        _CURRENT_JOB["cancel"] = job["cancel"]
    _run_job(job)
    return topic


def research_dir():
    """Where finished reports are saved (Q4)."""
    override = os.environ.get("FORGE_BROWSER_RESEARCH_DIR", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "research")


def save_report(topic, report):
    """Save a finished report as Markdown; returns the path.
    Same-second saves get -2, -3 suffixes, never overwriting."""
    folder = research_dir()
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-",
                  (topic or "research").lower()).strip("-") or "research"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{slug}.md"
    path = os.path.join(folder, filename)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stamp}-{slug}-{n}.md")
        n += 1
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(report)
    os.replace(tmp, path)
    return path


def list_reports():
    """Saved reports, newest first: [{name, topic, path, at}]."""
    folder = research_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    reports = []
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(folder, name)
        topic = name
        try:
            with open(path, encoding="utf-8") as handle:
                first = handle.readline().strip()
            if first.startswith("# Research:"):
                topic = first[len("# Research:"):].strip() or name
        except OSError:
            continue
        try:
            at = os.path.getmtime(path)
        except OSError:
            at = 0
        reports.append({"name": name, "topic": topic,
                        "path": path, "at": at})
    reports.sort(key=lambda r: r["at"], reverse=True)
    return reports


def build_report(plan, sources, failures, stopped=False):
    lines = [f"# Research: {plan['topic']}", ""]
    if stopped:
        lines.append("Stopped early — this report covers only the "
                     "sources read before the stop.")
        lines.append("")
    lines += ["## Questions", ""]
    for query in plan["queries"]:
        lines.append(f"- {query}")
    lines += ["", "## What the sources say", ""]
    if not sources:
        lines.append("No sources could be read.")
    for source in sources:
        lines.append(f"### [{source['n']}] {source['title'] or source['url']}")
        lines.append(f"Source: {source['url']} ({source['kind']})")
        for barrier in source["barriers"]:
            lines.append(f"Note: {barrier['note']}")
        lines.append("")
        lines.append(source["text"] or "(no readable text)")
        if source["truncated"]:
            lines.append("[... source continues — fetch it directly for the rest]")
        lines.append("")
    if failures:
        lines += ["## Could not be read", ""]
        for failure in failures:
            lines.append(f"- {failure}")
        lines.append("")
    lines += ["## Sources", ""]
    for source in sources:
        lines.append(f"[{source['n']}] {source['url']}")
    return "\n".join(lines).rstrip() + "\n"


def _inline(text):
    """Bold + links inside one line, everything else escaped.
    Bare URLs become links too (Q3) — the report's source lines
    are clickable, not copy-paste. A URL already inside a link
    (after href=", or as link text after >) is left alone."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"(?<![\w=\"'\(>])(https?://[^\s<]+)",
                  r'<a href="\1">\1</a>', text)
    return text


def report_to_html(report):
    """The Markdown report as a readable page, for a tab of the window."""
    title = "Research"
    for line in (report or "").splitlines():
        if line.startswith("# Research:"):
            title = line[len("# Research:"):].strip() or title
            break
    parts = []
    in_list = False
    for line in (report or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("- "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{_inline(stripped[2:])}</li>")
        elif stripped:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<p>{_inline(stripped)}</p>")
    if in_list:
        parts.append("</ul>")
    # Q5: the shared dark page style — the report page matches the
    # app and the comparison page now.
    from .webui import PAGE_STYLE
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Research: {html.escape(title)}</title><style>"
            f"{PAGE_STYLE}"
            f"body{{max-width:800px}}"
            f"h1{{font-size:24px}}h2{{font-size:19px;margin-top:28px}}"
            f"h3{{font-size:16px}}</style></head>"
            f"<body>{''.join(parts)}</body></html>")
