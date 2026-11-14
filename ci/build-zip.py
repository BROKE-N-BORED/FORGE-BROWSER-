#!/usr/bin/env python3
"""Build a throwaway Release-shaped zip from THIS tree and verify it (J1).

The free CI's zip step. It mirrors the Release zip builder's rules
(~/workspace/forge-browser-ai-release/build-release-zip.py) — the
same mode table, the same EXECUTABLES.txt manifest, the same
verification — but it builds from the tree it lives in, not from
the main tree path the Release builder hardcodes. That is the
whole point: from a fresh clone, or from any copy, this checks
the tree actually being checked.

Usage: python3 ci/build-zip.py --out <path>
Prints one summary line on success and exits 0; exits 1 with a
plain message on any failure. The zip is a throwaway — the check
is the point, not the artifact. The shipped Release zip is still
made by the out-of-repo builder at Release time.
"""

import argparse
import hashlib
import os
import sys
import time
import zipfile

TOP = "forge-browser-ai"

SKIP_DIRS = {"__pycache__", ".venv", ".git"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def tree_root():
    # ci/build-zip.py lives in ci/ — the tree root is one level up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def executables(tree):
    """The scripts that ship with the exec bit — read from the
    manifest in the tree (EXECUTABLES.txt), the single source of
    truth shared with install.sh and the test suite. Fails loudly
    if the manifest is missing rather than guessing."""
    path = os.path.join(tree, "EXECUTABLES.txt")
    if not os.path.isfile(path):
        raise SystemExit(f"missing manifest: {path}")
    names = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                names.add(line)
    if not names:
        raise SystemExit(f"empty manifest: {path}")
    return names


def collect(tree):
    """(files, dirs): root-relative paths, files sorted. Skips
    caches, the venv, git metadata, and bytecode — the same skips
    as the Release builder."""
    files = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            files.append((full, os.path.relpath(full, tree)))
    files.sort(key=lambda item: item[1])
    dirs = set()
    for _full, rel in files:
        parent = os.path.dirname(rel)
        while parent:
            dirs.add(parent)
            parent = os.path.dirname(parent)
    return files, dirs


def build(tree, out_path, executables_set):
    """Write the zip with the Release builder's modes: every
    entry gets create_system=3 (Unix) and an explicit
    external_attr with the file-type bits included (0o100755 for
    executables, 0o100644 for plain files, 0o40755 for
    directories) — without them, Mint's Archive Manager extracts
    every file non-executable. Returns (files, dirs, size)."""
    files, dirs = collect(tree)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in sorted(dirs):
            info = zipfile.ZipInfo(TOP + "/" + d + "/")
            info.create_system = 3
            info.external_attr = 0o40755 << 16
            zf.writestr(info, b"")
        for full, rel in files:
            arcname = TOP + "/" + rel
            mode = 0o100755 if rel in executables_set else 0o100644
            info = zipfile.ZipInfo(arcname)
            info.create_system = 3
            info.external_attr = mode << 16
            # Real mtime, not zipfile's 1980 default — the
            # extracted files should look like they were written
            # today.
            info.date_time = time.localtime(os.path.getmtime(full))[:6]
            with open(full, "rb") as handle:
                zf.writestr(info, handle.read())
    return len(files), len(dirs), os.path.getsize(out_path)


def verify(out_path, executables_set):
    """Reopen the zip and prove every entry carries the mode it
    should — the build fails loudly if an executable would
    extract without its exec bit. Same check as the Release
    builder's verify, basename against the manifest."""
    with zipfile.ZipFile(out_path) as zf:
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if info.create_system != 3:
                raise SystemExit(f"{info.filename}: create_system "
                                 f"is {info.create_system}, not 3 (Unix)")
            if info.is_dir():
                expect = 0o40755
            elif info.filename.split("/")[-1] in executables_set:
                expect = 0o100755
            else:
                expect = 0o100644
            if mode != expect:
                raise SystemExit(f"{info.filename}: mode {oct(mode)}, "
                                 f"expected {oct(expect)}")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a throwaway Release-shaped zip from this "
                    "tree and verify its stored modes (free CI, J1).")
    parser.add_argument("--out", required=True,
                        help="where to write the throwaway zip")
    args = parser.parse_args(argv)
    tree = tree_root()
    executables_set = executables(tree)
    files, dirs, size = build(tree, args.out, executables_set)
    verify(args.out, executables_set)
    print(f"OK: {files} files, {dirs} dirs, {size} bytes, modes "
          f"verified, SHA-256 {sha256_of(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
