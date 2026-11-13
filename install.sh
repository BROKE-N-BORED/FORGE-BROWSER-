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

# --- Exec bits ------------------------------------------------------------
# A fresh clone carries no exec bits (the snapshot push stores every
# file as 100644), so restore them from EXECUTABLES.txt before the
# self-check runs — a clone install must work, not just a zip one.
if [ -f EXECUTABLES.txt ]; then
    while IFS= read -r SCRIPT || [ -n "$SCRIPT" ]; do
        case "$SCRIPT" in
            ""|\#*) ;;
            *) chmod +x "$SCRIPT" 2>/dev/null ;;
        esac
    done < EXECUTABLES.txt
fi

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
# Pinned, not nightly: version + SHA-256 from engine-pins.txt (task #21).
# The hash is verified before the binary is installed; a mismatch
# refuses the install and keeps whatever was there before.
LP_DIR="$HOME/.local/share/forge-browser"
LP="$LP_DIR/lightpanda"
LP_VER_FILE="$LP_DIR/lightpanda.version"
OS=$(uname -s)
ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64) ASSET="lightpanda-x86_64-linux" ;;
    aarch64|arm64) ASSET="lightpanda-aarch64-linux" ;;
    *) ASSET="" ;;
esac
LP_PIN=$(grep "^$ASSET " engine-pins.txt 2>/dev/null || echo "")
LP_VERSION=$(echo "$LP_PIN" | awk '{print $2}')
LP_HASH=$(echo "$LP_PIN" | awk '{print $3}')
lp_installed_ok() {
    [ -x "$LP" ] && [ -f "$LP_VER_FILE" ] \
        && [ "$(cut -d' ' -f1 "$LP_VER_FILE")" = "$LP_VERSION" ] \
        && echo "$LP_HASH  $LP" | sha256sum -c - >/dev/null 2>&1
}
if [ "$OS" != "Linux" ] || [ -z "$ASSET" ]; then
    echo "Engine: no Lightpanda build for $OS/$ARCH — skipping."
    echo "        Search, fetch, research and the vault work without an engine;"
    echo "        install Chromium later for rendered pages."
elif [ -z "$LP_VERSION" ] || [ -z "$LP_HASH" ]; then
    echo "Engine: no pin for $ASSET in engine-pins.txt — refusing to guess a version."
    FAIL=1
elif [ "$CHECK_ONLY" = "1" ]; then
    if lp_installed_ok; then
        echo "Engine: Lightpanda $LP_VERSION installed at $LP (hash verified)"
    elif [ -x "$LP" ]; then
        echo "Engine: Lightpanda installed but NOT $LP_VERSION or hash mismatch — run without --check to install $LP_VERSION"
    else
        echo "Engine: Lightpanda NOT installed (run without --check to install $LP_VERSION)"
    fi
elif lp_installed_ok; then
    echo "Engine: Lightpanda $LP_VERSION already installed (hash verified)"
else
    mkdir -p "$LP_DIR"
    echo "Engine: downloading Lightpanda $LP_VERSION ($ASSET)..."
    if curl -fsSL -o "$LP.tmp" "https://github.com/lightpanda-io/browser/releases/download/$LP_VERSION/$ASSET" \
       && echo "$LP_HASH  $LP.tmp" | sha256sum -c - >/dev/null 2>&1; then
        chmod +x "$LP.tmp" && mv "$LP.tmp" "$LP"
        echo "$LP_VERSION $LP_HASH" > "$LP_VER_FILE"
        echo "Engine: Lightpanda $LP_VERSION installed at $LP (hash verified)"
    else
        rm -f "$LP.tmp"
        if [ -x "$LP" ]; then
            echo "Engine: download or hash check FAILED — keeping the existing install"
        else
            echo "Engine: download or hash check FAILED — search/fetch/research/vault still work"
        fi
    fi
fi

