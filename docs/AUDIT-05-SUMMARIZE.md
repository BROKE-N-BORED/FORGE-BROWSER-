# Audit #5 — Summarize this page

Audited 2026-11-12 from the code (summarize.py, chat.py summarize
ACTION, cards.py, app.py do_summarize/work_summary), the tests
(SummarizeTests), and live probes of chunking, cancel, and
blank-brain behavior.

## What it is

Task #5 from the improvements list: one click or one chat phrase
summarizes the page, map-reduce in chunks for long pages, cached
by URL + content hash. Built 2026-11-11.

## What it does

- Map-reduce: the page is split into ~1500-char chunks, each chunk
  is summarized on its own (2-3 sentences), then the chunk
  summaries are combined into one short summary (5-8 sentences).
- Cache at ~/.forge/summaries.json keyed by URL + content hash,
  newest 50 entries; a changed page never gets a stale summary.
- Single-chunk pages stream the map call live; multi-chunk pages
  keep the map calls silent and stream only the final combine.
- Three entry points: dock Summarize button, chat "summarize"
  ACTION, and cards (summarize-page, tl-dr).
- Empty page returns "" with zero brain calls.

## Strengths

- Map-reduce is the right shape for a small model: no giant
  prompt, each call is ~375 tokens of input.
- The cache by content hash means repeat asks cost nothing and a
  changed page always re-summarizes. Never stale.
- The streaming design is thoughtful: single chunk streams live,
  multi-chunk hides the scratch work and streams only the final
  combine.
- Cache failures are ignored silently — summarization never
  depends on the cache.
- The prompts pin the model ("Do not add anything the part does
  not say") — anti-hallucination in the prompt itself.
- Tests cover short/long pages, cache hit, changed text, empty
  page, and single-chunk streaming.

## Weaknesses (all verified by probe or code read)

1. No cancel path at all. PROBED: cancel() after the first map
   call did not stop anything — all 51 map calls still ran.
   summarize_page never checks _stop_requested. And the dock
   never enables the Stop button during a summarize
   (stop_btn.setEnabled(True) appears only in do_send). A
   50-chunk summarize runs on the order of 15 minutes (estimate:
   50 calls at ~20 s each) with no way to stop it.
2. No chunk cap and no second reduce level. PROBED: a 75 KB page
   means 50 map calls and a ~15,000-char reduce prompt. A 500 KB
   page would be ~330 map calls and a ~100,000-char reduce prompt
   (roughly 25,000 tokens — over the 8192 ctx, so Ollama silently
   truncates the part-summaries). No bound on the work or on the
   reduce prompt.
3. No per-chunk progress. During multi-chunk map calls nothing
   streams and the only feedback is the elapsed clock. The
   person cannot tell "part 3 of 50" from "stuck".
4. No overlap between chunks (unlike #3's 200-char overlap).
   PROBED: chunk boundaries are clean cuts with no repeated tail.
   A fact split across a chunk boundary is seen by neither map
   call whole.
5. The URL part of the cache key is dead: no caller passes a url
   (app.py, chat.py, and cards.py all call without it). The cache
   is content-hash only in practice. Harmless — same text means
   the same summary — but the docstring overclaims.
6. Blank brain output is indistinguishable from an empty page.
   PROBED: a brain that returns "" for every map call yields "".
   The dock then shows "(no summary — the page text was empty)",
   which is wrong when the page had text and the brain just
   produced nothing.
7. The chat ACTION summarize does not stream (no on_chunk passed),
   while the dock button does. Same feature, different feel.
8. The dock button always summarizes the current tab — no @tab
   support, though the chat action follows @tab. Inconsistent.
9. All map calls ride the main brain. The map step is mechanical
   (2-3 sentences per chunk) and could run on the fast seat,
   leaving the brain for the reduce. Audit #1 noted the missing
   per-task routing as a gap; this is the concrete instance.
10. Cache writes are not atomic (json.dump straight to the file).
    A crash mid-write corrupts the cache — the next load returns
    {} (handled gracefully), so the cost is lost summaries only.
    Minor.
11. No summary length control: always 5-8 sentences (multi-chunk)
    or 2-3 (single). No shorter/longer option. Minor.

## UI flaws

- No Stop during summarize (weakness 1): the button sits disabled
  while a long summarize runs.
- No "part N of M" progress (weakness 3): only the elapsed clock.
- The wrong empty message (weakness 6).
- Button vs chat inconsistency (weaknesses 7, 8).

## Gaps / missing

- No chunk cap / second-level reduce.
- No overlap between chunks.
- No fast-seat routing for the map step.
- No length option.
- No cache management: cached summaries live in
  ~/.forge/summaries.json, invisible — no way to see or clear
  them from the window.

## Improvements (concrete, in priority order)

1. Honor cancel() between map calls and enable the Stop button
   during summarize. A long summarize must be stoppable.
2. Cap the map work: cap chunks (e.g. 40) and/or add a second
   reduce level, so huge pages bound the work and the reduce
   prompt fits the ctx.
3. Per-chunk progress: an on_progress callback ("part 3 of 50")
   wired to the dock's progress label.
4. Add overlap between chunks (like #3's 200 chars) so boundary
   facts survive.
5. Distinguish empty page from empty brain output: return a
   marker or raise, and fix the dock's message.
6. Pass the URL from the callers (dock + chat know it) so the
   cache key works as documented.
7. Stream the chat ACTION summarize too (pass on_chunk through).
8. Let the dock button follow @tab (summarize the named tab when
   the entry has @tabN).
9. Route the map step to the fast seat (mechanical), keep the
   reduce on the brain.
10. Atomic cache writes (write temp + rename).
11. Length option (short/standard).

## Verdict

#5 is well-built for ordinary pages: the map-reduce shape, the
cache, and the streaming design are all sound. The holes are
scale and control: a long page means dozens of unstoppable,
unprogressed brain calls, and the reduce prompt can outgrow the
context. The missing Stop is the sharpest one — it is the only
long-running job the person cannot interrupt.
