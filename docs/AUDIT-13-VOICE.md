# Audit #13 — Push-to-talk voice

Audited 2026-11-12 from the code (voice.py, media.py
transcribe_saved_audio, app.py mic handlers, cli.py voice
command, engines.py status, install.sh whisper check), the tests
(VoiceTests), and live probes of failure modes, temp cleanup, and
the whisper.cpp invocation.

## What it is

Task #13 from the improvements list: hold a button (or a key),
speak the command, whisper.cpp base.en transcribes, text lands in
the chat box. Record-then-transcribe; no wake word, no
always-listening. Built 2026-11-11.

## What it does

- Hold the mic button (or `voice [seconds]` CLI, default 5 s):
  records 16 kHz mono WAV via arecord/parecord/ffmpeg (first
  found), killed on release; empty recordings refused (>44 bytes
  means real audio).
- Transcribed locally via the whisper chain (FORGE_TRANSCRIBE_CMD
  → whisper CLI → whisper.cpp → faster-whisper); the text lands
  in the chat entry for review, never auto-sent.

## Strengths

- The safety design is right: record-then-transcribe, no wake
  word, no always-listening, text reviewed before sending.
- The >44-byte check cleanly refuses empty recordings (no mic,
  recorder died).
- The recorder fallback chain (arecord → parecord → ffmpeg) covers
  ALSA/PulseAudio/ffmpeg setups.
- stop() is careful: SIGTERM, 5 s wait, SIGKILL fallback.
- The WAV is deleted after transcription — no accumulation.
- The CLI mirrors the dock (same Recorder, same transcribe).
- Tests cover find_recorder (none/found), record+stop, empty
  recording, delegation, and the CLI (success + no-recorder).

## Weaknesses (all verified by probe or code read)

1. The whisper.cpp backend passes no model flag. The invocation
   is [exe, "-f", path, "-otxt", "-of", out_base] — no -m.
   whisper.cpp then looks for its default model relative to the
   launch cwd (models/ggml-base.en.bin). The docstring says the
   model is FORGE_WHISPER_MODEL (default "base") but that env var
   is only used by the whisper CLI and faster-whisper paths. The
   whisper.cpp path — the one the task list names ("whisper.cpp
   base.en") — works only if the model happens to be at the
   default relative path from wherever the browser was launched.
2. No error distinction: every failure returns "". No backends,
   transcription failure, and empty audio all return "". The dock
   says "Couldn't make out anything in the recording" for all
   three — including "no transcriber installed". The person cannot
   tell a missing install from a bad recording.
3. No recording-length cap. Holding the button has no time limit.
   A long hold produces a long transcription job (whisper base on
   CPU runs roughly real-time or slower — estimate) that can hit
   the 1800 s subprocess timeout and return "" — a long recording
   silently becomes "couldn't make out anything".
4. No fallback to the next recorder. start() picks the first
   recorder on PATH; if it records nothing (mic busy, wrong
   device), stop() returns None — no retry with parecord/ffmpeg.
   Single attempt.
5. No recording feedback beyond "Listening…". No elapsed time, no
   level indication. A muted/broken mic means speaking into the
   void until release.
6. Temp dirs leak. PROBED: each transcribe call leaves its
   forge-transcribe-* dir behind (mkdtemp, never removed). Small
   files, but litter that accumulates.
7. faster-whisper downloads the model on first use. WhisperModel
   fetches from HuggingFace if not cached — a ~150 MB download
   (estimate) inside a transcription call, with no warning,
   failing offline. The browser's "no downloads" stance does not
   hold for this path.
8. The engines status under-reports transcription. engines.py
   checks only `whisper` (and FORGE_TRANSCRIBE_CMD) —
   whisper-cli/main/faster-whisper installs show as "no
   transcription". Minor.
9. The "main" binary detection depends on the install path
   containing "whisper". A whisper.cpp `main` in a dir without
   "whisper" in the path is skipped. Fragile. Minor.
10. No guard against overlapping transcriptions. Releasing and
    pressing again starts a second recording while the first
    transcription runs — two CPU-heavy whisper jobs at once.
    Minor.
11. start()/stop() can raise out of the dock's button handlers.
    Popen failure in start(), or a wedged process in stop() (kill
    + wait timeout), raise — the dock does not catch them, so the
    person gets no message. Minor/edge.
12. FORGE_TRANSCRIBE_CMD uses shell=True with string interpolation.
    The path comes from mkstemp (safe), and the template is the
    person's own env var — low risk, but a smell. Minor.

## UI flaws

- No elapsed time or level while recording (weakness 5).
- No transcription progress — only "transcribing…" while whisper
  works (30-60 s for a 30 s clip, estimate).
- One message for three failure modes (weakness 2).
- No way to pick the recorder or the microphone (no device
  selection — always "default").

## Gaps / missing

- No model path for whisper.cpp.
- No recording cap.
- No recorder fallback.
- No device selection.
- No install path for whisper (install.sh only reports presence;
  nothing installs a transcriber).

## Improvements (concrete, in priority order)

1. Pass the model to whisper.cpp: -m with a configured path
   (FORGE_WHISPER_MODEL_PATH, default ~/.forge/whisper/base.en)
   plus installing/downloading the model once with his say-so.
2. Distinguish failure reasons: return/raise distinct errors (no
   transcriber / transcription failed / no speech) so the dock
   can say the right thing.
3. Cap recording length (e.g. 2 min) with a visible cutoff, plus
   elapsed time while recording.
4. Fall back to the next recorder when the first records nothing.
5. Clean up transcribe temp dirs (shutil.rmtree after reading, or
   TemporaryDirectory).
6. Make faster-whisper local-only (no surprise download) or warn
   on first download.
7. Show a level/elapsed indicator while recording (file growth =
   audio present).
8. Report all transcription backends in engines status
   (whisper-cli/main/faster-whisper too).
9. Guard against overlapping transcriptions (disable mic while
   transcribing).
10. Catch start()/stop() errors in the dock and say so plainly.
11. Microphone/device selection (or at least an env override for
    the recorder + device).
12. Install path: install.sh offers to fetch whisper.cpp + base.en
    model (with his say-so), or documents it.

## Verdict

#13's interaction design is right — hold, speak, review, never
auto-send — and the recorder plumbing is careful. But the
transcription chain is the weak half: the whisper.cpp backend
passes no model flag, every failure looks the same, and nothing
on his PC installs a transcriber — so the feature may be entirely
dormant there, saying "couldn't make out anything" for what is
really "nothing installed".
