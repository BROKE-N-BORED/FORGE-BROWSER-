# Audit #18 — Markdown table separator row

Audited 2026-11-12 from the code (html2md.py, files.py
_csv_to_markdown), the tests (3 table tests), and live probes of
headerless tables, pipes in cells, links in cells, ragged rows,
and colspan.

## What it is

Task #18 from the improvements list: the Markdown table separator
row — the first row of each table is treated as the header and a
separator row ("| --- | --- |") is emitted under it, so viewers
render the table instead of plain text lines. Built 2026-11-11.

## What it does

- html2md converts HTML to Markdown for the model: structure kept
  (headings, paragraphs, lists, blockquotes, code, simple tables,
  links), noise dropped (script/style/nav/footer/aside/...),
  whitespace collapsed.
- #18 specifically: the first row of each table gets a separator
  row under it. Same shape in the CSV path.
- A streaming HTMLParser subclass: one pass, no DOM tree held in
  memory.

## Strengths

- The separator fix itself works: a well-formed table with a header
  row renders as a real table (tested: column count matches,
  second table gets its own separator, an empty leading row does
  not steal the separator).
- The streaming parser keeps memory flat (no DOM tree) — right for
  an 8 GB machine.
- Cell text is whitespace-collapsed, so multi-line cells stay on
  one line.
- An all-empty row is skipped and does not consume the separator.
- Links are collected separately for research citations.

## Weaknesses (all verified by probe or code read)

1. A link inside a table cell breaks the separator row — the very
   thing #18 fixed. PROBED: "| --- |[/p](http://x.example/p)" —
   the link is emitted to the parts stream (outside the cell
   buffer) and glues onto the separator line with no newline, so
   the separator is no longer a valid separator row and the whole
   table may render as plain text. The link text is also wrong
   ("/p" instead of "Click here" — the anchor text went to the
   cell buffer, _link_text was empty, so text fell back to the
   href).
2. A pipe in a cell splits the column. PROBED: "fast | cheap" →
   "| Widget | fast | cheap |" — one cell becomes two columns. No
   "|" escaping.
3. The first row is ALWAYS the header, even when it is data.
   PROBED: a <td>-only table gets its first data row promoted to
   header ("Apple | Red" + separator). The parser does not
   distinguish <th> from <td> at all.
4. Ragged rows break the column count. PROBED: a 3-cell row against
   a 2-column separator. No padding to the max width.
5. colspan/rowspan are ignored. PROBED: a colspan=2 header renders
   as 1 cell against 2-cell data rows. Common in real tables.
6. Nested tables clobber the outer row. A nested <table> resets
   _table_rows and its <tr> overwrites _row_cells, losing the outer
   row's cells collected before it. (Code-read; layout tables with
   nesting are common on old sites.)
7. Bold/italic/images in cells leak outside the table.
   <strong>/<em>/<img> emit to the parts stream, not the cell
   buffer — same stray-content class as links (weakness 1). The
   markers land between rows.
8. No column alignment. The separator is always "---" — align
   attributes ignored. Minor.
9. The CSV path shares the pipe + ragged flaws and silently
   truncates at 200 rows (a 500-row CSV loses 300 rows with no
   note).

## UI flaws

- A table with a link in a cell may render as plain text (weakness
  1) — the person sees raw Markdown lines instead of a table.
- A fake header mislabels the first data row (weakness 3).

## Gaps / missing

- No pipe escaping, no ragged padding, no colspan, no nested-table
  handling, no inline markup inside cells.

## Improvements (concrete, in priority order)

1. Route inline content inside cells into the cell buffer: links
   as [text](url), bold/italic markers, image alt — so nothing
   leaks into the parts stream mid-table (fixes the separator
   corruption + the wrong link text).
2. Escape "|" in cell text (and in CSV cells).
3. Only treat the first row as a header when it contains <th> (or
   the table has a <thead>); for headerless tables, emit an empty
   header row + separator or the rows without a header claim.
4. Pad ragged rows to the max column count (buffer the table's
   rows, then emit).
5. Honor colspan (repeat/span the cell) — at minimum, expand a
   colspan'd cell into that many cells so the column counts line
   up.
6. Make the table state a stack so nested tables do not clobber the
   outer row (or skip nested tables cleanly).
7. Note the 200-row CSV truncation in the output (or raise/lift
   it).
8. Honor align attributes in the separator (":---" / "---:" /
   ":---:").
9. Add tests for: link in a cell, pipe in a cell, headerless table,
   ragged rows, colspan.

## Verdict

#18's separator fix works for the clean case it was tested on, but
a single link inside a table cell breaks it — the link leaks out
of the cell buffer and glues onto the separator line, so the table
renders as plain text again, with the wrong link text besides.
Pipes split columns, headerless tables get a fake header, and
ragged/colspan rows break the column count.
