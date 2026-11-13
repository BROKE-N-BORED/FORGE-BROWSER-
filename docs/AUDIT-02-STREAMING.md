# Audit #2 — Streamed answers

Audited 2026-11-12 from the code (chat.py _chat + LiveDisplay,
app.py dock slots + worker, cli.py terminal chat + cards), the
tests (ChatCallTests streaming test, LiveDisplayTests), and live
probes of LiveDisplay.

## What it is

Task #2 from the improvements list: Ollama streaming into the chat
dock so words appear as they are generated instead of one long wait.
Built 2026-11-11.

## What it does

- `_chat` with on_chunk sends `"stream": true` to Ollama /api/chat,
  reads NDJSON lines, and calls on_chunk per content piece; without
  on_chunk it waits for the whole reply (stream False). Returns the
  full text either way.
- `LiveDisplay` filters streamed text for display: holds partial
  lines until they end with a newline, drops ACTION lines (the
  protocol never shows). feed() returns displayable completed
  lines; flush() returns the held remainder.
- The dock worker feeds each chunk through LiveDisplay and emits
  Qt signals; on_token opens the "Forge Browser:" paragraph on the
  first token and inserts text at the end; on_answer does not
  re-print what already streamed (no duplication); on_failed
  appends the error.
- The terminal CLI (chat + cards) and the Summarize button use the
  same LiveDisplay pattern, so one behavior holds everywhere.
- flush() is called at all four call sites (dock ask, dock
  summarize, CLI chat, CLI card), so a final line with no trailing
  newline is not lost.

## Strengths

- The core works: NDJSON streaming with per-token callbacks,
  verified by tests and the stub E2E (6 chunks over 1.80 s).
- LiveDisplay's design is sound: a split ACTION line can never
  leak (held until complete, then dropped), and split words never
  flash half-formed. The split-ACTION test proves it.
- No duplication: the live/answer handoff means streamed text is
  never re-printed. The Qt smoke verified "answer once".
- Thread-safe delivery: the worker emits Qt signals; all GUI
  updates happen on the GUI thread. No widget access from the
  worker.
- One filter shared by dock, CLI, cards, and summarize.
- The non-streaming path stays intact (ask_once and one-shot jobs
  still use stream False).

## Weaknesses (all verified by probe or code read)

1. Words appear line by line, not word by word. LiveDisplay holds
   everything until a newline. PROBED: 50 chunks of a long
   paragraph with no newline showed NOTHING until the newline
   arrived. A model writing one long paragraph (no newlines for
   200 tokens) shows silence for up to a minute at 3-6 tok/s,
   which is exactly the "feels broken" problem streaming was
   built to fix. Short-line answers feel live; long paragraphs
   feel dead.
2. The hold is over-broad. PROBED: "ACT" + "ually, here is the
   thing" stayed held until the newline, even though "ACTually"
   rules out an ACTION line after the fourth character. Only the
   prefix needs holding, not the whole line.
3. A cut-off stream leaves a partial answer with no marker. If
   _chat raises mid-stream, the exception skips display.flush()
   (the held partial line is lost) and on_failed appends the
   error as a new paragraph, so the partial answer reads as
   finished. Nothing says it was cut off.
4. The status line says "thinking" while words stream. on_step
   fires "thinking" before _chat and nothing updates it while
   tokens flow, so the working line contradicts what the user
   sees.
5. Every token yanks the view to the bottom. on_token calls
   moveCursor(End) on every token, so if the user scrolls up to
   read an earlier answer mid-stream, each token snaps the view
   back down. Same class of bug as the 2026-11-09 chat-scroll
   rupture (Code Forge).
6. The dock shows no action progress lines. The CLI prints
   "... search (12s)" per action; the dock's work() passes no
   on_action, so in the window an action's execution is visible
   only through the Plan line. CLI/dock parity gap.
7. append() inserts model text as HTML without escaping. Streamed
   tokens go through insertPlainText (safe), but the non-streamed
   path does log.append(f"<b>{who}:</b> {text}") with raw text,
   so a reply containing "<b>" or "<div>" renders as HTML.
   Inconsistent and sloppy.
8. Token signals are per-chunk with no coalescing. Fine at 3-6
   tok/s on his machine; a faster model or machine would make
   the GUI thread do a cursor move + insert + layout per token.
   A scalability note, not a live bug.

## UI flaws

- Long-paragraph silence (weakness 1) is the main one: the dock
  can sit blank for a minute while the brain is actually writing.
- Status line lies during streaming ("thinking" while answering).
- Scroll yank on every token (weakness 5).
- No streaming indicator: while tokens flow, the only sign is the
  text itself. A small "writing" marker would make the state plain.
- No action progress in the dock (weakness 6): the user cannot see
  what the brain is doing between replies except the Plan line.

## Gaps / missing

- No time-based or size-based partial flush in LiveDisplay.
- No cut-off marker and no flush on the failure path.
- No scroll-position awareness in on_token.
- No HTML escaping in append().
- No token batching for fast streams.

## Improvements (concrete, in priority order)

1. Stream partial lines: show held text once it cannot become an
   ACTION line (hold only while the buffer is still a prefix of
   "ACTION", or flush held text after ~0.5 s / ~40 chars). Keeps
   the leak protection and makes display near-real-time.
2. Only move to End if the user was already at the bottom (check
   the scrollbar before inserting). Stops the scroll yank.
3. Switch the status step to "answering" on the first token, and
   add a small "writing" marker while tokens flow.
4. On failure, flush the held remainder first and mark the partial
   stream as cut off (append "... (cut off)" to the live paragraph).
5. Pass on_action in the dock's work() so action progress lines
   match the CLI.
6. Escape text in append() (html.escape) so model replies cannot
   inject HTML.
7. Coalesce token signals (batch rapid tokens) for fast streams.

## Verdict

#2 does what its spec says for short-line answers, and the
leak protection plus no-duplication handoff are genuinely solid.
But the line-buffered display defeats the purpose for long
paragraphs: the dock can sit silent for a minute while the brain
writes. That one fix (stream partial lines) is the difference
between streaming that feels alive and streaming that only
sometimes does.
