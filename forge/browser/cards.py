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
import re
import time
from dataclasses import dataclass, field

from .errors import BrowserError
from .summarize import summarize_page

# Same values as chat.py — kept local so this module never imports
# chat at module level (chat imports run_card lazily instead).
RESULT_CAP = 4500
PAGE_CAP = 6000
# O8: the final prompt (card prompt + action results) must fit the
# brain's context — 6000 tokens at ~4 chars each, leaving headroom
# for the reply.
FINAL_PROMPT_CHARS = 24000

# O10: the scaffold a new card starts from.
CARD_SCAFFOLD = """# {name}

Write the prompt here. {{question}} is the person's message and
{{page}} is the current page's text.

## Actions
ACTION {{"type": "read_page"}}
"""


@dataclass
class Card:
    name: str
    title: str
    prompt: str
    actions: list = field(default_factory=list)
    error: str = ""  # O1: set when the file exists but is broken


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
        # O6: the Actions header is case-insensitive and tolerates
        # spacing variants ("## actions", "##  Actions", "##Actions").
        if re.match(r"##\s*actions\s*$", line.strip(), re.IGNORECASE):
            section = "actions"
            continue
        if section == "actions":
            action_lines.append(line)
        # O7: the title is the first "# " line before the first
        # non-blank prompt line — leading blank lines don't hide it.
        elif line.startswith("# ") and not any(p.strip() for p in prompt_lines):
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
    on a name clash). Returns [Card] sorted by name. A card whose
    file exists but fails to parse is listed with its error set
    (O1) — a broken card is reported, never silently skipped."""
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
            except BrowserError as exc:
                found[name] = Card(name=name, title=name, prompt="",
                                   error=str(exc))
            except OSError:
                continue
    return [found[name] for name in sorted(found)]


def load_card(name):
    """One card by name, or BrowserError naming what exists. A card
    whose file is broken raises naming the break (O1), not 'no
    card'. The list is read once (O12)."""
    cards = list_cards()
    for card in cards:
        if card.name == name:
            if card.error:
                raise BrowserError(
                    f"card {name!r} is broken: {card.error}")
            return card
    known = ", ".join(card.name for card in cards) or "none"
    raise BrowserError(
        f"no card named {name!r} — known cards: {known}")


def validate_card(name):
    """(ok, message): is this card's file parseable? (O10 — the
    `card check` command.)"""
    for card in list_cards():
        if card.name == name:
            if card.error:
                return False, f"card {name!r} is broken: {card.error}"
            return True, (f"card {name!r} is fine "
                          f"({len(card.actions)} action(s))")
    return False, f"no card named {name!r}"


def new_card(name):
    """Write a scaffold card file in the person's cards dir (O10 —
    the `card new` command). Returns the path. Raises BrowserError
    when the name is bad or the card already exists."""
    name = (name or "").strip()
    if (not name or "/" in name or "\\" in name
            or name in (".", "..") or name.endswith(".md")):
        raise BrowserError("a card name is one word, no slashes, no .md")
    folder = cards_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".md")
    if os.path.exists(path):
        raise BrowserError(f"a card named {name!r} already exists")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(CARD_SCAFFOLD.format(name=name))
    return path


def _fill(value, question):
    if isinstance(value, str):
        return value.replace("{question}", question)
    return value


def _budget_prompt(prompt, results):
    """Trim the oldest results first until prompt + results fit
    FINAL_PROMPT_CHARS (O8); the prompt itself is truncated only
    when it alone exceeds the budget."""
    results = list(results)
    while True:
        text = prompt + ("\n\n" + "\n\n".join(results) if results else "")
        if len(text) <= FINAL_PROMPT_CHARS or not results:
            return text[:FINAL_PROMPT_CHARS]
        results.pop(0)


def run_card(session, name, question="", page_text=None,
             on_action=None, on_chunk=None, on_progress=None,
             on_step_done=None):
    """Run a card: its actions in order, then one brain call on the
    prompt + results. Returns (answer, opens) — opens are the URLs
    the card's open actions fetched, so the window can show them
    as tabs (O9). Honors cancel() between actions (O4): a stopped
    card returns ("Stopped.", opens so far). on_progress(text)
    reports each step as it starts. on_step_done(kind, seconds)
    (V4) reports each step's duration when it finishes, plus the
    final brain call as ("thinking", seconds)."""
    from .chat import _run_open, _run_research, _run_search, visible_text
    from .retrieve import page_context

    card = load_card(name)
    results = []
    opens = []
    total = len(card.actions)
    for index, action in enumerate(card.actions):
        # O4: Stop lands between actions — a card's research action
        # no longer runs for minutes with no way out.
        if getattr(session, "_stop_requested", False):
            return "Stopped.", opens
        if on_progress is not None:
            on_progress(f"Step {index + 1} of {total}: {action.get('type', '')}")
        kind = action.get("type")
        if on_action:
            on_action(kind, action)
        # V4: each card step is timed like a chat step.
        step_started = time.monotonic()
        try:
            if kind == "search":
                result = _run_search(_fill(str(action.get("query", "")), question))
            elif kind == "open":
                url = _fill(str(action.get("url", "")), question)
                result = _run_open(url)
                if url:
                    opens.append(url)
            elif kind == "research":
                topic = _fill(str(action.get("topic", "") or question), question)
                result = _run_research(topic)
            elif kind == "read_page":
                result = (page_text or "No page text is available.")[:RESULT_CAP]
            elif kind == "summarize":
                # K9: the mechanical map step rides the fast seat
                # when one is picked; the reduce stays on the brain.
                from .chat import ChatSession
                from .seats import pick_fast_model
                fast = pick_fast_model()
                map_session = None
                if fast and fast != session.model:
                    map_session = ChatSession(model=fast)
                result = (summarize_page(session, page_text,
                                         map_session=map_session)
                          if page_text else "No page text is available.")
            else:
                result = f"Unknown action type: {kind}"
        except Exception as exc:
            result = f"That action failed: {exc}"
        if on_step_done is not None:
            on_step_done(kind, time.monotonic() - step_started)
        results.append(f"RESULT of {kind}:\n{result[:RESULT_CAP]}")
    if getattr(session, "_stop_requested", False):
        return "Stopped.", opens
    prompt = card.prompt.replace("{question}", question)
    # O5: {page} is the relevant part of a long page (retrieval),
    # not just the head — a card asking about the bottom of a long
    # page answers from the right part. Short pages go in whole;
    # with no question there is no retrieval query, so the head
    # is used.
    if page_text and question:
        page = page_context(session, question, page_text)
    else:
        page = (page_text or "")[:PAGE_CAP]
    prompt = prompt.replace("{page}", page)
    prompt = _budget_prompt(prompt, results)
    think_started = time.monotonic()
    reply = session._chat([{"role": "user", "content": prompt}], on_chunk)
    if on_step_done is not None:
        on_step_done("thinking", time.monotonic() - think_started)
    return visible_text(reply), opens
