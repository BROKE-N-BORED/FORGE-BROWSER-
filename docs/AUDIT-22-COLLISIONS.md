# Audit #22 — Name collisions

Audited 2026-11-12 from the code (files.py _keep_file, fetch_any's
temp-file creation), the tests (same-name test), and live probes
of degenerate names, long names, percent-encoded names, Unicode
names, kind/extension mismatch, and freed-number reuse.

## What it is

Task #22 from the improvements list: name collisions — files.py
_keep_file. Built 2026-11-11.

## What it does

- _keep_file(tmp_path, final_url, kind) moves an already-downloaded
  temp file to its media-dir name (~/.forge/browser-media/, or
  FORGE_BROWSER_MEDIA_DIR).
- A colliding name is uniquified (report-1.pdf, report-2.pdf, ...)
  instead of silently overwriting the file already there.
- The link call (os.link) is the atomic check, so two downloads of
  the same name can never clobber each other.
- The temp file is created IN the media dir (mkstemp
  dir=media_dir()) so the hard link is always same-filesystem.
- The name is sanitized ([^A-Za-z0-9._-] → _); a name with no
  extension gets "." + kind.

## Strengths

- The atomic link is the right mechanism — no check-then-create
  race, no clobbering under concurrency.
- The temp-in-media-dir design means the link never fails
  cross-device (verified by code read).
- Freed numbers are reused (PROBED: a-1.pdf taken again after
  deletion) — no gap accumulation.
- The uniquification is tested (pic.png/pic-1.png/pic-2.png).
- A name with no extension gets the kind as the extension (a PDF
  from a bare URL becomes download.pdf, not "download").

## Weaknesses (all verified by probe or code read)

1. A long filename crashes _keep_file. PROBED: a 300-char basename
   → OSError [Errno 36] File name too long — uncaught (only
   FileExistsError is caught). The raw OSError propagates out of
   fetch_any (not a BrowserError, so no plain-words message). No
   truncation to the 255-byte limit.
2. The basename is never URL-decoded. PROBED: "caf%C3%A9-menu.pdf"
   → "caf_C3_A9-menu.pdf"; "my%20report.pdf" → "my_20report.pdf".
   Percent-encoded names become garbage — every space and non-ASCII
   char is mangled.
3. The sanitizer strips non-ASCII letters. Even after decoding,
   "café-menu.pdf" → "caf_-menu.pdf" ([^A-Za-z0-9._-] → _).
   Non-English filenames lose their letters.
4. The extension comes from the URL, not the content. PROBED: a
   .txt URL serving a PDF keeps "report.txt" — the extension lies
   about the content; the person's viewer opens a PDF named .txt.
5. Degenerate names produce garbage files. A URL path ending in
   "/." produces a file named ".-1" (probed — no crash, but a
   garbage name). ".." → "..-1". A name that is all unsafe chars
   becomes all underscores.
6. No dedup by content. Two downloads of the same file (same bytes,
   same name) produce report.pdf + report-1.pdf. By design ("keeps
   both") — noted, not a defect. (A content-hash dedup would be a
   feature, not a fix.)
7. The number suffix has no cap. Bounded by directory contents in
   practice; a directory with thousands of same-name files means
   thousands of link attempts. Negligible.

## UI flaws

- Mangled filenames (weaknesses 2, 3) are what the person sees in
  their file manager — "caf_C3_A9-menu.pdf" instead of
  "café-menu.pdf".
- A lying extension (weakness 4) confuses the person's viewer.

## Gaps / missing

- No truncation, no URL-decoding, no Unicode preservation, no
  content-based extension.

## Improvements (concrete, in priority order)

1. Truncate long names to fit the 255-byte per-component limit
   (keep the extension, truncate the stem) before linking.
2. URL-decode the basename (unquote) before sanitizing, so %20
   becomes a space and %C3%A9 becomes é.
3. Let the sanitizer keep Unicode letters (allow non-ASCII word
   characters) instead of replacing them with _.
4. Prefer the content kind for the extension when the URL's
   extension disagrees with the detected kind (or append the kind:
   report.txt.pdf).
5. Guard degenerate names (".", "..", all-unsafe) — fall back to
   download.<kind>.
6. Catch OSError from os.link and raise a plain-words BrowserError
   (so a filesystem failure does not surface as a raw traceback).
7. Add tests for: long name, percent-encoded name, Unicode name,
   kind/extension mismatch, degenerate names.

## Verdict

#22's core is right — the atomic link plus temp-in-media-dir means
two downloads of the same name can never clobber each other, and
the uniquification is tested. But the name handling around it is
rough: a long filename crashes with a raw OSError, percent-encoded
names are never decoded (my%20report.pdf → my_20report.pdf), and
non-ASCII letters are stripped.
