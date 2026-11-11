# Forge Browser — Improvements, Enhancements & Model Guide

**Date:** 2026-11-11 · **Project:** FORGE BROWSER (the AI browser — standalone,
GitHub: BROKE-N-BORED/FORGE-BROWSER-) · **Status:** suggestions for Joshua to
pick from, one at a time. Nothing here is built until he names it.

**The machine every suggestion is fitted to:** Ryzen 3 3250U (2 cores /
4 threads, AVX2 only), 8 GB RAM total, CPU-only, spinning HDD, Linux Mint.
**The rules:** $0, no API keys, no accounts, local models first, one model
resident at a time, ~8k working context. Anything that needs a frontier cloud
model is marked as such and put on the do-not-build list, not wished into the
plan.

Research basis: a live web survey run 2026-11-11 (AI-browser landscape +
Ollama library pages read that day; sources at the end) plus Arlo's own
full analysis of the shipped browser code
(`forge-browser-analysis-2026-11-11.md`).

---

## Part 1 — The models suited for this browser

### The recommended stack (one line each)

| Seat | Pick | Pull | Why |
|---|---|---|---|
| **Chat brain + vision (resident)** | **Qwen3.5 4B** | `ollama pull qwen3.5:4b` | Tools + vision + thinking in one model — it drives the chat AND sees images, so no second model ever has to load. 3.3–4.0 GB download, 256K listed context. |
| Brain fallback (less RAM) | Qwen3 4B | `ollama pull qwen3:4b` | 2.5 GB, tools + thinking, the most battle-tested local agent model. Its non-thinking **Instruct-2507** variant is the ideal agent build if the tag pulls cleanly (IFEval 83.4, Apache 2.0). |
| Fast seat (titles, routing) | Qwen3 1.7B | `ollama pull qwen3:1.7b` | 1.4 GB, tools-capable, roughly 7–14 tok/s estimated — for micro-decisions, not research answers. |
| Vision seat (only if the brain is text-only) | Qwen3-VL 2B Instruct | `ollama pull qwen3-vl:2b-instruct` | 1.9 GB, strong OCR lineage. Swap in, swap out — never resident alongside the brain. Alternative: `minicpm-v4.6` (1.6 GB). |
| Memory / embeddings | nomic-embed-text | `ollama pull nomic-embed-text` | 274 MB, 768-dim. Powers tab grouping, history resurfacing, and retrieval. Storage math: ~30 MB per 10k page chunks — nothing, even on the HDD. |
| Voice input | whisper.cpp **base.en** | `ggml-base.en.bin` (148 MB) | ~5% word-error rate, faster than real time even on this CPU by estimate. Push-to-talk, not a voice mode. tiny.en (78 MB) for speed, small.en (488 MB, ~3.5% WER) for batch transcription only. |

Total disk for the whole AI stack: **≈3.7–4.5 GB.**
Total resident RAM target: the brain (~4.5 GB at 8k context) — everything else
loads on demand and unloads after.

### Honest speed expectations (estimates — read this before judging)

No published benchmark exists on an actual Ryzen 3 3250U; these are
bandwidth-model estimates from the research, and the single biggest unknown
is whether the laptop's RAM is single- or dual-channel (≈2× swing):

- `qwen3:4b` ≈ **4–8 tok/s** · `qwen3.5:4b` ≈ **3–6 tok/s** · `qwen3:1.7b` ≈ **7–14 tok/s** · any 8B ≈ **2–4 tok/s**
- **Prefill is the real pain:** feeding a full 8k-token page to this CPU can take a minute or more. The browser must feed the brain *selected chunks* (1.5–3k tokens), never whole pages.
- Cold loads from the HDD take tens of seconds — keep the brain warm (`keep_alive`), load everything else on demand.

### Runtime settings that matter more than model choice

- `num_ctx = 8192` — never the listed 256K; the KV cache would eat the machine.
- **Thinking OFF for the action loop** (`/no_think`) — every think-token is generated at the same slow speed before any answer appears.
- `OLLAMA_MAX_LOADED_MODELS = 1`.
- One action per turn, strict output format, validate-and-retry-once (already the chat design — keep it).

### Models checked and rejected for the brain seat (and why)

