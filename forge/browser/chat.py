"""The chat brain — what makes Forge Browser an AI browser.

You type plain language; a LOCAL model (Ollama, on your machine —
no API key, no account) drives the browser's own engine room:
keyless search, page reading, and the plan-gated research runner.
The model either answers directly or asks for one action at a
time with a line like:

    ACTION {"type": "search", "query": "..."}
    ACTION {"type": "open", "url": "https://..."}
    ACTION {"type": "research", "topic": "..."}
    ACTION {"type": "read_page"}
    ACTION {"type": "summarize"}
    ACTION {"type": "card", "name": "summarize-page"}
    ACTION {"type": "remember", "text": "the person likes dark mode"}
    ACTION {"type": "forget", "text": "dark mode"}
    ACTION {"type": "forget_page", "text": "https://example.org/page"}
    ACTION {"type": "recall"}
    ACTION {"type": "recall_pages", "query": "that thing about gears"}
    ACTION {"type": "brief"}
    ACTION {"type": "compare", "tabs": [1, 2, 3], "fields": ["price", "specs"]}

The session runs the action with the real engine, hands the
result back to the model, and the model answers from what it
actually found. Pages the person is looking at can be supplied
as context (the window passes the current tab's text).
"""

import json
import os
import re
import socket
import threading
import time
import urllib.request

from .errors import BrowserError
from .media import OLLAMA_URL
from .retrieve import page_context

MAX_ACTIONS = 3          # per user message — keeps small models on rails
RESULT_CAP = 4500        # chars of engine output handed to the model
PAGE_CAP = 6000          # chars of the current page handed to the model

# U9: one background brief build at a time — a second ask while
# one is running is told it is already building, not queued
# behind a second 30-60 s build.
_brief_job_lock = threading.Lock()
_brief_job_running = False


def _brief_job_start():
    global _brief_job_running
    with _brief_job_lock:
        if _brief_job_running:
            return False
        _brief_job_running = True
        return True


def _brief_job_finish():
    global _brief_job_running
    with _brief_job_lock:
        _brief_job_running = False
HISTORY_CAP = 12         # messages kept (plus the system prompt)
CHAT_NUM_CTX = 8192      # working context for the chat brain — never the
                         # 256K the model lists; the KV cache would eat the
                         # machine (Ryzen 3 / 8 GB)
TAB_LIST_LIMIT = 15      # tabs named in the tab list (J7); the rest
                         # are "and N more" — @tabN still works for them

# Ordered brain preference (task #1): each entry is the substrings a model
# name must contain, in order. qwen3.5:4b first — tools + vision in one
# model, so no second model ever has to load. Then qwen3:4b (the
# -instruct-2507 variant included), then qwen3.5:2b (his actual
# all-rounder), then the 1.7B fast seat. The first part of an entry
# is the family (plain substring); the rest are size tags, matched
# at a tag boundary (see _size_matches) so "4b" never matches "14b".
_CHAT_PREFERENCE = (
    ("qwen3.5", "4b"),
    ("qwen3", "4b"),
    ("qwen3.5", "2b"),
    ("qwen3", "1.7b"),
)

MAX_BRAIN_BYTES = int(4.5 * 1024 ** 3)  # size guide for an auto pick
                                        # (G6): over ~4.5 GB, a brain
                                        # swaps on an 8 GB machine
PROMPT_TOKEN_BUDGET = 7000  # of the 8192 ctx (G5); the rest is
                            # headroom for the reply
CHARS_PER_TOKEN = 4         # rough estimate for the budget

_PREFERRED = ("qwen", "llama", "gemma", "phi", "mistral", "minicpm",
              "deepseek", "smol", "tinyllama")

SYSTEM_PROMPT = """You are the brain inside Forge Browser, a web browser the user talks to.
You can browse for real. To do something, reply with ONE line exactly like:
ACTION {"type": "search", "query": "words to search"}
ACTION {"type": "open", "url": "https://example.org/page"}
ACTION {"type": "research", "topic": "a bigger question"}
ACTION {"type": "research", "topic": "a bigger question", "max_pages": 10, "detail": "full"}
ACTION {"type": "read_page"}
ACTION {"type": "summarize"}
ACTION {"type": "card", "name": "summarize-page"}
ACTION {"type": "remember", "text": "the person likes dark mode"}
ACTION {"type": "forget", "text": "dark mode"}
ACTION {"type": "forget_page", "text": "https://example.org/page"}
ACTION {"type": "recall"}
ACTION {"type": "recall_pages", "query": "that thing about gears"}
ACTION {"type": "brief"}
ACTION {"type": "compare", "tabs": [1, 2, 3], "fields": ["price", "specs"]}
Then wait for the RESULT. When you have enough, answer plainly and briefly,
using only what the results actually said. If the user just chats, answer
directly with no ACTION line. Never invent page contents, prices, or facts.
Tabs are numbered from 1 in the order shown; when the person writes @tab2,
answer from that tab's text, which is handed to you as its context.
When the person asks you to remember something, use the remember action
with their exact words. Recall lists what you remember; forget removes it.
When the person asks to forget a page they visited, use forget_page
with the page's URL or title.
When the person asks about something they read before ("that thing I read
last week"), use recall_pages with their words as the query.
When the person asks for the morning brief, use the brief action.
When the person asks to compare tabs, use the compare action with
the tab numbers and the fields to compare (price, specs, whatever
they named).
When the person asks for a short summary, pass "length": "short" on
the summarize action; otherwise leave it out for a standard one.
Research runs in the background while the person keeps browsing —
when you start it, say so plainly and that the report will open
when it is done; expect minutes, not seconds. A research action
may ask for depth: "max_pages" (1–20, default 6) caps the sources
read, "detail" (brief/standard/full, default standard) sets how
much of each source is excerpted. Ask for more depth only when
the question needs it — each source is a fetch plus a read, and
beyond 20 the run takes very long on this machine.
Your first action each turn is shown to the person as a plan for
approval before it runs; research, cards, and anything that
changes what is remembered ask again every time — so propose one
clear step at a time, never a vague one, and never act as if a
step already happened."""


