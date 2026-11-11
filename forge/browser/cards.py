"""Cards — saved workflows (task #9).

A card is a plain-text file: a `# Title`, a prompt, and an optional
`## Actions` section of ACTION lines. The session runs the card's
actions in order with the real engine, then hands the prompt +
results to the brain once for the final answer. Small models plan
badly and execute well — a canned workflow is a reliability boost,
not a shortcut.

Built-in cards ship in forge/browser/cards/. The person's own cards
live in ~/.forge/browser-cards/ (env FORGE_BROWSER_CARDS_DIR) and
win on a name clash. Placeholders: {question} is the person's
message, {page} is the current page's text.
"""

import json
import os
from dataclasses import dataclass, field

from .errors import BrowserError
from .summarize import summarize_page

# Same values as chat.py — kept local so this module never imports
# chat at module level (chat imports run_card lazily instead).
RESULT_CAP = 4500
PAGE_CAP = 6000


@dataclass
class Card:
    name: str
    title: str
    prompt: str
    actions: list = field(default_factory=list)


def cards_dir():
    override = os.environ.get("FORGE_BROWSER_CARDS_DIR", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "browser-cards")


def builtin_cards_dir():
    return os.path.join(os.path.dirname(__file__), "cards")


def _parse_action_line(line):
    """One 'ACTION {...}' line -> dict, or None when it is not one."""
    line = line.strip()
    if not line.startswith("ACTION "):
        return None
    try:
        action = json.loads(line[len("ACTION "):])
    except ValueError:
        return None
    return action if isinstance(action, dict) else None


def parse_card(name, text):
    """Parse card file text -> Card. Raises BrowserError on a bad
    action line, so a broken card fails loudly at load, not mid-run."""
    title = name
    prompt_lines, action_lines = [], []
    section = "prompt"
    for line in (text or "").splitlines():
        if line.strip() == "## Actions":
            section = "actions"
            continue
        if section == "actions":
            action_lines.append(line)
        elif line.startswith("# ") and not prompt_lines:
            title = line[2:].strip()
        else:
            prompt_lines.append(line)
    actions = []
    for line in action_lines:
        line = line.strip()
        if not line:
            continue
        action = _parse_action_line(line)
        if action is None:
            raise BrowserError(
                f"card {name}: bad action line: {line!r}")
        actions.append(action)
    return Card(name=name, title=title,
                prompt="\n".join(prompt_lines).strip(), actions=actions)


def list_cards():
    """All cards, built-in first, then the person's own (which win
    on a name clash). Returns [Card] sorted by name."""
    found = {}
    for folder in (builtin_cards_dir(), cards_dir()):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".md"):
                continue
            name = filename[:-3]
            path = os.path.join(folder, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    found[name] = parse_card(name, handle.read())
            except (OSError, BrowserError):
                continue
    return [found[name] for name in sorted(found)]


def load_card(name):
    """One card by name, or BrowserError naming what exists."""
    for card in list_cards():
        if card.name == name:
            return card
    known = ", ".join(card.name for card in list_cards()) or "none"
    raise BrowserError(
        f"no card named {name!r} — known cards: {known}")


def _fill(value, question):
    if isinstance(value, str):
        return value.replace("{question}", question)
    return value


def run_card(session, name, question="", page_text=None,
             on_action=None, on_chunk=None):
    """Run a card: its actions in order, then one brain call on the
    prompt + results. Returns the brain's answer."""
    from .chat import _run_open, _run_research, _run_search, visible_text

    card = load_card(name)
    results = []
    for action in card.actions:
        kind = action.get("type")
        if on_action:
            on_action(kind, action)
        try:
            if kind == "search":
                result = _run_search(_fill(str(action.get("query", "")), question))
            elif kind == "open":
                result = _run_open(_fill(str(action.get("url", "")), question))
            elif kind == "research":
                topic = _fill(str(action.get("topic", "") or question), question)
                result = _run_research(topic)
            elif kind == "read_page":
                result = (page_text or "No page text is available.")[:RESULT_CAP]
            elif kind == "summarize":
                result = (summarize_page(session, page_text)
                          if page_text else "No page text is available.")
            else:
                result = f"Unknown action type: {kind}"
        except Exception as exc:
            result = f"That action failed: {exc}"
        results.append(f"RESULT of {kind}:\n{result[:RESULT_CAP]}")
    prompt = card.prompt.replace("{question}", question)
    prompt = prompt.replace("{page}", (page_text or "")[:PAGE_CAP])
    if results:
        prompt += "\n\n" + "\n\n".join(results)
    reply = session._chat([{"role": "user", "content": prompt}], on_chunk)
    return visible_text(reply)
