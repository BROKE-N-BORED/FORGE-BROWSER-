"""Seamless site handling — get past the furniture, hand over the gates.

What the engine clears on its own:

* Cookie / consent banners — the dismiss button is found by its
  label and clicked (reject-all preferred when offered, accept
  otherwise). This is the same click a person makes.
* Age gates — the confirm button is a normal clickable element,
  so handle_page clicks it once the gate is detected (Y3).
* Logins with a vault entry — handle_page fills the form from the
  vault (never submits) when the vault has the site (Y2).
* Lazy pages — navigation already waits for the load event, and a
  scroll pass can be run so below-fold content loads before reading.

What it does NOT clear on its own: checks built to prove a human is
present (CAPTCHA / "verify you are human"), and logins it has no
vault entry for. Those are detected, named in plain words, and
handed to the person — the session runs on a PERSISTENT profile, so
once the person clears the gate or logs in once (headed handoff),
the same profile sails through on later visits, cookies and all.
That handoff is the seamless path: one human minute, once, instead
of the agent fighting a gate it should not fight.

detect_barriers works on raw HTML (fetch path) or a live session's
rendered HTML, so Research mode can report "this source is behind a
login" instead of silently reading a login page as if it were the
article.

One limit to know (Y9): the fetch path only sees what the server
sent. A barrier that loads via JavaScript — the common case on
modern sites — has no markers in the raw HTML and is invisible to
fetch_any and to Research's text pass. The CDP path (handle_page)
reads the rendered page, so it sees JS-rendered barriers; when the
fetch path reports no barriers on a page that still reads wrong,
the rendered path is the next step.
"""

import re
from urllib.parse import urlparse

_CONSENT_REJECT = ["reject all", "reject cookies", "decline", "only essential",
                   "essential only", "refuse"]
_CONSENT_ACCEPT = ["accept all", "accept cookies", "allow all", "i agree",
                   "agree and close", "got it", "ok, thanks"]

# Y3: an age gate's confirm button, most specific first — the
# generic words ("confirm", "enter") only run after an age gate
# was detected, so they cannot fire on an unrelated page.
_AGE_CONFIRM = ["i am over 18", "i'm over 18", "i am 18 or older",
                "yes, i am over 18", "i am 18", "over 18",
                "confirm", "enter", "i agree"]

# Y1: markers must appear in a GATE context, not in prose about
# gates. Widget markers match class/id attributes in the markup
# (a captcha widget in the page, not a mention of one); gate
# phrases match the visible text (instructions to the reader).
# The bare words "recaptcha" / "paywall" are NOT markers — an
# article about paywalls and CAPTCHAs used to flag both.
_CAPTCHA_WIDGET = re.compile(
    r"""(class|id)\s*=\s*["'][^"']*(g-recaptcha|grecaptcha|h-captcha|"""
    r"""hcaptcha|cf-turnstile|turnstile)""")
_PAYWALL_WIDGET = re.compile(
    r"""(class|id)\s*=\s*["'][^"']*paywall""")
_CHALLENGE_DOMAIN = "challenges.cloudflare"
_PASSWORD_FIELD = re.compile(r"""type\s*=\s*["']?password["']?""")

_CAPTCHA_PHRASES = ["verify you are human", "i'm not a robot",
                    "i am not a robot", "are you a robot",
                    "human verification", "please verify you are human",
                    "confirm you are human",
                    # Y5: non-English (de/fr/es/pt/it).
                    "ich bin kein roboter", "je ne suis pas un robot",
                    "no soy un robot", "não sou um robô",
                    "non sono un robot",
                    "bestätigen sie, dass sie ein mensch sind",
                    "vérifiez que vous êtes humain",
                    "confirma que eres humano"]
_LOGIN_PHRASES = ["sign in to continue", "log in to continue",
                  "you must be logged in", "sign in to read",
                  "log in to read", "sign in to view", "log in to view",
                  "anmelden, um fortzufahren",
                  "connectez-vous pour continuer",
                  "inicia sesión para continuar",
                  "faça login para continuar",
                  "accedi per continuare"]
_PAYWALL_PHRASES = ["subscribe to continue reading",
                    "subscribe to read",
                    "this article is for subscribers",
                    "subscribe to continue",
                    "abonnieren sie, um weiterzulesen",
                    "abonnez-vous pour continuer à lire",
                    "suscríbete para seguir leyendo",
                    "assine para continuar lendo",
                    "abbonati per continuare a leggere"]
_AGE_PHRASES = ["confirm you are over 18", "confirm your age",
                "you must be 18 or older", "are you over 18",
                "i am over 18", "i'm over 18", "i am 18 or older",
                "bestätigen sie, dass sie über 18 sind",
                "confirmez que vous avez plus de 18 ans",
                "confirma que tienes más de 18 años",
                "confirme que você tem mais de 18 anos",
                "conferma di avere più di 18 anni"]

# Y1: visible text over this length means the page reads fine —
# a password field on it is a login box, not a gate. A login page
# itself has only a few hundred characters of visible text.
_READABLE_CHARS = 500