def check_ollama():
    """(reachable, models) from the local Ollama /api/tags — models
    is [{"name", "size"}] with size in bytes. reachable is False
    when Ollama does not answer at all; an empty models list with
    reachable True means no models are installed (G9: the two
    states need different fixes)."""
    try:
        request = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False, []
    models = [{"name": m.get("name", ""), "size": m.get("size", 0) or 0}
              for m in data.get("models", []) if m.get("name")]
    return True, models


def list_models():
    """Names of the models the local Ollama reports, [] if none."""
    _reachable, models = check_ollama()
    return [m["name"] for m in models]


def _size_matches(low, size):
    """Does the size tag (e.g. "4b") match at a tag boundary —
    preceded by a non-alphanumeric (usually ":") and followed by a
    non-alphanumeric or the end? "14b" must not match "4b" (G1)."""
    return re.search(r"(?:^|[^0-9a-z])" + re.escape(size) +
                     r"(?:[^0-9a-z]|$)", low) is not None


def _preference_matches(low, parts):
    """A preference entry against a lowered model name: the family
    part is a plain substring, the size parts match at a boundary."""
    if parts[0] not in low:
        return False
    return all(_size_matches(low, part) for part in parts[1:])


def pick_chat_model_with_reason(models=None, sizes=None,
                                ignore_saved=False):
    """(model, reason, note). reason: "your pick" (saved seat),
    "env" (env var), "auto" (preference list), "fallback" (family
    hint or last resort), "none" (no brain). note: plain words when
    something was skipped or fell through, else "".

    Saved and env picks are validated against the installed list
    and fall through to auto when the named model is gone (G3).
    Auto picks skip models over the size guide (G6); an explicit
    pick is never overridden for size. An embed model is never
    picked, even as a last resort (G4). When Ollama does not
    answer, the pick is None — the header says so (G9) instead of
    naming a brain that cannot be reached.

    ignore_saved (T12): answer as if no pick were saved — what
    the "(auto)" entry in the picker would pick. The env var
    still applies, since (auto) only clears the saved pick."""
    if models is None:
        reachable, details = check_ollama()
        if not reachable:
            return None, "none", "Ollama is not answering"
        models = [d["name"] for d in details]
        if sizes is None:
            sizes = {d["name"]: d["size"] for d in details}
    sizes = sizes or {}
    note = ""
    from .seats import load_seats
    picked = None if ignore_saved else load_seats().get("brain")
    if picked:
        if picked in models:
            return picked, "your pick", ""
        note = (f"your saved pick {picked!r} is not installed — "
                "using auto")
    wanted = os.environ.get("FORGE_BROWSER_CHAT_MODEL", "").strip()
    if wanted:
        if wanted in models:
            return wanted, "env", ""
        if not note:
            note = (f"the {wanted!r} model from FORGE_BROWSER_CHAT_MODEL "
                    "is not installed — using auto")
    # Size-aware auto pick: skip models over the guide.
    oversize = [m for m in models
                if sizes.get(m) and sizes[m] > MAX_BRAIN_BYTES]
    candidates = [m for m in models if m not in oversize]
    if not candidates and models:
        # Every model is over the guide — take the smallest rather
        # than no brain at all, and say so.
        candidates = [min(models, key=lambda m: sizes.get(m) or 0)]
        if not note:
            note = ("every model is over the 4.5 GB size guide — using "
                    f"the smallest ({candidates[0]}); it may swap")
    lowered = [(name, name.lower()) for name in candidates]
    for parts in _CHAT_PREFERENCE:
        for name, low in lowered:
            if "embed" not in low and _preference_matches(low, parts):
                return name, "auto", note
    for name, low in lowered:
        if "embed" not in low and any(hint in low for hint in _PREFERRED):
            return name, "fallback", note
    for name, low in lowered:
        if "embed" not in low:
            return name, "fallback", note
    if not models:
        return None, "none", "no chat model is installed"
    return None, "none", note or "no chat model is installed"


