# Forge Browser — Changelog

## 0.9 — 2026-11-13

The deep-audit list, built (docs/DEEP-AUDIT-TASK-LIST.md): the
em-dash sweep, the design-rules pass, the doc fixes, the fence's
DNS pinning, a quieter suite, the CLI's missing pieces, and a
free CI to replace billing-locked GitHub Actions.

- U33 sweep (A1/A2): all 48 em-dash literals out of the window's
  visible strings in `app.py` — dialog titles to one pattern,
  the status-bar tagline first, the "memory" vs "Bookmarks"
  capitalization fixed. The sweep went further than the list
  asked: the served pages in `webui.py` (13 literals) and the
  comparison page in `compare.py` (2) are visible text too, so
  they were swept as well. A guard test fails the suite if an
  em-dash ever appears in a non-docstring string literal in any
  of the three files again.
- Design-rules pass (B1–B4): the BNB debug log layer, vendored
  verbatim, with a Debug report item in the Help menu; a How to
  Use index built from BROWSER.md's own headings, in the Help
  menu, with a first-run link on the start page; a
  single-instance guard (QLockFile + a local server — re-clicking
  the icon focuses the running app); and the window icon set
  from the shipped `forge-browser.svg`, resolved from the app
  folder first so it shows in the installed app, not just the
  repo.
- Doc fixes (E1–E4): the Limits section no longer calls OCR
  missing (it is in the build — `ocr.py`), and the one-page
  limit now says plainly it is the engine room's CDP session,
  not the window. The changelog no longer claims a GitHub
  Release v0.3 — versions 0.3–0.6 were built but shipped
  together in v0.7 at Joshua's order, so there are no v0.3–v0.6
  tags or releases. One `__version__`: `forge/__init__.py` used
  to say "2.5" (the Code Forge parent version) while the browser
  said "0.8"; the parent now mirrors the browser, pinned by a
  test.
- Fence DNS pinning (G1): the fetch no longer re-resolves after
  the fence's check — each request's host is resolved once, the
  answer is checked, and the socket connects to that same
  answer, so a hostile DNS cannot flip the answer between the
  check and the connection. For HTTPS the TLS handshake still
  uses the DNS name; only the socket is pinned. Proxied requests
  are not pinned (the socket goes to the proxy, not the origin).
  Proven by tests, including a scripted DNS flip mid-fetch.
- Suite hygiene (H1): `LocalPageServer.stop()` now closes the
  listening socket as well as stopping the serve loop — the
  ResourceWarnings are gone (16 of them across the web UI tests).
