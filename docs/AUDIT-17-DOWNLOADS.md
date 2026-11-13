# Audit #17 — Download handling

Audited 2026-11-12 from the code (_http.py http_get /
http_get_to_file / _iter_chunks, files.py fetch_any's disk
handling), the tests (DownloadCapTests, 8 tests), and live probes
of the gzip bomb, the cap + partial removal, and a mid-download
network failure.

## What it is

Task #17 from the improvements list: download limits, revised
2026-11-11 at Joshua's order — downloads need no cap (his machine
has plenty of storage). The 512 MiB cap was REMOVED; the 32 MiB
in-memory guard stays.

## What it does

- Files saved to disk stream there in chunks with NO size limit;
  his disk is the only limit.
- The 32 MiB in-memory guard stays (MAX_BODY_BYTES) — a RAM guard
  on an 8 GB machine, not a download cap: a big text file is saved
  to disk and reported as saved, never refused.
- A declared Content-Length over the in-memory cap is refused from
  the headers alone; the cap counts decompressed bytes (gzip bomb
  protection).
- An explicit max_bytes on http_get_to_file still refuses over
  that size and removes the partial file.

## Strengths

- The no-cap order is honored exactly: disk streaming is uncapped;
  the in-memory guard never refuses a download (files.py saves big
  files to disk and reports them).
- Gzip-bomb protection is real (PROBED: 33.6 KB wire → 34.6 MB
  decompressed → refused at the 32 MiB cap).
- The Content-Length precheck refuses before reading any body
  bytes.
- The cap is enforced on ACTUAL bytes in the chunk loop, so a lying
  Content-Length cannot sneak past.
- Partial-file removal on cap refusal works (PROBED).
- Tests cover the declared-length refusal, chunked refusal, gzip
  bomb, to-file cap + partial removal, no file cap by default,
  and fetch_any past the cap (8 tests).

## Weaknesses (all verified by probe or code read)

1. A mid-download network failure is misreported as a save
   failure. PROBED: a connection drop during http_get_to_file
   raises "could not save the download (<urlopen error connection
   dropped>)". URLError subclasses OSError, so the inner `except
   OSError` catches network failures and relabels them. The person
   is told the save failed when the network dropped.
2. A mid-download failure leaves a partial file (direct callers).
   PROBED: 100 bytes left behind. fetch_any cleans up its own temp
   file, so the leak is latent today — but any future direct caller
   leaks partials. The OSError branch does not unlink; only the
   BrowserError (cap) branch does.
3. No resume. A failed download restarts from zero — no Range
   header support. For big downloads on a flaky connection,
   restart-from-zero is painful.
4. No download progress reporting. http_get_to_file has no progress
   callback — a big download shows nothing while it runs. (The
   window has no download UI at all — E1 owns that side; the engine
   side needs an on_progress hook.)
5. No disk-space guard. The uncapped disk path has no defense
   against filling the disk — a gzip bomb decompressing to disk, or
   a download bigger than free space, fills the disk until the write
   fails with a bare OSError. A "stop cleanly when the disk is nearly
   full" guard is not a download cap (no size limit on the download
   itself) — it prevents a full-disk brick.
6. The 20 s timeout is per-read, not total. A slow-but-alive
   download can run indefinitely (each 64 KB chunk resets the
   clock); a connection dribbling 1 byte/19 s runs forever. Minor.
7. The User-Agent names CODE-FORGE ("ForgeBrowser/0.1 (Code Forge;
   +.../CODE-FORGE)") — the browser identifies itself as Code
   Forge's. Pairs with B5.
8. The refusal message says "download" for page fetches.
   _refuse_too_big is used for http_get too — a 40 MB page fetch
   reports "that download is too big". Minor copy.
9. Redirect to a local address is not fenced — the fence checks the
   original URL only; urllib follows redirects. Cross-ref to #20
   (the fix belongs to the fence).
10. No download UI in the window — E1 owns this. Cross-ref.
11. fetch_any's media temp files — the preview path downloads whole
    media files (L1 owns this). Cross-ref.

## UI flaws

- No download UI at all (E1).
- The mislabeled error (weakness 1) is user-visible copy.
- No progress (weakness 4).

## Gaps / missing

- No resume.
- No progress hook.
- No disk-space guard.

## Improvements (concrete, in priority order)

1. Fix the OSError catch: catch URLError/TimeoutError before
   OSError (or exclude them) so a network failure during a download
   reports as a network failure, not a save failure.
2. Unlink the partial file on ANY failure during the write loop,
   not just cap refusal.
3. Add resume support (Range requests) — restart a failed download
   from where it stopped.
4. Add an on_progress callback to http_get_to_file (bytes so far +
   total when known) so a UI can show download progress.
5. Add a disk-space guard: stop cleanly (plain message + partial
   removal) when free space runs low — not a size cap, a full-disk
   guard.
6. Fix the User-Agent to name Forge Browser's own repo (pairs with
   B5).
7. Clarify the refusal message for page fetches (or pass a kind to
   _refuse_too_big).
8. Consider a total time limit (or stall detection) for downloads.
9. Cross-refs: E1 (download UI), L1 (preview media download), #20
   (redirect fence).

## Verdict

#17 honors Joshua's no-cap order exactly and the gzip-bomb
protection is real (probed). But the error handling has a real
bug: a network failure mid-download is reported as a save failure
(URLError is an OSError), and the partial file is only removed on
cap refusal. The missing pieces are resume, progress, and a
disk-space guard — none of which is a size cap.
