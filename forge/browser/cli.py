"""Command line for Forge Browser: ``python3 -m forge.browser ...``

    search "query" [--max 5] [--json]        keyless web search
    fetch URL [--offset N] [--max-chars N]   page -> clean Markdown
    fetch-any URL [--json]                   any file type -> text/result
    render URL [--json]                      page after JavaScript (CDP engine)
    snapshot URL                             accessibility tree + elements
    screenshot URL --out shot.png            PNG of the rendered page
    describe FILE                            vision-seat description of an image
    transcribe FILE                          local transcription of audio
    research "topic" [--queries "a,b"]       prints the plan (the gate)
    research "topic" --yes [--out report.md] runs the approved plan
    verify page.html --workspace DIR         #65-style project page check
    engines                                  what this machine has
    vault create|list                        the encrypted login vault
    vault add SITE                           prompts for user + password
    vault remove SITE

Human-readable output by default; --json prints the exact dicts the
library returns. Vault passwords are prompted with no echo and are
never accepted as command-line arguments. Errors print one plain
line to stderr and exit 2.
"""

import argparse
import getpass
import json
import os
import sys
import time

from .errors import BrowserError
from .fetch import fetch
from .search import search


def _print_json(data):
    print(json.dumps(data, indent=2))


def _cmd_search(args):
    results = search(args.query, max_results=args.max)
    if args.json:
        _print_json([r.to_dict() for r in results])
    elif not results:
        print("No results.")
    else:
        for index, result in enumerate(results, 1):
            print(f"{index}. {result.title}")
            print(f"   {result.url}")
            if result.snippet:
                print(f"   {result.snippet}")
    return 0


def _cmd_fetch(args):
    page = fetch(args.url, max_chars=args.max_chars, offset=args.offset)
    if args.json:
        _print_json(page.to_dict())
        return 0
    print(f"# {page.title or page.final_url}")
    print(f"Source: {page.final_url}")
    print()
    print(page.markdown)
    if page.truncated:
        print()
        print(f"[... {page.total_chars - (page.offset + len(page.markdown))} more chars — "
              f"fetch again with --offset {page.next_offset}]")
    return 0


def _cmd_fetch_any(args):
    from .files import fetch_any
    result = fetch_any(args.url)
    if args.json:
        _print_json(result.to_dict())
        return 0
    print(f"{result.kind}: {result.final_url} ({result.size_bytes} bytes)")
    if result.saved_path:
        print(f"Saved: {result.saved_path}")
    for note in result.notes:
        print(f"Note: {note}")
    if result.text:
        print()
        print(result.text)
    return 0


def _cmd_render(args):
    from .render import render
    page = render(args.url, max_chars=args.max_chars, offset=args.offset)
    if args.json:
        _print_json(page.to_dict())
        return 0
    print(f"# {page.title or page.final_url}")
    print(f"Source: {page.final_url} (rendered)")
    print()
    print(page.markdown)
    if page.truncated:
        print(f"\n[... more — render again with --offset {page.next_offset}]")
    return 0


def _cmd_snapshot(args):
    from .render import snapshot
    data = snapshot(args.url)
    if args.json:
        _print_json(data)
        return 0
    print(f"# {data['title'] or data['url']}  (engine: {data['engine']})")
    print(data["snapshot"])
    print()
    print("Elements:")
    for element in data["elements"]:
        print(f"[{element['i']}] {element['tag']} {element['text']}".rstrip())
    return 0


def _cmd_screenshot(args):
    from .render import screenshot
    path = screenshot(args.url, args.out)
    print(f"Screenshot saved: {path}")
    return 0


def _cmd_describe(args):
    from .media import describe_saved_image, pick_vision_model
    if not pick_vision_model():
        raise BrowserError(
            "no local vision seat is configured — Ollama is not answering "
            "or no vision model is installed (see BROWSER.md: vision seat)")
    text = describe_saved_image(args.file)
    if not text:
        raise BrowserError("the vision seat returned no description")
    print(text)
    return 0


def _cmd_transcribe(args):
    from .media import transcribe_saved_audio
    text = transcribe_saved_audio(args.file)
    if not text:
        raise BrowserError(
            "no local transcription backend answered — install whisper or "
            "set FORGE_TRANSCRIBE_CMD (see BROWSER.md)")
    print(text)
    return 0


