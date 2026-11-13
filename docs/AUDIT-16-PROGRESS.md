# Audit #16 — Chat progress honesty

Audited 2026-11-12 from the code (chat.py on_step, app.py
turn_status_text/turn_done_text + tick timer + finish(), cli.py
per-action seconds + "(took Ns)"), the tests (status/done line
tests, on_step test), and live probes of the status formats.

## What it is

Task #16 from the improvements list: extend the dock's "working:
…" line with elapsed time and a cancel button per turn (pairs
with #10's Stop). Built 2026-11-11.

## What it does

- chat.ask gained on_step: "thinking" while the brain generates,
  the action kind while an action runs.
- The dock's working line is a turn_status label ticking every
  second ("working: search… — 12s"), closing with "Done in 23s."
  / "Stopped after 9s." / "Failed after 5s.". #10's Stop button
  is the per-turn cancel.
- Terminal chat prints step seconds + "(took Ns)".

## Strengths

- The elapsed clock is honest and always visible — the person
  always knows how long a turn has been running.
- The done line names the outcome (done/stopped/failed) with the
  time — no ambiguity about what happened.
- The 1 s tick keeps the line alive without log spam (moved from
  log spam to a label — the right call).
- The step names the current phase — thinking vs acting is
  visible.
- The CLI mirrors it (per-action seconds + took Ns).
- Tests cover the status format and all three done lines; the Qt
  smoke proved the tick + stop + done line.

## Weaknesses (all verified by probe or code read)

1. Step names are raw action kinds. PROBED: "working:
   recall_pages… — 12s", "working: read_page… — 12s". Jargon in
   the UI — describe_action has plain words for the plan dialog
   ("Search pages you visited for..."), but the status line shows
   the machine name.
2. The elapsed clock counts waiting-for-the-person as work.
   turn["started"] is set at send, before tab collection and the
   plan-gate wait. If the person takes 2 minutes to approve the
   plan, the done line says "Done in 150s" — most of it was their
   own approval time. The "honesty" in the feature's name is
   undermined: approval time counts as work time.
3. The status says "thinking" during the plan-gate wait. The step
   is set to "thinking" before the _chat call that produced the
   action; while the modal dialog waits for approval, the label
   reads "working: thinking… — Ns". It is waiting, not thinking.
4. No per-action durations in the dock. The dock shows only the
   current step + total elapsed. A 3-action turn never reports how
   long each step took. The CLI shows per-action timestamps, but
   turn-relative ("… search (12s)" = 12 s into the turn, not 12 s
   for the search) — which reads as the action's duration.
5. "Done in 0s." for sub-second turns. int() truncation: a 0.4 s
   turn shows "Done in 0s." Cosmetic.
6. The status line says "thinking" while words stream — H3 owns
   this. Cross-ref.
7. The status says "thinking" during tab collection — J5 owns
   this. Cross-ref.
8. No per-chunk progress for summarize — K3 owns this. Cross-ref.
9. No elapsed time on the research progress label — Q6 owns this.
   Cross-ref.
10. No tok/s readout — G10 owns this. Cross-ref.
11. The status label starts empty — before the first turn the label
    is blank (no "Ready" or idle state). Minor.

## UI flaws

- Jargon step names (weakness 1).
- Misleading phase labels during waits (weaknesses 2, 3 + H3/J5).
- No step durations (weakness 4).
- Blank idle state (weakness 11).

## Gaps / missing

- No plain-words step mapping.
- No wait-time separation.
- No per-step timing.
- No tok/s (G10).

## Improvements (concrete, in priority order)

1. Map step names through plain words ("searching…", "reading the
   page…", "searching visited pages…", "comparing…") — reuse
   describe_action's vocabulary.
2. Pause the clock during the plan-gate wait (or show the
   breakdown: "Done in 23s (12s working, 11s waiting)").
3. Set the step to "waiting for approval" while the plan dialog is
   up.
4. Show per-step durations: when a step ends, note it ("search
   took 8s") — in the dock (a small step log) and fix the CLI to
   show per-action duration, not turn-relative time.
5. Show "<1s" or one decimal for sub-second turns instead of "0s".
6. Idle text for the status label ("Ready" or the last outcome,
   kept until the next turn).
7. Cross-refs: H3 (answering step), J5 (reading tabs), K3
   (summarize progress), Q6 (research elapsed), G10 (tok/s).

## Verdict

#16 does what it says — the elapsed clock and the done line are
honest and visible, and the move from log spam to a ticking label
was the right call. But the line's words are not always honest:
it says "thinking" while waiting for approval, counts approval
time as work time, and shows machine names (recall_pages) instead
of plain words. The clock is right; the labels need the same
plain-words treatment the plan dialog got.
