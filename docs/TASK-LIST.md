# Forge Browser — Task List

**Source:** `forge-browser-improvements-2026-11-11.md` (the full guide — models,
research, and the reasoning behind each item live there).
**Rule:** one item at a time. Joshua names the number, Arlo builds it, verifies
it, and only then moves on. Nothing is started unasked.
**Status marks:** `[ ]` not started · `[~]` in progress · `[x]` done, verified on
Joshua's real PC.

## Suggested build order (the menu)

1. **#1 + #2** — brain upgrade + streaming
2. **#17 (P1)** — the download cap
3. **#5** — summarize this page
4. **#3 + #4** — retrieval feeding + @tab
5. **#9** — Cards
6. Then by appetite: #8 grouping, #12 memory, #13 voice, #10 plan-run, #15 brief

## A. Make the brain smarter without making it bigger

- [~] **#1 — Default brain upgrade [S].** `pick_chat_model` preference order
  becomes qwen3.5:4b → qwen3:4b(-instruct) → qwen3:1.7b; thinking OFF and
  `num_ctx 8192` on every chat call. Highest value-per-line on the list.
  *(Built 2026-11-11: suite 55/55, stub-Ollama E2E confirmed the pick order
  and `think: false` + `num_ctx 8192` on the wire. Real-PC verdict open.)*
- [~] **#2 — Streamed answers [S].** Ollama streaming into the chat dock so
  words appear as they're generated instead of one long wait.
  *(Built 2026-11-11: `_chat` streams NDJSON when given on_chunk, `ask`
  passes it through, dock + terminal CLI show words live via a LiveDisplay
  filter that holds partial lines and drops ACTION lines. Suite 61/61;
  stub E2E proved incremental delivery (6 chunks over 1.80 s) + payload;
  offscreen Qt smoke drove the real dock — answer once, no ACTION leak.
  Real-PC verdict open.)*
- [~] **#3 — Chunk-and-retrieve page feeding [M].** Chunk the current page
  (200–400 tokens), embed with nomic-embed-text, retrieve only the chunks that
  match the question. What makes "chat with my tabs" actually work at 8k context.
  *(Built 2026-11-11: new forge/browser/retrieve.py — ~1200-char chunks
  with 200-char overlap, /api/embed via nomic-embed-text, cosine top-4,
  per-page embedding cache; short pages go in whole, no-embeddings
  degrades silently to the old truncated head. Suite 85/85; stub E2E
  found the price past the old 6000-char cut (4821 chars sent of 8766)
  and proved the no-embeddings fallback. Real-PC verdict open.)*
- [~] **#4 — @tab addressing [S, needs #3].** "What does @tab2 say about the
  price?" — let the person name tabs in chat.
  *(Built 2026-11-11 with #3: ask() takes tabs=[{title,url,text,current}],
  @tabN picks that tab's text (read_page/summarize follow it), the brain
  gets the numbered tab list; dock reads every tab's text on send (5 s
  guard so a silent tab can't hang it). Offscreen Qt smoke with two real
  tabs: "@tab2 what is the price?" answered from tab 2, once. Real-PC
  verdict open.)*

## B. Reading features

- [~] **#5 — Summarize this page [S–M].** One click / one chat phrase,
  map-reduce in chunks for long pages, cached by URL + content hash.
  *(Built 2026-11-11: new forge/browser/summarize.py — ~1500-char chunks,
  map then combine, cache at ~/.forge/summaries.json keyed by URL+text
  hash (50 entries); chat ACTION "summarize" + dock Summarize button,
  both streaming live. Suite 75/75; stub E2E (6 maps + 1 reduce, cache
  hit = 0 calls, changed text re-summarizes); offscreen Qt smoke drove
  the real button — summary once in the log. Real-PC verdict open.)*
- [ ] **#6 — Link previews [M].** Hover a link, get a 2-line summary of where
  it goes. On demand only — no prefetching (would thrash the CPU).
- [ ] **#7 — Cross-tab compare [M].** "Compare these three" → extract the same
  fields (price, specs) from each tab in separate tiny calls, render the table
  in the window. Never one giant prompt.
- [~] **#8 — Tidy titles + tab grouping [M].** Fast seat renames messy titles;
  tabs grouped by topic using embeddings; history grouped into resumable
  "journeys".
  *(Built 2026-11-11: new forge/browser/titles.py — tidy_title (brain,
  2–5 words, raw fallback), group_items (embedding cosine ≥0.75, domain
  fallback), history at ~/.forge/history.json (deduped, 200 entries);
  tabs tidy in the background on titleChanged (URL-cached, local pages
  skipped); chat's @tab list shows embedding groups; start page shows a
  Recent section grouped by site. Suite 102/102; stub E2E (tidy + grouped
  tab note); offscreen Qt smoke — long title became "Gears Guide" in the
  real tab bar. Real-PC verdict open.)*

## C. Doing features (scoped hard, on purpose)

- [~] **#9 — Cards: saved workflows [M].** Plain-text prompt+action templates
  in a folder ("morning prices check", "pull the specs table"), invoked by name
  in chat. Ship 5–10 hand-tested ones.
  *(Built 2026-11-11: new forge/browser/cards.py — `# Title` + prompt +
  `## Actions` ACTION lines, {question}/{page} placeholders; built-ins in
  forge/browser/cards/, yours in ~/.forge/browser-cards/ (yours win on a
  name clash); chat ACTION {"type": "card"} runs the workflow and answers
  directly; CLI `cards` lists, `card <name> [question]` runs. 6 shipped:
  summarize-page, tl-dr, key-facts, explain-like-im-new, find-price,
  research-topic — all 6 run against a stub brain in E2E. Suite 95/95.
  Real-PC verdict open.)*