def _cmd_research(args):
    from .research import format_plan, make_plan, run_research
    queries = [q.strip() for q in (args.queries or "").split(",") if q.strip()] or None
    plan = make_plan(args.topic, queries=queries, max_pages=args.max_pages)
    if not args.yes:
        print(format_plan(plan))
        print()
        print("Plan only — nothing was searched. Run again with --yes to approve and run it.")
        return 0
    report, _sources = run_research(plan, approved=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"Report written: {args.out}")
    else:
        print(report)
    return 0


def _cmd_verify(args):
    from .verify import verify_in_browser
    result = verify_in_browser(args.target, args.workspace)
    if args.json:
        _print_json(result)
        return 0
    print(f"Verified: {result['target']} (rendered: {result['rendered']})")
    if result.get("screenshot"):
        print(f"Screenshot: {result['screenshot']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
    print()
    print(result["text"])
    return 0


def _cmd_engines(args):
    from .engines import engine_status, format_status
    status = engine_status()
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return 0


def _vault_passphrase(confirm=False):
    phrase = os.environ.get("FORGE_BROWSER_VAULT_PASSPHRASE", "")
    if phrase:
        return phrase
    phrase = getpass.getpass("Vault passphrase: ")
    if confirm:
        again = getpass.getpass("Repeat passphrase: ")
        if phrase != again:
            raise BrowserError("the passphrases did not match")
    return phrase


def _cmd_vault(args):
    from .vault import Vault, default_vault_path
    path = default_vault_path()
    if args.action == "create":
        Vault.create(path, _vault_passphrase(confirm=True))
        print(f"Vault created: {path}")
        return 0
    vault = Vault.open(path, _vault_passphrase())
    if args.action == "list":
        entries = vault.list_entries()
        if not entries:
            print("The vault is empty.")
        for entry in entries:
            print(f"{entry['site']} — user: {entry['username']}"
                  + (f" ({entry['notes']})" if entry["notes"] else ""))
        return 0
    if args.action == "add":
        username = input(f"Username for {args.site}: ").strip()
        password = getpass.getpass("Password (not shown): ")
        notes = input("Notes (optional): ").strip()
        vault.set(args.site, username, password, notes)
        print(f"Saved: {args.site}")
        return 0
    if args.action == "remove":
        print("Removed." if vault.delete(args.site) else "No such entry.")
        return 0
    raise BrowserError(f"unknown vault action {args.action}")


def _cmd_chat(args):
    from .chat import ChatSession, LiveDisplay
    from .seats import load_seats
    seats = load_seats()
    if not seats["ai_enabled"] or not seats["features"]["chat"]:
        print("AI is off — turn it back on in the browser's chat dock "
              "(or delete ~/.forge/seats.json).")
        return 0
    session = ChatSession()
    print(f"Forge Browser chat — brain: {session.model or 'none'} "
          "(type quit to leave)")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in ("quit", "exit"):
            return 0
        if not text:
            continue
        display = LiveDisplay()
        turn_started = time.monotonic()

        def on_chunk(delta):
            shown = display.feed(delta)
            if shown:
                print(shown, end="", flush=True)

        def ask_plan(plan_text):
            print(f"\nPlan: {plan_text}")
            try:
                reply = input("Allow it? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return reply in ("y", "yes")

        def on_progress(text):
            print(f"  [research] {text}", flush=True)

        def on_research_done(report, sources, error=None):
            if error:
                print(f"\n[research failed] {error}")
            else:
                print(f"\n[research done]\n{report}")

        def on_action(kind, action):
            elapsed = int(time.monotonic() - turn_started)
            print(f"  … {kind} ({elapsed}s)", flush=True)

        try:
            _answer, opens = session.ask(
                text, on_action=on_action,
                on_chunk=on_chunk, on_plan=ask_plan,
                on_progress=on_progress, on_research_done=on_research_done)
        except Exception as exc:
            print(f"forge browser: {exc}")
            continue
        # The answer already streamed above, line by line; only
        # the held remainder is left to print.
        rest = display.flush()
        if rest:
            print(rest, end="", flush=True)
        print(f"\n(took {int(time.monotonic() - turn_started)}s)")
        for url in opens:
            print(f"  (open in a tab in the window: {url})")


def _cmd_cards(args):
    from .cards import list_cards
    cards = list_cards()
    if not cards:
        print("no cards — built-ins live in forge/browser/cards/, "
              "yours in ~/.forge/browser-cards/")
        return 0
    for card in cards:
        print(f"{card.name:24} {card.title}")
    return 0


def _cmd_card(args):
    from .cards import run_card
    from .chat import ChatSession, LiveDisplay
    session = ChatSession()
    display = LiveDisplay()

    def on_chunk(delta):
        shown = display.feed(delta)
        if shown:
            print(shown, end="", flush=True)

    try:
        run_card(session, args.name, question=args.question or "",
                 on_action=lambda kind, a: print(f"  … {kind}"),
                 on_chunk=on_chunk)
    except Exception as exc:
        print(f"forge browser: {exc}")
        return 1
    rest = display.flush()
    if rest:
        print(rest, end="", flush=True)
    print()
    return 0


def _cmd_memory(args):
    from .memory import export_memory, forget, list_entries, remember, search_entries
    if args.action == "add":
        if not args.value:
            print("usage: memory add <what to remember>")
            return 1
        entry = remember(args.value, source="cli")
        print(f"remembered: {entry['text']}")
        return 0
    if args.action == "forget":
        removed = forget(args.value)
        print(f"forgot {removed} entr{'y' if removed == 1 else 'ies'}"
              if removed else "nothing matched that")
        return 0
    if args.action == "export":
        path = export_memory(
            args.value or os.path.expanduser("~/.forge/memory-export.json"))
        print(f"exported {len(list_entries())} entries to {path}")
        return 0
    if args.action == "search":
        from .retrieve import embed_texts
        found = search_entries(args.value, top_k=5, embed_fn=embed_texts)
        for entry in found:
            print(f"- {entry['text']}")
        if not found:
            print("nothing relevant remembered")
        return 0
    if args.action == "pages":
        from .memory import list_pages, search_pages
        if args.value:
            from .retrieve import embed_texts
            found = search_pages(args.value, embed_fn=embed_texts)
            for entry in found:
                print(f"{entry['title']} — {entry['url']}\n  {entry['text'][:200]}")
            if not found:
                print("nothing in your visited pages matches that")
        else:
            for page in list_pages():
                print(f"{page['title']} — {page['url']} ({page['chunks']} chunks)")
            if not list_pages():
                print("no pages remembered yet")
        return 0
    entries = list_entries()
    if not entries:
        print("nothing remembered yet — say 'remember ...' in chat, "
              "or: memory add <text>")
        return 0
    for entry in entries:
        print(f"{entry['id']}  {entry['text']}")
    return 0


def _cmd_voice(args):
    import time
    from .voice import Recorder, find_recorder, transcribe_recording
    if find_recorder() is None:
        print("no microphone recorder found (arecord, parecord, or ffmpeg)")
        return 1
    recorder = Recorder()
    if not recorder.start():
        print("couldn't start the microphone")
        return 1
    print(f"recording for {args.seconds}s — speak now…")
    time.sleep(args.seconds)
    path = recorder.stop()
    if path is None:
        print("nothing recorded — check the microphone")
        return 1
    text = transcribe_recording(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    print(text or "couldn't make out anything in the recording")
    return 0


def _cmd_brief(args):
    from .brief import brief_text, build_brief
    from .chat import ChatSession
    session = ChatSession()
    print(brief_text(build_brief(session if session.model else None)))
    return 0


def _cmd_preview(args):
    from .preview import preview_url
    text = preview_url(args.url)
    print(text or "nothing to preview for that link")
    return 0


def _cmd_app(args):
    from .app import run_app
    return run_app(args.url)


def _cmd_seats(args):
    """The dock picker, from the terminal: `seats` lists the picks
    and toggles; `seats brain qwen3:4b` picks a brain; `seats brain
    auto` clears the pick; `seats ai off` kills all AI features."""
    from .seats import load_seats, set_feature, set_seat
    seats = load_seats()
    if args.what is None:
        for key in ("brain", "fast", "vision"):
            print(f"{key:8} {seats[key] or '(auto)'}")
        print(f"ai: {'on' if seats['ai_enabled'] else 'off'}")
        for name, on in seats["features"].items():
            print(f"{name:8} {'on' if on else 'off'}")
        return 0
    if args.what in ("brain", "fast", "vision"):
        if args.value is None:
            print(f"{args.what}: {seats[args.what] or '(auto)'}")
            return 0
        set_seat(args.what, None if args.value == "auto" else args.value)
        print(f"{args.what}: {args.value}")
        return 0
    if args.what in ("ai", "chat", "tidy_titles", "memory"):
        if args.value not in ("on", "off"):
            print(f"usage: seats {args.what} on|off")
            return 1
        set_feature(args.what, args.value == "on")
        print(f"{args.what}: {args.value}")
        return 0
    print(f"unknown seat or feature: {args.what}")
    return 1


def _cmd_open(args):
    from .cdp import open_browser_window
    result = open_browser_window(args.url)
    if result["started"]:
        print(f"Forge Browser window opened — showing {result['url']}")
    else:
        print(f"Forge Browser window was already open — took it to {result['url']}")
    print(f"Anything can drive this window at {result['endpoint']} "
          "(Code Forge will, later). Close the window when you're done.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="forge.browser",
        description="Forge Browser — Code Forge's own keyless, multimodal web layer.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("chat", help="talk to the browser in the terminal (local model)")
    p.set_defaults(func=_cmd_chat)

    p = sub.add_parser("cards", help="list the saved workflow cards")
    p.set_defaults(func=_cmd_cards)

    p = sub.add_parser("card", help="run one saved workflow card")
    p.add_argument("name", help="card name, e.g. summarize-page")
    p.add_argument("question", nargs="?", default="",
                   help="the question or topic the card works on")
    p.set_defaults(func=_cmd_card)

    p = sub.add_parser("memory", help="the remembered facts (list/add/forget/search/export/pages)")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "forget", "search", "export", "pages"])
    p.add_argument("value", nargs="?", default="",
                   help="the fact, the search, or the export path")
    p.set_defaults(func=_cmd_memory)

    p = sub.add_parser("brief", help="the morning brief — what yesterday's browsing was about")
    p.set_defaults(func=_cmd_brief)

    p = sub.add_parser("preview", help="two lines about where a link goes (fetched on demand)")
    p.add_argument("url")
    p.set_defaults(func=_cmd_preview)

    p = sub.add_parser("seats", help="the model seats + AI toggles (same as the dock picker)")
    p.add_argument("what", nargs="?", default=None,
                   choices=["brain", "fast", "vision", "ai", "chat",
                            "tidy_titles", "memory"],
                   help="seat or feature to show/set")
    p.add_argument("value", nargs="?", default=None,
                   help="model name, 'auto', or on|off")
    p.set_defaults(func=_cmd_seats)

    p = sub.add_parser("voice", help="record the microphone and transcribe it (local whisper)")
    p.add_argument("seconds", nargs="?", type=int, default=5,
                   help="how long to record (default 5)")
    p.set_defaults(func=_cmd_voice)

    p = sub.add_parser("app", help="Forge Browser the program — its own browser window")
    p.add_argument("url", nargs="?", default=None, help="page to start on")
    p.set_defaults(func=_cmd_app)

    p = sub.add_parser("open", help="open the browser itself — a real window on your desktop")
    p.add_argument("url", nargs="?", default="https://lite.duckduckgo.com/lite/",
                   help="page to open on (default: the keyless search page)")
    p.set_defaults(func=_cmd_open)

    p = sub.add_parser("search", help="search the web (no API key)")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("fetch", help="fetch one page as clean Markdown")
    p.add_argument("url")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_fetch)

    p = sub.add_parser("fetch-any", help="fetch any file type (PDF, image, audio, data...)")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_fetch_any)

    p = sub.add_parser("render", help="fetch the page after JavaScript, via a local engine")
    p.add_argument("url")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("snapshot", help="accessibility snapshot + numbered elements")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("screenshot", help="save a PNG screenshot of a rendered page")
    p.add_argument("url")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_screenshot)

    p = sub.add_parser("describe", help="describe an image with the local vision seat")
    p.add_argument("file")
    p.set_defaults(func=_cmd_describe)

    p = sub.add_parser("transcribe", help="transcribe audio with a local backend")
    p.add_argument("file")
    p.set_defaults(func=_cmd_transcribe)

    p = sub.add_parser("research", help="plan-gated research with a cited report")
    p.add_argument("topic")
    p.add_argument("--queries", default="", help="comma-separated searches (default: the topic)")
    p.add_argument("--max-pages", type=int, default=6)
    p.add_argument("--yes", action="store_true", help="approve the plan and run it")
    p.add_argument("--out", default="", help="write the report to this file")
    p.set_defaults(func=_cmd_research)

    p = sub.add_parser("verify", help="check a project page the #65 way")
    p.add_argument("target")
    p.add_argument("--workspace", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("engines", help="what browser power this machine has")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_engines)

    p = sub.add_parser("vault", help="the encrypted login vault")
    p.add_argument("action", choices=["create", "list", "add", "remove"])
    p.add_argument("site", nargs="?", default="")
    p.set_defaults(func=_cmd_vault)

    args = parser.parse_args(argv)
    if getattr(args, "action", "") in ("add", "remove") and not args.site:
        print("Forge Browser: vault add/remove need a SITE", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except BrowserError as exc:
        print(f"Forge Browser: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
