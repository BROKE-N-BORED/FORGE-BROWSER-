# Audit #6 — Link previews

Audited 2026-11-12 from the code (preview.py, app.py hover/dwell/
popup, files.py fetch_any, _http.py), the tests (PreviewTests),
and live probes of caching, snippets, and URL handling.

## What it is

Task #6 from the improvements list: hover a link, get a 2-line
summary of where it goes. On demand only — no prefetching.
Built 2026-11-11.

## What it does

- Hover a link; after a 600 ms dwell, a popup by the cursor shows
  the destination's title plus the first lines of its text.
- The preview is the destination's own words, not a model summary:
  title + first text lines. Binary destinations (image, audio,
  video, unreadable files) say what they are, with size.
- Cached by URL (50 entries, in-memory) so a second hover on the
  same link costs nothing.
- Fetch runs on a worker thread; the popup hides when the hover
  ends or the page navigates. `preview <url>` CLI mirrors it.

## Strengths

- On-demand only: no prefetching, no model call — cheap by design,
  which is the whole point on this CPU.
- The dwell timer means a passing hover costs nothing.
- Stale-result guard: _display_preview ignores results for a URL
  no longer hovered.
- The popup hides on navigation and on hover end.
- Binary/media kinds are handled plainly (kind + size; unreadable
  binary named with its content type).
- The fetch never blocks the UI (worker thread + signal).
- Tests cover two lines + caching, empty/anchor, failure, binary
  kinds, no-readable-text, and the CLI.

## Weaknesses (all verified by probe or code read)

1. Previewing a media link downloads the whole file. fetch_any
   streams the entire file to disk before dispatching by
   Content-Type (http_get_to_file to a temp file, then _keep_file
   for save-kinds). Hovering a link to a large video downloads
   the whole video to ~/.forge/browser-media/ just to say "video
   file, 2048 MB". No HEAD-first check, no size cap for previews.
   Hovering also pollutes the media dir with files nobody asked
   to keep.
2. A failed preview is cached. PROBED: first hover fails, second
   hover (site now up) still says "Couldn't fetch that link" —
   the fetcher was called once. No TTL, no retry, and
   clear_preview_cache is called only by tests. The only cure
   for a stuck failure is restarting the browser.
3. The snippet is the first lines, even when they are nav junk.
   PROBED: a page starting with navigation produced 'Shop\nHome
   | About | Contact | Cart Sign in'. No content-aware selection:
   navigation, cookie text, and boilerplate win over the actual
   content.
4. The popup is never clamped to the screen. _display_preview
   moves it to cursor + (14, 18) with no available-geometry
   check. Near the right or bottom edge of his 1366x768 screen,
   the 380 px-wide popup runs off-screen.
5. The destination URL is never shown. The popup shows title +
   snippet only, and the status bar carries a static tagline
   ("Forge Browser — no accounts, no API keys, nothing tracked")
   that never shows the hovered link. The person cannot judge
   where a link goes — trust and safety — without clicking it.
6. No loading state. A slow fetch means nothing appears for
   seconds after the dwell; the person cannot tell "fetching"
   from "no preview".
7. No URL normalization for the cache. PROBED: trailing-slash
   variants were fetched twice. Minor.
8. No cap on concurrent preview fetches. Each dwell spawns a
   thread; rapid hovering across links stacks fetches, each a
   full download (see #1). Bounded by user speed. Minor.
9. The 600 ms dwell is fixed — no setting. Minor.

## UI flaws

- Popup runs off-screen at the edges (weakness 4).
- No destination URL shown (weakness 5) — the one thing hover
  feedback exists for in a browser.
- No loading state (weakness 6).
- Nav-junk snippets (weakness 3).

## Gaps / missing

- No HEAD-first / media-size check before downloading.
- No failure retry or TTL.
- No content-aware snippet selection.
- No dwell setting.
- No status-bar hover URL (the status bar exists but only shows
  a static tagline).

## Improvements (concrete, in priority order)

1. HEAD-first for previews: fetch headers only (Content-Type +
   Content-Length) to name media + size without downloading;
   only fetch the body for text kinds. Or a preview mode in
   fetch_any that caps the download.
2. Do not cache failures — or give failures a short TTL (e.g.
   60 s) so a transient failure retries on the next hover.
3. Clamp the popup to the available screen geometry.
4. Show the destination domain (or full URL) in the popup, and/or
   show the hovered URL in the status bar like every other
   browser.
5. Smarter snippet: skip nav/boilerplate lines (short link-bar
   lines, "Sign in", cookie text) and take the first real
   paragraph.
6. Loading state: show "Fetching preview…" while the fetch runs.
7. Normalize URLs for the cache (strip trailing slash, lowercase
   host).
8. Cap concurrent preview fetches (one at a time; drop the stale
   one).
9. Dwell setting (or a longer dwell for slow machines).

## Verdict

#6 does the cheap thing well — on-demand, no model, no
prefetch — and the popup mechanics (dwell, stale guard, hide)
are sound. But the fetch underneath is the full fetch_any:
hovering a media link downloads it whole, and a failed preview
sticks until restart. The popup also hides the one thing hover
feedback exists for: where the link goes.
