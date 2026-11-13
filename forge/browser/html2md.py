"""HTML -> clean Markdown for a small local model to read.

This is the read engine's core, following the approach crawl4ai proved
out (page in, LLM-ready Markdown out) but written here in pure standard
library so Forge carries no new dependency for it:

* Structure is kept: headings, paragraphs, lists, blockquotes, code
  (fenced for <pre>, inline for <code>), simple tables, and links —
  links stay inline as [text](absolute-url) AND are collected into a
  separate list, because Research mode cites its sources.
* Noise is dropped: <script>, <style>, <noscript>, <svg>, <iframe>,
  and the boilerplate landmarks <nav>, <footer>, <aside> never reach
  the output.
* Whitespace is collapsed and blank runs are capped, so pages cost
  far fewer tokens than the raw HTML.

The parser is a streaming HTMLParser subclass: one pass, no DOM tree
held in memory, which keeps it light on an 8 GB machine. Tables are
the one exception to pure streaming: a table's rows buffer until
</table> (X4), so ragged rows pad to the max width and the header
call is made with the whole table in view — one table's rows are
small next to the page.
"""

from html.parser import HTMLParser
from urllib.parse import urljoin

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "aside", "form", "button", "select", "textarea"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
_BLOCK_END = {"p", "div", "section", "article", "main", "blockquote", "ul", "ol", "table", "tr", "pre"} | set(_HEADINGS)


def _pad_row(cells, width):
    """A row padded with empty cells to the table's width (X4)."""
    return list(cells) + [""] * (width - len(cells))


def _separator_cell(align):
    """A separator cell honoring the align attribute (X8)."""
    if align == "left":
        return ":---"
    if align == "right":
        return "---:"
    if align in ("center", "centre"):
        return ":---:"
    return "---"


class _TableState:
    """One open table's buffered rows (X4/X6): rows accumulate
    here and emit at </table>, so ragged rows pad to the max
    width and a nested table never clobbers the outer row."""

    def __init__(self):
        self.rows = []          # (cells, has_header, aligns)
        self.has_thead = False
        self.row_cells = None   # the open row's cells, or None
        self.row_aligns = None  # the open row's per-cell aligns
        self.row_has_header = False
        self.cell_text = None   # the open cell's buffer, or None
        self.cell_colspan = 1
        self.cell_align = ""
        self.cell_seq = 0       # when the cell opened (see below)


