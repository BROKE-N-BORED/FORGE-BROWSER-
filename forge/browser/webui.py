"""Forge Browser's own pages, served locally by the browser app.

The start page and the search-results page are part of the browser
itself — served from 127.0.0.1 by this module, rendered by the
browser's own window. Search runs the same keyless chain as the
CLI (no API key, no account). Plain standard library.
"""

import html
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import engines
from .search import search as keyless_search

_STYLE = """
body { font-family: sans-serif; background: #101216; color: #e8e4da;
       margin: 0; padding: 48px 24px; }
.wrap { max-width: 760px; margin: 0 auto; }
h1 { font-size: 34px; letter-spacing: 1px; margin: 0 0 4px; }
h1 .dot { color: #ffb347; }
.sub { color: #9a958a; margin: 0 0 28px; }
form { display: flex; gap: 8px; margin-bottom: 28px; }
input[type=text] { flex: 1; padding: 12px 14px; font-size: 16px;
  background: #171b22; color: #e8e4da; border: 1px solid #2c313a;
  border-radius: 8px; }
button { padding: 12px 20px; font-size: 16px; background: #ffb347;
  color: #14161a; border: 0; border-radius: 8px; cursor: pointer; }
a { color: #ffb347; text-decoration: none; }
a:hover { text-decoration: underline; }
.result { margin: 0 0 20px; }
.result .t { font-size: 17px; }
.result .u { color: #7fae7a; font-size: 13px; word-break: break-all; }
.result .s { color: #b9b4a8; font-size: 14px; margin-top: 2px; }
.status { background: #171b22; border: 1px solid #2c313a;
  border-radius: 8px; padding: 14px 18px; font-size: 14px;
  line-height: 1.7; color: #b9b4a8; white-space: pre-wrap; }
.quick { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 28px; }
.quick a { background: #171b22; border: 1px solid #2c313a;
  border-radius: 16px; padding: 6px 14px; font-size: 14px; }
.brief-box { background: #171b22; border: 1px solid #2c313a;
  border-radius: 8px; padding: 14px 18px; margin-bottom: 28px; }
.brief-box h2 { font-size: 18px; margin: 0 0 8px; }
.brief-box .result { margin-bottom: 12px; }
.tip { color: #9a958a; font-size: 14px; margin: 0 0 28px; }
.tip b { color: #e8e4da; }
"""

# Q5: the one dark page style for the browser's own engine-room
# pages — compare.py and research.py build on this, so the app's
# pages share one look instead of drifting apart.
PAGE_STYLE = ("body{font-family:sans-serif;margin:24px;"
              "background:#101216;color:#e8e4da}"
              "a{color:#ffb347}")


def _when(at):
    """A history timestamp as 'Nov 12 14:02' — '' when missing."""
    if not at:
        return ""
    try:
        return time.strftime("%b %d %H:%M", time.localtime(at))
    except (TypeError, ValueError, OverflowError):
        return ""


def history_page(query=None, cleared=False):
    """The full history view (N11): every visit with its timestamp,
    a search box, and a clear-history control."""
    from .titles import load_history
    entries = load_history()
    total = len(entries)
    if query:
        lowered = query.lower()
        entries = [e for e in entries
                   if lowered in (e.get("title") or "").lower()
                   or lowered in (e.get("url") or "").lower()]
    note = ""
    if cleared:
        note = ('<p style="color:#7fae7a;">History cleared.</p>')
    if query:
        note += (f'<p class="sub">{len(entries)} of {total} visits '
                 f'match "{html.escape(query)}".</p>')
    elif total:
        note += f'<p class="sub">{total} visits, newest first.</p>'
    rows = []
    for entry in entries:
        rows.append(
            f'<div class="result"><a class="t" href="{html.escape(entry["url"])}">'
            f"{html.escape(entry.get('title') or entry['url'])}</a>"
            f'<div class="u">{html.escape(entry["url"])}</div>'
            f'<div class="s">{html.escape(_when(entry.get("at")))}</div></div>')
    body = "\n".join(rows) or "<p>No history yet. Pages you visit show up here.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>History · Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>History</h1>
