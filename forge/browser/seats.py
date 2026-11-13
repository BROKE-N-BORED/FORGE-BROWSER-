"""Seat picks + AI feature toggles, saved in ~/.forge/seats.json.

The chat dock's picker writes here; the seat choosers read it.
Precedence for a seat: a pick in this file beats the env var,
which beats the automatic preference. A pick of None (or the
"(auto)" entry in the dock) means "no pick saved".

Features, Firefox-style: one master AI kill-switch plus a toggle
per feature. When the master is off, every AI feature is off.
chat = the dock brain and every brain job (summarize, brief,
compare, research); tidy_titles = brain-written tab titles;
memory = remembered facts offered as context + pages recorded
for later recall.

What the switch covers (T2/T3/T4): the morning brief and the CLI
voice command are gated on chat (the same gate as the dock mic);
image describing is gated on the master switch only — it is not
a chat feature, so the chat toggle does not kill it, but "AI
off" does. The `transcribe` CLI command is a file utility, not
an AI feature, and is not gated.

Writes are locked (T9): every load-modify-save runs under an
exclusive flock on seats.json.lock, so the window and the CLI
cannot clobber each other, and the write itself is atomic
(tmp file + os.replace).
"""

import fcntl
import json
import os

_FEATURES = ("chat", "tidy_titles", "memory")

# P5: the action kinds a person may always-allow — cheap, visible,
# reversible. Submit actions (remember/forget) always re-ask, and
# expensive actions (research/card) are always gated; neither can
# ever be always-allowed.
ALWAYS_ALLOWABLE_KINDS = frozenset(
    ("search", "open", "read_page", "recall", "recall_pages",
     "brief", "summarize", "compare"))


def seats_path():
    override = os.environ.get("FORGE_BROWSER_SEATS_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "seats.json")


def _defaults():
    return {"brain": None, "fast": None, "vision": None,
            "ai_enabled": True,
            "features": {name: True for name in _FEATURES},
            "always_allow": {}}


def load_seats():
    seats = _defaults()
    try:
        with open(seats_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for key in ("brain", "fast", "vision"):
                if isinstance(data.get(key), str) and data[key].strip():
                    seats[key] = data[key].strip()
            if isinstance(data.get("ai_enabled"), bool):
                seats["ai_enabled"] = data["ai_enabled"]
            if isinstance(data.get("features"), dict):
                for name in _FEATURES:
                    if isinstance(data["features"].get(name), bool):
                        seats["features"][name] = data["features"][name]
            if isinstance(data.get("always_allow"), dict):
                for name in ALWAYS_ALLOWABLE_KINDS:
                    if isinstance(data["always_allow"].get(name), bool):
                        seats["always_allow"][name] = data["always_allow"][name]
    except (OSError, ValueError):
        pass
    return seats


def _write_seats(seats):
    """Atomic write (tmp + replace), no locking — the caller
    holds the lock. A crashed write leaves the old file, never
    a half-written one."""
    path = seats_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(seats, handle, indent=2)
    os.replace(tmp_path, path)


def _locked_update(mutate):
    """Load, mutate, and save under one exclusive lock (T9): the
    window and the CLI each see the other's writes, so neither
    clobbers the other. mutate edits the loaded seats in place,
    or returns a replacement dict to write instead. Returns the
    saved seats."""
    path = seats_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seats = load_seats()
        replacement = mutate(seats)
        if replacement is not None:
            seats = replacement
        try:
            _write_seats(seats)
        except OSError:
            pass
        return seats


def save_seats(seats):
    try:
        _locked_update(lambda _loaded: seats)
    except OSError:
        pass


def set_seat(name, model):
    """Save (or clear, with None/\"\") a seat pick. An unknown
    seat name is ignored, not stored (T10)."""
    if name not in ("brain", "fast", "vision"):
        return load_seats()

    def mutate(seats):
        seats[name] = (model or "").strip() or None

    return _locked_update(mutate)


def set_feature(name, enabled):
    def mutate(seats):
        if name == "ai":
            seats["ai_enabled"] = bool(enabled)
        elif name in _FEATURES:
            seats["features"][name] = bool(enabled)

    return _locked_update(mutate)


def seats_enabled(feature):
    """Is this AI feature on — master switch and its own toggle."""
    seats = load_seats()
    if not seats["ai_enabled"]:
        return False
    return bool(seats["features"].get(feature, True))


def pick_fast_model():
    """The fast seat's model, or None for 'same as the brain'."""
    return load_seats().get("fast")


def always_allowed(kind):
    """Has the person always-allowed this action kind? Only cheap
    kinds can be — submit and expensive kinds always return False,
    even if the file somehow says otherwise."""
    if kind not in ALWAYS_ALLOWABLE_KINDS:
        return False
    return bool(load_seats()["always_allow"].get(kind, False))


def set_always_allow(kind, allowed):
    """Save (or clear) an always-allow for one cheap kind. A kind
    that may not be always-allowed is ignored, not stored."""
    if kind not in ALWAYS_ALLOWABLE_KINDS:
        return load_seats()

    def mutate(seats):
        if allowed:
            seats["always_allow"][kind] = True
        else:
            seats["always_allow"].pop(kind, None)

    return _locked_update(mutate)
