# FORGE BROWSER

An AI browser. You talk to it in plain language — it browses for you.

Forge Browser is a standalone browser program with its own window:
tabs, an address bar, and a **chat panel** where a local AI model
drives the browsing. Ask it to find something, open a page, read
what's on your screen, or research a question — it searches with a
keyless search chain, opens real pages in real tabs, and answers
from what it actually found.

**No API keys. No accounts. No paid services.** The chat brain is a
local [Ollama](https://ollama.com) model running on your own machine.
Search is keyless. Nothing is tracked.

## Install it (no terminal)

Unzip the release, then double-click **Install Forge Browser**.
It installs the program, puts its icon in your app menu, and
offers to open it. No terminal, no commands. (If your file
manager asks what to do with it, choose Run.)

## Run it (terminal)

```sh
sh install.sh   # engines + the app venv (PySide6), one time
sh run.sh       # opens Forge Browser
```

- Type an **address** in the address bar to go there.
- Type **words** in the address bar to search (keyless chain).
- Type in the **chat panel** to talk to the browser: "open
  example.org", "search for small block chevy heads", "what does
  this page say", "research growing tomatoes in zone 7".

Pick the chat brain with `FORGE_BROWSER_CHAT_MODEL` if you don't
like the automatic pick (any small chat model you have in Ollama).

## Under the hood

The engine room is a Python standard-library package
(`forge/browser/`): keyless search, fetch of any file type
(HTML→Markdown, PDF text, CSV, JSON…), a CDP-driven rendering
engine (Lightpanda / Chromium), image description and audio
transcription via local seats, an encrypted vault for site
passwords, barrier handling (consent banners dismissed; CAPTCHAs
are handed to the human, never solved), and a plan-gated research
runner that produces cited reports. The chat brain
(`forge/browser/chat.py`) drives that engine room one action at
a time; the window (`forge/browser/app.py`) is Qt + WebEngine.

The same engine room is also wired into Code Forge as a tool.
This project is the standalone AI browser built on top of it.

Full manual: [BROWSER.md](BROWSER.md)

## Tests

```sh
python3 -m unittest discover -s tests -p "test_browser*.py"
```

184 checks, offline, including the chat protocol with a stubbed
brain. The guided tour: `sh test-forge-browser.sh`.
