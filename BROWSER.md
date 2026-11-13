# Forge Browser (standalone — the AI browser build)

This tree is the FORGE BROWSER GitHub project: the standalone
browser program with the chat interface (an AI browser — you talk
to it, it browses). It is the current build: the Qt window plus
the full engine room, with all 22 improvements from the task list.

Forge Browser is a browser program, built 2026-11-11. It runs
STANDALONE: its own folder, its own window, no Code Forge
required. The engine room is also wired into Code Forge as a
tool, but this build stands on its own — the package layout
(forge/browser/) is kept compatible with that wiring, and
nothing in this build needs, touches, or installs into Code Forge.

No API key, no account, and no paid service is used for any part
of it, search included. The engine room (search, fetch, research,
vault) is Python standard library only; the program's window is
Qt/PySide6, installed into its own venv by install.sh.

## Install (standalone)

**No terminal:** unzip the release, then double-click **Install
Forge Browser**. It installs the program under
`~/.local/share/forge-browser-app`, adds Forge Browser to the app
menu with its icon, and offers to open it. Re-running it updates
the program. (If the file manager asks what to do, choose Run.)

**Terminal:** unzip the zip anywhere, for example
`~/forge-browser/`. Open a terminal in the unzipped folder, then:

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

The `open` command (a plain Chromium window on port 9222
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
plan-gated runner — asking in chat is the approval), read_page
(the current tab's text is its context), or summarize (the current
page, map-reduce in ~1500-char chunks so a long page still fits a
small model; cached by URL + content hash under
~/.forge/summaries.json). At most 3 actions per message, so a small
model stays on rails; engine output is capped before it reaches
the model, sized for small context windows. The dock also has a
Summarize button: one click summarizes the current tab, streamed
live into the log. No Ollama answering = the dock says so and
browsing still works. Terminal version: `python3 -m forge.browser chat`.

## Model picker + AI controls

The chat dock has three seat dropdowns — Brain, Fast, Vision —
fed by `ollama list`, plus a master AI switch and one toggle per
feature (Chat, Tidy titles, Memory). Picks live in
`~/.forge/seats.json` and apply at once; a pick beats the env
var (`FORGE_BROWSER_CHAT_MODEL`, `FORGE_VISION_MODEL`), which
beats the automatic choice. "(auto)" means no pick saved.

- Brain: the model the dock chats with. Picking one switches
  the session live; the header shows what is driving the chat.
- Fast: the model that writes short tab titles. None (the
  default) means the brain does it — no extra model swap on a
  one-model machine.
- Vision: the model that describes images. Used by `describe`
  and by fetch-any on image files.
- AI off: every AI feature stops — chat, tidy titles, memory
  context and page recording, summarize, brief, compare,
  research, voice, and image describing. Flipping it off
  mid-turn stops the turn in progress, not just the next one.
  The browser itself keeps working.
- Chat off: the dock is quiet, but tidy titles and memory keep
  running. Tidy titles off: tabs keep their raw titles.
  Memory off: remembered facts are never offered as context
  and pages are not recorded for recall.
- The morning brief and the terminal `voice` command are gated
  on Chat, like the dock mic. Image describing is gated on the
  master AI switch only — Chat off does not kill it. The
  terminal `transcribe` command is a file utility, not an AI
  feature, and is not gated.

The picker shows each model's size next to its name, and the
Brain combo's "(auto)" entry names the model auto would pick.
Embedding models are not offered as Brain or Fast. A saved pick
for a model that is no longer installed shows as "(not
installed)" instead of hiding behind "(auto)".

The terminal has the same controls: `seats` lists the picks
and toggles; `seats brain qwen3:4b`, `seats brain auto`,
`seats ai off`, `seats tidy_titles off`. Terminal chat refuses
to start while AI or Chat is off.

## Chat progress honesty

While a turn runs, the dock shows a working line under the log —
"working: search… — 12s" — naming the step (thinking, search,
open, …) and ticking once a second. The Stop button is live for
the whole turn; hitting it stops the turn at the next step
boundary, and the line then says how long it took: "Stopped
after 9s." A finished turn says "Done in 23s."; a failed one
says "Failed after 5s." Terminal chat prints the same honesty:
each step line carries its seconds, and the turn ends with
"(took 23s)".

## Fetch address fence

Fetch, fetch-any, render, snapshot, and screenshot refuse local
addresses by default: loopback (127.0.0.0/8, ::1), the LAN ranges
(10/8, 172.16/12, 192.168/16), link-local, and localhost /
*.local names. A page or a prompt can no longer steer the browser
at the router admin page, Ollama on 127.0.0.1:11434, or a file
server on the LAN. The refusal says so in plain words and names
the switch.

