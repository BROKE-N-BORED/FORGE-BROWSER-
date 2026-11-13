# Forge Browser — Task List

**Source:** `docs/IMPROVEMENTS.md` (the full guide — models,
research, and the reasoning behind each item live there).
**Rule:** one item at a time. Joshua names the number, Arlo builds it, verifies
it, and only then moves on. Nothing is started unasked.
**Status marks:** `[ ]` not started · `[~]` in progress · `[x]` done, verified on
Joshua's real PC.
**Suite:** 493 checks, all green.

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
- [~] **#6 — Link previews [M].** Hover a link, get a 2-line summary of where
  it goes. On demand only — no prefetching (would thrash the CPU).
  *(Built 2026-11-11: new forge/browser/preview.py — destination
  title + first text lines (no model call), binary kinds named
  plainly, cached by URL (50 entries); window hovers via the
  page's linkHovered signal + 600ms dwell, popup by the cursor,
  hidden when the hover or the page moves on; `preview <url>` CLI.
  Suite 142/142; Qt offscreen smoke (real hover → "Forge Browser"
  preview of the home page). Real-PC verdict open.)*
- [~] **#7 — Cross-tab compare [M].** "Compare these three" → extract the same
  fields (price, specs) from each tab in separate tiny calls, render the table
  in the window. Never one giant prompt.
  *(Built 2026-11-11: new forge/browser/compare.py — extract_fields
  (one ask_once per tab, JSON with "not listed"/"couldn't extract"
  fallbacks), compare_tabs, Markdown + HTML table; `compare` chat
  ACTION (tab numbers, fields, first-three default); window opens
  the table in its own tab (new_tab html=). Also fixed in place:
  history + page memory now skip non-web URLs (about:blank/data:)
  so comparison tabs leave no junk behind. Suite 150/150; stub E2E;
  Qt offscreen smoke (real compare → Comparison tab, $42 vs $55).
  Real-PC verdict open.)*
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
- [~] **#11 — Research as a background job [S–M, UI only].** The research
  runner already works; give it a progress line and let it run while browsing
  continues. Strictly sequential. Expect minutes.
  *(Built 2026-11-11: research.start_research_job (one job at a
  time, second start refused; on_progress steps + on_done delivery),
  report_to_html; chat research ACTION starts the job and ends the
  turn at once (cards keep the synchronous runner); dock progress
  label + report tab on completion; CLI chat prints progress and
  the finished report. Suite 155/155; Qt offscreen smoke (chat free
  at once, Research tab opened, progress line said done). Real-PC
  verdict open.)*

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

