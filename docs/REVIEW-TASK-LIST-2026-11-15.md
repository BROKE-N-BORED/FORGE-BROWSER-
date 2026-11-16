# Forge Browser — Review task list (2026-11-15)

Review of v0.9 as it stands, ordered by Joshua 2026-11-15 ~14:19.
Two passes: Arlo's direct review (sections A–F) and a three-reviewer
code sweep (167 findings, folded in below). Everything in A–F was
verified against the tree by Arlo. The sweep's 14 High findings were
all re-verified by Arlo against the code before listing; its Mediums
and Lows are recorded with file:line evidence and marked [sweep] —
spot-verify before building. Overlaps between the passes are merged
into one item, not listed twice.

Severity: HIGH items are marked. Everything else is Medium unless
marked (low).

## A. Safety and data loss — highest

- [~] A1 [HIGH] Vault save is not atomic, and there is no backup.
      `forge/browser/vault.py` `save()` opens the vault file with
      O_TRUNC and writes the new blob straight into it. If the
      process dies mid-write (crash, power loss, OOM kill on an
      8 GB machine), the vault is left truncated and every saved
      password is gone — and there is no backup or export anywhere.
      Worse: `vault.py:95` puts `json.load` outside the `try`, so
      a truncated vault surfaces as a raw JSONDecodeError
      traceback instead of a plain-words error. Fix: write to a
      temp file and rename into place (+fsync), keep one `.bak`,
      move `json.load` inside the `try`.
- [~] A2 [HIGH] Destructive removes delete with no confirmation.
      Memory "Forget selected fact" / "Remove selected page"
      (`app.py:1533-1543`) and bookmark Remove (`app.py:1047-1055`)
      delete immediately on click — no confirm, no undo. U31
      requires a confirm with a clean escape, U23 requires the
      default to be No. Fix: a confirm dialog defaulting to No
      on all three.
- [~] A3 [HIGH] Transcription command template allows shell
      injection via filename. `media.py:157-158` does
      `template.replace("{file}", path)` then
      `subprocess.run(command, shell=True)` with the path
      unquoted, so a downloaded filename containing shell
      metacharacters executes arbitrary commands (plain spaces
      break it too). Fix: run without `shell=True` (substitute
      as argv items) or `shlex.quote` the path.
- [~] A4 [HIGH] Unauthenticated CDP on fixed port 9222 plus
      `--no-sandbox`, on a persistent logged-in profile.
      `cdp.py:125` (default port 9222), `cdp.py:147-149` and
      `cdp.py:435-437` (`--no-sandbox`,
      `--remote-debugging-port`). Any local process can connect
      to 127.0.0.1:9222 with no auth and fully drive the user's
      logged-in browser, and the disabled renderer sandbox means
      a compromised renderer has the user's full privileges.
      Fix: bind the debugging port to a random per-launch port,
      drop `--no-sandbox` where the sandbox works, and document
      the local-process trust assumption.
- [~] A5 [HIGH] WebSocket frame length is trusted with no cap;
      one giant frame can OOM the 8 GB machine. `_ws.py:78-86,
      107-129` — `recv_message` takes a 64-bit length from the
      peer and `_read_exact` grows `_buffer` without bound, and
      `_fragments` accumulates without limit. Fix: refuse
      frames/fragments over a sane cap (e.g. 64 MB) before
      reading them. (Peer is the local engine, so this needs a
      buggy or malicious engine — still an OOM path on 8 GB.)
- [~] A6 Hardcoded SearXNG `secret_key` in the repo — the one
      literal secret found. `browser-setup.sh:67`
      (`secret_key: "forge-browser-local-only-change-if-you-like"`).
      Impact is limited (localhost-only service, key only signs
      preference cookies), but it is a published, predictable
      secret key and violates the no-secrets-in-code rule.
      Flagged to main chat 2026-11-15 per the rule (already
      committed — history is not rewritten). Fix: generate a
      random key per install with `secrets.token_hex` at setup.

## B. Design-rules gaps

- [~] B1 No activity indicator for page loads (U16). The window
      has a status bar and shows messages, but there is no
      load progress bar and no activity light: `loadFinished`
      is wired (`app.py:450`) but nothing shows while a page is
      loading. Fix: a progress bar or activity light in the
      status bar during loads. [sweep: M-D6]
- [~] B2 No app identity in the window (U15). The only
      `setStyleSheet` call in the app is one popup
      (`app.py:716`); the window itself is default Qt grey,
      no ember identity anywhere on screen. Fix: an ember
      identity stylesheet for the window, kept cheap
      (no heavy blur, no layout thrash on his PC).
- [~] B3 Modal dialogs can stack (U18). `QMessageBox` calls at
      7+ sites (`app.py:201, 919, 923, 931, 1268, 1295, 1961`)
      fire directly with no queue, so two can overlap. Fix:
      one-dialog-at-a-time queue with an honest "waiting for"
      message. [sweep: L-D4]
- [~] B4 The vault has no UI at all (U1). History and research
      reports have served pages (`/history`, `/research`);
      the vault is terminal-only plus autofill — adding,
      listing, or removing entries needs the CLI. The vault
      also stays unlocked until quit, and the passphrase
      prompt fires before the app can know whether an entry
      exists for the site. Fix: a Vault window in the app
      (unlock/lock, list, add, delete) reachable from Tools,
      prompting to unlock only from there. [sweep: M-U8]
- [~] B5 The backend does not configure itself (U35). Nothing
      measures the PC; model picks are hardcoded preference
      orders, and the "auto" brain pick uses a hardcoded
      4.5 GB guide (`chat.py:86`). Fix: measure total/free RAM
      at startup and size the brain guide from it, re-checking
      under load — under the hood, nothing for the user to set.
      [sweep: M-D3]
- [~] B6 No live measurements (U37). No graphs or live stats
      anywhere in the app; the only numbers are per-turn tok/s
      and the static engine-status dump on the start page. Fix:
      a small live readout (RAM in use, model speed, turn
      count) — display only, the backend adjusts. [sweep: M-D4]
- [~] B7 Model choices are not guided (U39). The Brain/Fast/
      Vision combos show names plus "(not installed)" /
      "(embed model)" (`app.py:1729-1743`), but not what each
      model does or whether it fits this PC's RAM; the
      Settings combos show bare names, not even sizes. Fix: a
      one-line plain-words description and fit note per model.
      [sweep: M-D5]
- [~] B8 Settings fields lack explanations underneath (U20).
      The seat combos in Settings have labels only
      ("Brain model:") with no plain-words line underneath
      saying what each seat is for; the chat dock's seat
      combos likewise. Fix: one line under each. [sweep: M-D2]
- [~] B9 [sweep] "Nothing tracked" copy is false (U17/U9).
      The start page (`webui.py:242`), tagline (`app.py:32`),
      About (`app.py:934`), and desktop-file Comment all say
      "nothing tracked", while every visit is recorded in
      `history.json` and page text in `page-memory.json`. Fix:
      say what is true — nothing leaves the machine, but visits
      and page text are kept locally — and link to
      History/Memory from the same line.
- [~] B10 [sweep] The debug log layer exists but the app never
      feeds it (U12). `app.py:2622` is the only `.action()` call
      in the app ("Forge Browser started"); there are no other
      `.action()`/`.info()`/`.error()` calls in `app.py` or
      `chat.py`, so the ring buffer and Errors sections the rule
      requires are nearly always empty. Fix: log one line per
      user action and per caught failure (send, navigate,
      download, vault unlock, turn failure).
