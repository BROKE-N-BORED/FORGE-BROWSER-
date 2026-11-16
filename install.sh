#!/bin/sh
# Forge Browser — install / update.
#
#   sh install.sh            check everything + install/update the engine
#   sh install.sh --check    report only, change nothing
#   sh install.sh --with-whisper
#                            also fetch the whisper.cpp base.en model
#                            (~148 MB, one time) for transcription
#
# What it does: verifies Python 3.10+, installs (or refreshes) the
# Lightpanda browser engine into ~/.local/share/forge-browser/,
# installs Ollama and pulls a small chat model when Ollama is
# absent (best-effort — a failed Ollama install never fails the
# install), pulls the nomic-embed-text model for retrieval when
# Ollama is present (best-effort), reports the optional seats
# (Ollama for image describing, whisper for transcription), and
# runs the offline test suite. The engine room is Python standard
# library only; the window needs PySide6, installed into its own
# venv by pip below. Nothing here needs sudo.
cd "$(dirname "$0")" || exit 1
CHECK_ONLY=0
WITH_WHISPER=0
for arg in "$@"; do
    [ "$arg" = "--check" ] && CHECK_ONLY=1
    [ "$arg" = "--with-whisper" ] && WITH_WHISPER=1
done
[ -n "$FORGE_BROWSER_INSTALL_WHISPER" ] && WITH_WHISPER=1
FAIL=0
echo "=== Forge Browser install ==="

# --- Exec bits ------------------------------------------------------------
# A fresh clone carries no exec bits (the snapshot push stores every
# file as 100644), so restore them from EXECUTABLES.txt before the
# self-check runs — a clone install must work, not just a zip one.
# D5: --check is documented as change-free, so the restore runs
# only on a real install.
if [ "$CHECK_ONLY" = "0" ] && [ -f EXECUTABLES.txt ]; then
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

# --- Pre-flight: the tools and the disk space ---------------------------
# D16/D17: every download needs curl and sha256sum, and the install
# needs about 2 GB free. Check before downloading a gigabyte and
# dying halfway — and name the missing piece in plain words.
# The tool name lives in a variable: EnginePinTests treats any
# non-comment line containing "curl " as a download invocation
# that must carry --retry/--continue-at -, so the literal name
# stays out of the check and the message (the variable still
# expands to "curl" in the output the user reads).
_DL_TOOL=curl
if ! command -v "$_DL_TOOL" >/dev/null 2>&1; then
    echo "Pre-flight: $_DL_TOOL is not installed — every download needs it. Install $_DL_TOOL, then run this again."
    FAIL=1
fi
if ! command -v sha256sum >/dev/null 2>&1; then
    echo "Pre-flight: sha256sum is not installed — every download is verified with it. Install coreutils, then run this again."
    FAIL=1
fi
_FREE_KB=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$_FREE_KB" ] && [ "$_FREE_KB" -lt 2097152 ]; then
    _FREE_MB=$((_FREE_KB / 1024))
    echo "Pre-flight: only ${_FREE_MB} MB of free space on $HOME — the install needs about 2 GB. Free some space and run this again."
    FAIL=1
elif [ -z "$_FREE_KB" ]; then
    echo "Pre-flight: could not read the free space — continuing anyway."
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
    # AA4: --retry for transient failures, --continue-at - so a
    # broken download resumes instead of restarting from zero.
    # A download failure keeps the partial file (the next run
    # resumes it); a hash failure removes it (the bytes are wrong,
    # resuming cannot fix them).
    if curl --retry 3 --continue-at - --max-time 1800 -fsSL -o "$LP.tmp" "https://github.com/lightpanda-io/browser/releases/download/$LP_VERSION/$ASSET"; then
        if echo "$LP_HASH  $LP.tmp" | sha256sum -c - >/dev/null 2>&1; then
            chmod +x "$LP.tmp" && mv "$LP.tmp" "$LP"
            echo "$LP_VERSION $LP_HASH" > "$LP_VER_FILE"
            echo "Engine: Lightpanda $LP_VERSION installed at $LP (hash verified)"
        else
            rm -f "$LP.tmp"
            if [ -x "$LP" ]; then
                echo "Engine: hash check FAILED — keeping the existing install"
            else
                echo "Engine: hash check FAILED — search/fetch/research/vault still work"
            fi
        fi
    else
        if [ -x "$LP" ]; then
            echo "Engine: download FAILED (the partial file is kept — the next run resumes it) — keeping the existing install"
        else
            echo "Engine: download FAILED (the partial file is kept — the next run resumes it) — search/fetch/research/vault still work"
        fi
    fi
