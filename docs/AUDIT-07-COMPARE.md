# Audit #7 — Cross-tab compare

Audited 2026-11-12 from the code (compare.py, chat.py compare
ACTION, app.py compare_table signal), the tests (CompareTests),
and live probes of extraction parsing, tab selection, limits,
cancel, and table rendering.

## What it is

Task #7 from the improvements list: "Compare these three" →
extract the same fields (price, specs) from each tab in separate
tiny calls, render the table in the window. Never one giant
prompt. Built 2026-11-11.

## What it does

- One ask_once per tab, each extracting the same fields as a JSON
  object from that tab alone (through the #3 retrieval path).
- Fields default to price + key specs; tab numbers come from the
  ACTION or @tab refs in the person's words; no numbers means
  the first three open tabs. Six-tab limit.
- A field not on the page reads "not listed"; a brain reply that
  is not usable JSON reads "couldn't extract".
- The table renders as an HTML page in its own window tab, and
  the brain gets the Markdown table so it can talk about it.

## Strengths

- One small call per tab — never one giant prompt. The right
  shape for a small model.
- "not listed" vs "couldn't extract" distinguishes missing data
  from a failed extraction.
- Fenced JSON is handled (```json blocks stripped).
- The HTML table escapes values (test proves it).
- The table opens in a real tab and the brain gets the Markdown
  to talk about.
- The first-three default makes "compare these" work with no
  numbers at all.
- Tests cover JSON parsing, missing keys, bad JSON, fenced JSON,
  one call per tab, table text, HTML escaping, and the chat
  action.

## Weaknesses (all verified by probe or code read)

1. A preamble before the JSON kills the whole extraction. PROBED:
   "Here is what I found:\n{...}" → all fields "couldn't
   extract". Small models often add a preamble; nothing extracts
   the {...} block from surrounding text.
2. Key casing mismatch loses extracted values. PROBED: the model
   returning {"Price": "$42"} makes price read "not listed".
   No case-insensitive key matching.
3. Tab numbers as strings silently pick the wrong tabs. PROBED:
   tabs ["1", "2"] → the isinstance(int) check drops them, and
   the code falls back to the first three tabs. The person asked
   for A and B and got A, B, and C — silently wrong.
4. The 6-tab limit drops tabs silently. PROBED: 8 tabs → 6 rows,
   no note. Neither the brain nor the person learns two tabs
   were dropped.
5. Same-title tabs make identical columns. PROBED: two "Amazon"
   tabs → "| Amazon | Amazon |". URLs are not shown, so the
   columns are indistinguishable.
6. Cancel is ignored between tabs. PROBED: cancel() after the
   first extraction → all 4 extractions still ran. Stop during
   a compare does nothing until every tab is extracted (2-3
   minutes for 6 tabs, estimate).
7. No per-tab progress. During extraction the only feedback is
   the elapsed clock; no "tab 2 of 6".
8. The comparison page is light-themed while the app is dark.
   The start page is #101216/#171b22; the table is #222-on-white
   with #f0ede6 headers. Visual mismatch.
9. The Markdown result can be truncated: the table goes back to
   the brain capped at RESULT_CAP (4500 chars). A 6-tab,
   many-field table can exceed it, and the brain then talks
   about a partial table.
10. No extraction cache: comparing the same tabs twice re-runs
    all brain calls. (Summarize has a cache; compare does not.)
    Minor.
11. Fields come only from the model's ACTION. If the model forgets
    the fields the person named ("compare on price and
    warranty"), DEFAULT_FIELDS (price + key specs) is used and
    warranty never appears. The person's words are not parsed
    for field names. Minor.
12. "not listed" is ambiguous when retrieval is off: the price
    may be listed past the 6000-char fallback head. The fix is
    #3's task I4 (label the fallback); noted here as the
    compare-side consequence.

## UI flaws

- Light-themed table in a dark app (weakness 8).
- Indistinguishable columns for same-title tabs (weakness 5).
- No per-tab progress (weakness 7).
- No Stop during compare (weakness 6).
- The comparison tab is not marked 🤖 though the brain made it.
  Minor.
- No refresh/re-run of the comparison; no export (CSV/Markdown)
  from the tab.

## Gaps / missing

- No JSON extraction from preamble text.
- No case-insensitive key matching.
- No string-number coercion for tabs.
- No limit note when tabs are dropped.
- No extraction cache.
- No refresh or export.

## Improvements (concrete, in priority order)

1. Extract the first {...} block from the reply (regex) before
   giving up — preamble-tolerant parsing.
2. Match field keys case-insensitively (normalize keys before
   lookup).
3. Coerce numeric strings in the tabs list (int("1") → 1)
   instead of silently dropping them.
4. Say so when tabs are dropped by the 6-tab limit (note in the
   result and the table).
5. Show the domain under each title in the table so same-title
   tabs are distinguishable.
6. Honor cancel() between tab extractions, plus per-tab progress
   ("tab 2 of 6").
7. Dark-theme the comparison page to match the app.
8. Do not let the Markdown result truncate silently: cap the
   fields/tabs in the brain's copy, or summarize the table when
   it exceeds RESULT_CAP.
9. Cache extractions (tab text hash + fields) so a repeat compare
   costs nothing.
10. Parse the person's words for field names when the model's
    ACTION omits them.
11. Mark the comparison tab 🤖 (brain-made) and add refresh/export
    (CSV/Markdown) to the tab.

## Verdict

#7's design is right — one small call per tab, honest
fallbacks, a real table in a real tab. But the extraction
parsing is brittle exactly where small models are sloppy:
preambles, key casing, string numbers. Each one silently
produces a wrong table ("couldn't extract", "not listed",
wrong tabs) instead of the right one. The fixes are all small
parsing tolerances.
