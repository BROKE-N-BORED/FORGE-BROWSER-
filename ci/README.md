# Free CI (J1)

GitHub Actions is billing-locked account-wide — the jobs are
killed at startup, so the repo has no push-time CI at all.
`ci/check.sh` is the free replacement: it runs locally, no GitHub
runners, no account, nothing to lock — and it checks MORE than
Actions did (Actions ran only the suite).

## How to run it

```sh
sh ci/check.sh        # from the repo root
```

Then push. `CI-STATUS.md` rides in the push, so the repo page
itself shows the last verdict. The script exits 0 only when
everything passed, non-zero on any failure.

## What it checks

1. **The full suite** under `timeout 280`, with `TMPDIR` on the
   home filesystem (never `/tmp` — the sandbox `/tmp` is a small
   tmpfs and a suite run can fill it). The suite includes
   DocsTests, so the docs' test counts are verified too.
2. **A Release-shaped zip build** from this tree
   (`ci/build-zip.py`): the zip is built with the Release
   builder's mode rules, then reopened and every entry's stored
   mode is verified, and its SHA-256 is recorded. The zip is a
   throwaway — the check is the point, not the artifact.

The verdict goes to `CI-STATUS.md` at the repo root: last checked
commit, date, suite count + result, zip result.

## Why the zip step is vendored

The Release zip builder
(`~/workspace/forge-browser-ai-release/build-release-zip.py`)
lives OUTSIDE the repo and hardcodes the main tree path — from a
fresh clone it is absent, and from any other copy it would build
the main tree instead of the tree being checked. So `ci/` carries
its own small builder (`ci/build-zip.py`) that mirrors the Release
builder's rules (same mode table, same `EXECUTABLES.txt`
manifest, same verification) but builds from the tree it lives
in. The shipped Release zip is still made by the out-of-repo
builder at Release time.

## What it does not do

It does not push and does not make a Release — those stay manual,
after this script passes. `.github/workflows/test.yml` stays in
place: harmless while the lock holds, and Actions runs again too
if the lock clears.
