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
import urllib.request

from .errors import BrowserError
from .media import OLLAMA_URL
from .retrieve import page_context

MAX_ACTIONS = 3          # per user message — keeps small models on rails
RESULT_CAP = 4500        # chars of engine output handed to the model
PAGE_CAP = 6000          # chars of the current page handed to the model
HISTORY_CAP = 12         # messages kept (plus the system prompt)
CHAT_NUM_CTX = 8192      # working context for the chat brain — never the
                         # 256K the model lists; the KV cache would eat the
                         # machine (Ryzen 3 / 8 GB)

# Ordered brain preference (task #1): each entry is the substrings a model
# name must contain, in order. qwen3.5:4b first — tools + vision in one
# model, so no second model ever has to load. Then qwen3:4b (the
# -instruct-2507 variant included), then the 1.7B fast seat.
_CHAT_PREFERENCE = (
    ("qwen3.5", "4b"),
    ("qwen3", "4b"),
    ("qwen3", "1.7b"),
)

_PREFERRED = ("qwen", "llama", "gemma", "phi", "mistral", "minicpm",
              "deepseek", "smol", "tinyllama")

SYSTEM_PROMPT = """You are the brain inside Forge Browser, a web browser the user talks to.
You can browse for real. To do something, reply with ONE line exactly like:
ACTION {"type": "search", "query": "words to search"}
ACTION {"type": "open", "url": "https://example.org/page"}
ACTION {"type": "research", "topic": "a bigger question"}
ACTION {"type": "read_page"}
ACTION {"type": "summarize"}
ACTION {"type": "card", "name": "summarize-page"}
ACTION {"type": "remember", "text": "the person likes dark mode"}
ACTION {"type": "forget", "text": "dark mode"}
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
When the person asks about something they read before ("that thing I read
last week"), use recall_pages with their words as the query.
When the person asks for the morning brief, use the brief action.
When the person asks to compare tabs, use the compare action with
the tab numbers and the fields to compare (price, specs, whatever
they named).
Research runs in the background while the person keeps browsing —
when you start it, say so plainly and that the report will open
when it is done; expect minutes, not seconds.
Your first action each turn is shown to the person as a plan for
approval before it runs, and anything that changes what is
remembered asks again — so propose one clear step at a time,
never a vague one, and never act as if a step already happened."""


def list_models():
    """Names of the models the local Ollama reports, [] if none."""
    try:
        request = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def pick_chat_model(models=None):
    """Which local model drives the chat. A pick saved in the
    seats file wins; then the env var; then the ordered
    preference (qwen3.5:4b → qwen3:4b → qwen3:1.7b); then any
    chat-shaped name; then whatever is there. None = no brain."""
    if models is None:
        models = list_models()
    from .seats import load_seats
    picked = load_seats().get("brain")
    if picked:
        return picked
    wanted = os.environ.get("FORGE_BROWSER_CHAT_MODEL", "").strip()
    if wanted:
        return wanted
    lowered = [(name, name.lower()) for name in models]
    for parts in _CHAT_PREFERENCE:
        for name, low in lowered:
            if "embed" not in low and all(part in low for part in parts):
                return name
    for name, low in lowered:
        if "embed" not in low and any(hint in low for hint in _PREFERRED):
            return name
    return models[0] if models else None


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
SUBMIT_ACTIONS = {"remember", "forget"}


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
        return f"Run the \"{action.get('name', '')}\" workflow card"
    if kind == "remember":
        return f"Remember: \"{action.get('text', '')}\""
    if kind == "forget":
        return f"Forget anything about \"{action.get('text', '')}\""
    if kind == "recall":
        return "List what has been remembered"
    if kind == "recall_pages":
        return f"Search pages you visited for \"{action.get('query', '')}\""
    if kind == "brief":
        return "Build the morning brief from yesterday's pages"
    if kind == "compare":
        tabs = action.get("tabs") or []
        fields = action.get("fields") or []
        return (f"Compare tabs {', '.join(str(n) for n in tabs) or 'the open tabs'}"
                f" on {', '.join(fields) or 'price and key specs'}")
    return f"Run the {kind} action"


