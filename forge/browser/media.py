"""Multimodal seats — the browser hears and sees through LOCAL models.

Joshua's order (2026-11-11): the browser must be multimodal — hear
and transcribe, describe what it sees and finds. It follows the
vision-seat design he set for Forge on 2026-11-09: a background local
model turns media into TEXT, and that text is what any driver model
reads. Media itself is never attached to a model that cannot parse
it, and when no local seat is configured the functions return ""
and the caller says so in plain words — a description is never
invented.

VISION — Ollama at 127.0.0.1:11434 (local, no key). Seat choice:
FORGE_VISION_MODEL when set; otherwise the first installed model
from Joshua's seat list (eriktonon/bonsai-2-uncensored:2b — vision
projector included — then dolphinlabs/qwen3.5-uncensored), then any
installed model whose Ollama capabilities report "vision", then
common vision model names. The prompt asks for a description plus
any text visible in the image (OCR), per the seat design.

TRANSCRIPTION — local backends only, first that exists wins:
FORGE_TRANSCRIBE_CMD (a command template with {file}), the `whisper`
CLI, whisper.cpp (`whisper-cli` / `main`), or an installed
faster-whisper Python package. Model size defaults to "base"
(FORGE_WHISPER_MODEL) — small enough for a CPU-only machine.
"""

import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin

from .errors import BrowserError

def ollama_url():
    """Where Ollama answers: FORGE_OLLAMA_URL, else the address
    saved in Settings, else the local default. Read at import
    time into OLLAMA_URL below, so a Settings change takes effect
    on the next launch."""
    from .settings import get_setting
    return (os.environ.get("FORGE_OLLAMA_URL", "").strip()
            or get_setting("ollama_url")
            or "http://127.0.0.1:11434").rstrip("/")


OLLAMA_URL = ollama_url()

_SEAT_PREFERENCE = ["eriktonon/bonsai-2-uncensored", "bonsai-2-uncensored",
                    "dolphinlabs/qwen3.5-uncensored", "qwen3.5-uncensored"]
_KNOWN_VISION = ["moondream", "llava", "qwen2.5vl", "qwen3-vl", "llama3.2-vision",
                 "minicpm-v", "bakllava", "gemma3"]

_DESCRIBE_PROMPT = (
    "Describe this image thoroughly for someone who cannot see it: "
    "what it shows, layout, objects, people (appearance only), colours, "
    "and context. Transcribe ALL text visible in the image exactly. "
    "If it is a screenshot, describe the interface state: what page or "
    "app, what controls are visible, and any message or error shown."
)


