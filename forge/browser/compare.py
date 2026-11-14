"""Cross-tab compare: "compare these three" → one table.

Each tab gets ONE small brain call that extracts the same fields
from that tab alone — never one giant prompt with every tab in
it. A field that isn't on the page reads "not listed"; a brain
reply that isn't usable JSON reads "couldn't extract". The table
is rendered in the window itself (native HTML tab, dark like the
app) and handed to the brain as a Markdown table so it can talk
about it. The last comparison is kept so the tab can offer
refresh and CSV/Markdown export (M11).
"""

import csv
import hashlib
import html
import io
import json
import re
import urllib.parse

from .retrieve import page_context

DEFAULT_FIELDS = ["price", "key specs"]
_TAB_LIMIT = 6
_CACHE_LIMIT = 50

# Extraction cache (M9): (sha256 of the tab text, fields) → values.
# A repeat compare of unchanged tabs costs no brain calls.
_EXTRACTION_CACHE = {}

# The last comparison, for the tab's refresh/export links (M11).
_LAST_COMPARISON = {"rows": [], "fields": list(DEFAULT_FIELDS)}

# The local server's base URL (set by the window at startup), so
# the comparison page's export/refresh links are absolute.
_SERVER_BASE = ""


def clear_extraction_cache():
    _EXTRACTION_CACHE.clear()


def set_server_base(base_url):
    global _SERVER_BASE
    _SERVER_BASE = base_url or ""


def set_last_comparison(rows, fields):
    _LAST_COMPARISON["rows"] = list(rows)
    _LAST_COMPARISON["fields"] = list(fields)


def last_comparison():
    return list(_LAST_COMPARISON["rows"]), list(_LAST_COMPARISON["fields"])


def _parse_json_object(reply):
    """A JSON object out of a brain reply (M1): the whole reply,
    else the first {...} block — small models preamble."""
    try:
        data = json.loads(reply)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    start = reply.find("{")
    end = reply.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(reply[start:end + 1])
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    for match in re.finditer(r"\{.*?\}", reply, re.DOTALL):
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_fields(session, title, text, fields):
    """One tiny brain call: the same fields from one tab's text."""
    key = (hashlib.sha256((text or "").encode("utf-8", "ignore"))
           .hexdigest(), tuple(fields))
    if key in _EXTRACTION_CACHE:
        return dict(_EXTRACTION_CACHE[key])
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
    data = _parse_json_object(reply)
    if data is None:
        # A failed parse is model sloppiness, not a fact about the
        # page — never cached, so a retry gets a fresh chance.
        return {field: "couldn't extract" for field in fields}
    # M2: match keys case-insensitively — {"Price": ...} counts.
    lowered = {str(k).lower(): v for k, v in data.items()}
    result = {field: str(lowered.get(field.lower(), "not listed"))
              for field in fields}
    if len(_EXTRACTION_CACHE) >= _CACHE_LIMIT:
        _EXTRACTION_CACHE.pop(next(iter(_EXTRACTION_CACHE)))
    _EXTRACTION_CACHE[key] = dict(result)
    return result


def compare_tabs(session, tabs, fields, limit=_TAB_LIMIT, on_progress=None):
    """One tiny brain call per tab; never one giant prompt.

    Returns (rows, dropped): dropped is how many tabs the limit
    cut off (M4). Cancel is honored between tabs (M6) — the rows
    gathered so far are returned, not thrown away — and
    on_progress(i, total) reports each tab as it starts."""
    rows = []
    chosen = list(tabs)[:limit]
    dropped = max(0, len(tabs) - limit)
    total = len(chosen)
    for index, tab in enumerate(chosen):
        if getattr(session, "_stop_requested", False):
            break
        if on_progress:
            on_progress(index + 1, total)
        title = tab.get("title") or tab.get("url") or "Untitled"
        values = extract_fields(session, title, tab.get("text") or "", fields)
        rows.append({"title": title, "url": tab.get("url") or "",
                     "fields": values})
    return rows, dropped