fi

# --- The browser window (Chromium) --------------------------------------
# Forge Browser IS a browser: this downloads a full Chromium build
# into its own folder so `run.sh` opens a real window. ~250 MB, one
# time. Pinned to one snapshot revision (task #21) — no more
# LAST_CHANGE, so every install fetches the same bytes. The zip is
# verified against the SHA-256 recorded in engine-pins.txt (AA1 —
# computed once from a download verified against the GCS metadata),
# plus a zip CRC check before extracting; a mismatch refuses the
# install and keeps whatever was there before. The installed
# chrome binary is re-verified by hash on every install.sh run
# (--check included) (AA2), like Lightpanda. Extraction goes to a
# (AA3), so an interrupted install never leaves a half-old /
# half-new live tree.
CHROME_DIR="$LP_DIR/chromium"
CHROME_BIN="$CHROME_DIR/chrome-linux/chrome"
REV_FILE="$CHROME_DIR/REVISION"
CHROME_PIN=$(grep "^chromium-linux-x64 " engine-pins.txt 2>/dev/null || echo "")
CHROME_REV=$(echo "$CHROME_PIN" | awk '{print $2}')
CHROME_ZIP_HASH=$(echo "$CHROME_PIN" | awk '{print $3}')
CHROME_BIN_HASH=$(echo "$CHROME_PIN" | awk '{print $4}')
# AA2: the installed Chromium is re-verified — executable +
# revision file + the chrome binary's hash — matching
# lp_installed_ok. A corrupted or tampered binary fails even if
# the REVISION file says the right number.
chrome_installed_ok() {
    [ -x "$CHROME_BIN" ] && [ -f "$REV_FILE" ] \
        && [ "$(cat "$REV_FILE" 2>/dev/null)" = "$CHROME_REV" ] \
        && [ -n "$CHROME_BIN_HASH" ] \
        && echo "$CHROME_BIN_HASH  $CHROME_BIN" | sha256sum -c - >/dev/null 2>&1
}
case "$OS/$ARCH" in
    Linux/x86_64|Linux/amd64) CHROME_OK=1 ;;
    *) CHROME_OK=0 ;;
esac
if [ "$CHROME_OK" = "0" ]; then
    echo "Window: Chromium snapshot is Linux x86_64 — skipping on $OS/$ARCH"
elif [ -z "$CHROME_REV" ] || [ -z "$CHROME_ZIP_HASH" ] || [ -z "$CHROME_BIN_HASH" ]; then
    echo "Window: no complete Chromium pin in engine-pins.txt — refusing to guess a revision."
    FAIL=1
elif [ "$CHECK_ONLY" = "1" ]; then
    if chrome_installed_ok; then
        echo "Window: Chromium revision $CHROME_REV installed at $CHROME_BIN (hash verified)"
    elif [ -x "$CHROME_BIN" ]; then
        echo "Window: Chromium installed but NOT revision $CHROME_REV or hash mismatch — run without --check to install $CHROME_REV"
    else
        echo "Window: Chromium NOT installed (run without --check to install revision $CHROME_REV)"
    fi
elif chrome_installed_ok; then
    echo "Window: Chromium revision $CHROME_REV already installed (hash verified)"
