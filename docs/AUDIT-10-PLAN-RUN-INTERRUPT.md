# Audit #10 — Plan → Run → Interrupt

Audited 2026-11-12 from the code (chat.py ask() plan gate +
cancel(), describe_action, app.py plan dialog + Stop button +
agent-tab marking, _close_tab pruning), the tests
(PlanGateTests), and live probes of gating scope and denial.

## What it is

Task #10 from the improvements list: brain shows its step plan
first; person approves; each step runs visibly in a marked tab;
a Stop button actually stops it. Confirm before anything that
submits (purchase, post, send). Built 2026-11-11.

## What it does

- ask() gates the first action of a turn on on_plan approval,
  described in plain words (describe_action). remember/forget
  re-ask every time, even inside an approved turn.
- Denial of the first action ends the turn ("Okay — I didn't do
  that."); denial of a submit action feeds "The person said no"
  back to the model as a RESULT and the turn continues.
- cancel() stops the turn at the next step boundary ("Stopped.");
  a brain call already in flight finishes first.
- Dock: a QMessageBox plan dialog (the worker waits on an event),
  a "Plan:" log line, and a Stop button enabled only mid-turn.
- Tabs the brain opens are marked 🤖 in the tab bar and the chat's
  tab list; the mark is pruned when the tab closes.

## Strengths

- The gate is real: the first action never runs without approval
  (tests prove both approve and deny).
- Submit actions (memory changes) re-ask every time, even inside
  an approved turn — the right call for the one thing that
  persists.
- describe_action is plain words, not JSON — the person approves
  a sentence, not a payload.
- Denial of a memory action feeds back to the model (it can
  continue the turn) — good design.
- Cancel is tested (stops between steps, "Stopped.").
- Agent-tab marking is pruned on close — the id-reuse worry was
  checked and does not hold (_close_tab discards the id).
- The worker waits on an event, not the GUI thread — the window
  stays alive during approval.

## Weaknesses (all verified by probe or code read)

1. Only the FIRST action is gated. PROBED: approving a search let
   a background research job start as action 2 with no approval.
   The spec says each step runs with approval; steps 2-3 are
   neither approved nor (in the dock) shown — the dock passes no
   on_action (audit #2's H5). A minutes-long research job can
   start on the strength of an approval for a search.
2. The plan wait has no timeout. state["event"].wait() blocks
   forever. The dialog is modal (QMessageBox.question), so while
   it is up the person cannot click Stop or use the window — they
   must answer. If the dialog is missed (window behind something,
   stepped away), the turn hangs indefinitely with the UI showing
   "working".
3. Stop cannot interrupt a brain call in flight. cancel() only
   lands at the next loop iteration; a streaming generation (up
   to the 240 s timeout) runs to completion first. At 3-6 tok/s
   a long reply means Stop feels dead for minutes. The streaming
   loop never checks _stop_requested.
4. First-action denial ends the turn; submit-denial continues it.
   Inconsistent: denying the first action gives the model no
   chance to propose an alternative, while denying a remember
   feeds the refusal back as a RESULT and the turn continues.
   The submit path is the better design.
5. Stop cannot interrupt a long ACTION either (research, summarize,
   compare, and card runs all happen inside one step). The
   per-feature fixes live in K1/M6/O4; the #10-level point is the
   principle: long actions should check the stop flag.
6. No "always allow" for repeated actions: every turn's first
   action needs approval, even the same search pattern every time.
   Click-fatigue with no relief valve (memory actions should
   always re-ask; searches need not).
7. The 🤖 mark is sticky for the tab's life: if the person takes
   over an agent tab and navigates it elsewhere, it stays marked
   🤖. Minor.
8. describe_action's compare fallback reads awkwardly: "Compare
   tabs the open tabs on..." when no tabs are named. Minor copy.

## UI flaws

- The modal plan dialog blocks the whole window until answered
  (weakness 2).
- No timeout or defer on the dialog — no "ask me later".
- Steps 2-3 are invisible in the dock (H5 cross-ref).
- Stop feels unresponsive during long generations (weakness 3).
- The 🤖 mark is emoji-only (no color or grouping) — subtle on a
  crowded tab bar. Minor.

## Gaps / missing

- No per-action-type approval (expensive actions always gated).
- No always-allow.
- No plan timeout.
- No streaming interrupt.
- No denial-feedback for the first action.

## Improvements (concrete, in priority order)

1. Gate expensive actions every time (research, card, open?) —
   not just the first action of the turn.
2. Timeout the plan wait (e.g. 5 min → treat as denied) and/or
   make the approval non-modal (approve in the dock, not a popup)
   so a missed dialog cannot hang the turn.
3. Check _stop_requested in the _chat streaming loop so Stop
   interrupts a generation in flight (return the partial text +
   "Stopped.").
4. Feed first-action denial back to the model like submit-denial
   (append "The person said no" as a RESULT and continue) instead
   of ending the turn.
5. "Always allow" per action type (search/open), with
   remember/forget always re-asking.
6. Unmark an agent tab when the person navigates it manually (or
   when its URL changes by hand).
7. Fix the compare describe_action copy ("Compare the open tabs
   on...").
8. Show each action in the dock as it runs — owned by #2 task H5,
   listed here as the visibility half of this finding.

## Verdict

#10's gate is real and tested — the first action never runs
unapproved, and memory changes always re-ask. But the gate
covers one step, not the turn: steps 2-3 run ungated and (in the
dock) unseen, the plan dialog can hang a turn forever with no
timeout, and Stop cannot interrupt a generation in flight. The
design is sound; the holes are all in what happens AFTER the
first approval.