class LiveDisplay:
    """Incrementally filters streamed reply text for display.

    Holds partial lines until they end, and drops ACTION lines —
    the person sees the brain's words as they arrive, not the
    one-line action protocol. feed() returns the displayable
    text for a chunk; flush() returns any held remainder."""
    def __init__(self):
        self.buf = ""

    def feed(self, delta):
        self.buf += delta or ""
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if not line.strip().startswith("ACTION"):
                out.append(line + "\n")
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
        self.model = model if model is not None else pick_chat_model()
        self.history = []
        self._retrieve_cache = {}  # page hash -> (chunks, embeddings)
        self._tab_groups_cache = {}  # (title, url) tuple -> grouped tabs
        self._stop_requested = False  # set by cancel(); read between steps

    def cancel(self):
        """Stop the running turn at the next step boundary. A brain
        call already in flight finishes first — Stop lands between
        steps, never mid-word."""
        self._stop_requested = True

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
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                if on_chunk is None:
                    data = json.loads(response.read().decode("utf-8"))
                    return (data.get("message") or {}).get("content", "")
                text = []
                while True:
                    line = response.readline()
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
                        text.append(content)
                        on_chunk(content)
                return "".join(text)
        except Exception as exc:
            raise BrowserError(
                f"The chat brain ({self.model}) did not answer: {exc}") from exc

    def ask(self, user_text, page_text=None, on_action=None, on_chunk=None,
            tabs=None, page_url=None, on_plan=None, on_table=None,
            on_progress=None, on_research_done=None):
        """One chat turn. Returns (answer, urls_to_open_in_tabs).
        tabs: [{title, url, text, current}] in visible order, for @tabN.
        on_plan(plan_text) -> bool gates the work: the first action
        of the turn runs only when it approves, and submit actions
        (remember/forget) ask again each time. None = no gate
        (tests and programmatic use); the window and the CLI pass
        one. on_table(html) receives a comparison table to render
        in the window. on_progress(text) carries research progress;
        on_research_done(report, sources, error) delivers the
        finished report. cancel() stops the turn at the next step boundary."""
        self._stop_requested = False
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Memory: what the person asked to be remembered, when it is
        # relevant — never on a page that looks sensitive.
        from .memory import is_sensitive_url, search_entries
        if page_url is None and tabs:
            for tab in tabs:
                if tab.get("current"):
                    page_url = tab.get("url") or ""
                    break
        from .seats import seats_enabled
        if seats_enabled("memory") and not is_sensitive_url(page_url):
            from .retrieve import embed_texts
            found = search_entries(user_text, embed_fn=embed_texts)
            if found:
                lines = "\n".join(f"- {e['text']}" for e in found)
                messages.append({"role": "system",
                                 "content": "Things you asked me to remember:\n" + lines})
        # @tabN picks a tab; otherwise the current tab's text.
        chosen_text = page_text
        chosen_title = None
        if tabs:
            from .retrieve import embed_texts
            from .titles import group_items
            cache_key = tuple((t.get("title") or "", t.get("url") or "")
                              for t in tabs)
            groups = self._tab_groups_cache.get(cache_key)
            if groups is None:
                groups = group_items(tabs, embed_fn=embed_texts)
                self._tab_groups_cache = {cache_key: groups}
            pos = {id(tab): i + 1 for i, tab in enumerate(tabs)}
            lines = []
            for group in groups:
                if len(groups) > 1:
                    lines.append(f"Group: {group['name']}")
                for tab in group["items"]:
                    marker = " (current)" if tab.get("current") else ""
                    agent = "🤖 " if tab.get("agent") else ""
                    lines.append(f"{pos[id(tab)]}. {agent}{tab.get('title') or 'Untitled'} — "
                                 f"{tab.get('url') or ''}{marker}")
            tab_note = "Open tabs:\n" + "\n".join(lines)
            refs = re.findall(r"@tab(\d+)", user_text or "")
            if refs:
                n = int(refs[0])
                if 1 <= n <= len(tabs):
                    chosen_text = tabs[n - 1].get("text") or ""
                    chosen_title = tabs[n - 1].get("title") or f"tab {n}"
                else:
                    tab_note += (f"\n(The person mentioned @tab{n}, but "
                                 f"there are only {len(tabs)} tabs — "
                                 "say so plainly.)")
            messages.append({"role": "system", "content": tab_note})
        elif re.search(r"@tab\d+", user_text or ""):
            messages.append({"role": "system",
                             "content": "The person mentioned a tab (@tabN), "
                                        "but this chat has no tabs — say so plainly."})
        if chosen_text:
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
            reply = self._chat(messages, on_chunk)
            messages.append({"role": "assistant", "content": reply})
            action = parse_action(reply)
            if action is None:
                answer = visible_text(reply)
                break
            kind = action.get("type")
            # The plan gate: first action of the turn needs approval;
            # submit actions ask again each time, even when approved.
            if on_plan is not None:
                plan = describe_action(kind, action)
                if actions_run == 0:
                    if not on_plan(plan):
                        answer = "Okay — I didn't do that."
                        break
                elif kind in SUBMIT_ACTIONS:
                    if not on_plan(plan + " (this changes what I remember)"):
                        messages.append({"role": "user", "content":
                                         f"RESULT of {kind}:\n"
                                         "The person said no to that."})
                        continue
            if on_action:
                on_action(kind, action)
            actions_run += 1
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
                    from .research import research_job_running, start_research_job
                    topic = str(action.get("topic", "") or user_text)
                    running = research_job_running()
                    if running:
                        result = (f"A research job is already running on "
                                  f"{running} — wait for it to finish "
                                  "before starting another.")
                    else:
                        start_research_job(
                            topic, on_progress=on_progress,
                            on_done=(lambda report, sources, error=None: (
                                on_research_done(report, sources, error)
                                if on_research_done else None)))
                        result = (f"Research on {topic!r} started in the "
                                  "background — it runs while you browse, "
                                  "and the report opens when it is done. "
                                  "Expect minutes, not seconds.")
                elif kind == "read_page":
                    result = (chosen_text or "No page text is available "
                              "in this chat.")[:RESULT_CAP]
                elif kind == "summarize":
                    if chosen_text:
                        from .summarize import summarize_page
                        result = summarize_page(self, chosen_text)
                    else:
                        result = ("No page text is available in this chat — "
                                  "open a page in the window first.")
                elif kind == "card":
                    # A card runs its own workflow and answers directly —
                    # the turn ends here, not with another model step.
                    from .cards import run_card
                    answer = run_card(self, str(action.get("name", "")),
                                      question=user_text, page_text=chosen_text,
                                      on_action=on_action, on_chunk=on_chunk)
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
                elif kind == "recall":
                    from .memory import list_entries
                    entries = list_entries()
                    result = ("\n".join(f"- {e['text']}" for e in entries)
                              if entries else "Nothing remembered yet.")
                elif kind == "recall_pages":
                    from .memory import search_pages
                    query = str(action.get("query", "") or user_text)
                    found = search_pages(query)
                    if found:
                        result = "\n\n".join(
                            f"{e['title']} — {e['url']}\n{e['text']}"
                            for e in found)
                    else:
                        result = ("Nothing in the pages you visited "
                                  "matches that.")
                elif kind == "brief":
                    from .brief import brief_text, build_brief
                    result = brief_text(build_brief(self))
                elif kind == "compare":
                    from .compare import (DEFAULT_FIELDS, compare_table_html,
                                          compare_table_text, compare_tabs)
                    numbers = action.get("tabs") or [
                        int(n) for n in re.findall(r"@tab(\d+)", user_text or "")]
                    fields = [str(f) for f in (action.get("fields") or [])
                              if str(f).strip()] or DEFAULT_FIELDS
                    all_tabs = list(tabs or [])
                    chosen = [all_tabs[n - 1] for n in numbers
                              if isinstance(n, int) and 1 <= n <= len(all_tabs)]
                    if not chosen and all_tabs:
                        # "compare these three" with no numbers: the
                        # first three open tabs.
                        chosen = all_tabs[:3]
                    if not chosen:
                        result = "There are no tabs open to compare."
                    else:
                        rows = compare_tabs(self, chosen, fields)
                        if on_table:
                            on_table(compare_table_html(rows, fields))
                        result = compare_table_text(rows, fields)
                else:
                    result = f"Unknown action type: {kind}"
            except Exception as exc:
                result = f"That action failed: {exc}"
            messages.append({"role": "user",
                             "content": f"RESULT of {kind}:\n{result[:RESULT_CAP]}"})
        else:
            answer = visible_text(reply) or \
                "I ran out of steps on that one — try asking a smaller piece."
        self.history.append({"role": "user", "content": user_text})
        if answer:
            self.history.append({"role": "assistant", "content": answer})
        return answer, open_requests
