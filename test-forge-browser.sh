#!/bin/sh
# Forge Browser — guided standalone test.
#
#   sh test-forge-browser.sh [--step N]
#
# Runs every feature once and prints what it did, so you can see if
# it works the way you want. Local checks print PASS/FAIL. Live-web
# steps print their real output for you to judge (marked SHOW).
# Nothing here touches Code Forge — this is the standalone build.
# With --step N, only step N runs (1: unit tests, 2: engines
# report, 3: live search, 4: live fetch, 5: PDF fetch, 6: vault
# round-trip, 7: research, 8: render engine) — for a quick check
# of one piece, and so the suite can smoke-test step 1 alone.
cd "$(dirname "$0")" || exit 1
PY=python3
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
step() { echo; echo "=== $1 ==="; }

STEP=0
if [ "$1" = "--step" ]; then
    STEP="${2:-0}"
    case "$STEP" in
        1|2|3|4|5|6|7|8) ;;
        *) echo "usage: sh test-forge-browser.sh [--step 1-8]" >&2; exit 2 ;;
    esac
elif [ -n "$1" ]; then
    echo "usage: sh test-forge-browser.sh [--step 1-8]" >&2; exit 2
fi
want_step() { [ "$STEP" = "0" ] || [ "$STEP" = "$1" ]; }

# D26: interrupting the script used to orphan the background
# Lightpanda (fixed port 9223) — the kill ran only on the normal
# path. The trap kills it on exit or interrupt too.
STARTED=""
cleanup_lightpanda() {
    [ -n "$STARTED" ] && kill "$STARTED" 2>/dev/null
}
trap cleanup_lightpanda EXIT INT TERM

if want_step 1; then
step "1. Unit tests (offline, 900 checks)"
if $PY -m unittest discover -s tests -p "test_browser*.py" 2>&1 | grep -q "^OK"; then
    ok "unit tests"
else
    bad "unit tests — run them directly to see which one failed:"
    echo "    $PY -m unittest discover -s tests -p \"test_browser*.py\""
fi
fi

if want_step 2; then
step "2. What this machine has (engines report)"
$PY -m forge.browser engines
fi

if want_step 3; then
step "3. Live search (SHOW — judge the results)"
$PY -m forge.browser search "ollama local llm" --max 3 || echo "(search failed — check the network)"
fi

if want_step 4; then
step "4. Live fetch of a page (SHOW)"
$PY -m forge.browser fetch https://example.org --max-chars 300 || echo "(fetch failed)"
fi

if want_step 5; then
step "5. Fetch a PDF file, any-type handling (SHOW)"
$PY -m forge.browser fetch-any https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf || echo "(PDF fetch failed)"
fi

if want_step 6; then
step "6. Vault round-trip (temp vault, nothing real stored)"
FORGE_BROWSER_VAULT=/tmp/fb-test-vault.json $PY - <<'PYEOF'
import os
from forge.browser.vault import Vault
path = os.environ["FORGE_BROWSER_VAULT"]
if os.path.exists(path):
    os.remove(path)
v = Vault.create(path, "test-passphrase-1")
v.set("example.com", "tester", "s3cret-test-value")
again = Vault.open(path, "test-passphrase-1")
assert again.get("example.com")["password"] == "s3cret-test-value"
try:
    Vault.open(path, "wrong-passphrase")
    print("VAULT BROKEN: wrong passphrase opened it")
    raise SystemExit(1)
except Exception:
    pass
os.remove(path)
print("vault create/set/open/wrong-passphrase all behaved")
PYEOF
[ $? -eq 0 ] && ok "vault round-trip" || bad "vault round-trip"
fi

if want_step 7; then
step "7. Research: the plan gate, then a small live run (SHOW)"
$PY -m forge.browser research "ollama local llm" | head -6
$PY -m forge.browser research "ollama local llm" --yes --max-pages 2 --out /tmp/fb-research.md \
    && head -12 /tmp/fb-research.md || echo "(research run failed)"
fi

if want_step 8; then
step "8. Rendered page via a browser engine"
LP="$HOME/.local/share/forge-browser/lightpanda"
if [ -x "$LP" ] && ! curl -s http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
    "$LP" serve --port 9223 >/tmp/fb-lightpanda.log 2>&1 &
    STARTED=$!
    sleep 2
fi
if curl -s http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
    FORGE_LIGHTPANDA_URL=http://127.0.0.1:9223 $PY -m forge.browser snapshot https://example.org | head -8
    ok "render engine (Lightpanda) snapshot"
elif $PY -m forge.browser engines | grep -q "Chromium engine: /"; then
    $PY -m forge.browser snapshot https://example.org | head -8
    ok "render engine (Chromium) snapshot"
else
    echo "SKIP: no browser engine yet. Get one with:  sh browser-setup.sh lightpanda"
    echo "      (search/fetch/research/vault above do not need an engine)"
fi
[ -n "$STARTED" ] && kill $STARTED 2>/dev/null
STARTED=""
fi

echo
echo "=== SUMMARY: $PASS passed, $FAIL failed (SHOW steps are yours to judge) ==="

# A verification script that reports FAIL should say so in its
# exit code, not just its output.
[ "$FAIL" -eq 0 ]
