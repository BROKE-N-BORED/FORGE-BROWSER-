# Audit #21 — Engine pins

Audited 2026-11-12 from the code (engine-pins.txt, install.sh
engine + window sections), the tests (pins format tests,
no-nightly test), and live checks of the pins file's hashes and
the script's POSIX cleanliness.

## What it is

Task #21 from the improvements list: engine pins — engine-pins.txt
+ install.sh verification. Built 2026-11-11.

## What it does

- engine-pins.txt pins the exact versions install.sh fetches:
  Lightpanda 1.0.3 (x86_64 + aarch64) with SHA-256s, Chromium
  snapshot revision 1741687 (no hash — Google publishes none).
- install.sh verifies before installing: Lightpanda is hash-checked
  (SHA-256) before the binary is moved into place; a mismatch
  refuses the install and keeps the existing install. Chromium is
  verified against the MD5 in the object's own GCS metadata + a zip
  CRC check.
- The pins file is the single source of truth; the scripts read
  versions from it, never hard-code them; a test
  (test_install_scripts_fetch_no_nightly) forbids unpinned URLs
  (nightly tag, LAST_CHANGE).
- lp_installed_ok re-verifies the installed Lightpanda (executable
  + version file + hash) on every run.

## Strengths

- Lightpanda's verification is the real thing: pinned version +
  SHA-256, verified before install, re-verified on every run,
  failure keeps the old install.
- The no-guessing rule: a missing pin refuses to fetch ("refusing
  to guess a version") instead of falling back to latest.
- The pins file documents its own update procedure (download, check
  size, sha256sum, record together).
- The nightly/LAST_CHANGE test guards against regression to moving
  targets.
- install.sh is POSIX-clean (verified: no bashisms) — "sh
  install.sh" is correct for this script.
- Exit code reflects problems (exit $FAIL); the self-check runs the
  suite and fails the install on test failure.

## Weaknesses (all verified by probe or code read)

1. The Chromium expected hash comes from the same source as the
   download. EXPECTED_MD5 is fetched from the GCS metadata API for
   the same object being downloaded. A tamper that can alter the
   download can alter the metadata response too (both over HTTPS —
   the practical risk needs a cert compromise — but the check is
   integrity, not authenticity). The pins file carries NO Chromium
   hash at all (PROBED: the Chromium line is revision-only). The
   fix: record the zip's SHA-256 in engine-pins.txt (computed once
   from a verified download) and verify against the repo's hash,
   like Lightpanda.
2. The installed Chromium is never re-verified. The "already
   installed" check trusts the REVISION file alone (`[ -x
   "$CHROME_BIN" ] && [ "$(cat "$REV_FILE")" = "$CHROME_REV" ]`) —
   no hash of the installed binary. A corrupted or tampered chrome
   binary passes if the REVISION file says the right number.
   Lightpanda re-hashes on every run; Chromium does not.
3. Chromium extraction is not atomic. The zip extracts OVER the
   existing tree; an interrupted extraction leaves a half-old/
   half-new tree. (Self-healing — the REVISION file is written only
   after extraction, so the next run re-downloads — but the tree is
   mixed until then.)
4. No retry/resume on the downloads. curl -fsSL with no --retry; a
   transient failure on the 250 MB Chromium download restarts from
   zero. (Pairs with W3.)
5. PySide6 is unpinned — `pip install "PySide6>=6.6"
   "PySide6-WebEngine"` — the biggest dependency (~600 MB) floats.
   The whole-project audit owns this (B-section). Cross-ref.
6. chmod +x on the whole chrome-linux tree — makes every file
   executable, including data files. Over-broad (harmless but
   sloppy). Minor.
7. The Lightpanda version file is written after the mv — an
   interruption between them leaves a new binary with an old/missing
   version file → re-download next run. Self-healing, just wasteful.
   Minor.
8. The MD5 check uses MD5 — collision-prone; fine for corruption
   detection, not for tamper resistance. Folds into weakness 1 (the
   fix is a pinned SHA-256).

## UI flaws

- None in the window (install-time). The install output is plain
  and clear.

## Gaps / missing

- No Chromium hash in the pins file.
- No re-verification of installed Chromium.
- No retry/resume.
- PySide6 unpinned (B-section).

## Improvements (concrete, in priority order)

1. Record the Chromium zip's SHA-256 in engine-pins.txt (computed
   once from a verified download) and verify the download against
   the pinned hash instead of the GCS metadata MD5.
2. Re-verify the installed Chromium on --check and on install (hash
   the chrome binary, or the extracted tree's key files) — match
   Lightpanda's lp_installed_ok.
3. Extract Chromium to a temp dir and swap it into place (atomic)
   instead of extracting over the live tree.
4. Add --retry (and --continue-at - for resume) to the curl
   downloads.
5. Tighten the chmod to the chrome binary + known executables
   instead of the whole tree.
6. Cross-ref: PySide6 pinning (B-section), download resume (W3).

## Verdict

#21 does the important half well — Lightpanda is pinned +
SHA-256-verified before install and re-verified on every run, and
nothing fetches a moving target. But Chromium, the bigger download,
is verified against a hash fetched from the same source as the bytes
(integrity, not authenticity), carries no hash in the pins file at
all, and is never re-verified once installed.
