"""Universal file handling — fetch_any(url) works for any file type.

Joshua's order (2026-11-11): the browser must work regardless of
file or extension type. So fetch dispatches on what actually came
back — the Content-Type first, the extension only when the server
sent a generic binary type:

* HTML / XHTML        -> the Item 1 Markdown conversion
* PDF                 -> text extracted by the small standard-library
                         extractor below (FlateDecode streams; the
                         common Tj / TJ text operators). Scanned-image
                         PDFs have no text layer — they are read with
                         OCR when tesseract + pdftoppm are installed
                         (ocr.py), otherwise that is reported plainly
                         with what to install, and the file is saved.
* plain text / JSON / XML / CSV / Markdown / source code
                      -> returned as text (JSON pretty-printed, CSV
                         as a Markdown table, XML as indented text)
* images              -> saved under the media folder and described
                         by the vision seat when one is configured
                         (forge/browser/media.py); the description is
                         the text a model reads, the image itself is
                         never attached to a model that cannot see it
* audio / video       -> saved; audio is transcribed by a local
                         transcription backend when one is configured
* anything else       -> saved, with its type and size reported

Every non-HTML result is a FileResult: kind, title, text (possibly
empty), the saved path when the bytes were kept, and notes saying
exactly what was and was not done. Nothing is silently dropped.
"""

import csv
import io
import json
import os
import re
import tempfile
import zlib
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from ._http import MAX_BODY_BYTES, decode_body, http_get_to_file
from .errors import BrowserError
from .html2md import html_to_markdown

_TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json",
               "application/xml", "text/xml", "application/javascript",
               "text/javascript"}
_EXT_KIND = {
    ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".svg": "image", ".bmp": "image",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio",
    ".flac": "audio", ".mp4": "video", ".webm": "video", ".mov": "video",
    ".txt": "text", ".md": "text", ".json": "text", ".csv": "text",
    ".xml": "text", ".html": "html", ".htm": "html",
}

# AB4: the extensions that agree with each kind (the inverse of
# _EXT_KIND) — a .png URL serving an image agrees, a .txt URL
# serving a PDF does not.
_KIND_EXTENSIONS = {}
for _ext, _kind in _EXT_KIND.items():
    _KIND_EXTENSIONS.setdefault(_kind, set()).add(_ext)

# AB4: the exact extension for a content type, used when the URL's
# extension disagrees with the detected kind — the file's name
# must not lie about its content.
_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html", "application/xhtml+xml": ".html",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
    "application/json": ".json", "application/xml": ".xml",
    "text/xml": ".xml", "application/javascript": ".js",
    "text/javascript": ".js",
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
    "image/avif": ".avif", "image/x-icon": ".ico",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/mp4": ".m4a",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
}

# AB1: the per-component filename limit on Linux filesystems, in
# bytes — a name with multi-byte characters counts its UTF-8
# length, not its character count.
_MAX_NAME_BYTES = 255


@dataclass
class FileResult:
    url: str
    final_url: str
    kind: str            # html | pdf | text | image | audio | video | binary
    title: str = ""
    text: str = ""
    saved_path: str = ""
    content_type: str = ""
    size_bytes: int = 0
    notes: list = field(default_factory=list)
    barriers: list = field(default_factory=list)  # gates, from the RAW html

    def to_dict(self):
        return {"url": self.url, "final_url": self.final_url, "kind": self.kind,
                "title": self.title, "text": self.text,
                "saved_path": self.saved_path, "content_type": self.content_type,
                "size_bytes": self.size_bytes, "notes": self.notes,
                "barriers": self.barriers}


