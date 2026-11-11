# Forge Browser (standalone — the AI browser build)

This tree is the FORGE BROWSER GitHub project: the standalone
browser program with the chat interface (an AI browser — you talk
to it, it browses). It builds on the v0.6 standalone program.

Forge Browser is a browser program, built 2026-11-11. It runs
STANDALONE first: its own folder, its own window, no Code Forge
required. The plan is for it to be baked into Code Forge later,
once it has proven itself on its own — the package layout
(forge/browser/) is kept compatible with that later step, but
nothing in this build needs, touches, or installs into Code Forge.

No API key, no account, and no paid service is used for any part
of it, search included. The engine room (search, fetch, research,
vault) is Python standard library only; the program's window is
Qt/PySide6, installed into its own venv by install.sh.

## Install (standalone)

Unzip the zip anywhere, for example `~/forge-browser/`. Open a
terminal in the unzipped folder, then:

```
sh install.sh     # checks Python, installs/updates the engines, builds the app venv, runs the test suite
sh run.sh         # opens Forge Browser — the program, its own window
```

`install.sh` is also the update path — run it again any time to
refresh everything and re-check. It installs two engines into
`~/.local/share/forge-browser/`: Lightpanda (the fast headless
reader) and a full Chromium build (~250 MB, one time) used by
the headless driving commands, and it builds `.venv` with
PySide6 + Qt WebEngine (the Chromium engine inside the program's
own window; WebEngine ships as its own PySide6-WebEngine package
in current PySide6). Nothing needs sudo.
`sh install.sh --check` reports without changing anything.

## The program

`sh run.sh` opens Forge Browser itself: a browser window with
tabs, back/forward/reload, and an address bar. Type an address
to go there; type words instead and Forge Browser searches with
its own keyless chain and shows the results on its own page. The
start page is Forge Browser's own too — served locally, showing
what this machine has (engines, seats, vault). Your logins and
cookies persist in Forge Browser's own profile under
`~/.forge/browser-profile/`.

The v0.5 `open` command (a plain Chromium window on port 9222
that other tools can drive) is still there for the engine room;
the program is the front of the house now.

`run.sh` passes any command straight through:
`sh run.sh search "..."`, `sh run.sh fetch URL`,
`sh run.sh snapshot URL`, `sh run.sh research "..." --yes`,
`sh run.sh demo` runs the terminal demo, and `sh run.sh test`
runs the full guided test below.

## Test it

```
sh test-forge-browser.sh
```

The script runs every feature once: the offline unit tests, the
engines report, a live search, a live fetch, a PDF fetch, a vault
round-trip in a throwaway vault, the research plan gate plus a
small live research run, and a rendered-page snapshot if a browser
engine is present. Local checks print PASS/FAIL; live steps print
their real output (marked SHOW) for you to judge. To give it an
engine first (optional — search/fetch/research/vault work without
one): `sh browser-setup.sh lightpanda`.


## Chat — the AI part

