# Forge Browser — Deep Audit Task List (v0.8 tree)

Source: `docs/DEEP-AUDIT-2026-11-13.md` — second-sweep deep audit of HEAD
`446bc17b` (v0.8), received 2026-11-13 evening. Every claim below was
re-verified against the tree before it became a task; corrections to the
audit are noted inline.

Organized highest priority to lowest, following the audit's own Top 5.
`[ ]` open · `[~]` built + sandbox-verified · `[x]` verified on his real PC.
An audit list is for Joshua to direct — nothing here is built until he says so.

## A. U33 sweep — em-dashes in visible UI text (audit Top 1)

Verified: exactly **48** non-docstring string literals in `forge/browser/app.py`
contain "—" (AST count, docstrings excluded — Arlo's count matches the audit's).
Rule U33 (immutable): no em-dashes in visible UI text.

- [~] A1 Remove all 48 em-dashes from visible strings in `app.py`:
      window/dialog titles, the status-bar tagline (most-seen string in the
      app — fix first), tab titles, tooltips, status/error strings.
      " — " becomes ": " or a restructure. Dialog titles go to ONE pattern
      ("X" alone, or "X · Forge Browser" — never "X — Forge Browser", which
      is still an em-dash) and one capitalization ("memory" vs "Bookmarks"
      is inconsistent today).
- [~] A2 Guard test: fail the suite if an em-dash appears in a non-docstring
      string literal in `app.py` — the same test-enforced pattern as B1
      (test count).

## B. Design-rules compliance pass (audit Top 2)

All four verified missing by grep (0 matches each). All are established
patterns in sibling repos.

- [~] B1 U12 — Debug log layer: vendor the BNB S61 + T21 `debug_log.py`
      (DUO and ACR already carry it verbatim: rotating logs, ring buffer,
      traceback capture, scrubbed report). Add a Debug report button/item
      in the app — the Help menu is the natural home.
- [~] B2 U22 — How to Use index: build it from BROWSER.md's own headings,
      in the Help menu (today the Help menu is About-only). Add a first-run
      link to it on the start page.
- [~] B3 U27 — Single-instance guard: `QLockFile` — re-clicking the icon
      focuses the running app instead of starting a second copy.
- [~] B4 Window icon: `setWindowIcon` from the shipped `forge-browser.svg`
      (the menu entry already uses it; the window itself shows generic).

## C. Real-PC verification — F5 + verdicts (audit Top 3) — BLOCKED, his PC

Nothing in v0.2–v0.8 has been verified on his real PC: 237 items `[~]`,
0 `[x]`. These cannot move without him at his Mint PC.

- [ ] C1 F5 — 5-minute timing run on his PC (hard gate, carried over from
      AUDIT-TASK-LIST.md — the only open item left on that list besides
      cleared D2). Grounds the speed estimates.
- [ ] C2 Real-PC verdicts: flip `[~]` items to `[x]` as he verifies them —
      window eyeballed on 1366×768 (colors, spacing, dark style are
      code-read only so far), installer end-to-end on a fresh Mint box,
      `.desktop` in a real file manager, a real Ollama chat turn
      (sandbox used a stubbed brain only), `install.sh` end to end
      (~930 MB of downloads, never run here).

## D. Kitesurf live build (audit Top 4) — BLOCKED, his PC

- [ ] D1 Build the Kitesurf engine on his PC (Bazelisk + Node 22+ + pnpm,
      normal home internet — the sandbox build failed on Bazel's module
      download, a sandbox-network artifact, not a Kitesurf defect) and
      drive it live through the v0.8 adapter. First build will be long
      on the 2-core Ryzen 3 + HDD (likely 1+ hours, several GB of cache).
- [ ] D2 Record the build outcome honestly in the CHANGELOG, the same way
      the sandbox failure was recorded.
- [ ] D3 Threshold validators on his PC: `tests/validate_threshold.py` and
      `tests/validate_memory_threshold.py` need a real Ollama +
      nomic-embed-text — deliberately not in the suite, never run anywhere.

## E. Doc fixes (audit Top 5)

All three verified in the tree.

- [~] E1 BROWSER.md "Limits, plainly": fix the stale OCR line (line 656:
      "Scanned PDFs need OCR, which is not in this build") — OCR **is**
      in the build (`forge/browser/ocr.py`, CHANGELOG 0.3 F2).
- [~] E2 BROWSER.md "Limits, plainly": fix the ambiguous line (line 649:
      "One engine session = one page. No parallel tabs in this build") —
      it means the engine room's CDP session, not the window; the window
      has tabs. Say so plainly.