def media_dir():
    path = os.environ.get("FORGE_BROWSER_MEDIA_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".forge", "browser-media")
    os.makedirs(path, exist_ok=True)
    return path


def download_dir():
    """Where window downloads go: FORGE_BROWSER_DOWNLOAD_DIR, else
    the folder picked in Settings, else ~/Downloads."""
    override = os.environ.get("FORGE_BROWSER_DOWNLOAD_DIR", "").strip()
    if override:
        path = override
    else:
        from .settings import get_setting
        path = get_setting("download_dir") or os.path.join(
            os.path.expanduser("~"), "Downloads")
    os.makedirs(path, exist_ok=True)
    return path


def _fit_stem(stem, reserved_bytes):
    """Truncate a filename stem so the stem plus the reserved
    suffix (extension, uniquification number) fits the 255-byte
    limit. A cut multi-byte character is dropped whole (decode
    with errors="ignore" loses at most the character the cut lands
    in)."""
    keep = _MAX_NAME_BYTES - reserved_bytes
    return stem.encode("utf-8")[:max(keep, 0)].decode("utf-8", errors="ignore")


def _fit_name(candidate):
    """AB1: fit a filename inside the 255-byte per-component limit,
    keeping the extension — truncate the stem, never the suffix."""
    encoded = candidate.encode("utf-8")
    if len(encoded) <= _MAX_NAME_BYTES:
        return candidate
    stem, ext = os.path.splitext(candidate)
    return _fit_stem(stem, len(ext.encode("utf-8"))) + ext


def _sanitize_name(name):
    """AB3: keep Unicode letters — \\w is Unicode-aware for str,
    so café keeps its é — replacing only what cannot be a filename
    character."""
    return re.sub(r"[^\w.-]", "_", name)


def _honest_extension(ext, kind, content_type=""):
    """AB4: the extension for a download. The URL's extension is
    kept when it agrees with the detected kind's family; when it
    disagrees, the content type names the format (a .txt URL
    serving a PDF becomes .pdf). A missing extension is supplied
    the same way. Kind "binary" carries no content info, so any
    extension agrees with it."""
    if not ext:
        return _TYPE_EXTENSIONS.get(content_type) or "." + kind
    if kind == "binary":
        return ext
    family = _KIND_EXTENSIONS.get(kind)
    if family is not None and ext.lower() in family:
        return ext
    exact = _TYPE_EXTENSIONS.get(content_type)
    if exact is not None:
        return exact
    if kind in ("pdf", "html"):
        return "." + kind
    # The kind alone cannot name the format (image/audio/video/
    # text with an unknown content type): append it, so the final
    # extension is the kind and the original is preserved.
    return ext + "." + kind


def _safe_media_name(final_url, kind, content_type=""):
    """The media-dir filename for a download (AB2–AB5): the
    basename is URL-decoded, Unicode letters are kept, degenerate
    names fall back to download.<kind>, and the extension is
    honest about the content."""
    raw = os.path.basename(urlparse(final_url).path) or ""
    name = unquote(raw)
    stem, ext = os.path.splitext(name)
    safe_stem = _sanitize_name(stem)
    if not safe_stem.strip("._-"):
        # AB5: ".", "..", or a name of nothing but unsafe chars —
        # there is no name to keep.
        return f"download.{kind}"
    return safe_stem + _honest_extension(ext, kind, content_type)


def _link_unique(tmp_path, directory, safe):
    """Atomically link a downloaded temp file into directory under
    `safe`, uniquifying (name-1, name-2, ...) on collision. The
    link call is the atomic check, so two downloads of the same
    name can never clobber each other. Returns the final path;
    the temp file is removed."""
    stem, ext = os.path.splitext(safe)
    ext_bytes = len(ext.encode("utf-8"))
    number = 0
    while True:
        # The uniquification suffix is reserved before the stem
        # is fitted — fitting the whole candidate would truncate
        # the "-N" away and retry the identical name forever.
        suffix = f"-{number}" if number else ""
        fitted = _fit_stem(stem, len(suffix.encode("utf-8")) + ext_bytes)
        candidate = fitted + suffix + ext
        path = os.path.join(directory, candidate)
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            number += 1
            continue
        except OSError as exc:
            # AB6: a filesystem failure (a shorter filename limit
            # on another filesystem, a full disk, a missing
            # directory) surfaces as a plain-words BrowserError,
            # never a raw traceback. The temp file is cleaned up.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise BrowserError(
                f"could not save the download as {candidate!r} — the "
                f"filesystem refused it ({exc})") from exc
        break
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    return path


def _keep_file(tmp_path, final_url, kind, content_type=""):
    """Move an already-downloaded temp file to its media-dir name.

    A colliding name is uniquified (report-1.pdf, report-2.pdf, ...)
    instead of silently overwriting the file already there.
    """
    return _link_unique(
        tmp_path, media_dir(),
        _safe_media_name(final_url, kind, content_type))


def keep_download(tmp_path, name, directory=None):
    """Move a finished window download to its final name in the
    download folder, uniquifying on collision with the same atomic
    link check as _keep_file. Returns the final path."""
    safe = _sanitize_name(name or "download")
    if not safe.strip("._"):
        safe = "download"
    target = directory or download_dir()
    os.makedirs(target, exist_ok=True)
    return _link_unique(tmp_path, target, safe)