- **Any 7–8B as the default** — a Q4 8B is ~6.5 GB RAM at 8k context; add the OS + Qt/WebEngine and 8 GB is gone → swap on a spinning HDD → freeze. 8B is "expert mode, everything else closed," never default.
- **DeepSeek-R1 distills** — Ollama templates lack reliable tool support and the long think traces burn the whole context before acting. Popular name, wrong tool.
- **Gemma 3 4B as brain** — good vision model, but its Ollama entry has **no tools support**; it cannot drive the action protocol. Vision/chat seat only.
- **Gemma 4 "e2b"** — a trap: the name sounds tiny, the download is 4.6–7.5 GB.
- **Llama 3.2 3B** — works, but tool-calling is measurably weaker than Qwen's (Meta's own card: 67.0 at 3B, 25.7 at 1B). Only if licensing preference outweighs reliability.
- **SmolLM3 3B** — excellent on paper, but no official Ollama library entry as of Nov 2026 (community mirrors only). Revisit if it lands officially.
- **Qwen2.5 3B** — capable, but its license is Qwen Research, not Apache 2.0 — matters for a public open-source project.
- Worth an A/B test someday on the exact action protocol: `hermes3:3b` (~2.0 GB, function-calling tuned) and `phi4-mini` (3.8B, 2.5 GB, MIT license).
- Joshua's existing uncensored seats remain selectable any time via `FORGE_BROWSER_CHAT_MODEL` — these picks are about reliability-per-RAM, not permission.

---

## Part 2 — Improvements & enhancements

Grouped. Each item says what it is, where the idea comes from, and the honest
size. **[FIX]** items come from Arlo's code analysis of the shipped build —
they're defects and risks in what already exists, not new features.

### A. Make the brain smarter without making it bigger

1. **Default brain upgrade.** Change `pick_chat_model`'s preference order to
   qwen3.5:4b → qwen3:4b(-instruct) → qwen3:1.7b, and pass thinking-off +
   `num_ctx 8192` on every chat call. *Size: S.* Highest value-per-line on
   this list.
2. **Streamed answers.** Ollama streaming into the chat dock so words appear
   as they're generated — at 3–6 tok/s, waiting for the full answer feels
   broken; streaming feels alive. *Size: S.*
3. **Chunk-and-retrieve page feeding.** Stop handing the brain whole pages:
   chunk the current page (200–400 tokens), embed with nomic-embed-text,
   retrieve the chunks that match the question. This is what makes
   "chat with my tabs" actually work at 8k context. *Size: M.* (Borrowed from
   Comet/Dia, rebuilt for a small window.)
4. **@tab addressing.** "What does @tab2 say about the price?" — the chat
   already knows the tabs; let the person name them. *Size: S on top of A3.*

### B. Reading features (where AI browsers actually earn their keep)

5. **Summarize this page** — one click / one chat phrase, map-reduce in
   chunks for long pages, cached by URL + content hash. The research calls
   this the best first feature in the whole field. *Size: S–M.*