_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_OR_STYLE_TAG = re.compile(
    r"<(script|style)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _strip_script_style(html):
    """Remove <script> and <style> elements, tags and content —
    a loaded reCAPTCHA script is not a presented CAPTCHA (Y1)."""
    text = _SCRIPT_OR_STYLE.sub(" ", html or "")
    return _SCRIPT_OR_STYLE_TAG.sub(" ", text)


def _visible_text(html):
    """The page's visible words: script/style stripped, tags
    removed, whitespace collapsed. Gate phrases match here, not
    in raw HTML — markup can never say "verify you are human"."""
    text = _TAG.sub(" ", _strip_script_style(html))
    return " ".join(text.split()).lower()


def _site_of(url):
    """The host a barrier belongs to, for per-site notes (Y4)."""
    return (urlparse(url or "").hostname or "").lower()


def detect_barriers(html, url=""):
    """Name the gates a page presents, from its HTML (or extracted
    text — the research second pass calls this on Markdown, where
    there are no tags to strip). [] = clear. Each barrier carries
    its site (the url's host) for per-site notes (Y4).

    Markers must appear in a gate context (Y1): <script>/<style>
    content is stripped first; captcha/paywall widgets match by
    class/id in the markup; gate phrases match in the visible
    text. An article ABOUT gates, a login box on a readable page,
    and a script-only reCAPTCHA do not flag. A password field
    flags 'login' only when it gates the content — a gate phrase,
    or a page too short to be anything but the login form.

    The fetch path only sees what the server sent — a barrier
    that loads via JavaScript is invisible here (Y9); the CDP
    path (handle_page) sees the rendered page.
    """
    stripped = _strip_script_style(html)
    lowered = stripped.lower()
    visible = _visible_text(html)
    site = _site_of(url)
    found = []
    if (_CAPTCHA_WIDGET.search(lowered) is not None
            or _CHALLENGE_DOMAIN in lowered
            or any(phrase in visible for phrase in _CAPTCHA_PHRASES)):
        found.append({"kind": "captcha", "site": site, "needs_human": True,
                      "note": "the page asks for a human check (CAPTCHA) — "
                              "it needs the person, once, in a visible window"})
    has_password = _PASSWORD_FIELD.search(lowered) is not None
    if (any(phrase in visible for phrase in _LOGIN_PHRASES)
            or (has_password and len(visible) < _READABLE_CHARS)):
        found.append({"kind": "login", "site": site, "needs_human": True,
                      "note": "the page wants a login — a vault entry fills it, "
                              "otherwise the person logs in once and the "
                              "persistent profile remembers"})
    if (_PAYWALL_WIDGET.search(lowered) is not None
            or any(phrase in visible for phrase in _PAYWALL_PHRASES)):
        # Y7: "may be" — whether the fetched text is preview-only
        # was never verified, so the note must not claim it.
        found.append({"kind": "paywall", "site": site, "needs_human": False,
                      "note": "the page may be behind a paywall — "
                              "what was read could be preview text only"})
    if any(phrase in visible for phrase in _AGE_PHRASES):
        found.append({"kind": "age_gate", "site": site, "needs_human": False,
                      "note": "the page shows an age gate — its confirm "
                              "button is a normal clickable element"})
    return found


def _click_labelled(session, phrases):
    elements = session.elements()
    for phrase in phrases:
        for element in elements:
            if element.get("tag") in ("button", "a", "input") and \
                    phrase in (element.get("text") or "").lower():
                try:
                    session.click(element["i"])
                    return element.get("text") or phrase
                except Exception:
                    # Y6: a failed click tries the next element or
                    # phrase instead of giving up on the banner.
                    continue
    return None


def dismiss_consent(session):
    """Click the consent banner's dismiss button; return its label or None."""
    return _click_labelled(session, _CONSENT_REJECT) or \
        _click_labelled(session, _CONSENT_ACCEPT)


def scroll_page(session, steps=3):
    """Scroll so lazy-loaded content renders before the page is read."""
    for _ in range(steps):
        session._evaluate("window.scrollBy(0, window.innerHeight); true")
    session._evaluate("window.scrollTo(0, 0); true")


def handle_page(session, url, scroll=False, vault=None):
    """Navigate, clear what clears, and report what remains.

    Returns {url, consent_dismissed, barriers, needs_human,
    age_gate_cleared, login_filled}. An age gate's confirm button
    is clicked when one is detected (Y3) — it is a normal
    clickable element, not a human check — and the barriers are
    re-detected after the click. A login barrier is filled (never
    submitted) when the vault has an entry for the site: pass an
    opened Vault as vault= (Y2). CAPTCHAs are never cleared here:
    they need the person, once, in a visible window.
    """
    session.navigate(url)
    if scroll:
        scroll_page(session)
    clicked = dismiss_consent(session)
    barriers = detect_barriers(session.html(), url)
    age_cleared = False
    if any(b["kind"] == "age_gate" for b in barriers):
        if _click_labelled(session, _AGE_CONFIRM) is not None:
            age_cleared = True
            barriers = detect_barriers(session.html(), url)
    login_filled = False
    if vault is not None and any(b["kind"] == "login" for b in barriers):
        from .vault import fill_from_vault, find_entry_for_url
        found = find_entry_for_url(vault, url)
        if found is not None:
            try:
                fill_from_vault(session, vault, found[0], submit=False)
                login_filled = True
            except Exception:
                login_filled = False
    return {
        "url": url,
        "consent_dismissed": clicked,
        "barriers": barriers,
        "needs_human": any(b["needs_human"] for b in barriers),
        "age_gate_cleared": age_cleared,
        "login_filled": login_filled,
    }