To fetch a local address on purpose: `--allow-local` on the
command line, `allow_local=True` in code, or
`FORGE_BROWSER_ALLOW_LOCAL=1`. A locally hosted SearXNG instance
you configured (FORGE_SEARXNG_URL) is allowed the same way — the
configuration is the switch. The verify flow for project pages
allows its own throwaway local server on its own. The browser
window itself is not fenced, by design: the person driving it can
open anything, including the start page it serves locally. The
fence guards the fetches the browser makes on a page's or a
prompt's say-so, where the person is not choosing the address.

The env switch is all-or-nothing for the whole process:
`FORGE_BROWSER_ALLOW_LOCAL=1` opens local fetching for every
fetch, including brain-triggered ones. The scoped alternative is
`allow_local=True` on one call.

IP literals in non-dotted forms (2130706433, 0x7f.0.0.1,
0177.0.0.1, 127.1) are normalized before the check — the socket
layer accepts them, so an un-normalized fence would wave them
through. Redirects are re-fenced on every hop: a public URL that
302s to a local address is refused, not followed. DNS names are
resolved and refused when they point at the local machine or the
LAN; CGNAT (100.64/10) is allowed, because a public name can
resolve there behind a proxy and CGNAT is ISP space, not this
machine. One window remains: the fetch re-resolves after the
fence's check, so a hostile DNS could in principle flip the
answer between the two lookups (in practice the resolver usually
caches the first answer); pinning the connection to the checked
address would close that. Every refusal is recorded (host +
reason) in `~/.forge/fence-log.json`, so steering attempts are
visible afterwards.

## Engine downloads are pinned + verified

The engine binaries install.sh fetches are pinned to exact
versions, not moving nightlies, and every download is verified
before it is installed. The pins live in `engine-pins.txt`:

- Lightpanda **1.0.3** (latest stable, 2026-11-05), x86_64 and
  aarch64, each with a SHA-256 of the exact release bytes.
  `browser-setup.sh` uses the same pin.
- Chromium snapshot revision **1741687** (Chromium 160.0.8156.0),
  pinned for the window, with the zip's SHA-256 and the
  extracted chrome binary's SHA-256.

Lightpanda is checked with `sha256sum` against the pinned hash;
a mismatch refuses the install and keeps whatever was there
before. The Chromium zip is checked the same way — against the
SHA-256 recorded in `engine-pins.txt`, plus a zip CRC check
before extracting. Google publishes no SHA-256 for snapshots,
so the recorded hashes were computed once from a download
verified against the GCS metadata (size + MD5, both matched);
honest limit: a hash computed from a download proves the bytes
match what was recorded, not that Google published them — what
the pin catches is any later tamper or corruption. The installed
chrome binary is re-verified by hash on every run, like
Lightpanda; a corrupted or tampered binary fails even if the
REVISION file says the right number. Chromium extracts to a
temp dir and swaps into place, so an interrupted install never
leaves a half-old / half-new live tree, and only the known
executables (chrome, chrome-wrapper, chrome_crashpad_handler,
chrome_sandbox) get +x. Every download retries transient
failures and resumes a broken download instead of restarting
from zero; a download failure keeps the partial file for the
next run, a verification failure removes it. Install writes a
version record next to each engine (`lightpanda.version`,
`chromium/REVISION`), and `engines` reports the installed
version / revision. A re-run skips any engine whose installed
version and hash already match the pin. (PySide6 is pinned in
`pyside-requirements.txt`, not here; in-code download resume
lives in `_http.http_get_to_file`.)

To bump a pin: update the version AND the hash in
`engine-pins.txt` together — download the release, check its
size against the release page, sha256sum it, and record the hash.

## Kitesurf-local: a third engine

Kitesurf is Cloudflare's browser engine for AI agents, open
source under Apache 2.0 (github.com/cloudflare/kitesurf). It
parses HTML, applies CSS, runs page JavaScript, and renders
screenshots without Chrome or a browser container — Rust
compiled to WebAssembly (Blitz layout, Stylo CSS, Boa JS),
running inside a Workers isolate. Forge Browser treats it as a
third engine behind the same seam as Chromium and Lightpanda,
for read, screenshot, and extract jobs: the niche where
Chromium is heavy on an 8 GB machine and Lightpanda cannot run
JavaScript.

