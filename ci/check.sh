#!/bin/sh
# Forge Browser — free CI (J1). GitHub Actions is billing-locked
# account-wide (the jobs are killed at startup), so this script is
# the push-time check: it runs locally, no runners, no account,
# nothing to lock — and it checks MORE than Actions did (Actions
# ran only the suite).
#
#   sh ci/check.sh [--commit SHA]   from the repo root (or
#                         anywhere — the script finds the repo
#                         from its own path)
#
# What it checks: the full suite under timeout (which includes
# DocsTests, so the docs' test counts are verified too), and a
# Release-shaped zip build from this tree with stored-modes + SHA
# verification. It writes the verdict to CI-STATUS.md at the repo
# root, then exits 0 only when everything passed — non-zero on any
# failure. The push discipline is: run this, then push, with
# CI-STATUS.md riding in the push.
#
# The commit in the verdict comes from --commit, else
# FORGE_BROWSER_CI_COMMIT, else git (D2 — the tree has no .git in
# the snapshot workflow, so the push step stamps the real SHA).
#
# Temp work lives under ${TMPDIR:-/tmp} (override with
# FORGE_BROWSER_CI_TMP, D25). In this sandbox, run with
# TMPDIR=~/workspace/tmp — /tmp is a small tmpfs and a suite run
# can fill it.
#
# POSIX sh.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# D2: the commit, from the argument, the environment, or git.
COMMIT_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) COMMIT_ARG="${2:-}"; shift 2 ;;
        --commit=*) COMMIT_ARG="${1#--commit=}"; shift ;;
        *) echo "ci/check.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [ -n "$COMMIT_ARG" ]; then
    COMMIT="$COMMIT_ARG"
elif [ -n "${FORGE_BROWSER_CI_COMMIT:-}" ]; then
    COMMIT="$FORGE_BROWSER_CI_COMMIT"
else
    COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null \
              || echo "unknown (not a git checkout)")"
fi
DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# D25: the override is created when missing; the TMPDIR default
# must already exist — failing early and clearly beats dying
# halfway through the suite with a cryptic mktemp error.
if [ -n "${FORGE_BROWSER_CI_TMP:-}" ]; then
    TMP_BASE="$FORGE_BROWSER_CI_TMP"
    mkdir -p "$TMP_BASE" || exit 1
else
    TMP_BASE="${TMPDIR:-/tmp}"
    if [ ! -d "$TMP_BASE" ]; then
        echo "ci/check.sh: the temp directory $TMP_BASE does not exist (TMPDIR) — point TMPDIR at a directory that exists, or set FORGE_BROWSER_CI_TMP." >&2
        exit 1
    fi
fi
WORK="$(mktemp -d "$TMP_BASE/ci-check.XXXXXX")" || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

# D1: the status-count test reads CI-STATUS.md with no existence
# guard, so the first run on a tree without the file fails its own
# suite. Write a provisional verdict before the suite runs — the
# count comes from README.md, which DocsTests pins to the true
# suite count — and overwrite it with the real verdict below.
README_COUNT="$(grep -o "[0-9][0-9]* checks" README.md 2>/dev/null \
                | head -1 | grep -o "[0-9][0-9]*" || true)"
[ -n "$README_COUNT" ] || README_COUNT="?"
cat > CI-STATUS.md <<EOF
# Forge Browser — CI status

Free CI (\`ci/check.sh\`) — GitHub Actions is billing-locked
account-wide, so this file is the verdict record. A check is
running now; this is the provisional verdict, replaced when it
finishes.

- Commit: $COMMIT
- Date (UTC): $DATE
- Suite: $README_COUNT checks — CHECKING
- Release zip: pending (modes checked after the suite)

Run \`sh ci/check.sh\` from the repo root to re-check.
EOF

FAILED=0

echo "=== Suite ==="
SUITE_LOG="$WORK/suite.log"
if TMPDIR="$WORK" timeout 900 python3 -m unittest discover -s tests \
        >"$SUITE_LOG" 2>&1; then
    SUITE_RESULT="PASS"
else
    SUITE_RESULT="FAIL"
    FAILED=1
fi
SUITE_COUNT="$(grep -o "Ran [0-9]* tests" "$SUITE_LOG" | tail -1 \
               | grep -o "[0-9]*" || true)"
# A timed-out suite leaves no "Ran N tests" line; the count is a
# property of the tree, and README.md already states it.
[ -n "$SUITE_COUNT" ] || SUITE_COUNT="$README_COUNT"
tail -5 "$SUITE_LOG"
if [ "$SUITE_RESULT" = "FAIL" ]; then
    # Name the failing tests — a failing CI that does not say
    # what failed is half-useless, and the work dir is removed
    # on exit, so the names have to reach the terminal now.
    grep -E "^(FAIL|ERROR):" "$SUITE_LOG" || true
fi
echo "suite: $SUITE_COUNT checks — $SUITE_RESULT"

echo
echo "=== Release zip (vendored build from this tree) ==="
ZIP_LOG="$WORK/zip.log"
if python3 ci/build-zip.py --out "$WORK/check.zip" \
        >"$ZIP_LOG" 2>&1; then
    ZIP_RESULT="$(cat "$ZIP_LOG")"
else
    ZIP_RESULT="FAIL: $(head -1 "$ZIP_LOG")"
    FAILED=1
fi
echo "$ZIP_RESULT"

cat > CI-STATUS.md <<EOF
# Forge Browser — CI status

Free CI (\`ci/check.sh\`) — GitHub Actions is billing-locked
account-wide, so this file is the verdict record. Last checked:

- Commit: $COMMIT
- Date (UTC): $DATE
- Suite: $SUITE_COUNT checks — $SUITE_RESULT
- Release zip: $ZIP_RESULT

Run \`sh ci/check.sh\` from the repo root to re-check.
EOF

echo
if [ "$FAILED" -eq 0 ]; then
    echo "CI PASS — verdict written to CI-STATUS.md"
    exit 0
fi
echo "CI FAIL — verdict written to CI-STATUS.md"
exit 1
