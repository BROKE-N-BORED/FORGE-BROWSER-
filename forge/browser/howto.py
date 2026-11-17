"""How to Use index (U22) — the manual as an index.

The manual is BROWSER.md at the repo/app root. This module parses
its headings into sections; the Help menu's "How to Use" window and
the local /howto page both read from here, so the index can never
drift from the manual — it IS the manual, split by heading.

Standard library only, like everything in the engine room.
"""

import os


# R3-10b: the manual's developer topics — engine-room internals,
# CLI reference, and build/verify notes a first-time user never
# needs. How to Use lists the user topics first and these under
# a "Developer topics" heading, so a new user never meets
# "Verify (the #65 contract)" or "Research (Item 3)" unprompted.
# Titles are matched after load_sections' em-dash cleanup.
DEVELOPER_SECTIONS = frozenset((
    "Fetch address fence",
    "Engine downloads are pinned + verified",
    "Kitesurf-local: a third engine",
    "Every verification starts fresh",
    "Commands",
    "Rendered pages, snapshots, screenshots (Item 2)",
    "The browser engine's debugging port",
    "Verify (the #65 contract)",
    "Research (Item 3)",
    "Setup for the optional engines (Item 4)",
    "What is in the folder",
    "Tests",
))


def split_sections(sections):
    """(user_sections, developer_sections) from load_sections'
    output — every section lands in exactly one list, in manual
    order. The developer content stays reachable, just grouped
    after the user topics."""
    user = [entry for entry in sections
            if entry[1] not in DEVELOPER_SECTIONS]
    developer = [entry for entry in sections
                 if entry[1] in DEVELOPER_SECTIONS]
    return user, developer


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
