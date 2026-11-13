# Audit #1 — Default brain upgrade

Audited 2026-11-12 from the code (chat.py, seats.py, app.py), the
tests (test_browser3.py PickModelTests + ChatCallTests), and live
probes of pick_chat_model against his real 11-model roster.

## What it is

Task #1 from the improvements list: pick_chat_model prefers
qwen3.5:4b, then qwen3:4b, then qwen3:1.7b; every chat call runs
with thinking off and num_ctx 8192. Built 2026-11-11.

## What it does

- Auto-picks the best brain from the installed models. No setup.
- Precedence: saved seat pick (seats.json, via the dock picker)
  beats the FORGE_BROWSER_CHAT_MODEL env var, beats the ordered
  preference, beats any family-hint name, beats first model, else
  None (no brain).
- Thinking off on every call: no wasted think-tokens at 3-6 tok/s
  before the answer starts.
- num_ctx 8192 on every call: the KV cache stays off the 8 GB
  machine's throat (the model advertises 256K; that would not fit).
- Embedding models are excluded from the preference and family
  loops, so nomic-embed-text is never picked as the brain there.
- Instruct variants count: qwen3:4b-instruct-2507 matches the 4b
  entry.
- No brain: _chat raises a plain-words error (start Ollama, pull a
  small chat model, browsing still works); the dock header shows
  "Brain: none answering".

## Strengths

- The top pick is right for his machine. qwen3.5:4b is installed
  (3.3 GB, pulled 2026-11-12) and the probe confirms it is picked.
- Zero-config: works out of the box with no picker interaction.
- The precedence chain is sane (his pick, then env, then auto).
- Think-off + ctx cap are the two highest-value one-line changes
  for a slow CPU machine.
- Failure messages are plain words, not tracebacks.
- Tests cover the pick order, the embed skip, env/seats
  precedence, and think-off + ctx on the wire.

## Weaknesses (all verified by probe or code read)

1. Substring "4b" matches "14b". PROBED: ["qwen3:14b", "qwen3:4b"]
   picks qwen3:14b. If he ever pulls a 14B model and it sorts
   first in Ollama's list, the brain becomes a 14B model on 8 GB
   RAM. The matcher needs a tag boundary (":4b" or a regex), not
   a bare substring. Same shape of flaw could hit "24b"/"34b".
2. The fallback chain's 2nd and 3rd picks are not installed.
   qwen3:4b and qwen3:1.7b are not on his PC. If qwen3.5:4b goes
   away, the pick falls to the unordered family-hint loop.
   PROBED on his roster minus qwen3.5:4b: phi4-mini in one list
   order, qwen3.5:2b-q4_K_M in the reversed order. The fallback
   is luck, not a decision.
3. qwen3.5:2b-q4_K_M, his actual all-rounder (1.9 GB), is not in
   the preference list at all. It only wins by list-order luck.
4. A saved or env pick is never validated. If seats.json names a
   model he later deleted, pick returns the dead name and every
   chat call fails until he re-picks. No existence check, no
   fall-through to auto.
5. An embedding model can be the brain. PROBED: ["nomic-embed-text"]
   picks nomic-embed-text. The last-resort models[0] has no embed
   exclusion. Bites on an embed-only install (fresh pull of
   nomic-embed-text for retrieval, before any chat model).
6. No token budget against the 8192 ctx. HISTORY_CAP is 12
   messages and RESULT_CAP is 4500 chars per result, so a long
   turn's prompt can exceed 8192 tokens and Ollama silently
   drops the oldest context (possibly the page text or early
   results). Nothing measures or trims to fit the ctx it sets.
7. Ollama-down vs no-models are indistinguishable. list_models()
   swallows all exceptions and returns [], so the pick cannot
   tell "Ollama isn't running" from "no models installed". The
   dock shows the same "none answering" for both, and the fix
   is different (start Ollama vs pull a model).
8. The vision model can be picked as the chat brain. qwen3-vl
   matches the "qwen" family hint, so chat and vision can end
   up sharing one model. It can chat, but the seats blur.

## UI flaws

- Header copy is wrong: "start Ollama and reopen". Reopening is
  unnecessary; the refresh button re-picks without a restart.
  The copy should point at the refresh button.
- No pick reason shown. The header names the model but never says
  how it was picked (auto, saved, fallback). A silent fallback
  to phi4-mini looks identical to the top pick.
- No brain health shown: no answering check, no speed, no context
  use. The person cannot tell at a glance whether the brain is
  healthy.
- The Brain dropdown does not mark what (auto) would pick. If he
  clears his pick to (auto), he cannot see the auto choice until
  he hits refresh.

## Gaps / missing

- No size awareness: the picker never reads the size field from
  /api/tags. A 4b Q8 (~8 GB) is picked the same as a 4b Q4.
  On 8 GB RAM, size is what kills you.
- No answer-check on pick: the picker never verifies the model
  actually answers before adopting it. A corrupted or partial
  pull gets picked and fails on first use.
- No per-task routing: one brain for everything. Tidy titles
  already use the fast seat, but summarize, brief, and compare
  all ride the main brain.
- No "why this brain" anywhere (see UI flaws).

## Improvements (concrete, in priority order)

1. Tag-boundary matching (":4b" or regex). Kills the 14B trap.
   Small change, highest value.
2. Add ("qwen3.5", "2b") to the preference list, after the 4B
   entries. Matches his actual roster.
3. Validate the saved/env pick against the installed list; fall
   through to auto (and say so in the header) when it is gone.
4. Exclude embed models from the last-resort pick too; return
   None instead of an embed model.
5. Token-budget the prompt: estimate tokens (~4 chars each),
   trim oldest history to fit ~7000 of the 8192 ctx, keep the
   system prompt and page context pinned.
6. Size-aware picking: read size from /api/tags, skip models
   over ~4.5 GB (or warn in the header).
7. Answer-check on pick (or on refresh): a tiny call proving the
   model answers, so a dead pick shows as dead before the first
   real question.
8. Header copy fix + pick-reason label: "Brain: qwen3.5:4b
   (auto)" / "(your pick)" / "(fallback, top pick not
   installed)".
9. Distinguish Ollama-down from no-models in the header.
10. Per-turn speed readout (tok/s) in the status line, so brain
    health is visible at a glance.

## Verdict

#1 does what its spec says, and the top pick is right for his
machine today. The weaknesses are all in the edges: fallback
order, validation, matching precision. The kind of thing that
bites later, not today. The 14B trap is the sharpest one.
