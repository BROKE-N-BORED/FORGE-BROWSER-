#!/bin/sh
# Forge Browser — run.
#
#   sh run.sh                 open Forge Browser, the program (its own window)
#   sh run.sh URL             open the program on that page (http/https/file)
#   sh run.sh check           report what this machine has + the threshold
#                             validators' verdicts (needs Ollama for those)
#   sh run.sh demo            terminal demo: engines + live search + live fetch
#   sh run.sh test            the full guided test (test-forge-browser.sh)
#   sh run.sh search "..."    any browser command, passed straight through
#   sh run.sh fetch URL       (same for snapshot / research / engines / vault ...)
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x ".venv/bin/python" ] && PY=.venv/bin/python
# D8: the window launch is wrapped — a launch that dies must not
# fail silently. On failure the last lines of the program's output
# go to a dialog (zenity, else the installer's tkinter dialog) and
# to stderr, and run.sh exits non-zero. On a normal quit (exit 0)
# nothing is shown.
launch_app() {
    if [ -n "$1" ]; then
        _out="$("$PY" -m forge.browser app "$1" 2>&1)"
    else
        _out="$("$PY" -m forge.browser app 2>&1)"
    fi
    _status=$?
    if [ "$_status" != "0" ]; then
        _tail="$(printf '%s\n' "$_out" | tail -n 5)"
        _msg="Forge Browser did not start (it exited with code $_status)."
        if [ -n "$_tail" ]; then
            _msg="$_msg

The last lines were:
$_tail"
        fi
        _msg="$_msg

If this keeps happening, run the installer again (sh install.sh) — it rebuilds the app window."
        if command -v zenity >/dev/null 2>&1; then
            zenity --error --title="Forge Browser" --text="$_msg" --no-wrap
        elif [ -f "install-gui.py" ]; then
            python3 install-gui.py error "Forge Browser" "$_msg"
        fi
        echo "Forge Browser did not start (exit $_status)." >&2
        [ -n "$_tail" ] && printf '%s\n' "$_tail" >&2
    fi
    return "$_status"
}
if [ $# -gt 0 ]; then
    if [ "$1" = "test" ]; then
        exec sh test-forge-browser.sh
    fi
    if [ "$1" = "check" ]; then
        # I2: the engines report, then the threshold validators'
        # verdicts beside it. The validators need a real Ollama
        # to run — probe once first: when Ollama is not
        # answering, both are reported as not run instead of
        # failing the check. A validator that RAN and found its
        # threshold does not separate fails the check (exit 1).
        echo "=== Forge Browser — what this machine has ==="
        $PY -m forge.browser engines
        echo
        echo "=== Threshold validators ==="
        echo "(0.75 tab-grouping, 0.5 memory floor — checked against real"
        echo " nomic-embed-text output; needs Ollama answering)"
        # D28: the probe honors the Ollama URL from the settings
        # file too, not only the env var and the default — the
        # same resolution the app itself uses (media.ollama_url).
        if $PY -c "import urllib.request; from forge.browser.media import ollama_url; urllib.request.urlopen(ollama_url() + '/api/tags', timeout=5)" 2>/dev/null; then
            FAILED=0
            for v in validate_threshold validate_memory_threshold; do
                echo
                echo "--- $v ---"
                if $PY "tests/$v.py"; then
                    echo "$v: PASS"
                else
                    echo "$v: FAIL"
                    FAILED=1
                fi
            done
            exit $FAILED
        fi
        echo "not run: no Ollama answering (the validators need Ollama +"
        echo "nomic-embed-text — install.sh pulls the model)"
        exit 0
    fi
    if [ "$1" = "demo" ]; then
        echo "=== Forge Browser — what this machine has ==="
        $PY -m forge.browser engines
        echo
        echo "=== Live search: ollama local llm ==="
        $PY -m forge.browser search "ollama local llm" --max 3
        echo
        echo "=== Live fetch: https://example.org ==="
        $PY -m forge.browser fetch https://example.org --max-chars 400
        echo
        echo "Open the browser program itself with:  sh run.sh"
        exit 0
    fi
    # D13: a bare URL (from the menu entry, %u) opens the program
    # on that page instead of dying with "unknown command".
    case "$1" in
        http://*|https://*|file://*)
            launch_app "$1"
            exit $?
            ;;
    esac
    exec $PY -m forge.browser "$@"
fi
launch_app
exit $?