def _ollama_json(path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        OLLAMA_URL + path, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def installed_models():
    """Names of models the local Ollama reports, [] when unreachable."""
    try:
        data = _ollama_json("/api/tags", timeout=5)
        return [model.get("name", "") for model in data.get("models", [])]
    except Exception:
        return []


# The vision-seat pick is cached briefly (G-P11): picking from
# scratch costs /api/tags plus up to one /api/show per installed
# model, and describe + status checks ask on every call. The
# cache is keyed on the installed-model list, so a newly pulled
# (or removed) model is picked up at once; the 60 s TTL only
# bounds how long a capability answer is believed.
_VISION_PICK_TTL = 60.0
_vision_pick_cache = {"names": None, "pick": None, "at": 0.0}


def clear_vision_pick_cache():
    """Forget the cached vision pick (tests, and any caller that
    just installed or removed a model)."""
    _vision_pick_cache.update({"names": None, "pick": None, "at": 0.0})


def pick_vision_model():
    """The configured/installed local vision seat's model name, or None.
    A pick saved in the seats file wins; then the env var."""
    from .seats import load_seats
    picked = load_seats().get("vision")
    if picked:
        return picked
    override = os.environ.get("FORGE_VISION_MODEL", "").strip()
    if override:
        return override
    names = installed_models()
    if not names:
        return None
    key = tuple(names)
    now = time.monotonic()
    if (_vision_pick_cache["names"] == key
            and now - _vision_pick_cache["at"] < _VISION_PICK_TTL):
        return _vision_pick_cache["pick"]
    pick = None
    lowered = {name.lower(): name for name in names}
    for wanted in _SEAT_PREFERENCE + _KNOWN_VISION:
        for lowered_name, original in lowered.items():
            if wanted in lowered_name:
                pick = original
                break
        if pick is not None:
            break
    if pick is None:  # last resort: ask Ollama what each model can do
        for name in names:
            try:
                info = _ollama_json("/api/show", {"model": name}, timeout=5)
                if "vision" in (info.get("capabilities") or []):
                    pick = name
                    break
            except Exception:
                continue
    _vision_pick_cache.update({"names": key, "pick": pick, "at": now})
    return pick


# An image over this size is refused, not sent (G-P24): the bytes
# are base64-inflated by a third into a POST body held in RAM
# twice over, so a 32 MB photo costs ~90 MB transiently on an
# 8 GB machine — before Ollama even loads it.
MAX_DESCRIBE_IMAGE_BYTES = 10 * 1024 * 1024


def describe_image_bytes(image_bytes, prompt=_DESCRIBE_PROMPT):
    """Describe image bytes via the local vision seat; "" if none.
    Raises BrowserError when the image is over the size cap."""
    if len(image_bytes or b"") > MAX_DESCRIBE_IMAGE_BYTES:
        raise BrowserError(
            "that image is too big to describe (over 10 MB). "
            "Shrink it first and try again")
    model = pick_vision_model()
    if not model:
        return ""
    try:
        data = _ollama_json("/api/generate", {
            "model": model,
            "prompt": prompt,
            "images": [base64.b64encode(image_bytes).decode()],
            "stream": False,
        }, timeout=180)
        return (data.get("response") or "").strip()
    except Exception:
        return ""


def describe_saved_image(path, prompt=_DESCRIBE_PROMPT):
    """Describe an image file via the local vision seat. The size
    is checked before the file is read, so an oversize image is
    refused without ever being slurped into memory."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > MAX_DESCRIBE_IMAGE_BYTES:
        raise BrowserError(
            "that image is too big to describe (over 10 MB). "
            "Shrink it first and try again")
    with open(path, "rb") as handle:
        return describe_image_bytes(handle.read(), prompt=prompt)


def whisper_model_path():
    """The whisper.cpp model file: FORGE_WHISPER_MODEL_PATH, else
    ~/.forge/whisper/base.en (S1/S12) — where install.sh puts the
    downloaded model."""
    override = os.environ.get("FORGE_WHISPER_MODEL_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "whisper",
                        "base.en")


def transcribe_saved_audio_detailed(path):
    """(text, reason): the transcript of an audio file, and why
    there is none when there is none. reason is None on success;
    else "no-transcriber" (no backend installed at all),
    "failed" (a backend was tried and errored), or "no-speech"
    (a backend ran cleanly but heard nothing). The backends, in
    order: FORGE_TRANSCRIBE_CMD, the `whisper` CLI, whisper.cpp
    (`whisper-cli` / `main`), faster-whisper. Temp dirs are
    cleaned up (S5); faster-whisper never downloads a model
    unannounced (S6)."""
    template = os.environ.get("FORGE_TRANSCRIBE_CMD", "").strip()
    if template:
        try:
            with tempfile.TemporaryDirectory(prefix="forge-transcribe-") as out_dir:
                # A3: the template is split into argv and the paths
                # substituted as argv items — never through a shell.
                # A filename with spaces or shell metacharacters
                # used to execute arbitrary commands (and plain
                # spaces broke the command outright).
                argv = [part.replace("{file}", path).replace("{out_dir}", out_dir)
                        for part in shlex.split(template)]
                if not argv:
                    return ("", "failed")
                subprocess.run(argv, capture_output=True,
                               timeout=1800, check=True)
                for name in sorted(os.listdir(out_dir)):
                    if name.endswith((".txt", ".srt", ".vtt")):
                        with open(os.path.join(out_dir, name)) as handle:
                            text = handle.read().strip()
                        return (text, None if text else "no-speech")
            return ("", "failed")  # ran, produced no transcript file
        except Exception:
            return ("", "failed")
    whisper = shutil.which("whisper")
    if whisper:
        try:
            with tempfile.TemporaryDirectory(prefix="forge-transcribe-") as out_dir:
                subprocess.run([whisper, path, "--output_format", "txt",
                                "--output_dir", out_dir,
                                "--model", os.environ.get("FORGE_WHISPER_MODEL", "base"),
                                "--fp16", "False"],
                               capture_output=True, timeout=1800, check=True)
                base = os.path.splitext(os.path.basename(path))[0] + ".txt"
                with open(os.path.join(out_dir, base)) as handle:
                    text = handle.read().strip()
                return (text, None if text else "no-speech")
        except Exception:
            return ("", "failed")
    attempted = False
    for binary in ("whisper-cli", "main"):
        exe = shutil.which(binary)
        if exe and "whisper" in (exe + binary):
            attempted = True
            try:
                with tempfile.TemporaryDirectory(prefix="forge-transcribe-") as out_dir:
                    out_base = os.path.join(out_dir, "out")
                    # S1: pass the model explicitly when the
                    # configured file exists; otherwise whisper.cpp
                    # falls back to its default relative path.
                    command = [exe, "-f", path, "-otxt", "-of", out_base]
                    model = whisper_model_path()
                    if os.path.exists(model):
                        command[1:1] = ["-m", model]
                    subprocess.run(command, capture_output=True, timeout=1800,
                                   check=True)
                    with open(out_base + ".txt") as handle:
                        text = handle.read().strip()
                    return (text, None if text else "no-speech")
            except Exception:
                continue
    try:
        from faster_whisper import WhisperModel
        # S6: local-only — never download a model unannounced
        # inside a transcription call.
        model = WhisperModel(os.environ.get("FORGE_WHISPER_MODEL", "base"),
                             device="cpu", compute_type="int8",
                             local_files_only=True)
        segments, _info = model.transcribe(path)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return (text, None if text else "no-speech")
    except ImportError:
        return ("", "failed" if attempted else "no-transcriber")
    except Exception:
        return ("", "failed")


def transcribe_saved_audio(path):
    """Transcribe an audio file with a local backend; "" if none.
    (The reason a transcription failed is in
    transcribe_saved_audio_detailed.)"""
    text, _reason = transcribe_saved_audio_detailed(path)
    return text


class _MediaParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.media = {"images": [], "audio": [], "video": []}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        src = attrs.get("src") or attrs.get("data-src") or ""
        if tag == "img" and src:
            self.media["images"].append(urljoin(self.base_url, src))
        elif tag == "audio" and src:
            self.media["audio"].append(urljoin(self.base_url, src))
        elif tag == "video" and src:
            self.media["video"].append(urljoin(self.base_url, src))
        elif tag == "source" and src:
            kind = "audio" if (attrs.get("type") or "").startswith("audio") else "video"
            self.media[kind].append(urljoin(self.base_url, src))


def extract_media(html, base_url):
    """All image/audio/video URLs a page references, by kind."""
    parser = _MediaParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.media
