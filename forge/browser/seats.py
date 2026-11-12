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
"""

import json
import os

_FEATURES = ("chat", "tidy_titles", "memory")


def seats_path():
    override = os.environ.get("FORGE_BROWSER_SEATS_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "seats.json")


def _defaults():
    return {"brain": None, "fast": None, "vision": None,
            "ai_enabled": True,
            "features": {name: True for name in _FEATURES}}


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
    except (OSError, ValueError):
        pass
    return seats


def save_seats(seats):
    try:
        path = seats_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(seats, handle, indent=2)
    except OSError:
        pass


def set_seat(name, model):
    """Save (or clear, with None/\"\") a seat pick."""
    seats = load_seats()
    seats[name] = (model or "").strip() or None
    save_seats(seats)
    return seats


def set_feature(name, enabled):
    seats = load_seats()
    if name == "ai":
        seats["ai_enabled"] = bool(enabled)
    elif name in _FEATURES:
        seats["features"][name] = bool(enabled)
    save_seats(seats)
    return seats


def seats_enabled(feature):
    """Is this AI feature on — master switch and its own toggle."""
    seats = load_seats()
    if not seats["ai_enabled"]:
        return False
    return bool(seats["features"].get(feature, True))


def pick_fast_model():
    """The fast seat's model, or None for 'same as the brain'."""
    return load_seats().get("fast")
