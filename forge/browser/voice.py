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
"""

import os
import shutil
import subprocess
import tempfile

from .media import transcribe_saved_audio

SAMPLE_RATE = 16000

_RECORDERS = (
    ("arecord", lambda path: ["arecord", "-f", "S16_LE", "-r", str(SAMPLE_RATE),
                              "-c", "1", "-q", path]),
    ("parecord", lambda path: ["parecord", "--format=s16le", "--rate",
                               str(SAMPLE_RATE), "--channels=1", path]),
    ("ffmpeg", lambda path: ["ffmpeg", "-y", "-f", "alsa", "-i", "default",
                             "-ar", str(SAMPLE_RATE), "-ac", "1", path]),
)


def find_recorder():
    """Name of the first usable system recorder, or None."""
    for name, _command in _RECORDERS:
        if shutil.which(name):
            return name
    return None


class Recorder:
    """One push-to-talk recording. start() spawns the recorder;
    stop() kills it and returns the WAV path, or None when nothing
    usable was recorded (no mic, recorder died at once)."""

    def __init__(self):
        self._proc = None
        self.path = None
        self.name = None

    def start(self):
        self.name = find_recorder()
        if self.name is None:
            return False
        _name, command = next((n, c) for n, c in _RECORDERS
                              if n == self.name)
        handle, self.path = tempfile.mkstemp(prefix="forge-voice-", suffix=".wav")
        os.close(handle)
        self._proc = subprocess.Popen(command(self.path),
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        return True

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
