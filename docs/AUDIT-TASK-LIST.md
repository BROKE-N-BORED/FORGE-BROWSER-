# Forge Browser — Audit Task List

Built from Joshua's whole-project audit (2026-11-12 ~20:16), with
every claim verified firsthand before it became a task, plus the
improvement lists from each per-item audit as they land (#1–#22
all merged — the per-item audit cycle is complete).

**Status marks:** `[ ]` not started · `[~]` built + sandbox-verified,
awaiting his real-PC verdict · `[x]` done + verified on his PC.
This list is a proposal until he signs off on it. Nothing here is
built until he names it.

## What the verification found (2026-11-12)

Checked by cloning HEAD e025e73a fresh, running its suite, reading
the code, and downloading the published Release asset back:

- CONFIRMED: every file in the repo is 100644, no exec bits. The
  clone suite runs 188 tests, 187 pass, and the one failure is
  `test_installer_is_double_clickable` (it asserts an on-disk exec
  bit the snapshot push cannot store). install.sh's self-check
  greps `^OK` and exits 1, so a clone install fails and the GUI
  installer reports failure.
- CONFIRMED: test-count drift four ways. True count is 188. README
  says 184, TASK-LIST says 182, BROWSER.md and the guided test
  say 50.
- CONFIRMED: BROWSER.md claims a sample screenshot that is not in
  the repo (line ~458), and two referenced docs are not in the
  repo (TASK-LIST.md cites `forge-browser-improvements-2026-11-11.md`;
  IMPROVEMENTS.md cites `forge-browser-analysis-2026-11-11.md`).
- CONFIRMED: package `__version__` is 0.2 while the Release line
  is v0.1; README states no version; BROWSER.md cites the v0.5/v0.6
  lineage (lines 5, 56, 177). Package docstring still says "Code
  Forge's own web layer".
- CONFIRMED: window default is 1240x820 (52 px taller than his
  1366x768 screen) with zero geometry persistence (no QSettings,
  no save/restore anywhere in app.py).
- CONFIRMED: PySide6 is unpinned in install.sh (`PySide6>=6.6`,
  `PySide6-WebEngine` with no version, no hash) while the engines
  are pinned and hashed.
- CONFIRMED: the installer deletes the old install before the new
  one is in place (`rm -rf "$APP_DIR"` then `mv`, lines 115-116).
  If the move fails, there is no install at all.
- CONFIRMED: no download handling in the window at all (zero
  "download" matches in app.py). No menu bar, zoom, find-in-page,
  bookmarks, session restore, or settings window. The window title
  never shows the page (only `setWindowTitle("Forge Browser")`).
- CONFIRMED: no LICENSE file in the repo.
- CORRECTED: the Release zip DOES carry the exec bit. The published
  asset was downloaded back: SHA-256 matches the verified build
  exactly, and all four scripts carry 0o100755 with create_system=3.
  The exec-bit problem is clone-only; the Release install path works.
- CLEARED: no SearXNG secret exists. The search code takes only a
  base URL from FORGE_SEARXNG_URL; there is no secret, key, or token
  anywhere in the tree. The audit's "public hardcoded default"
  claim does not match the code. No task for it.

## A. Red at HEAD — exec bit + CI (Top 5 #1)

- [~] **A1 — Fix the exec-bit failure.** The test asserts an on-disk
  exec bit the snapshot push cannot store. Fix the test to verify
  the shipped modes (the zip builder is the source of truth for
  what ships) instead of the checkout bit, and/or make install.sh
  chmod +x the scripts before its self-check. Also investigate
  whether the push tooling can store 100755 (git trees API) so a
  clone carries the bits too. Done when: fresh clone suite is
  188/188 and a clone install exits 0.
- [~] **A2 — Add CI.** A GitHub Actions workflow that runs the suite
  on every push, so a red HEAD is caught at once instead of found
  by hand.

## B. Docs reconciliation (Top 5 #2)

- [~] **B1 — One test count: 188 everywhere.** README (184), TASK-LIST
  (182), BROWSER.md (50), test-forge-browser.sh (50). Pick one
  canonical spot for the number and point the others at it so it
  cannot drift four ways again.
- [~] **B2 — Refresh BROWSER.md.** Remove the phantom screenshot claim,
  fix the v0.5/v0.6 lineage mentions to describe the current build,
  and fold in B1's count.
- [~] **B3 — Fix the two dangling doc references.** TASK-LIST.md's
  Source line points at the in-repo docs/IMPROVEMENTS.md; the
  analysis cite in IMPROVEMENTS.md either gets the analysis file
  added to the repo or gets reworded.