- [~] **#10 — Plan → Run → Interrupt [L].** Brain shows its step plan first;
  person approves; each step runs visibly in a marked tab; a Stop button
  actually stops it. Confirm before anything that submits (purchase, post, send).
  *(Built 2026-11-11: chat.ask on_plan gate — first action of a
  turn needs approval, remember/forget re-ask every time, plain-word
  describe_action; ChatSession.cancel() stops the turn between
  steps; dock dialog (worker waits on an event) + "Plan:" log line +
  Stop button (enabled only mid-turn); tabs the brain opens are
  marked 🤖 in the tab bar and in the chat's tab list. Suite 128/128;
  Qt offscreen smoke (plan dialog approved, page in a 🤖 tab).
  Real-PC verdict open.)*
- [ ] **#11 — Research as a background job [S–M, UI only].** The research
  runner already works; give it a progress line and let it run while browsing
  continues. Strictly sequential. Expect minutes.

## D. Memory

- [~] **#12 — Local memory store [M–L].** Visited pages chunked + embedded
  into a local file store; chat can answer "that thing I read last week…".
  Plus a short interests file the person can read, edit, and delete. Off for
  sensitive sites; nothing ever leaves the disk.
  *(Built 2026-11-11: new forge/browser/memory.py — facts store at
  ~/.forge/memory.json (remember/forget/recall, dedup, embedding
  search with keyword fallback, JSON export, sensitive-URL
  withholding); page store at ~/.forge/page-memory.json (chunks kept
  as TEXT, embedded only on explicit recall — embedding at visit
  time would keep evicting the chat brain; recall_pages ACTION +
  `memory pages` CLI). Suite 115/115; stub E2E (facts + pages + CLI).
  Real-PC verdict open.)*

## E. Voice

- [~] **#13 — Push-to-talk [M].** Hold a button (or a key), speak the command,
  whisper.cpp base.en transcribes, text lands in the chat box.
  Record-then-transcribe; no wake word, no always-listening.
  *(Built 2026-11-11: new forge/browser/voice.py — Recorder over
  arecord/parecord/ffmpeg (16 kHz mono WAV, killed on release,
  empty recordings refused); dock "🎤 Hold to talk" button (button
  reads "● Listening…" while held, text lands in the entry for
  review, never auto-sent); `voice [seconds]` CLI. Suite 122/122;
  Qt offscreen smoke (fake arecord + stub transcriber: hold →
  "hello from the mic" in the entry). Real-PC verdict open.)*

## F. The window itself

- [ ] **#14 — Model picker in the chat dock [S].** Brain / fast / vision seat
  dropdowns fed by `ollama list` instead of env vars. Plus Firefox-style AI
  controls: per-feature on/off and one master AI kill-switch.
- [~] **#15 — Morning brief card [M, needs #12].** On first launch of the day
  (or on demand): a card on the start page summarizing yesterday's browsing
  from the local memory store.
  *(Built 2026-11-11: new forge/browser/brief.py — yesterday's
  pages from the page store + history, brain-written summary via
  ChatSession.ask_once, cached once a day at ~/.forge/brief-<date>.json
  (a failed brain call is NOT cached); start page shows the cached
  brief or a plain "no brief yet" note; `brief` chat ACTION + `brief`
  CLI; background build on launch. Suite 136/136; stub E2E (CLI
  brief built, cached, reused). Real-PC verdict open.)*
- [ ] **#16 — Chat progress honesty [S].** Extend the dock's "working: …" line
  with elapsed time and a cancel button per turn (pairs with #10's Stop).

## G. Engine-room hardening [FIX] — defects in the shipped build

- [~] **#17 — [P1] Cap downloads.** Fetched files currently load fully into
  RAM with no size limit — a big file can OOM an 8 GB machine. Stream to disk
  with a cap. *Highest-risk open item in the analysis.*
  *(Built 2026-11-11: _http.py caps — 32 MiB in memory, 512 MiB streamed
  to disk, Content-Length refused from headers, cap counts decompressed
  bytes (gzip bombs refused), partial files removed; fetch_any streams
  to a temp file first and reads text back only under the memory cap.
  Suite 75/75 incl. 6 new cap tests. Real-PC verdict open.)*
- [ ] **#18 — [E2] Markdown table separator row.** html2md tables currently
  aren't valid Markdown tables.
- [ ] **#19 — [E3] Barrier detection on raw HTML.** Research scans extracted
  Markdown, so markup markers like password fields and captcha classes can
  never fire.
- [ ] **#20 — [S3] Fetch address fence.** The fetcher will happily fetch
  127.0.0.1 / LAN addresses; add a fence with an explicit local-allow switch
  (the verify flow needs local pages; plain fetching doesn't).
- [ ] **#21 — [S2] Pin engine downloads.** Lightpanda/Chromium downloads are
  unpinned nightlies with no checksum. Pin versions + verify hashes.
- [ ] **#22 — [E9] Download name collisions.** Colliding filenames silently
  overwrite — uniquify names.

## Do NOT build (researched, wrong for this machine/product)

- Generative mini-apps from tabs (Disco GenTabs, Neon Make) — frontier-model work.
- Parallel background agents — one slow model is the whole computer.
- Cloud work-app connectors (Gmail/Calendar/Slack) — sends personal data out.
- Conversational voice mode — streaming ASR + TTS this CPU can't carry.
- Autonomous shopping/booking — even the cloud vendors gate it as experimental.
