# Forge Browser — Changelog

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
