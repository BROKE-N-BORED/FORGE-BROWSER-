# Forge Browser v0.2 — full software analysis (2026-11-11)

Scope: the Forge Browser package only (forge/browser/, 17 modules,
2,733 lines of Python + 771 lines of tests + setup script + docs),
as shipped in FORGE-BROWSER-v0.2.zip. Method: full code read,
compile + AST import scan, pattern scan for risky calls, and
targeted probes for suspected defects (each finding below says how
it was verified: PROBE = run and observed, CODE = read in the code,
LIVE = observed in the 2026-11-11 live tests). This is an audit —
a list for Joshua to direct from. Nothing here was fixed.

## Strengths (verified)

1. Zero dependencies, compiles clean. The whole package is Python
   standard library, including a hand-written WebSocket client —
   it installs by unzipping and can never break on a pip conflict.
2. Test quality for its size: 31 tests, and the CDP tests run the
   REAL protocol code against a raw-socket fake CDP server (real
   handshake/framing), plus a live Lightpanda session and live web
   search/fetch/research runs on 2026-11-11.
3. Resilience where it counts: the keyless search chain (SearXNG →
   DDG Lite → DDG HTML → Bing) exists because a live test caught a
   backend bot-walling; first-with-results wins, errors fall through.
4. Small-model contract: paging on fetch (offset/next_offset),
   numbered interactive elements, accessibility snapshots — pages
   are shaped for an 8k-context local model throughout.
5. Security posture in the vault: PBKDF2-HMAC-SHA256 at 600k rounds,
   encrypt-then-MAC, 0600 file, and tests that prove the password
   appears nowhere — not in the file, not in listings, not in fill
   results. Screenshots are PNG-signature-validated before saving.
   verify refuses path traversal. Errors are plain language
   everywhere; nothing reports success it did not check.
6. Honest failure design: no seat → "not described", never an
   invented description; render refuses rather than silently
   returning the unrendered page; barriers are named, not hidden.
7. Fits his established designs: the vision seat follows his
   2026-11-09 seat order; the research plan gate follows Forge's
   plan-gate rule; verify follows the #65 contract.
8. Resource discipline: one engine, one page, throwaway profiles,
   timeouts on HTTP (20–30 s) and on the transcription/vision calls.

## Errors (defects in the code)

- E1 (HIGH, PROBE) CDP calls can hang forever. CDPClient.call checks
  its deadline only AFTER recv_message returns, and the socket has
  no timeout during a call — against a silent engine, call(timeout=2)
  was still hung at 8 s when the probe killed it. A frozen renderer
  or a wedged engine hangs the agent with no error at all.
  (cdp.py CDPClient.call / _ws.py recv path.)
- E2 (PROBE) HTML tables are not valid Markdown tables: rows are
  emitted with no `| --- |` separator row, so viewers render them
  as plain text lines and models get a weaker structure signal.
  (html2md.py table handling; the CSV path in files.py does emit
  the separator — the two paths disagree.)
- E3 (PROBE + CODE) Research barrier detection is degraded:
  run_research passes the extracted Markdown text to
  detect_barriers, whose strongest markers are HTML markup
  (`type="password"`, recaptcha class names) that can never appear
  in extracted text. Phrase markers ("sign in to continue") still
  fire, so some login pages are caught — but a CAPTCHA wall with no
  visible phrase, or a login page with different wording, reads as
  a normal source. (research.py + barriers.py marker design.)
- E4 (CODE) fill_login can type the username into the wrong field:
  it keeps the LAST text/email input seen before the password
  field whose name/text hints at user/email/login — or any text
  input if none hint — so a search box or newsletter field placed
  before the login form can receive the username. (cdp.py.)
- E5 (CODE) wait_event strands replies: a CDP message carrying an
  id that arrives during wait_event is appended to _events, which
  call() never drains. Rare with current call patterns (no call is
  in flight during a load wait), but a late reply to a timed-out
  call can be lost permanently. (cdp.py.)
- E6 (CODE) The whisper.cpp transcription path is effectively dead:
  its check requires a binary named whisper-cli/main whose path
  contains "whisper", which excludes a plain whisper.cpp `main`
  build in a normal folder. The CLI and faster-whisper paths work.
  (media.py.)
- E7 (CODE) Misleading notes for media failures: when an image
  cannot be described, the note always says "no local vision seat
  is configured" — also when a seat IS configured but errored,
  returned empty, or the "image" is an SVG (which fetch_any routes
  to the vision seat as un-rasterizable bytes). (files.py/media.py.)
