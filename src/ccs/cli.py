"""ccs (Claude Code Sessions): browse and resume every Claude Code session across all projects.

  Enter   resume the session in its project folder (claude --resume <id>)
  Ctrl-Y  copy the resume command to the clipboard
  Type    to filter by title, project or date
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from ccs import __version__
from ccs.index import load_sessions
from ccs.pricing import load_prices
from ccs.sessions import Session, parse_session, prompts

PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cc-sessions"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
TITLE_WIDTH, PROJECT_WIDTH = 60, 22


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Browse and resume every Claude Code session across all your projects (newest first).",
        epilog="Keys: Enter resumes the session in its folder, Ctrl-Y copies the resume command, Esc quits.",
    )
    parser.add_argument("--list", action="store_true", help="print the session list without fzf")
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    parser.add_argument("--preview", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--copy", nargs=2, metavar=("CWD", "ID"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.preview:
        print_preview(Path(args.preview))
        return 0
    if args.copy:
        return copy_to_clipboard(resume_command(*args.copy))

    sessions = load_sessions(PROJECTS_DIR, CACHE_DIR)
    prices = load_prices(CACHE_DIR)
    if args.list:
        print(header_row())
        for s in sessions:
            print(row(s, prices))
        return 0
    return pick(sessions, prices)


# --- list rows ---

def header_row() -> str:
    return "\t".join(["LAST ACTIVITY   ", pad("PROJECT", PROJECT_WIDTH), pad("TITLE", TITLE_WIDTH), "  COST", "TIME"])


def row(s: Session, prices) -> str:
    cost = s.usage.cost(prices)
    return "\t".join([
        fmt_time(s.last_ts),
        pad(s.project, PROJECT_WIDTH),
        pad(s.title or "(untitled)", TITLE_WIDTH),
        (f"${cost:,.0f}" if cost is not None else "—").rjust(6),
        fmt_duration((s.last_ts or 0) - (s.first_ts or s.last_ts or 0)).rjust(4),
    ])


def pad(text: str, width: int) -> str:
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)


def fmt_time(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "????-??-?? ??:??"


def fmt_duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# --- interactive picker ---

def pick(sessions: List[Session], prices) -> int:
    fzf = os.environ.get("FZF") or shutil.which("fzf")
    if not fzf:
        print("ccs: fzf is required (https://github.com/junegunn/fzf). Use --list for a plain list.", file=sys.stderr)
        return 1

    me = f"{shlex.quote(sys.executable)} -m ccs"
    # hidden fields after the visible columns: 6 = transcript file, 7 = cwd, 8 = session id
    lines = [header_row()] + [f"{row(s, prices)}\t{s.file}\t{s.cwd}\t{s.id}" for s in sessions]
    cmd = [
        fzf, "--ansi", "--layout=reverse", "--header-lines=1", "--delimiter=\t", "--with-nth=1..5",
        "--no-sort", "--tiebreak=index", "--prompt=sessions> ",
        "--header=Enter: resume · Ctrl-Y: copy resume command · Esc: quit",
        f"--preview={me} --preview {{6}}", "--preview-window=right,50%,wrap",
        f"--bind=ctrl-y:execute-silent({me} --copy {{7}} {{8}})+abort",
    ]
    result = subprocess.run(cmd, input="\n".join(lines), stdout=subprocess.PIPE, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return 0  # Esc, Ctrl-C or Ctrl-Y

    fields = result.stdout.rstrip("\n").split("\t")
    cwd, session_id = fields[6], fields[7]
    if not os.path.isdir(cwd):
        print(f"Folder {cwd} no longer exists. Session ID: {session_id}", file=sys.stderr)
        return 1
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--resume", session_id])
    return 0  # not reached


def resume_command(cwd: str, session_id: str) -> str:
    return f"cd {shlex.quote(cwd)} && claude --resume {session_id}"


def copy_to_clipboard(text: str) -> int:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True, check=False)
            return 0
    print(text)  # no clipboard tool: at least show the command
    return 1


def print_preview(path: Path) -> None:
    s = parse_session(path, with_usage=False) if path.exists() else None
    if s is None:
        print(f"{path} not found")
        return
    print(f"{BOLD}{s.title or '(untitled)'}{RESET}")
    print(f"📁 {s.cwd}")
    if s.branch:
        print(f"🌿 {s.branch}")
    print(f"🆔 {s.id}")
    print()
    for i, (ts, text) in enumerate(prompts(path)):
        if i == 40:
            print(f"{DIM}…{RESET}")
            break
        print(f"{DIM}{fmt_time(ts)}{RESET}  {' '.join(text.split())[:300]}")
