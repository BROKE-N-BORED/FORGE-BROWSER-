#!/bin/sh
# Forge Browser — free CI (J1). GitHub Actions is billing-locked
# account-wide (the jobs are killed at startup), so this script is
# the push-time check: it runs locally, no runners, no account,
# nothing to lock — and it checks MORE than Actions did (Actions
# ran only the suite).
#
#   sh ci/check.sh        from the repo root (or anywhere — the
#                         script finds the repo from its own path)
#
# What it checks: the full suite under timeout (which includes
# DocsTests, so the docs' test counts are verified too), and a
# Release-shaped zip build from this tree with stored-modes + SHA
# verification. It writes the verdict to CI-STATUS.md at the repo
# root, then exits 0 only when everything passed — non-zero on any
# failure. The push discipline is: run this, then push, with
# CI-STATUS.md riding in the push.
#
# POSIX sh. Temp work lives on the home filesystem, never /tmp
# (the sandbox /tmp is a small tmpfs and a suite run can fill it).

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

TMP_BASE="${FORGE_BROWSER_CI_TMP:-$HOME/workspace/tmp}"
mkdir -p "$TMP_BASE" || exit 1
WORK="$(mktemp -d "$TMP_BASE/ci-check.XXXXXX")" || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

FAILED=0

echo "=== Suite ==="
SUITE_LOG="$WORK/suite.log"
if TMPDIR="$WORK" timeout 280 python3 -m unittest discover -s tests \
        >"$SUITE_LOG" 2>&1; then
    SUITE_RESULT="PASS"
else
    SUITE_RESULT="FAIL"
    FAILED=1
fi
SUITE_COUNT="$(grep -o "Ran [0-9]* tests" "$SUITE_LOG" | tail -1 \
               | grep -o "[0-9]*" || true)"
[ -n "$SUITE_COUNT" ] || SUITE_COUNT="?"
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

COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null \
          || echo "unknown (not a git checkout)")"
DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

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
