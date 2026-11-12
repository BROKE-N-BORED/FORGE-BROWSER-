"""The morning brief: what yesterday's browsing was about.

Source: the local memory store (#12) — visited pages (with their
text) and history. Built once a day and cached at
~/.forge/brief-YYYY-MM-DD.json; the start page shows the cached
brief, chat answers it on demand ("give me my morning brief"),
and `brief` on the terminal builds it.

A brain-written summary is used only when a brain is answering;
otherwise the card is the plain list of pages. Nothing is ever
invented: a day with no pages says so. A failed brain call is
NOT cached — the next launch or ask retries instead of keeping
a summary-less brief forever.
"""

import datetime
import json
import os
import time

from .memory import page_texts
from .titles import load_history

BRIEF_PROMPT = ("Summarize what this person was looking into yesterday "
                "in 3 to 5 plain sentences, from the page titles and "
                "snippets below. Say only what the pages show; if the "
                "list is thin, say so in one sentence instead of padding.")


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
    """(start, end) epoch seconds for yesterday, local time."""
    day = datetime.date.fromisoformat(yesterday_date(now))
    start = time.mktime(day.timetuple())
    return start, start + 86400


def yesterday_pages(now=None):
    """Yesterday's visited pages, newest first, deduped by URL.
    Page-store entries first (they carry text); history fills gaps."""
    start, end = _day_bounds(now)
    pages = []
    seen = set()
    for page in page_texts():
        at = page.get("at", 0)
        if start <= at < end and page["url"] not in seen:
            seen.add(page["url"])
            chunks = page.get("chunks") or []
            pages.append({"url": page["url"], "title": page["title"],
                          "at": at, "text": chunks[0] if chunks else ""})
    for entry in load_history():
        at = entry.get("at", 0)
        if start <= at < end and entry.get("url") not in seen:
            seen.add(entry["url"])
            pages.append({"url": entry["url"],
                          "title": entry.get("title") or entry["url"],
                          "at": at, "text": ""})
    pages.sort(key=lambda p: -p["at"])
    return pages


def load_brief(date=None):
    try:
        with open(brief_path(date), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and "pages" in data else None
    except (OSError, ValueError):
        return None


def build_brief(session=None, now=None):
    """Build (and cache) the brief for yesterday. session is a
    ChatSession whose brain writes the summary; None = plain list.
    Returns {"date", "pages", "summary"}."""
    day = yesterday_date(now)
    cached = load_brief(day)
    if cached is not None:
        return cached
    pages = yesterday_pages(now=now)
    summary = None
    has_brain = session is not None and getattr(session, "model", None)
    if pages and has_brain:
        lines = []
        for page in pages[:12]:
            line = f"- {page['title']} ({page['url']})"
            if page.get("text"):
                line += f": {page['text'][:300]}"
            lines.append(line)
        try:
            summary = session.ask_once(BRIEF_PROMPT + "\n\n" + "\n".join(lines))
            summary = (summary or "").strip() or None
        except Exception:
            summary = None
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
    return brief


def brief_text(brief):
    """The brief as plain text, for chat and the terminal."""
    if not brief["pages"]:
        return (f"Nothing to brief for {brief['date']} — "
                "no pages yesterday.")
    lines = [f"Yesterday ({brief['date']}) you looked at:"]
    for page in brief["pages"]:
        lines.append(f"- {page['title']} — {page['url']}")
    if brief.get("summary"):
        lines.append("")
        lines.append(brief["summary"])
    return "\n".join(lines)