6. **Link previews** — hover a link, get a 2-line summary of where it goes
   (Arc's 5-Second Previews), generated on demand only — prefetching would
   thrash this CPU. *Size: M.*
7. **Cross-tab compare** — "compare these three" → extract the *same fields*
   (price, specs) from each tab in separate tiny calls, render the table in
   the window itself. Never one giant prompt. *Size: M.*
8. **Tidy titles + tab grouping** — messy page titles renamed by the fast
   seat; tabs grouped by topic using embeddings; history grouped the same
   way into resumable "journeys" (Edge/Firefox pattern — Firefox does the
   grouping on-device with a 57 MB model; ours is even cheaper). *Size: M.*

### C. Doing features (scoped hard, on purpose)

9. **Cards — saved workflows.** Dia Skills / Opera Cards, rebuilt local:
   plain-text prompt+action templates in a folder ("morning prices check",
   "pull the specs table"), invoked by name in chat. On a small model,
   canned workflows are a *reliability feature* — a tested template succeeds
   where free-form planning wanders. Ship 5–10 hand-tested ones. *Size: M.*
10. **Plan → Run → Interrupt.** For multi-step asks, the brain shows its
    step plan first; the person approves; each step runs visibly in a marked
    tab; a Stop button actually stops it. Confirm-before-anything-that-
    submits (purchase, post, send). This is the Fellou/Chrome safety shape,
    and it's the only honest way to grow the current 3-action cap. *Size: L.*
11. **Research as a background job.** The research runner already works;
    give it a progress line and let it run while browsing continues (strictly
    sequential — see the do-not-build list for parallel agents). Expect
    minutes, not the cloud's "1-minute research." *Size: S–M (UI only).*

### D. Memory (local, visible, yours)

12. **Local memory store.** Visited pages chunked + embedded into a local
    file store; the chat can answer "that thing I read last week about…".
    Plus a short **interests file the person can read, edit, and delete** —
    memory that hides itself rots (Dia is already phasing theirs out).
    Off for sensitive sites; nothing ever leaves the disk. This is the one
    feature where local beats cloud outright. *Size: M–L.*

### E. Voice

13. **Push-to-talk.** Hold a button (or a key), speak the command,
    whisper.cpp base.en transcribes, the text lands in the chat box.
    Record-then-transcribe; no wake word, no always-listening. *Size: M.*

### F. The window itself

14. **A model picker in the chat dock** — brain seat, fast seat, vision seat
    dropdowns fed by `ollama list`, instead of env vars. Plus Firefox-style
    **AI controls**: per-feature on/off and one master AI kill-switch. *Size: S.*
15. **Morning brief card.** On first launch of the day (or on demand): a card
    on the start page summarizing yesterday's browsing from the local memory
    store. Batch work done at idle hides the machine's slowness. *Size: M
    (needs D12).*
16. **Chat progress honesty** — the dock already prints "working: search…";
    extend it with elapsed time and a cancel button per turn (pairs with C10's
    Stop). *Size: S.*

### G. Engine-room hardening [FIX] — from the code analysis, still open

17. **[P1] Cap downloads.** Fetched files currently load fully into RAM with
    no size limit — a big file can OOM an 8 GB machine. Stream to disk with a
    cap. *Highest-risk open item in the analysis.*
18. **[E2] Markdown table separator row** in html2md — tables currently
    aren't valid Markdown tables.
19. **[E3] Barrier detection on raw HTML** during research (it currently
    scans extracted Markdown, so markup markers like password fields and
    captcha classes can never fire).
20. **[S3] Fetch address fence** — the fetcher will happily fetch
    127.0.0.1/LAN addresses; add a fence with an explicit local-allow switch
    (the verify flow needs local pages; plain fetching doesn't).
21. **[S2] Pin engine downloads** — the Lightpanda/Chromium downloads are
    unpinned nightlies with no checksum. Pin versions + verify hashes.
22. **[E9] Download name collisions** silently overwrite — uniquify names.

### Do NOT build (researched, and wrong for this machine/product)

- **Generative mini-apps from tabs** (Disco GenTabs, Neon Make) — frontier-model work.
- **Parallel background agents** (Comet Background Assistants) — one slow model is the whole computer; "background" here can only mean a sequential idle queue.
- **Cloud work-app connectors** (Gmail/Calendar/Slack into the chat) — OAuth cloud plumbing that sends personal data out; against the product's reason to exist.
- **Conversational voice mode** — spoken back-and-forth needs streaming ASR + TTS this CPU can't carry. Push-to-talk only.
- **Autonomous shopping/booking** — even the cloud vendors gate, cap, and label it experimental with frontier models. Confirm-before-submit (C10) is the ceiling.

---

## Suggested build order (a menu — Joshua picks, one at a time)

1. **A1 + A2** — brain upgrade + streaming (the chat suddenly feels twice as smart and twice as fast, same models class).
2. **G17 (P1)** — the download cap, before any big-file accident does it for us.
3. **B5** — summarize-this-page (the field's best first feature).
4. **A3 + A4** — retrieval feeding + @tab (real "chat with your tabs").
5. **C9** — Cards (saved workflows).
6. Then by appetite: B8 grouping, D12 memory, E13 voice, C10 plan-run, F15 brief.

## What could not be confirmed (honest gaps in the research)

- No inference benchmark on a real Ryzen 3 3250U exists anywhere the research looked — all tok/s figures are estimates from memory bandwidth, and prefill is even less certain. First build task on the real PC should include a 5-minute timing run to ground them.
- Whether the target laptop's RAM is single- or dual-channel is unknown and swings speed ≈2×.
- The `qwen3:4b-instruct-2507` Ollama tag was attested by secondary sources and the official HF card, but not confirmed as a first-class entry on the Ollama library page that day — check the qwen3 tags page before hard-coding it.
- SmolLM3's official Ollama status and the newer embedders (qwen3-embedding, embeddinggemma) can change — recheck at build time.

## Sources (all read 2026-11-11)

Ollama library (live): ollama.com/library/qwen3.5 · /qwen3 · /qwen2.5 ·
/gemma3 · /gemma4 · /minicpm-v · nomic-embed-text page · phi4-mini page.
Landscape: Perplexity Comet (shila.so, geotoolbox.ai) · Dia
(supasidebar.com) · Opera Neon (press.opera.com, operaneon.com) ·
Arc (crows-eye.com) · ChatGPT Atlas shutdown (shila.so) · Fellou
(smartsight.in) · Chrome Auto Browse (blog.google) · Disco GenTabs
(blog.google) · Edge Copilot changes (neowin.net) · Firefox on-device AI
(techwalla.com) · Brave Leo BYOM (aimultiple.com, agenticindex.io).
Models/hardware: WikiChip Ryzen 3 3250U specs · localaimaster tool-calling
survey · cliodeck Ollama-tools research · dogkeeper886/ollama37 issue #366
(DeepSeek templates) · anchorterminal Qwen3-4B-Instruct-2507 ·
morphllm embedding roundup · bigiron.cc whisper.cpp measurements ·
promptquorum whisper comparison.
