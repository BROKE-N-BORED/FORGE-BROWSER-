# Audit #12 — Local memory store

Audited 2026-11-12 from the code (memory.py facts + pages,
chat.py memory context + recall_pages ACTION, cli.py memory
command, app.py page capture), the tests (MemoryTests), and live
probes of the cap, forget, fallbacks, tie-breaks, and chunk caps.

## What it is

Task #12 from the improvements list: visited pages chunked +
embedded into a local file store; chat can answer "that thing I
read last week…". Plus a short interests file the person can read,
edit, and delete. Off for sensitive sites; nothing ever leaves
the disk. Built 2026-11-11.

## What it does

- Facts store (~/.forge/memory.json): remember/forget/recall,
  dedup by normalized text, 200-entry cap, embedding search
  (floor 0.5, top 3) with keyword fallback, JSON export. Nothing
  is recorded automatically — only an explicit remember writes
  here, never page contents.
- Page store (~/.forge/page-memory.json): visited pages kept as
  text chunks (max 12 per page, 100 pages), embedded only on
  explicit recall (batched 200); recall_pages ACTION + `memory
  pages` CLI.
- Sensitive-URL withholding: no memory context on pages matching
  bank/paypal/login/health/etc.; sensitive pages are never stored.

## Strengths

- The privacy design is thoughtful: nothing recorded
  automatically, sensitive pages excluded from storage AND
  context, local plain JSON the person can read and edit.
- The embed-at-recall (not at visit) design avoids evicting the
  chat brain on every navigation — the right tradeoff, documented
  in the code.
- Dedup by normalized text (case/whitespace-insensitive).
- The keyword fallback for facts means recall works with no
  embeddings model.
- The 0.5 similarity floor means irrelevant entries are not
  offered.
- Tests cover dedup, forget by text/id, embedding search, and
  the keyword fallback.

## Weaknesses (all verified by probe or code read)

1. The 200-entry cap drops the NEWEST fact. PROBED: after 201
   remembers, the store held 200 — the newest (fact 200) was
   dropped, the oldest kept. _save slices entries[:200] but the
   file order is insertion order (append), so the newest entry is
   cut. The 201st remember is silently lost. (The page store does
   it right: newest-first + [:100].)
2. forget() with a short needle mass-deletes. PROBED: forget("e")
   removed 200 of 200 entries. Substring matching with no minimum
   length and no confirmation — a one-letter forget wipes the
   store. The count is reported only after deletion.
3. search_pages has no keyword fallback. PROBED: with embeddings
   down, search_pages returns [] while search_entries (same
   failure) finds the fact by keywords. On his PC today (no
   nomic), "that thing I read last week" NEVER works — recall_pages
   always says nothing matches.
4. Recall re-embeds every stored chunk, every time, with no cache.
   search_pages embeds the query + all chunks (up to 100 pages x
   12 = 1200 chunks) on each recall, in 60 s-timeout batches of
   200. At CPU embedding speeds (estimate ~100-500 tok/s), a full
   store means many minutes of embedding — and each 200-chunk
   batch must fit its 60 s timeout or the whole recall fails
   silently ([]). The feature likely fails outright on a full
   store, and is slow on a modest one.
5. A long page keeps only its first 12 chunks. PROBED: a 40 KB
   page stored 12 chunks — the rest is dropped. Later content is
   unrecallable.
6. Every chat turn re-embeds all facts. ask() calls search_entries
   each turn, which embeds [query] + all entry texts in one call
   — no caching of fact embeddings. With 200 facts, every turn
   pays a 201-text embed call (plus the brain-evict churn the page
   store was designed to avoid).
7. The embedding tie-break picks the OLDEST fact. PROBED:
   identical vectors returned the older fact first — the same
   reverse-tie-break class as #3's I3. The keyword path breaks
   ties newest-first. Inconsistent.
8. Sensitive-URL matching is substring-based: it over- and
   under-matches. "bank" flags riverbank pages and wallet-product
   pages; a banking site with a clean URL (chase.com/home) is not
   flagged. No per-site control (whole-project F3 cross-ref).
9. No way to remove a stored page. No forget for pages in chat or
   CLI — only hand-editing page-memory.json. A page stored by
   mistake (or one that later feels private) cannot be removed
   from the UI.
10. No fact editing: correcting a fact means forget + remember.
    Minor.
11. Writes are not atomic (json.dump straight to the file) — a
    crash mid-write loses the store (load returns [] on corrupt
    JSON). Minor.
12. The 0.5 floor is untested against real nomic output (same
    threshold-guess class as #8's 0.75). Minor.

## UI flaws

- No memory UI in the window: no list of remembered facts, no way
  to forget/edit from the dock — CLI only. The memory toggle
  exists, but the contents are invisible.
- No page-memory UI at all (list/remove).
- No warning before a mass forget (weakness 2).
- No sign when recall fails for lack of embeddings (weakness 3) —
  "nothing matches" is indistinguishable from "no embeddings
  model".

## Gaps / missing

- No embedding cache (facts or pages).
- No keyword fallback for pages.
- No page removal.
- No per-site memory controls (F3).
- No import (export only).

## Improvements (concrete, in priority order)

1. Fix the cap: keep the NEWEST 200 facts (slice from the end, or
   store newest-first like pages).
2. Guard forget(): require a minimum needle length (e.g. 3 chars)
   and/or confirm when the match count is large; never let a 1-2
   char needle wipe the store.
3. Add a keyword fallback to search_pages (like facts) so recall
   works without embeddings.
4. Cache embeddings: persist chunk embeddings (sidecar or in the
   page store) and fact embeddings, keyed by text hash — a recall
   embeds only the query + new/changed chunks.
5. Raise or smarten the 12-chunk cap (e.g. keep the most
   informative chunks, or raise to 24 — storage is cheap).
6. Cache fact embeddings across turns (keyed by entry id + text
   hash) so each turn does not re-embed all 200 facts.
7. Fix the tie-break: sort by (-score, i) so ties go to the
   newest (consistent with the keyword path). Same fix shape as
   I3.
8. Per-site memory controls (owned by whole-project task F3) +
   tighten sensitive matching (word-boundary or domain-aware).
9. Add page removal (chat + CLI): forget a stored page by
   URL/title.
10. Atomic writes (temp + rename) for both stores.
11. A memory view in the dock (list facts + pages, forget/remove
    buttons).
12. Validate the 0.5 floor against real nomic output once
    installed (with I1/N9).

## Verdict

#12's privacy design is the best in the browser — nothing
automatic, sensitive pages excluded, local plain JSON. But the
store has a real bug (the cap drops the newest fact), a dangerous
forget (one letter wipes everything), and the page-recall feature
is dead on his PC today (no embeddings, no keyword fallback) and
likely too slow to work even with the model installed (no
embedding cache). Fix the cap and the forget first; the cache is
what makes recall real.
