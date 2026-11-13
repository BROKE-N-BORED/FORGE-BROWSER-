# Audit #8 — Tidy titles + tab grouping

Audited 2026-11-12 from the code (titles.py, app.py
_set_tab_title/_tidy_in_background/_apply_tidy_title/_on_load_finished,
webui.py Recent section), the tests (TitlesTests), and live probes
of truncation, grouping order, and history recording.

## What it is

Task #8 from the improvements list: fast seat renames messy
titles; tabs grouped by topic using embeddings; history grouped
into resumable "journeys". Built 2026-11-11.

## What it does

- tidy_title: the fast seat writes a 2-5 word title for long or
  messy tab titles, in the background on titleChanged. The raw
  title shows until the tidy one lands (and stays when no brain
  answers). URL-cached; local pages skipped; toggle-able.
- group_items: groups tabs by embedding similarity (cosine >=0.75
  against the group centroid, greedy first-fit), with plain domain
  grouping as the fallback. Used for the chat's tab list.
- History at ~/.forge/history.json: deduped by URL, newest first,
  200 entries. The start page shows Recent grouped by site (4
  groups x 4 entries).

## Strengths

- Tidy titles run on the fast seat — the one place per-task
  routing exists. The brain never pays for a tab title.
- The raw title shows until the tidy one lands: no blank tabs,
  no blocking.
- Stale-tidy guard: _apply_tidy_title checks the URL still matches
  before applying, so a slow tidy never lands on a new page.
- The URL cache means a revisit never re-asks.
- Toggle-able (tidy_titles + master AI switch) and local pages
  skipped.
- History never raises; non-web URLs excluded; dedup keeps it
  clean.
- The domain fallback means grouping works with no embeddings
  model at all.
- Tests cover tidy trimming, fallback, embedding grouping, domain
  fallback, and history dedup/grouping.

## Weaknesses (all verified by probe or code read)

1. The 5-word cut strands dangling words. PROBED: 'The Complete
   Guide To The', 'A Short History Of The'. Truncation takes the
   first 5 words with no regard for sense — trailing articles and
   prepositions ("the", "of", "to") are left hanging in the tab
   bar. (The test itself asserts "The Best Gears In The".)
2. "Journeys" were never built. The spec says history grouped into
   resumable "journeys"; the implementation groups by domain only.
   A research session across five sites is scattered across five
   domain groups — there is no journey to resume. The docstring
   admits the substitution ("a group is a site there").
3. History records sensitive pages with no exclusion. PROBED:
   bank and health URLs recorded. memory.py has sensitive-URL
   patterns; history does not use them. No private mode, no
   exclusion list.
4. Grouping is order-dependent. PROBED: the same three tabs in
   different orders produced different group names and membership
   order (greedy first-fit plus centroid drift). The chat's tab
   list can regroup when tabs reorder.
5. Embedding groups are named after their first tab's title, which
   may not describe the group ("Home" for a mixed group). Domain
   groups are named clearly; embedding groups are not.
6. The tidy cache is unbounded. _tidy_cache (URL -> title) has no
   limit and no eviction — it grows with every distinct URL for
   the life of the window.
7. No queue or debounce on tidy calls: every titleChanged spawns
   a thread plus a brain call. SPAs that retitle often, or several
   tabs loading at once, pile up concurrent calls on the fast
   model.
8. Tidy titles are not length-capped on display: the raw title is
   truncated to 28 chars, but the tidy title is applied whole —
   five long words can make a wide tab.
9. The embedding text is "title + URL", so the URL biases
   similarity toward same-domain grouping anyway — topic grouping
   is diluted by site identity.
10. The 0.75 threshold is untested against real embeddings
    (nomic-embed-text cosine values for short titles). On his PC
    the embedding path never runs (no nomic), so the threshold is
    a guess until the model is installed.
11. History writes are not atomic (json.dump straight to the file):
    a crash mid-write loses history (load returns [] on corrupt
    JSON). Minor.
12. Recent shows 4 groups x 4 entries with no timestamps, no "see
    all", and no way to reach older history. The other 184 entries
    are unreachable from the window.

## UI flaws

- Dangling-word titles in the tab bar (weakness 1).
- Recent is a dead end: 16 links max, no timestamps, no full
  history view, no search, no clear-history.
- No tab grouping visible in the window: groups exist only in the
  chat's tab list — the tab bar itself shows no grouping (no
  group labels or colors). The grouping is invisible where the
  person looks.
- No history management UI at all (view/search/clear).

## Gaps / missing

- Journeys (spec) not built.
- No sensitive-URL exclusion for history.
- No history page/search/clear.
- No visible tab groups in the tab bar.
- No tidy cache limit.

## Improvements (concrete, in priority order)

1. Trim trailing stop-words ("the", "a", "of", "to", "in", "and")
   after the 5-word cut — or ask the model for a title that ends
   on a content word.
2. Build journeys (spec): group history by task/session (time
   gaps + topic), resumable from the start page — or restate the
   spec to domain grouping if journeys are out.
3. Exclude sensitive URLs from history (reuse memory.py's
   patterns) plus a clear-history control.
4. Make grouping order-stable: sort items before grouping (or use
   a deterministic assignment), and name embedding groups from
   their shared topic, not the first title.
5. Cap the tidy cache (e.g. 200 entries, FIFO).
6. Queue/debounce tidy calls: one at a time, coalesce rapid
   titleChanged events per tab.
7. Cap tidy titles on display (same 28 chars as raw titles).
8. Embed titles without the URL (or weight the title higher) so
   topic grouping is not diluted by site identity.
9. Validate the 0.75 threshold against real nomic-embed-text
   output once the model is installed (needs his PC / the I1
   fix).
10. Atomic history writes (temp + rename).
11. A history view: full list with search + timestamps + clear,
    reachable from the start page.
12. Show groups in the tab bar (group labels or colors) so
    grouping is visible where the person looks.

## Verdict

#8's tidy titles are the best-routed feature in the browser
(fast seat, background, cached, guarded) — and the tab-bar
result is genuinely useful. But the 5-word cut leaves broken
titles, the "journeys" half of the spec was swapped for domain
grouping without saying so, and history has no privacy
exclusion and no UI beyond 16 Recent links.
