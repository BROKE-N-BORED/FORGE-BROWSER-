# Audit #11 — Research as a background job

Audited 2026-11-12 from the code (research.py job + runner +
report rendering, chat.py research ACTION, app.py progress label
+ on_research_done), the tests (ResearchJobTests), and live probes
of job refusal, report links, and the cancel API.

## What it is

Task #11 from the improvements list: the research runner already
works; give it a progress line and let it run while browsing
continues. Strictly sequential. Expect minutes. Built 2026-11-11.

## What it does

- start_research_job runs run_research on a daemon thread, one job
  at a time (a second start returns None); on_progress steps
  ("Searched...", "Reading N of M...") and on_done delivers
  (report, sources, error). research_job_running returns the
  running topic.
- The chat research ACTION starts the job and ends the turn at
  once; the dock shows a progress label and opens the report in a
  new tab on completion; the CLI prints progress and the finished
  report.
- report_to_html renders the Markdown report as a readable page
  for the tab.

## Strengths

- The turn does not wait: chat is free at once, browsing
  continues — the core promise, delivered.
- Strictly sequential: one job at a time, enforced by a lock (no
  parallel agents on a slow CPU — the right call).
- The refusal names the running topic, so the person knows what to
  wait for.
- Progress steps are concrete ("Reading 3 of 6: <title>") — not a
  spinner.
- Failures are reported in the report ("Could not be read") rather
  than hidden; a failed job delivers an error, not silence.
- The report is excerpts + citations, no synthesis — nothing
  invented.
- Tests cover background start + delivery and the second-job
  refusal; the Qt smoke proved chat-free + report tab + done line.

## Weaknesses (all verified by probe or code read)

1. A running job cannot be stopped. No cancel API exists (zero
   "cancel" in research.py), no UI. The dock's Stop cancels the
   chat turn, not the job — the job outlives the turn that started
   it. A job the person no longer wants runs to completion
   (minutes).
2. The second ask is refused, not queued. PROBED: start returns
   None while busy. The person must remember to re-ask. (The queue
   idea is whole-project task F1 — cross-ref, not duplicated here.)
3. The finished report steals focus. on_research_done calls
   new_tab(html=...), which sets the new tab current — the person
   is yanked from what they were reading when a background job
   lands. A background job's completion should not interrupt the
   foreground.
4. Source URLs in the report tab are not clickable. PROBED:
   report_to_html emits no <a href> for sources — the URLs are
   plain text. The person must copy-paste a URL to open it.
5. Reports are not saved anywhere. The report lives only in the
   tab; close the tab and it is gone (HTML tabs skip history). No
   file, no way back.
6. The report page is light-themed while the app is dark (same
   mismatch as #7's M7 — the fix should cover both pages' shared
   style).
7. Progress lags by one fetch: "Reading N of M" fires AFTER each
   fetch completes, so during a slow fetch (up to 30 s) the label
   shows the previous step. No "fetching URL..." state.
8. No elapsed time or ETA on the job — the progress label shows
   the step only, not how long it has been running.
9. No depth control: max_pages=6 and max_chars_per_source=4000
   are fixed; the chat ACTION has no way to ask for a bigger or
   smaller research.
10. Dedup is by exact URL only — the same content at http vs https,
    trailing-slash, or tracking-param variants is read twice.
    Minor.
11. Excerpts are the source's first 4000 chars (head-cut), not the
    relevant part. For a long source the excerpt may miss the
    relevant section. Minor.
12. The job is a daemon thread: closing the window mid-job loses
    it with no trace. No persistence or resume. Minor (acceptable
    for v1).

## UI flaws

- Focus steal on completion (weakness 3).
- Unclickable source URLs (weakness 4).
- Light-themed report in a dark app (weakness 6).
- No job control: no stop, no queue, no list of past reports
  (weaknesses 1, 2, 5).
- The progress label shows the step only (weaknesses 7, 8).

## Gaps / missing

- No cancel.
- No queue (F1).
- No saved reports.
- No depth control.
- No clickable sources.

## Improvements (concrete, in priority order)

1. Cancellable jobs: a cancel flag checked between sources, plus
   a Stop for the running job (reuse the dock Stop while a job
   runs, or a job line with its own stop).
2. Open the finished report tab in the background (do not switch
   to it) — the chat log already announces it.
3. Make source URLs clickable links in the report tab (render
   <a href> in report_to_html).
4. Save finished reports to ~/.forge/research/ (Markdown) so
   closing the tab does not lose them; list past reports from the
   start page or a command.
5. Dark-theme the report page to match the app (shared with M7 —
   one page style for both).
6. Progress before each fetch ("Fetching <url>...") plus elapsed
   time on the job label.
7. Depth control: let the chat ACTION (or a follow-up) set
   max_pages / detail.
8. Normalize URLs for dedup (strip trailing slash, tracking
   params, http/https).
9. Queue (owned by whole-project task F1 — listed here as the
   #11-side gap).
10. Retrieve the relevant excerpt per source instead of the
    head-cut.

## Verdict

#11 delivers its core promise — the turn does not wait, the
report lands in a tab, progress is concrete. But a background
job is a one-way door: it cannot be stopped, a second ask is
refused rather than queued, and the finished report yanks focus
and then vanishes when its tab closes. The job needs the same
control the chat turn has: stop, queue, and a saved result.