def pick_chat_model(models=None, sizes=None):
    """Which local model drives the chat. A pick saved in the
    seats file wins; then the env var; then the ordered
    preference (qwen3.5:4b → qwen3:4b → qwen3.5:2b → qwen3:1.7b);
    then any chat-shaped name; then whatever is there.
    None = no brain. See pick_chat_model_with_reason for the
    reason and the validation/size rules."""
    model, _reason, _note = pick_chat_model_with_reason(models, sizes)
    return model


def check_model_answers(model, timeout=30):
    """A tiny call proving the model answers — "Say OK." with a
    short reply. True when it answers, False otherwise (Ollama
    down, model missing or broken, timeout). Used on refresh (G7)
    so a dead pick shows as dead before the first real question."""
    if not model:
        return False
    payload = json.dumps({"model": model,
                          "messages": [{"role": "user",
                                        "content": "Say OK."}],
                          "stream": False, "think": False,
                          "options": {"num_ctx": 512}}).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL + "/api/chat", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return bool((data.get("message") or {}).get("content", "").strip())
    except Exception:
        return False


def _estimate_tokens(messages):
    return sum(len(m.get("content", "")) for m in messages) // CHARS_PER_TOKEN


def budget_messages(messages, budget=PROMPT_TOKEN_BUDGET):
    """Trim a prompt to fit the token budget, oldest first. System
    messages (system prompt, page context, memory) are pinned —
    they never drop — and the final message (the question) always
    stays. Returns a new list (G5)."""
    messages = list(messages)
    while _estimate_tokens(messages) > budget:
        dropped = False
        for index, message in enumerate(messages[:-1]):
            if message["role"] != "system":
                del messages[index]
                dropped = True
                break
        if not dropped:
            break
    return messages


def parse_action(text):
    """The first ACTION line in a model reply, as a dict, or None."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("ACTION"):
            payload = stripped[len("ACTION"):].strip()
            try:
                action = json.loads(payload)
            except ValueError:
                return None
            return action if isinstance(action, dict) and action.get("type") else None
    return None


def visible_text(text):
    """A model reply with its ACTION lines removed (for display)."""
    lines = [ln for ln in (text or "").splitlines()
             if not ln.strip().startswith("ACTION")]
    return "\n".join(lines).strip()


# Actions that change something the person keeps (their memory
# file) — these ask again even inside an approved turn.
SUBMIT_ACTIONS = {"remember", "forget", "forget_page"}

# P1: actions gated every time, not just when first — a research
# job or a card run can take minutes, so an approval for a search
# must never start one. "open" is deliberately not here: it is
# cheap, visible (a tab appears), and reversible (close the tab),
# so gating it every time is click-fatigue, not safety.
EXPENSIVE_ACTIONS = {"research", "card"}


def describe_action(kind, action):
    """One action in plain words, for the plan-approval dialog."""
    action = action or {}
    if kind == "search":
        return f"Search the web for \"{action.get('query', '')}\""
    if kind == "open":
        return f"Open {action.get('url', '')} in a new tab"
    if kind == "research":
        return (f"Research \"{action.get('topic', '')}\" in the background "
                f"(the report opens when it is done)")
    if kind == "read_page":
        return "Read the text of the current page"
    if kind == "summarize":
        return "Summarize the current page"
    if kind == "card":
        # O3: the plan shows the card's steps, not just its name —
        # the approval covers what will actually run. A nested card
        # step is named, not expanded (a self-referential card
        # must not recurse).
        name = action.get("name", "")
        try:
            from .cards import load_card
            card = load_card(name)
        except Exception:
            card = None
        if card is None or not card.actions:
            return f"Run the \"{name}\" workflow card"

        def _step(step_action):
            if step_action.get("type") == "card":
                return (f"Run the \"{step_action.get('name', '')}\" "
                        "workflow card")
            return describe_action(step_action.get("type"), step_action)

        lines = "\n".join(
            f"  {i}. {_step(a)}" for i, a in enumerate(card.actions, 1))
        return f"Run the \"{name}\" workflow card:\n{lines}"
    if kind == "remember":
        return f"Remember: \"{action.get('text', '')}\""
    if kind == "forget":
        return f"Forget anything about \"{action.get('text', '')}\""
    if kind == "forget_page":
        return (f"Forget the stored page matching "
                f"\"{action.get('text', '')}\"")
    if kind == "recall":
        return "List what has been remembered"
    if kind == "recall_pages":
        return f"Search pages you visited for \"{action.get('query', '')}\""
    if kind == "brief":
        return "Build the morning brief from yesterday's pages"
    if kind == "compare":
        tabs = action.get("tabs") or []
        fields = action.get("fields") or []
        # P7: "Compare the open tabs on..." when no tabs are named,
        # not "Compare tabs the open tabs on...".
        tabs_text = (f"tabs {', '.join(str(n) for n in tabs)}"
                     if tabs else "the open tabs")
        return (f"Compare {tabs_text}"
                f" on {', '.join(fields) or 'price and key specs'}")
    return f"Run the {kind} action"


# V1: an action kind (or "thinking") in plain words for the
# working line and the step log — the same vocabulary as
# describe_action, as a present participle ("searching…",
# "reading the page…", "searching visited pages…", "comparing…").
_STEP_WORDS = {
    "thinking": "thinking",
    "answering": "answering",
    "search": "searching",
    "open": "opening the page",
    "research": "researching",
    "read_page": "reading the page",
    "summarize": "summarizing",
    "card": "running the card",
    "remember": "remembering",
    "forget": "forgetting",
    "forget_page": "forgetting the page",
    "recall": "recalling",
    "recall_pages": "searching visited pages",
    "brief": "building the brief",
    "compare": "comparing",
}


def step_words(kind):
    """A step name in plain words. Anything unknown passes through
    unchanged — the dock's own phases ("reading tabs", "running
    card", "waiting for approval") are already plain words."""
    return _STEP_WORDS.get(kind, kind)


