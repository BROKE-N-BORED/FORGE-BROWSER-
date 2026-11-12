#!/bin/sh
# Forge Browser — double-click installer (no terminal).
#
# Double-click "Install Forge Browser" in the file manager. It
# installs the program under ~/.local/share/forge-browser-app,
# adds Forge Browser to the app menu with its icon, and offers
# to open it. Everything lives under $HOME — no sudo, no admin.
#
# What it needs from the internet (one time): the browser engine
# (Lightpanda + Chromium, ~330 MB) and the Qt window kit (~600 MB).
# After the first install, re-running it just updates the program.
# Your data (vault, memory, history) lives in ~/.forge and is
# never touched by an install or an update.
#
# Dialogs use zenity when installed (Linux Mint has it by default),
# else a small Tkinter helper (install-gui.py), else desktop
# notifications while the install runs quietly.

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR="$HOME/.local/share/forge-browser-app"
APP_TMP="$APP_DIR.new"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons"
LOG="$APP_DIR/install.log"
DONE="$APP_DIR.install-done"

TITLE="Forge Browser installer"

# --- dialogs ---------------------------------------------------------
# GUI_MODE is zenity, tkinter, or notify.
if command -v zenity >/dev/null 2>&1; then
    GUI_MODE=zenity
elif python3 -c "import tkinter" 2>/dev/null; then
    GUI_MODE=tkinter
else
    GUI_MODE=notify
fi

gui_confirm() { # $1 = text — returns 0 on yes
    case "$GUI_MODE" in
        zenity) zenity --question --title="$TITLE" --text="$1" --no-wrap ;;
        tkinter) python3 "$DIR/install-gui.py" question "$TITLE" "$1" ;;
        *) return 0 ;;  # no GUI: double-clicking was the confirmation
    esac
}

gui_info() { # $1 = text
    case "$GUI_MODE" in
        zenity) zenity --info --title="$TITLE" --text="$1" --no-wrap ;;
        tkinter) python3 "$DIR/install-gui.py" info "$TITLE" "$1" ;;
        *) command -v notify-send >/dev/null 2>&1 && notify-send "$TITLE" "$1" ;;
    esac
}

gui_error() { # $1 = text
    case "$GUI_MODE" in
        zenity) zenity --error --title="$TITLE" --text="$1" --no-wrap ;;
        tkinter) python3 "$DIR/install-gui.py" error "$TITLE" "$1" ;;
        *) command -v notify-send >/dev/null 2>&1 && notify-send -u critical "$TITLE" "$1" ;;
    esac
}

gui_progress() { # shows progress until $DONE appears
    case "$GUI_MODE" in
        zenity)
            (
                while [ ! -f "$DONE" ]; do
                    LAST=$(tail -n 1 "$LOG" 2>/dev/null | tr -d '\r' | cut -c1-120)
                    echo "# ${LAST:-Working...}"
                    sleep 2
                done
            ) | zenity --progress --pulsate --auto-close --no-cancel \
                --title="$TITLE" \
                --text="Installing Forge Browser — the first install downloads the browser engine, so it takes a few minutes."
            ;;
        tkinter)
            python3 "$DIR/install-gui.py" progress "$TITLE" \
                "Installing Forge Browser — the first install downloads the browser engine, so it takes a few minutes." \
                "$LOG" "$DONE"
            ;;
        *)
            command -v notify-send >/dev/null 2>&1 && notify-send "$TITLE" "Installing Forge Browser — this takes a few minutes the first time."
            while [ ! -f "$DONE" ]; do sleep 5; done
            ;;
    esac
}

fail() { # $1 = plain-words reason
    gui_error "$1

Details are in the install log:
$LOG"
    rm -f "$DONE" "$DONE.tmp"
    exit 1
}

# --- pre-flight ------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    fail "Python 3 is not installed, so Forge Browser cannot install. (Linux Mint normally has it — this machine may need its system Python restored.)"
fi
if [ ! -f "$DIR/run.sh" ] || [ ! -f "$DIR/install.sh" ]; then
    fail "The install files are missing from this folder — the download may be incomplete. Download the release zip again and try once more."
fi

if ! gui_confirm "Install Forge Browser?

It installs under your home folder, adds Forge Browser to the app menu with its icon, and never needs the terminal. The first install downloads the browser engine (about 1 GB, one time), so it takes a few minutes."; then
    exit 0
fi

# --- copy the program in ---------------------------------------------
rm -rf "$APP_TMP"
mkdir -p "$APP_TMP" || fail "Could not create the install folder."
cp -R "$DIR"/. "$APP_TMP"/ || fail "Could not copy the program files."
rm -rf "$APP_DIR"
mv "$APP_TMP" "$APP_DIR" || fail "Could not move the program into place."
rm -f "$DONE" "$DONE.tmp"

# --- run the engine + window install, with progress ------------------
# install.sh runs inside a background subshell that records its own
# exit status in $DONE; the progress dialog watches for that file.
# The whole install is capped at one hour — a stall past that means
# something is broken, and the log says what.
: > "$LOG"
if command -v timeout >/dev/null 2>&1; then
    ( timeout 3600 sh "$APP_DIR/install.sh" >"$LOG" 2>&1; echo $? > "$DONE.tmp"; mv "$DONE.tmp" "$DONE" ) &
else
    ( sh "$APP_DIR/install.sh" >"$LOG" 2>&1; echo $? > "$DONE.tmp"; mv "$DONE.tmp" "$DONE" ) &
fi
gui_progress
STATUS=$(cat "$DONE" 2>/dev/null || echo 1)
rm -f "$DONE" "$DONE.tmp"
if [ "$STATUS" != "0" ]; then
    fail "The install did not finish cleanly. Check the log, then run the installer again — it picks up where it left off."
fi

# --- the window part must actually work --------------------------------
if ! "$APP_DIR/.venv/bin/python" -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
    fail "Forge Browser installed, but the window part did not — it needs the internet for a one-time download. Check the connection and run the installer again."
fi

# --- menu entry + icon --------------------------------------------------
mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
if [ -f "$DIR/forge-browser.svg" ]; then
    cp "$DIR/forge-browser.svg" "$ICON_DIR/forge-browser.svg"
fi
cat > "$DESKTOP_DIR/forge-browser.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Forge Browser
Comment=Your browser. No accounts, no API keys, nothing tracked.
Exec=sh $APP_DIR/run.sh
Path=$APP_DIR
Icon=$ICON_DIR/forge-browser.svg
Terminal=false
Categories=Network;WebBrowser;
EOF
chmod +x "$DESKTOP_DIR/forge-browser.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

# --- done ----------------------------------------------------------------
if gui_confirm "Forge Browser is installed — find it in your app menu.

Open it now?"; then
    ( nohup sh "$APP_DIR/run.sh" >/dev/null 2>&1 & )
fi
exit 0