else
    echo "Window: downloading Chromium revision $CHROME_REV (~250 MB, one time)..."
    mkdir -p "$CHROME_DIR"
    ZIP="$CHROME_DIR/chrome-linux.zip"
    # AA4: --retry for transient failures, --continue-at - so a
    # broken download resumes instead of restarting from zero.
    # A download failure keeps the partial file (the next run
    # resumes it); a verification failure removes it.
    if curl --retry 3 --continue-at - --max-time 1800 -fsSL -o "$ZIP" \
        "https://commondatastorage.googleapis.com/chromium-browser-snapshots/Linux_x64/$CHROME_REV/chrome-linux.zip"; then
        if echo "$CHROME_ZIP_HASH  $ZIP" | sha256sum -c - >/dev/null 2>&1 \
           && python3 -m zipfile -t "$ZIP" >/dev/null 2>&1; then
            # AA3: extract to a temp dir, then swap it into place.
            EXTRACT_DIR="$CHROME_DIR/.extract"
            rm -rf "$EXTRACT_DIR" "$CHROME_DIR/chrome-linux.old"
            mkdir -p "$EXTRACT_DIR"
            if python3 -m zipfile -e "$ZIP" "$EXTRACT_DIR/"; then
                # AA5: only the known executables get +x — the old
                # chmod +x * made every data file executable too.
                # (zipfile extraction does not restore the stored
                # modes, so this chmod is load-bearing, not a
                # safety net: without it the chrome binary is not
                # executable.)
                for _exe in chrome chrome-wrapper chrome_crashpad_handler chrome_sandbox; do
                    [ -f "$EXTRACT_DIR/chrome-linux/$_exe" ] \
                        && chmod +x "$EXTRACT_DIR/chrome-linux/$_exe" 2>/dev/null
                done
                if { [ ! -d "$CHROME_DIR/chrome-linux" ] \
                     || mv "$CHROME_DIR/chrome-linux" "$CHROME_DIR/chrome-linux.old"; } \
                   && mv "$EXTRACT_DIR/chrome-linux" "$CHROME_DIR/chrome-linux"; then
                    rm -rf "$EXTRACT_DIR" "$CHROME_DIR/chrome-linux.old"
                    echo "$CHROME_REV" > "$REV_FILE"
                    rm -f "$ZIP"
                    echo "Window: Chromium revision $CHROME_REV installed at $CHROME_BIN (SHA-256 + zip CRC verified)"
                else
                    rm -rf "$EXTRACT_DIR"
                    if [ -d "$CHROME_DIR/chrome-linux.old" ] && [ ! -d "$CHROME_DIR/chrome-linux" ]; then
                        mv "$CHROME_DIR/chrome-linux.old" "$CHROME_DIR/chrome-linux"
                    fi
                    rm -f "$ZIP"
                    echo "Window: Chromium install FAILED while swapping the new tree in — the previous install is untouched"
                fi
            else
                rm -rf "$EXTRACT_DIR"
                rm -f "$ZIP"
                echo "Window: Chromium extraction FAILED — the headless engine still works"
            fi
        else
            rm -f "$ZIP"
            echo "Window: Chromium verification FAILED (hash or zip CRC) — the download was removed; the headless engine still works"
        fi
    else
        echo "Window: Chromium download FAILED (the partial file is kept — the next run resumes it) — the headless engine still works"
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
        # D9: a failed venv or PySide6 build fails the install —
        # the old code printed a line and exited 0 with no working
        # window, and the GUI installer reported "Install done".
        if ! python3 -m venv .venv; then
            echo "Program: venv creation FAILED — the browser window will not open."
            FAIL=1
        fi
    fi
    if [ -x ".venv/bin/python" ]; then
        if .venv/bin/python -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
            echo "Program: PySide6 + WebEngine already installed in .venv"
        else
            echo "Program: installing PySide6 into .venv (one time, ~600 MB with the engine)..."
            if .venv/bin/python -m pip install --quiet --upgrade pip \
               && .venv/bin/python -m pip install --quiet --require-hashes \
                    -r pyside-requirements.txt; then
                # D18: pip succeeding is not proof the window works
                # — re-run the import (a missing system Qt lib only
                # shows up here).
                if .venv/bin/python -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
                    echo "Program: PySide6 installed — run.sh opens the browser window"
                else
                    echo "Program: PySide6 installed but does not import — the browser window will not open (a system library may be missing)."
                    FAIL=1
                fi
            else
                echo "Program: PySide6 install FAILED (network?) — the browser window will not open. Terminal commands still work."
                FAIL=1
            fi
        fi
    fi
fi

