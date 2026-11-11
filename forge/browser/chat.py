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

MAX_ACTIONS = 3          # per user message — keeps small models on rails
RESULT_CAP = 4500        # chars of engine output handed to the model
PAGE_CAP = 6000          # chars of the current page handed to the model
HISTORY_CAP = 12         # messages kept (plus the system prompt)

_PREFERRED = ("qwen", "llama", "gemma", "phi", "mistral", "minicpm",
              "deepseek", "smol", "tinyllama")

SYSTEM_PROMPT = """You are the brain inside Forge Browser, a web browser the user talks to.
You can browse for real. To do something, reply with ONE line exactly like:
ACTION {"type": "search", "query": "words to search"}
ACTION {"type": "open", "url": "https://example.org/page"}
ACTION {"type": "research", "topic": "a bigger question"}
ACTION {"type": "read_page"}
Then wait for the RESULT. When you have enough, answer plainly and briefly,
using only what the results actually said. If the user just chats, answer
directly with no ACTION line. Never invent page contents, prices, or facts."""


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
    """Which local model drives the chat. Env wins; then a
    chat-shaped name; then whatever is there. None = no brain."""
    if models is None:
        models = list_models()
    wanted = os.environ.get("FORGE_BROWSER_CHAT_MODEL", "").strip()
    if wanted:
        return wanted
    for name in models:
        low = name.lower()
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

    def _chat(self, messages):
        if not self.model:
            raise BrowserError(
                "No chat brain: no local model is answering at "
                f"{OLLAMA_URL}. Start Ollama (and pull a small chat "
                "model), then try again — browsing itself still works.")
        payload = json.dumps({"model": self.model, "messages": messages,
                              "stream": False}).encode("utf-8")
        request = urllib.request.Request(
            OLLAMA_URL + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise BrowserError(
                f"The chat brain ({self.model}) did not answer: {exc}") from exc
        return (data.get("message") or {}).get("content", "")

    def ask(self, user_text, page_text=None, on_action=None):
        """One chat turn. Returns (answer, urls_to_open_in_tabs)."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if page_text:
            messages.append({"role": "system",
                             "content": "Text of the page the user is "
                                        f"looking at:\n{page_text[:PAGE_CAP]}"})
        messages.extend(self.history[-HISTORY_CAP:])
        messages.append({"role": "user", "content": user_text})
        open_requests = []
        answer = ""
        for _ in range(MAX_ACTIONS + 1):
            reply = self._chat(messages)
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
                    result = (page_text or "No page text is available "
                              "in this chat.")[:RESULT_CAP]
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