- CLI (I2–I4): `sh run.sh check` reports the engines plus the
  threshold validators' verdicts — no Ollama answering means
  "not run", never a crash. `--version` prints the whatsnew line
  (engine-room parity with the window's "what changed" line).
  `history export <file>` writes the visit history out as JSON,
  and `reports` lists the saved research reports.
- Free CI (J1): GitHub Actions is billing-locked account-wide,
  so `ci/check.sh` is the push-time check — the full suite under
  timeout, a Release-shaped zip build from the tree with
  stored-modes + SHA verification, and the verdict in
  `CI-STATUS.md` at the repo root. The zip step is vendored in
  `ci/` because the Release builder lives outside the repo and
  hardcodes the main tree path. `.github/workflows/test.yml`
  stays in place.
- Not built (blocked, still open in the list): C1, C2, D1, D2,
  D3 (need his real PC), F1, F2 (the GitHub billing lock — his
  account action), I1 (needs D1's live Kitesurf build first).

## 0.8 — 2026-11-13

The Kitesurf borrowables (ordered 2026-11-13): Kitesurf-local as a
third engine behind the engines.py seam, and fresh state for every
verification.

- Kitesurf-local is a third engine alongside Chromium and
  Lightpanda, for read, screenshot, and extract jobs — the niche
  where Chromium is heavy on an 8 GB machine and Lightpanda cannot
  run JavaScript. It speaks the same CDP as the other engines, so
  the existing session code drives it unchanged: `render`,
  `snapshot`, `screenshot`, and `verify` take
  `--engine kitesurf`, and `engines` reports whether a
  Kitesurf-local is serving. It connects to a dev server the
  person starts by hand (`pnpm run dev` in a Kitesurf checkout,
  serving on 127.0.0.1:8787); Forge Browser never starts one
  itself, and there is no cloud path — Cloudflare deployment
  needs a Paid plan, which breaks the $0 rule. The endpoint only
  counts as Kitesurf when its /json/version names itself
  Kitesurf; anything else on the port is reported as absent.
  Local-only, by design.
- Honest build verdict (2026-11-13, sandbox): the Kitesurf
  engine itself could NOT be built here — Bazel could not fetch
  its module registry in this sandbox at all. With the sandbox
  proxy set, Bazel's Java downloader crashed on the proxy's
  CONNECT handshake; with the proxy unset, direct TLS to
  bcr.bazel.build failed (the sandbox egress answers it with
  plain HTTP). The toolchain up to that point worked: pnpm
  9.15.4 and Bazelisk installed cleanly, `pnpm install
  --frozen-lockfile` finished in about a minute, and the repo
  (commit 4a290381) documents the build clearly. The adapter was
  built and tested against a fake CDP server speaking Kitesurf's
  documented protocol instead, so it is real and green — but no
  live Kitesurf engine has been driven yet. On the real PC the
  build should be attempted with normal home internet (no
  proxy): it needs Bazelisk, Node 22+, and pnpm installed, and
  the first build compiles the Rust core and its vendored
  crates, which is slow on a 2-core machine with a hard drive.
  The running engine itself is light (Cloudflare claims 3–7x
  less CPU and memory than Chromium), so the build is the heavy
  part, not the run.
- Every verification now starts FRESH. Before this build, verify
  attached to an already-running engine whenever one was
  configured (FORGE_CDP_URL / FORGE_LIGHTPANDA_URL), and its
  page landed in that engine's shared default context — cookies
  and storage from earlier runs could leak in (cookies are
  host-scoped, so the throwaway server's fresh port did not
  isolate them). Now a verification launches a fresh engine
  whenever it can (throwaway profile for Chromium, fresh
  process for Lightpanda) and, when the only engine is one
  already running, opens its page in its own isolated browser
  context and throws the context away on close. An engine that
  cannot isolate refuses in plain words instead of sharing
  state. Tests prove two fresh sessions on the same engine get
  different contexts, that a non-fresh session still shares the
  default context, and that verify itself creates and disposes
  a context end to end.

## 0.7 — 2026-11-13

Audit batch 6 (sections V–AB of `docs/AUDIT-TASK-LIST.md`), built
2026-11-13 from the per-item audits #16–#22. The last batch — the
audit list is complete except the F5 hard gate (a timing run on
the real PC).

- V1–V7: progress honesty fixed — the status line names each step
  in plain words ("searching", "reading the page") instead of raw
  action kinds, every step reports its own seconds in a step log
  ("searching took 4s"), the elapsed clock no longer counts time
  spent waiting at the plan gate (the done line names it
  separately, "(3s waiting)"), sub-second steps read "<1s", and
  the status label starts on "Ready" instead of blank.
- W1–W9: downloads fixed — a mid-download connection drop says
  the connection dropped (never "could not save") and the partial
  file is cleaned up; downloads resume from a partial file with
  Range (a server that ignores Range restarts cleanly, a 416
  keeps the partial and says to delete it); progress callbacks
  report bytes so far and the total; a download stops cleanly
  when free disk drops below 256 MiB in reserve (not a size cap —
  downloads are still uncapped); the User-Agent names this repo;
  over-size messages name the kind (page vs download); and a
  stalled download (under 1 KiB in 60s) aborts instead of
  dribbling forever.
- X1–X9: Markdown tables fixed — links inside cells stay inside
  the cell (the separator row no longer breaks) and links
  anywhere no longer print twice; pipes in cells are escaped;
  headerless tables get an empty header row instead of promoting
  the first data row; ragged rows pad to the widest row; colspan
  is honored; nested tables flatten into their cell instead of
  clobbering the outer row; alignment attributes shape the
  separator; and the CSV path escapes pipes, pads ragged rows,
  and names its 200-row truncation.
- Y1–Y9: barrier detection fixed — markers must appear in a gate
  context, so articles about gates no longer flag and a reCAPTCHA
  script alone no longer flags 'captcha' (scripts are stripped
  before matching; widget markers match class/id); a login box on
  a readable page is not a login page; a detected login barrier
  fills credentials from the vault (never submits); an age gate
  is cleared by clicking its confirm button and re-detecting;
  every barrier names its site; gate phrases cover German,
  French, Spanish, Portuguese, and Italian; a failed click tries
  the next element instead of giving up; the paywall note is
  softened to "may be behind a paywall"; and the JS-rendered
  blind spot is documented (the CDP path reads the rendered page).
- Z1–Z7: fetch fence bypasses closed — non-dotted IP forms
  (2130706433, 0x7f.0.0.1, 0177.0.0.1, 127.1) are normalized
  before the check; redirects are re-fenced on every hop, so a
  public URL that 302s to a local address is refused; DNS names
  are resolved and refused when they point at the local machine
  or the LAN (CGNAT is allowed — a public name can resolve there
  behind a proxy, and it is ISP space, not this machine); the env
  switch is documented as all-or-nothing for the process; every
  refusal is logged (host + reason) to ~/.forge/fence-log.json;
  and the window's unfenced navigation is documented as by
  design. One window remains and is documented: the fetch
  re-resolves after the fence's check, so pinning the connection
  to the checked address is the remaining step.
- AA1–AA6: engine pins fixed — the Chromium zip's SHA-256 and the
  extracted chrome binary's SHA-256 are recorded in
  engine-pins.txt (computed from a download verified against the
  GCS metadata: size + MD5 both matched); the download verifies
  against the pinned hash instead of the metadata MD5; the
  installed chrome binary is re-verified by hash on --check and
  on install, like Lightpanda; Chromium extracts to a temp dir
  and swaps into place (the old tree is restored if the swap
  fails); every curl download retries transient failures and
  resumes a broken download; and only the known executables get
  +x. Honest limit, recorded in the pins file and the docs: a
  hash computed from a download proves the bytes match what was
  recorded, not that Google published them.
- AB1–AB7: filename collisions fixed — long names truncate to
  the 255-byte limit keeping the extension (the uniquification
  suffix always survives truncation); basenames are URL-decoded
  before sanitizing (my%20report.pdf, caf%C3%A9-menu.pdf);
  Unicode letters are kept (café stays café); the extension
  follows the content — a .txt URL serving a PDF becomes
  report.pdf, a .txt URL serving a PNG becomes photo.png;
  degenerate names (".", "..", all-unsafe) fall back to
  download.<kind>; and a filesystem failure raises a plain-words
  BrowserError instead of a raw OSError.

## 0.6 — 2026-11-13

Audit batch 5 (sections R–U of `docs/AUDIT-TASK-LIST.md`), built
2026-11-13 from the per-item audits #12–#15:

- R1–R12: memory store fixed — the 200-entry cap keeps the newest
  facts instead of dropping them; forget() needs a 3+ char needle
  so forget("e") no longer wipes the store; page search falls back
  to keywords when embeddings fail; an embedding cache (a sidecar
  file, 1500 entries) stops re-embedding the same text; long pages
  keep 24 chunks; embedding ties go to the newest entry;
  sensitive-URL matching uses word boundaries (riverbank is not a
  bank); pages can be forgotten by URL or title from chat or the
  CLI; all three memory files save atomically; a Memory… view in
  the dock lists facts and pages with forget/remove; and a
  threshold-validation script proves the 0.5 similarity cut
  separates same-topic from different-topic pairs.
- S1–S12: push-to-talk voice fixed — whisper.cpp gets the model
  passed explicitly (-m, `~/.forge/whisper/base.en`, fetchable with
  `install.sh --with-whisper`); transcription failures say which
  kind they were (no transcriber, backend error, no speech);
  recording caps at 2 minutes with an elapsed timer and a "no
  audio yet" indicator; a dead recorder falls through to the next;
  transcribe temp dirs are cleaned up; faster-whisper never
  downloads a model unannounced; the engines status lists every
  installed transcriber; the mic is disabled while transcribing so
  jobs can't overlap; start/stop errors say so in plain words; and
  the recorder and microphone can be pinned with env vars.
- T2–T12: model picker + kill-switch fixed (T1 verified still
  holding) — the brief and the CLI voice command honor the AI/chat
  gate, image describing honors the master switch (the transcribe
  command is a file utility and is not gated); a dead saved pick
  shows as "(not installed)" instead of hiding behind "(auto)",
  and a saved embed pick as "(embed model)"; embed models are not
  offered as Brain or Fast; flipping AI off mid-turn stops the
  turn; the picker shows model sizes and the Brain combo's (auto)
  entry names what auto would pick; seats.json writes are locked
  and atomic; unknown seat names are ignored; and every toggle
  carries a tooltip saying what it covers.
- U1–U11: morning brief fixed — a summary-less brief cached by a
  no-brain launch is rebuilt when a brain appears; sensitive pages
  never reach the brief; the summary samples 12 pages across the
  day with 600 chars each; the start page lists every page, shows
  "Today so far" alongside yesterday, and has a refresh control;
  `brief today` briefs today so far; old brief files prune to 30
  days; the chat ACTION brief builds in the background (one job at
  a time) and lands in the log; the DST edge in the day window is
  fixed; and the kill-switch gate rides with T2.

## 0.5 — 2026-11-13

Audit batch 4 (sections N–Q of `docs/AUDIT-TASK-LIST.md`), built
2026-11-13 from the per-item audits #8–#11:

- N1–N12: tidy titles + tab grouping fixed — titles no longer end
  on a stop-word, browsing sessions show as journeys on the start
  page, sensitive pages are never recorded and history can be
  wiped from a full history view, grouping is deterministic with
  named embedding groups, the tidy cache is capped at 200, one
  tidy runs at a time with per-tab coalescing, tab titles are
  capped at 28 chars, embedding uses the title only, history
  writes are atomic, and groups wear stable colors in the tab bar.
- O1–O12: cards fixed — broken cards are listed with their parse
  error instead of vanishing, the brain is told which cards exist,
  a card's plan lists its steps (nested cards named, never
  expanded), a card run can be stopped between actions with
  per-step progress, {page} pulls the relevant part of a long page
  instead of the head, the Actions header and the title parse
  tolerates spacing and blank lines, the final prompt is budgeted
  to 24,000 chars trimming oldest results first, pages a card
  opens surface as tabs, `card check` validates and `card new`
  scaffolds, the dock has a cards list with a Run button, and a
  card load reads the list once.
- P1–P8: plan → run → interrupt fixed — expensive actions
  (research, card) are gated every time, not just when first; the
  plan dialog times out after 5 minutes (denied, never hung); Stop
  interrupts a streaming generation in flight and a stopped
  generation runs no action; a first-action denial feeds back to
  the model like a submit denial so it can propose an alternative;
  cheap action kinds can be always-allowed from the dialog or the
  CLI (memory and expensive actions always re-ask); an agent tab
  the person navigates by hand loses its 🤖 mark; the compare
  plan copy reads right; and each action already shows in the dock
  as it runs (H5).
- Q1–Q10: research as a background job fixed — a running job can
  be stopped from the dock and delivers a partial report marked
  "Stopped early"; the finished report opens in a background tab
  instead of stealing focus; source URLs in the report are
  clickable; finished reports are saved to ~/.forge/research/ and
  listed from the start page and /research; the report page shares
  the app's dark style with the comparison page; progress names
  each fetch before it starts and carries elapsed seconds; the
  chat ACTION sets depth (max_pages 1–20, detail brief/standard/
  full); dedup normalizes URLs (scheme, trailing slash, tracking
  params); the second ask already queues (F1); and excerpts are
  the relevant part of each source, not the head-cut.

## 0.4 — 2026-11-13

Audit batch 3 (sections J–M of `docs/AUDIT-TASK-LIST.md`), built
2026-11-13 from the per-item audits #4–#7:

- J1–J9: @tab addressing fixed — multiple @tab refs honored (first
  named tab supplies the context, the rest are named), empty named
  tabs are named to the brain and the log, the tab list is in
  visible order with group labels and capped at 15, other tabs'
  text is read only on demand, the working line says "reading
  tabs" during collection, embed failures short-circuit for a
  minute instead of retrying every call, and the entry placeholder
  plus a start-page tip teach @tab2.
- K1–K11: summarize fixed — a long summarize can be stopped
  between map calls, map work is capped at 40 evenly sampled
  chunks with a second reduce level, per-chunk progress shows,
  chunks overlap by 200 chars, an empty page and an empty brain
  are told apart, the URL rides the cache key, the chat ACTION
  summarize streams, the dock button follows @tab, the map step
  runs on the fast seat, cache writes are atomic, and a short
  length option exists in the chat ACTION and the dock.
- L1–L9: link previews fixed — media links are named from a HEAD
  request instead of being downloaded whole, failures are never
  cached, the popup clamps to the screen and shows the destination
  domain, the status bar shows the hovered URL, the snippet skips
  nav/boilerplate lines, a loading state shows at once, cache URLs
  are normalized, one fetch runs at a time with stale hovers
  dropped, and the dwell is a setting (100–5000 ms).
- M1–M11: cross-tab compare fixed — extraction tolerates a
  preamble before the JSON and case-mismatched keys, numeric
  strings coerce to tab numbers, dropped tabs are named in the
  result and the table, the domain shows under each title, cancel
  is honored between tabs with per-tab progress, the comparison
  page is dark like the app, the brain's copy drops fields to fit
  instead of truncating silently, extractions are cached, field
  names are parsed from the person's words when the ACTION omits
  them, and the comparison tab wears the 🤖 mark with refresh and
  CSV/Markdown export.

## 0.3 — 2026-11-12

Audit batch 2 (sections F–I of `docs/AUDIT-TASK-LIST.md`), built
2026-11-12 from the per-item audits #1–#3. Versions 0.3–0.6 were
built as version lines but never released separately — batches
2–6 shipped together as GitHub Release v0.7 (2026-11-13) at
Joshua's order ("ship them together"), so there are no v0.3–v0.6
tags or releases:

- F1–F4: research asks queue instead of being refused; scanned
  PDFs are read with OCR when tesseract is installed (and say
  plainly what to install when it is not); per-site memory
  controls beyond the sensitive-page list; a "what changed" line
  in the chat dock on launch after an update.
- G1–G10: brain picking fixed — tag-boundary matching (no more
  14B trap), qwen3.5:2b in the preference list, saved picks
  validated, embed models never picked, prompts token-budgeted
  to the 8192 context, size-aware picking, an answer-check on
  refresh, the pick reason in the header, Ollama-down told apart
  from no-models, a per-turn tok/s readout, and refresh (plus a
  Settings save) re-picking the brain instead of keeping a stale
  one.
- H1–H7: streaming fixed — partial lines stream as they arrive,
  the log no longer yanks the scroll to the bottom, the status
  says "answering" while words flow, cut-off streams are marked,
  the dock shows action progress like the terminal, model text
  cannot inject HTML into the log, and rapid tokens are batched.
- I1–I10: retrieval fixed — install.sh pulls nomic-embed-text
  and the dock shows retrieval state, chunks join in document
  order with position labels, ties go to the earliest chunk, the
  fallback is labeled, a relevance floor skips non-matching
  chunks, embedding is batched for big pages, embed failures
  name their reason, @tab refs are stripped from the query,
  overlaps are deduplicated, and the status line shows what was
  searched.

## 0.2 — 2026-11-12

Audit batch 1 (sections A–E of `docs/AUDIT-TASK-LIST.md`), built
2026-11-12 from Joshua's whole-project audit and the per-item
audits #1–#3 (GitHub Release v0.2, 2026-11-12):

- A1: the exec-bit failure is fixed — the test now checks the
  shipped modes via `EXECUTABLES.txt` (the single source of truth
  shared with `install.sh` and the Release zip builder) instead
  of the checkout bit a snapshot push cannot store; `install.sh`
  restores the bits on a clone install.
- A2: GitHub Actions CI runs the suite on every push.
- B1–B6: docs reconciled — one test count everywhere (enforced by
  a test), BROWSER.md refreshed, dangling doc references fixed,
  version aligned to 0.2, package docstring fixed, MIT license.
- C1–C2: the window fits a 1366x768 screen and remembers its size
  and position.
- D1: PySide6 pinned (6.12.0 / 6.12.0.140) with hashes.
  D3: the installer swap is safe — the old install is moved aside
  and restored if the move fails.
- E1–E10: window completeness — downloads, session restore, vault
  autofill, find-in-page, zoom, bookmarks, page titles in the
  window title, menu bar, settings window, refreshed start page.

## 0.1 — 2026-11-11 to 2026-11-12

The first release line (GitHub Release v0.1, 2026-11-12).

- The standalone AI browser program: Qt window with tabs, address
  bar, and a chat dock driven by a local Ollama model — no API
  keys, no accounts, no paid services anywhere.
- The engine room: keyless search chain, fetch of any file type,
  CDP-driven rendering (Lightpanda / Chromium), image description
  and audio transcription via local seats, an encrypted vault,
  barrier handling, and a plan-gated research runner.
- All 22 task-list improvements (#1–#22): brain upgrade, streamed
  answers, chunk-and-retrieve page feeding, @tab addressing,
  summarize, link previews, cross-tab compare, tidy titles and
  grouping, Cards, Plan→Run→Interrupt, background research jobs,
  memory store, push-to-talk voice, model picker and AI
  kill-switch, morning brief, progress honesty, download handling,
  Markdown table separators, barrier detection on raw HTML, the
  fetch address fence, engine pins, and download name uniquifying.
- The download cap was removed at Joshua's order (no download cap;
  the 32 MiB in-memory guard stays as RAM protection).
- Release retrofit: double-click installer (`Install Forge Browser`),
  app-menu entry with the Forge Browser icon, and the Release zip
  carrying the exec bits the repo cannot store.
