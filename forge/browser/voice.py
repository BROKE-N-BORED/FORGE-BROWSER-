"""Push-to-talk voice input for the chat dock.

Hold the mic button, speak, release: the recording is transcribed
locally and the text lands in the chat entry for review — it is
never sent on its own.

Recording is a system recorder subprocess (arecord / parecord /
ffmpeg), killed on release. 16 kHz mono WAV is what whisper
wants. Transcription reuses media.transcribe_saved_audio — the
whisper.cpp backend, model FORGE_WHISPER_MODEL (default "base";
"base.en" is the English-only 148 MB one), then the whisper CLI,
then faster-whisper. No recorder or no transcriber gives a plain
sentence saying so, never a silent failure.

Recorder choice: FORGE_BROWSER_RECORDER forces one recorder
(arecord/parecord/ffmpeg); FORGE_BROWSER_MIC_DEVICE picks the
input device (arecord -D, parecord --device, ffmpeg -i). A
recorder that dies at once is skipped for the next one (S4).
Recordings are capped at MAX_RECORD_SECONDS (S3).
"""

import os
import shutil
import subprocess
import tempfile
import time

from .media import transcribe_saved_audio, transcribe_saved_audio_detailed

SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 120  # S3: a hold longer than this is cut off


def mic_device():
    """The forced input device, or "" for the default (S11)."""
    return os.environ.get("FORGE_BROWSER_MIC_DEVICE", "").strip()


def _command_for(name, path):
    device = mic_device()
    if name == "arecord":
        command = ["arecord", "-f", "S16_LE", "-r", str(SAMPLE_RATE),
                   "-c", "1", "-q"]
        if device:
            command += ["-D", device]
        return command + [path]
    if name == "parecord":
        command = ["parecord", "--format=s16le", "--rate",
                   str(SAMPLE_RATE), "--channels=1"]
        if device:
            command.append(f"--device={device}")
        return command + [path]
    return ["ffmpeg", "-y", "-f", "alsa", "-i", device or "default",
            "-ar", str(SAMPLE_RATE), "-ac", "1", path]


_RECORDERS = ("arecord", "parecord", "ffmpeg")


def available_recorders():
    """Usable recorders in preference order. FORGE_BROWSER_RECORDER
    (S11) forces one: it is the whole list when installed, and an
    empty list when it is not."""
    forced = os.environ.get("FORGE_BROWSER_RECORDER", "").strip()
    if forced:
        return [forced] if shutil.which(forced) else []
    return [name for name in _RECORDERS if shutil.which(name)]


def find_recorder():
    """Name of the first usable system recorder, or None."""
    found = available_recorders()
    return found[0] if found else None


class Recorder:
    """One push-to-talk recording. start() spawns the recorder;
    stop() kills it and returns the WAV path, or None when nothing
    usable was recorded (no mic, recorder died at once). A
    recorder that exits at once is skipped for the next one (S4).
    elapsed() is the seconds since start (S3/S7)."""

    def __init__(self):
        self._proc = None
        self.path = None
        self.name = None
        self.started = None

    def start(self):
        for name in available_recorders():
            handle, path = tempfile.mkstemp(prefix="forge-voice-", suffix=".wav")
            os.close(handle)
            try:
                proc = subprocess.Popen(_command_for(name, path),
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            # A recorder that cannot open the mic dies at once —
            # give it a moment, then try the next one (S4).
            time.sleep(0.3)
            if proc.poll() is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            self._proc = proc
            self.path = path
            self.name = name
            self.started = time.monotonic()
            return True
        return False

    def elapsed(self):
        """Seconds since start, 0.0 when not recording."""
        if self.started is None:
            return 0.0
        return time.monotonic() - self.started

    def stop(self):
        if self._proc is None:
            return None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None
        self.started = None
        if self.path and os.path.getsize(self.path) > 44:
            # A WAV header alone is 44 bytes; more means real audio.
            return self.path
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return None


def transcribe_recording(path):
    """Transcribe a recorded WAV with the local backend."""
    return transcribe_saved_audio(path)


def transcribe_recording_detailed(path):
    """(text, reason) — see media.transcribe_saved_audio_detailed
    (S2): the dock says the right thing for each failure."""
    return transcribe_saved_audio_detailed(path)
