# FORGE-BROWSER- — Deep Audit

**Repo:** BROKE-N-BORED/FORGE-BROWSER- · **Audit date:** 2026-11-13 (evening EST) · **Auditor:** subagent (read-only — no pushes, releases, or edits)
**HEAD at audit:** `446bc17b` — "Kitesurf borrowables — v0.8: Kitesurf-local third engine + fresh state per verification (493 checks)" (2026-11-13T17:14:37Z)
**Repo #12** in the oldest-first audit queue (second sweep; first sweep audited 2026-11-12, HEAD then `e025e73a`).

## Verdict

The headline of this audit is that **every top finding from the last audit is fixed**. The suite was red with 1 failure; it is now 493/493 green. The exec-bit failure, the four-way test-count drift, the 52-px-too-tall window, the unpinned PySide6, the unsafe installer swap, the missing LICENSE/CI/description/topics — all closed, most of them test-pinned so they cannot silently regress. And the repo did not just fix itself: it grew from ~55 files / ~470 KB / 26 commits to **90 files / 1,219,880 B / 30 commits**, with six version lines (v0.2–v0.8) built in one day from a 239-item audit task list, 237 of them built and sandbox-verified.

What is left is a different class of gap: **nothing in the v0.2–v0.8 work has been verified on his real PC yet** (0 of 237 audit items carry the real-PC `[x]` mark), the Kitesurf engine has never been driven live, and the window carries three design-rules gaps — 48 em-dash strings in visible UI text (U33), no debug log layer (U12), and no How to Use index (U22) — plus no single-instance guard (U27) and no window icon. The repo is in the strongest shape of any in this sweep so far; the remaining work is mostly his-PC verification and a rules-compliance pass.

## 1. What is actually in the repo

