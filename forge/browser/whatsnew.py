"""The "what changed" line (task F4).

After an update, the chat dock shows one line saying what the
new version brought — taken from the top of CHANGELOG.md, so it
is never stale. The last version seen is kept in a tiny file
under ~/.forge; a first launch records the version and shows
nothing (nothing changed yet).
"""

import os

from . import __version__


def version_seen_path():
    override = os.environ.get("FORGE_BROWSER_VERSION_SEEN_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "version-seen")


def changelog_path():
    # whatsnew.py lives in forge/browser/ — the changelog is at the
    # tree root, three levels up.
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "CHANGELOG.md")


def read_changelog_section(version, path=None):
    """The body lines of the `## <version>` section, or [] when the
    changelog or the section is missing."""
    try:
        with open(path or changelog_path(), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    heading = f"## {version}"
    body = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            in_section = (stripped == heading or
                          stripped.startswith(heading + " "))
            continue
        if in_section:
            body.append(line)
    return body


def whats_new_line(version, path=None):
    """One line for the new version: "What's new in 0.3: <the
    section's summary paragraph>". Falls back to a plain version
    line when the changelog has nothing for it."""
    body = [ln.strip() for ln in read_changelog_section(version, path)
            if ln.strip()]
    if body:
        # The first paragraph is the section's summary; join its
        # wrapped lines, stopping at the first bullet.
        summary = []
        for line in body:
            if line.startswith("- "):
                break
            summary.append(line)
        text = " ".join(summary) or body[0]
        if len(text) > 220:
            text = text[:217].rstrip() + "…"
        return f"What's new in {version}: {text}"
    return f"Forge Browser updated to {version}."


def check_whats_new():
    """The "what changed" line for this launch, or None. Records
    the current version when it runs, so the line shows once per
    update. A first launch (no record yet) shows nothing."""
    path = version_seen_path()
    try:
        with open(path, encoding="utf-8") as handle:
            seen = handle.read().strip()
    except OSError:
        seen = ""
    current = __version__
    if seen == current:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(current)
    except OSError:
        pass
    if not seen:
        return None  # first launch — nothing changed yet
    return whats_new_line(current)
