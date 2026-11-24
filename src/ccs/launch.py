"""How ccs opens a session that may already be running somewhere else."""

from __future__ import annotations

import subprocess
from typing import Callable, List, Optional

from ccs.sessions import Live


def resume_command(session_id: str, live: Optional[Live],
                   ask: Callable[[str], str] = None) -> Optional[List[str]]:
    """The claude command that opens the session, or None when the user cancels.
    A background session is attached. A session open somewhere else asks first:
    resuming it starts a second Claude on the same conversation."""
    if live is None:
        return ["claude", "--resume", session_id]
    if live.job_id:
        return ["claude", "attach", live.job_id]
    answer = (ask or ask_tty)(
        f"This session is already open ({where(live)}); resuming it here starts a second Claude on it.\n"
        "  c  open a copy (--fork-session)\n"
        "  r  resume it anyway\n"
        "  Enter  cancel\n> "
    )
    answer = answer.strip().lower()
    if answer == "c":
        return ["claude", "--resume", session_id, "--fork-session"]
    if answer == "r":
        return ["claude", "--resume", session_id]
    return None


def where(live: Live) -> str:
    tty = _tty(live.pid)
    return f"pid {live.pid}, terminal {tty}" if tty else f"pid {live.pid}, no terminal: an IDE or the desktop app"


def ask_tty(question: str) -> str:
    """Ask on the terminal itself, whatever stdin is."""
    try:  # two handles: Python cannot open a terminal "r+" in text mode (it is not seekable)
        with open("/dev/tty", "w") as out, open("/dev/tty") as keyboard:
            out.write(question)
            out.flush()
            return keyboard.readline().strip()
    except OSError:
        return ""  # no terminal: cancel


def _tty(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True).stdout.strip()
    except OSError:
        return ""
    return "" if out in ("", "??", "?") else out
