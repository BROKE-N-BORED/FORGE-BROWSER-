# Audit #9 — Cards: saved workflows

Audited 2026-11-12 from the code (cards.py, chat.py card ACTION,
cli.py cards/card commands), the 6 built-in cards, the tests
(CardsTests), and live probes of broken cards, header parsing,
{page} handling, and title parsing.

## What it is

Task #9 from the improvements list: plain-text prompt+action
templates in a folder ("morning prices check", "pull the specs
table"), invoked by name in chat. Ship 5-10 hand-tested ones.
Built 2026-11-11.

## What it does

- A card is a .md file: `# Title`, a prompt, and an optional
  `## Actions` section of ACTION lines. Built-ins ship in
  forge/browser/cards/ (6: summarize-page, tl-dr, key-facts,
  explain-like-im-new, find-price, research-topic); the person's
  own cards live in ~/.forge/browser-cards/ and win on a name
  clash.
- Placeholders: {question} is the person's message, {page} is the
  current page's text.
- run_card: the card's actions run in order with the real engine,
  then one brain call on the prompt + results returns the answer.
- Invoked via the chat ACTION {"type": "card"} (the turn ends with
  the card's answer) or the CLI (`cards` lists, `card <name>
  [question]` runs).

## Strengths

- The format is dead simple: a text file anyone can write, no
  code.
- The design insight is right: small models plan badly and execute
  well — a canned workflow is a reliability boost, not a shortcut.
- User cards win on a name clash (override without touching the
  install).
- A bad action line fails loudly at parse (BrowserError naming the
  line) — in parse_card.
- The 6 built-ins are well-written prompts (plain words,
  anti-hallucination: "say so plainly instead of guessing", "Do
  not add anything...").
- The final brain call gets prompt + results in one message —
  cheap (one call).
- Tests cover parsing, bad lines, built-ins, clash, and running
  three card kinds.

## Weaknesses (all verified by probe or code read)

1. A broken user card is silently skipped. PROBED: a card with a
   bad action line vanishes from list_cards (the except swallows
   parse_card's BrowserError), and load_card says "no card named
   'broken'" with no mention that the file exists but is broken.
   The person cannot tell a typo from a missing card.
2. The brain does not know which cards exist. The SYSTEM_PROMPT
   names only summarize-page as an example; the card list is never
   given to the brain. The brain cannot suggest or pick cards — it
   must guess exact names. A wrong guess ends as "That action
   failed: no card named..." in the turn.
3. Cards bypass the per-step plan gate. The person approves "Run
   the 'research-topic' workflow card" — the plan dialog shows
   only the name, not the steps. Inside, the card's actions
   (including a synchronous minutes-long research run) execute
   with no further approval and no visibility into what will run.
4. No cancel between card actions. run_card never checks
   _stop_requested. A card's research action runs synchronously
   for minutes with no way to stop it — the dock's Stop only
   lands between ask() steps, and the card runs inside one step.
5. {page} is the raw 6000-char head, not retrieval. PROBED: a
   needle at the bottom of a long page was missing from the
   prompt (6011 chars total). Cards do not use #3's page_context,
   so a card asking about the bottom of a long page answers from
   the head.
6. The "## Actions" header is case/space-sensitive. PROBED:
   "## actions" (lowercase) → the ACTION lines land in the prompt
   as text and never execute. Silent misparse — the card "runs"
   but does nothing.
7. A leading blank line breaks title parsing. PROBED: "\n# My
   Title\n..." → the title falls back to the filename. The `# Title`
   only counts if it is the first prompt line.
8. No total prompt budget: each action result is capped at 4500
   chars, but a card with 3 actions plus the prompt can exceed
   the 8192 ctx (3 x 4500 = 13.5K chars plus prompt) and Ollama
   silently truncates. No guard.
9. Card opens do not surface as tabs: the open action fetches the
   URL but (unlike chat's open) never adds it to open_requests —
   the person never sees the page. Minor.
10. load_card calls list_cards() twice (re-reads and re-parses all
    files for the error message). Minor inefficiency.
11. _fill only substitutes top-level string values — nested
    placeholders in an action would not be filled. Minor (no
    current action nests strings).

## UI flaws

- Cards are undiscoverable in the window: no cards UI in the dock,
  no list, no run button. The person must know card names (or use
  the CLI).
- No authoring help: no "new card" / "check card" command — the
  person hand-writes .md files and a typo silently kills the card
  (weakness 1).
- The plan dialog shows only the card name, not its steps
  (weakness 3).
- No card descriptions: the CLI list shows name + title only.

## Gaps / missing

- The brain never sees the card list.
- No card validation/authoring command.
- No {page} retrieval.
- No cancel/progress inside a card run.
- No per-step visibility in the plan gate.

## Improvements (concrete, in priority order)

1. Surface broken cards: list_cards should report (not swallow)
   parse failures — list the card with an error, or warn in the
   CLI/chat.
2. Give the brain the card list (names + titles) in the system
   prompt or a system message so it can pick and suggest cards.
3. Show the card's steps in the plan dialog (describe_action for
   card lists its actions) so the approval covers what will
   actually run.
4. Honor cancel() between card actions, plus progress per step.
5. Use retrieval for {page} (page_context) so cards see the
   relevant part of a long page, not just the head.
6. Accept the Actions header case-insensitively (and tolerate
   spacing variants).
7. Parse the title from the first "# " line anywhere before the
   first non-blank prompt line (skip leading blanks).
8. Budget the final prompt: cap total results to fit the ctx
   (trim oldest results first).
9. Surface card opens as tabs (return open_requests like chat's
   open does).
10. A `card check <name>` (validate) and `card new` (scaffold)
    command.
11. A cards list in the dock (names + run buttons) so cards are
    discoverable in the window.
12. Do not re-read all cards for the load error (cache the list in
    load_card).

## Verdict

#9 is a good idea well executed at the core — the format is
simple, the built-ins are well-written, and the reliability
argument is sound. But the feature is half-hidden: the brain
does not know the cards exist, the window shows no cards UI,
and a typo'd card vanishes without a word. The plan-gate bypass
(approve a name, run unseen steps including a minutes-long
research) is the sharpest design tension.