def format_duration(seconds):
    """A turn or step length in plain words: "<1s" under a second
    (V5 — "Done in 0s." read as if nothing happened), whole
    seconds above it."""
    if seconds < 1:
        return "<1s"
    return f"{int(seconds)}s"


class LiveDisplay:
    """Incrementally filters streamed reply text for display.

    Drops ACTION lines — the person sees the brain's words as
    they arrive, not the one-line action protocol — and streams
    partial lines at once (H1): a held remainder is shown as
    soon as it cannot become an ACTION line, so a long paragraph
    with no newline still appears word by word instead of
    sitting silent until the newline. Only a remainder that is
    still an ACTION prefix ("ACT") or already starts with
    "ACTION" ("ACTION {..." mid-line) is held. feed() returns
    the displayable text for a chunk; flush() returns any held
    remainder."""
    def __init__(self):
        self.buf = ""

    def feed(self, delta):
        self.buf += delta or ""
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if not line.strip().startswith("ACTION"):
                out.append(line + "\n")
        # The remainder is a partial line. Hold it only while it
        # could still turn out to be an ACTION line.
        stripped = self.buf.lstrip()
        if not ("ACTION".startswith(stripped)
                or stripped.startswith("ACTION")):
            out.append(self.buf)
            self.buf = ""
        return "".join(out)

    def flush(self):
        line, self.buf = self.buf, ""
        if line and not line.strip().startswith("ACTION"):
            return line
        return ""


# --- engine hands (wrapped so tests can stand in) -----------------

def _run_search(query):
    from .search import search
    results = search(query, max_results=5)
    if not results:
        return "No results — every search backend came back empty."
    return "\n".join(
        f"{i}. {r.title or r.url}\n   {r.url}\n   {r.snippet or ''}"
        for i, r in enumerate(results, 1))


def _run_open(url):
    from .fetch import fetch
    return fetch(url, max_chars=RESULT_CAP).markdown


def _run_research(topic):
    from .research import make_plan, run_research
    plan = make_plan(topic)
    # The person asking in chat IS the plan approval.
    report, _sources = run_research(plan, approved=True)
    return report[:RESULT_CAP]


# --- the session ---------------------------------------------------

