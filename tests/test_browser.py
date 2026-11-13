"""Offline tests for forge.browser (Item 1: search + fetch).

Everything here runs without the internet: the search parsers are fed
fixture responses, and fetch/CLI run against a throwaway stdlib HTTP
server on 127.0.0.1 serving a fixture page — the same pattern Forge's
#65 preview server uses. Live-web behaviour is a separate smoke check,
never a unit test.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import BrowserError, fetch, search  # noqa: E402
from forge.browser.html2md import html_to_markdown  # noqa: E402
from forge.browser.search import (  # noqa: E402
    parse_bing_html,
    parse_ddg_html,
    parse_searxng_json,
)

FIXTURE_HTML = """<!DOCTYPE html>
<html><head><title>Fixture Page</title><style>body{}</style>
<script>var secret = "should not appear";</script></head>
<body>
<nav><a href="/menu">Menu noise</a></nav>
<main>
<h1>Real Heading</h1>
<p>First paragraph with <strong>bold</strong> and a
<a href="/docs/page.html">relative link</a> plus an
<a href="https://example.org/abs">absolute link</a>.</p>
<ul><li>item one</li><li>item two</li></ul>
<ol><li>first</li><li>second</li></ol>
<pre>code line 1
code line 2</pre>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>alpha</td><td>1</td></tr></table>
</main>
<footer>Footer noise</footer>
</body></html>"""

DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone&amp;rut=x">First Result</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Snippet   one text.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2Ftwo">Second Result</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=y">Snippet two text.</a>
</div>"""

DDG_LITE_HTML = """
<table>
<tr><td><a rel="nofollow" class="result-link"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Follama.com%2F&amp;rut=abc">Ollama Home</a></td></tr>
<tr><td class="result-snippet">Run models   locally.</td></tr>
<tr><td><a rel="nofollow" class="result-link"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2Flite">Lite Two</a></td></tr>
<tr><td class="result-snippet">Second snippet.</td></tr>
</table>"""

BING_HTML = """
<ol><li class="b_algo"><h2><a href="https://example.org/bing-one">Bing One</a></h2>
<div class="b_caption"><p> Bing   snippet one. </p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9leGFtcGxlLm5ldC90d28">Bing Two</a></h2>
<p>Snippet two.</p></li></ol>"""