# --- The browser window (Chromium) --------------------------------------
# Forge Browser IS a browser: this downloads a full Chromium build
# into its own folder so `run.sh` opens a real window. ~250 MB, one
# time. Pinned to one snapshot revision (task #21) — no more
# LAST_CHANGE, so every install fetches the same bytes. Google
# publishes no SHA-256 for snapshots; the download is verified
# against the MD5 in the object's own Google Cloud Storage metadata,
# plus a zip CRC check before extracting. A mismatch refuses the
# install and keeps whatever was there before.
CHROME_DIR="$LP_DIR/chromium"
CHROME_BIN="$CHROME_DIR/chrome-linux/chrome"
REV_FILE="$CHROME_DIR/REVISION"
CHROME_REV=$(grep "^chromium-linux-x64 " engine-pins.txt 2>/dev/null | awk '{print $2}')
case "$OS/$ARCH" in
    Linux/x86_64|Linux/amd64) CHROME_OK=1 ;;
    *) CHROME_OK=0 ;;
esac
if [ "$CHROME_OK" = "0" ]; then
    echo "Window: Chromium snapshot is Linux x86_64 — skipping on $OS/$ARCH"
elif [ -z "$CHROME_REV" ]; then
    echo "Window: no Chromium pin in engine-pins.txt — refusing to guess a revision."
    FAIL=1
elif [ "$CHECK_ONLY" = "1" ]; then
    if [ -x "$CHROME_BIN" ]; then
        echo "Window: Chromium installed (revision $(cat "$REV_FILE" 2>/dev/null || echo unknown), pinned $CHROME_REV)"
    else
        echo "Window: Chromium NOT installed (run without --check to install revision $CHROME_REV)"
    fi
elif [ -x "$CHROME_BIN" ] && [ "$(cat "$REV_FILE" 2>/dev/null)" = "$CHROME_REV" ]; then
    echo "Window: Chromium revision $CHROME_REV already installed"
else
    echo "Window: downloading Chromium revision $CHROME_REV (~250 MB, one time)..."
    mkdir -p "$CHROME_DIR"
    ZIP="$CHROME_DIR/chrome-linux.zip"
    META_URL="https://www.googleapis.com/storage/v1/b/chromium-browser-snapshots/o/Linux_x64%2F${CHROME_REV}%2Fchrome-linux.zip"
    EXPECTED_MD5=$(curl -fsSL "$META_URL" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('md5Hash',''))" 2>/dev/null)
    if curl -fsSL -o "$ZIP" \
        "https://commondatastorage.googleapis.com/chromium-browser-snapshots/Linux_x64/$CHROME_REV/chrome-linux.zip" \
       && [ -n "$EXPECTED_MD5" ] \
       && [ "$(md5sum "$ZIP" | cut -d' ' -f1 | python3 -c "import base64,binascii,sys; print(base64.b64encode(binascii.unhexlify(sys.stdin.read().strip())).decode())")" = "$EXPECTED_MD5" ] \
       && python3 -m zipfile -t "$ZIP" >/dev/null 2>&1; then
        python3 -m zipfile -e "$ZIP" "$CHROME_DIR/"
        chmod +x "$CHROME_DIR/chrome-linux/"* 2>/dev/null
        echo "$CHROME_REV" > "$REV_FILE"
        rm -f "$ZIP"
        echo "Window: Chromium revision $CHROME_REV installed at $CHROME_BIN (MD5 + zip CRC verified)"
    else
        rm -f "$ZIP"
        echo "Window: Chromium download or verification FAILED — the headless engine still works"
    fi
fi

# --- The browser program (Qt window, in its own venv) --------------------
# Forge Browser is a program with its own window. The window is Qt
# (PySide6 + Qt WebEngine — a Chromium engine inside the app), kept
# in .venv like Code Forge does, so the system Python stays clean.
# PySide6 is pinned with hashes in pyside-requirements.txt (task
# D1), like the engines in engine-pins.txt — pip refuses to install
# anything whose bytes do not match the pinned hash.
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
               && .venv/bin/python -m pip install --quiet --require-hashes \
                    -r pyside-requirements.txt; then
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