def parse_fields_from_text(text):
    """Field names from the person's words (M10): "compare on
    price and warranty" → ["price", "warranty"]. Used when the
    model's ACTION omits the fields."""
    match = re.search(r"\b(?:on|by)\s+([^@]+)$", (text or "").lower())
    if not match:
        return []
    fields = []
    for part in re.split(r"\s*(?:,|and|by)\s*", match.group(1)):
        part = part.strip(" .!?")
        if part and len(part) <= 40 and part not in fields:
            fields.append(part)
    return fields[:6]


def _domain(url):
    return urllib.parse.urlparse(url or "").netloc


def _cell(value):
    return str(value).replace("|", "/").replace("\n", " ").strip() or "not listed"


def _table_lines(rows, fields):
    heads = []
    for row in rows:
        domain = _domain(row.get("url"))
        heads.append(f"{row['title']} ({domain})" if domain else row["title"])
    lines = ["| Field | " + " | ".join(_cell(h) for h in heads) + " |",
             "|" + "---|" * (len(rows) + 1)]
    for field in fields:
        cells = [r["fields"].get(field, "not listed") for r in rows]
        lines.append("| " + _cell(field) + " | " +
                     " | ".join(_cell(c) for c in cells) + " |")
    return lines


def compare_table_text(rows, fields, dropped=0, max_chars=None):
    """The comparison as a Markdown table, for the brain to talk about."""
    if not rows:
        return "Nothing to compare."
    shown = list(fields)
    lines = _table_lines(rows, shown)
    left_out = 0
    # M8: when the table is too long for the brain's copy, drop
    # trailing fields until it fits and say so — never truncate
    # mid-table with no note. 120 chars are reserved for the notes.
    if max_chars:
        budget = max(0, max_chars - 120)
        while len("\n".join(lines)) > budget and len(shown) > 1:
            shown.pop()
            left_out += 1
            lines = _table_lines(rows, shown)
    text = "\n".join(lines)
    if left_out:
        text += f"\n({left_out} field(s) left out to fit.)"
    if dropped:
        text += (f"\n({dropped} tab(s) left out. The comparison "
                 f"covers {len(rows)} tabs.)")
    return text


def compare_table_csv(rows, fields):
    """The comparison as CSV, for the export link (M11)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Field"] + [r["title"] for r in rows])
    for field in fields:
        writer.writerow([field] + [str(r["fields"].get(field, "not listed"))
                                    for r in rows])
    return out.getvalue()


def compare_table_html(rows, fields, dropped=0):
    """The comparison as a page, rendered in a tab of the window."""
    if not rows:
        return ("<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>Comparison</title></head>"
                "<body><p>Nothing to compare.</p></body></html>")
    heads = ""
    for row in rows:
        domain = _domain(row.get("url"))
        if domain:
            heads += (f"<th>{html.escape(row['title'])}"
                      f"<br><small>{html.escape(domain)}</small></th>")
        else:
            heads += f"<th>{html.escape(row['title'])}</th>"
    body = ""
    for field in fields:
        cells = "".join(
            f"<td>{html.escape(str(r['fields'].get(field, 'not listed')))}</td>"
            for r in rows)
        body += f"<tr><th>{html.escape(field)}</th>{cells}</tr>"
    note = ""
    if dropped:
        note = (f"<p class=\"note\">{dropped} tab(s) left out. "
                f"The comparison covers {len(rows)} tabs.</p>")
    links = ""
    if _SERVER_BASE:
        links = (f"<p class=\"actions\"><a href=\"{_SERVER_BASE}compare-refresh\">"
                 f"Refresh</a> · <a href=\"{_SERVER_BASE}compare.csv\">"
                 f"Download CSV</a> · <a href=\"{_SERVER_BASE}compare.md\">"
                 f"Download Markdown</a></p>")
    # Q5: the shared dark page style — the comparison page and the
    # research report page match, from one definition.
    from .webui import PAGE_STYLE
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>Comparison</title><style>"
            f"{PAGE_STYLE}"
            f"table{{border-collapse:collapse}}"
            f"th,td{{border:1px solid #3a3f47;padding:8px 12px;text-align:left}}"
            f"th{{background:#171b22}}"
            f"small{{color:#b0aca0;font-weight:normal}}"
            f".note{{color:#b0aca0}}</style></head>"
            f"<body><h1>Comparison</h1>"
            f"<table><tr><th>Field</th>{heads}</tr>{body}</table>"
            f"{note}{links}"
            f"</body></html>")
