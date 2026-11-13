# Audit #20 — Fetch address fence

Audited 2026-11-12 from the code (_http.py check_fetch_url,
http_get/http_get_to_file wiring, render.py, fetch.py, files.py,
cli.py --allow-local), the tests (FenceTests, 4 tests), and live
probes of literal, mapped, integer, hex, and octal addresses.

## What it is

Task #20 from the improvements list: the fetch address fence —
refuse fetches to non-public addresses unless local fetching was
explicitly allowed. Built 2026-11-11.

## What it does

- check_fetch_url(url, allow_local=False) refuses fetches to
  non-public addresses: loopback (127.0.0.0/8, ::1), RFC 1918 LAN
  ranges, link-local, localhost/*.local names — unless
  allow_local=True or FORGE_BROWSER_ALLOW_LOCAL=1.
- Wired into http_get + http_get_to_file (so fetch, fetch_any,
  search all inherit it) and render.py's snapshot/screenshot/render
  (refuses before the engine starts).
- CLI --allow-local threads through; the verify flow passes
  allow_local=True; the local SearXNG endpoint passes
  allow_local=True by design.
- DNS names are deliberately NOT resolved (the docstring explains:
  in proxied environments a public name can resolve to a non-global
  address, so resolving would refuse ordinary browsing).

## Strengths

- The fence covers every engine fetch path (http_get,
  http_get_to_file, render) — one choke point, no path around it.
- The refusal message is plain words and tells the person exactly
  how to allow local fetching (their machine, their choice).
- The env switch + per-call flag + CLI flag give three granularities
  of opt-in.
- IPv6 handled correctly (::1 refused; ::ffff:127.0.0.1 refused —
  probed, is_global False).
- Trailing-dot and case handled (rstrip("."), lower()).
- Tests cover loopback/LAN refusal (7 URLs), public pass, env
  switch, render refusal.

## Weaknesses (all verified by probe or code read)

1. Integer/hex/octal IP forms bypass the fence. PROBED:
   'http://2130706433/' PASSES the fence, and getaddrinfo resolves
   it to 127.0.0.1. Same for 0x7f.0.0.1 and 0177.0.0.1.
   ipaddress.ip_address rejects these non-dotted forms (ValueError
   → treated as "a DNS name"), but the socket layer (inet_aton)
   accepts them as loopback. A URL the fence passes connects to
   the local machine.
2. Redirects are not fenced. http_get calls check_fetch_url once,
   on the original URL; urllib's default redirect handler follows
   301/302/303/307/308 internally with no per-hop check. A public
   URL that redirects to 127.0.0.1:11434 (Ollama) or a LAN address
   is fetched without any check. (Code-read verified — the fence
   is a single pre-check; there is no custom redirect handler.)
3. DNS names pointing at private addresses are not caught.
   Documented and deliberate (the docstring is honest about it) —
   but it means a domain resolving to 127.0.0.1 (DNS rebinding / a
   malicious domain) passes the fence. The docstring says catching
   it "needs the fetch to connect to the address the fence checked,
   which is a bigger change than this item" — true, but it is the
   hole that matters most for SSRF: an attacker cannot put a literal
   127.0.0.1 in a search result, but they CAN register a domain
   pointing at it.
4. The env switch is global and sticky. FORGE_BROWSER_ALLOW_LOCAL=1
   opens local fetching for the whole process — every fetch,
   including ones the brain triggers from untrusted pages. No
   per-fetch granularity in the env path.
5. The fence does not cover the window's navigation. The Qt
   WebEngine window browses anywhere, including local addresses,
   with no fence. By design (the person drives the window) — but
   worth stating: the fence covers the engine's fetches, not the
   window.
6. No logging of refused fetches. A refusal raises BrowserError;
   the caller reports it. There is no central log of fence hits
   (useful for spotting steering attempts). Minor.
7. The refusal message's bypass instructions are helpful for the
   owner (his machine, his choice) — not a weakness for this
   threat model. Noted as by-design.

## UI flaws

- None in the window (the fence is engine-side). The CLI refusal
  message is long but plain.

## Gaps / missing

- No non-dotted IP normalization (weakness 1).
- No redirect re-check (weakness 2).
- No DNS resolve-and-check (weakness 3).
- No fence-hit logging.

## Improvements (concrete, in priority order)

1. Normalize non-dotted IP forms before the fence check: resolve
   the host through socket.inet_aton (or an equivalent parser) so
   2130706433, 0x7f.0.0.1, and 0177.0.0.1 are recognized as
   127.0.0.1 and refused. Add tests for all three forms.
2. Fence redirects: install a redirect handler that re-runs
   check_fetch_url on each redirect target (or disable automatic
   redirects and follow them manually with a per-hop fence check +
   a hop cap).
3. Resolve-and-check DNS names: resolve the host, refuse if any
   address is non-global, and connect to the checked address (pin
   the connection to avoid TOCTOU) — the bigger change the docstring
   names. At minimum, resolve and check (accepting the TOCTOU
   window) as a first step.
4. Scope the env switch: consider a per-session or per-fetch
   allow-local instead of a process-global switch (or document that
   the env switch is all-or-nothing).
5. Log fence refusals (URL + reason) so steering attempts are
   visible.
6. Document that the window's navigation is unfenced by design (the
   person drives it).
7. Add tests for: integer/hex/octal IP bypass, redirect-to-local,
   DNS-to-private (documents the known hole until fixed).

## Verdict

#20's fence is solid for the literal cases it was built for
(loopback, LAN, localhost — all refused, tested), but it has two
real bypasses: integer/hex/octal IP forms sail past it (probed —
2130706433 resolves to 127.0.0.1), and redirects are never
re-checked (a public URL that 302s to Ollama is fetched). The DNS
hole is documented and deliberate, but it is the one that matters
most for SSRF.