- E8 (CODE) Vault.open never checks the file's app/version fields;
  a future format change has no migration hook. Low today.
- E9 (CODE) Download filename collisions silently overwrite: two
  fetch_any saves with the same basename (common: index.mp3,
  image.png) replace each other in the media folder. (files.py.)
- E10 (CODE) Minor: unused `urlparse` import in cdp.py; a failed
  Chromium launch can leak its temp profile folder; `forge dev`
  trivia — none affect behaviour.

## Security findings

- S1 Homebrew cryptography. The vault's HMAC-keystream +
  encrypt-then-MAC construction is sound in design and test, but
  it is not a standard, audited cipher (AES-GCM/ChaCha20 via the
  `cryptography` package would be), and the minimum passphrase is
  only 8 characters. The honest rating: strong against a stolen
  file + weak passphrase guessing is the real limit (PBKDF2 600k
  slows guessing; a short passphrase is still the weak point).
- S2 Setup script supply chain: browser-setup.sh downloads a
  NIGHTLY Lightpanda binary with no checksum or signature check,
  and nightly is a moving target (any future build changes under
  the same URL). SearXNG is cloned at HEAD, also unpinned. The
  generated SearXNG settings contain a placeholder secret_key that
  stays in place if the user never edits it (localhost-only bind
  limits the exposure).