It speaks the Chrome DevTools Protocol, so the existing session
code drives it unchanged — `/json/version` discovery, then the
browser WebSocket. Pick it with `--engine kitesurf` on `render`,
`snapshot`, `screenshot`, and `verify`, or point
`FORGE_KITESURF_URL` at a running one (it also joins the `auto`
chain, after FORGE_CDP_URL and FORGE_LIGHTPANDA_URL). `engines`
reports whether a Kitesurf-local is serving; the endpoint only
counts when its /json/version names itself Kitesurf.

Local-only, by design. Kitesurf-local is a dev server you start
by hand — it is not installed by install.sh and Forge Browser
never launches it:

```sh
git clone https://github.com/cloudflare/kitesurf.git
cd kitesurf
pnpm install --frozen-lockfile
pnpm run dev        # builds the engine, starts wrangler dev
```

The engine listens on `http://127.0.0.1:8787`
(`http://127.0.0.1:8787/health` confirms it is up;
`KITESURF_PORT` moves it). It needs Bazelisk installed as
`bazel`, Node.js 22 or newer, and pnpm 9.15.4 (Bazel downloads
its pinned Rust toolchain itself; rustup is only needed for
editing the vendored crate patches). The first build compiles
the Rust core and its vendored crates, which takes a while.
There is no cloud path here on purpose: running Kitesurf on
Cloudflare needs Dynamic Workers, which are part of the Workers
Paid plan — that breaks the $0 rule. Kitesurf has no built-in
authentication either, so a deployed instance would be public
without an auth layer in front of it; the local dev server only
listens on 127.0.0.1.

Honest build verdict (2026-11-13): the engine itself could NOT
be built in the sandbox. Bazel could not fetch its module
registry there at all — with the sandbox proxy set, Bazel's
Java downloader crashed on the proxy's CONNECT handshake; with
the proxy unset, direct TLS to bcr.bazel.build failed (the
sandbox egress answers it with plain HTTP). The toolchain up to
that point worked (pnpm 9.15.4 and Bazelisk installed cleanly,
`pnpm install --frozen-lockfile` finished in about a minute),
and the adapter was built and tested against a fake CDP server
speaking Kitesurf's documented protocol — but no live Kitesurf
engine has been driven yet. On the real PC the build should be
attempted with normal home internet (no proxy): the first build
is the heavy part on a 2-core machine with a hard drive, while
the running engine is light (Cloudflare claims 3–7x less CPU
and memory than Chromium). Kitesurf is still experimental and
under heavy development; some CDP surface is missing (its own
README says some DevTools features do not work yet), so a page
that needs a capability it lacks fails in plain words, like any
other engine gap.

## Every verification starts fresh