<p class="sub"><a href="/">← Start page</a> · <a href="/history-clear">Clear history…</a></p>
<form action="/history" method="get">
<input type="text" name="q" value="{html.escape(query or "")}" placeholder="Search history">
<button type="submit">Search</button>
</form>
{note}
{body}
</div></body></html>"""


def history_clear_page():
    """The clear-history confirmation (N3/N11) — a destructive
    action gets a second look, not a one-click wipe."""
    from .titles import load_history
    count = len(load_history())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Clear history · Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>Clear history?</h1>
<p>This deletes all {count} recorded visits. It cannot be undone.</p>
<p><a href="/history-clear?confirm=1">Yes, clear it</a> &nbsp;·&nbsp;
<a href="/history">Cancel</a></p>
</div></body></html>"""


def start_page():
    from .bookmarks import list_bookmarks
    from .brief import load_brief, today_pages, yesterday_date
    from .titles import grouped_history, journeys
    status = html.escape(engines.format_status(engines.engine_status()))
    # Quick links: the person's bookmarks, first thing on the page.
    quick = ""
    marks = list_bookmarks()[:8]
    if marks:
        links = "".join(
            f'<a href="{html.escape(b["url"])}">'
            f"{html.escape(b.get('title') or b['url'])}</a>"
            for b in marks)
        quick = (f'<h2 style="font-size:18px;margin:0 0 8px;">Quick links</h2>'
                 f'<div class="quick">{links}</div>')
    day = yesterday_date()
    cached = load_brief(day)
    if cached is None:
        brief = ('<h2>Morning brief</h2>'
                 '<p class="sub">No brief yet for yesterday. It builds in the '
                 'background after launch, or ask for it in chat.</p>')
    elif not cached["pages"]:
        brief = ('<h2>Morning brief</h2>'
                 f'<p class="sub">Nothing to brief for {html.escape(day)}. '
                 'No pages yesterday.</p>')
    else:
        parts = ['<h2>Morning brief</h2>']
        if cached.get("summary"):
            parts.append(f'<p>{html.escape(cached["summary"])}</p>')
        # U5: every page, not the first 6 — the start page matches
        # the CLI and chat lists.
        for page in cached["pages"]:
            parts.append(
                f'<div class="result"><a class="t" href="{html.escape(page["url"])}">'
                f"{html.escape(page['title'])}</a>"
                f'<div class="u">{html.escape(page["url"])}</div></div>')
        brief = "\n".join(parts)
    # U7: rebuild the brief on demand — a bad summary re-rolls
    # without deleting the file by hand.
    brief += ('<p><a href="/brief-refresh">Refresh brief ↻</a></p>')
    brief = f'<div class="brief-box">{brief}</div>'
    # U6: today so far, alongside yesterday — a list only,
    # computed live (today keeps changing, so it is not cached).
    today = today_pages()
    today_html = ""
    if today:
        parts = ['<h2 style="font-size:18px;margin:28px 0 8px;">'
                 'Today so far</h2>']
        for page in today:
            parts.append(
                f'<div class="result"><a class="t" href="{html.escape(page["url"])}">'
                f"{html.escape(page['title'])}</a>"
                f'<div class="u">{html.escape(page["url"])}</div></div>')
        today_html = "\n".join(parts)
    recent = ""
    groups = grouped_history()
    if groups:
        parts = ['<h2 style="font-size:18px;margin:28px 0 8px;">Recent</h2>']
        for group in groups[:4]:
            parts.append(f'<p style="color:#9a958a;margin:12px 0 4px;">'
                         f'{html.escape(group["name"])}</p>')
            for entry in group["items"][:4]:
                parts.append(
                    f'<div class="result"><a class="t" href="{html.escape(entry["url"])}">'
                    f"{html.escape(entry.get('title') or entry['url'])}</a>"
                    f'<div class="u">{html.escape(entry["url"])}</div></div>')
        # N11: the full history view is one click away.
        parts.append('<p><a href="/history">See all history →</a></p>')
        recent = "\n".join(parts)
    # N2: journeys — browsing sessions, resumable by clicking any
    # of their pages back open.
    journey_html = ""
    found = journeys()
    if found:
        parts = ['<h2 style="font-size:18px;margin:28px 0 8px;">Journeys</h2>']
        for journey in found[:4]:
            span = _when(journey["started"])
            if journey["ended"] != journey["started"]:
                span += f" – {_when(journey['ended'])}"
            parts.append(
                f'<p style="color:#9a958a;margin:12px 0 4px;">'
                f'{html.escape(journey["name"])} · {html.escape(span)}</p>')
            for entry in journey["items"][:6]:
                parts.append(
                    f'<div class="result"><a class="t" href="{html.escape(entry["url"])}">'
                    f"{html.escape(entry.get('title') or entry['url'])}</a>"
                    f'<div class="u">{html.escape(entry["url"])}</div></div>')
            extra = len(journey["items"]) - 6
            if extra > 0:
                parts.append(f'<p class="sub">+{extra} more in this journey ('
                             f'<a href="/history">see all history</a>).</p>')
        journey_html = "\n".join(parts)
    # Q4: saved research reports — the latest few, one click from
    # the full list. Nothing shows until a report exists.
    research_html = ""
    from .research import list_reports
    reports = list_reports()
    if reports:
        parts = ['<h2 style="font-size:18px;margin:28px 0 8px;">Research</h2>']
        for report in reports[:4]:
            parts.append(
                f'<div class="result"><a class="t" href="/research/'
                f'{html.escape(report["name"], quote=True)}">'
                f"{html.escape(report['topic'])}</a>"
                f'<div class="u">{html.escape(_when(report["at"]))}</div></div>')
        parts.append('<p><a href="/research">See all reports →</a></p>')
        research_html = "\n".join(parts)
    # U22: a first run shows the way in — no history and nothing
    # today yet means the person has never used the browser.
    howto = ""
    if not groups and not today:
        howto = ('<p class="tip">First time here? '
                 '<a href="/howto">How to use Forge Browser</a></p>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>FORGE BROWSER<span class="dot">.</span></h1>
<p class="sub">Your browser. No accounts, no API keys, nothing tracked.</p>
<p class="tip">Tip: in chat, write <b>@tab2</b> to ask about tab 2, or compare tabs by number.</p>
{howto}
<form action="/search" method="get">
<input type="text" name="q" placeholder="Search the web, or type an address up top" autofocus>
<button type="submit">Search</button>
</form>
{quick}
{brief}
{today_html}
{recent}
{journey_html}
{research_html}
<div class="status">{status}</div>
</div></body></html>"""


def howto_page():
    """U22: the How to Use index as a page — BROWSER.md's headings
    as links, each section's text under its heading. The start
    page's first-run link and the Help menu's window both lead
    here (the window shows the same sections natively)."""
    from .howto import load_sections
    sections = load_sections()
    if not sections:
        body = ("<p>The manual (BROWSER.md) is not installed with "
                "this copy of Forge Browser, so there is nothing to "
                "show here. Reinstalling puts it back.</p>")
    else:
        index = "".join(
            f'<div style="margin:4px 0 4px {(level - 1) * 18}px;">'
            f'<a href="#s{i}">{html.escape(title)}</a></div>'
            for i, (level, title, _body) in enumerate(sections))
        blocks = []
        for i, (level, title, text) in enumerate(sections):
            tag = "h2" if level <= 2 else "h3"
            blocks.append(
                f'<{tag} id="s{i}">{html.escape(title)}</{tag}>'
                f"<pre>{html.escape(text)}</pre>")
        body = (f"<h2>Index</h2>{index}"
                + "\n".join(blocks))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>How to Use · Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>How to Use</h1>
<p class="sub"><a href="/">← Start page</a></p>
{body}
</div></body></html>"""


def research_list_page():
    """Q4: every saved research report, newest first."""
    from .research import list_reports
    reports = list_reports()
    if not reports:
        body = ("<p>No research reports saved yet. Ask the browser "
                "to research something and the report lands here.</p>")
    else:
        parts = []
        for report in reports:
            when = _when(report["at"])
            parts.append(
                f'<div class="result"><a class="t" href="/research/'
                f'{html.escape(report["name"], quote=True)}">'
                f"{html.escape(report['topic'])}</a>"
                f'<div class="u">{html.escape(when)}</div></div>')
        body = "\n".join(parts)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Research reports · Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>Research reports</h1>
<p class="sub"><a href="/">← Start page</a></p>
{body}
</div></body></html>"""


def research_report_page(name):
    """Q4: one saved report as HTML, or None when no such report
    exists. The name must match a listed report exactly — no path
    traversal out of the research dir."""
    from .research import list_reports, report_to_html
    for report in list_reports():
        if report["name"] == name:
            with open(report["path"], encoding="utf-8") as handle:
                return report_to_html(handle.read())
    return None


def search_page(query, results=None):
    if results is None:
        results = keyless_search(query, max_results=10)
    items = []
    for result in results:
        items.append(
            f'<div class="result"><a class="t" href="{html.escape(result.url)}">'
            f"{html.escape(result.title or result.url)}</a>"
            f'<div class="u">{html.escape(result.url)}</div>'
            f'<div class="s">{html.escape(result.snippet or "")}</div></div>')
    body = "\n".join(items) or "<p>No results. Every search backend came back empty.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(query)} · Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<form action="/search" method="get">
<input type="text" name="q" value="{html.escape(query)}">
<button type="submit">Search</button>
</form>
{body}
</div></body></html>"""


def resolve_address(text):
    """What the address bar does with typed text.

    Returns ("url", address) for something address-shaped, or
    ("search", query) for words to search with the keyless chain.
    """
    text = (text or "").strip()
    if not text:
        return ("url", "/")
    if "://" in text:
        return ("url", text)
    if " " not in text and ("." in text or text.startswith("localhost")):
        return ("url", "https://" + text)
    return ("search", text)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            content_type = "text/html; charset=utf-8"
            disposition = None
            if parsed.path == "/search":
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                page = search_page(query)
            elif parsed.path == "/history":
                # N11: the full history view, with search.
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                page = history_page(query or None)
            elif parsed.path == "/research":
                # Q4: the saved reports, newest first.
                page = research_list_page()
            elif parsed.path.startswith("/research/"):
                # Q4: one saved report, rendered as a page.
                page = research_report_page(parsed.path[len("/research/"):])
                if page is None:
                    page = ("<h1>No such report</h1><p><a href=\"/research\">"
                            "← All reports</a></p>")
            elif parsed.path == "/history-clear":
                # N3/N11: confirm first; ?confirm=1 does the wipe.
                params = urllib.parse.parse_qs(parsed.query)
                if params.get("confirm", [""])[0] == "1":
                    from .titles import clear_history
                    clear_history()
                    page = history_page(cleared=True)
                else:
                    page = history_clear_page()
            elif parsed.path == "/brief-refresh":
                # U7: rebuild the brief on demand, in the
                # background — the page says to reload in a
                # minute. Gated on the kill-switch (T2): no brain
                # job runs while AI/chat is off.
                from .seats import seats_enabled
                if not seats_enabled("chat"):
                    page = ('<h1>Morning brief</h1><p>AI is off. The '
                            'brief does not build while AI/chat is off.</p>'
                            '<p><a href="/">← Start page</a></p>')
                else:
                    def _rebuild_brief():
                        from .brief import build_brief
                        from .chat import ChatSession
                        try:
                            build_brief(ChatSession(), force=True)
                        except Exception:
                            pass

                    threading.Thread(target=_rebuild_brief,
                                     daemon=True).start()
                    page = ('<h1>Morning brief</h1><p>Rebuilding the '
                            'brief. Reload the start page in a minute.</p>'
                            '<p><a href="/">← Start page</a></p>')
            elif parsed.path == "/compare.csv":
                # M11: the last comparison as a CSV download.
                from .compare import compare_table_csv, last_comparison
                rows, fields = last_comparison()
                page = compare_table_csv(rows, fields)
                content_type = "text/csv; charset=utf-8"
                disposition = 'attachment; filename="comparison.csv"'
            elif parsed.path == "/compare.md":
                # M11: the last comparison as a Markdown download.
                from .compare import compare_table_text, last_comparison
                rows, fields = last_comparison()
                page = compare_table_text(rows, fields)
                content_type = "text/markdown; charset=utf-8"
                disposition = 'attachment; filename="comparison.md"'
            elif parsed.path == "/compare-refresh":
                # M11: the window intercepts this navigation and
                # re-runs the comparison; a direct visit just shows
                # the stored table again.
                from .compare import compare_table_html, last_comparison
                rows, fields = last_comparison()
                page = compare_table_html(rows, fields)
            elif parsed.path == "/howto":
                # U22: the manual as an index page — the same
                # sections the Help menu's How to Use window shows.
                page = howto_page()
            else:
                page = start_page()
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # the page must say so, never hang
            body = f"Forge Browser hit a problem: {exc}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalPageServer:
    """Serves Forge Browser's own pages on 127.0.0.1 (app use)."""

    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def home_url(self):
        return f"http://127.0.0.1:{self.port}/"

    def search_url(self, query):
        return f"http://127.0.0.1:{self.port}/search?q={urllib.parse.quote(query)}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        # shutdown() stops the serve loop; server_close() closes
        # the listening socket. Without the close, the socket
        # stays open until the server is garbage-collected — the
        # ResourceWarning the suite used to print (H1).
        self._server.shutdown()
        self._server.server_close()