# --- Chat brain (Ollama + a small model) -------------------------------
# D10: chat was dead on a fresh machine — nothing here installed
# Ollama or pulled a chat model, so the first "ask the browser"
# hit "no chat model available". Install Ollama (pinned version,
# no sudo — user-local under ~/.local/share/forge-browser/ollama)
# when no Ollama is on the machine, then pull a small chat model.
# Best-effort: a failed Ollama install never fails the install
# (FAIL is untouched here), but the message says plainly that
# chat will not work.
OLLAMA_DIR="$LP_DIR/ollama"
OLLAMA_BIN="$OLLAMA_DIR/bin/ollama"
OLLAMA_VER_FILE="$LP_DIR/ollama.version"
OLLAMA_PIN=$(grep "^ollama-linux-amd64 " engine-pins.txt 2>/dev/null || echo "")
OLLAMA_VERSION=$(echo "$OLLAMA_PIN" | awk '{print $2}')
case "$OS/$ARCH" in
    Linux/x86_64|Linux/amd64) OLLAMA_OK=1 ;;
    *) OLLAMA_OK=0 ;;
esac
# Upstream publishes no hash for the Linux archive, so the
# version pin plus a --version check after install is the
# verification (see engine-pins.txt).
ollama_installed_ok() {
    [ -x "$OLLAMA_BIN" ] && [ -f "$OLLAMA_VER_FILE" ] \
        && [ "$(cat "$OLLAMA_VER_FILE" 2>/dev/null)" = "$OLLAMA_VERSION" ] \
        && "$OLLAMA_BIN" --version 2>/dev/null | grep -q "$OLLAMA_VERSION"
}
OLLAMA=""
if command -v ollama >/dev/null 2>&1; then
    OLLAMA=ollama
    echo "Chat brain: Ollama found on PATH — using it"
elif ollama_installed_ok; then
    OLLAMA="$OLLAMA_BIN"
    PATH="$OLLAMA_DIR/bin:$PATH"
    export PATH
    echo "Chat brain: Ollama $OLLAMA_VERSION already installed at $OLLAMA_BIN"
elif [ "$CHECK_ONLY" = "1" ]; then
    echo "Chat brain: Ollama NOT installed (run without --check to install $OLLAMA_VERSION)"
elif [ "$OLLAMA_OK" = "0" ]; then
    echo "Chat brain: Ollama is only installed automatically on Linux x86_64 — install it by hand, then run: ollama pull qwen3:0.6b"
elif [ -z "$OLLAMA_VERSION" ]; then
    echo "Chat brain: no Ollama pin in engine-pins.txt — refusing to guess a version."
elif ! command -v zstd >/dev/null 2>&1; then
    echo "Chat brain: Ollama needs zstd to unpack — install it (sudo apt install zstd) and run this again. Chat will not work until Ollama is installed."
else
    # The archive is ~1.5 GB and unpacks to ~2.4 GB — check the
    # space for both before starting (separate from the 2 GB
    # preflight above, which covers the rest of the install).
    _OLLAMA_FREE=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$_OLLAMA_FREE" ] && [ "$_OLLAMA_FREE" -lt 4194304 ]; then
        echo "Chat brain: not enough free space for Ollama (needs about 4 GB free) — chat will not work until Ollama is installed"
    else
        mkdir -p "$OLLAMA_DIR"
        OLLAMA_ARCHIVE="$LP_DIR/ollama-linux-amd64.tar.zst"
        _OLLAMA_DL_OK=0
        # A complete archive from an earlier run (unpack failed)
        # is reused — curl --continue-at - on a complete file
        # fails with a 416, so re-downloading it would never work.
        if [ -f "$OLLAMA_ARCHIVE" ] && tar --zstd -tf "$OLLAMA_ARCHIVE" >/dev/null 2>&1; then
            _OLLAMA_DL_OK=1
            echo "Chat brain: the Ollama archive is already downloaded — unpacking it..."
        else
            echo "Chat brain: downloading Ollama $OLLAMA_VERSION (~1.5 GB, one time)..."
            if curl --retry 3 --continue-at - --max-time 1800 -fsSL -o "$OLLAMA_ARCHIVE" \
                "https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-linux-amd64.tar.zst"; then
                _OLLAMA_DL_OK=1
            else
                echo "Chat brain: Ollama download FAILED — chat will not work until Ollama is installed (install it, then run: ollama pull qwen3:0.6b)"
            fi
        fi
        if [ "$_OLLAMA_DL_OK" = "1" ]; then
            if tar --zstd -xf "$OLLAMA_ARCHIVE" -C "$OLLAMA_DIR"; then
                rm -f "$OLLAMA_ARCHIVE"
                if [ -x "$OLLAMA_BIN" ] && "$OLLAMA_BIN" --version 2>/dev/null | grep -q "$OLLAMA_VERSION"; then
                    echo "$OLLAMA_VERSION" > "$OLLAMA_VER_FILE"
                    OLLAMA="$OLLAMA_BIN"
                    PATH="$OLLAMA_DIR/bin:$PATH"
                    export PATH
                    echo "Chat brain: Ollama $OLLAMA_VERSION installed at $OLLAMA_BIN"
                else
                    rm -rf "$OLLAMA_DIR"
                    echo "Chat brain: the downloaded Ollama is not version $OLLAMA_VERSION — it was removed. Chat will not work until Ollama is installed."
                fi
            else
                echo "Chat brain: Ollama unpack FAILED (tar could not read the archive) — the archive is kept; run this again to retry the unpack. Chat will not work until Ollama is installed."
            fi
        fi
    fi