The window has a chat dock ("Chat — talk to your browser"). A
LOCAL Ollama model is the brain (automatic pick, or set
FORGE_BROWSER_CHAT_MODEL). It answers directly, or takes one
action at a time with the engine room: search (keyless), open a
page (which also opens it in a tab for you), research (the
plan-gated runner — asking in chat is the approval), or read_page
(the current tab's text is its context). At most 3 actions per
message, so a small model stays on rails; engine output is capped
before it reaches the model, sized for small context windows.
No Ollama answering = the dock says so and browsing still works.
Terminal version: `python3 -m forge.browser chat`.

## Commands

From the unzipped folder:

```
python3 -m forge.browser app [url]         # THE PROGRAM: Forge Browser's own window (needs the .venv from install.sh)
python3 -m forge.browser open [url]        # engine-room window: plain Chromium on port 9222, drivable
python3 -m forge.browser search "ollama local llm" [--max 10] [--json]
python3 -m forge.browser fetch https://example.org [--offset 12000] [--json]
python3 -m forge.browser fetch-any <url> [--json]
python3 -m forge.browser render <url> [--json]
python3 -m forge.browser snapshot <url> [--json]
python3 -m forge.browser screenshot <url> --out shot.png
python3 -m forge.browser describe photo.jpg
python3 -m forge.browser transcribe talk.mp3
python3 -m forge.browser research "topic" [--queries "a,b"] [--max-pages 6]
python3 -m forge.browser research "topic" --yes [--out report.md]
python3 -m forge.browser verify page.html --workspace /path/to/project
python3 -m forge.browser engines [--json]
python3 -m forge.browser vault create | list | add SITE | remove SITE
```

`--json` prints the same data the Python functions return, which is
the shape a Forge tool wrapper uses.

## Search

Keyless. Backends in order: a local SearXNG instance when
`FORGE_SEARXNG_URL` is set; otherwise DuckDuckGo Lite, then
DuckDuckGo HTML, then Bing HTML — the first backend that returns
results wins. All are public web pages read with a plain GET.
(DuckDuckGo's main endpoint bot-checks some networks; that is why
the chain exists.)

## Fetch — pages and every other file type

`fetch` reads a page into clean Markdown: structure kept, scripts /
styles / nav / footer removed, links kept with full URLs, long pages
paged via `--offset` so a small model can read them in slices.

`fetch-any` handles whatever a URL actually is:

- HTML → the same Markdown conversion
- PDF → text from the PDF's text layer (standard-library extractor).
  A scanned PDF has no text layer; that is reported, the file is
  saved, and nothing is invented.
- text / JSON / CSV / XML / code → text (JSON pretty-printed, CSV as
  a Markdown table)
- image → saved, then described by the vision seat when configured
- audio → saved, then transcribed when a backend is configured
- video / anything else → saved, with type and size reported

Files are saved under `~/.forge/browser-media/` (override:
`FORGE_BROWSER_MEDIA_DIR`).

## Rendered pages, snapshots, screenshots (Item 2)

`render`, `snapshot`, and `screenshot` drive a browser engine on
this machine over the Chrome DevTools Protocol — no cloud browser,
no key. Engine choice ("auto"): `FORGE_CDP_URL` if set, else
`FORGE_LIGHTPANDA_URL`, else a Chromium-family browser found on the
machine (chromium, google-chrome, brave, edge). The engine opens
ONE page on a throwaway profile and closes it after.

- `render` returns the page after JavaScript, as Markdown.
- `snapshot` returns the accessibility tree (role + name lines) and
  the interactive elements as a numbered list. A model acts by
  number (`BrowserSession.click(i)`, `type_text(i, ...)`,
  `fill_login(...)`) instead of writing selectors.
- `screenshot` saves a PNG, checked against the PNG signature
  before saving. Rendering quality is the engine's: Chromium is
  full-fidelity; the Lightpanda nightly tested 2026-11-11 returned
  a valid 1920×1080 PNG with basic, mostly unstyled layout and some
  missing font glyphs.

Sessions can use a PERSISTENT profile (`profile_dir=` in
`BrowserSession.start`, or the same folder reused): cookies and
logins survive, which is what makes the handoff below work.

## Multimodal seats

Media is turned into text by a local seat; the text is what models
read. An image is never attached to a model that cannot see images,
and nothing is described or transcribed by invention.

Vision seat — local Ollama (`FORGE_OLLAMA_URL`, default
http://127.0.0.1:11434). Model choice: `FORGE_VISION_MODEL` when
set, else the first installed of Forge's seat models
(eriktonon/bonsai-2-uncensored:2b — it carries a vision projector —
then dolphinlabs/qwen3.5-uncensored), else any installed model
Ollama reports as vision-capable, else common vision models
(moondream, llava, qwen2.5vl...). The seat prompt asks for a full
description plus all visible text transcribed.

Transcription — first working backend wins: `FORGE_TRANSCRIBE_CMD`
(a command template with {file} and {out_dir}), the `whisper` CLI,
whisper.cpp, or an installed faster-whisper package. Model:
`FORGE_WHISPER_MODEL` (default `base`, sized for CPU).

## Getting past site furniture and gates

Cookie/consent banners are dismissed by clicking their own dismiss
button (reject-all preferred when offered). Dialogs and lazy pages
are handled by the session (load wait + optional scroll pass).

Checks designed to prove a human is present (CAPTCHA, "verify you
are human") and logins with no vault entry are not fought: the
barrier is detected and named (`barriers.detect_barriers`), and the
page is handed to the person in a visible window
(`BrowserSession.start(..., headed=True, profile_dir=...)`). The
person clears it once; the persistent profile then carries the
session, so later visits are seamless. Paywalls and age gates are
likewise detected and reported.

## The vault — passwords and account information

`vault` stores site logins in one encrypted file
(`~/.forge/browser-vault.json`, override `FORGE_BROWSER_VAULT`):

- The whole file — sites, usernames, passwords, notes — is
  encrypted as one blob. Key: the master passphrase, stretched with
  PBKDF2-HMAC-SHA256 (600,000 rounds, random salt). Encryption:
  HMAC-SHA256 keystream with a fresh nonce per save, authenticated
  encrypt-then-MAC. A wrong passphrase or one changed byte fails
  the check and the vault will not open.
- The passphrase is prompted (never a command-line argument, never
  stored). For scripts, `FORGE_BROWSER_VAULT_PASSPHRASE` works and
  carries the obvious risk of living in the environment.
- The file is written 0600 in a 0700 folder. `vault list` shows
  site + username only — never passwords.
- Filling: `BrowserSession.fill_login` (or
  `vault.fill_from_vault`) types the credentials into the page and
  reports only how many fields were filled. Values never enter a
  model's context, a tool result, or a log.
- Scope, plainly: this protects the file at rest. It cannot protect
  against malware already running as you while the vault is open;
  no local vault can.

## Verify (the #65 contract)

`verify page.html --workspace DIR` serves the project file from a
throwaway 127.0.0.1 server (paths outside the project are refused),
returns the page text (rendered when an engine is available, plain
otherwise — the result says which), the accessibility snapshot
when rendered, and saves a screenshot under
`<project>/.forge/artifacts/` when the engine can take one. The
screenshot is a file path, never image data in the result.

## Research (Item 3)

Plan first, always: `research "topic"` prints the plan — the exact
searches and how many sources will be read — and searches nothing.
`--yes` approves and runs it: each query is searched, top sources
are read with `fetch-any` (pages, PDFs, other files), deduplicated,
numbered once, and written into a Markdown report — what each
source said under its citation number, barriers noted, full source
list at the end. The report contains only what the sources said;
it does not write findings of its own.

## Setup for the optional engines (Item 4)

```
sh browser-setup.sh lightpanda   # one binary into ~/.local/share/forge-browser/
sh browser-setup.sh searxng      # upstream git repo into a private venv
sh browser-setup.sh all
```

SearXNG is installed from its own git repo deliberately — the PyPI
package named "searxng" is not the upstream project. The script
prints the two `export` lines (FORGE_LIGHTPANDA_URL /
FORGE_SEARXNG_URL) to put in the shell once installed.
`python3 -m forge.browser engines` reports what this machine has.

## What is in the folder

`forge/browser/` (the package), `tests/` (50 checks),
`test-forge-browser.sh` (the guided test above), `install.sh`,
`run.sh`, `browser-setup.sh` (optional engines), this file, and a
sample screenshot from the sandbox's live Lightpanda test. When
the bake-in day comes, `forge/browser/` drops into Code Forge's
tree as-is — that later step changes nothing in this build.

## Tests

```
python3 -m unittest discover -s tests -p "test_browser*.py"
```

Verified in the sandbox on 2026-11-11: 50/50 pass (offline suite,
including a fake CDP server exercising the real WebSocket/CDP code).
The program itself was also run for real in the sandbox (offscreen
Qt): window opened on its start page, a live site loaded with its
real content, and words typed in the address bar came back as a
results page from the keyless chain.
Live checks the same day: real keyless search; live fetch; a live
research run producing a cited report; and a REAL engine session —
the Lightpanda nightly binary driven over CDP by this code
(navigate, rendered text, elements, accessibility snapshot, click,
and a valid screenshot of example.org). Chromium itself was not
available in the sandbox, so the Chromium launch path and headed
handoff are verified by the fake-server suite only until they run
on Joshua's PC. His run closes the items, as usual.

## Limits, plainly

- One engine session = one page. No parallel tabs in this build;
  that is deliberate on an 8 GB machine.
- Lightpanda is a beta engine: partial web APIs, rough rendering,
  and link-click navigation did not complete in the live test
  (the click dispatched; the page did not change). Chromium is the
  engine for interactive flows; Lightpanda is the fast reader.
- The PDF extractor reads text-layer PDFs (the common generated
  kind). Scanned PDFs need OCR, which is not in this build.
- Search engines can bot-check any network. The backend chain and
  the local SearXNG option are the answers built in.