- [~] B11 [sweep] (low) Settings that don't change sit in
      Settings (U36). `app.py:2564-2579` ("Link preview dwell
      (ms)"), `app.py:2546-2563` (Ollama/SearXNG addresses).
      Fix: remove the dwell control (keep the 600 ms default)
      and move the addresses behind an Advanced section.
- [~] B12 [sweep] (low) History and Research have no native
      window (U2). They exist only as local web pages
      (`webui.py:86-130`, `:300-330`); the menus offer native
      dialogs for How to Use, Bookmarks, Downloads, Memory,
      and Settings but nothing native for History or Research.
      Fix: give History and Research native windows like the
      Memory dialog.
- [~] B13 [sweep] (low) No collapsible headers in any panel
      (U38). The chat dock stacks header, log, status, entry
      row, cards row, memory row, seat pickers, and toggles in
      one long column. Fix: group the dock's seat pickers and
      toggles under one collapsible "Models and AI" header.
- [~] B14 [sweep] (low) The start page dumps engine-room
      internals on the main screen (U34/U19).
      `webui.py:148-152` (full `engines.format_status` block:
      filesystem paths, revisions, env-var names). A child or
      grandparent meets backend plumbing on first launch. Fix:
      replace the status dump with one plain line ("Everything
      is ready" / what is missing and its one-button fix).
- [~] B15 [sweep] (low) Silent caps and silently swallowed
      write failures (U32). History capped at 200
      (`titles.py:27,321`), session at 50 tabs
      (`session.py:12,38`), memory at 200 (`memory.py:25,199`),
      and `_save` swallows `OSError`, so `remember()` reports
      success when the disk write failed. Fix: disclose each
      cap where the data is shown, and surface a failed memory
      write instead of swallowing it.

## C. Docs

- [~] C1 BROWSER.md documents none of the v0.9 features. Zero
      matches for How to Use, Debug Report, single instance,
      `run.sh check`, or free CI in the manual — they exist
      only in CHANGELOG.md. Fix: add a short section per
      feature where a user would look for it. [sweep: M-Doc7]
- [~] C2 U10 brand mentions in docs. `BROWSER.md:216,220`
      ("Google publishes no SHA-256…"), `BROWSER.md:474`
      ("DuckDuckGo HTML, then Bing HTML"),
      `CHANGELOG.md:196` ("Google published them");
      `docs/IMPROVEMENTS.md` is the worst offender
      (`:105,118,143,203-204` — Edge/Firefox/Fellou/Chrome/
      blog.google/neowin.net). The rule is zero brand mentions;
      only open-source/ordinary infra by name. Fix: reword to
      "the snapshot publisher" and "the keyless search
      backends". [sweep: L-D1]
- [~] C3 [sweep] BROWSER.md still claims the suite is "50/50".
      `BROWSER.md:637` ("Verified in the sandbox on 2026-11-11:
      50/50 pass") while `BROWSER.md:625` itself says 534 — the
      file contradicts itself. Fix: update the Tests section to
      534 and the current verification date.
- [~] C4 [sweep] Commands section omits 8 of 25 CLI subcommands.
      `BROWSER.md:447-465` lists only 17; `cli.py:707-751` also
      defines `chat`, `cards`, `card`, `memory`, `brief`,
      `preview`, `seats`, `voice`. Fix: add the eight.
- [~] C5 [sweep] Stop is described as letting the in-flight
      brain call finish; the code interrupts it mid-stream.
      `BROWSER.md:392` vs `chat.py:656-657` ("interrupts a
      streaming generation in flight"). Fix: rewrite the
      sentence to say Stop interrupts the generation in flight.
- [~] C6 [sweep] fetch-any's PDF bullet omits OCR and
      contradicts the Limits section. `BROWSER.md:488-490`
      ("A scanned PDF has no text layer; that is reported…")
      while `files.py:502-507` runs OCR when tesseract is
      installed and `BROWSER.md:664-666` (Limits) says the
      same. Fix: add the OCR sentence to the PDF bullet.
- [~] C7 [sweep] The "auto" engine chain omits Kitesurf.
      `BROWSER.md:506-508` lists `FORGE_CDP_URL`, then
      `FORGE_LIGHTPANDA_URL`, then Chromium — but `cdp.py:346-347`
      and `:380-382` put `FORGE_KITESURF_URL` third, before
      launching Chromium. Fix: insert Kitesurf as the third
      auto step.
- [~] C8 [sweep] "Re-verified by hash on every run" overstates
      an install-time check. `BROWSER.md:221-223` and
      `engine-pins.txt:19` claim every-run re-verification;
      hash verification happens only in `install.sh`
      (`:75`, `:149-152`). Fix: change both lines to "on every
      install.sh run (--check included)".
- [~] C9 [sweep] `IMPROVEMENTS.md` still presents all 22 items
      as unbuilt. `:4-5` ("Nothing here is built until he names
      it") and `:153` ("still open") — all 22 are built
      (`docs/TASK-LIST.md`: 22 `[~]`, 0 `[ ]`). Fix: add a
      dated header note that all 22 were built 2026-11-11 and
      point at `TASK-LIST.md` for status.
- [~] C10 [sweep] (low) Research section says the second ask
      "is told to wait", hiding the queue. `BROWSER.md:334-335`
      vs `research.py:47,297` (the ask is accepted and queued
      with a position). Fix: say the ask queues and starts in
      turn.
- [~] C11 [sweep] (low) Chat paragraph lists 5 action kinds;
      the brain has 13. `BROWSER.md:87-94` names only search,
      open, research, read_page, summarize; `chat.py:9-21` also
      defines `card`, `remember`, `forget`, `forget_page`,
      `recall`, `recall_pages`, `brief`, `compare`. Fix: name
      the rest (or say "including memory, cards, brief, and
      compare").
- [~] C12 [sweep] (low) `KITESURF_PORT` is named as the port
      knob, but no code reads it. `BROWSER.md:278` vs
      `cdp.py:56` (comment only); the actual knobs are
      `FORGE_KITESURF_URL` or the 8787 default. Fix: replace
      with `FORGE_KITESURF_URL`, or implement `KITESURF_PORT`
      in `find_kitesurf()`.
- [~] C13 [sweep] (low) Vendored `debug_log.py` examples still
      name the donor project. `debug_log.py:25,36` (`duo`,
      `0.3.1`) while the app passes `("forge-browser",
      __version__)`. Fix: change the docstring examples to
      `forge-browser` / `0.9` — noting the deliberate
      divergence from the verbatim vendored file (its SHA
      match to the BNB-TOOLS canonical copy would break).
- [~] C14 [sweep] (low) C2's "~930 MB" install size omits the
      nomic-embed-text pull. `docs/DEEP-AUDIT-TASK-LIST.md:58-59`
      vs `install.sh:279-282` (also pulls nomic-embed-text
      ~274 MB when Ollama is present — ~1.2 GB on an Ollama
      machine). Fix: say "~930 MB (≈1.2 GB with Ollama
      present)".

## D. CI and ship

- [~] D1 CI bootstrap quirk. The first `ci/check.sh` run on a
      tree with no CI-STATUS.md FAILs its own suite (the
      status-count test reads CI-STATUS.md with no existence
      guard, `tests/test_browser2.py:3414-3421`); the second
      run passes because the script wrote the file. Fix: the
      test tolerates a missing file, or check.sh writes a
      provisional CI-STATUS.md before running the suite.
      [sweep: L-I10, mechanism confirmed]
- [~] D2 CI-STATUS.md never names the commit. `ci/check.sh`
      reads it from git, and the tree has no `.git` (snapshot
      workflow), so it always says "unknown (not a git
      checkout)". Fix: check.sh takes the commit from an
      env var or argument so the push step stamps the real SHA.
- [~] D3 Hunt the unreproduced suite flake. One run during the
      v0.9 build failed with `failures=1` and the name was
      lost; every run since (including Arlo's own 534/534)
      was green. The sweep found no obvious candidate (sleeps
      have one-directional margins, research tests use Events,
      fence tests restore state in tearDown). Fix: run the
      suite repeatedly until it reproduces, then fix the
      flaking test.
- [~] D4 E3 test scoping wart (low). The 0.9 CHANGELOG entry
      names the v0.3 claim while correcting it, so the E3
      test scopes its no-claim assertion to the 0.3 section.
      Tidy if the area is touched.
- [~] D5 [HIGH] `--check` is documented as change-free, but it
      chmods files and runs the whole suite. Doc:
      `BROWSER.md:44` and `install.sh:5` ("report only, change
      nothing"). Code: `install.sh:31-41` restores exec bits
      unconditionally with no CHECK_ONLY guard, and
      `install.sh:347-355` runs the full suite unconditionally
      (~80 s, writes `__pycache__`) — the script has no early
      exit for --check (only exit is line 363). Fix: gate both
      blocks on CHECK_ONLY=0 (or correct the doc).
- [~] D6 [HIGH] `install.sh` behavior is never executed by any
      test, so D5 is uncatchable by the suite.
      `tests/test_browser2.py:2597-2867` (InstallerTests) only
      greps script text and runs `sh -n`; nothing executes
      `install.sh --check`, a hash-mismatch refusal, the
      Chromium swap/restore, or the `--with-whisper` fetch.
      Fix: add one harness test running `sh install.sh --check`
      in a fake home asserting exit 0, no mode changes, and no
      suite run.
- [~] D7 [HIGH] `ci/build-zip.py` and `ci/check.sh` are never
      executed; their core logic is untested, hiding a
      basename/path mismatch. `tests/test_browser2.py:3398-3415`
      (CiTests) only runs `sh -n` and `py_compile`. Latent
      divergence: `build()` matches executables by full
      relative path (`ci/build-zip.py:94`) while `verify()`
      matches by basename (`ci/build-zip.py:119`) — a nested
      file sharing a manifest basename would fail verification
      of a zip `build()` just made (same pattern in
      `build-release-zip.py:71-73` vs `:94-105`). Fix: test
      `build()` + `verify()` on a fixture tree (including a
      nested same-basename file) and run `ci/check.sh` against
      a deliberately failing suite.
- [~] D8 [HIGH] Launch failure from the app menu is completely
      silent. `run.sh:59-61` execs straight into Python with no
      error handling; the desktop entry uses `Terminal=false`;
      `cli.py:867-872` prints errors to stderr only. If PySide6
      breaks after install, the user double-clicks, nothing
      happens, and no message appears anywhere. Fix: have
      `run.sh` trap a failed launch and show the error in a
      zenity/tkinter dialog (the installer already has both
      helpers).
- [~] D9 [sweep] `install.sh` exits 0 and prints "Install done"
      with no working window. `install.sh:261` (PySide6 failure
      only echoes, never sets FAIL), `:363` (`exit $FAIL`);
      engine/Chromium download failures likewise never set
      FAIL. Fix: set FAIL=1 when the venv/PySide6 build fails
      and say the window will not open.
- [~] D10 [sweep] The installer never installs Ollama or a chat
      model, so chat is dead on a fresh machine.
      `install.sh:265-282` (only nomic-embed-text, only if
      Ollama is already present). D3 requires the installer to
      install or update *all* dependencies; the chat brain is
      the app's core feature. Fix: install Ollama and pull a
      small chat model during install, or make the first-run
      dock do it with one button.
- [~] D11 [sweep] The GUI installer copies a `.venv` wholesale
      when one exists in the source tree. `Install Forge
      Browser:119` (`cp -R` with no exclusions). A copied venv
      is broken at its new path (absolute paths in
      `pyvenv.cfg` and shebangs). Fix: exclude `.venv` (and
      `__pycache__`) from the copy.
- [~] D12 [sweep] Every update rebuilds the venv and re-downloads
      ~600 MB of PySide6. The venv lives inside the swapped
      tree, so each update re-downloads the whole Qt kit on a
      spinning HDD. Fix: keep the venv outside the swapped
      tree, or carry the old `.venv` across the swap when it
      still imports.
- [~] D13 [sweep] The desktop entry cannot make Forge the
      default browser, and has no `StartupWMClass`. No `%u`,
      no `MimeType`, no `StartupWMClass`; `run.sh:59-61` passes
      a bare URL argument to the CLI parser, which rejects it.
      Links clicked elsewhere can never open Forge, and the
      running window may not group under its menu icon. Fix:
      add `MimeType=text/html;x-scheme-handler/http;
      x-scheme-handler/https;`, `Exec=… %u`, `StartupWMClass`,
      and URL forwarding in `run.sh`.
- [~] D14 [sweep] From a fresh clone, every installer script is
      mode 644, so double-clicking the installer does nothing.
      The snapshot push stores everything as 100644; the zip
      path is fine (builders store 0755), but the clone path
      needs a terminal to restore the bits — the exact
      chicken-and-egg U3 forbids. Fix: ship a documented
      one-click path for clones (e.g. a README-first step or a
      `.desktop` file that runs `sh` on the installer).
- [~] D15 [sweep] Release verification never extracts the zip it
      verifies and never launches the app.
      `verify-install.sh` contains no `unzip` step (its header
      promises "fresh unzip" but it runs whatever stale tree
      sits in `$EXTRACT`) and no app launch — only a PySide6
      import check — so a release whose window crashes on
      launch still passes. Fix: extract the freshly built zip
      into a clean directory, then launch the app headless and
      assert the window appears.
- [~] D16 [sweep] (low) `install.sh` never checks that `curl`
      exists, though every download needs it. Fix: pre-flight
      `curl` (and `sha256sum`) and name the missing tool.
- [~] D17 [sweep] (low) No disk-space preflight before ~1 GB of
      downloads. Fix: refuse early with a plain message when
      free space is under ~2 GB.
- [~] D18 [sweep] (low) `install.sh` never re-verifies the
      PySide6 import after installing it. The import check runs
      *before* the pip install; success is claimed from pip's
      exit code, so missing system Qt libs report "PySide6
      installed" and fail at first launch. Fix: re-run the
      import check after the install step.
- [~] D19 [sweep] (low) Whisper model download has no hash check
      and an unpinned `main`-branch URL (`install.sh:317-318`).
      Fix: pin the model file with a SHA-256 in
      `engine-pins.txt` and verify it.
- [~] D20 [sweep] (low) Stale header comment claims "NO pip
      dependencies" while the script pip-installs PySide6
      (`install.sh:9-11` vs `:247-260`). Fix: correct the header
      (engine room stdlib-only, window needs PySide6 via pip).
- [~] D21 [sweep] (low) Install progress cannot be cancelled,
      and `curl` has no time limit. Fix: add `curl --max-time`
      and a Cancel button that kills the background install
      cleanly.
- [~] D22 [sweep] (low) A 1-hour install timeout reports only
      "did not finish cleanly". Fix: name the timeout in the
      failure message and say re-running resumes.
- [~] D23 [sweep] (low) A missing icon file is silently skipped,
      leaving a generic menu icon. Fix: warn in the failure
      dialog when the icon is missing from the release.
- [~] D24 [sweep] (low) SearXNG setup is unpinned: latest `main`
      clone plus unhashed pip requirements
      (`browser-setup.sh:55,58`); no `git`/`curl`/`python3-venv`
      checks, and a stale `src/` dir is silently reused. Fix:
      pin a SearXNG release tag and verify it, and check for
      `git` before cloning.
- [~] D25 [sweep] (low) `ci/check.sh` hardcodes a sandbox temp
      path (`$HOME/workspace/tmp`). It works anywhere
      (`mkdir -p` creates it) but litters a stray
      `~/workspace/tmp` on any machine that is not this
      sandbox. Fix: default to `mktemp -d` under
      `${TMPDIR:-/tmp}` with the override kept.
- [~] D26 [sweep] (low) `test-forge-browser.sh` has no trap:
      interrupting it orphans the background Lightpanda
      (fixed port 9223, `kill` only on the normal path). Fix:
      add a `trap` that kills the background engine on
      exit/interrupt.
- [~] D27 [sweep] (low) Release-verification timeout kill is
      shallow (`verify-install.sh:18-40` kills the installer
      shell but orphans the background `install.sh`/curl/pip
      children). Fix: kill the whole process group on timeout.
- [~] D28 [sweep] (low) `run.sh check` ignores the Ollama
      address saved in Settings. Fix: probe the same effective
      URL the app uses (`media.ollama_url()`).
- [~] D29 .deb packaging built (tooling verified) — NOT
      shipped as the release. Joshua 2026-11-15 20:42: "just
      release it as a zip dude, stop being difficult." The
      release stays a zip, as v0.1–v0.9. The .deb tooling
      (build-deb.py, verify-deb.py, launcher, test-deb.sh) is
      built, verified, and kept in
      ~/workspace/forge-browser-ai-release/ in case he wants
      it later. ADDED 2026-11-15 by
      Joshua (furious, on his PC): the release zip DOES carry
      the double-click installer `Install Forge Browser` stored
      0755 (verified from a rebuilt zip), but his extractor
      strips the exec bit, so double-clicking does nothing and
      he ends up installing and running from a terminal. A
      .deb fixes it properly: Mint opens it in the graphical
      installer on double-click (no exec bit needed), and dpkg
      sets the installed modes from the package. Build:
      `build-deb.py` in the release tooling (stdlib only: ar +
      tarfile + gzip), source tree under
      `/usr/share/forge-browser/`, icon, .desktop file in
      `/usr/share/applications/`, `/usr/bin/forge-browser`
      launcher that ensures the user-local install (first-run
      setup with GUI progress, never as root) then launches,
      passing URL arguments through. Engines are NEVER bundled
      (~1 GB downloads on first run, as today). Verify by
      extracting the .deb and checking modes + contents.

## E. Carried open work — blocked (his PC / his billing)

From docs/DEEP-AUDIT-TASK-LIST.md, re-verified open 2026-11-15
(the sweep confirmed all 8 independently).

- [ ] E1 C1/F5 — 5-minute timing run on his PC (hard gate).
- [ ] E2 C2 — Real-PC verdicts: flip `[~]` items to `[x]` as he
      verifies them on his PC. 0 of 237+ items verified so far.
- [ ] E3 D1 — Build the Kitesurf engine on his PC (Bazelisk +
      Node 22+ + pnpm) and drive it live. The adapter is tested
      against a fake CDP server; no live Kitesurf engine has
      been driven yet.
- [ ] E4 D2 — Record the Kitesurf build outcome honestly in the
      CHANGELOG (needs E3).
- [ ] E5 D3 — Threshold validators on his PC
      (`tests/validate_threshold.py`,
      `tests/validate_memory_threshold.py` — need real Ollama
      + nomic-embed-text).
- [ ] E6 F1/F2 — Clear the GitHub billing lock (his action in
      billing settings) and confirm CI green on the next push.
      Still locked: the v0.9 push's run was killed in 2 seconds
      (2026-11-14T14:22:50Z → 14:22:52Z), the 4th in a row.
- [ ] E7 I1 — Kitesurf install step (needs E3).
- [ ] E8 G1 proxy carve-out — proxied requests are not fence-
      pinned (the socket goes to the proxy, not the origin, so
      pinning would connect to the wrong host; proved by a
      port-3128 failure in the v0.9 build). Documented in v0.9.
      Needs a design decision: pin through the proxy CONNECT
      or accept the carve-out.

## F. Verified clean — not tasks

Checked during this review and holding, so they are not listed
as work. Two corrections from the first draft of this list: the
engine-room download resume holds but WebEngine downloads do
NOT resume (now item G-UX10), and "nothing tracked" is not true
as stated (now item B9).

- Engine-room resume: `_http.py:605-638` restarts downloads
  from the saved part with Range requests.
- U23 default No: the plan-approval dialog sets default No
  (`app.py:1967`); the "Fill login?" Yes/No is not destructive.
- U29 address bar: bad input becomes a search, never a dead
  end (`webui.py:352` `resolve_address`).
- History clear confirms first (`webui.py:118-120`, two-step).
- Vault crypto: PBKDF2 600k rounds, encrypt-then-MAC,
  constant-time MAC compare, 0600 file in a 0700 dir.
- Suite count 534 consistent everywhere pinned, matching the
  534 `def test_` methods; version 0.9 aligned in both
  `__init__.py` files, README, CHANGELOG, and the User-Agent.
- Vault wrong-password path IS tested; research cancellation
  IS tested; fence pinning flip/redirect/proxy/HTTPS IS
  tested.
- No telemetry or outbound traffic except the user's own
  actions (keyless search, user/model-chosen fetches through
  the fence, user-configured SearXNG) and installer downloads;
  Ollama is localhost by default; no keys sent anywhere.
- No hardcoded secrets except the SearXNG `secret_key` (A6)
  and one obvious test fixture.
- U33/U11/U27/U22/U5 hold; worker threads are used for
  chat/preview/voice/cards, so U30 holds at the app level
  except startup (G-P1).
- Research jobs are queued and run one at a time on their
  own thread (`research.py:255-290`).
- Real menu bar (File/Edit/View/Bookmarks/Tools/Help);
  QSettings geometry; U33 guard test; U12/U22/U27 all in v0.9.

## G. Second pass — code, performance, security, UX, tests

All [sweep] except the HIGH items, which Arlo re-verified
against the code.

### G-CODE — correctness

- [~] G-C1 [HIGH] `memory add` always crashes with
      AttributeError. `cli.py:863` runs `if getattr(args,
      "action", "") in ("add", "remove") and not args.site:`
      for every subcommand, but the memory parser
      (`cli.py:721-728`) defines only `action`/`value`, no
      `site` — so `memory add <text>` raises AttributeError
      before `_cmd_memory` runs, and `main` catches only
      BrowserError. Fix: scope the check to
      `args.command == "vault"`.
- [~] G-C2 [HIGH] `wait_event` swallows a dead engine and
      busy-spins at full CPU. `cdp.py:313-314` uses `except
      Exception: continue`, catching the BrowserError from
      `recv_message`; on a dead engine `recv` returns `b""`
      instantly, so the loop spins with no blocking wait until
      the 15–25 s deadline. Fix: re-raise BrowserError and
      retry only on `socket.timeout`.
- [~] G-C3 [HIGH] Streaming chat has no overall timeout; a
      stalled Ollama hangs the turn forever. `chat.py:600-604`
      retries `socket.timeout` with `continue` indefinitely;
      only `cancel()` breaks it, while the non-streaming path
      has a 240 s timeout (`chat.py:590`). Fix: add an overall
      deadline (e.g. 240 s of no tokens) that raises
      BrowserError.
- [~] G-C4 [HIGH] Download completion handler catches the wrong
      exception; failed saves stick at "downloading" forever.
      `app.py:1155` catches `OSError` around `keep_download`,
      but the failure path in `files.py:232` raises
      BrowserError, which escapes the Qt slot uncaught and
      leaves the record stuck. (Correction to the sweep: the
      `.part` file IS cleaned — `files.py:229-231` unlinks it
      before raising.) Fix: catch BrowserError alongside
      OSError.
- [~] G-C5 [HIGH] Comparison refresh failure leaves
      "Refreshing…" stuck forever and disables future refreshes.
      `app.py:802-808` — the refresh `work()` thread has no
      try/except, so any failure (e.g. brain down) dies
      silently; the view is removed from `_refreshing_views`
      only on success (`app.py:1919`), so later refresh clicks
      are ignored too. Fix: wrap `work()` in try/except that
      emits an error page and always discards the view.
- [~] G-C6 [sweep] A configured-but-down SearXNG breaks search
      instead of falling back. `search.py:284` returns
      `search_searxng(...)` directly, so its BrowserError
      propagates rather than falling through to the DDG/Bing
      chain the module docstring promises. Fix: catch
      BrowserError from `search_searxng` and continue.
- [~] G-C7 [sweep] Non-object SearXNG JSON raises a raw
      AttributeError. `search.py:64` — `data.get("results",
      [])` assumes the parsed JSON is a dict. Fix: check
      `isinstance(data, dict)` (and each item) and raise
      BrowserError.
- [~] G-C8 [sweep] Bing parser closes a result at any nested
      `</li>`. `search.py:192,208` — `_in_algo` is decremented
      on every `</li>`, so a nested list item inside a result
      drops the rest of that result. Fix: only decrement on the
      `</li>` matching the `b_algo` `<li>`.
- [~] G-C9 [sweep] `http_head` lacks the dropped-connection
      handler `http_get` has. `_http.py:552-570` vs `:596` —
      a reset mid-HEAD surfaces as a raw RemoteDisconnected.
      Fix: add the same `(http.client.HTTPException,
      ConnectionError)` handler.
- [~] G-C10 [sweep] `BrowserSession.start` leaks the engine on
      non-`BrowserError` failures. `cdp.py:511` — cleanup runs
      only on `except BrowserError`, so a `KeyError` or
      `ValueError` leaks the WebSocket, the spawned process,
      and the temp profile. Fix: catch `Exception` for cleanup,
      then re-raise.
- [~] G-C11 [sweep] Malformed CDP frames escape as raw
      `ValueError`. `cdp.py:287,317` — `json.loads(raw)` is
      unguarded in both `call()` and `wait_event()`. Fix: wrap
      parse failures in BrowserError.
- [~] G-C12 [sweep] Accessibility-tree formatter recurses with
      no depth limit or cycle guard. `cdp.py:744-758` — a
      deeply nested or cyclic AX tree raises RecursionError and
      kills the snapshot. Fix: cap depth and track visited
      node ids.
- [~] G-C13 [sweep] One exception in the tidy callback kills the
      tidy worker permanently. `titles.py:131` — `self._on_done`
      sits outside the try, and `request()` only starts the
      thread when `self._thread is None`, so after one failure
      no tab is ever tidied again. Fix: wrap `_on_done` in
      try/except (and/or restart a dead worker in `request()`).
- [~] G-C14 [sweep] The plan promises nested card steps that the
      runner refuses. `cards.py:258` reports a nested `card`
      action as `Unknown action type: card`, while
      `describe_action` (`chat.py:362`) presents nested steps
      as "Run the X workflow card" in the approval plan. Fix:
      implement nested cards in `run_card` (with a depth cap)
      or describe them as unsupported in the plan.
- [~] G-C15 [sweep] Verify URL is built without URL-quoting.
      `verify.py:67` — the relpath is concatenated raw, so a
      filename with a space or `#` produces a broken URL. Fix:
      `urllib.parse.quote` each path segment.
- [~] G-C16 [sweep] `Recorder.stop()` raises when no recording
      file exists. `voice.py:136` — `os.path.getsize` is
      unguarded (the CLI path in `cli.py:564` has no try around
      it). Fix: catch `OSError` around `getsize` and return
      `None`.
- [~] G-C17 [sweep] Memory stores race: concurrent
      load-modify-save loses entries. `memory.py:90,190,428` —
      no locking, while `app.py:603` spawns a `remember_page`
      thread on every navigation and chat threads write
      concurrently (contrast `seats.py`, which uses `flock`).
      Fix: guard all three stores with the same file-lock
      pattern as `_locked_update`.
- [~] G-C18 [sweep] Embedding cache is not keyed by model; stale
      vectors survive a model change. `memory.py:330-336` —
      cache keys are `sha256(text)` only. Fix: include the
      model name in the cache key (and in the on-disk file).
- [~] G-C19 [sweep] One malformed CMap entry kills whole-PDF
      extraction. `files.py:354` — `_cmap_unicode` calls
      `bytes.fromhex` on regex-matched hex that can be
      odd-length, and no caller catches `ValueError`. Fix:
      catch `ValueError` in `_cmap_unicode` and skip that
      mapping.
- [~] G-C20 [sweep] Background brief build and tidy worker share
      one `ChatSession` with user turns. `app.py:1427` (brief
      thread) and the tidy worker via `_fast_seat_session`
      (`app.py:551-560`) call `_chat` concurrently with user
      turns, racing on `_turn_tokens`, `_turn_seconds`,
      `last_speed`, and `_retrieve_last`. Fix: give background
      jobs their own `ChatSession`.
- [~] G-C21 [sweep] Closing/reordering a tab during send
      misattributes tab text. `app.py:2424-2446` — `collect()`
      stores text by position index while `start_work()`
      rebuilds the tab list from live widgets, so an index shift
      in the 5 s window pairs text with the wrong tab. Fix: key
      collected text by view identity (`id(view)`).
- [~] G-C22 [sweep] `find_kitesurf()` crashes on non-dict version
      JSON; other Kitesurf error paths untested. `cdp.py:229-236`
      — `version.get("Browser")` sits outside the `try`, so a
      `/json/version` returning a list/string raises
      AttributeError instead of returning `None`; a version
      dict missing `webSocketDebuggerUrl` is then polled for
      25 s, and auto-chain precedence has no test with two
      engines configured. Fix: guard the `.get` and add tests.
- [~] G-C23 [sweep] (low) `voice` with negative seconds crashes.
      `cli.py:561-563` — `time.sleep(-5)` raises ValueError,
      uncaught by `main`. Fix: clamp seconds to `>= 0`.
- [~] G-C24 [sweep] (low) `memory add` with whitespace-only text
      crashes. `cli.py:454-455` — `remember()` returns `None`
      for blank text, then `entry['text']` raises TypeError.
      Fix: check for `None` before printing.
- [~] G-C25 [sweep] (low) `research --out` / `history export`
      file errors escape as tracebacks. `cli.py:179,621` —
      plain `open(path, "w")` with no `OSError` handling, while
      `main` catches only BrowserError. Fix: wrap in
      BrowserError like the rest of the CLI.
- [~] G-C26 [sweep] (low) Cards named "check" or "new" can never
      be run. `cli.py:398` — the names are intercepted as
      subcommands before card lookup. Fix: use a flag or a
      distinct subcommand for check/new.
- [~] G-C27 [sweep] (low) `make_plan` accepts any `max_pages`,
      including negative. `research.py:87,97` — a negative value
      silently produces an empty "Done — no sources" report.
      Fix: clamp/validate `max_pages` to 1–20 as the chat action
      does.
- [~] G-C28 [sweep] (low) Terminated engine processes are never
      reaped. `cdp.py:718` (`kill()` with no `wait`) and
      `cdp.py:210` (`terminate()` with no `wait`) leave zombies.
      Fix: `wait()` after `kill()`/`terminate()` (with a
      timeout, ignoring errors).
- [~] G-C29 [sweep] (low) Engine-start poll ignores the `timeout`
      parameter. `cdp.py:462` — the `/json/version` poll uses a
      hardcoded 25 s deadline while `start()` takes `timeout`.
      Fix: use `timeout` for the poll deadline.
- [~] G-C30 [sweep] (low) "Smallest model" fallback prefers
      models with unknown size. `chat.py:231` — `min(models,
      key=lambda m: sizes.get(m) or 0)` sorts unknown-size
      models as 0, ahead of any real size. Fix: sort unknown
      sizes last.
      (checked 2026-11-15: not real. The fallback min() only
      runs when candidates is empty, and unknown-size models
      are always candidates, so the sort never prefers them.
      The sort-key change stays as harmless defense-in-depth.
      Recorded here so it is not re-litigated.)
- [~] G-C31 [sweep] (low) Brief cache write is non-atomic.
      `brief.py:221` — direct `open(path, "w")` instead of tmp
      + replace (self-heals via `load_brief`, but a crash still
      loses the cached brief). Fix: write tmp + `os.replace`.
- [~] G-C32 [sweep] (low) Restored `current` tab index can exceed
      the returned tab list. `session.py:74` — `current` is
      clamped against the pre-slice list length while `tabs` is
      sliced to `MAX_TABS`. Fix: clamp against the sliced list.
- [~] G-C33 [sweep] (low) `_cosine` is triplicated.
      `memory.py:253`, `retrieve.py:80`, `titles.py:146` — three
      copies of the same function. Fix: keep one in
      `retrieve.py` and import it elsewhere.
- [~] G-C34 [sweep] (low) `save_seats` is dead code with
      clobbering semantics. `seats.py:114` — no callers
      anywhere, and its replace-the-whole-dict write would
      discard concurrent edits. Fix: delete it (or reimplement
      via `_locked_update` merging).
- [~] G-C35 [sweep] (low) `memory pages` reads the page list
      twice. `cli.py:530` — `if not list_pages():` re-reads and
      re-parses the file instead of reusing the already-loaded
      list. Fix: store the result once.
- [~] G-C36 [sweep] (low) History page assumes every entry has a
      URL. `webui.py:91-93` — `entry["url"]` is indexed
      directly, but `load_history` returns unvalidated entries,
      so one malformed entry turns `/history` into a 500. Fix:
      validate entries on load or use `.get()` with a skip.

### G-PERF — performance / 8 GB PC

- [~] G-P1 [HIGH] Launch freezes up to ~10 s with no window when
      Ollama is down. `app.py:1361` (`ChatSession()`),
      `app.py:1714` (`fill_seat_combos`), `app.py:2496`
      (`list_models()`) each block the GUI thread; `chat.py:144-152`
      `check_ollama` is a blocking `urlopen` with `timeout=5`,
      called before `window.show()` (line 2644). On a fresh
      machine with no Ollama (the default state) every launch
      stalls with nothing on screen. Fix: move the brain pick
      and seat fill to a background thread and paint the window
      first with a "checking…" header.
- [~] G-P2 [sweep] Gzip bomb transient: one chunk can balloon
      before the cap check. `_http.py:511` — `decoder.decompress
      (raw)` has no `max_length`, so a single 64 KB compressed
      chunk can expand to far more than `MAX_BODY_BYTES` in RAM
      before the cap is enforced. Fix: decompress with
      `max_length` set to the remaining budget.
- [~] G-P3 [sweep] DNS is resolved twice per fetch despite the
      "resolve once" claim. `_http.py:110` (`check_fetch_url`)
      and `_http.py:309` (`_pinned_addrs_for`) each call
      `_resolve_checked_host`, doubling DNS latency on every
      fetch and redirect hop. Fix: resolve once in
      `check_fetch_url` and pass the addresses through.
- [~] G-P4 [sweep] A fresh opener (new TCP+TLS) is built for every
      request. `_http.py:386` — `_urlopen` calls `build_opener`
      per call with no connection reuse, so search chains and
      research runs pay full handshakes each time. Fix: reuse
      one opener per `allow_local` setting.
- [~] G-P5 [sweep] WebSocket masking is a per-byte Python loop.
      `_ws.py:101,122` — `bytes(byte ^ mask[i % 4] ...)` runs one
      Python iteration per byte, so MB-sized CDP messages cost
      millions of iterations on 2 cores. Fix: mask with chunked
      XOR (e.g. via `int.from_bytes` blocks).
- [~] G-P6 [sweep] CDP event stash grows without bound.
      `cdp.py:295,319,321` — events and stray replies are
      appended forever; only the awaited method is ever removed.
      Fix: cap `_events` (e.g. drop oldest past 100).
- [~] G-P7 [sweep] Chat history grows without bound for the
      session's life. `chat.py:1073-1075` — `self.history` is
      appended to every turn and never trimmed (`HISTORY_CAP`
      only slices for the prompt, despite its "messages kept"
      comment). Fix: trim `self.history` to `HISTORY_CAP` (or a
      larger bound) after appending.
- [~] G-P8 [sweep] PDF extraction holds all decompressed streams
      in memory at once. `files.py:324` — every stream is
      decompressed into `streams` before any text is read, so a
      32 MB PDF can transiently occupy hundreds of MB. Fix:
      process each stream one at a time.
- [~] G-P9 [sweep] CMap `bfrange` expansion is unbounded.
      `files.py:384` — `range(int(lo,16), int(hi,16)+1)` can add
      65,536 entries per range, with no cap across ranges. Fix:
      cap total CMap entries (e.g. 65,536) and stop expanding
      past it.
- [~] G-P10 [sweep] JSON files are fully re-serialized before
      truncation. `files.py:527` — `json.dumps(json.loads(text),
      indent=2)[:200000]` builds the entire indented string
      (potentially far larger than the 32 MB input) just to keep
      200 KB. Fix: truncate the input or stream the dump and
      stop at the limit.
- [~] G-P11 [sweep] Vision-seat pick re-queries Ollama on every
      describe and status check. `media.py:82-108` —
      `pick_vision_model` calls `/api/tags` plus up to one
      `/api/show` per installed model each time, with no
      caching. Fix: cache the pick briefly (e.g. 60 s).
- [~] G-P12 [sweep] Every start-page load pays for full engine
      status probing. `webui.py:129` — `start_page()` calls
      `engine_status()`, which does a Kitesurf HTTP probe and
      the uncached vision-model pick, on every home-page load.
      Fix: cache the status or render it asynchronously.
- [~] G-P13 [sweep] Every start-page load re-reads history twice
      and all page-memory chunks. `webui.py:168,180,197` call
      `today_pages()`, `grouped_history()`, `journeys()`
      (history.json parsed twice, page-memory fully parsed),
      while the brief only uses `chunks[0]`. Fix: load history
      once per request and add a metadata-only page listing.
- [~] G-P14 [sweep] Every navigation rewrites the entire
      page-memory file. `memory.py:440-456` + `app.py:603` —
      `remember_page` loads and re-saves up to 100 pages × 24
      chunks (megabytes of JSON) per page visit, on a fresh
      thread each time. Fix: debounce/coalesce saves, or
      append-only storage with periodic compaction.
- [~] G-P15 [sweep] Every embedding call that adds entries rewrites
      the whole vector cache file. `memory.py:304-321` — `_save`
      dumps up to 1500 vectors as JSON on each `vectors_for`
      with misses. Fix: save lazily (on session end /
      periodically) or append incrementally.
- [~] G-P16 [sweep] Page recall scores up to 2,400 vectors with
      pure-Python cosine. `memory.py:513` + `_cosine` at
      `memory.py:253` — up to 100 pages × 24 chunks × 768 dims
      of interpreted arithmetic per recall. Fix: precompute
      vector norms once per call (or keep a small norm cache).
- [~] G-P17 [sweep] A link hover downloads the entire target file
      to show two lines. `preview.py:188` — the preview path
      calls `fetch_any`, which streams the whole file and reads
      text kinds up to 32 MB into memory, so hovering a big PDF
      costs a full download. Fix: cap preview fetches (e.g.
      Range request or a small `max_bytes`) and excerpt from
      the head.
- [~] G-P18 [sweep] Settings/seats/site-lists are re-read from
      disk on every call, with no cache. `settings.py:63`,
      `seats.py:57`, `memory.py:74` — hit per search, per tab
      title change, per page load, per hover dwell, and per
      chat turn. Fix: cache parsed contents with mtime-based
      invalidation.
- [~] G-P19 [sweep] (low) A screenshot is held ~3× over in
      memory. `cdp.py:669` — the base64 string, the decoded
      bytes, and the enclosing JSON text coexist, plus `_ws`
      buffer copies. Fix: stream-decode or release the JSON
      text before decoding (minor on typical PNG sizes).
- [~] G-P20 [sweep] (low) Download records are never removed.
      `app.py:327` — `self._downloads` (holding
      `QWebEngineDownloadRequest` objects) grows for the app's
      life. Fix: prune finished records (or cap the dict).
- [~] G-P21 [sweep] (low) History file is rewritten on every page
      load. `titles.py:305` — `record_visit` loads and re-saves
      all 200 entries per `loadFinished`. Fix: debounce the save
      or batch rapid visits.
- [~] G-P22 [sweep] (low) Tab grouping recomputes centroids from
      scratch per comparison. `titles.py:208` — rebuilt for
      every item×group pair (O(n²·d)). Fix: maintain running
      centroids per group.
- [~] G-P23 [sweep] (low) Card files are re-read and re-parsed on
      every chat turn. `chat.py:767` — `list_cards()` reads
      every card file from disk per turn. Fix: cache the parsed
      list with mtime invalidation.
- [~] G-P24 [sweep] (low) Image describing reads the whole file
      with no size cap. `media.py:127-129` —
      `describe_saved_image` slurps the file before
      base64-encoding it into a POST body. Fix: refuse or
      downscale images over a size cap before sending.

### G-SEC — security / privacy

- [~] G-S1 [sweep] Debug-report scrubbing misses JSON-style
      secrets and unscrubbed tracebacks, while the UI promises
      "nothing in it carries passwords or keys".
      `debug_log.py:69-90` (the `key[:=]value` pattern cannot
      match `"password": "…"`, and bare `token` is not in the
      alternation), `debug_log.py:313` (`exception()` scrubs
      only the message — the attached traceback is formatted
      unscrubbed), `debug_log.py:403,409` (ring-buffer and error
      sections go into the report without the re-scrub that
      only the log tail gets). Fix: add a `"key"\s*:\s*"value"`
      JSON pattern plus bare `token`, and run `scrub_text` over
      ring/error records at report time.
- [~] G-S2 [sweep] Sensitive-page detection is URL-keyword
      matching, so real banking/health visits are recorded in
      plaintext history and page memory. `memory.py:31-52`,
      `titles.py:306-322`, `memory.py:440-460`. Page content is
      never considered, so a bank or clinic on a neutral domain
      is stored as full text chunks. Fix: add a maintained list
      of sensitive host patterns (bank/health/government
      domains) and treat pages whose visible text looks like an
      account/checkout page as sensitive.
- [~] G-S3 [sweep] Ollama/SearXNG addresses accept any remote host
      with no validation or warning, redirecting all AI traffic
      off-machine. `app.py:2546-2563` (address fields),
      `app.py:2592-2605` (`save()` stores them unvalidated),
      `chat.py:274-278` (prompts, page text, memory facts,
      images POSTed to that host). One typo or paste sends
      browsing data to a stranger's server with no confirmation.
      Fix: validate the URL at save time and require an explicit
      warning dialog when the host is not loopback.
- [~] G-S4 [sweep] Research reports allow `javascript:`/`data:`
      links, rendered in the privileged local-server origin.
      `research.py:409-419` (`_inline` linkifies `[text](url)`
      with no scheme allowlist), `webui.py:322-330` (served
      `/research/<name>` pages run on the same origin as
      `/history-clear?confirm=1`). A prompt-injected page could
      plant a link in a report that, clicked, runs script in
      the local origin. Fix: allow only `http(s)` hrefs in
      `_inline` and render report links with `rel="noopener"`.
- [~] G-S5 [sweep] (low) Element snapshots capture
      password-field values. `cdp.py:65-70` (`_ELEMENTS_JS`
      reads `e.value` for all inputs including
      `type=password`), `cli.py:117-121` (`snapshot` prints
      every element's text verbatim). Fix: return `""` for the
      text of password (and hidden) inputs in `_ELEMENTS_JS`.
- [~] G-S6 [sweep] (low) The local page server has no auth, and
      history wipe is a GET with no token. `webui.py:390-395`
      (`/history-clear?confirm=1` wipes on GET), `webui.py:486-496`
      (binds 127.0.0.1, random port, no token, no security
      headers). Any local process that finds the port can read
      history/research/bookmarks or wipe history. Fix: require
      a per-launch token for state-changing routes and add
      `X-Content-Type-Options`/`Content-Security-Policy`
      headers.
- [~] G-S7 [sweep] (low) History, memory, page memory, and
      session are plaintext 0644 while the vault is 0600.
      `session.py:52-58`, `titles.py:295-303`,
      `memory.py:185-196` (plain `open()` writes) vs
      `vault.py:132` (0600). These files hold browsing history,
      personal facts, and full page text. Fix: write the
      `~/.forge` stores with 0600 and the folder 0700, like the
      vault.
- [~] G-S8 [sweep] (low) DNS-pinning fail-open: a name that fails
      to resolve is connected to unchecked. `_http.py:169`
      (`_resolve_checked_host` returns `[]` on resolution
      failure), `_http.py:326-327` (empty pin falls back to
      `socket.create_connection` on the hostname — a fresh,
      unchecked resolution). Fix: when the pin resolution
      fails, fail the fetch instead of connecting unpinned.
- [~] G-S9 [sweep] (low) `BrowserSession.navigate` itself is
      unfenced; `barriers.handle_page` calls it directly.
      `cdp.py:540-548` (no scheme or fence check in `navigate`),
      `barriers.py:227` (direct call). All *live* paths fence
      at `render.py`/`fetch.py`, so this is defense-in-depth
      only today. Fix: fence inside `navigate` (or assert
      callers have fenced) so a future caller cannot bypass it.
- [~] G-S10 [sweep] (low) Autofill does not re-verify the tab
      still shows the entry's site before filling.
      `app.py:1264-1291` (lookup uses the captured URL, but
      `_fill_login` fills the live view after seconds of modal
      dialogs, during which the page could have navigated or
      redirected). Fix: check `view.url()` still matches the
      entry's host immediately before filling.
- [~] G-S11 [sweep] (low) `verify_in_browser` serves the whole
      workspace directory, with listings and symlink escape.
      `verify.py:60-75` (`SimpleHTTPRequestHandler` rooted at
      the workspace serves every file plus directory listings;
      the traversal check uses `abspath`, not `realpath`, so a
      symlink inside the workspace can point outside it). Fix:
      serve only the single target file and resolve symlinks
      before the containment check.

### G-UX — UX / missing function

- [~] G-UX1 [sweep] No History menu or shortcut; history is
      reachable only from the start page. `app.py:846-877`
      (menus are File/Edit/View/Bookmarks/Tools/Help — no
      History, no Ctrl+H). Fix: add a History menu with "Show
      All History" (Ctrl+H) and "Clear History…".
- [~] G-UX2 [sweep] Standard browser shortcuts are missing:
      Ctrl+L, Ctrl+R/F5, Ctrl+Tab, Ctrl+Shift+T. Shortcut
      inventory is only Escape, Ctrl+F, Ctrl+T/W/Q, Ctrl+D,
      and zoom. Fix: add Ctrl+L (focus address bar), Ctrl+R
      (reload), Ctrl+Tab/Ctrl+Shift+Tab (tab switching), and
      Ctrl+Shift+T (reopen closed tab).
- [~] G-UX3 [sweep] Ctrl+D is dead until the Bookmarks menu is
      first opened, then duplicates on every open.
      `app.py:866-868` (the Ctrl+D action is created only in
      `_rebuild_bookmarks_menu`, wired to `aboutToShow`),
      `app.py:1018-1030` (`menu.clear()` removes but does not
      delete the window-parented actions), so each menu open
      leaks another live Ctrl+D shortcut. Fix: build the
      Bookmarks menu once (or parent its actions to the menu so
      `clear()` deletes them).
- [~] G-UX4 [sweep] A closed chat dock cannot be re-shown, and
      Settings' "Show the chat dock" checkbox does not show or
      hide it. The dock is closable by default, the View menu
      has only Zoom, `closeEvent` persists the closed state via
      `saveState`, and `apply_ai_state` (`app.py:1675-1705`)
      never calls `setVisible`. Fix: add a View → Chat toggle
      wired to dock visibility, and relabel the Settings
      checkbox to what it actually toggles. EXTENDED 2026-11-15
      by Joshua (looking at v0.9 on his PC): the dock must ALSO
      be freely resizable by dragging, with a sane minimum
      width, and the size must persist across restarts.
- [~] G-UX5 [sweep] No way to start Ollama or pull a model from
      the app; the header prescribes terminal-only fixes.
      `app.py:1374-1381` ("Start Ollama, then ↻"),
      `app.py:2199` ("Run install.sh to pull it"). Fix: add
      "Start Ollama" and "Get a chat model" actions to the dock
      header's no-brain states.
- [~] G-UX6 [sweep] Failed loads show Qt's technical error page,
      and failed loads are recorded as history.
      `app.py:450-451` (`loadFinished` connected with the `ok`
      flag ignored), `app.py:586-600` (records the visit without
      checking success). A typo'd address yields
      `ERR_NAME_NOT_RESOLVED` and a history entry for a page
      never seen. Fix: intercept failed loads with a friendly
      page ("Could not reach X — check the address or
      connection") and skip them in history.
- [~] G-UX7 [sweep] "Cancel download" is unusable during an
      active download. `app.py:1100-1107` (every
      `receivedBytesChanged` calls `_refresh_downloads_list`),
      `app.py:1127-1132` (which does `listing.clear()` and
      rebuilds, dropping the selection), `app.py:1195-1201`
      (`cancel_selected` reads `currentRow()`, which is −1
      after a rebuild). Fix: update list items in place instead
      of clearing, preserving the selection.
- [~] G-UX8 [sweep] Address bar forces `https://` onto
      localhost/loopback addresses with ports.
      `webui.py:336-351` (`resolve_address`: `localhost:8888`
      and `127.0.0.1:9222` both become `https://…`, which fails
      — local servers are plain HTTP). Fix: use `http://` for
      `localhost`, loopback, and `.local` hosts.
- [~] G-UX9 [sweep] Chat log is unbounded with no clear/export,
      history is lost on restart, and the entry is single-line
      and disabled mid-turn. `app.py:1436` (no
      `maximumBlockCount`), `app.py:1444-1448` (no Clear
      button), `chat.py:519` (history is in-memory only),
      `app.py:1470-1475` (single-line `QLineEdit`),
      `app.py:2455-2462` (entry disabled for the whole turn).
      Fix: cap the log, add Clear/Export, persist chat history,
      and use a multi-line entry that stays editable while a
      turn runs.
- [~] G-UX10 [sweep] Browser downloads are not resumable or
      retryable (U24). `app.py:1140-1160` (an interrupted
      download deletes its partial file and ends as "Failed"
      with no retry control); the downloads dialog
      (`app.py:1180-1210`) offers only Cancel and Open-folder.
      The engine-room resume in `_http.py` does not apply to
      WebEngine downloads. Fix: keep the partial file on
      failure and add a Retry button that resumes it.
- [~] G-UX11 [sweep] (low) Closing the last tab silently does
      nothing. `app.py:611-620` (`_close_tab` only acts when
      `count > 1`; no `else`). Fix: closing the last tab should
      open a fresh start-page tab.
- [~] G-UX12 [sweep] (low) No tab context menu, no
      reopen-closed-tab, no pin/duplicate. No
      `setContextMenuPolicy`/context-menu code exists for the
      tab bar; a closed tab is gone except via session restore
      on next launch. Fix: add a right-click tab menu (Close,
      Close Others, Duplicate, Reopen Closed Tab).
- [~] G-UX13 [sweep] (low) Downloads dialog: no Open/Show-in-
      folder per file, and failures say only "Failed".
      `app.py:1112-1124`, `app.py:1180-1210`. Fix: keep the
      failure reason on the record and add per-file Open and
      Show-in-folder actions.
- [~] G-UX14 [sweep] (low) Find shows no match count.
      `app.py:960-968` (`_on_find_result` sets the label to `""`
      or `"No matches"` only). Fix: show "3 of 12 matches" from
      the find result.
- [~] G-UX15 [sweep] (low) Autofill is never offered for
      password-only forms. `app.py:1222-1258` (the probe returns
      `'password-only'` when there is no username field, and
      `_on_login_form_found` returns early unless the result is
      `'login'`). Fix: offer the fill for password-only forms
      too, filling just the password.
- [~] G-UX16 [sweep] (low) Bookmarks cannot be renamed or
      edited. `app.py:1021-1068` (the dialog offers only Open
      and Remove). Fix: add Rename (and let the star toggle
      bookmark/unbookmark).
- [~] G-UX17 [sweep] (low) Address-bar placeholder uses insider
      jargon ("keyless"). `app.py:390-394` ("Type an address, or
      words to search. Forge Browser searches keyless"). Fix:
      drop "keyless" — "Type an address, or words to search" is
      enough.
- [~] G-UX18 [sweep] (low) Per-site memory controls have no
      window UI. `memory.py` implements block/allow per site,
      but the Memory dialog (`app.py:1509-1577`) shows only
      facts and pages — the privacy control is CLI/chat-only.
      Fix: add a per-site block/allow list to the Memory dialog.
- [~] G-UX19 [sweep] (low) Voice/transcription failures prescribe
      terminal fixes. `app.py:2041-2056` ("Install whisper or
      set `FORGE_TRANSCRIBE_CMD`"), `app.py:2199` ("Run
      install.sh to pull it"). Fix: point at the GUI installer
      or an in-app fix instead of naming env vars and scripts.

### G-TEST — test gaps

- [~] G-T1 [sweep] Single-instance has no behavioral test, and
      the cited "offscreen smoke run" does not exist in the
      repo. `tests/test_browser2.py:3195-3204` only asserts
      source strings; its docstring claims Qt behavior "is
      proven by the offscreen smoke run", but grep finds no
      smoke/offscreen script anywhere in the tree. Fix: add a
      Qt-offscreen smoke script (or a test driving
      `_take_single_instance`/`_handle_show_request` under
      offscreen Qt) to the repo.
- [~] G-T2 [sweep] Fence pinning lacks the flip-to-local-at-
      pin-time test and any IPv6 test. `tests/test_browser.py:
      615-848` covers flip-to-public, redirect, fall-through,
      all-fail, HTTPS SNI, literals, unresolvable, and proxy —
      but never the case where the pin-time resolution itself
      returns a local address, which `_resolve_checked_host`
      refuses (`_http.py:178-186`); every pin test uses IPv4
      literals although IPv6 addresses are also returned. Fix:
      add a flip-to-127.0.0.1-on-second-resolution test and an
      IPv6 pin test.
- [~] G-T3 [sweep] `DocsTests` cannot catch stale counts; the
      "50/50" and "493/493" pass the suite.
      `tests/test_browser2.py:2869-2899` only asserts the
      current count string (`"534 checks"`) appears in four
      files; it never rejects stale counts. Fix: assert no
      `NN/NN pass` or older count string survives in
      `BROWSER.md`/`README.md`.
- [~] G-T4 [sweep] `run.sh check` is tested only for the
      no-Ollama path. `tests/test_browser2.py:3353-3365` tests
      only "not run: no Ollama answering"; the Ollama-answering
      branches in `run.sh:33-45` (validator PASS, and
      validator-ran-but-failed → exit 1) have no test, nor do
      `run.sh demo` / `run.sh test` passthrough. Fix: add a test
      with a stub Ollama answering on `FORGE_OLLAMA_URL` plus
      stub validators covering PASS and FAIL.
- [~] G-T5 [sweep] (low) Barrier handling lacks captcha/paywall
      `handle_page` tests and any `scroll_page` test.
      `tests/test_browser2.py:1666-1816` covers consent,
      age-gate clearing, and login fill, but no test drives
      `handle_page` on a captcha page (must report
      `needs_human: True` and never clear) or a paywall page,
      and `scroll_page` has zero test references. Fix: add
      captcha/paywall `handle_page` tests with a fake session
      plus a `scroll_page` test.
- [~] G-T6 [sweep] (low) How to Use has no negative or fallback
      tests. `tests/test_browser2.py:3158-3170` tests only that
      the start page *links* `/howto` on first run — never that
      it omits the link when history exists, and `find_manual`'s
      fallbacks (`howto.py:26-33`) are untested. Fix: add a
      with-history start-page test and a fallback-path
      `find_manual` test.
- [~] G-T7 [sweep] (low) The app's debug-log wiring is untested.
      `tests/test_browser2.py:2962-3087` (DebugLogTests)
      exercises `DebugLogger` with fake app names only; nothing
      pins that `get_debug_log()` passes `("forge-browser",
      __version__)`, logs under `~/.forge/logs`, and registers
      Ollama/PySide6 (`app.py:131-146`). Fix: add a test calling
      `get_debug_log()` with a stubbed home asserting app name,
      version, and log dir.
- [~] G-T8 [sweep] (low) Several tests assert on source text or
      mocks instead of behavior. `AppWiringTests` and most of
      `InstallerTests` assert substrings in files;
      `test_transcribe_recording_delegates` asserts a mock
      returns its own canned value;
      `test_cli_render_accepts_engine_flag` asserts mock kwargs.
      Each passes while the real path is broken — D5/D6 are the
      proof for the script-grep class. Fix: keep them as wiring
      pins but pair each with one behavioral test.
- [~] G-T9 [sweep] (low) `describe_image_bytes` Ollama-error path
      is untested. `media.py:119-125` returns `""` on any Ollama
      exception, but `MediaSeatTests` only tests the success path
      and seat preference. Fix: add a test with a fake Ollama
      that errors on `/api/generate` asserting `""`.
- [~] G-T10 [sweep] (low) `test-forge-browser.sh`,
      `browser-setup.sh`, and `install-gui.py` behavior is
      untested. Only `sh -n` and `py_compile` plus the count
      string; the guided script's 8 steps, the
      `browser-setup.sh` lightpanda/searxng paths, and the GUI
      installer's behavior have no tests. Fix: add a smoke test
      running `test-forge-browser.sh` step 1 (suite) in
      isolation, or document them as manual-only.

## Counts

173 items in 172 entries (E6 holds F1+F2): A6 + B15 + C14 + D29
+ E8 + G100 (code 36 + perf 24 + sec 11 + ux 19 + tests 10).
Blocked: E1–E8 (his PC, his GitHub billing, or a design decision)
— 9 items in 8 entries.
Built in v0.10: 164 items (G-C30 checked, not a real issue).
Buildable now: 0 items.