- S3 Fetch has no address fence. fetch/fetch_any/render will go to
  any http(s) address they are given, including 127.0.0.1 and LAN
  addresses — a page or prompt that steers the agent could make it
  read local services (router admin, Ollama, file servers). This
  diverges from the fence posture of Forge's existing Browse
  connector (#65 allowlist). On his own machine the blast radius
  is his own network, but the gap is real.
- S4 No robots.txt respect and no per-site delay: the research loop
  fetches back-to-back. Operational risk (IP blocks on some sites)
  more than security.
- S5 Vault fill has no http warning: credentials can be filled
  into a plain-http page without any note.
- S6 FORGE_TRANSCRIBE_CMD runs through shell=True. It is the user's
  own environment variable (same trust class as a plugin), but
  anything that can set Forge's environment gains command
  execution through a transcribe call.
- S7 Saved media/screenshots sit in ~/.forge/ with default file
  permissions (not 0600). Single-user PC, low risk; the vault
  itself is strict, the media folder is not.

## Performance (against his PC: Ryzen 3 3250U, 8 GB, HDD)

- P1 (HIGH, CODE) Unbounded downloads into RAM. http_get reads the
  ENTIRE response into memory (and fetch_any then copies/decodes
  it). A 500 MB video or a hostile endless stream can OOM the
  machine or drive it into HDD swap. No streaming, no size cap,
  no Content-Length precheck anywhere in the fetch paths.
- P2 Research is fully sequential: search, then one fetch at a
  time. Sandbox live run: ~3.2 s for 2 sources on a fast network.
  Six sources from slow sites on home WiFi will take minutes, and
  nothing is cached — a repeat research re-downloads everything.
- P3 Vault cost per save: PBKDF2 runs on every save (0.5 s measured
  in the sandbox; expect ~1–2 s on the Ryzen 3). Adding entries
  one by one is fine; bulk edits would crawl.
- P4 Transcription reloads its model per call (whisper CLI spawns
  fresh; faster-whisper constructs the model inside the function).
  On CPU that is the dominant cost of transcribing anything.
- P5 Screenshots move as base64 inside CDP JSON (~33% overhead,
  several full-size copies in RAM transiently). Fine at 1080p.
- P6 html2md is a single streaming pass (good), but builds the full
  Markdown string plus a line-split copy; multi-MB pages churn
  memory on top of P1's whole-body read.

## UI and usability gaps

- U1 (the big one) No integration into Code Forge itself yet. The
  zip contains a library + CLI only — no Forge plugin/tool
  registration, no bench surface, no deck. A model inside Forge
  cannot call any of this until a wrapper lands (the DEV thread's
  browser merge is that work; from this package's side it does
  not exist yet).
- U2 The CLI cannot run a multi-step browser flow. Every command
  opens and closes its own session; the CLI exposes no --profile
  or --headed flags, so the human handoff (a headline behaviour)
  is unreachable from the command line, and there are no click /
  type / vault-fill commands at all — those exist only as Python
  calls.
- U3 No progress output. Research and long renders are silent
  until they finish — on his phone there is no sign of life,
  the exact complaint he has made about background work before.
- U4 Configuration is nine FORGE_* environment variables and
  nothing else: no config file, no Forge Customize surface. The
  `engines` command reports state well, but a setting can only be
  changed by editing the shell environment.
- U5 Vault management gaps: no change-passphrase, no backup/export,
  and site keys are exact strings — an entry saved as "github.com"
  does not match a fill request for "www.github.com/login".
- U6 Research reports are evidence packs, not reports: excerpts
  are the first N characters of each source (mid-sentence cuts,
  page furniture included, as the live ollama.com excerpt showed).
  Synthesis is deliberately left to the reading model, but it
  means the browser layer alone does not produce anything like a
  finished Gemini-style brief — the Research room has to do that
  step, and the hand-off contract (which model, what prompt) is
  not defined anywhere yet.
- U7 Discoverability: the top-level package exports only the
  headline functions; engines/barriers/vault/media/cdp are
  importable but undocumented in __init__ — a future project
  reusing the package has to read the source or BROWSER.md.

## Test and verification gaps

- T1 Untested surfaces: CLI render/snapshot/screenshot/research/
  vault commands; barriers.handle_page; the real Chromium launch
  path (impossible in the sandbox — no Chromium there; needs his
  PC); WebSocket large frames (126/127-bit length paths);
  fetch_any video kind; the vault CLI flow; SearXNG live (only
  fixture JSON tested).
- T2 No performance tests at all — nothing pins time or memory
  budgets the way Forge's own #57 perf-budget work would demand.
- T3 No hostile-input fixtures: malformed HTML nesting, huge
  pages, PDFs with anything beyond one FlateDecode Tj stream
  (UTF-16/ToUnicode PDFs will extract garbage or nothing — the
  extractor's real-world hit rate on arbitrary PDFs is untested).
- T4 Search parsers will drift with upstream markup; only live
  runs catch that. No recorded real-page fixtures exist.

## Missing capabilities (vs the ambition)

- M1 No request blocking (ads/trackers/heavy media) — on an HDD
  machine this is a speed feature, not just cleanliness.
- M2 No session recording/replay: a flow the agent works out once
  cannot be replayed without a model re-driving it.
- M3 No downloads manager beyond single-file fetch_any; no
  multi-tab work (deliberate for RAM), no find-in-page, no
  network log/HAR for debugging why a page failed.
- M4 No OCR for scanned PDFs or image text beyond the vision seat;
  no PDF form filling; no read-aloud (TTS) of pages.
- M5 No translation, no history/bookmarks, no per-site notes —
  the browser has no memory of where it has been between runs
  except the vault and the media folder.

## Corrections and in-place fixes (2026-11-11, standalone packaging)

- U1 is RETIRED. Joshua (2026-11-11 ~08:54): the browser is meant
  to run independently first, be tested on its own, and only be
  baked into Code Forge eventually. "No Forge integration yet" is
  the design, not a gap.
- E1 is FIXED (v0.3): CDPClient.call now enforces its deadline on
  the socket. The silent-engine probe that hung past 8 s now fails
  at exactly 2.0 s with the plain timeout message.
- T3 partially closed (v0.3): the PDF extractor now parses
  /ToUnicode CMaps and decodes CID-keyed fonts (and hex-string Tj,
  which it previously skipped entirely). The real W3C dummy.pdf
  extracts; a synthetic CID fixture is in the suite.
- Engine discovery gap (found by the guided test script, not in
  the original list): browser-setup.sh installed Lightpanda where
  nothing looked for it. find_lightpanda() now checks PATH, the
  FORGE_LIGHTPANDA_BIN override, and the setup install folder,
  and sessions auto-start the binary — no env vars needed.
- All other numbered items stand as written, awaiting direction.

## Bottom line

The skeleton is sound — the architecture (stdlib, seats, plan
gate, keyless chain, vault) matches every constraint it was built
under, and its verified core (search, fetch, research loop, vault,
CDP against a real Lightpanda) genuinely works. The risks are
concentrated in three places: hangs and memory on his hardware
(E1, P1), the security edges (S2, S3), and the fact that Forge
itself cannot call it yet (U1) with the CLI unable to drive its
own headline flows (U2). Fix order I would recommend, if directed:
E1, P1, U2, S3, E2/E3, then the rest by number.