def _kind_for(content_type, final_url):
    if content_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if content_type == "application/pdf":
        return "pdf"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if content_type in _TEXT_TYPES or content_type.startswith("text/"):
        return "text"
    ext = os.path.splitext(urlparse(final_url).path)[1].lower()
    return _EXT_KIND.get(ext, "binary")


def _csv_to_markdown(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    shown = rows[:200]
    # X4: ragged rows pad to the max column count, like the HTML
    # table path. X2: pipes in cells are escaped, or one cell
    # becomes two columns.
    width = max(len(row) for row in shown)
    lines = []
    for index, row in enumerate(shown):
        cells = [cell.strip().replace("|", "\\|") for cell in row]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    if len(rows) > 200:
        # X7: the truncation is named, not silent.
        lines.append(f"({len(shown)} of {len(rows)} rows shown — "
                     "the rest were cut off.)")
    return "\n".join(lines)


def extract_pdf_text(body):
    """Best-effort text from a PDF using only the standard library.

    Handles FlateDecode (or plain) content streams carrying Tj / TJ
    text-show operators with literal or hex strings, including
    CID-keyed fonts — the kind Word and Chrome produce — whose
    glyph codes only mean something through the font's /ToUnicode
    CMap, which is parsed and applied. Returns "" when the PDF has
    no extractable text layer (scans) — the caller reports that
    plainly.
    """
    streams = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", body, re.DOTALL):
        raw = match.group(1)
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = raw
        streams.append(data)
    cid_map, code_len = {}, 0
    for data in streams:
        if b"begincmap" in data:
            found_map, found_len = _parse_tounicode_cmap(data)
            if found_map:
                cid_map.update(found_map)
                code_len = code_len or found_len
    chunks = []
    for data in streams:
        if b"Tj" not in data and b"TJ" not in data:
            continue
        text_parts = []
        for token in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj|<([0-9A-Fa-f\s]+)>\s*Tj|\[(.*?)\]\s*TJ", data, re.DOTALL):
            segment = token.group(0)
            for literal in re.finditer(rb"\((?:\\.|[^\\()])*\)", segment):
                text_parts.append(_decode_pdf_bytes(
                    _pdf_unescape(literal.group(0)[1:-1]), cid_map, code_len))
            for hexed in re.finditer(rb"<([0-9A-Fa-f\s]+)>", segment):
                try:
                    raw = bytes.fromhex(hexed.group(1).decode())
                except ValueError:
                    continue
                text_parts.append(_decode_pdf_bytes(raw, cid_map, code_len))
        if text_parts:
            chunks.append(" ".join(text_parts))
    return "\n".join(chunks).strip()


def _cmap_unicode(hex_bytes):
    raw = bytes.fromhex(hex_bytes.decode())
    if len(raw) >= 2 and len(raw) % 2 == 0:
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


def _parse_tounicode_cmap(data):
    """Parse a /ToUnicode CMap stream -> ({code: text}, code byte length)."""
    mapping = {}
    code_len = 0

    def note_code(hex_code):
        nonlocal code_len
        if not code_len:
            code_len = max(1, len(hex_code) // 2)

    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block.group(1)):
            note_code(src)
            mapping[int(src, 16)] = _cmap_unicode(dst)
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.DOTALL):
        body = block.group(1)
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body):
            note_code(lo)
            start = int(_cmap_unicode(dst).encode("utf-16-be").hex() or "0", 16) \
                if len(bytes.fromhex(dst.decode())) >= 2 else int(dst, 16)
            for offset, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                mapping[code] = chr(start + offset)
        for lo, hi, dsts in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", body, re.DOTALL):
            note_code(lo)
            for offset, dst in enumerate(
                    re.findall(rb"<([0-9A-Fa-f]+)>", dsts)):
                mapping[int(lo, 16) + offset] = _cmap_unicode(dst)
    return mapping, code_len


def _decode_pdf_bytes(raw, cid_map, code_len):
    if cid_map and code_len and len(raw) >= code_len and len(raw) % code_len == 0:
        codes = [int.from_bytes(raw[i:i + code_len], "big")
                 for i in range(0, len(raw), code_len)]
        if codes and sum(1 for c in codes if c in cid_map) >= max(1, int(len(codes) * 0.8)):
            return "".join(cid_map.get(c, "") for c in codes)
    return raw.decode("latin-1", errors="replace")


def _pdf_unescape(raw):
    out = raw.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
    out = re.sub(rb"\\[nrtbf]", b" ", out)
    out = re.sub(rb"\\[0-7]{1,3}", b" ", out)
    return out


