"""The morning brief: what yesterday's browsing was about.

Source: the local memory store (#12) — visited pages (with their
text) and history. Built once a day and cached at
~/.forge/brief-YYYY-MM-DD.json; the start page shows the cached
brief, chat answers it on demand ("give me my morning brief"),
and `brief` on the terminal builds it. "Today so far" is a
separate, uncached brief (today keeps changing).

A brain-written summary is used only when a brain is answering;
otherwise the card is the plain list of pages. Nothing is ever
invented: a day with no pages says so. A failed brain call is
NOT cached — the next launch or ask retries instead of keeping
a summary-less brief forever — and neither is a no-brain build
left standing once a brain appears: a cached summary-less brief
is rebuilt when a brain is available (U1). Sensitive URLs never
reach the brief, list or prompt (U2). Old brief files are
pruned to the last 30 days (U8).
"""

import datetime
import json
import os
import time

from .memory import is_sensitive_url, page_texts
from .titles import load_history

BRIEF_PROMPT = ("Summarize what this person was looking into yesterday "
                "in 3 to 5 plain sentences, from the page titles and "
                "snippets below. Say only what the pages show; if the "
                "list is thin, say so in one sentence instead of padding.")

TODAY_BRIEF_PROMPT = ("Summarize what this person has been looking into "
                      "today so far in 3 to 5 plain sentences, from the "
                      "page titles and snippets below. Say only what the "
                      "pages show; if the list is thin, say so in one "
                      "sentence instead of padding.")

BRIEF_KEEP_DAYS = 30
BRIEF_SUMMARY_PAGES = 12
BRIEF_CHARS_PER_PAGE = 600


def brief_path(date=None):
    day = date or datetime.date.today().isoformat()
    return os.path.join(os.path.expanduser("~"), ".forge", f"brief-{day}.json")


def yesterday_date(now=None):
    """Yesterday's date (local time), as YYYY-MM-DD."""
    if now is None:
        today = datetime.date.today()
    else:
        today = datetime.datetime.fromtimestamp(now).date()
    return (today - datetime.timedelta(days=1)).isoformat()


def _day_bounds(now=None):
    """(start, end) epoch seconds for yesterday, local time. The
    end is the next day's start, not start + 86400 — on a DST
    transition day the two differ by an hour (U10)."""
    day = datetime.date.fromisoformat(yesterday_date(now))
    start = time.mktime(day.timetuple())
    end = time.mktime((day + datetime.timedelta(days=1)).timetuple())
    return start, end


def _pages_in_window(start, end):
    """Pages visited in [start, end), newest first, deduped by
    URL. Page-store entries first (they carry text); history
    fills gaps. Sensitive URLs are excluded from both (U2) — the
    page store already excludes them, but the history fallback
    did not."""
    pages = []
    seen = set()
    for page in page_texts():
        at = page.get("at", 0)
        if start <= at < end and page["url"] not in seen:
            if is_sensitive_url(page["url"]):
                continue
            seen.add(page["url"])
            chunks = page.get("chunks") or []
            pages.append({"url": page["url"], "title": page["title"],
                          "at": at, "text": chunks[0] if chunks else ""})
    for entry in load_history():
        at = entry.get("at", 0)
        if start <= at < end and entry.get("url") not in seen:
            if is_sensitive_url(entry["url"]):
                continue
            seen.add(entry["url"])
            pages.append({"url": entry["url"],
                          "title": entry.get("title") or entry["url"],
                          "at": at, "text": ""})
    pages.sort(key=lambda p: -p["at"])
    return pages


def yesterday_pages(now=None):
    """Yesterday's visited pages, newest first, deduped by URL."""
    start, end = _day_bounds(now)
    return _pages_in_window(start, end)


def today_pages(now=None):
    """Today's visited pages so far, newest first, deduped by
    URL (U6)."""
    if now is None:
        today = datetime.date.today()
        end = time.time()
    else:
        today = datetime.datetime.fromtimestamp(now).date()
        end = now
    start = time.mktime(today.timetuple())
    return _pages_in_window(start, end)


