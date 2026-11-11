#!/bin/sh
# Forge Browser — run.
#
#   sh run.sh                 open Forge Browser, the program (its own window)
#   sh run.sh demo            terminal demo: engines + live search + live fetch
#   sh run.sh test            the full guided test (test-forge-browser.sh)
#   sh run.sh search "..."    any browser command, passed straight through
#   sh run.sh fetch URL       (same for snapshot / research / engines / vault ...)
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x ".venv/bin/python" ] && PY=.venv/bin/python
if [ $# -gt 0 ]; then
    if [ "$1" = "test" ]; then
        exec sh test-forge-browser.sh
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
    exec $PY -m forge.browser "$@"
fi
exec $PY -m forge.browser app