- [~] **B4 — One version + CHANGELOG.** Align the package version with
  the Release line (v0.1), state the version in the README, and add
  a CHANGELOG.md.
- [~] **B5 — Fix the package docstring.** "Code Forge's own web layer"
  becomes a standalone Forge Browser description.
- [~] **B6 — LICENSE: MIT, decided 2026-11-12.** Joshua picked MIT.
  LICENSE file at the repo root (Copyright (c) 2026 Joshua Lofland).
- [ ] **B7 — Repo topics + description.** Set on GitHub (browser task).

## C. Fit the screen + remember geometry (Top 5 #3)

- [~] **C1 — Default window fits 1366x768.** 1240x820 is 52 px too
  tall. Size from the available screen, or a default that fits.
- [~] **C2 — Geometry persistence.** Save and restore window size and
  position (QSettings) so it reopens where he left it.

## D. Front door + PySide6 pin (Top 5 #4)

- [~] **D1 — Pin PySide6.** install.sh pins PySide6 and
  PySide6-WebEngine to the verified versions (6.12.0 / 6.12.0.140)
  with hashes, like the engines.
- [ ] **D2 — SearXNG secret: CLEARED, no task.** Checked 2026-11-12:
  no secret exists in the tree. Recorded here so it is not
  re-litigated.
- [~] **D3 — Installer swap safety.** Replace rm-then-mv with:
  move the old install aside, move the new one in, then remove
  the old one (and restore the old one if the move fails). A
  failed swap must leave a working install behind.

## E. Window completeness (Top 5 #5)

- [~] **E1 — Download handling in the window.** A downloadRequested
  handler that saves through the existing uniquify logic, with a
  download UI (progress + where it went).
- [~] **E2 — Session restore.** Reopen last session's tabs on launch.
- [~] **E3 — Vault autofill in the window.** Fill saved logins on
  login pages from the vault, with the approval pattern.
- [~] **E4 — Find-in-page (Ctrl+F).**
- [~] **E5 — Zoom (Ctrl+/-, Ctrl+0).**
- [~] **E6 — Bookmarks (add + list + open).**
- [~] **E7 — Window title shows the current page title.**
- [~] **E8 — Menu bar.**
- [~] **E9 — Settings window** (instead of env vars and hand-edited
  seats.json only).
- [~] **E10 — Start page refresh.** Less plain: quick links and the
  morning brief front and center.

## F. Ideas from the audit

- [ ] **F1 — Research job queue** instead of refusing the second ask.
- [ ] **F2 — OCR for scanned PDFs.** Today: text layer only, scanned
  PDFs reported plainly and saved.
- [ ] **F3 — Per-site memory controls**, beyond the sensitive-URL list.
- [ ] **F4 — A "what changed" line in the app** (version/change note
  on launch after an update).
- [ ] **F5 — Ground the speed estimates.** A 5-minute timing run on his
  PC (real tok/s for the brain on the Ryzen 3). Needs his PC: hard
  gate until he runs it.

## G. From audit #1 — Default brain upgrade

Merged from docs/AUDIT-01-BRAIN.md (2026-11-12).

- [ ] **G1 — Tag-boundary matching (":4b").** Kills the 14B trap
  (substring "4b" matches "14b"; probed).
- [ ] **G2 — Add ("qwen3.5", "2b") to the preference list.** Matches
  his actual roster.
- [ ] **G3 — Validate the saved/env pick.** Fall through to auto when
  the named model is gone.
- [ ] **G4 — Never pick an embed model**, even as last resort.
- [ ] **G5 — Token-budget the prompt** to fit the 8192 ctx (trim
  oldest history, keep system + page context pinned).
- [ ] **G6 — Size-aware picking.** Read size from /api/tags, skip
  models over ~4.5 GB.
- [ ] **G7 — Answer-check on pick/refresh.** Prove the model answers
  before adopting it.
- [ ] **G8 — Header shows pick reason** (auto / your pick / fallback).
- [ ] **G9 — Distinguish Ollama-down from no-models** in the header.
- [ ] **G10 — Per-turn speed readout (tok/s)** in the status line.

## H. From audit #2 — Streamed answers

Merged from docs/AUDIT-02-STREAMING.md (2026-11-12).

- [ ] **H1 — Stream partial lines.** Show held text once it cannot
  become an ACTION line (hold only while the buffer is still a
  prefix of "ACTION", or flush held text after ~0.5 s / ~40
  chars). A long paragraph with no newline currently shows
  nothing until it ends (probed: 50 chunks, zero shown).
- [ ] **H2 — Stop the scroll yank.** Only move to End if the user
  was already at the bottom (check the scrollbar before
  inserting).
- [ ] **H3 — Status says "answering" on first token**, plus a small
  "writing" marker while tokens flow. Today it says "thinking"
  while words stream.
