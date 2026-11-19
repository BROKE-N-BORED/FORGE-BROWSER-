# The doorway: browsing jobs for other programs

The doorway is a tiny local HTTP endpoint so another program on
this PC (Fyrn, the local model runner) can hand Forge Browser a
browsing job and get the result back. It runs from the CLI, needs
no window, and uses the engine room only: each task opens a page
in a real local engine, reads the rendered page as markdown, and
takes a real PNG screenshot.

## Start it

```
python3 -m forge.browser doorway
```

It listens on 127.0.0.1 port 18787 by default. `--port N` picks
another port; `FORGE_BROWSER_DOORWAY_PORT` does the same from the
environment (the flag wins). When the port is taken, the doorway
tries the next few ports. `--host` exists but only accepts
loopback addresses (127.0.0.1, localhost); anything else is
refused plainly.

The actual bound port is written to `~/.forge/doorway/port`, a
plain file with just the number, so a client can discover the
address instead of guessing. The file is removed when the doorway
stops. Ctrl+C (or SIGTERM) stops it cleanly.

## Local only

The doorway binds 127.0.0.1 only, so only programs on this
machine can reach it. There is no auth, no key, no token:
local-only binding is the protection.

Tasks are fenced the same way the browser's own fetches are: a
task URL must be http(s) and public. Local addresses
(127.0.0.1, localhost, LAN ranges) are refused with a plain
reason, so no program can browse the local machine through the
doorway. A page that needs a human (captcha, login) fails the
task plainly naming the barrier: the doorway never solves human
checks, same as the app.

## One task at a time

Tasks run one at a time, in submission order. A second task waits
queued; it does not fail. Each task opens a fresh engine session,
so one job can never see another job's cookies or storage.

## Submit a task

POST /task with a JSON body. Two kinds of task:

```json
{"type": "open", "url": "https://example.org"}
{"type": "research", "query": "local small language models"}
```

`open` navigates to the URL and reads the rendered page.
`research` searches (keyless, no API key), opens the best
result, and reads that page. One page per task: the full
multi-page research report is not part of the doorway. Submit
follow-up tasks when you want more.

The answer is the task id and its status:

```json
{"task_id": "9f2c41a0b7e5d318", "status": "queued"}
```

curl example:

```bash
curl -s -X POST http://127.0.0.1:18787/task \
  -H 'Content-Type: application/json' \
  -d '{"type": "open", "url": "https://example.org"}'
```

Python example (stdlib only; copy this):

```python
import json, time, urllib.request
from pathlib import Path

port = int(Path.home().joinpath(".forge/doorway/port").read_text().strip())
BASE = f"http://127.0.0.1:{port}"

def submit(task):
    body = json.dumps(task).encode()
    req = urllib.request.Request(BASE + "/task", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)

def status(task_id):
    with urllib.request.urlopen(BASE + "/task/" + task_id, timeout=10) as resp:
        return json.load(resp)

task = submit({"type": "open", "url": "https://example.org"})
task_id = task["task_id"]
for _ in range(120):
    current = status(task_id)
    if current["state"] in ("done", "failed"):
        break
    time.sleep(1)
print(json.dumps(current, indent=2))
```

## Poll a task

GET /task/<id> returns the live status. Poll it while a task
runs:

```json
{
  "task_id": "9f2c41a0b7e5d318",
  "type": "open",
  "state": "running",
  "phase": "opening https://example.org",
  "current_url": "https://example.org",
  "result": null,
  "error": null
}
```

`state` is one of `queued`, `running`, `done`, `failed`.
`phase` is plain words for what the task is doing right now:
`waiting`, `searching`, `opening <url>`, `reading the page`,
`done`, `failed`. `current_url` is the page being read (null
until the task starts opening). `result` appears when the state
is `done`; `error` carries the plain reason when it is `failed`.

GET /tasks lists recent tasks, most recent first, capped at 50.
A finished task stays pollable by id for at least an hour after
it finishes.

Errors are plain JSON with the right HTTP status, never a
traceback: bad JSON or a refused task is 400, an unknown task id
is 404, a wrong method is 405.

## The result

When the state is `done`, `result` looks like this:

```json
{
  "final_url": "https://example.org/",
  "markdown": "# Example Domain\n\n...",
  "markdown_path": "/home/xprnc/.forge/doorway/9f2c41a0b7e5d318/page.md",
  "screenshot_path": "/home/xprnc/.forge/doorway/9f2c41a0b7e5d318/shot.png",
  "truncated": false
}
```

`final_url` is where the page ended up (after redirects).
`markdown` is the rendered page as markdown, capped at 20,000
characters in the JSON answer; when it is cut, `truncated` is
true and the inline text ends with a plain note saying so. The
full text is always in `page.md` (itself capped at 1,000,000
characters, far above any real page). `screenshot_path` is a
real PNG of the rendered page. A `note` field appears only when
there is something to say about what was read, for example when
the page may be behind a paywall and what was read could be
preview text only.

Files live under `~/.forge/doorway/<task-id>/`:

- `shot.png`: the screenshot. It is written as soon as the page
  lands and updated again once the page is read, so a view window
  can show the live page by polling the file while the task runs.
  When a task fails on a human check, the shot still shows the
  gate, at the same path.
- `page.md`: the full markdown of the page.

Task files stay on disk until the person deletes them; only the
task listing is bounded.
