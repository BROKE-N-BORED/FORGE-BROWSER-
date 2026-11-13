# Audit #4 — @tab addressing

Audited 2026-11-12 from the code (chat.py ask() @tab handling,
app.py do_send() tab collection, titles.py group_items), the tests
(test_browser3.py tab-reference tests), and live probes of
ask() with fake tabs.

## What it is

Task #4 from the improvements list: "What does @tab2 say about
the price?" — let the person name tabs in chat. Built 2026-11-11
with #3.

## What it does

- ask() takes tabs=[{title, url, text, current}] in visible order.
  @tabN picks that tab's text as the page context (through the
  #3 retrieval path); read_page and summarize follow the named
  tab.
- The brain gets a numbered tab list (grouped by topic via
  embeddings, domain fallback), with the current tab marked and
  agent tabs marked 🤖.
- Out-of-range ref (@tab9 with 3 tabs): a note tells the brain to
  say so plainly. @tab mention with no tabs at all: a note says
  this chat has no tabs.
- The dock reads every tab's innerText on send (runJavaScript per
  tab), with a 5 s singleShot guard so a silent tab cannot hang
  the send.

## Strengths

- Simple and it works: the Qt smoke proved "@tab2 what is the
  price?" answered from tab 2, once.
- Out-of-range and no-tabs are handled with plain notes, not
  errors or silence.
- Numbers match the tab-bar visible order, which is what the
  person sees.
- read_page and summarize follow the named tab, so the whole
  turn stays on the tab the person meant.
- The 5 s guard means a hung tab delays the send but never
  blocks it.
- Tests cover selection, out-of-range, and no-tabs.

## Weaknesses (all verified by probe or code read)

1. Only the FIRST @tab ref is honored for context. PROBED:
   "@tab1 and @tab3, compare them" put only tab 1's text in the
   context; tab 3's text was nowhere. The compare action handles
   multiple refs, but plain chat takes refs[0] and drops the
   rest, with no sign the second ref was ignored.
2. A silent or empty named tab gives NO context and NO warning.
   PROBED: @tab2 with empty text produced no "Text of..." system
   message at all. The brain answers with no tab text and no
   explanation that the tab came back empty — it can only guess
   from the tab's title.
3. The grouped tab list shows numbers out of order. PROBED: tabs
   1 and 3 sharing a domain printed as "1. Gear guide / 3. Gear
   parts" under one group, then "2. News" under the next. The
   SYSTEM_PROMPT says tabs are "numbered from 1 in the order
   shown" — but the order shown is group order while the numbers
   are visible order. A small model can misread which number is
   which.
4. The dock reads EVERY tab's text on EVERY send, even when the
   message has no @tab ref. Each send waits for all tabs (or the
   5 s guard) before the brain starts. With many tabs or one slow
   page, every send pays up to 5 s for text nobody asked for.
5. The working line says "thinking" while the dock is actually
   collecting tab text (turn step is set before collection).
   The label misnames the phase.
6. No cap on the tab list. Twenty tabs is roughly 1600 chars of
   system message every turn (estimate), eating context for a
   list the brain mostly ignores.
7. A bare "@tab" with no number is silently ignored (the regex
   needs digits). A typo'd ref vanishes without a word. Minor.
8. The tab list grouping runs an embedding call on every send
   when the tab set changed. With nomic-embed-text missing (his
   PC today), that is a failed round trip per send before the
   domain fallback kicks in — plus the model-load/evict churn
   when the embed model IS present. The cache only helps when
   the tab set is unchanged.
9. The out-of-range note has a grammar bug: "only 1 tabs" (the
   test itself asserts that wording). Minor.
10. @tabN noise in the retrieval query is already owned by #3
    (task I8); noted here, not duplicated.

## UI flaws

- @tab is undiscoverable. The entry placeholder says "Ask for a
  page, a search, a summary…" with no @tab hint, and the start
  page never mentions it. The person has to know it exists.
- The tab bar shows titles, not numbers. Nothing in the window
  shows which tab is @tab2 — the person has to count. (Agent
  tabs get a 🤖 prefix; no tab gets a number.)
- No feedback when a named tab was empty (weakness 2): the
  person sees an answer that may have been made from nothing.
- The "thinking" label during tab collection (weakness 5).

## Gaps / missing

- No multi-tab context: only the first ref feeds the brain.
- No naming a tab by title ("@gear guide") — numbers only.
- No tab numbers shown in the window.
- No way to ask about a tab without reading all of them first
  (weakness 4).

## Improvements (concrete, in priority order)

1. Honor multiple @tab refs: join the named tabs' texts (each
   through retrieval, labeled per tab) so "@tab1 and @tab3"
   answers from both.
2. Warn when the named tab came back empty: tell the brain (and
   the log) the tab had no readable text instead of sending no
   context at all.
3. Print the tab list in visible order (numbers ascending), with
   the group name as a label on each line — not grouped blocks
   that reorder the numbers.
4. Only read other tabs' text when the message contains @tabN
   (or a compare): the current tab's text is always needed; the
   rest only on demand. Kills the per-send wait.
5. Show the working line honestly during collection ("reading
   tabs…") instead of "thinking".
6. Short-circuit the embed call when the embed model is known
   missing (cache the failure briefly) so every send does not
   pay a failed round trip. Also serves #3 and #12.
7. Cap the tab list for very many tabs (e.g. first 15 plus "and
   N more").
8. Discoverability: hint "@tab2" in the entry placeholder or a
   one-line tip on the start page; show tab numbers in the tab
   bar (or a tooltip).
9. Fix the "only 1 tabs" grammar in the out-of-range note.

## Verdict

#4 does what its spec says for the single-ref case, and the Qt
smoke proved it end to end. The gaps are all around the edges:
multi-ref, silent tabs, list ordering, per-send cost,
discoverability. The per-send read-everything is the most
user-visible cost; the dropped second ref is the most likely
"it ignored half my question" moment.
