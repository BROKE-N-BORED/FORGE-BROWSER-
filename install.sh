#!/bin/sh
# Forge Browser — install / update.
#
#   sh install.sh            check everything + install/update the engine
#   sh install.sh --check    report only, change nothing
#
# What it does: verifies Python 3.10+, installs (or refreshes) the
# Lightpanda browser engine into ~/.local/share/forge-browser/,
# reports the optional seats (Ollama for image describing, whisper
# for transcription), and runs the offline test suite. There are
# NO pip dependencies — the package is Python standard library
# only — and nothing here needs sudo.
cd "$(dirname "$0")" || exit 1
CHECK_ONLY=0
[ "$1" = "--check" ] && CHECK_ONLY=1
FAIL=0
echo "=== Forge Browser install ==="

# --- Python -------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "Python: $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') — OK"
    else
        echo "Python: too old — Forge Browser needs Python 3.10 or newer"
        FAIL=1
    fi
else
    echo "Python: python3 not found — install Python 3.10+ first"
    FAIL=1
fi

# --- Browser engine (Lightpanda) ---------------------------------------
LP_DIR="$HOME/.local/share/forge-browser"
LP="$LP_DIR/lightpanda"
OS=$(uname -s)
ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64) ASSET="lightpanda-x86_64-linux" ;;
    aarch64|arm64) ASSET="lightpanda-aarch64-linux" ;;
    *) ASSET="" ;;
esac
if [ "$OS" != "Linux" ] || [ -z "$ASSET" ]; then
    echo "Engine: no Lightpanda build for $OS/$ARCH — skipping."
    echo "        Search, fetch, research and the vault work without an engine;"
    echo "        install Chromium later for rendered pages."
elif [ "$CHECK_ONLY" = "1" ]; then
    if [ -x "$LP" ]; then
        echo "Engine: Lightpanda installed at $LP ($(du -h "$LP" | cut -f1))"
    else
        echo "Engine: Lightpanda NOT installed (run without --check to install)"
    fi
else
    mkdir -p "$LP_DIR"
    echo "Engine: downloading the latest Lightpanda nightly ($ASSET)..."
    if curl -fsSL -o "$LP.tmp" "https://github.com/lightpanda-io/browser/releases/download/nightly/$ASSET"; then
        chmod +x "$LP.tmp" && mv "$LP.tmp" "$LP"
        echo "Engine: Lightpanda installed/updated at $LP"
    else
        rm -f "$LP.tmp"
        if [ -x "$LP" ]; then
            echo "Engine: download failed — keeping the existing install"
        else
            echo "Engine: download FAILED (network?) — search/fetch/research/vault still work"
        fi
    fi
fi

# --- The browser window (Chromium) --------------------------------------
# Forge Browser IS a browser: this downloads a full Chromium build
# into its own folder so `run.sh` opens a real window. ~250 MB, one
# time; updates only when the upstream revision changes.
CHROME_DIR="$LP_DIR/chromium"
CHROME_BIN="$CHROME_DIR/chrome-linux/chrome"
REV_FILE="$CHROME_DIR/REVISION"
LAST_CHANGE_URL="https://www.googleapis.com/download/storage/v1/b/chromium-browser-snapshots/o/Linux_x64%2FLAST_CHANGE?alt=media"
case "$OS/$ARCH" in
    Linux/x86_64|Linux/amd64) CHROME_OK=1 ;;
    *) CHROME_OK=0 ;;
esac
if [ "$CHROME_OK" = "0" ]; then
    echo "Window: Chromium snapshot is Linux x86_64 — skipping on $OS/$ARCH"
elif [ "$CHECK_ONLY" = "1" ]; then
    if [ -x "$CHROME_BIN" ]; then
        echo "Window: Chromium installed (revision $(cat "$REV_FILE" 2>/dev/null || echo unknown))"
    else
        echo "Window: Chromium NOT installed (run without --check to install)"
    fi
