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
    curl -fL -o "$BASE/lightpanda.tmp" "$URL"
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
    if [ ! -d "$BASE/searxng/src" ]; then
        git clone --depth 1 https://github.com/searxng/searxng "$BASE/searxng/src"
    fi
    python3 -m venv "$BASE/searxng/venv"
    "$BASE/searxng/venv/bin/pip" install -q -r "$BASE/searxng/src/requirements.txt"
    mkdir -p "$BASE/searxng/config"
    cat > "$BASE/searxng/config/settings.yml" <<'YAML'
use_default_settings: true
general:
  instance_name: "Forge Browser SearXNG (local)"
server:
  port: 8888
  bind_address: "127.0.0.1"
  secret_key: "forge-browser-local-only-change-if-you-like"
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