def _sample_pages(pages, count):
    """Up to count pages spread evenly across the list (U3) — a
    busy day's summary covers the whole day, not just the newest.
    Order is kept (newest first)."""
    if len(pages) <= count:
        return list(pages)
    picked = []
    seen = set()
    for i in range(count):
        index = round(i * (len(pages) - 1) / (count - 1))
        if index not in seen:
            seen.add(index)
            picked.append(pages[index])
    return picked


def _summarize_pages(pages, session, prompt):
    """The brain's summary of these pages, or None when there is
    no brain or it does not answer. Samples across the day (U3),
    600 chars per page (U4)."""
    has_brain = session is not None and getattr(session, "model", None)
    if not pages or not has_brain:
        return None
    lines = []
    for page in _sample_pages(pages, BRIEF_SUMMARY_PAGES):
        line = f"- {page['title']} ({page['url']})"
        if page.get("text"):
            line += f": {page['text'][:BRIEF_CHARS_PER_PAGE]}"
        lines.append(line)
    try:
        summary = session.ask_once(prompt + "\n\n" + "\n".join(lines))
        return (summary or "").strip() or None
    except Exception:
        return None


def load_brief(date=None):
    try:
        with open(brief_path(date), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and "pages" in data else None
    except (OSError, ValueError):
        return None


def prune_briefs(keep_days=BRIEF_KEEP_DAYS, now=None):
    """Delete brief files older than keep_days (U8). Files whose
    names do not parse as brief dates are left alone. Returns the
    number removed."""
    base = os.path.join(os.path.expanduser("~"), ".forge")
    if now is None:
        today = datetime.date.today()
    else:
        today = datetime.datetime.fromtimestamp(now).date()
    cutoff = today - datetime.timedelta(days=keep_days)
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not (name.startswith("brief-") and name.endswith(".json")):
            continue
        try:
            day = datetime.date.fromisoformat(
                name[len("brief-"):-len(".json")])
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.unlink(os.path.join(base, name))
                removed += 1
            except OSError:
                pass
    return removed


def build_brief(session=None, now=None, force=False):
    """Build (and cache) the brief for yesterday. session is a
    ChatSession whose brain writes the summary; None = plain list.
    force rebuilds even when a brief is cached (U7 refresh).
    Returns {"date", "pages", "summary"}."""
    day = yesterday_date(now)
    has_brain = session is not None and getattr(session, "model", None)
    cached = load_brief(day)
    if cached is not None and not force:
        if cached.get("summary") or not has_brain:
            return cached
        # U1: a summary-less brief cached by a no-brain launch is
        # rebuilt now a brain is here — the plain list showed
        # immediately, and the summary fills in when Ollama comes up.
    pages = yesterday_pages(now=now)
    summary = _summarize_pages(pages, session, BRIEF_PROMPT)
    brief = {"date": day,
             "pages": [{"url": p["url"], "title": p["title"]} for p in pages],
             "summary": summary}
    if summary is None and pages and has_brain:
        # The brain was there but didn't answer — don't cache a
        # summary-less brief; the next launch or ask retries.
        return brief
    try:
        path = brief_path(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(brief, handle)
    except OSError:
        pass
    prune_briefs(now=now)
    return brief


def build_today_brief(session=None, now=None):
    """Today's brief so far (U6) — never cached, since today keeps
    changing. Returns {"date", "pages", "summary"} like build_brief."""
    if now is None:
        day = datetime.date.today().isoformat()
    else:
        day = datetime.datetime.fromtimestamp(now).date().isoformat()
    pages = today_pages(now=now)
    summary = _summarize_pages(pages, session, TODAY_BRIEF_PROMPT)
    return {"date": day,
            "pages": [{"url": p["url"], "title": p["title"]} for p in pages],
            "summary": summary}


def brief_text(brief, when="yesterday"):
    """The brief as plain text, for chat and the terminal.
    when is "yesterday" (default) or "today" — the wording follows."""
    if not brief["pages"]:
        tail = ("no pages today yet." if when == "today"
                else "no pages yesterday.")
        return f"Nothing to brief for {brief['date']} — {tail}"
    head = "Today so far" if when == "today" else "Yesterday"
    lines = [f"{head} ({brief['date']}) you looked at:"]
    for page in brief["pages"]:
        lines.append(f"- {page['title']} — {page['url']}")
    if brief.get("summary"):
        lines.append("")
        lines.append(brief["summary"])
    return "\n".join(lines)