class ChatSession:
    def __init__(self, model=None):
        if model is not None:
            self.model = model
            self.pick_reason = "given"
            self.pick_note = ""
            self.brain_state = "ok" if model else "no-models"
        else:
            self.repick()
        self.history = []
        self._retrieve_cache = {}  # page hash -> (chunks, embeddings)
        self._embed_cache = None  # lazy EmbedCache (R6): fact + chunk
        # vectors reused across turns, persisted on disk (R4)
        self._retrieve_last = None  # what the last page_context did (I10)
        self._retrieve_notice_shown = False  # the I7 notice fires once
        self._tab_groups_cache = {}  # (title, url) tuple -> grouped tabs
        self._stop_requested = False  # set by cancel(); read between steps
        self.last_speed = None  # tok/s of the last finished turn (G10)
        self._turn_tokens = 0   # tokens generated this turn
        self._turn_seconds = 0.0  # seconds spent generating this turn

    def repick(self):
        """Re-run the brain pick (refresh button, seat change) and
        update the model, reason, note, and state. Returns the model."""
        (self.model, self.pick_reason,
         self.pick_note) = pick_chat_model_with_reason()
        # brain_state: "ok", "ollama-down", or "no-models" — the
        # header renders each differently (G9).
        if self.model:
            self.brain_state = "ok"
        elif self.pick_note == "Ollama is not answering":
            self.brain_state = "ollama-down"
        else:
            self.brain_state = "no-models"
        return self.model

    def cancel(self):
        """Stop the running turn at the next step boundary. A brain
        call already in flight finishes first — Stop lands between
        steps, never mid-word."""
        self._stop_requested = True

    def _get_embed_cache(self):
        """The session's embedding cache (R6), created on first use
        — the file loads lazily, so a session that never recalls
        never touches the disk."""
        if self._embed_cache is None:
            from .memory import EmbedCache
            self._embed_cache = EmbedCache()
        return self._embed_cache

    def ask_once(self, prompt):
        """One brain call with no action loop — for one-shot jobs
        like the morning brief. Returns the reply text."""
        return self._chat([{"role": "user", "content": prompt}])

    def _chat(self, messages, on_chunk=None):
        """One brain call. Without on_chunk this waits for the
        whole reply (stream False). With on_chunk, Ollama streams
        NDJSON lines and each content piece is handed to on_chunk
        as it arrives, so words appear while they are generated —
        at 3–6 tok/s, waiting for the full answer feels broken.
        Returns the full reply text either way."""
        if not self.model:
            raise BrowserError(
                "No chat brain: no local model is answering at "
                f"{OLLAMA_URL}. Start Ollama (and pull a small chat "
                "model), then try again — browsing itself still works.")
        # Thinking OFF: every think-token is generated at the same
        # slow speed before any answer appears. num_ctx capped at
        # 8192 (see CHAT_NUM_CTX) — the listed 256K would not fit.
        payload = json.dumps({"model": self.model, "messages": messages,
                              "stream": on_chunk is not None, "think": False,
                              "options": {"num_ctx": CHAT_NUM_CTX}}).encode("utf-8")
        request = urllib.request.Request(
            OLLAMA_URL + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        started = time.monotonic()
        # P3: a streaming read waits at most 10 s per line, so Stop
        # lands within ~10 s instead of after a minutes-long
        # generation. A timeout on one read is retried — the
        # server may just be slow between tokens. The non-streaming
        # call keeps the 240 s wait for the whole reply.
        read_timeout = 10 if on_chunk is not None else 240
        try:
            with urllib.request.urlopen(request,
                                        timeout=read_timeout) as response:
                if on_chunk is None:
                    data = json.loads(response.read().decode("utf-8"))
                    text = (data.get("message") or {}).get("content", "")
                else:
                    parts = []
                    while True:
                        if self._stop_requested:
                            break
                        try:
                            line = response.readline()
                        except socket.timeout:
                            continue
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line.decode("utf-8"))
                        except ValueError:
                            continue
                        content = (data.get("message") or {}).get("content") or ""
                        if content:
                            parts.append(content)
                            on_chunk(content)
                    text = "".join(parts)
        except Exception as exc:
            raise BrowserError(
                f"The chat brain ({self.model}) did not answer: {exc}") from exc
        # Per-call speed, for the per-turn readout (G10). Tokens are
        # estimated the same way as the prompt budget (~4 chars each).
        self._turn_tokens += len(text) // CHARS_PER_TOKEN
        self._turn_seconds += max(time.monotonic() - started, 0.001)
        return text

    def ask(self, user_text, page_text=None, on_action=None, on_chunk=None,
            tabs=None, page_url=None, on_plan=None, on_table=None,
            on_progress=None, on_research_done=None, on_step=None,
            on_note=None, on_brief_done=None, on_step_done=None):
        """One chat turn. Returns (answer, urls_to_open_in_tabs).
        tabs: [{title, url, text, current}] in visible order, for @tabN.
        on_plan(plan_text, kind) -> bool gates the work: the first
        action of the turn runs only when it approves, and expensive
        actions (research/card) and submit actions (remember/forget)
        ask again every time. A cheap kind the person has
        always-allowed runs without asking. A denial feeds "The
        person said no to that." back to the model as a RESULT and
        the turn continues, so it can propose an alternative. None =
        no gate (tests and programmatic use); the window and the CLI
        pass one. on_table(html) receives a comparison table to render
        in the window. on_progress(text) carries research progress;
        on_research_done(report, sources, error) delivers the
        finished report. on_brief_done(text) delivers a background
        brief build (U9). on_step(step) names what the turn is doing
        right now — "thinking" while the brain generates, the action
        kind while an action runs — so the window can show an honest
        working line with elapsed time. on_step_done(step, seconds)
        fires when a step finishes (V4) — each brain call as
        "thinking", each action under its kind — so the dock and
        the CLI can log per-step durations ("search took 8s").
        on_note(text) carries a plain-words note for the log (an
        empty named tab, J2).
        cancel() stops the turn at the next step boundary, and
        interrupts a streaming generation in flight (P3)."""
        self._stop_requested = False
        self._turn_tokens = 0
        self._turn_seconds = 0.0
        self.last_speed = None
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Memory: what the person asked to be remembered, when it is
        # relevant — never on a page that looks sensitive.
        from .memory import search_entries, site_memory_allowed
        if page_url is None and tabs:
            for tab in tabs:
                if tab.get("current"):
                    page_url = tab.get("url") or ""
                    break
        from .seats import seats_enabled
        if seats_enabled("memory") and site_memory_allowed(page_url):
            from .retrieve import embed_texts
            found = search_entries(user_text, embed_fn=embed_texts,
                                   embed_cache=self._get_embed_cache())
            if found:
                lines = "\n".join(f"- {e['text']}" for e in found)
                messages.append({"role": "system",
                                 "content": "Things you asked me to remember:\n" + lines})
        # @tabN picks tabs; otherwise the current tab's text.
        chosen_text = page_text
        chosen_title = None
        chosen_url = page_url
        named = []  # (number, title, text) for every valid @tabN ref (J1)
        if tabs:
            from .retrieve import embed_texts
            from .titles import group_items
            cache_key = tuple((t.get("title") or "", t.get("url") or "")
                              for t in tabs)
            groups = self._tab_groups_cache.get(cache_key)
            if groups is None:
                groups = group_items(tabs, embed_fn=embed_texts)
                self._tab_groups_cache = {cache_key: groups}
            # J3: the list prints in visible order (numbers
            # ascending), the group name a label on each line.
            # Grouped blocks reordered the numbers, and a small
            # model could misread which number was which.
            group_of = {}
            for group in groups:
                for tab in group["items"]:
                    group_of[id(tab)] = group["name"]
            show_groups = len(groups) > 1
            lines = []
            # J7: cap the list — the first 15 plus "and N more".
            # @tabN still works for the unlisted tabs.
            for i, tab in enumerate(list(tabs)[:TAB_LIST_LIMIT], 1):
                marker = " (current)" if tab.get("current") else ""
                agent = "🤖 " if tab.get("agent") else ""
                label = (f" [group: {group_of.get(id(tab), '')}]"
                         if show_groups else "")
                lines.append(f"{i}. {agent}{tab.get('title') or 'Untitled'} — "
                             f"{tab.get('url') or ''}{marker}{label}")
            if len(tabs) > TAB_LIST_LIMIT:
                lines.append(
                    f"... and {len(tabs) - TAB_LIST_LIMIT} more tabs")
            tab_note = "Open tabs:\n" + "\n".join(lines)
            refs = re.findall(r"@tab(\d+)", user_text or "")
            seen_refs = set()
            for raw in refs:
                n = int(raw)
                if n in seen_refs:
                    continue
                seen_refs.add(n)
                if 1 <= n <= len(tabs):
                    tab = tabs[n - 1]
                    named.append((n, tab.get("title") or f"tab {n}",
                                  tab.get("text") or ""))
                else:
                    # J9: "only 1 tab", not "only 1 tabs".
                    tab_note += (f"\n(The person mentioned @tab{n}, but "
                                 f"there {'is' if len(tabs) == 1 else 'are'} "
                                 f"only {len(tabs)} "
                                 f"tab{'s' if len(tabs) != 1 else ''} — "
                                 "say so plainly.)")
            if named:
                # J1: every named tab feeds the brain, each through
                # retrieval and labeled per tab — the first ref is
                # also the tab read_page/summarize follow.
                chosen_text = named[0][2]
                chosen_title = named[0][1]
                chosen_url = tabs[named[0][0] - 1].get("url") or ""
                parts = []
                for n, title, text in named:
                    if (text or "").strip():
                        parts.append(
                            f"Text of {title} (@tab{n}):\n"
                            f"{page_context(self, user_text, text)}")
                    else:
                        # J2: an empty named tab is named, to the
                        # brain and to the log — never silently
                        # dropped.
                        parts.append(f"(@tab{n}, {title}, had no "
                                     f"readable text.)")
                        if on_note is not None:
                            on_note(f"Tab {n} ({title}) had no readable text.")
                messages.append({"role": "system",
                                 "content": "\n\n".join(parts)})
            messages.append({"role": "system", "content": tab_note})
        elif re.search(r"@tab\d+", user_text or ""):
            messages.append({"role": "system",
                             "content": "The person mentioned a tab (@tabN), "
                                        "but this chat has no tabs — say so plainly."})
        # O2: the brain knows which cards exist, so it can pick and
        # suggest them instead of guessing exact names. Broken
        # cards are left out — the brain cannot run them.
        from .cards import list_cards
        cards = [c for c in list_cards() if not c.error]
        if cards:
            lines = "\n".join(f"- {c.name}: {c.title}" for c in cards)
            messages.append({"role": "system",
                             "content": "Saved workflow cards you can run "
                                        "with the card action:\n" + lines})
        if chosen_text and not named:
            label = "Text of the page the user is looking at"
            if chosen_title:
                label = f"Text of {chosen_title} (the tab the user named)"
            messages.append({"role": "system",
                             "content": f"{label}:\n"
                                        f"{page_context(self, user_text, chosen_text)}"})
        messages.extend(self.history[-HISTORY_CAP:])
        messages.append({"role": "user", "content": user_text})
        open_requests = []
        answer = ""
        actions_run = 0
        for _ in range(MAX_ACTIONS + 1):
            if self._stop_requested:
                answer = "Stopped."
                break
            if on_step:
                on_step("thinking")
            # Token-budget the prompt (G5): trim oldest history to
            # fit the ctx, keeping the system prompt + page context
            # pinned. The working list keeps growing across steps;
            # the budget is re-applied on every brain call.
            # V4: the brain call is a timed step — its duration
            # lands in the step log when it returns.
            think_started = time.monotonic()
            reply = self._chat(budget_messages(messages), on_chunk)
            if on_step_done is not None:
                on_step_done("thinking", time.monotonic() - think_started)
            # P3: a generation Stop interrupted never runs an
            # action — the partial text may hold an ACTION line.
            if self._stop_requested:
                answer = "Stopped."
                break
            messages.append({"role": "assistant", "content": reply})
            action = parse_action(reply)
            if action is None:
                answer = visible_text(reply)
                break
            kind = action.get("type")
            # The plan gate (P1/P4/P5): the first action of the turn
            # needs approval; expensive actions (research, card)
            # and submit actions (remember/forget) ask again every
            # time, even inside an approved turn. A cheap kind the
            # person has always-allowed runs without asking. A
            # denial feeds back to the model as a RESULT and the
            # turn continues — the same for the first action as for
            # a submit action, so the model can propose something
            # else instead of the turn ending.
            if on_plan is not None:
                from .seats import always_allowed
                needs_gate = (actions_run == 0
                              or kind in EXPENSIVE_ACTIONS
                              or kind in SUBMIT_ACTIONS)
                if needs_gate and not always_allowed(kind):
                    plan = describe_action(kind, action)
                    if kind in SUBMIT_ACTIONS:
                        plan += " (this changes what I remember)"
                    if not on_plan(plan, kind):
                        messages.append({"role": "user", "content":
                                         f"RESULT of {kind}:\n"
                                         "The person said no to that."})
                        continue
            if on_action:
                on_action(kind, action)
            if on_step:
                on_step(kind)
            actions_run += 1
            # V4: the action is a timed step — the finally fires
            # even for the card branch, which breaks out of the
            # try. The plan-gate wait is NOT inside the timing:
            # the gate ran before on_action, so waiting never
            # counts as action time.
            action_started = time.monotonic()
            try:
                if kind == "search":
                    result = _run_search(str(action.get("query", "")))
                elif kind == "open":
                    url = str(action.get("url", ""))
                    result = _run_open(url)
                    if url:
                        open_requests.append(url)
                elif kind == "research":
                    # Research runs in the background — the turn does
                    # not wait minutes for it. Cards still use the
                    # synchronous _run_research (they need the report).
                    # A second ask queues behind the running job instead
                    # of being refused (task F1).
                    from .research import (DETAIL_CHARS,
                                           research_job_running,
                                           research_queue,
                                           start_research_job)
                    topic = str(action.get("topic", "") or user_text)
                    # Q7: depth control — max_pages clamped to 1–20
                    # (each source is a fetch plus a read; beyond 20
                    # the run takes very long on this machine) and
                    # detail mapped to excerpt length.
                    raw_pages = action.get("max_pages")
                    try:
                        max_pages = (int(raw_pages)
                                     if raw_pages is not None else 6)
                    except (TypeError, ValueError):
                        max_pages = 6
                    max_pages = max(1, min(20, max_pages))
                    detail = str(action.get("detail") or "standard").lower()
                    max_chars = DETAIL_CHARS.get(detail, 4000)
                    running = research_job_running()
                    start_research_job(
                        topic, on_progress=on_progress,
                        max_pages=max_pages,
                        max_chars_per_source=max_chars,
                        on_done=(lambda report, sources, error=None: (
                            on_research_done(report, sources, error)
                            if on_research_done else None)))
                    if running:
                        position = len(research_queue())
                        result = (f"A research job is already running on "
                                  f"{running} — research on {topic!r} is "
                                  f"queued (position {position}) and starts "
                                  "when the running job finishes.")
                    else:
                        result = (f"Research on {topic!r} started in the "
                                  "background — it runs while you browse, "
                                  "and the report opens when it is done. "
                                  "Expect minutes, not seconds.")
                elif kind == "read_page":
                    result = (chosen_text or "No page text is available "
                              "in this chat.")[:RESULT_CAP]
                elif kind == "summarize":
                    if chosen_text:
                        from .seats import pick_fast_model
                        from .summarize import (SummarizeEmptyError,
                                                summarize_page)
                        # K9: the mechanical map step rides the fast
                        # seat when one is picked; the reduce stays
                        # on the brain.
                        fast = pick_fast_model()
                        map_session = None
                        if fast and fast != self.model:
                            map_session = ChatSession(model=fast)
                        try:
                            result = summarize_page(
                                self, chosen_text, url=chosen_url,
                                on_chunk=on_chunk, on_progress=on_progress,
                                map_session=map_session,
                                length=str(action.get("length") or
                                           "standard"))
                        except SummarizeEmptyError:
                            result = ("The page has text, but the brain "
                                      "produced no summary.")
                    else:
                        result = ("No page text is available in this chat — "
                                  "open a page in the window first.")
                elif kind == "card":
                    # A card runs its own workflow and answers directly —
                    # the turn ends here, not with another model step.
                    # O9: pages the card opened surface as tabs, like
                    # the chat's own open does.
                    from .cards import run_card
                    answer, card_opens = run_card(
                        self, str(action.get("name", "")),
                        question=user_text, page_text=chosen_text,
                        on_action=on_action, on_chunk=on_chunk,
                        on_progress=on_progress,
                        on_step_done=on_step_done)
                    open_requests.extend(card_opens)
                    break
                elif kind == "remember":
                    from .memory import remember
                    entry = remember(str(action.get("text", "")))
                    result = (f"Remembered: {entry['text']}" if entry
                              else "There was nothing to remember.")
                elif kind == "forget":
                    from .memory import forget
                    removed = forget(str(action.get("text", "")))
                    result = (f"Forgot {removed} remembered thing(s)." if removed
                              else "I don't have that remembered.")
                elif kind == "forget_page":
                    from .memory import forget_page
                    removed = forget_page(str(action.get("text", "")))
                    result = (f"Forgot {removed} stored page(s)." if removed
                              else "No stored page matched that.")
                elif kind == "recall":
                    from .memory import list_entries
                    entries = list_entries()
                    result = ("\n".join(f"- {e['text']}" for e in entries)
                              if entries else "Nothing remembered yet.")
                elif kind == "recall_pages":
                    from .memory import search_pages
                    query = str(action.get("query", "") or user_text)
                    found = search_pages(
                        query, embed_cache=self._get_embed_cache())
                    if found:
                        result = "\n\n".join(
                            f"{e['title']} — {e['url']}\n{e['text']}"
                            for e in found)
                    else:
                        result = ("Nothing in the pages you visited "
                                  "matches that.")
                elif kind == "brief":
                    # U9: a cached brief answers at once; an
                    # uncached one builds in the background like
                    # research — the turn does not wait 30-60 s
                    # for it, and the result arrives via
                    # on_brief_done. Without a callback (the CLI)
                    # the build runs inline, as before. The cached
                    # check mirrors build_brief's U1 rule: a
                    # summary-less cached brief is rebuilt when a
                    # brain is available, not served as-is.
                    from .brief import (brief_text, build_brief,
                                        load_brief, yesterday_date)
                    cached = load_brief(yesterday_date())
                    has_brain = bool(getattr(self, "model", None))
                    if cached is not None and (cached.get("summary")
                                               or not has_brain):
                        result = brief_text(cached)
                    elif on_brief_done is None:
                        result = brief_text(build_brief(self))
                    elif _brief_job_start():
                        def _build_brief_job():
                            try:
                                job_session = ChatSession(
                                    model=self.model)
                                text = brief_text(
                                    build_brief(job_session))
                            except Exception as exc:
                                text = ("Couldn't build the brief: "
                                        f"{exc}")
                            finally:
                                _brief_job_finish()
                            on_brief_done(text)

                        threading.Thread(target=_build_brief_job,
                                         daemon=True).start()
                        result = ("Building your morning brief in the "
                                  "background — it will appear here "
                                  "when it is done.")
                    else:
                        result = ("A brief is already building — it "
                                  "will appear here when it is done.")
                elif kind == "compare":
                    from .compare import (DEFAULT_FIELDS, compare_table_html,
                                          compare_table_text, compare_tabs,
                                          parse_fields_from_text,
                                          set_last_comparison)
                    raw_numbers = action.get("tabs") or [
                        int(n) for n in re.findall(r"@tab(\d+)", user_text or "")]
                    # M3: numeric strings coerce (int("1") → 1);
                    # only non-numbers are dropped.
                    numbers = []
                    for number in raw_numbers:
                        try:
                            numbers.append(int(number))
                        except (TypeError, ValueError):
                            continue
                    fields = [str(f) for f in (action.get("fields") or [])
                              if str(f).strip()]
                    if not fields:
                        # M10: the person's words name the fields
                        # when the model's ACTION omits them.
                        fields = parse_fields_from_text(user_text or "")
                    if not fields:
                        fields = DEFAULT_FIELDS
                    all_tabs = list(tabs or [])
                    chosen = [all_tabs[n - 1] for n in numbers
                              if 1 <= n <= len(all_tabs)]
                    if not chosen and all_tabs:
                        # "compare these three" with no numbers: the
                        # first three open tabs.
                        chosen = all_tabs[:3]
                    if not chosen:
                        result = "There are no tabs open to compare."
                    else:
                        def _compare_progress(i, total):
                            if on_progress:
                                on_progress(f"tab {i} of {total}")

                        rows, dropped = compare_tabs(
                            self, chosen, fields,
                            on_progress=_compare_progress)
                        set_last_comparison(rows, fields)
                        if on_table:
                            on_table(compare_table_html(rows, fields,
                                                        dropped=dropped))
                        result = compare_table_text(
                            rows, fields, dropped=dropped,
                            max_chars=RESULT_CAP)
                else:
                    result = f"Unknown action type: {kind}"
            except Exception as exc:
                result = f"That action failed: {exc}"
            finally:
                if on_step_done is not None:
                    on_step_done(kind, time.monotonic() - action_started)
            messages.append({"role": "user",
                             "content": f"RESULT of {kind}:\n{result[:RESULT_CAP]}"})
        else:
            answer = visible_text(reply) or \
                "I ran out of steps on that one — try asking a smaller piece."
        if self._turn_seconds > 0:
            self.last_speed = self._turn_tokens / self._turn_seconds
        self.history.append({"role": "user", "content": user_text})
        if answer:
            self.history.append({"role": "assistant", "content": answer})
        return answer, open_requests
