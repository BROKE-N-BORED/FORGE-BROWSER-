# Audit #3 — Chunk-and-retrieve page feeding

Audited 2026-11-12 from the code (retrieve.py, chat.py's use of
page_context, install.sh's model handling), the tests
(RetrieveTests), and live probes of chunking, ranking, and join
order.

## What it is

Task #3 from the improvements list: stop handing the brain whole
pages. Chunk the current page, embed with nomic-embed-text,
retrieve only the chunks that match the question. Built 2026-11-11.

## What it does

- Long pages (over 6000 chars): split into ~1200-char chunks at
  line boundaries with 200-char overlap, embedded once with the
  local nomic-embed-text model via Ollama /api/embed, top 4 by
  cosine similarity to the question joined with "---" separators
  and handed to the brain (~1400 tokens).
- Short pages (6000 chars or under): go in whole, zero embed calls.
- Per-page embedding cache (4 pages, keyed by full-text SHA-256)
  in the session; chunk embeddings are reused across questions on
  the same page.
- No embeddings model: silent fallback to the first 6000 chars.
  Chat never breaks because the memory seat is missing.
- embed_texts is shared infrastructure: memory search (#12), tab
  grouping (#8), and the CLI memory/pages commands all use it.

## Strengths

- Solves the real problem: the answer at the bottom of a long page
  is now reachable (the old first-6000-chars head cut it off).
- The overlap is the right call: an answer split across a boundary
  is still found whole (probed: chunk 2 starts with chunk 1's last
  200 chars).
- Short pages skip embedding entirely (test proves zero embed
  calls): no wasted work, no model load.
- The cache means chunks are embedded once per page per session;
  repeat questions only embed the query.
- Graceful degradation: no embed model means fallback, never a
  chat failure. Tests cover it.
- Tests: chunking/overlap, ranking, embed POST shape, short-page
  whole, long-page retrieval, cache reuse.
- ~1400 tokens of context fits the 8192 ctx with room for history
  and results.

## Weaknesses (all verified by probe or code read)

1. The dependency is not installed on his PC: retrieval is
   silently OFF right now. nomic-embed-text is not in his
   11-model roster, install.sh never pulls it (it only checks
   whether `ollama` exists, for the vision seat), and nothing
   says so. Every long page on his machine today falls back to
   first-6000-chars. #3's whole benefit is dormant, and #8
   grouping plus #12 memory search (same embed dependency)
   degrade silently too.
2. The fallback is invisible, to him AND to the brain. When
   embeddings fail, page_context returns the first 6000 chars
   with no label. The brain does not know it only saw the head,
   so it can answer "not on this page" for content at the bottom.
   The error reason (Ollama down vs model missing) is discarded,
   and the two need different fixes.
3. Joined context is in rank order, not document order. PROBED:
   the joined context came out PARA6, PARA1, PARA3, PARA7
   (best first). For sequences (steps, timelines, stories) the
   brain gets scrambled context with "---" separators and no
   position labels.
4. Ties break by REVERSE document order. PROBED: 8 identical
   vectors return top 4 as [7, 6, 5, 4]. sorted(reverse=True) on
   (score, i) tuples breaks ties by descending index. When
   similarities tie (repetitive text, unrelated question), the
   brain gets the END of the page.
5. No relevance threshold. If no chunk matches the question, the
   top-4 least-bad chunks still go to the brain, which may answer
   from irrelevant context instead of saying "not on this page".
   No score floor, no "nothing matched" signal.
6. No batching or chunk cap; the 60 s timeout can fail big pages.
   All chunks go in ONE /api/embed call. A 50-100 KB article is
   roughly 50-80 chunks (~20K tokens); at CPU embedding speeds
   that can exceed the 60 s timeout, which raises BrowserError
   and silently falls back. Retrieval can silently not work on
   exactly the long pages it exists for. (The speed figure is an
   estimate; the structural point stands: unbounded work plus a
   fixed timeout means silent failure on big pages.)
7. Model-swapping cost is invisible. Each embed call loads
   nomic-embed-text (possibly evicting the chat brain from RAM);
   the next chat call reloads the brain. First question on a new
   long page is two model swaps on a CPU machine, seconds each.
   The cache helps across questions, but the cost is never shown.
8. The query includes "@tab2" noise. page_context embeds the raw
   user text including @tabN refs. Minor.
9. Overlap duplication in the joined context. When adjacent chunks
   are both picked, their 200-char overlaps appear twice (up to
   ~600 chars of ~5600 duplicated). Minor waste.
10. Cache key is the full page text hash. Any page change (live
    page, re-fetch) re-embeds everything. The cache is per-session
    and in-memory only, so a new window launch re-embeds. Minor.
11. TOP_K=4 is fixed with no tuning. For broad questions
    ("summarize this page") top-4 may miss coverage. No way to
    ask for more. Minor.

## UI flaws

- Retrieval is completely invisible: no sign of how many chunks
  were searched, whether retrieval is on or off, or which part
  of the page the answer came from. Answers do not cite chunks.
- No "retrieval off" state anywhere: no header line, no status,
  no first-run hint to pull nomic-embed-text.
- The brain's context has no chunk positions, so it cannot say
  "in the third section": it does not know where anything came
  from.

## Gaps / missing

- nomic-embed-text is not installed by install.sh; no in-app
  prompt to pull it.
- No chunk position labels in the joined context.
- No relevance floor / "nothing matched" signal.
- No batching or cap for big pages.
- No visibility (status) for retrieval state.

## Improvements (concrete, in priority order)

1. Install nomic-embed-text: pull it in install.sh (or offer it
   on first use) and show retrieval state in the dock header
   ("Retrieval: on/off, pull nomic-embed-text"). This one change
   activates #3, #8 grouping, and #12 memory search on his PC.
2. Sort picked chunks back into document order before joining,
   and label each with its position ("--- chunk 3 of 12 ---").
3. Fix the tie-break: sort by (-score, i) so ties go to the
   earliest chunk.
4. Label the fallback: when returning the first 6000 chars, tell
   the brain it only saw the head ("first 6000 chars, the rest
   was not searched").
5. Add a relevance floor: skip chunks below a similarity threshold
   and tell the brain when nothing matched.
6. Cap or batch embedding: cap chunks per call (e.g. 64) or batch
   with progress; raise or remove the fixed 60 s timeout for the
   batch.
7. Surface the embed failure reason once (Ollama down vs model
   missing) instead of discarding it.
8. Strip @tabN refs from the query before embedding.
9. Deduplicate overlaps when joining adjacent picked chunks.
10. Show retrieval state: chunks searched plus whether retrieval
    is on, in the status line or a small note.

## Verdict

#3 is well-designed and well-tested: the chunking, overlap,
cache, and fallback are all sound. But on his PC today it is a
dead feature. The model it needs is not installed, and nothing
says so. The ordering bugs (rank-order join, reverse tie-break)
are real but second to that: install the model and #3 starts
working; fix the ordering and it works correctly.
