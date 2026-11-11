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
Then wait for the RESULT. When you have enough, answer plainly and briefly,
using only what the results actually said. If the user just chats, answer
directly with no ACTION line. Never invent page contents, prices, or facts.
Tabs are numbered from 1 in the order shown; when the person writes @tab2,
answer from that tab's text, which is handed to you as its context."""


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
    """Which local model drives the chat. Env wins; then the
    ordered preference (qwen3.5:4b → qwen3:4b → qwen3:1.7b);
    then any chat-shaped name; then whatever is there.
    None = no brain."""
    if models is None:
        models = list_models()
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
            tabs=None):
        """One chat turn. Returns (answer, urls_to_open_in_tabs).
        tabs: [{title, url, text, current}] in visible order, for @tabN."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
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
                    lines.append(f"{pos[id(tab)]}. {tab.get('title') or 'Untitled'} — "
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
        for _ in range(MAX_ACTIONS + 1):
            reply = self._chat(messages, on_chunk)
            messages.append({"role": "assistant", "content": reply})
            action = parse_action(reply)
            if action is None:
                answer = visible_text(reply)
                break
            kind = action.get("type")
            if on_action:
                on_action(kind, action)
            try:
                if kind == "search":
                    result = _run_search(str(action.get("query", "")))
                elif kind == "open":
                    url = str(action.get("url", ""))
                    result = _run_open(url)
                    if url:
                        open_requests.append(url)
                elif kind == "research":
                    result = _run_research(str(action.get("topic", "") or user_text))
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
