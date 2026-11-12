"""Cross-tab compare: "compare these three" → one table.

Each tab gets ONE small brain call that extracts the same fields
from that tab alone — never one giant prompt with every tab in
it. A field that isn't on the page reads "not listed"; a brain
reply that isn't usable JSON reads "couldn't extract". The table
is rendered in the window itself (native HTML tab) and handed to
the brain as a Markdown table so it can talk about it.
"""

import html
import json

from .retrieve import page_context

DEFAULT_FIELDS = ["price", "key specs"]
_TAB_LIMIT = 6


def extract_fields(session, title, text, fields):
    """One tiny brain call: the same fields from one tab's text."""
    context = page_context(session, " ".join(fields), text)
    prompt = (f"From the page below, extract ONLY these fields as a JSON object "
              f"with exactly these keys: {json.dumps(fields)}. If a field is not "
              f"on the page, use \"not listed\". Reply with the JSON object only, "
              f"no other text.\n\nPage: {title}\n\n{context}")
    reply = (session.ask_once(prompt) or "").strip()
    if reply.startswith("```"):
        reply = reply.strip("`").strip()
        if reply.lower().startswith("json"):
            reply = reply[4:].strip()
    try:
        data = json.loads(reply)
    except (ValueError, TypeError):
        return {field: "couldn't extract" for field in fields}
    if not isinstance(data, dict):
        return {field: "couldn't extract" for field in fields}
    return {field: str(data.get(field, "not listed")) for field in fields}


def compare_tabs(session, tabs, fields, limit=_TAB_LIMIT):
    """One tiny brain call per tab; never one giant prompt."""
    rows = []
    for tab in list(tabs)[:limit]:
        title = tab.get("title") or tab.get("url") or "Untitled"
        values = extract_fields(session, title, tab.get("text") or "", fields)
        rows.append({"title": title, "url": tab.get("url") or "",
                     "fields": values})
    return rows


def _cell(value):
    return str(value).replace("|", "/").replace("\n", " ").strip() or "not listed"


def compare_table_text(rows, fields):
    """The comparison as a Markdown table, for the brain to talk about."""
    if not rows:
        return "Nothing to compare."
    lines = ["| Field | " + " | ".join(_cell(r["title"]) for r in rows) + " |",
             "|" + "---|" * (len(rows) + 1)]
    for field in fields:
        cells = [r["fields"].get(field, "not listed") for r in rows]
        lines.append("| " + _cell(field) + " | " +
                     " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def compare_table_html(rows, fields):
    """The comparison as a page, rendered in a tab of the window."""
    if not rows:
        return ("<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>Comparison</title></head>"
                "<body><p>Nothing to compare.</p></body></html>")
    heads = "".join(f"<th>{html.escape(r['title'])}</th>" for r in rows)
    body = ""
    for field in fields:
        cells = "".join(
            f"<td>{html.escape(str(r['fields'].get(field, 'not listed')))}</td>"
            for r in rows)
        body += f"<tr><th>{html.escape(field)}</th>{cells}</tr>"
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Comparison</title><style>"
            f"body{{font-family:sans-serif;margin:24px;color:#222}}"
            f"table{{border-collapse:collapse}}"
            f"th,td{{border:1px solid #bbb;padding:8px 12px;text-align:left}}"
            f"th{{background:#f0ede6}}</style></head>"
            f"<body><h1>Comparison</h1>"
            f"<table><tr><th>Field</th>{heads}</tr>{body}</table>"
            f"</body></html>")