else
    REV=$(curl -fsSL "$LAST_CHANGE_URL" 2>/dev/null || echo "")
    CUR=$(cat "$REV_FILE" 2>/dev/null || echo "")
    if [ -x "$CHROME_BIN" ] && [ -n "$REV" ] && [ "$REV" = "$CUR" ]; then
        echo "Window: Chromium up to date (revision $CUR)"
    elif [ -z "$REV" ]; then
        if [ -x "$CHROME_BIN" ]; then
            echo "Window: could not check for a newer Chromium (network?) — keeping revision ${CUR:-unknown}"
        else
            echo "Window: could not download Chromium (network?) — run install.sh again when online"
        fi
    else
        echo "Window: downloading Chromium revision $REV (~250 MB, one time)..."
        mkdir -p "$CHROME_DIR"
        if curl -fsSL -o "$CHROME_DIR/chrome-linux.zip" \
            "https://commondatastorage.googleapis.com/chromium-browser-snapshots/Linux_x64/$REV/chrome-linux.zip"; then
            python3 -m zipfile -e "$CHROME_DIR/chrome-linux.zip" "$CHROME_DIR/"
            chmod +x "$CHROME_DIR/chrome-linux/"* 2>/dev/null
            echo "$REV" > "$REV_FILE"
            rm -f "$CHROME_DIR/chrome-linux.zip"
            echo "Window: Chromium installed at $CHROME_BIN"
        else
            rm -f "$CHROME_DIR/chrome-linux.zip"
            echo "Window: Chromium download FAILED (network?) — the headless engine still works"
        fi
    fi
fi

# --- The browser program (Qt window, in its own venv) --------------------
# Forge Browser is a program with its own window. The window is Qt
# (PySide6 + Qt WebEngine — a Chromium engine inside the app), kept
# in .venv like Code Forge does, so the system Python stays clean.
if [ "$CHECK_ONLY" = "1" ]; then
    if [ -x ".venv/bin/python" ] && .venv/bin/python -c "import PySide6" 2>/dev/null; then
        echo "Program: app venv ready (PySide6 imports)"
    else
        echo "Program: app venv NOT built (run without --check to build it)"
    fi
elif ! python3 -c 'import ensurepip' 2>/dev/null; then
    echo "Program: python3-venv is missing, so the app venv cannot be built."
    echo "         Install it with:  sudo apt install python3-venv"
    echo "         (the terminal commands still work without the window)"
else
    if [ ! -x ".venv/bin/python" ]; then
        echo "Program: building the app venv (one time)..."
        python3 -m venv .venv || echo "Program: venv creation FAILED"
    fi
    if [ -x ".venv/bin/python" ]; then
        if .venv/bin/python -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
            echo "Program: PySide6 + WebEngine already installed in .venv"
        else
            echo "Program: installing PySide6 into .venv (one time, ~600 MB with the engine)..."
            if .venv/bin/python -m pip install --quiet --upgrade pip \
               && .venv/bin/python -m pip install --quiet "PySide6>=6.6" "PySide6-WebEngine"; then
                echo "Program: PySide6 installed — run.sh opens the browser window"
            else
                echo "Program: PySide6 install FAILED (network?) — terminal commands still work"
            fi
        fi
    fi
fi

# --- Optional seats (report only) ---------------------------------------
if command -v ollama >/dev/null 2>&1; then
    echo "Vision seat: Ollama found — image describing can be configured"
else
    echo "Vision seat: Ollama not found (optional — only image describing uses it)"
fi
if command -v whisper >/dev/null 2>&1 || command -v whisper-cli >/dev/null 2>&1; then
    echo "Transcription: whisper found — audio transcription available"
else
    echo "Transcription: no whisper found (optional — only audio uses it)"
fi
if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 \
   || command -v google-chrome >/dev/null 2>&1; then
    echo "Chromium: found — usable as the interactive engine"
fi

# --- Self-check -----------------------------------------------------------
echo "Self-check: running the offline test suite..."
if python3 -m unittest discover -s tests -p "test_browser*.py" 2>&1 | grep -q "^OK"; then
    echo "Self-check: all tests pass"
else
    echo "Self-check: TESTS FAILED — run: python3 -m unittest discover -s tests -p \"test_browser*.py\""
    FAIL=1
fi

echo
if [ "$FAIL" = "0" ]; then
    echo "Install done. Run it with:  sh run.sh"
    echo "Full guided test:           sh run.sh test"
else
    echo "Install finished WITH PROBLEMS (see above)."
fi
exit $FAIL