**Metadata (verified live from the GitHub listing, 2026-11-13):** public, created 2026-11-11T16:30:38Z, updated 2026-11-13T14:56:03Z, description **"Forge Browser — a local AI browser program with a chat interface, keyless search, and its own engine room. No API keys."**, 0 stars, 0 forks, 0 open issues, topics `ai-browser, local-ai, ollama, pyside6, python, web-browser` (6), default branch `main`, not archived, not a fork. (The GitHub API's `license` field reads `None` even though a valid MIT LICENSE file is in the tree — likely API lag; the file itself is correct, "MIT License / Copyright (c) 2026 Joshua Lofland".)

**Commits:** 30 (was 26). Four new since the last audit, all 2026-11-13:
- `283e4f3c` (14:53Z) — "Audit batch 1 (A-E) — v0.2: exec-bit fix + CI, docs reconciled, screen fit + geometry, PySide6 pinned, installer safe swap, window completeness"
- `1771dfd9` (14:57Z) — "Add CI workflow (upload via web UI — snapshot push cannot carry workflow files)"
- `8e34c79c` (15:06Z) — "Audit batches 2-6 — v0.7: ideas, brain, streaming, retrieval; tabs, summarize, previews, compare; titles, Cards, plan gate, research jobs; memory, voice, seats, brief; progress, downloads, tables, barriers, fence, pins, filenames"
- `446bc17b` (17:14Z) — "Kitesurf borrowables — v0.8: Kitesurf-local third engine + fresh state per verification (493 checks)"

**Tags:** v0.1, v0.2, v0.7, v0.8 (verified via `git show-ref`). Note: CHANGELOG claims a "GitHub Release v0.3, 2026-11-12" but **no v0.3 tag exists** (nor v0.4–v0.6) — see §6.5.

**Tree (fresh clone at `/tmp/fb-verify2`, HEAD `446bc17b`):** 90 tracked files, 1,219,880 B total. Layout:
- `forge/browser/` — 36 Python modules, 12,422 lines: the engine room + the Qt window
- `tests/` — 3 test modules (8,967 lines) + 2 deliberate non-unittest validators
- `docs/` — 22 per-item audit docs (`AUDIT-01`–`AUDIT-22`), `AUDIT-TASK-LIST.md`, `TASK-LIST.md`, `IMPROVEMENTS.md`, `forge-browser-analysis-2026-11-11.md`
- Root: `README.md` (2,580 B), `BROWSER.md` (31,634 B), `CHANGELOG.md` (20,106 B), `LICENSE`, `install.sh`, `run.sh`, `browser-setup.sh`, `test-forge-browser.sh`, `Install Forge Browser` (double-click installer), `install-gui.py`, `EXECUTABLES.txt`, `engine-pins.txt`, `pyside-requirements.txt`, `forge-browser.svg`, `.github/workflows/test.yml`, `.gitignore`

**File modes:** all 90 files are still 100644 in git — **by design now, not a defect**. `EXECUTABLES.txt` documents why: the GitHub snapshot push stores every file as 100644, so a clone cannot carry exec bits; the manifest is the single source of truth read by the tests, `install.sh` (which restores the bits on a clone install), and the Release zip builder (which stores 0o100755 for the 6 listed scripts: `Install Forge Browser`, `install.sh`, `run.sh`, `browser-setup.sh`, `test-forge-browser.sh`, `install-gui.py`).

**Issues / PRs:** 0 open issues (`{"issues":[],"totalCount":0}`), 0 open PRs (`[]`).

## 2. Features

A standalone AI browser program (Qt + WebEngine window) plus a stdlib-only engine room that is also wired into Code Forge as a tool. No API keys, no accounts, no paid services anywhere.

**The window (`forge/browser/app.py`, `create_window` at line 128, `run_app` at 2458):**
- Tabs + address bar (an address goes there; words search through the keyless chain)
- **Chat dock on the right** (line 1413, `RightDockWidgetArea`) — a local Ollama model drives the browsing one action at a time, max 3 actions per user message (`chat.py:41`, `MAX_ACTIONS = 3`)
- Model picker + AI kill-switch (master switch turns off every AI feature; flipping AI off mid-turn stops the turn)
- Plan gate: expensive actions (research, card) are gated every time; the plan dialog times out after 5 minutes (denied, never hung); Stop interrupts a streaming generation in flight
- Downloads (resume with Range, 256 MiB free-disk reserve, stall abort, uniquified names), session restore, vault autofill (fills credentials, never submits), find-in-page, zoom, bookmarks, page titles in the window title, menu bar (File/Edit/View/Bookmarks/Tools/Help, with shortcuts), settings window, history view + wipe
- Morning brief, memory store (facts + pages, forget by URL/title, Memory… view in the dock), push-to-talk voice (whisper.cpp, 2-minute cap), tidy titles + tab grouping (28-char cap, stable group colors), link previews (popup clamps to screen, shows destination domain), cross-tab compare (dark comparison page, CSV/Markdown export, refresh)
- Cards: 6 shipped (`explain-like-im-new`, `find-price`, `key-facts`, `research-topic`, `summarize-page`, `tl-dr`) — a card run can be stopped between actions with per-step progress; `card check` validates, `card new` scaffolds
- Research as a background job: stoppable from the dock, partial report marked "Stopped early", finished report opens in a background tab, saved to `~/.forge/research/`, clickable source URLs
- "What changed" line in the chat dock after an update, taken from the top of CHANGELOG.md (`whatsnew.py`, task F4)

**The engine room (CLI via `python -m forge.browser`, `forge/browser/cli.py`):** keyless search chain (SearXNG when a local instance is configured, else the backend chain), fetch of any file type (HTML→Markdown, PDF text, CSV, JSON), CDP-driven rendering with **three engines** (Chromium, Lightpanda, Kitesurf-local — v0.8), screenshot/snapshot/verify, OCR for scanned PDFs (tesseract, `ocr.py`), image description and audio transcription via local seats, an encrypted vault, barrier handling (consent banners dismissed; CAPTCHAs handed to the human, never solved), a fetch address fence (non-dotted IP forms normalized, redirects re-fenced per hop, DNS names resolved and refused when local/LAN, refusals logged to `~/.forge/fence-log.json`).

**Install story:** double-click `Install Forge Browser` (zenity dialogs on Mint, Tkinter fallback via `install-gui.py`, notify fallback); installs under `~/.local/share/forge-browser-app`, app-menu entry with the SVG icon, data in `~/.forge` never touched by install/update. `install.sh` (POSIX sh — `sh install.sh` is correct here) fetches the pinned engines + builds the app venv; `run.sh` opens the window (venv-aware, with `demo`/`test`/command passthrough).

## 3. Functions

The chat brain (`chat.py`) picks its model automatically (preference qwen3.5:4b → qwen3:4b → qwen3.5:2b → qwen3:1.7b, tag-boundary matching, embed models never picked, 4.5 GB size guide, prompts token-budgeted to the 8192 context) or honors `FORGE_BROWSER_CHAT_MODEL` / a saved pick (a dead saved pick shows "(not installed)" instead of hiding behind "(auto)"). It streams answers, shows action progress like the terminal, cannot inject HTML into the log, and reports per-turn tok/s. Retrieval chunks long pages in document order with position labels, a relevance floor, and batched embedding (nomic-embed-text, pulled by install.sh). The window degrades gracefully with no Ollama: the dock shows "Brain: none — Ollama is not running. Start Ollama, then ↻. Browsing still works."

## 4. Strengths

1. **Suite green at HEAD — verified by running it.** `python3 -m unittest discover -s tests -p "test_browser*.py"` from a fresh clone: **"Ran 493 tests in 79.995s — OK"**, system Python 3.12, zero skips. AST count matches exactly (47 + 132 + 314 = 493). The last audit's red suite is closed.
2. **The test-count drift is now test-enforced.** `DocsTests.test_docs_carry_the_true_test_count` (`tests/test_browser2.py:2857`) counts the suite by discovery and asserts "493 checks" appears in README.md, BROWSER.md, test-forge-browser.sh, and the `**Suite:**` line of docs/TASK-LIST.md. The four-way drift from the last audit cannot come back silently.
3. **The exec-bit problem is solved at the design level, not papered over.** EXECUTABLES.txt + `install.sh` bit-restore (lines 31–39) + `test_installer_is_double_clickable` (now asserts shebang + manifest membership, with a comment explaining why the on-disk bit is NOT asserted) + `test_executables_manifest_matches_scripts` (a new script cannot silently ship non-executable).
4. **The window fits his screen and remembers itself.** First size 1240×720 (was 1240×820, 52 px too tall), clamped to available screen (`app.py:91–94`); QSettings geometry save/restore (lines 178–183, 323–324) plus `saveState`/`restoreState` for the dock and toolbar (lines 191, 325).
5. **Pins with hashes, everywhere it matters.** PySide6 6.12.0 / WebEngine 6.12.0.140 pinned with per-arch SHA-256 in `pyside-requirements.txt` (installed with `--require-hashes`); Lightpanda 1.0.3 + Chromium snapshot 1741687 pinned with SHA-256 in `engine-pins.txt`, including the extracted chrome binary's hash for re-verification on every run — with an honest recorded limit (a hash computed from a download proves the bytes match what was recorded, not that Google published them).
6. **Safe swaps with rollback, both installers.** `Install Forge Browser` builds in `$APP_DIR.new`, moves the old install to `$APP_DIR.old`, swaps in, restores the old one on failure (lines 117–129). The Chromium extract does the same (install.sh:201–210). The old `rm -rf` then `mv` is gone.
7. **The desktop entry is correct.** `Exec=sh $APP_DIR/run.sh` — the app dir is baked in absolute at install time, no `%k` anywhere (`Install Forge Browser`:166); Icon is the absolute SVG path. U4 satisfied.
8. **The PySide6 import guard gives a plain-words first-run error.** `app.py:100–113`: lazy Qt import, ImportError becomes a BrowserError telling the user to run install.sh then run.sh. `run.sh` falling back to system Python is therefore a guided dead end, not a traceback.
9. **Honesty is structural, not decorative.** CHANGELOG 0.8 records that the Kitesurf engine itself could NOT be built in the sandbox (Bazel blocked) and no live Kitesurf engine has been driven yet — the adapter was tested against a fake CDP server instead. The fence's remaining DNS re-resolution window is documented. The pins file records its own limits. BROWSER.md has a "Limits, plainly" section.
10. **CI exists and matches the suite.** `.github/workflows/test.yml` runs `python3 -m unittest discover -s tests -v` on Python 3.12 with no dependencies — correct, because the suite is stdlib-only (the one test that touches PySide6 skips when PySide6 is present, testing the no-PySide6 path).
11. **Clean mechanics.** Zero TODO/FIXME in `forge/`; zero secrets found by scan; `quaternion` 0 and `maternion` 0 (no model-tag typo either way); `.gitignore` covers `.env`, keys, vaults, `seats.json` and cites the no-secrets rule; the SearXNG "secret" from the last audit was checked and cleared (AUDIT-TASK-LIST D2: "no secret exists in the tree") — my own grep agrees: `search.py:279–283` reads `FORGE_SEARXNG_URL` or the setting, default `""`, no hardcoded public instance anywhere.
12. **The window launches clean.** I ran the real entry point (`python -m forge.browser app`, testenv, offscreen) — it stayed running past the 60 s timeout with zero tracebacks. (First attempt failed on Chromium's "running as root without --no-sandbox" refusal — a sandbox artifact, fixed with `QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"`, not a product defect.)

## 5. Weaknesses

1. **48 em-dash strings in visible UI text (U33) — the biggest rules gap.** AST count of `app.py` string literals containing "—", excluding docstrings: **48**. They are window titles ("Forge Browser — Bookmarks", "— Downloads", "— memory", "— vault", "— Settings", "— approve this step?", "— fill login?"), the status-bar tagline ("Forge Browser — no accounts, no API keys, nothing tracked.", shown at lines 307 and 560), tab titles ("{title} — Forge Browser"), tooltips, and status/error strings ("Brain: none — Ollama is not running", "AI is off — turn it on below…", "Plan timed out after 5 minutes — treated as no.", "⚠ Nothing recorded — check the microphone."). U33 says no em-dashes in visible UI text. (The 6 bare `&`s in `app.py` are NOT a U11 violation — they are deliberate menu mnemonics: `&File`, `&Edit`, `&View`, `&Bookmarks`, `&Tools`, `&Help`.)
2. **No debug log layer (U12).** Zero matches for `debug_log` / "Debug report" anywhere in `forge/` or `install.sh`, and no `logging` module use at all. The only log file is `~/.forge/fence-log.json` (fence refusals). Every app is supposed to carry the BNB S61 + T21 debug log layer (rotating logs, ring buffer, traceback capture, scrubbed report, Debug report button).
3. **No How to Use index (U22).** The Help menu contains exactly one item: "About Forge Browser" (`app.py:771–773`). The manual exists as BROWSER.md, but there is no in-window index.
4. **No single-instance guard (U27).** Zero matches for `QLockFile`/`QLocalServer` in `app.py`. Re-clicking the icon starts a second copy.
5. **No window icon.** Zero `setWindowIcon` in `app.py`, even though `forge-browser.svg` ships in the repo and the menu entry uses it.
6. **Nothing verified on his real PC.** AUDIT-TASK-LIST.md: **237 items `[~]`** (built + sandbox-verified, awaiting his verdict), **0 `[x]`** (done + verified on his PC), 2 `[ ]` — D2 (cleared, no task) and **F5, the hard gate: a 5-minute timing run on his PC to ground the speed estimates**. Every v0.2–v0.8 claim rests on sandbox verification only.
7. **Kitesurf has never been driven live.** The adapter is real and green against a fake CDP server, but the engine itself could not be built in the sandbox (Bazel could not fetch its module registry; the proxy handshake crashed Bazel's Java downloader). On the real PC the build needs Bazelisk + Node 22+ + pnpm with normal home internet, and the first build compiles the Rust core — slow on 2 cores with a hard drive. Until that happens, the third engine is unproven end to end.
8. **BROWSER.md "Limits, plainly" is stale on OCR.** It says "Scanned PDFs need OCR, which is not in this build" — but OCR **is** in the build: `forge/browser/ocr.py` (74 lines, tesseract-based) and CHANGELOG 0.3 F2 ("scanned PDFs are read with OCR when tesseract is installed"). The doc contradicts the code.
9. **Tags vs CHANGELOG disagree on v0.3.** CHANGELOG 0.3 says "(GitHub Release v0.3, 2026-11-12)" but no v0.3 tag exists (tags are v0.1, v0.2, v0.7, v0.8; nor v0.4–v0.6). Either the releases were never tagged or the CHANGELOG overstates. Releases could not be checked with the read-only tools.
10. **Two `__version__` values in one tree.** `forge/browser/__init__.py:55` says `"0.8"` (correct, used by the app); `forge/__init__.py:1` says `"2.5"` — the Code Forge parent package version carried along in the shared `forge/` namespace. Minor, but a reader grepping for the version finds two answers.
11. **"One engine session = one page. No parallel tabs in this build"** (BROWSER.md Limits) reads as if the browser has one tab. It means the engine room's CDP session, not the window — but the window does have tabs, so the line is ambiguous.
12. **Test-run ResourceWarnings.** The green suite prints unclosed-socket warnings (`LocalPageServer` sockets in test_browser2.py). Cosmetic, but the suite output is noisy.
13. **Documented residual: the fence's DNS window.** CHANGELOG 0.7 Z records it honestly: "the fetch re-resolves after the fence's check, so pinning the connection to the checked address is the remaining step." A TOCTOU gap, known and named, not yet closed.

## 6. Gaps

- **Design-rules compliance pass** (U33 ×48, U12, U22, U27, window icon) — §5.1–5.5.
- **Real-PC verification** — the entire v0.2–v0.8 body of work, plus the F5 timing run.
- **Kitesurf live build + drive** — needs his PC and normal internet.
- **Releases/tags hygiene** — v0.3–v0.6 tags missing vs CHANGELOG claims; whether a v0.8 Release exists with the exec-bit-carrying zip is unverified.
- **No release-read verification** of any Release asset (no release-read tool in the read-only set).
- **The two threshold validators** (`tests/validate_threshold.py`, `tests/validate_memory_threshold.py`) need Ollama + nomic-embed-text on the real machine — deliberately not in the suite, and not yet run anywhere this audit could see.
- **No lifetime stats / export** for the browser itself (history wipe exists; no export of history or research reports beyond the files in `~/.forge/research/`).

## 7. UI flaws

- The 48 em-dash strings (§5.1) — the single largest UI flaw, and the only one that violates an immutable rule by count.
- Help menu is About-only (§5.3); no How to Use index.
- No window icon (§5.5) — the taskbar/window switcher shows the generic icon.
- No single-instance behavior (§5.4) — a second click opens a second browser.
- No debug report surface (§5.2) — when something goes wrong, the user has no in-app way to capture it.
- Positive, verified: window fits 1366×768 and remembers geometry + dock state; menu bar is real with shortcuts; chat dock is on the right; the status line starts on "Ready" and names steps in plain words ("searching", "reading the page") with per-step seconds; the link-preview popup clamps to the screen; the plan dialog cannot hang (5-minute timeout, denied); the finished research report opens in a background tab instead of stealing focus; the log no longer yanks scroll to the bottom.
- Not checked visually: the window was launched offscreen, never eyeballed on 1366×768 — colors, spacing, and the dark style are code-read only.

## 8. Innovation suggestions

1. **Close the rules gap in one pass** — a U33 sweep (48 literals, mostly " — " → ": " or a restructure; dialog titles "Forge Browser — X" → "X — Forge Browser" is still an em-dash, so use "X" alone or "X · Forge Browser"), the T21 debug log layer vendored in (DUO and ACR already carry it verbatim), a How to Use index built from BROWSER.md's own headings, a QLockFile single-instance guard, and `setWindowIcon` from the shipped SVG.
2. **Pin the fence's DNS window** — the one documented residual (CHANGELOG 0.7 Z): resolve once, connect to the checked address.
3. **Make the Kitesurf build a first-class install step** — once the real-PC build is proven, `install.sh --with-kitesurf` (or a browser-setup.sh target) so the third engine is one command, not a hand-built dev server.
4. **Surface the validators** — the two threshold scripts need a real Ollama; wire them into a `run.sh check` that reports the verdicts alongside `engines --check`.
5. **A "what changed" for the engine room** — `whatsnew.py` covers the window; the CLI could print the same line on `--version`.

## 9. Design suggestions

- The Help menu is the natural home for the How to Use index (U22) and a Debug report item (U12) — two menu items close two rules.
- The status-bar tagline ("Forge Browser — no accounts, no API keys, nothing tracked.") is the most-seen string in the app; its em-dash is the first one to fix.
- Dialog titles are inconsistent ("Forge Browser — Bookmarks" vs "Forge Browser — memory" — one capitalized, one not). One title pattern for all dialogs.
- The start page already lists pages, journeys, and "Today so far" — it is the right surface for a first-run How to Use link as well.
- Keep the "Limits, plainly" section — it is the best honesty device in the docs — but fix the two stale/ambiguous lines (OCR, "no parallel tabs").

## 10. Improvements / updates / upgrades

1. **U33 sweep** — remove the 48 em-dashes from `app.py` visible strings; add a test that fails the suite if an em-dash appears in a non-docstring string literal in `app.py` (the same test-enforced pattern as B1).
2. **Debug log layer (U12) + How to Use (U22) + single instance (U27) + window icon** — one compliance pass; all four are established patterns in sibling repos.
3. **Fix the two doc inconsistencies** — BROWSER.md Limits OCR line; the v0.3–v0.6 tag/CHANGELOG mismatch (tag the releases or correct the CHANGELOG).
4. **F5 timing run on his PC** — the hard gate; then flip `[~]` items to `[x]` as he verifies them.
5. **Kitesurf live build on his PC** — Bazelisk + Node 22+ + pnpm, normal internet; record the build outcome in the CHANGELOG the same honest way the sandbox failure was recorded.
6. **Pin the fence's DNS window** (the documented residual).
7. **Quiet the suite** — close the LocalPageServer sockets in the tests (ResourceWarnings).
8. **One `__version__`** — decide whether `forge/__init__.py`'s "2.5" belongs in this repo at all.

## 11. Change since the last audit (2026-11-12)

| Area | Last audit (HEAD `e025e73a`) | Now (HEAD `446bc17b`) |
|---|---|---|
| Version | 0.2 muddle (`__version__` "0.2", docs disagreed) | **0.8**, aligned everywhere (README, CHANGELOG, `__version__`, tag) |
| Commits / files / size | 26 / ~55 / ~470 KB | **30 / 90 / 1,219,880 B** |
| Suite | 188 tests, **FAILED (failures=1)** — `test_installer_is_double_clickable` | **493 tests, OK** in 80 s, zero skips |
| Test count claims | Drift four ways (188 / 184 / 182 / 50) | **493 everywhere, test-enforced** (DocsTests) |
| Exec bits | Every file 100644, clone install exits 1 | Still 100644 in git — **by design**: EXECUTABLES.txt + install.sh restore + manifest tests |
| Window | 1240×820 (52 px too tall), no geometry memory | **1240×720, clamped to screen, QSettings geometry + dock state** |
| PySide6 | Unpinned | **Pinned 6.12.0 / 6.12.0.140 with hashes** |
| SearXNG secret | "Public hardcoded default" | **Cleared — no secret in the tree** (D2); env var or setting, default empty |
| Installer swap | `rm -rf` then `mv`, no rollback | **.new/.old safe swap with restore on failure**, both installers |
| LICENSE / CI / description / topics | All missing | **MIT LICENSE, GitHub Actions CI, full description, 6 topics** |
| BROWSER.md screenshot claim | Claimed a sample screenshot that did not exist | **Claim gone**; no screenshot files in repo |
| Engine pins | None | **Lightpanda 1.0.3 + Chromium 1741687, SHA-256 pinned** |
| Engines | Chromium + Lightpanda | **+ Kitesurf-local (v0.8)**, adapter green vs fake CDP, never driven live |
| Verify | Attached to a shared engine, state could leak | **Fresh engine / isolated context per verification (v0.8)** |
| Audit docs | None | **22 per-item AUDIT docs + 239-item AUDIT-TASK-LIST** (237 built, 0 real-PC-verified) |
| Window completeness | Missing downloads/session/vault/find/zoom/bookmarks/menu/settings | **All built (v0.2 E1–E10)** |
| New in v0.3–v0.7 | — | Brain picking, streaming, retrieval, @tab, summarize, previews, compare, titles/grouping, Cards, plan gate, research jobs, memory, voice, seats, brief, progress honesty, downloads, tables, barriers, fence, pins, filenames |

## 12. What could NOT be verified

- **Releases** (v0.1–v0.8) — no release-read tool in the read-only set; whether the v0.8 Release exists, and whether its zip carries the exec bits, is unconfirmed. The v0.3 tag absence (§5.9) is the visible symptom.
- **CI run status** — the workflow file exists and matches the suite, but whether it has run green on a push is not visible through the read tools.
- **The Kitesurf engine live** — no live engine has been driven (CHANGELOG is honest about this).
- **Anything on his real PC** — all 237 audit items await his verdict; the F5 timing run is the hard gate.
- **The window eyeballed on 1366×768** — launched offscreen only; colors, spacing, and the dark style are code-read.
- **A real Ollama chat turn** — no Ollama in the sandbox; the chat protocol is covered by stubbed-brain tests only.
- **install.sh end to end on a fresh Mint box** — needs ~930 MB of downloads (engines ~330 MB + Qt ~600 MB) and was not run here.
- **The .desktop file in a real file manager** — `desktop-file-validate` is not installed in the sandbox; the Exec/Icon lines were verified by reading the installer script only.

## 13. Top 5 recommendations

1. **U33 sweep + a guard test** — remove the 48 em-dashes from `app.py` visible strings and pin the rule with a test, the same way B1 pins the test count.
2. **One design-rules compliance pass** — debug log layer (U12), How to Use index in the Help menu (U22), single-instance guard (U27), window icon from the shipped SVG. All four are established patterns in sibling repos.
3. **F5 timing run + real-PC verdicts** — the hard gate. 237 items are built and sandbox-verified; none is `[x]` until he runs them.
4. **Kitesurf live build on his PC** — Bazelisk + Node 22+ + pnpm, normal internet; record the outcome honestly in the CHANGELOG.
5. **Fix the two doc inconsistencies** — BROWSER.md's stale OCR limit line, and the v0.3–v0.6 tag/CHANGELOG mismatch.

## Evidence index

- **Clone:** `/tmp/fb-verify2`, HEAD `446bc17b6e99cb6ccc47aec869afc04f6a76ef4e`; `git ls-files` — 90 files; `git log` — 30 commits; `git tag -l` — v0.1, v0.2, v0.7, v0.8; all files mode 100644.
- **Suite (run by the auditor from the fresh clone):** `python3 -m unittest discover -s tests -p "test_browser*.py"` → "Ran 493 tests in 79.995s — OK", system python3, zero skips. AST count 47 + 132 + 314 = 493.
- **Window launch (run by the auditor):** `python -m forge.browser app` (testenv, offscreen, `QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"`) — still running at the 60 s timeout, 0 tracebacks. The first attempt failed on Chromium's as-root sandbox refusal (sandbox artifact, not a product defect).
- **Live verification:** `search_repositories` (description, 6 topics, 0 stars/forks, updated 2026-11-13T14:56:03Z), `list_commits` (30, head `446bc17b`), `list_issues` (`{"issues":[],"totalCount":0}`), `list_pull_requests` (`[]`), root listing (19 entries).
- **Counts by the auditor:** em-dash string literals in `app.py` 48 (AST, docstrings excluded); AUDIT-TASK-LIST `[~]` 237 / `[x]` 0 / `[ ]` 2; `quaternion` 0 / `maternion` 0 repo-wide; TODO/FIXME 0 in `forge/`; bare `&` in `app.py` 6, all menu mnemonics; `QSettings`/`saveGeometry`/`restoreGeometry` present (`app.py:178–183, 323–324`); `setWindowIcon` 0; `QLockFile`/`QLocalServer` 0; `debug_log` 0.
- **Files read in full:** README.md, CHANGELOG.md (all 361 lines), EXECUTABLES.txt, engine-pins.txt, pyside-requirements.txt, .github/workflows/test.yml, .gitignore, LICENSE (head), run.sh, browser-setup.sh (head), install-gui.py (head), `Install Forge Browser` (head + swap + desktop sections), install.sh (grep-verified sections). **Grep-verified:** app.py (window size, geometry, menus, em-dashes, dock, closeEvent), chat.py (MAX_ACTIONS), search.py (SearXNG), engines.py/verify.py/cdp.py (Kitesurf + fresh state), tests (count enforcement, installer tests).