A verification never reuses browser state from an earlier run.
Before v0.8, verify attached to an already-running engine
whenever one was configured, and its page landed in that
engine's shared default context — cookies and storage from
earlier runs could leak in (cookies are host-scoped, so the
throwaway server's fresh port did not isolate them). Now a
verification launches a fresh engine whenever it can (a
throwaway profile for Chromium, a fresh process for Lightpanda)
and, when the only engine is one already running, opens its
page in its own isolated browser context and throws the context
away when the run ends. An engine that cannot isolate a context
refuses in plain words instead of sharing state. The plain
read, screenshot, and snapshot paths are unchanged — they attach
to the engine you point them at, because that is what they are
for; only verification forces fresh.

## Research as a background job

Ask for research in chat and the turn ends at once — the job
runs behind your browsing, strictly one at a time, with a
progress line in the dock ("Reading 2 of 6: …"). Expect minutes,
not seconds. When it finishes, the report opens in its own tab
and the dock says so. If a job is already running, the next ask
is told to wait. The terminal `research` command still runs a
plan synchronously, the way it always has.

## Cross-tab compare

Say "compare these three" (or "compare @tab1 @tab2 on price") and
the brain pulls the same fields out of each tab — one small call
per tab, never one giant prompt — and opens a Comparison tab with
the table in the window. A field a page doesn't list reads "not
listed"; a reply the brain couldn't use reads "couldn't extract".
The brain also gets the table as text, so it can talk about it.

## Link previews

Rest on a link for a moment and a small box by the cursor shows
two lines about where it goes — the destination's title plus the
first lines of its own text. Nothing is fetched on hover alone
and nothing is prefetched; the fetch happens only after the short
dwell, and the result is cached by URL so a second hover costs
nothing. Images, audio, and files say what they are instead.
Terminal version: `python3 -m forge.browser preview <url>`.

## Morning brief

On first launch of the day the browser builds, in the background,
a short card about what yesterday's browsing was about — from the
local memory store, cached at `~/.forge/brief-YYYY-MM-DD.json`
(yesterday's date) and shown on the start page the next time it
loads. The brain writes the summary when one is answering;
otherwise the card is the plain list of pages. A day with no
pages says so, and a failed brain call is not cached — the next
launch retries. A launch with no brain still caches the plain
list so the card shows at once, but the summary fills in later:
a cached summary-less brief is rebuilt as soon as a brain is
available. The summary samples 12 pages spread across the day
(600 chars each), and sensitive pages — banking, logins,
checkout — never reach the brief, list or summary. Old brief
files are pruned to the last 30 days. On demand: ask "give me
my morning brief" in chat (a cached brief answers at once; an
uncached one builds in the background and lands in the log when
it is done), or `python3 -m forge.browser brief` in the
terminal. The start page lists every page in the brief, has a
"Refresh brief ↻" link that rebuilds it on demand, and shows
"Today so far" — today's pages, listed live — alongside
yesterday. `python3 -m forge.browser brief today` briefs today
so far with a summary instead.

## Plan, run, interrupt

The brain never acts on its own. When it wants to do something,
it shows the plan first — "Search the web for gears", "Open
example.org in a new tab" — and waits for your yes in a dialog
(the terminal asks on its own line). Anything that changes what
is remembered asks again, even inside an approved turn. Pages
the brain opens land in tabs marked 🤖, so you can see at a
glance which tabs it opened and which are yours. The Stop button
ends the turn at the next step — a brain call already in flight
finishes first, then it stops, and nothing after it runs.

## Voice — hold to talk

The chat dock has a "🎤 Hold to talk" button. Hold it, speak,
release: the recording is transcribed on this machine and the
text lands in the chat box for you to read and send — never sent
on its own. While recording, the button shows the elapsed time
and whether audio is arriving; recording stops itself at the
2-minute cap. Recording needs a system recorder: `arecord`,
`parecord`, or `ffmpeg` (first working one wins — if the first
dies at once, the next is tried). Pin one with
`FORGE_BROWSER_RECORDER` and pick a microphone with
`FORGE_BROWSER_MIC_DEVICE` (e.g. `hw:1,0`). Terminal version:
`python3 -m forge.browser voice [seconds]` (default 5, capped
at 120).

Transcription backends, in order: `FORGE_TRANSCRIBE_CMD` (a
command template with `{file}`/`{path}` and `{out_dir}`), the
`whisper` CLI, whisper.cpp (`whisper-cli`/`main`), faster-whisper
(local files only — it never downloads a model unannounced).
whisper.cpp gets the model file passed explicitly:
`FORGE_WHISPER_MODEL_PATH`, default `~/.forge/whisper/base.en`
(the English-only 148 MB one). Fetch it with
`sh install.sh --with-whisper` (opt-in); the whisper.cpp binary
itself comes from https://github.com/ggerganov/whisper.cpp
(build it, or take a release binary) and goes on PATH. A failed
transcription says which kind it was: no transcriber installed,
the transcriber errored, or nothing was heard.

## Memory — what it remembers

Say "remember ..." in chat (or `python3 -m forge.browser memory add
"...") and the browser keeps that fact in `~/.forge/memory.json` —
plain JSON you can read, edit, and export (`memory export
path.json`). It offers the relevant ones back as context when you
ask something they bear on, and never records anything on its own:
no page contents, ever. On pages that look sensitive (banking,
login, checkout, health) it withholds memory context entirely.
Chat actions: `remember` / `forget` / `recall`. CLI: `memory`
(list), `memory search <words>`, `memory forget <words>`.

Pages you visit are kept too, as text chunks in
`~/.forge/page-memory.json` (never on sensitive pages, never on
the browser's own local pages). Ask "that thing I read about
gears" and the brain searches them (`recall_pages` action; CLI:
`memory pages <words>`). Chunks are stored as text and embedded
only when you ask — embedding at visit time would keep evicting
the chat brain from the one-model slot.

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
`FORGE_BROWSER_MEDIA_DIR`). A name that is already taken gets
uniquified — report-1.pdf, report-2.pdf — instead of overwriting
the file already there.

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
whisper.cpp, or an installed faster-whisper package (local files
only). Model: `FORGE_WHISPER_MODEL` (default `base`, sized for
CPU); whisper.cpp reads the model file at
`FORGE_WHISPER_MODEL_PATH`, default `~/.forge/whisper/base.en` —
fetch it with `sh install.sh --with-whisper`.

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

`forge/browser/` (the package), `tests/` (493 checks),
`test-forge-browser.sh` (the guided test above), `install.sh`,
`run.sh`, `browser-setup.sh` (optional engines), and this file.
The engine room is also wired into Code Forge as a tool; that
wiring changes nothing in this build.

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