- [~] **#14 — Model picker in the chat dock [S].** Brain / fast / vision seat
  dropdowns fed by `ollama list` instead of env vars. Plus Firefox-style AI
  controls: per-feature on/off and one master AI kill-switch.
  *(Built 2026-11-11: new forge/browser/seats.py (~/.forge/seats.json;
  saved pick beats env var beats auto); dock Brain/Fast/Vision
  dropdowns + ↻ refresh + AI master + Chat/Tidy titles/Memory toggles,
  all live (brain switches the session, fast drives tidy titles via a
  cached session, vision drives describe); master off kills chat,
  tidy, memory, summarize/brief/compare/research/voice; terminal
  `seats` subcommand mirrors the picker and terminal chat refuses
  while AI/Chat is off. Suite 163/163; stub E2E (seats CLI,
  kill-switch, picks driving brain + vision); Qt offscreen smoke
  (picker live, kill-switch disables Send, tidy toggle keeps raw
  titles). Real-PC verdict open.)*
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
- [~] **#16 — Chat progress honesty [S].** Extend the dock's "working: …" line
  with elapsed time and a cancel button per turn (pairs with #10's Stop).
  *(Built 2026-11-11: chat.ask gained on_step ("thinking" while the
  brain generates, the action kind while it runs); the dock's working
  line moved from log spam to a turn_status label ticking every second
  ("working: search… — 12s") and closing with "Done in 23s." /
  "Stopped after 9s." / "Failed after 5s."; #10's Stop button is the
  per-turn cancel, live for the whole turn; terminal chat prints step
  seconds + "(took Ns)". Suite 166/166; stub E2E (terminal "(took 2s)");
  Qt offscreen smoke (line ticked 0s→1s, Stop ended the turn,
  "Stopped after 8s."; rerun twice, 9s both times). Real-PC verdict open.)*

## G. Engine-room hardening [FIX] — defects in the shipped build

- [~] **#17 — [P1] Cap downloads.** Fetched files currently load fully into
  RAM with no size limit — a big file can OOM an 8 GB machine. Stream to disk
  with a cap. *Highest-risk open item in the analysis.*
  *(Built 2026-11-11: _http.py caps — 32 MiB in memory, 512 MiB streamed
  to disk, Content-Length refused from headers, cap counts decompressed
  bytes (gzip bombs refused), partial files removed; fetch_any streams
  to a temp file first and reads text back only under the memory cap.
  Suite 75/75 incl. 6 new cap tests. REVISED same day at Joshua's order
  ("downloads don't need a cap, my computer has plenty of storage"):
  the 512 MiB file cap is GONE — downloads stream to disk with no size
  limit, his disk is the only limit. The 32 MiB in-memory guard stays:
  that is RAM protection on an 8 GB machine, and it never refuses a
  download — a big text file is saved to disk instead of read.
  Real-PC verdict open.)*
- [~] **#18 — [E2] Markdown table separator row.** html2md tables currently
  aren't valid Markdown tables.
  *(Fixed 2026-11-11: html2md now emits `| --- | --- |` after the first
  emitted row of each table (first row = header, same shape as the CSV
  path in files.py); empty leading rows don't steal the separator; the
  counter resets per table. Suite 169/169 incl. 3 new table tests.
  Real-PC verdict open.)*
- [~] **#19 — [E3] Barrier detection on raw HTML.** Research scans extracted
  Markdown, so markup markers like password fields and captcha classes can
  never fire.
  *(Fixed 2026-11-11: fetch_any now runs detect_barriers on the raw HTML
  while it still has it and carries the result on FileResult.barriers
  (also in to_dict); research merges those with what the extracted text
  still shows, one entry per kind — a fetcher that never ran detection
  still gets the text fallback. Suite 172/172 incl. 3 new tests; live
  check: a markup-only gated page now reports captcha + login and the
  report carries both Note lines. Real-PC verdict open.)*
- [~] **#20 — [S3] Fetch address fence.** The fetcher will happily fetch
  127.0.0.1 / LAN addresses; add a fence with an explicit local-allow switch
  (the verify flow needs local pages; plain fetching doesn't).
  *(Built 2026-11-11: _http.check_fetch_url — refuses loopback, RFC 1918,
  link-local, localhost/*.local literals; DNS names deliberately NOT
  resolved (this sandbox's resolver answers example.org with a CGNAT
  address, so resolving would refuse ordinary browsing — documented);
  wired into http_get/http_get_to_file/fetch/fetch_any/render/snapshot/
  screenshot; switch = allow_local param, --allow-local on the CLI, or
  FORGE_BROWSER_ALLOW_LOCAL=1; a configured local SearXNG instance and
  the verify flow pass allow_local on their own; the window itself is
  not fenced. Suite 176/176 incl. 4 new FenceTests; CLI E2E (127.0.0.1
  refused, --allow-local opens it, 192.168.1.1 + localhost refused).
  Real-PC verdict open.)*
- [~] **#21 — [S2] Pin engine downloads.** Lightpanda/Chromium downloads are
  unpinned nightlies with no checksum. Pin versions + verify hashes.
  *(Built 2026-11-11: engine-pins.txt pins Lightpanda 1.0.3 with SHA-256s
  verified against the official release assets — sizes matched the release
  page — and Chromium revision 1741687. install.sh reads the pins, verifies
  Lightpanda with sha256sum before installing (mismatch refuses, keeps the
  old install), verifies the Chromium zip against the MD5 in its GCS object
  metadata plus a zip CRC check, and writes version records
  (lightpanda.version, chromium/REVISION) that `engines` reports; re-runs
  skip an engine whose installed version + hash already match. browser-
  setup.sh uses the same Lightpanda pin + verify. Suite 181/181.
  Real-PC verdict open.)*
- [~] **#22 — [E9] Download name collisions.** Colliding filenames silently
  overwrite — uniquify names.
  *(Built 2026-11-11: _keep_file now uniquifies a colliding name —
  report-1.pdf, report-2.pdf — via an atomic link call, so two downloads
  of the same name can never clobber each other. Test fetches the same
  image three times into a fresh media dir: pic.png, pic-1.png, pic-2.png
  all exist, no stray temp files. Suite 182/182. Real-PC verdict open.)*

## Do NOT build (researched, wrong for this machine/product)

- Generative mini-apps from tabs (Disco GenTabs, Neon Make) — frontier-model work.
- Parallel background agents — one slow model is the whole computer.
- Cloud work-app connectors (Gmail/Calendar/Slack) — sends personal data out.
- Conversational voice mode — streaming ASR + TTS this CPU can't carry.
- Autonomous shopping/booking — even the cloud vendors gate it as experimental.
