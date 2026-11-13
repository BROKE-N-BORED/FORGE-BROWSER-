"""OCR for scanned PDFs (task F2) — via system tools, $0.

A scanned PDF has no text layer, so the stdlib extractor in
files.py finds nothing. When the machine has tesseract (the OCR
engine) and pdftoppm (poppler, renders PDF pages to images),
this module reads the pages: pdftoppm renders the first few
pages to PNGs, tesseract reads each one, and the text comes
back with page markers. Both tools are free system packages
(sudo apt install tesseract-ocr poppler-utils) — no download,
no account, no new dependency.

When either tool is missing, ocr_pdf returns "" and the caller
says plainly what to install. Nothing here pretends a scan was
read when it was not.
"""

import glob
import os
import shutil
import subprocess
import tempfile

MAX_OCR_PAGES = 10  # bound the work — a 200-page scan is not read whole
OCR_DPI = 200       # enough for tesseract; higher is slower, not better
_OCR_TIMEOUT = 120  # seconds per tesseract call


def find_ocr_tools():
    """{"tesseract": path or None, "pdftoppm": path or None}."""
    return {"tesseract": shutil.which("tesseract"),
            "pdftoppm": shutil.which("pdftoppm")}


def ocr_available(tools=None):
    """True when both system tools are installed."""
    tools = tools if tools is not None else find_ocr_tools()
    return bool(tools.get("tesseract") and tools.get("pdftoppm"))


def ocr_pdf(path, max_pages=MAX_OCR_PAGES, tools=None):
    """Text read from a scanned PDF, or "" when it cannot be read
    (tools missing, render failed, or no text found). Pages are
    marked [page N] so the reader knows where text came from."""
    tools = tools if tools is not None else find_ocr_tools()
    if not ocr_available(tools):
        return ""
    tmp_dir = tempfile.mkdtemp(prefix="forge-ocr-")
    try:
        rendered = subprocess.run(
            [tools["pdftoppm"], "-png", "-r", str(OCR_DPI),
             "-f", "1", "-l", str(max_pages), path,
             os.path.join(tmp_dir, "page")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_OCR_TIMEOUT)
        if rendered.returncode != 0:
            return ""
        pages = []
        for index, image in enumerate(sorted(
                glob.glob(os.path.join(tmp_dir, "page-*.png"))), 1):
            try:
                run = subprocess.run(
                    [tools["tesseract"], image, "stdout"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=_OCR_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                continue
            text = run.stdout.decode("utf-8", errors="replace").strip()
            if text:
                pages.append(f"[page {index}]\n{text}")
        return "\n\n".join(pages)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
