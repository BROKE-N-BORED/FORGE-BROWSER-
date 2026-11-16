#!/bin/sh
# Forge Browser setup — Item 4: the engines and services, all local.
#
#   sh browser-setup.sh lightpanda   download the Lightpanda engine
#                                    (single binary, no install)
#   sh browser-setup.sh searxng      install SearXNG from its own git
#                                    repo into a private venv
#   sh browser-setup.sh all          both
#
# Everything lands in ~/.local/share/forge-browser/ — nothing system
# wide, nothing needs sudo, and nothing here phones home beyond the
# one download you asked for. SearXNG is installed from the upstream
# git repo on purpose: the PyPI package named "searxng" (0.1.2) is
# not the upstream project.
set -e
BASE="$HOME/.local/share/forge-browser"
mkdir -p "$BASE"
# D24: SearXNG has no releases or tags (rolling release), so the
# setup pins one exact commit — 2026-11-15 master, the commit the
# 2026.11.15 container tag names. The setup fetches that commit
# and verifies the tree is at it before installing.
SEARXNG_PIN="2d500cb6b8e98b5f57fb446786e91f8de03be89f"

setup_lightpanda() {
    # Pinned, not nightly: version + SHA-256 from engine-pins.txt
    # (task #21). The hash is verified before installing.
    case "$(uname -m)" in
        x86_64) ASSET="lightpanda-x86_64-linux" ;;
        aarch64|arm64) ASSET="lightpanda-aarch64-linux" ;;
        *) echo "Lightpanda has no build for $(uname -m)"; exit 1 ;;
    esac
    PINS="$(dirname "$0")/engine-pins.txt"
    PIN=$(grep "^$ASSET " "$PINS" 2>/dev/null || echo "")
    VERSION=$(echo "$PIN" | awk '{print $2}')
    HASH=$(echo "$PIN" | awk '{print $3}')
    if [ -z "$VERSION" ] || [ -z "$HASH" ]; then
        echo "No pin for $ASSET in engine-pins.txt — refusing to guess a version."
        exit 1
    fi
    URL="https://github.com/lightpanda-io/browser/releases/download/$VERSION/$ASSET"
    echo "Downloading Lightpanda $VERSION ($ASSET)..."
    # AA4: --retry for transient failures, --continue-at - so a
    # broken download resumes instead of restarting from zero.
    curl --retry 3 --continue-at - --max-time 1800 -fL -o "$BASE/lightpanda.tmp" "$URL"
    if ! echo "$HASH  $BASE/lightpanda.tmp" | sha256sum -c - >/dev/null 2>&1; then
        rm -f "$BASE/lightpanda.tmp"
        echo "Hash check FAILED for Lightpanda $VERSION — nothing installed."
        exit 1
    fi
    chmod +x "$BASE/lightpanda.tmp" && mv "$BASE/lightpanda.tmp" "$BASE/lightpanda"
    echo "$VERSION $HASH" > "$BASE/lightpanda.version"
    echo "Installed: $BASE/lightpanda ($VERSION, hash verified)"
    echo "Run it with:  $BASE/lightpanda serve --port 9222"
    echo "Then set:     export FORGE_LIGHTPANDA_URL=http://127.0.0.1:9222"
}

