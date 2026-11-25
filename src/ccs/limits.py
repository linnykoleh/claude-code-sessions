"""Claude's usage limits: how much of the 5-hour window and of the week is used, and when each resets.

Claude Code gives them only to the status line command, on its stdin, as
`"rate_limits": {"five_hour": {"used_percentage": 19, "resets_at": <epoch>}, "seven_day": {...}}`.
A status line saves them with `ccs --save-limits` (or writes the same file itself), and ccs shows the last
snapshot under its status counts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

WINDOWS = (("five_hour", "5h"), ("seven_day", "Week"))


def save_limits(raw: str, path: Path, now: float) -> bool:
    """Keep the `rate_limits` of a status line's JSON in `path`, with when they were seen; False if it has none."""
    try:
        limits = json.loads(raw or "{}").get("rate_limits")
    except (ValueError, AttributeError):
        return False
    if not isinstance(limits, dict):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")  # whole or not at all: ccs may be reading it
        tmp.write_text(json.dumps({"saved": now, "rate_limits": limits}))
        tmp.replace(path)
    except OSError:
        return False  # a status line runs this on every update: it must never fail loudly
    return True


def read_limits(path: Path) -> Optional[dict]:
    """The last snapshot: {"saved": epoch, "rate_limits": {...}}, or None when there is none or it is broken."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        return None
    return data


def window(limits: dict, key: str, now: float) -> Optional[dict]:
    """{"used": 0..100 or None once it has reset, "resets_in": seconds or None}, or None when the window is missing."""
    w = limits.get(key)
    if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
        return None
    resets_at = w.get("resets_at")
    resets_in = resets_at - now if isinstance(resets_at, (int, float)) and resets_at > 0 else None
    if resets_in is not None and resets_in <= 0:
        return {"used": None, "resets_in": None}  # the snapshot is from before the reset: what is used now is unknown
    return {"used": max(0.0, min(100.0, float(w["used_percentage"]))), "resets_in": resets_in}