- [ ] **H4 — Mark cut-off streams.** On failure, flush the held
  remainder first and mark the partial stream as cut off.
- [ ] **H5 — Dock action progress lines**, matching the CLI's
  "... search (12s)". Pass on_action in the dock's work().
- [ ] **H6 — Escape text in append()** (html.escape) so model
  replies cannot inject HTML into the log.
- [ ] **H7 — Coalesce token signals** (batch rapid tokens) for fast
  streams.

## I. From audit #3 — Chunk-and-retrieve page feeding

Merged from docs/AUDIT-03-RETRIEVAL.md (2026-11-12).

- [ ] **I1 — Install nomic-embed-text.** Pull it in install.sh (or
  offer it on first use) and show retrieval state in the dock
  header. This one change activates #3, #8 grouping, and #12
  memory search on his PC. Today retrieval is silently OFF: the
  model is not in his roster and nothing installs it or says so.
- [ ] **I2 — Join chunks in document order**, labeled with position
  ("--- chunk 3 of 12 ---"). Today they join best-first, scrambling
  sequences.
- [ ] **I3 — Fix the tie-break.** Sort by (-score, i) so ties go to
  the earliest chunk. Today ties return the END of the page.
- [ ] **I4 — Label the fallback.** When returning the first 6000
  chars, tell the brain it only saw the head.
- [ ] **I5 — Relevance floor.** Skip chunks below a similarity
  threshold and tell the brain when nothing matched.
- [ ] **I6 — Cap or batch embedding.** Cap chunks per call (e.g. 64)
  or batch with progress; raise or remove the fixed 60 s timeout
  for the batch. Big pages can silently fail today.
- [ ] **I7 — Surface the embed failure reason once** (Ollama down vs
  model missing) instead of discarding it.
- [ ] **I8 — Strip @tabN refs from the query** before embedding.
- [ ] **I9 — Deduplicate overlaps** when joining adjacent picked
  chunks.
- [ ] **I10 — Show retrieval state.** Chunks searched plus whether
  retrieval is on, in the status line or a small note.

## J. From audit #4 — @tab addressing

Merged from docs/AUDIT-04-TAB-ADDRESSING.md (2026-11-12).

- [ ] **J1 — Honor multiple @tab refs.** Join the named tabs' texts
  (each through retrieval, labeled per tab) so "@tab1 and @tab3"
  answers from both. Today only the first ref feeds the brain.
- [ ] **J2 — Warn when the named tab came back empty.** Tell the
  brain (and the log) the tab had no readable text instead of
  sending no context at all.
- [ ] **J3 — Print the tab list in visible order** (numbers
  ascending), with the group name as a label on each line — not
  grouped blocks that reorder the numbers.
- [ ] **J4 — Only read other tabs' text on demand.** The current
  tab's text is always needed; the rest only when the message
  contains @tabN (or a compare). Kills the per-send wait.