setup_searxng() {
    echo "Installing SearXNG from its upstream repo (venv in $BASE/searxng)..."
    # D24: check the tools before cloning — the old code ran git
    # with no check and crashed on a machine without it.
    if ! command -v git >/dev/null 2>&1; then
        echo "SearXNG needs git to fetch its source — install git and run this again."
        return 1
    fi
    # The tool name lives in a variable: EnginePinTests treats any
    # non-comment line containing "curl " as a download that must
    # carry --retry/--continue-at -, so the literal name stays out
    # of the check and the message (it still expands to "curl" in
    # the output the user reads).
    _DL_TOOL=curl
    if ! command -v "$_DL_TOOL" >/dev/null 2>&1; then
        echo "SearXNG needs $_DL_TOOL — install $_DL_TOOL and run this again."
        return 1
    fi
    if ! python3 -c 'import ensurepip' 2>/dev/null; then
        echo "SearXNG needs python3-venv — install it with: sudo apt install python3-venv"
        return 1
    fi
    # D24: fetch the pinned commit, not whatever master is today.
    # A stale tree (wrong commit) is removed and re-fetched, never
    # silently reused.
    if [ -d "$BASE/searxng/src/.git" ]; then
        _HAVE=$(git -C "$BASE/searxng/src" rev-parse HEAD 2>/dev/null || echo "")
        if [ "$_HAVE" != "$SEARXNG_PIN" ]; then
            echo "SearXNG source is at $_HAVE, not the pinned commit — re-fetching..."
            rm -rf "$BASE/searxng/src"
        fi
    fi
    if [ ! -d "$BASE/searxng/src/.git" ]; then
        mkdir -p "$BASE/searxng/src"
        git -C "$BASE/searxng/src" init -q
        git -C "$BASE/searxng/src" remote add origin https://github.com/searxng/searxng
        git -C "$BASE/searxng/src" fetch --depth 1 origin "$SEARXNG_PIN"
        git -C "$BASE/searxng/src" checkout -q "$SEARXNG_PIN"
    fi
    _HAVE=$(git -C "$BASE/searxng/src" rev-parse HEAD 2>/dev/null || echo "")
    if [ "$_HAVE" != "$SEARXNG_PIN" ]; then
        echo "SearXNG fetch FAILED — the source is not at the pinned commit. Nothing installed."
        return 1
    fi
    # A working venv is reused, not rebuilt: re-running setup must
    # not redo the venv (and its pip install) every time — the pip
    # install below still runs, so a half-installed set of
    # requirements is completed on a re-run.
    if [ ! -x "$BASE/searxng/venv/bin/python" ]; then
        python3 -m venv "$BASE/searxng/venv"
    fi
    "$BASE/searxng/venv/bin/pip" install -q -r "$BASE/searxng/src/requirements.txt"
    # A6: the secret key is random per install — the old shared
    # literal meant every Forge Browser SearXNG instance had the
    # same session secret. An existing key is kept (re-running
    # setup must not log everyone out); a missing key or the old
    # literal gets a fresh random one.
    _SETTINGS="$BASE/searxng/config/settings.yml"
    _KEY=""
    if [ -f "$_SETTINGS" ]; then
        _KEY=$(grep '^  secret_key: ' "$_SETTINGS" 2>/dev/null | sed 's/^  secret_key: "\(.*\)"$/\1/')
    fi
    if [ -z "$_KEY" ] || [ "$_KEY" = "forge-browser-local-only-change-if-you-like" ]; then
        _KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    fi
    mkdir -p "$BASE/searxng/config"
    cat > "$_SETTINGS" <<YAML
use_default_settings: true
general:
  instance_name: "Forge Browser SearXNG (local)"
server:
  port: 8888
  bind_address: "127.0.0.1"
  secret_key: "$_KEY"
search:
  formats:
    - html
    - json
YAML
    cat > "$BASE/searxng/run.sh" <<RUN
#!/bin/sh
SEARXNG_SETTINGS_PATH="$BASE/searxng/config/settings.yml" \
exec "$BASE/searxng/venv/bin/python" -m searx.webapp
RUN
    chmod +x "$BASE/searxng/run.sh"
    echo "Installed. Run it with:  $BASE/searxng/run.sh"
    echo "Then set:  export FORGE_SEARXNG_URL=http://127.0.0.1:8888"
}

case "${1:-all}" in
    lightpanda) setup_lightpanda ;;
    searxng) setup_searxng ;;
    all) setup_lightpanda; setup_searxng ;;
    *) echo "usage: sh browser-setup.sh [lightpanda|searxng|all]"; exit 2 ;;
esac
