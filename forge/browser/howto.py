"""How to Use index (U22) — the manual as an index.

The manual is BROWSER.md at the repo/app root. This module parses
its headings into sections; the Help menu's "How to Use" window and
the local /howto page both read from here, so the index can never
drift from the manual — it IS the manual, split by heading.

Standard library only, like everything in the engine room.
"""

import os


def find_manual():
    """Path to BROWSER.md, or None when it is not installed.

    The manual ships at the app root (next to the forge/ package),
    so the first candidate covers both a repo checkout and the
    installed app. The others are fallbacks, not guesses.
    """
    here = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    candidates = (
        os.path.join(here, "BROWSER.md"),
        os.path.join(os.getcwd(), "BROWSER.md"),
        os.path.join(os.path.expanduser("~"), ".local", "share",
                     "forge-browser-app", "BROWSER.md"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_sections(path=None):
    """[(level, title, text)] — one entry per heading in BROWSER.md.

    level is the heading depth (1 for '#', 2 for '##', ...); text
    is the section body up to the next heading, stripped. An empty
    list means the manual is missing or has no headings — callers
    say so plainly instead of showing a blank index.
    """
    if path is None:
        path = find_manual()
    if path is None:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    sections = []
    current = None
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[hashes:].strip()
            if title:
                if current is not None:
                    sections.append(current)
                current = [hashes, title, []]
                continue
        if current is not None:
            current[2].append(line)
    if current is not None:
        sections.append(current)
    # Titles are index entries — visible UI text — so they carry
    # no em-dashes (U33): "Chat — the AI part" reads "Chat: the
    # AI part" in the index. Bodies stay the manual's own prose.
    return [(level, title.replace(" — ", ": "), "\n".join(body).strip())
            for level, title, body in sections]
