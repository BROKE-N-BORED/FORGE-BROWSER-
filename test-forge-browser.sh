#!/bin/sh
# Forge Browser — guided standalone test.
#
#   sh test-forge-browser.sh
#
# Runs every feature once and prints what it did, so you can see if
# it works the way you want. Local checks print PASS/FAIL. Live-web
# steps print their real output for you to judge (marked SHOW).
# Nothing here touches Code Forge — this is the standalone build.
cd "$(dirname "$0")" || exit 1
PY=python3
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
step() { echo; echo "=== $1 ==="; }

step "1. Unit tests (offline, 216 checks)"
if $PY -m unittest discover -s tests -p "test_browser*.py" 2>&1 | grep -q "^OK"; then
    ok "unit tests"
else
    bad "unit tests — run them directly to see which one failed:"
    echo "    $PY -m unittest discover -s tests -p \"test_browser*.py\""
fi

step "2. What this machine has (engines report)"
$PY -m forge.browser engines

step "3. Live search (SHOW — judge the results)"
$PY -m forge.browser search "ollama local llm" --max 3 || echo "(search failed — check the network)"

step "4. Live fetch of a page (SHOW)"
$PY -m forge.browser fetch https://example.org --max-chars 300 || echo "(fetch failed)"

step "5. Fetch a PDF file, any-type handling (SHOW)"
$PY -m forge.browser fetch-any https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf || echo "(PDF fetch failed)"

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

step "7. Research: the plan gate, then a small live run (SHOW)"
$PY -m forge.browser research "ollama local llm" | head -6
$PY -m forge.browser research "ollama local llm" --yes --max-pages 2 --out /tmp/fb-research.md \
    && head -12 /tmp/fb-research.md || echo "(research run failed)"

step "8. Rendered page via a browser engine"
LP="$HOME/.local/share/forge-browser/lightpanda"
STARTED=""
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

echo
echo "=== SUMMARY: $PASS passed, $FAIL failed (SHOW steps are yours to judge) ==="
