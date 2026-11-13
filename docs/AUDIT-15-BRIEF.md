# Audit #15 — Morning brief card

Audited 2026-11-12 from the code (brief.py, webui.py start page
brief section, app.py background build, chat.py brief ACTION,
cli.py brief command), the tests (BriefTests), and live probes of
the no-brain cache hole, the 12-page cap, and sensitive pages.

## What it is

Task #15 from the improvements list: on first launch of the day
(or on demand), a card on the start page summarizing yesterday's
browsing from the local memory store. Built 2026-11-11.

## What it does

- yesterday_pages: yesterday's visited pages (page store first
  with text, history fills gaps), deduped by URL, newest first.
- build_brief: cached per-day at ~/.forge/brief-YYYY-MM-DD.json;
  brain-written 3-5 sentence summary via ask_once (first 12 pages,
  title + URL + 300 chars each); a failed brain call is NOT
  cached (retries next launch); no brain means a plain list.
- The start page shows the cached brief (or "no brief yet"); a
  background build runs on launch; `brief` chat ACTION + CLI.

## Strengths

- The failed-brain-not-cached rule is the right call (a transient
  failure does not poison the day).
- The summary prompt pins the model ("Say only what the pages
  show; if the list is thin, say so in one sentence instead of
  padding") — anti-hallucination.
- A day with no pages says so plainly.
- The page-store-first design gives the summary real text, not
  just titles.
- Cached per-day: repeat views cost nothing.
- Tests cover the date filter, summarize+cache, no-brain list,
  failed-brain-not-cached, empty day, and the chat action.

## Weaknesses (all verified by probe or code read)

1. A no-brain build caches a summary-less brief forever (for that
   day). PROBED: build with no brain → cached, summary None; a
   brain available later → 0 calls, summary still None. The
   no-cache rule covers a FAILED brain call but not a MISSING
   brain. The background build on launch runs when Ollama may not
   be up yet → caches a summary-less brief → the brief never gets
   a summary that day.
2. Sensitive pages from history appear in the brief. PROBED: a
   bank visit in history lands in yesterday_pages. The page store
   excludes sensitive pages, but the history fallback does not.
   The brief lists (and summarizes from) bank/health pages on the
   start page.
3. The summary covers only the first 12 pages (the newest).
   PROBED: 20 pages → 12 in the prompt. A busy day's summary
   reflects only the latest browsing; the rest is listed but not
   summarized.
4. The summary is written from titles + 300-char heads. Each page
   contributes its title, URL, and the first 300 chars of its
   first chunk. Thin evidence — the brain summarizes headlines,
   not content.
5. The start page shows only 6 pages (cached["pages"][:6]) with
   no "see all" — the rest are hidden. The CLI/chat list all
   pages. Inconsistent.
6. No "today so far" brief. Only yesterday. An evening launch gets
   stale news; no way to brief today or the week.
7. No refresh. Once cached, the brief is fixed for the day — a bad
   summary cannot be re-rolled except by deleting the file by
   hand.
8. Brief files accumulate forever. One file per day, never pruned.
   (~1-2 MB/year — trivial, but unbounded.)
9. The chat ACTION brief blocks the turn. build_brief runs
   synchronously (one ask_once, ~30-60 s) with no streaming or
   progress. Minor.
10. DST edge: _day_bounds uses start+86400 — on a DST transition
    day the window is off by an hour. Minor.
11. The brief depends on evictable stores. If the person does not
    launch for days, yesterday's pages can be evicted from the
    100-page/200-entry stores before the brief builds — a thin or
    empty brief. Minor.
12. No kill-switch check — T2 owns this. Cross-ref.

## UI flaws

- The start page truncates to 6 pages with no "see all"
  (weakness 5).
- Sensitive pages displayed on the start page (weakness 2).
- No refresh control (weakness 7).

## Gaps / missing

- No today/week brief.
- No refresh.
- No sensitive exclusion in the brief.
- No pruning.

## Improvements (concrete, in priority order)

1. Do not cache a summary-less brief when no brain was available
   either (or rebuild a cached summary-less brief when a brain
   appears). Close the retry hole.
2. Exclude sensitive URLs from the brief (reuse is_sensitive_url)
   — both the page list and the summary prompt.
3. Summarize across the day: sample pages across the day (or
   two-pass summarize) instead of only the 12 newest.
4. Give the summary more per page (more than 300 chars, or the
   most relevant chunk).
5. Show all pages on the start page (or a "see all" link) to match
   the CLI/chat.
6. Add a "today so far" brief (and/or week) alongside yesterday.
7. A refresh control (rebuild the brief on demand).
8. Prune old brief files (e.g. keep 30 days).
9. Stream or show progress for the chat ACTION brief (or run it in
   the background like research).
10. Fix the DST edge (compute the next day's start instead of
    +86400).
11. Gate on the kill-switch — owned by T2.

## Verdict

#15 is a good feature with one real bug: the no-cache retry rule
has a hole — a launch without a brain caches a summary-less brief
that no later brain ever fixes. The summary is also thin by
design (12 newest pages, 300-char heads) and the brief surfaces
sensitive history the page store deliberately excludes.