def fetch_any(url, timeout=30, describe=True, transcribe=True,
              allow_local=False, ocr=True):
    """Fetch any URL and return a FileResult for whatever it is.

    The download streams straight to a temp file first (task #17),
    so RAM stays flat no matter the size — there is no file-size
    cap (Joshua's call, 2026-11-11: his disk is the limit). Text
    kinds are read back only when they fit the in-memory cap;
    anything bigger is kept on disk and reported as saved, never
    half-read. Local addresses are refused unless allow_local
    (task #20).
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise BrowserError(f"Forge Browser only fetches http(s) — refused {url!r}")
    fd, tmp_path = tempfile.mkstemp(prefix="forge-fetch-", dir=media_dir())
    os.close(fd)
    try:
        final_url, headers, size = http_get_to_file(
            url, tmp_path, timeout=timeout, allow_local=allow_local)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    content_type = ""
    if headers is not None:
        content_type = (headers.get_content_type() or "").lower()
    kind = _kind_for(content_type, final_url)
    result = FileResult(url=url, final_url=final_url, kind=kind,
                        content_type=content_type, size_bytes=size)
    try:
        body = None
        if kind in ("html", "pdf", "text") and size <= MAX_BODY_BYTES:
            with open(tmp_path, "rb") as handle:
                body = handle.read()
        if body is None:
            # A save-kind, or a text kind too big to hold in memory:
            # the file is already on disk — keep it and say so.
            result.saved_path = _keep_file(
                tmp_path, final_url, kind, content_type)
            if kind in ("html", "pdf", "text"):
                result.notes.append(
                    f"too big to read as text ({size} bytes, over the "
                    f"{MAX_BODY_BYTES} byte limit) — saved the file instead")
            else:
                result.notes.append(f"saved the {kind} file ({size} bytes)")
            if kind == "image" and describe:
                # T4: auto-describing is an AI feature — the master
                # switch kills it (the file is still saved).
                from .seats import load_seats
                if not load_seats()["ai_enabled"]:
                    result.notes.append(
                        "AI is off, so the image was saved but not "
                        "described")
                else:
                    from .media import describe_saved_image
                    description = describe_saved_image(result.saved_path)
                    if description:
                        result.text = description
                    else:
                        result.notes.append(
                            "no local vision seat is configured, so the image was "
                            "saved but not described (see BROWSER.md: vision seat)")
            if kind == "audio" and transcribe:
                from .media import transcribe_saved_audio
                transcript = transcribe_saved_audio(result.saved_path)
                if transcript:
                    result.text = transcript
                else:
                    result.notes.append(
                        "no local transcription backend is configured, so the "
                        "audio was saved but not transcribed (see BROWSER.md)")
            return result

        if kind == "html":
            text = decode_body(body, headers)
            # Barrier detection runs on the RAW html, here, while it
            # still exists — the markup markers (password fields,
            # captcha class names) can never appear in the extracted
            # Markdown, so detecting later would miss them.
            from .barriers import detect_barriers
            result.barriers = detect_barriers(text, final_url)
            result.title, result.text, _ = html_to_markdown(text, final_url)
        elif kind == "pdf":
            result.text = extract_pdf_text(body)
            result.title = os.path.basename(urlparse(final_url).path)
            if not result.text:
                result.saved_path = _keep_file(
                    tmp_path, final_url, "pdf", "application/pdf")
                from .ocr import ocr_available, ocr_pdf
                if ocr and ocr_available():
                    result.text = ocr_pdf(result.saved_path)
                    if result.text:
                        result.notes.append(
                            "no text layer — read with OCR (tesseract)")
                    else:
                        result.notes.append(
                            "no readable text layer (likely scanned images) — "
                            "OCR found no readable text; the file was saved")
                elif ocr:
                    result.notes.append(
                        "no readable text layer (likely scanned images) — "
                        "reading it needs OCR: install tesseract and "
                        "poppler-utils (sudo apt install tesseract-ocr "
                        "poppler-utils), then fetch it again; the file "
                        "was saved")
                else:
                    result.notes.append(
                        "no readable text layer (likely scanned images) — "
                        "the file was saved")
        elif kind == "text":
            text = decode_body(body, headers)
            if content_type == "application/json" or final_url.split("?")[0].endswith(".json"):
                try:
                    result.text = json.dumps(json.loads(text), indent=2)[:200000]
                except json.JSONDecodeError:
                    result.text = text
            elif content_type == "text/csv" or final_url.split("?")[0].endswith(".csv"):
                result.text = _csv_to_markdown(text)
            else:
                result.text = text
        return result
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