- [ ] **J5 — Honest working line during collection** ("reading
  tabs…") instead of "thinking".
- [ ] **J6 — Short-circuit the embed call when the embed model is
  known missing** (cache the failure briefly) so every send does
  not pay a failed round trip. Also serves #3 and #12.
- [ ] **J7 — Cap the tab list for very many tabs** (e.g. first 15
  plus "and N more").
- [ ] **J8 — @tab discoverability.** Hint "@tab2" in the entry
  placeholder or a one-line tip on the start page; show tab
  numbers in the tab bar (or a tooltip).
- [ ] **J9 — Fix the "only 1 tabs" grammar** in the out-of-range
  note.

## K. From audit #5 — Summarize this page

Merged from docs/AUDIT-05-SUMMARIZE.md (2026-11-12).

- [ ] **K1 — Stoppable summarize.** Honor cancel() between map
  calls and enable the Stop button during summarize. Today a
  long summarize cannot be stopped at all.
- [ ] **K2 — Cap the map work.** Cap chunks (e.g. 40) and/or add a
  second reduce level, so huge pages bound the work and the
  reduce prompt fits the ctx.
- [ ] **K3 — Per-chunk progress.** An on_progress callback ("part 3
  of 50") wired to the dock's progress label.
- [ ] **K4 — Overlap between chunks** (like #3's 200 chars) so
  boundary facts survive.
- [ ] **K5 — Distinguish empty page from empty brain output.**
  Return a marker or raise, and fix the dock's message.
- [ ] **K6 — Pass the URL from the callers** (dock + chat know it)
  so the cache key works as documented.
- [ ] **K7 — Stream the chat ACTION summarize too** (pass on_chunk
  through).
- [ ] **K8 — Let the dock button follow @tab** (summarize the named
  tab when the entry has @tabN).
- [ ] **K9 — Route the map step to the fast seat** (mechanical),
  keep the reduce on the brain.
- [ ] **K10 — Atomic cache writes** (write temp + rename).
- [ ] **K11 — Length option** (short/standard).

## L. From audit #6 — Link previews

Merged from docs/AUDIT-06-LINK-PREVIEWS.md (2026-11-12).

- [ ] **L1 — HEAD-first for previews.** Fetch headers only
  (Content-Type + Content-Length) to name media + size without
  downloading; only fetch the body for text kinds. Today hovering
  a media link downloads the whole file.
- [ ] **L2 — Do not cache failures**, or give failures a short TTL
  (e.g. 60 s) so a transient failure retries on the next hover.
- [ ] **L3 — Clamp the popup to the available screen geometry.**
- [ ] **L4 — Show the destination domain (or full URL)** in the
  popup, and/or show the hovered URL in the status bar like every
  other browser.
- [ ] **L5 — Smarter snippet.** Skip nav/boilerplate lines and take
  the first real paragraph.
- [ ] **L6 — Loading state.** Show "Fetching preview…" while the
  fetch runs.
- [ ] **L7 — Normalize URLs for the cache** (strip trailing slash,
  lowercase host).
- [ ] **L8 — Cap concurrent preview fetches** (one at a time; drop
  the stale one).
- [ ] **L9 — Dwell setting** (or a longer dwell for slow machines).

## M. From audit #7 — Cross-tab compare

Merged from docs/AUDIT-07-COMPARE.md (2026-11-12).

- [ ] **M1 — Preamble-tolerant JSON parsing.** Extract the first
  {...} block from the reply (regex) before giving up.
- [ ] **M2 — Case-insensitive field-key matching** (normalize keys
  before lookup).
- [ ] **M3 — Coerce numeric strings in the tabs list** (int("1") →
  1) instead of silently dropping them.
- [ ] **M4 — Say so when tabs are dropped by the 6-tab limit**
  (note in the result and the table).
- [ ] **M5 — Show the domain under each title** in the table so
  same-title tabs are distinguishable.
- [ ] **M6 — Honor cancel() between tab extractions**, plus
  per-tab progress ("tab 2 of 6").
- [ ] **M7 — Dark-theme the comparison page** to match the app.
- [ ] **M8 — Do not let the Markdown result truncate silently.**
  Cap the fields/tabs in the brain's copy, or summarize the table
  when it exceeds RESULT_CAP.
- [ ] **M9 — Cache extractions** (tab text hash + fields) so a
  repeat compare costs nothing.
- [ ] **M10 — Parse the person's words for field names** when the
  model's ACTION omits them.
- [ ] **M11 — Mark the comparison tab 🤖** (brain-made) and add
  refresh/export (CSV/Markdown) to the tab.

## N. From audit #8 — Tidy titles + tab grouping

Merged from docs/AUDIT-08-TITLES-GROUPING.md (2026-11-12).

- [ ] **N1 — Trim trailing stop-words** ("the", "a", "of", "to",
  "in", "and") after the 5-word cut — or ask the model for a
  title that ends on a content word.
- [ ] **N2 — Build journeys (spec).** Group history by
  task/session (time gaps + topic), resumable from the start
  page — or restate the spec to domain grouping if journeys are
  out.
- [ ] **N3 — Exclude sensitive URLs from history** (reuse
  memory.py's patterns) plus a clear-history control.
- [ ] **N4 — Order-stable grouping.** Sort items before grouping
  (or use a deterministic assignment), and name embedding groups
  from their shared topic, not the first title.
- [ ] **N5 — Cap the tidy cache** (e.g. 200 entries, FIFO).
- [ ] **N6 — Queue/debounce tidy calls.** One at a time, coalesce
  rapid titleChanged events per tab.
- [ ] **N7 — Cap tidy titles on display** (same 28 chars as raw
  titles).
- [ ] **N8 — Embed titles without the URL** (or weight the title
  higher) so topic grouping is not diluted by site identity.
- [ ] **N9 — Validate the 0.75 threshold** against real
  nomic-embed-text output once the model is installed (needs his
  PC / the I1 fix).
- [ ] **N10 — Atomic history writes** (temp + rename).
- [ ] **N11 — A history view.** Full list with search +
  timestamps + clear, reachable from the start page.
- [ ] **N12 — Show groups in the tab bar** (group labels or colors)
  so grouping is visible where the person looks.

## O. From audit #9 — Cards: saved workflows

Merged from docs/AUDIT-09-CARDS.md (2026-11-12).

- [ ] **O1 — Surface broken cards.** list_cards should report (not
  swallow) parse failures — list the card with an error, or warn
  in the CLI/chat.
- [ ] **O2 — Give the brain the card list** (names + titles) in the
  system prompt or a system message so it can pick and suggest
  cards.
- [ ] **O3 — Show the card's steps in the plan dialog**
  (describe_action for card lists its actions) so the approval
  covers what will actually run.
- [ ] **O4 — Honor cancel() between card actions**, plus progress
  per step.
- [ ] **O5 — Use retrieval for {page}** (page_context) so cards see
  the relevant part of a long page, not just the head.
- [ ] **O6 — Accept the Actions header case-insensitively** (and
  tolerate spacing variants).
- [ ] **O7 — Parse the title past leading blank lines** (first "# "
  line before the first non-blank prompt line).
- [ ] **O8 — Budget the final prompt.** Cap total results to fit
  the ctx (trim oldest results first).
- [ ] **O9 — Surface card opens as tabs** (return open_requests
  like chat's open does).
- [ ] **O10 — `card check <name>` (validate) and `card new`
  (scaffold)** commands.
- [ ] **O11 — A cards list in the dock** (names + run buttons) so
  cards are discoverable in the window.
- [ ] **O12 — Do not re-read all cards for the load error** (cache
  the list in load_card).

## P. From audit #10 — Plan → Run → Interrupt

Merged from docs/AUDIT-10-PLAN-RUN-INTERRUPT.md (2026-11-12).

- [ ] **P1 — Gate expensive actions every time** (research, card,
  open?) — not just the first action of the turn.
- [ ] **P2 — Timeout the plan wait** (e.g. 5 min → treat as
  denied) and/or make the approval non-modal (approve in the
  dock, not a popup) so a missed dialog cannot hang the turn.
- [ ] **P3 — Check _stop_requested in the _chat streaming loop**
  so Stop interrupts a generation in flight (return the partial
  text + "Stopped.").
- [ ] **P4 — Feed first-action denial back to the model** like
  submit-denial (append "The person said no" as a RESULT and
  continue) instead of ending the turn.
- [ ] **P5 — "Always allow" per action type** (search/open), with
  remember/forget always re-asking.
- [ ] **P6 — Unmark an agent tab** when the person navigates it
  manually (or when its URL changes by hand).
- [ ] **P7 — Fix the compare describe_action copy** ("Compare the
  open tabs on...").
- [ ] **P8 — Show each action in the dock as it runs** — owned by
  #2 task H5, listed here as the visibility half of this finding.

## Q. From audit #11 — Research as a background job

Merged from docs/AUDIT-11-RESEARCH-JOB.md (2026-11-12).

- [ ] **Q1 — Cancellable jobs.** A cancel flag checked between
  sources, plus a Stop for the running job (reuse the dock Stop
  while a job runs, or a job line with its own stop).
- [ ] **Q2 — Open the finished report tab in the background**
  (do not switch to it) — the chat log already announces it.
- [ ] **Q3 — Make source URLs clickable links** in the report tab
  (render <a href> in report_to_html).
- [ ] **Q4 — Save finished reports** to ~/.forge/research/
  (Markdown) so closing the tab does not lose them; list past
  reports from the start page or a command.
- [ ] **Q5 — Dark-theme the report page** to match the app (shared
  with M7 — one page style for both).
- [ ] **Q6 — Progress before each fetch** ("Fetching <url>...")
  plus elapsed time on the job label.
- [ ] **Q7 — Depth control.** Let the chat ACTION (or a follow-up)
  set max_pages / detail.
- [ ] **Q8 — Normalize URLs for dedup** (strip trailing slash,
  tracking params, http/https).
- [ ] **Q9 — Queue** (owned by whole-project task F1 — listed here
  as the #11-side gap).
- [ ] **Q10 — Retrieve the relevant excerpt per source** instead
  of the head-cut.

## R. From audit #12 — Local memory store

Merged from docs/AUDIT-12-MEMORY.md (2026-11-12).

- [ ] **R1 — Fix the cap.** Keep the NEWEST 200 facts (slice from
  the end, or store newest-first like pages). Today the 201st
  remember is silently dropped.
- [ ] **R2 — Guard forget().** Require a minimum needle length
  (e.g. 3 chars) and/or confirm when the match count is large;
  never let a 1-2 char needle wipe the store.
- [ ] **R3 — Keyword fallback for search_pages** (like facts) so
  recall works without embeddings.
- [ ] **R4 — Cache embeddings.** Persist chunk embeddings (sidecar
  or in the page store) and fact embeddings, keyed by text hash
  — a recall embeds only the query + new/changed chunks.
- [ ] **R5 — Raise or smarten the 12-chunk cap** (e.g. keep the
  most informative chunks, or raise to 24 — storage is cheap).
- [ ] **R6 — Cache fact embeddings across turns** (keyed by entry
  id + text hash) so each turn does not re-embed all 200 facts.
- [ ] **R7 — Fix the tie-break.** Sort by (-score, i) so ties go
  to the newest (consistent with the keyword path). Same fix shape
  as I3.
- [ ] **R8 — Per-site memory controls** (owned by whole-project
  task F3) + tighten sensitive matching (word-boundary or
  domain-aware).
- [ ] **R9 — Add page removal** (chat + CLI): forget a stored page
  by URL/title.
- [ ] **R10 — Atomic writes** (temp + rename) for both stores.
- [ ] **R11 — A memory view in the dock** (list facts + pages,
  forget/remove buttons).
- [ ] **R12 — Validate the 0.5 floor** against real nomic output
  once installed (with I1/N9).

## S. From audit #13 — Push-to-talk voice

Merged from docs/AUDIT-13-VOICE.md (2026-11-12).

- [ ] **S1 — Pass the model to whisper.cpp.** -m with a configured
  path (FORGE_WHISPER_MODEL_PATH, default ~/.forge/whisper/
  base.en) plus installing/downloading the model once with his
  say-so.
- [ ] **S2 — Distinguish failure reasons.** Return/raise distinct
  errors (no transcriber / transcription failed / no speech) so
  the dock can say the right thing.
- [ ] **S3 — Cap recording length** (e.g. 2 min) with a visible
  cutoff, plus elapsed time while recording.
- [ ] **S4 — Fall back to the next recorder** when the first
  records nothing.
- [ ] **S5 — Clean up transcribe temp dirs** (shutil.rmtree after
  reading, or TemporaryDirectory).
- [ ] **S6 — Make faster-whisper local-only** (no surprise
  download) or warn on first download.
- [ ] **S7 — Show a level/elapsed indicator while recording**
  (file growth = audio present).
- [ ] **S8 — Report all transcription backends in engines status**
  (whisper-cli/main/faster-whisper too).
- [ ] **S9 — Guard against overlapping transcriptions** (disable
  mic while transcribing).
- [ ] **S10 — Catch start()/stop() errors in the dock** and say so
  plainly.
- [ ] **S11 — Microphone/device selection** (or at least an env
  override for the recorder + device).
- [ ] **S12 — Install path.** install.sh offers to fetch whisper.cpp
  + base.en model (with his say-so), or documents it.

## T. From audit #14 — Model picker + AI kill-switch

Merged from docs/AUDIT-14-SEATS.md (2026-11-12).

- [ ] **T1 — Make ↻ re-pick the brain** (call pick_chat_model +
  update the session, like on_seat_changed does) so "start
  Ollama, then ↻" actually works.
- [ ] **T2 — Gate the brief on the kill-switch** (background build
  + CLI): skip the build when AI/chat is off.
- [ ] **T3 — Gate the CLI voice command on the switch** (or
  document voice as exempt).
- [ ] **T4 — Decide describe's place.** Gate it on the switch or
  document the exemption.
- [ ] **T5 — Show a dead saved pick as dead in the combo** (e.g.
  "qwen3:4b (not installed)") instead of masking it as "(auto)"
  — plus the G3 fall-through.
- [ ] **T6 — Filter embed models out of the Brain/Fast combos**
  (or mark them).
- [ ] **T7 — Cancel the running turn** when AI is switched off
  mid-turn.
- [ ] **T8 — Show model sizes in the picker** (from /api/tags).
- [ ] **T9 — Lock seats.json on write** (or accept
  last-writer-wins and document it).
- [ ] **T10 — Validate seat names in set_seat.**
- [ ] **T11 — Tooltips on the toggles** explaining what each
  covers.
- [ ] **T12 — Mark what (auto) would pick** — owned by G8.

## U. From audit #15 — Morning brief card

Merged from docs/AUDIT-15-BRIEF.md (2026-11-12).

- [ ] **U1 — Do not cache a summary-less brief when no brain was
  available either** (or rebuild a cached summary-less brief when
  a brain appears). Close the retry hole.
- [ ] **U2 — Exclude sensitive URLs from the brief** (reuse
  is_sensitive_url) — both the page list and the summary prompt.
- [ ] **U3 — Summarize across the day.** Sample pages across the
  day (or two-pass summarize) instead of only the 12 newest.
- [ ] **U4 — Give the summary more per page** (more than 300
  chars, or the most relevant chunk).
- [ ] **U5 — Show all pages on the start page** (or a "see all"
  link) to match the CLI/chat.
- [ ] **U6 — Add a "today so far" brief** (and/or week) alongside
  yesterday.
- [ ] **U7 — A refresh control** (rebuild the brief on demand).
- [ ] **U8 — Prune old brief files** (e.g. keep 30 days).
- [ ] **U9 — Stream or show progress for the chat ACTION brief**
  (or run it in the background like research).
- [ ] **U10 — Fix the DST edge** (compute the next day's start
  instead of +86400).
- [ ] **U11 — Gate on the kill-switch** — owned by T2.

## V. From audit #16 — Chat progress honesty

Merged from docs/AUDIT-16-PROGRESS.md (2026-11-12).

- [ ] **V1 — Plain-words step names** ("searching…", "reading the
  page…", "searching visited pages…", "comparing…") — reuse
  describe_action's vocabulary.
- [ ] **V2 — Pause the clock during the plan-gate wait** (or show
  the breakdown: "Done in 23s (12s working, 11s waiting)").
- [ ] **V3 — Set the step to "waiting for approval"** while the
  plan dialog is up.
- [ ] **V4 — Show per-step durations.** When a step ends, note it
  ("search took 8s") — in the dock (a small step log) and fix the
  CLI to show per-action duration, not turn-relative time.
- [ ] **V5 — Show "<1s" or one decimal for sub-second turns**
  instead of "0s".
- [ ] **V6 — Idle text for the status label** ("Ready" or the last
  outcome, kept until the next turn).
- [ ] **V7 — Cross-refs.** H3 (answering step), J5 (reading tabs),
  K3 (summarize progress), Q6 (research elapsed), G10 (tok/s).

## W. From audit #17 — Download handling

Merged from docs/AUDIT-17-DOWNLOADS.md (2026-11-12).

- [ ] **W1 — Fix the OSError catch.** Catch URLError/TimeoutError
  before OSError (or exclude them) so a network failure during a
  download reports as a network failure, not a save failure.
- [ ] **W2 — Unlink the partial file on ANY failure** during the
  write loop, not just cap refusal.
- [ ] **W3 — Add resume support** (Range requests) — restart a
  failed download from where it stopped.
- [ ] **W4 — Add an on_progress callback to http_get_to_file**
  (bytes so far + total when known) so a UI can show download
  progress.
- [ ] **W5 — Add a disk-space guard.** Stop cleanly (plain message
  + partial removal) when free space runs low — not a size cap, a
  full-disk guard.
- [ ] **W6 — Fix the User-Agent to name Forge Browser's own repo**
  (pairs with B5).
- [ ] **W7 — Clarify the refusal message for page fetches** (or
  pass a kind to _refuse_too_big).
- [ ] **W8 — Consider a total time limit** (or stall detection) for
  downloads.
- [ ] **W9 — Cross-refs.** E1 (download UI), L1 (preview media
  download), #20 (redirect fence).

## X. From audit #18 — Markdown table separator row

Merged from docs/AUDIT-18-TABLES.md (2026-11-12).

- [ ] **X1 — Route inline content inside cells into the cell
  buffer.** Links as [text](url), bold/italic markers, image alt
  — so nothing leaks into the parts stream mid-table (fixes the
  separator corruption + the wrong link text).
- [ ] **X2 — Escape "|" in cell text** (and in CSV cells).
- [ ] **X3 — Only treat the first row as a header when it contains
  <th>** (or the table has a <thead>); for headerless tables, emit
  an empty header row + separator or the rows without a header
  claim.
- [ ] **X4 — Pad ragged rows to the max column count** (buffer the
  table's rows, then emit).
- [ ] **X5 — Honor colspan** (repeat/span the cell) — at minimum,
  expand a colspan'd cell into that many cells so the column counts
  line up.
- [ ] **X6 — Make the table state a stack** so nested tables do not
  clobber the outer row (or skip nested tables cleanly).
- [ ] **X7 — Note the 200-row CSV truncation in the output** (or
  raise/lift it).
- [ ] **X8 — Honor align attributes in the separator** (":---" /
  "---:" / ":---:").
- [ ] **X9 — Add tests for:** link in a cell, pipe in a cell,
  headerless table, ragged rows, colspan.

## Y. From audit #19 — Barrier detection on raw HTML

Merged from docs/AUDIT-19-BARRIERS.md (2026-11-12).

- [ ] **Y1 — Reduce false positives.** Require the marker to appear
  in a gate context — e.g. strip <script>/<style> content before
  matching (kills the reCAPTCHA-script false positive); for
  'login', require the password field to gate content (or downgrade
  the note to "this page has a login form"); for paywall/captcha,
  prefer markers in the visible text over raw HTML.
- [ ] **Y2 — Fix the handle_page docstring** (remove the vault-fill
  promise) or implement the vault login fill.
- [ ] **Y3 — Clear age gates in handle_page** (click the confirm
  button — it is a normal clickable element, as the note says).
- [ ] **Y4 — Use the url parameter** (or drop it) — e.g. pass it
  through for per-site barrier notes.
- [ ] **Y5 — Add non-English markers** for the common gates (at
  least the top languages).
- [ ] **Y6 — Make _click_labelled try the next element/phrase**
  after a failed click instead of returning None.
- [ ] **Y7 — Verify the paywall claim** (or soften the note to "may
  be behind a paywall").
- [ ] **Y8 — Add tests for the false-positive cases:** article about
  gates, login form on a readable page, script-only reCAPTCHA, JS
  shell (documents the known blind spot).
- [ ] **Y9 — Cross-ref.** The CDP path (handle_page) is the answer
  for JS-rendered barriers — document that the fetch path cannot
  see them.

## Z. From audit #20 — Fetch address fence

Merged from docs/AUDIT-20-FENCE.md (2026-11-12).

- [ ] **Z1 — Normalize non-dotted IP forms before the fence
  check.** Resolve the host through socket.inet_aton (or an
  equivalent parser) so 2130706433, 0x7f.0.0.1, and 0177.0.0.1 are
  recognized as 127.0.0.1 and refused. Add tests for all three
  forms.
- [ ] **Z2 — Fence redirects.** Install a redirect handler that
  re-runs check_fetch_url on each redirect target (or disable
  automatic redirects and follow them manually with a per-hop fence
  check + a hop cap).
- [ ] **Z3 — Resolve-and-check DNS names.** Resolve the host,
  refuse if any address is non-global, and connect to the checked
  address (pin the connection to avoid TOCTOU) — the bigger change
  the docstring names. At minimum, resolve and check (accepting
  the TOCTOU window) as a first step.
- [ ] **Z4 — Scope the env switch.** Consider a per-session or
  per-fetch allow-local instead of a process-global switch (or
  document that the env switch is all-or-nothing).
- [ ] **Z5 — Log fence refusals** (URL + reason) so steering
  attempts are visible.
- [ ] **Z6 — Document that the window's navigation is unfenced by
  design** (the person drives it).
- [ ] **Z7 — Add tests for:** integer/hex/octal IP bypass,
  redirect-to-local, DNS-to-private (documents the known hole until
  fixed).

## AA. From audit #21 — Engine pins

Merged from docs/AUDIT-21-PINS.md (2026-11-12).

- [ ] **AA1 — Record the Chromium zip's SHA-256 in
  engine-pins.txt** (computed once from a verified download) and
  verify the download against the pinned hash instead of the GCS
  metadata MD5.
- [ ] **AA2 — Re-verify the installed Chromium on --check and on
  install** (hash the chrome binary, or the extracted tree's key
  files) — match Lightpanda's lp_installed_ok.
- [ ] **AA3 — Extract Chromium to a temp dir and swap it into
  place** (atomic) instead of extracting over the live tree.
- [ ] **AA4 — Add --retry (and --continue-at - for resume)** to the
  curl downloads.
- [ ] **AA5 — Tighten the chmod to the chrome binary + known
  executables** instead of the whole tree.
- [ ] **AA6 — Cross-ref.** PySide6 pinning (B-section), download
  resume (W3).

## AB. From audit #22 — Name collisions

Merged from docs/AUDIT-22-COLLISIONS.md (2026-11-12).

- [ ] **AB1 — Truncate long names to fit the 255-byte
  per-component limit** (keep the extension, truncate the stem)
  before linking.
- [ ] **AB2 — URL-decode the basename (unquote) before
  sanitizing**, so %20 becomes a space and %C3%A9 becomes é.
- [ ] **AB3 — Let the sanitizer keep Unicode letters** (allow
  non-ASCII word characters) instead of replacing them with _.
- [ ] **AB4 — Prefer the content kind for the extension** when the
  URL's extension disagrees with the detected kind (or append the
  kind: report.txt.pdf).
- [ ] **AB5 — Guard degenerate names** (".", "..", all-unsafe) —
  fall back to download.<kind>.
- [ ] **AB6 — Catch OSError from os.link and raise a plain-words
  BrowserError** (so a filesystem failure does not surface as a raw
  traceback).
- [ ] **AB7 — Add tests for:** long name, percent-encoded name,
  Unicode name, kind/extension mismatch, degenerate names.

## Standing gates (not tasks)

- All 22 original task-list items still await his real-PC verdicts.
  Verdicts close items but do not block this list.
- The Release zip is verified good (exec bits 0o100755, SHA-256
  matches the local build). The exec-bit problem is clone-only.