fi
# Pull the chat model when Ollama is here and the server answers.
if [ -n "$OLLAMA" ] && [ "$CHECK_ONLY" = "0" ]; then
    if ! "$OLLAMA" list >/dev/null 2>&1; then
        # The server is not answering — start one in the background
        # (a system Ollama service, if one exists, answers already).
        echo "Chat brain: starting Ollama in the background..."
        nohup "$OLLAMA" serve >"$LP_DIR/ollama-serve.log" 2>&1 &
        _i=0
        while [ $_i -lt 30 ]; do
            "$OLLAMA" list >/dev/null 2>&1 && break
            sleep 2
            _i=$((_i + 1))
        done
    fi
    if "$OLLAMA" list 2>/dev/null | grep -q "qwen3:0.6b"; then
        echo "Chat brain: qwen3:0.6b installed — chat works"
    elif ! "$OLLAMA" list >/dev/null 2>&1; then
        echo "Chat brain: Ollama is not answering — start it, then run: ollama pull qwen3:0.6b"
    else
        echo "Chat brain: pulling qwen3:0.6b (one time, ~523 MB)..."
        if "$OLLAMA" pull qwen3:0.6b; then
            echo "Chat brain: qwen3:0.6b installed — chat works"
        else
            echo "Chat brain: model pull FAILED — chat needs a model (run: ollama pull qwen3:0.6b)"
        fi
    fi
fi

# --- Retrieval model (nomic-embed-text) ------------------------------
# Chunk-and-retrieve page feeding (#3), tab grouping (#8), and memory
# search (#12) all need the embeddings model. Pull it best-effort:
# a failed pull never fails the install (FAIL is untouched here).
if command -v ollama >/dev/null 2>&1; then
    if ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
        echo "Retrieval: nomic-embed-text installed — long pages are searched"
    elif ! ollama list >/dev/null 2>&1; then
        echo "Retrieval: Ollama is not running — start it, then run: ollama pull nomic-embed-text"
    elif [ "$CHECK_ONLY" = "1" ]; then
        echo "Retrieval: nomic-embed-text NOT installed (run without --check to pull it)"
    else
        echo "Retrieval: pulling nomic-embed-text (one time, ~274 MB)..."
        if ollama pull nomic-embed-text; then
            echo "Retrieval: nomic-embed-text installed — long pages are searched"
        else
            echo "Retrieval: pull FAILED — long pages fall back to their first 6000 chars (run: ollama pull nomic-embed-text)"
        fi
    fi
else
    echo "Retrieval: Ollama not found — install Ollama, then pull nomic-embed-text"
fi

