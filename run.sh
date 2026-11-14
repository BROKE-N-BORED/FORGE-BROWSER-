#!/bin/sh
# Forge Browser — run.
#
#   sh run.sh                 open Forge Browser, the program (its own window)
#   sh run.sh check           report what this machine has + the threshold
#                             validators' verdicts (needs Ollama for those)
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
        if $PY -c "import os,urllib.request; urllib.request.urlopen(os.environ.get('FORGE_OLLAMA_URL','http://127.0.0.1:11434')+'/api/tags',timeout=5)" 2>/dev/null; then
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
    exec $PY -m forge.browser "$@"
fi
exec $PY -m forge.browser app