class _MarkdownParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts = []
        self.links = []
        self._seen_links = set()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._pre_depth = 0
        self._list_stack = []  # ("ul"|"ol", counter)
        self._link_href = None
        self._link_text = []
        self._link_seq = 0
        self._seq = 0  # opening order: a link inside a cell owns
        # its text, but a cell inside a link owns its own — the
        # one that opened last wins (X1 vs <a><table>).
        self._heading_prefix = None
        self._tables = []  # stack of _TableState (X6)

    # -- helpers ------------------------------------------------------
    def _emit(self, text):
        self.parts.append(text)

    def _newline(self, count=1):
        tail = "".join(self.parts[-4:])
        have = len(tail) - len(tail.rstrip("\n"))
        for _ in range(max(0, count - have)):
            self.parts.append("\n")

    def _add_link(self, text, href):
        absolute = urljoin(self.base_url, href)
        if absolute not in self._seen_links:
            self._seen_links.add(absolute)
            self.links.append({"text": text, "url": absolute})
        return absolute

    def _open_cell_state(self):
        """The innermost table with an open cell, or None (X6)."""
        for state in reversed(self._tables):
            if state.cell_text is not None:
                return state
        return None

    def _link_owns_text(self):
        """True when inline content belongs to the open link: a
        link is open, and it opened after the innermost open cell
        (or there is no open cell)."""
        if self._link_href is None:
            return False
        state = self._open_cell_state()
        return state is None or self._link_seq > state.cell_seq

    def _emit_inline(self, text):
        """Inline content (bold markers, a link, image alt): into
        the link text while a link owns it, else into the
        innermost open cell buffer (X1), else the parts stream —
        so nothing leaks into the parts stream mid-table."""
        if self._link_owns_text():
            self._link_text.append(text)
        else:
            state = self._open_cell_state()
            if state is not None:
                state.cell_text.append(text)
            else:
                self._emit(text)

    def _close_cell(self, state):
        """Flush the open cell into its row (X2/X5): pipe-escaped,
        colspan-expanded."""
        if state.cell_text is None:
            return
        if state.row_cells is None:
            state.cell_text = None
            return
        text = " ".join("".join(state.cell_text).split()).replace("|", "\\|")
        state.row_cells.append(text)
        state.row_aligns.append(state.cell_align)
        for _ in range(max(0, state.cell_colspan - 1)):
            state.row_cells.append("")
            state.row_aligns.append("")
        state.cell_text = None
        state.cell_colspan = 1
        state.cell_align = ""

    def _close_row(self, state):
        """Buffer the open row (X4) — an all-empty row is skipped
        and never steals the separator."""
        if state.row_cells is None:
            return
        if any(state.row_cells):
            state.rows.append((state.row_cells, state.row_has_header,
                               state.row_aligns))
        state.row_cells = None
        state.row_aligns = None
        state.row_has_header = False

    def _emit_table(self, state):
        """Emit a buffered table as Markdown (X3/X4/X8)."""
        rows = state.rows
        width = max(len(cells) for cells, _h, _a in rows)
        first_is_header = rows[0][1] or state.has_thead
        if first_is_header:
            header, header_aligns = rows[0][0], rows[0][2]
            rest = rows[1:]
        else:
            # X3: a headerless table gets an empty header row +
            # separator, so it still renders as a table — the
            # first data row is never promoted to a header.
            header, header_aligns = [""] * width, rows[0][2]
            rest = rows
        lines = ["| " + " | ".join(_pad_row(header, width)) + " |"]
        lines.append("| " + " | ".join(
            _separator_cell(a) for a in _pad_row(header_aligns, width))
            + " |")
        for cells, _h, _a in rest:
            lines.append("| " + " | ".join(_pad_row(cells, width)) + " |")
        self._newline(1)
        self._emit("\n".join(lines))

    def _close_table(self):
        """Pop the top table and emit it — or flatten it into the
        outer cell when nested (X6): a multi-line table cannot
        live inside a single cell, but its words still belong."""
        if not self._tables:
            return
        state = self._tables.pop()
        if state.cell_text is not None:
            self._close_cell(state)
        if state.row_cells is not None:
            self._close_row(state)
        if not state.rows:
            return
        outer = self._open_cell_state()
        if outer is not None:
            flat = "; ".join(" ".join(c for c in cells if c)
                             for cells, _h, _a in state.rows)
            outer.cell_text.append(flat)
            return
        self._emit_table(state)

    def close(self):
        # A table missing its </table> still emits (X4): buffered
        # rows are not lost to malformed HTML.
        super().close()
        while self._tables:
            self._close_table()

    # -- tag handling -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attrs = dict(attrs)
        if self._open_cell_state() is not None and tag not in (
                "a", "img", "code", "strong", "b", "em", "i", "br",
                "table", "tr", "td", "th"):
            # X1: anything else inside a cell is just a word gap —
            # no newline or marker may leak into the parts stream
            # mid-table.
            self._open_cell_state().cell_text.append(" ")
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._newline(2)
            self._heading_prefix = _HEADINGS[tag] + " "
            self._emit(self._heading_prefix)
        elif tag == "p":
            self._newline(2)
        elif tag == "br":
            if self._link_owns_text():
                self._link_text.append(" ")
            else:
                state = self._open_cell_state()
                if state is not None:
                    state.cell_text.append(" ")
                else:
                    self._newline(1)
        elif tag in ("ul", "ol"):
            self._list_stack.append([tag, 0])
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                self._list_stack[-1][1] += 1
                self._emit(f"{self._list_stack[-1][1]}. ")
            else:
                self._emit("- ")
        elif tag == "a":
            href = attrs.get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._link_href = href
                self._link_text = []
                self._seq += 1
                self._link_seq = self._seq
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self._emit_inline(f" [image: {alt}] ")
        elif tag == "pre":
            self._newline(2)
            self._emit("```\n")
            self._pre_depth += 1
        elif tag == "code" and not self._pre_depth:
            self._emit_inline("`")
        elif tag in ("strong", "b"):
            self._emit_inline("**")
        elif tag in ("em", "i"):
            self._emit_inline("*")
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag == "table":
            self._tables.append(_TableState())
        elif tag == "thead":
            if self._tables:
                self._tables[-1].has_thead = True
        elif tag == "tr":
            if not self._tables:
                # A row outside any table opens an implicit one —
                # the old streaming code emitted stray rows too.
                self._tables.append(_TableState())
            state = self._tables[-1]
            if state.cell_text is not None:
                # A row starting inside an open cell is malformed
                # HTML — ignore the row start; the cells that
                # follow join the open row instead of clobbering
                # it (X6).
                return
            state.row_cells = []
            state.row_aligns = []
            state.row_has_header = False
        elif tag in ("td", "th"):
            if not self._tables:
                return
            state = self._tables[-1]
            if state.row_cells is None:
                return
            if state.cell_text is not None:
                # A missing </td>: close the open cell first, so
                # its text survives instead of being overwritten.
                self._close_cell(state)
            try:
                colspan = int(attrs.get("colspan", "1"))
            except (TypeError, ValueError):
                colspan = 1
            state.cell_text = []
            state.cell_colspan = max(1, colspan)
            state.cell_align = (attrs.get("align") or "").strip().lower()
            self._seq += 1
            state.cell_seq = self._seq
            if tag == "th":
                state.row_has_header = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._open_cell_state() is not None and tag not in (
                "a", "code", "strong", "b", "em", "i",
                "td", "th", "tr", "table"):
            # X1: no block close may touch the parts stream
            # mid-cell (its start already added a word gap).
            return
        if tag == "title":
            self._in_title = False
            self.title = self.title.strip()
        elif tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip() or self._link_href
            absolute = self._add_link(text, self._link_href)
            # Clear the link BEFORE emitting: _emit_inline routes
            # to the link text while a link owns it, and the
            # finished [text](url) must land in the cell or the
            # parts stream, not back inside the link text.
            self._link_href = None
            self._link_text = []
            self._link_seq = 0
            self._emit_inline(f"[{text}]({absolute})")
        elif tag == "pre":
            self._emit("\n```")
            self._pre_depth = max(0, self._pre_depth - 1)
            self._newline(2)
        elif tag == "code" and not self._pre_depth:
            self._emit_inline("`")
        elif tag in ("strong", "b"):
            self._emit_inline("**")
        elif tag in ("em", "i"):
            self._emit_inline("*")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._newline(1)
        elif tag in ("td", "th"):
            state = self._open_cell_state()
            if state is not None:
                self._close_cell(state)
        elif tag == "tr":
            if self._tables:
                state = self._tables[-1]
                if state.cell_text is not None:
                    self._close_cell(state)
                self._close_row(state)
        elif tag == "table":
            self._close_table()
        elif tag in _BLOCK_END:
            self._newline(2)

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if self._link_owns_text():
            # Inside a link the text belongs to the link — the
            # </a> handler turns it into [text](url) in the right
            # buffer (X1). The old code also emitted it raw, so
            # every link printed twice ("text[text](url)").
            self._link_text.append(data)
            return
        state = self._open_cell_state()
        if state is not None:
            state.cell_text.append(data)
            return
        if self._pre_depth:
            self._emit(data)
        else:
            self._emit(" ".join(data.split()) if data.strip() else (" " if data else ""))


def html_to_markdown(html, base_url):
    """Convert an HTML string to (title, markdown, links)."""
    parser = _MarkdownParser(base_url)
    parser.feed(html)
    parser.close()
    raw = "".join(parser.parts)
    lines = [line.rstrip() for line in raw.split("\n")]
    cleaned = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            cleaned.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
    markdown = "\n".join(cleaned).strip()
    return parser.title, markdown, parser.links
