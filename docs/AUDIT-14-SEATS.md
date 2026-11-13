# Audit #14 — Model picker + AI kill-switch

Audited 2026-11-12 from the code (seats.py, app.py picker +
apply_ai_state + background brief build, media.py vision pick,
cli.py seats/voice/brief commands), the tests (SeatsTests), and
code-read verification of the refresh path and kill-switch
coverage.

## What it is

Task #14 from the improvements list: Brain / fast / vision seat
dropdowns fed by `ollama list` instead of env vars. Plus
Firefox-style AI controls: per-feature on/off and one master AI
kill-switch. Built 2026-11-11.

## What it does

- seats.py: ~/.forge/seats.json — Brain/Fast/Vision picks (saved
  pick beats env var beats auto), master ai_enabled plus
  per-feature toggles (chat/tidy_titles/memory).
  seats_enabled(feature) = master AND feature.
- Dock: three dropdowns fed by `ollama list` + ↻ refresh + AI
  master + Chat/Tidy/Memory toggles, all live. A brain switch
  re-picks the session; a fast switch clears the cached fast
  session.
- CLI `seats` mirrors the picker (list/pick/clear/toggles).
  Terminal chat refuses while AI/Chat is off.

## Strengths

- The precedence is sane and documented (saved > env > auto).
- The master switch + per-feature toggles mirror Firefox's
  pattern — familiar.
- load_seats is defensive: unknown keys ignored, wrong types
  ignored, corrupt file → defaults. Never raises.
- The toggles apply at once (no restart).
- The fast-session cache is cleared when the fast pick changes.
- The CLI mirrors the dock fully.
- Tests cover defaults, round-trips, master kill, feature
  isolation, fast default, and vision precedence.

## Weaknesses (all verified by probe or code read)

1. The ↻ refresh button does not re-pick the brain. VERIFIED BY
   CODE READ: refresh only runs fill_seat_combos() +
   apply_ai_state(); fill uses blockSignals so on_seat_changed
   (the only re-pick path) never fires; apply_ai_state only reads
   session.model. If Ollama starts after launch, ↻ shows the
   models in the dropdowns but the brain stays None, the header
   still says "none answering", and Send stays disabled. The
   header's own instruction ("start Ollama, then ↻") does not
   work — the person must change the Brain dropdown to trigger
   the re-pick.
2. The kill-switch does not cover the brief. The background brief
   build on launch (app.py:335-345) and the `brief` CLI call
   build_brief with no seats check. With AI off, the morning
   brief still builds (a brain call) every launch. The task list
   claims master off kills "brief" — it does not.
3. The kill-switch does not cover the CLI voice path. _cmd_voice
   records + transcribes with no seats check (the dock mic is
   gated via chat_ok, but the CLI is not).
4. Describe (vision) is not gated either. No seats_enabled check
   anywhere in the describe path (media.py/fetch.py/files.py).
   The task list's kill list does not mention describe, so this
   may be intended — but the master switch says "AI off" and
   image description is an AI feature. Either gate it or document
   the exemption.
5. The combo masks a dead pick as "(auto)". fill_seat_combos shows
   "(auto)" when the saved pick is not installed — but seats.json
   still holds the dead name and pick_chat_model returns it. The
   UI says auto while the brain uses a dead model and every call
   fails. (The validation fix is G3; the UI half is here: the
   combo should show the dead pick as dead, not as auto.)
6. The picker lists embedding models as Brain/Fast choices.
   fill_seat_combos adds every model from list_models() with no
   embed exclusion — the person can pick nomic-embed-text as the
   Brain. Auto-pick excludes embeds; the manual picker does not.
7. Flipping AI off mid-turn does not stop the turn. apply_ai_state
   never cancels a running turn ("Never re-enables the send
   controls mid-turn" — and never stops one either). The
   kill-switch kills the NEXT turn, not the current one.
8. No model sizes in the picker. Names only — the person cannot
   tell a 2 GB model from an 8 GB one (pairs with G6 size-aware
   picking).
9. No file locking on seats.json. Two writers (window + CLI) can
   clobber each other — last-writer-wins. Minor (single window in
   practice).
10. set_seat does not validate the name. set_seat("bogus", "x")
    writes a bogus key. Minor.
11. The Brain dropdown does not mark what (auto) would pick —
    audit #1's UI flaw, owned by G8. Cross-ref.
12. No tooltips explaining what each toggle kills. The person must
    guess what "Memory off" covers. Minor.

## UI flaws

- ↻ does not recover a brain (weakness 1) — the recovery path the
  header itself instructs is broken.
- A dead pick shown as "(auto)" (weakness 5).
- Embed models offered as brains (weakness 6).
- No sizes in the picker (weakness 8).
- No tooltips on the toggles (weakness 12).
- The kill-switch does not stop the current turn (weakness 7).

## Gaps / missing

- Brief / CLI voice / describe outside the kill-switch.
- No pick validation (G3).
- No (auto) preview (G8).

## Improvements (concrete, in priority order)

1. Make ↻ re-pick the brain (call pick_chat_model + update the
   session, like on_seat_changed does) so "start Ollama, then ↻"
   actually works.
2. Gate the brief on the kill-switch (background build + CLI):
   skip the build when AI/chat is off.
3. Gate the CLI voice command on the switch (or document voice as
   exempt).
4. Decide describe's place: gate it on the switch or document the
   exemption.
5. Show a dead saved pick as dead in the combo (e.g. "qwen3:4b
   (not installed)") instead of masking it as "(auto)" — plus the
   G3 fall-through.
6. Filter embed models out of the Brain/Fast combos (or mark
   them).
7. Cancel the running turn when AI is switched off mid-turn.
8. Show model sizes in the picker (from /api/tags).
9. Lock seats.json on write (or accept last-writer-wins and
   document it).
10. Validate seat names in set_seat.
11. Tooltips on the toggles explaining what each covers.
12. Mark what (auto) would pick — owned by G8.

## Verdict

#14's storage and precedence are solid — defensive loading, sane
precedence, full CLI parity. But the picker has a real bug (↻
does not re-pick, so the documented recovery path is broken), the
kill-switch has three holes (brief, CLI voice, describe), and the
combo hides dead picks behind "(auto)". The switch is a
kill-switch with exceptions nobody documented.