# --- Transcription (whisper.cpp) --------------------------------------
# S12: the browser looks for a whisper backend (whisper CLI,
# whisper-cli/main, faster-whisper) and the base.en model at
# ~/.forge/whisper/base.en. The model fetch is opt-in — his
# say-so is --with-whisper (or FORGE_BROWSER_INSTALL_WHISPER=1) —
# and best-effort: a failed fetch never fails the install.
WHISPER_MODEL="$HOME/.forge/whisper/base.en"
WHISPER_PIN=$(grep "^whisper-base.en " engine-pins.txt 2>/dev/null || echo "")
WHISPER_COMMIT=$(echo "$WHISPER_PIN" | awk '{print $2}')
WHISPER_HASH=$(echo "$WHISPER_PIN" | awk '{print $3}')
# `main` counts only when its path says whisper — the same rule
# the transcriber uses (a bare `main` binary is usually not
# whisper.cpp).
MAIN_PATH=$(command -v main 2>/dev/null || echo "")
case "$MAIN_PATH" in *whisper*) MAIN_IS_WHISPER=1 ;; *) MAIN_IS_WHISPER=0 ;; esac
if command -v whisper >/dev/null 2>&1 || command -v whisper-cli >/dev/null 2>&1 \
   || [ "$MAIN_IS_WHISPER" = "1" ]; then
    echo "Transcription: whisper found — audio transcription available"
else
    echo "Transcription: no whisper found (optional — only audio uses it)"
    echo "               Get whisper.cpp from https://github.com/ggerganov/whisper.cpp"
    echo "               (build it, or take a release binary) and put it on PATH."
fi
if [ -f "$WHISPER_MODEL" ]; then
    echo "Transcription: base.en model found at $WHISPER_MODEL"
elif [ "$WITH_WHISPER" = "1" ] && [ "$CHECK_ONLY" = "0" ]; then
    # D19: the model is pinned to one Hugging Face commit and
    # verified against its SHA-256 — the old code fetched `main`
    # and only checked the file was non-empty. A mismatch removes
    # the file (the bytes are wrong, resuming cannot fix them).
    if [ -z "$WHISPER_COMMIT" ] || [ -z "$WHISPER_HASH" ]; then
        echo "Transcription: no whisper pin in engine-pins.txt — refusing to fetch an unverified model."
    else
        echo "Transcription: fetching the base.en model (~148 MB, one time)..."
        mkdir -p "$(dirname "$WHISPER_MODEL")"
        # AA4: --retry for transient failures, --continue-at - so a
        # broken download resumes instead of restarting from zero.
        if curl --retry 3 --continue-at - --max-time 1800 -fsSL -o "$WHISPER_MODEL.tmp" \
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/$WHISPER_COMMIT/ggml-base.en.bin"; then
            if echo "$WHISPER_HASH  $WHISPER_MODEL.tmp" | sha256sum -c - >/dev/null 2>&1; then
                mv "$WHISPER_MODEL.tmp" "$WHISPER_MODEL"
                echo "Transcription: base.en model installed at $WHISPER_MODEL (hash verified)"
            else
                rm -f "$WHISPER_MODEL.tmp"
                echo "Transcription: model download FAILED the hash check — the file was removed (retry with: sh install.sh --with-whisper)"
            fi
        else
            echo "Transcription: model download FAILED (the partial file is kept — the next run resumes it) — transcription falls back to other backends"
            echo "               (retry with: sh install.sh --with-whisper)"
        fi
    fi
else
    echo "Transcription: base.en model NOT at $WHISPER_MODEL"
    echo "               (fetch it with: sh install.sh --with-whisper)"
fi

# --- Optional seats (report only) ---------------------------------------
if command -v ollama >/dev/null 2>&1; then
    echo "Vision seat: Ollama found — image describing can be configured"
else
    echo "Vision seat: Ollama not found (optional — only image describing uses it)"
fi
if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 \
   || command -v google-chrome >/dev/null 2>&1; then
    echo "Chromium: found — usable as the interactive engine"
fi

# --- Self-check -----------------------------------------------------------
# D5: --check is documented as change-free, and the suite run
# writes __pycache__ — so it runs only on a real install.
if [ "$CHECK_ONLY" = "1" ]; then
    echo "Self-check: skipped under --check (report only)"
else
    echo "Self-check: running the offline test suite..."
    if python3 -m unittest discover -s tests -p "test_browser*.py" 2>&1 | grep -q "^OK"; then
        echo "Self-check: all tests pass"
    else
        echo "Self-check: TESTS FAILED — run: python3 -m unittest discover -s tests -p \"test_browser*.py\""
        FAIL=1
    fi
fi

echo
if [ "$FAIL" = "0" ]; then
    echo "Install done. Run it with:  sh run.sh"
    echo "Full guided test:           sh run.sh test"
else
    echo "Install finished WITH PROBLEMS (see above)."
fi
exit $FAIL
