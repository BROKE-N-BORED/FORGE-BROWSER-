"""Forge Browser's own pages, served locally by the browser app.

The start page and the search-results page are part of the browser
itself — served from 127.0.0.1 by this module, rendered by the
browser's own window. Search runs the same keyless chain as the
CLI (no API key, no account). Plain standard library.
"""

import html
import threading
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
"""


def start_page():
    from .titles import grouped_history
    status = html.escape(engines.format_status(engines.engine_status()))
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
        recent = "\n".join(parts)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Forge Browser</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>FORGE BROWSER<span class="dot">.</span></h1>
<p class="sub">Your browser. No accounts, no API keys, nothing tracked.</p>
<form action="/search" method="get">
<input type="text" name="q" placeholder="Search the web, or type an address up top" autofocus>
<button type="submit">Search</button>
</form>
{recent}
<div class="status">{status}</div>
</div></body></html>"""


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
    body = "\n".join(items) or "<p>No results — every search backend came back empty.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(query)} — Forge Browser</title><style>{_STYLE}</style></head>
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
            if parsed.path == "/search":
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                page = search_page(query)
            else:
                page = start_page()
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
        self._server.shutdown()
