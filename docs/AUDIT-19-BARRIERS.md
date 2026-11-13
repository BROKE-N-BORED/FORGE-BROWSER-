# Audit #19 — Barrier detection on raw HTML

Audited 2026-11-12 from the code (barriers.py, files.py fetch_any
barrier wiring, research.py barrier merge + report), the tests
(BarrierTests, gated-page test, research barrier test), and live
probes of false positives, false negatives, and the dead url
parameter.

## What it is

Task #19 from the improvements list: barrier detection on raw HTML
in fetch_any — FileResult.barriers. Built 2026-11-11.

## What it does

- detect_barriers(html, url="") names the gates a page presents
  from its HTML: captcha, login, paywall, age_gate — plain
  substring matching on lowered HTML against marker lists.
- fetch_any runs detection on the RAW HTML (files.py:326-327) —
  the right place, since markup markers (password fields, captcha
  class names) can never appear in the extracted Markdown.
- FileResult.barriers carries the gates; research merges the
  raw-HTML barriers with a second pass over the extracted text
  (deduped by kind) and prints "Note: {barrier['note']}" per source
  in the report.
- The CDP path (handle_page) navigates, scrolls, dismisses consent
  (reject-all preferred), then detects barriers on the rendered
  HTML.
- CAPTCHAs and logins without vault entries are NEVER cleared
  automatically — detected, named, handed to the person (the
  standing rule).

## Strengths

- Detection on raw HTML is the right call (markup markers survive
  there).
- The research double pass (raw HTML + extracted text, deduped by
  kind) catches both markup and visible-phrase gates.
- The barriers ARE surfaced in the research report (not silently
  swallowed).
- Consent dismissal prefers reject-all (privacy-preferring).
- The never-solve-CAPTCHA rule is honored — detection names the
  gate and hands it to the person.
- The consent dismissal runs BEFORE barrier detection in
  handle_page, so a dismissed banner does not pollute detection.

## Weaknesses (all verified by probe or code read)

1. False positives from article content. PROBED: an article ABOUT
   paywalls and CAPTCHAs → ['captcha', 'paywall']. Substring
   matching cannot tell a gate from a discussion of gates. The
   research report would warn "this source is behind a paywall"
   for an article that merely discusses paywalls.
2. A login form on a readable page flags 'login'. PROBED: a page
   with a password field in the header (login box) but a fully
   readable article → 'login'. The marker cannot tell a gate from
   a site that merely offers login. Every site with a header login
   box is flagged.
3. A reCAPTCHA script alone flags 'captcha'. PROBED: a page that
   only LOADS the reCAPTCHA script (invisible v3, no human check
   shown) → 'captcha'. The script tag is in the raw HTML; no
   CAPTCHA is actually presented.
4. JS-rendered barriers are invisible. PROBED: a JS app shell →
   []. A barrier that loads via JS (the common case for modern
   sites) has no markers in the raw HTML. The fetch path can only
   see what the server sent.
5. The url parameter is dead. detect_barriers(html, url="") never
   uses url. (PROBED: same result for different URLs.)
6. The vault login fill is documented but not implemented.
   handle_page's docstring says "A login barrier is cleared first
   when the vault has an entry for the site — pass vault= and
   passphrase-free opened Vault to enable it" — but handle_page
   takes no vault parameter and never fills a login. The docstring
   promises a feature that does not exist.
7. The age gate is detected but never cleared. The note says "its
   confirm button is a normal clickable element" — but nothing
   clicks it in either path (fetch has no clicking; handle_page
   only dismisses consent).
8. The paywall note claims more than is measured. "only the preview
   text is readable" — the fetch already read the full HTML;
   whether the text is preview-only is not verified.
9. A failed consent click aborts the whole dismissal.
   _click_labelled returns None on the first click exception
   instead of trying the next matching element or phrase.
10. Markers are English-only. A German/French CAPTCHA or paywall
    page is not detected.

## UI flaws

- False-positive barrier notes in research reports (weaknesses 1-3)
  — the person is told a source is gated when it is not.
- The vault-fill docstring promise (weakness 6) misleads anyone
  reading the API.

## Gaps / missing

- No distinction between a gate and a discussion of gates / a login
  form / a loaded script.
- No JS-rendered barrier detection in the fetch path (needs the CDP
  path).
- No age-gate clearing.
- No vault login filling (despite the docstring).
- No non-English markers.

## Improvements (concrete, in priority order)

1. Reduce false positives: require the marker to appear in a gate
   context — e.g. strip <script>/<style> content before matching
   (kills the reCAPTCHA-script false positive); for 'login',
   require the password field to gate content (or downgrade the
   note to "this page has a login form"); for paywall/captcha,
   prefer markers in the visible text over raw HTML.
2. Fix the handle_page docstring (remove the vault-fill promise) or
   implement the vault login fill.
3. Clear age gates in handle_page (click the confirm button — it is
   a normal clickable element, as the note says).
4. Use the url parameter (or drop it) — e.g. pass it through for
   per-site barrier notes.
5. Add non-English markers for the common gates (at least the top
   languages).
6. Make _click_labelled try the next element/phrase after a failed
   click instead of returning None.
7. Verify the paywall claim (or soften the note to "may be behind a
   paywall").
8. Add tests for the false-positive cases: article about gates,
   login form on a readable page, script-only reCAPTCHA, JS shell
   (documents the known blind spot).
9. Cross-ref: the CDP path (handle_page) is the answer for
   JS-rendered barriers — document that the fetch path cannot see
   them.

## Verdict

#19 puts detection in the right place (raw HTML, before
extraction) and surfaces the gates honestly in research reports,
but the detection itself is plain substring matching — it flags
articles ABOUT gates, pages with a login box, and pages that merely
load the reCAPTCHA script. And the vault login fill the docstring
promises does not exist.