SEARXNG_JSON = json.dumps({
    "query": "test",
    "results": [
        {"title": "S Result", "url": "https://example.org/s",
         "content": "searxng   snippet", "engines": ["duckduckgo"]},
        {"title": "No URL", "content": "skipped"},
    ],
})


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page.html":
            body = FIXTURE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/redirect-local":
            # Z2/Z7: a public-looking URL that steers at a local
            # address — the fenced redirect handler refuses it
            # before any connection is made.
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/redirect-ok":
            # Z2/Z7: a legitimate relative redirect — followed.
            self.send_response(302)
            self.send_header("Location", "/page.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path.startswith("/search"):
            body = SEARXNG_JSON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"nope"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path == "/page.html":
            length = len(FIXTURE_HTML.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(length))
        elif self.path == "/clip.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "2048")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "4")
        self.end_headers()

    def log_message(self, *args):
        pass


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class HtmlToMarkdownTests(unittest.TestCase):
    def test_structure_noise_and_links(self):
        title, markdown, links = html_to_markdown(FIXTURE_HTML, "http://site.test/dir/")
        self.assertEqual(title, "Fixture Page")
        self.assertIn("# Real Heading", markdown)
        self.assertIn("**bold**", markdown)
        self.assertIn("- item one", markdown)
        self.assertIn("1. first", markdown)
        self.assertIn("```\ncode line 1", markdown)
        self.assertIn("| Name | Value |", markdown)
        self.assertIn("| --- | --- |", markdown)
        # The separator sits between header and first data row.
        self.assertLess(markdown.index("| --- | --- |"),
                        markdown.index("| alpha | 1 |"))
        self.assertIn("| alpha | 1 |", markdown)
        self.assertNotIn("should not appear", markdown)
        self.assertNotIn("Menu noise", markdown)
        self.assertNotIn("Footer noise", markdown)
        urls = [link["url"] for link in links]
        self.assertIn("http://site.test/docs/page.html", urls)
        self.assertIn("https://example.org/abs", urls)

    def test_table_separator_matches_column_count(self):
        html = ("<table><tr><th>A</th><th>B</th><th>C</th></tr>"
                "<tr><td>1</td><td>2</td><td>3</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| --- | --- | --- |", markdown)

    def test_second_table_gets_its_own_separator(self):
        html = ("<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
                "<p>between</p>"
                "<table><tr><th>B</th></tr><tr><td>2</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertEqual(markdown.count("| --- |"), 2)

    def test_empty_leading_row_does_not_steal_separator(self):
        html = ("<table><tr><td></td><td></td></tr>"
                "<tr><th>Name</th><th>Value</th></tr>"
                "<tr><td>alpha</td><td>1</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| --- | --- |", markdown)
        self.assertEqual(markdown.count("| --- | --- |"), 1)


class TableAuditTests(unittest.TestCase):
    """Audit #18: inline content in cells, pipes, headerless
    tables, ragged rows, colspan, nested tables, alignment."""

    def test_link_in_cell_stays_in_cell(self):
        # X1: the link renders inside the cell with its real text,
        # and the separator row stays a valid separator.
        html = ("<table><tr><th>Name</th><th>Link</th></tr>"
                "<tr><td>thing</td>"
                "<td><a href=\"http://x.example/p\">Click here</a></td>"
                "</tr></table>")
        _title, markdown, links = html_to_markdown(html, "http://x.example/")
        self.assertIn("| thing | [Click here](http://x.example/p) |", markdown)
        self.assertIn("| --- | --- |", markdown)
        self.assertNotIn("| --- |[", markdown)
        self.assertEqual(links[0]["text"], "Click here")

    def test_link_no_longer_prints_twice(self):
        # X1's side fix: outside a cell, a link prints once as
        # [text](url), not "text[text](url)".
        html = ("<p>See <a href=\"http://x.example/p\">Click here</a> "
                "now.</p>")
        _title, markdown, _links = html_to_markdown(html, "http://x.example/")
        self.assertIn("[Click here](http://x.example/p)", markdown)
        self.assertNotIn("Click here[Click here]", markdown)
        self.assertEqual(markdown.count("Click here"), 1)

    def test_bold_and_image_in_cell(self):
        # X1: bold markers and image alt land in the cell buffer,
        # not between the rows.
        html = ("<table><tr><th>X</th></tr><tr><td><strong>bold</strong>"
                " and <img alt=\"pic\"></td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| **bold** and [image: pic] |", markdown)
        self.assertEqual(markdown.count("**"), 2)

    def test_pipe_in_cell_escaped(self):
        # X2: "fast | cheap" stays one cell.
        html = ("<table><tr><th>Item</th><th>Note</th></tr>"
                "<tr><td>Widget</td><td>fast | cheap</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| Widget | fast \\| cheap |", markdown)

    def test_headerless_table_gets_empty_header(self):
        # X3: a <td>-only table's first data row is never promoted
        # to a header — an empty header row + separator come first.
        html = ("<table><tr><td>Apple</td><td>Red</td></tr>"
                "<tr><td>Plum</td><td>Purple</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        lines = markdown.split("\n")
        self.assertEqual(lines[0], "|  |  |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertEqual(lines[2], "| Apple | Red |")
        self.assertEqual(lines[3], "| Plum | Purple |")

    def test_thead_marks_header(self):
        # X3: a <thead> makes the first row the header even when
        # its cells are <td>.
        html = ("<table><thead><tr><td>Name</td><td>Value</td></tr></thead>"
                "<tr><td>alpha</td><td>1</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        lines = markdown.split("\n")
        self.assertEqual(lines[0], "| Name | Value |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertEqual(lines[2], "| alpha | 1 |")

    def test_ragged_rows_padded(self):
        # X4: every row has the max column count.
        html = ("<table><tr><th>A</th><th>B</th><th>C</th></tr>"
                "<tr><td>1</td><td>2</td><td>3</td></tr>"
                "<tr><td>4</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| 4 |  |  |", markdown)
        for line in markdown.split("\n"):
            self.assertEqual(line.count("|"), 4)

    def test_colspan_expands(self):
        # X5: a colspan=2 header counts as two cells.
        html = ("<table><tr><th colspan=\"2\">Head</th></tr>"
                "<tr><td>a</td><td>b</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| Head |  |", markdown)
        self.assertIn("| --- | --- |", markdown)

    def test_nested_table_does_not_clobber(self):
        # X6: the outer row's other cells survive, and the nested
        # table's words flatten into its cell.
        html = ("<table><tr><th>Outer</th><th>Other</th></tr>"
                "<tr><td><table><tr><td>inner1</td><td>inner2</td></tr>"
                "</table></td><td>kept</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| inner1 inner2 | kept |", markdown)
        self.assertEqual(markdown.count("| --- | --- |"), 1)

    def test_align_in_separator(self):
        # X8: align attributes shape the separator row.
        html = ("<table><tr><th align=\"left\">L</th>"
                "<th align=\"center\">C</th>"
                "<th align=\"right\">R</th></tr>"
                "<tr><td>1</td><td>2</td><td>3</td></tr></table>")
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| :--- | :---: | ---: |", markdown)

    def test_unclosed_table_still_emits(self):
        # A table missing its </table> keeps its rows.
        html = "<table><tr><th>A</th></tr><tr><td>1</td></tr>"
        _title, markdown, _links = html_to_markdown(html, "http://site.test/")
        self.assertIn("| A |", markdown)
        self.assertIn("| 1 |", markdown)

    def test_csv_pipe_escaped_and_ragged_padded(self):
        # X2/X4 in the CSV path.
        from forge.browser.files import _csv_to_markdown
        text = "name,note\na,b | c\nd\n"
        out = _csv_to_markdown(text)
        self.assertIn("| a | b \\| c |", out)
        self.assertIn("| d |  |", out)

    def test_csv_truncation_noted(self):
        # X7: a 250-row CSV says 200 of 250 rows are shown.
        from forge.browser.files import _csv_to_markdown
        rows = ["name,value"] + [f"r{i},{i}" for i in range(249)]
        out = _csv_to_markdown("\n".join(rows))
        self.assertIn("(200 of 250 rows shown", out)
        self.assertNotIn("r249", out)


class SearchParserTests(unittest.TestCase):
    def test_ddg_html_unwraps_real_urls(self):
        results = parse_ddg_html(DDG_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "First Result")
        self.assertEqual(results[0].url, "https://example.org/one")
        self.assertEqual(results[0].snippet, "Snippet one text.")
        self.assertEqual(results[1].url, "https://example.net/two")

    def test_ddg_max_results(self):
        self.assertEqual(len(parse_ddg_html(DDG_HTML, 1)), 1)

    def test_ddg_lite_markup(self):
        results = parse_ddg_html(DDG_LITE_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://ollama.com/")
        self.assertEqual(results[0].snippet, "Run models locally.")
        self.assertEqual(results[1].title, "Lite Two")

    def test_bing_markup_and_ck_unwrap(self):
        results = parse_bing_html(BING_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://example.org/bing-one")
        self.assertEqual(results[0].snippet, "Bing snippet one.")
        self.assertEqual(results[1].url, "https://example.net/two")
        self.assertEqual(results[1].engine, "bing")

    def test_searxng_json(self):
        results = parse_searxng_json(SEARXNG_JSON, 5)
        self.assertEqual(len(results), 1)  # the URL-less result is skipped
        self.assertEqual(results[0].url, "https://example.org/s")
        self.assertEqual(results[0].snippet, "searxng snippet")
        self.assertEqual(results[0].engine, "duckduckgo")

    def test_searxng_bad_json_is_plain_error(self):
        with self.assertRaises(BrowserError):
            parse_searxng_json("not json", 5)

    def test_empty_query_refused(self):
        with self.assertRaises(BrowserError):
            search("   ")


class SearxngBackendTests(_ServerCase):
    def test_search_uses_configured_local_instance(self):
        old = os.environ.get("FORGE_SEARXNG_URL")
        os.environ["FORGE_SEARXNG_URL"] = self.base
        try:
            results = search("anything", max_results=5)
        finally:
            if old is None:
                del os.environ["FORGE_SEARXNG_URL"]
            else:
                os.environ["FORGE_SEARXNG_URL"] = old
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "S Result")


class FetchTests(_ServerCase):
    def test_fetch_local_page(self):
        # The test server is local — the fence is explicitly allowed.
        page = fetch(self.base + "/page.html", allow_local=True)
        self.assertEqual(page.title, "Fixture Page")
        self.assertIn("# Real Heading", page.markdown)
        self.assertEqual(page.final_url, self.base + "/page.html")
        self.assertFalse(page.truncated)
        self.assertIsNone(page.next_offset)

    def test_fetch_paging_covers_the_whole_page(self):
        full = fetch(self.base + "/page.html", max_chars=100000, allow_local=True)
        seen = ""
        offset = 0
        while True:
            page = fetch(self.base + "/page.html", max_chars=50, offset=offset,
                         allow_local=True)
            seen += page.markdown
            if page.next_offset is None:
                break
            offset = page.next_offset
        self.assertEqual(seen, full.markdown)
        self.assertTrue(full.total_chars > 50)

    def test_fetch_refuses_non_http(self):
        with self.assertRaises(BrowserError):
            fetch("file:///etc/passwd")

    def test_fetch_404_is_plain_error(self):
        with self.assertRaises(BrowserError) as caught:
            fetch(self.base + "/missing", allow_local=True)
        self.assertIn("404", str(caught.exception))


class HttpHeadTests(_ServerCase):
    """L1: HEAD returns headers with no body."""

    def test_head_returns_headers_no_body(self):
        from forge.browser._http import http_head
        final, headers = http_head(self.base + "/page.html",
                                   allow_local=True)
        self.assertEqual(final, self.base + "/page.html")
        self.assertEqual(headers.get_content_type(), "text/html")
        self.assertEqual(int(headers["Content-Length"]),
                         len(FIXTURE_HTML.encode()))

    def test_head_names_media(self):
        from forge.browser._http import http_head
        _final, headers = http_head(self.base + "/clip.mp4",
                                    allow_local=True)
        self.assertEqual(headers.get_content_type(), "video/mp4")
        self.assertEqual(headers["Content-Length"], "2048")

    def test_head_404_is_plain_error(self):
        from forge.browser._http import http_head
        from forge.browser.errors import BrowserError
        with self.assertRaises(BrowserError) as caught:
            http_head(self.base + "/missing", allow_local=True)
        self.assertIn("404", str(caught.exception))


class FenceTests(_ServerCase):
    """Task #20: the fetch fence refuses local addresses by default."""

    def test_loopback_refused(self):
        from forge.browser.files import fetch_any
        for url in ("http://127.0.0.1:9/x", "http://127.0.0.2/x",
                    "http://[::1]/x", "http://localhost:11434/",
                    "http://192.168.1.1/", "http://10.0.0.5/",
                    "http://172.16.0.9/"):
            with self.assertRaises(BrowserError, msg=url) as caught:
                fetch(url)
            self.assertIn("local addresses", str(caught.exception))
            with self.assertRaises(BrowserError, msg=url):
                fetch_any(url)

    def test_public_literal_passes_the_fence(self):
        from forge.browser._http import check_fetch_url
        check_fetch_url("http://93.184.216.34/")  # a public IP: no raise
        check_fetch_url("https://example.org/")  # a public name: no raise

    def test_env_switch_opens_local(self):
        old = os.environ.get("FORGE_BROWSER_ALLOW_LOCAL")
        os.environ["FORGE_BROWSER_ALLOW_LOCAL"] = "1"
        try:
            page = fetch(self.base + "/page.html")
        finally:
            if old is None:
                del os.environ["FORGE_BROWSER_ALLOW_LOCAL"]
            else:
                os.environ["FORGE_BROWSER_ALLOW_LOCAL"] = old
        self.assertEqual(page.title, "Fixture Page")

    def test_render_refuses_local_before_engine(self):
        from forge.browser.render import render
        with self.assertRaises(BrowserError) as caught:
            render("http://127.0.0.1:9/")
        self.assertIn("local addresses", str(caught.exception))


class FenceAuditTests(_ServerCase):
    """Task Z: the fence catches the bypass forms — non-dotted IP
    literals, redirects to local addresses, and DNS names that
    resolve to private addresses."""

    def setUp(self):
        # Keep the Z5 fence log out of the real home for the suite.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_log = os.environ.get("FORGE_BROWSER_FENCE_LOG_PATH")
        os.environ["FORGE_BROWSER_FENCE_LOG_PATH"] = os.path.join(
            self._tmpdir.name, "fence-log.json")

    def tearDown(self):
        if self._old_log is None:
            del os.environ["FORGE_BROWSER_FENCE_LOG_PATH"]
        else:
            os.environ["FORGE_BROWSER_FENCE_LOG_PATH"] = self._old_log
        self._tmpdir.cleanup()

    def _fence_log(self):
        path = os.environ["FORGE_BROWSER_FENCE_LOG_PATH"]
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_integer_hex_octal_ip_forms_refused(self):
        # Z1: the socket layer accepts these forms, so the fence
        # must normalize them — all five are 127.0.0.1.
        from forge.browser._http import check_fetch_url
        for url in ("http://2130706433/", "http://0x7f.0.0.1/",
                    "http://0177.0.0.1/", "http://127.1/",
                    "http://0x7f000001/"):
            with self.assertRaises(BrowserError, msg=url) as caught:
                check_fetch_url(url)
            self.assertIn("local addresses", str(caught.exception))

    def test_public_ip_forms_pass(self):
        # Z1: the same normalization must not refuse public
        # addresses written in integer or hex form (8.8.8.8).
        from forge.browser._http import check_fetch_url
        check_fetch_url("http://134744072/")
        check_fetch_url("http://0x08080808/")

    def test_redirect_to_local_refused(self):
        # Z2: the first hop is opened directly (no fence applies
        # to it) — only the redirect target is fenced, and the
        # local target is refused before any connection is made.
        import urllib.request as _urlreq
        from forge.browser._http import _urlopen
        request = _urlreq.Request(self.base + "/redirect-local")
        with self.assertRaises(BrowserError) as caught:
            _urlopen(request, 10, False)
        self.assertIn("local addresses", str(caught.exception))

    def test_legitimate_redirect_followed(self):
        # Z2: a relative redirect to a page on the same (allowed)
        # host is still followed.
        from forge.browser._http import http_get
        final_url, headers, body = http_get(
            self.base + "/redirect-ok", allow_local=True)
        self.assertTrue(final_url.endswith("/page.html"))
        self.assertIn(b"Fixture Page", body)

    def _patch_getaddrinfo(self, answers):
        original = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if isinstance(answers, Exception):
                raise answers
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (address, 0)) for address in answers]

        socket.getaddrinfo = fake
        return original

    def test_dns_name_resolving_to_private_refused(self):
        # Z3: a name that points at the local machine is refused,
        # and the refusal names the resolution.
        from forge.browser._http import check_fetch_url
        original = self._patch_getaddrinfo(["127.0.0.1"])
        try:
            with self.assertRaises(BrowserError) as caught:
                check_fetch_url("http://evil.example/")
        finally:
            socket.getaddrinfo = original
        self.assertIn("resolves to", str(caught.exception))
        self.assertIn("127.0.0.1", str(caught.exception))

    def test_dns_name_resolving_to_cgnat_passes(self):
        # Z3: CGNAT is ISP space, not this machine — a public name
        # that resolves there (as behind a proxy) still fetches.
        from forge.browser._http import check_fetch_url
        original = self._patch_getaddrinfo(["198.18.27.2"])
        try:
            check_fetch_url("http://example.org/")
        finally:
            socket.getaddrinfo = original

    def test_dns_name_resolving_to_public_passes(self):
        from forge.browser._http import check_fetch_url
        original = self._patch_getaddrinfo(["93.184.216.34"])
        try:
            check_fetch_url("http://example.org/")
        finally:
            socket.getaddrinfo = original

    def test_unresolvable_name_passes_the_fence(self):
        # Z3: a name that does not resolve passes the fence — the
        # fetch itself fails with "could not reach".
        from forge.browser._http import check_fetch_url
        original = self._patch_getaddrinfo(socket.gaierror("no such host"))
        try:
            check_fetch_url("http://does-not-exist.invalid/")
        finally:
            socket.getaddrinfo = original

    def test_fence_refusals_are_logged(self):
        # Z5: every refusal is recorded with the host and the
        # reason, so steering attempts are visible afterwards.
        from forge.browser._http import check_fetch_url
        before = len(self._fence_log())
        with self.assertRaises(BrowserError):
            check_fetch_url("http://2130706433/")
        entries = self._fence_log()
        self.assertEqual(len(entries), before + 1)
        self.assertEqual(entries[-1]["host"], "2130706433")
        self.assertIn("127.0.0.1", entries[-1]["reason"])
        self.assertIn("time", entries[-1])


class CliTests(_ServerCase):
    def _run_cli(self, *argv):
        root = os.path.join(os.path.dirname(__file__), "..")
        return subprocess.run(
            [sys.executable, "-m", "forge.browser", *argv],
            cwd=root, capture_output=True, text=True, timeout=30,
        )

    def test_cli_fetch_json(self):
        proc = self._run_cli("fetch", self.base + "/page.html", "--json",
                             "--allow-local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["title"], "Fixture Page")
        self.assertIn("# Real Heading", data["markdown"])

    def test_cli_fetch_refusal_exits_2(self):
        proc = self._run_cli("fetch", "file:///etc/passwd")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Forge Browser:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