- [~] E3 CHANGELOG: correct the v0.3 release claim (line 283:
      "GitHub Release v0.3, 2026-11-12") — no v0.3 tag or release exists
      (nor v0.4–v0.6). v0.3–v0.6 were built but shipped together in v0.7
      at Joshua's order ("ship them together"). Record that plainly;
      do NOT retro-tag releases that were never cut.
- [~] E4 One `__version__`: decide whether `forge/__init__.py`'s `"2.5"`
      (the Code Forge parent package version) belongs in this repo at all;
      align it or remove it. `forge/browser/__init__.py` says `"0.8"`.

## F. CI red — cause found (Arlo's verification, not in the audit) — BLOCKED, his GitHub billing

The auditor could not check CI (read-only tools). Checked via the API:
all 3 runs failed — 14:57Z (workflow upload), 15:06Z (v0.7 push),
17:14Z (v0.8 push) — and each lasted only **3–4 seconds**. The suite takes
~80 seconds, so the suite never ran: the jobs were killed at startup.
This matches the BNB-SKILLS CI finding exactly (same account, same day:
"your account is locked due to a billing issue — the job was not started,
3s total"). The cause is the account-wide GitHub Actions billing lock,
not the code — the suite is green locally (493/493) and from a fresh
clone, and both discover patterns collect the same 493 tests. Clearing
it is his action in GitHub billing settings.

- [ ] F1 Clear the GitHub billing lock (his action) so Actions can run.
- [ ] F2 Confirm CI green on the next push after the lock clears.

## G. Fence DNS window — documented residual

- [~] G1 Pin the connection to the checked address: resolve once, connect
      to the checked address. Closes the TOCTOU the CHANGELOG 0.7 Z entry
      names honestly ("the fetch re-resolves after the fence's check").

## H. Suite hygiene

- [~] H1 Quiet the suite: close the `LocalPageServer` sockets in the tests
      (`tests/test_browser2.py:2048, 2069, 2106`) — the green suite prints
      unclosed-socket ResourceWarnings (auditor-reported).

## I. Auditor suggestions — Joshua's call, lowest priority

- [ ] I1 Kitesurf install step (`install.sh --with-kitesurf` or a
      `browser-setup.sh` target) — only after D1 proves the build.
- [~] I2 Surface the threshold validators in `run.sh check`, reporting
      verdicts alongside `engines --check`.
- [~] I3 CLI `--version` prints the whatsnew line (engine-room parity with
      the window's "what changed" line).
- [~] I4 Lifetime stats / export for the browser (history export; research
      report export beyond the files in `~/.forge/research/`).

## J. Free CI replacement (Joshua, 2026-11-13 ~23:03) — replaces GitHub Actions

GitHub Actions is billing-locked account-wide (section F) — the jobs are killed at startup, so the repo has no push-time CI at all. Joshua's order: build a free version / workaround that works better than Actions can. It runs locally (no GitHub runners, no account, nothing to lock) and checks MORE than Actions did — Actions ran only the suite.

- [~] J1 Free CI script + verdict record: `ci/check.sh` — clones nothing, verifies the local tree pre-push (full suite under timeout, Release zip build + stored-modes + SHA check, docs-count consistency via the suite's DocsTests), and writes the verdict to `CI-STATUS.md` at the repo root (last checked commit, date, suite count + result, zip result) so the repo page itself shows the last verdict. `ci/README.md` explains why (Actions billing-locked), how to run it, and what it checks. The push discipline becomes: run `ci/check.sh`, then push (CI-STATUS.md rides in the push). Leave `.github/workflows/test.yml` in place — harmless, and Actions runs again too if the lock clears.

## Closed by verification — not tasks

- v0.8 Release exists with the exec-bit-carrying zip (verified by Arlo:
  asset downloaded back, SHA-256 matches the local build exactly).
  v0.1 / v0.2 / v0.7 also live. The auditor's read-only tools could not
  check releases.
- GitHub API `license` field reads `None` — API lag; the MIT LICENSE file
  in the tree is correct ("MIT License / Copyright (c) 2026 Joshua Lofland").
- The 6 bare `&`s in `app.py` are deliberate menu mnemonics
  (`&File`, `&Edit`, `&View`, `&Bookmarks`, `&Tools`, `&Help`) — not a
  U11 violation (auditor confirmed).
- D2 (SearXNG "secret") stays cleared — the audit re-confirmed it with its
  own grep (`search.py:279–283`, env var or setting, default empty).

## Counts

24 items: A2 + B4 + C2 + D3 + E4 + F2 + G1 + H1 + I4 + J1.
Blocked: 8 items — on his PC: C1, C2, D1, D2, D3 (5); on his GitHub
billing: F1, F2 (2); on D1's live Kitesurf build: I1 (1).
Buildable now: 16 items.
