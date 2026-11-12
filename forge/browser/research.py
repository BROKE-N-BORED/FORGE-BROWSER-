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
   URL and numbered ONCE — that number is the citation, stable
   across the whole report.
4. THE REPORT. A Markdown document: the questions, what each source
   actually said (an excerpt of its real text, under its citation
   number, with its URL), barriers found on a source (login /
   paywall / captcha are reported, never silently read past), and
   the full source list. The report contains only what the sources
   said — synthesis is the reading model's job, so nothing here
   invents findings.
"""

import html
import re
import threading

from .errors import BrowserError
from .files import fetch_any
from .search import search

_JOB_LOCK = threading.Lock()
_CURRENT_JOB = {"topic": None}


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
                 search_fn=search, fetch_fn=fetch_any, on_progress=None):
    """Run an approved plan; return (report_markdown, sources).

    Sources: [{n, title, url, kind, text, barriers}] — text is the
    source's own words, cut to max_chars_per_source.
    on_progress(text) is called as the run moves — searching,
    reading each source, done — so a person watching knows where
    it is. Research takes minutes, not seconds.
    """
    if not approved:
        raise BrowserError(
            "this research plan has not been approved — show the plan "
            "first and run it only after the go (the plan gate)")
    from .barriers import detect_barriers

    def progress(text):
        if on_progress is not None:
            on_progress(text)

    progress(f"Researching '{plan['topic']}' — up to {plan['max_pages']} sources")
    sources = []
    seen_urls = set()
    failures = []
    for query in plan["queries"]:
        if len(sources) >= plan["max_pages"]:
            break
        try:
            results = search_fn(query, max_results=plan["max_pages"])
        except BrowserError as exc:
            failures.append(f"search {query!r} failed: {exc}")
            continue
        progress(f"Searched '{query}' — {len(results)} result(s)")
        for result in results:
            if len(sources) >= plan["max_pages"]:
                break
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
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
            sources.append({
                "n": len(sources) + 1,
                "title": getattr(found, "title", "") or result.title,
                "url": result.url,
                "kind": found.kind,
                "text": text[:max_chars_per_source],
                "truncated": len(text) > max_chars_per_source,
                "barriers": barriers,
                "query": query,
            })
            progress(f"Reading {len(sources)} of {plan['max_pages']}: "
                     f"{sources[-1]['title'] or sources[-1]['url']}")
    progress(f"Done — {len(sources)} source(s) read" if sources
             else "Done — no sources could be read")
    return build_report(plan, sources, failures), sources


def research_job_running():
    """Topic of the running research job, or None."""
    with _JOB_LOCK:
        return _CURRENT_JOB["topic"]


def start_research_job(topic, queries=None, on_progress=None, on_done=None,
                       search_fn=None, fetch_fn=None):
    """Start a research job in the background. Returns the topic, or
    None when a job is already running (strictly sequential — one
    at a time, no parallel agents). on_done(report, sources, error)
    runs when the job finishes or fails."""
    with _JOB_LOCK:
        if _CURRENT_JOB["topic"] is not None:
            return None
        _CURRENT_JOB["topic"] = topic

    def work():
        try:
            plan = make_plan(topic, queries=queries)
            report, sources = run_research(
                plan, approved=True, on_progress=on_progress,
                search_fn=search_fn or search, fetch_fn=fetch_fn or fetch_any)
            if on_done is not None:
                on_done(report, sources, None)
        except Exception as exc:
            if on_done is not None:
                on_done(None, [], str(exc))
        finally:
            with _JOB_LOCK:
                _CURRENT_JOB["topic"] = None

    threading.Thread(target=work, daemon=True).start()
    return topic


def build_report(plan, sources, failures):
    lines = [f"# Research: {plan['topic']}", "",
             "## Questions", ""]
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
    """Bold + links inside one line, everything else escaped."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
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
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Research: {html.escape(title)}</title><style>"
            f"body{{font-family:sans-serif;margin:24px;max-width:800px;color:#222}}"
            f"h1{{font-size:24px}}h2{{font-size:19px;margin-top:28px}}"
            f"h3{{font-size:16px}}a{{color:#1a5fb4}}</style></head>"
            f"<body>{''.join(parts)}</body></html>")
