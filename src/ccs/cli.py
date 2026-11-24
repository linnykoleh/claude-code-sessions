"""ccs (Claude Code Sessions): browse and resume every Claude Code session across all projects.

  Enter   resume the session in its project folder (claude --resume <id>)
  Type    to filter by title, project or date
  Ctrl-S  switch to searching inside conversations (your prompts and Claude's replies) and back
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
from typing import Dict, List, Optional

from ccs import __version__
from ccs.index import load_sessions
from ccs.launch import resume_command
from ccs.search import highlight, query_words, search, snippets
from ccs.pricing import load_prices
from ccs.sessions import Live, Session, live_status, local_day, parse_session, prompts

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIVE_DIR = CLAUDE_DIR / "sessions"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cc-sessions"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
TIME_WIDTH, PROJECT_WIDTH, TITLE_WIDTH = 19, 18, 60  # the date is 16 wide; 3 more keep it apart from PROJECT
STATUS_LEGEND = {"●": "Claude is working", "○": "open, waiting for you", "!": "stopped mid-turn"}
LIST_PROMPT, TEXT_PROMPT = "sessions> ", "text> "
HIT = "\033[1;33m"  # search words in the preview


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Browse and resume every Claude Code session across all your projects (newest first).",
        epilog="Keys: Enter resumes the session in its folder, Ctrl-S searches inside conversations, Esc quits.",
    )
    parser.add_argument("-s", "--search", nargs="+", metavar="WORD",
                        help='only sessions whose prompts, replies or title contain every word; "two words" is a phrase')
    parser.add_argument("--list", action="store_true", help="print the session list without fzf")
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    parser.add_argument("--preview", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--find", metavar="QUERY", help=argparse.SUPPRESS)  # with --preview: show the hits
    parser.add_argument("--rows", metavar="QUERY", help=argparse.SUPPRESS)  # fzf reload in text search
    parser.add_argument("--switch", action="store_true", help=argparse.SUPPRESS)  # fzf Ctrl-S
    args = parser.parse_args(argv)

    if args.preview:
        print_preview(Path(args.preview), query_words(args.find or ""))
        return 0
    if args.switch:
        print(switch_actions(os.environ.get("FZF_PROMPT", ""), me_command()))
        return 0

    sessions = load_sessions(PROJECTS_DIR, CACHE_DIR)
    prices = load_prices(CACHE_DIR)
    live = live_status(LIVE_DIR)
    query = args.rows if args.rows is not None else search_query(args.search)
    if args.rows is not None:
        print("\n".join(fzf_lines(search(sessions, query, CACHE_DIR), prices, live)))
        return 0
    if args.list:
        print(header_row())
        for s in search(sessions, query or "", CACHE_DIR):
            print(row(s, prices, live))
        return 0
    return pick(sessions, prices, live, query)


def search_query(words: Optional[List[str]]) -> Optional[str]:
    """`ccs -s "retry policy" refund` -> '"retry policy" refund': an argument with spaces stays a phrase."""
    if not words:
        return None
    return " ".join(f'"{w}"' if " " in w.strip() else w for w in words)


# --- list rows ---

# The title goes last: when the fzf pane is narrow, only the title gets cut.
def header_row() -> str:
    return "\t".join([pad("LAST ACTIVITY", TIME_WIDTH), pad("PROJECT", PROJECT_WIDTH), " COST", "TIME", " ", "TITLE"])


def row(s: Session, prices, live: Dict[str, Live]) -> str:
    cost = s.usage.cost(prices)
    return "\t".join([
        pad(fmt_time(s.last_ts), TIME_WIDTH),
        pad(s.project, PROJECT_WIDTH),
        (fmt_money(cost) if cost is not None else "—").rjust(5),
        fmt_duration(span(s)).rjust(4),
        status_mark(s, live),
        cut(s.title or "(untitled)", TITLE_WIDTH),
    ])


def status_mark(s: Session, live: Dict[str, Live]) -> str:
    if s.id in live:
        return "●" if live[s.id].busy else "○"
    return "!" if s.interrupted else " "


def legend(sessions: List[Session], live: Dict[str, Live]) -> str:
    """The status marks with how many sessions have each: "● 1 Claude is working · …"."""
    marks = [status_mark(s, live) for s in sessions]
    return " · ".join(f"{mark} {marks.count(mark)} {meaning}" for mark, meaning in STATUS_LEGEND.items())


def cut(text: str, width: int) -> str:
    return text[: width - 1] + "…" if len(text) > width else text


def pad(text: str, width: int) -> str:
    return cut(text, width).ljust(width)


def fmt_time(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "????-??-?? ??:??"


def span(s: Session) -> float:
    """Seconds from the first to the last message."""
    return (s.last_ts or 0) - (s.first_ts or s.last_ts or 0)


def stats_lines(sessions: List[Session], prices, now: float) -> List[str]:
    """A small table for the fzf header: sessions, projects, cost and Claude's working time
    today, in the last 7 and 30 days and since the oldest session, then the top projects.
    Cost and time count by the day they were spent; a session counts in a period it was active in."""
    today = time.localtime(now)

    def day(days_back: int) -> str:  # noon, so a DST change cannot shift the date
        return local_day(time.mktime(today[:2] + (today[2] - days_back, 12, 0, 0, 0, 0, -1)))

    oldest = min((local_day(s.first_ts) for s in sessions if s.first_ts), default=day(0))
    periods = [("Today", day(0)), ("Last 7 days", day(6)), ("Last 30 days", day(29)), (f"Since {oldest}", "")]

    lines = [f"{'':<18}{'SESSIONS':>9}{'PROJECTS':>10}{'COST':>10}{'CLAUDE TIME':>13}"]
    for label, since in periods:
        active = [s for s in sessions if local_day(s.last_ts) >= since]
        cost = sum(_cost_since(s, prices, since) for s in active)
        work = sum(t for s in active for d, t in s.daily_work.items() if d >= since)
        projects = len({s.project for s in active})
        lines.append(f"{label:<18}{len(active):>9,}{projects:>10,}{fmt_money(cost):>10}{fmt_total(work):>13}")

    by_project: Dict[str, float] = {}
    for s in sessions:
        by_project[s.project] = by_project.get(s.project, 0.0) + _cost_since(s, prices, "")
    top = sorted(by_project.items(), key=lambda kv: kv[1], reverse=True)[:3]
    lines.append("Top projects: " + " · ".join(f"{name} {fmt_money(cost)}" for name, cost in top))
    return lines


def _cost_since(s: Session, prices, since: str) -> float:
    """Cost of the days from `since` on; models without a known price count as 0."""
    return sum(u.cost(prices) or 0.0 for d, u in s.daily_usage.items() if d >= since)


def fmt_money(dollars: float) -> str:
    return f"${dollars:,.0f}"


def fmt_total(seconds: float) -> str:
    hours, minutes = divmod(int(seconds // 60), 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours:,}h {minutes}m" if minutes else f"{hours:,}h"


def fmt_duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# --- interactive picker ---

def me_command() -> str:
    return f"{shlex.quote(sys.executable)} -m ccs"


def fzf_lines(sessions: List[Session], prices, live: Dict[str, Live]) -> List[str]:
    # hidden fields after the visible columns: 7 = transcript file, 8 = cwd, 9 = session id
    return [header_row()] + [f"{row(s, prices, live)}\t{s.file}\t{s.cwd}\t{s.id}" for s in sessions]


def switch_actions(prompt: str, me: str) -> str:
    """fzf actions for Ctrl-S. The list filters itself (fuzzy, over title, project and date);
    text search turns fzf's own filter off and asks ccs for the matching sessions on every keystroke."""
    if prompt == TEXT_PROMPT:
        return (f"change-prompt({LIST_PROMPT})+enable-search+change-preview({me} --preview {{7}})"
                f"+reload({me} --rows '')+unbind(change)")
    return (f"change-prompt({TEXT_PROMPT})+disable-search+change-preview({me} --preview {{7}} --find {{q}})"
            f"+reload({me} --rows {{q}})+rebind(change)")


def pick(sessions: List[Session], prices, live: Dict[str, Live], query: Optional[str] = None) -> int:
    """The list; with a query it opens in text search."""
    fzf = os.environ.get("FZF") or shutil.which("fzf")
    if not fzf:
        print("ccs: fzf is required (https://github.com/junegunn/fzf). Use --list for a plain list.", file=sys.stderr)
        return 1

    me = me_command()
    table, *rows, top = stats_lines(sessions, prices, time.time())
    stats = "\n".join([f"{DIM}{table}{RESET}", *rows, f"{DIM}{top}{RESET}"])
    keys = "Enter: resume · Ctrl-S: search inside conversations · Esc: quit"
    header = f"{stats}\n\n{keys}\n{legend(sessions, live)}\n "  # blank lines set the parts apart
    cmd = [
        fzf, "--ansi", "--layout=reverse", "--header-lines=1", "--delimiter=\t", "--with-nth=1..6",
        "--tabstop=1",  # columns are padded already; a tab adds one space, not up to eight
        "--no-sort", "--tiebreak=index",
        f"--header={header}", "--preview-window=right,50%,wrap",
        f"--bind=ctrl-s:transform:{me} --switch",
        f"--bind=change:reload:{me} --rows {{q}}",
    ]
    if query is None:
        shown = sessions
        cmd += [f"--prompt={LIST_PROMPT}", f"--preview={me} --preview {{7}}", "--bind=start:unbind(change)"]
    else:
        shown = search(sessions, query, CACHE_DIR)
        cmd += [f"--prompt={TEXT_PROMPT}", f"--preview={me} --preview {{7}} --find {{q}}", "--disabled",
                f"--query={query}"]
    lines = fzf_lines(shown, prices, live)
    result = subprocess.run(cmd, input="\n".join(lines), stdout=subprocess.PIPE, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return 0  # Esc or Ctrl-C

    fields = result.stdout.rstrip("\n").split("\t")
    cwd, session_id = fields[7], fields[8]
    if not os.path.isdir(cwd):
        print(f"Folder {cwd} no longer exists. Session ID: {session_id}", file=sys.stderr)
        return 1
    argv = resume_command(session_id, live_status(LIVE_DIR).get(session_id))  # now, not when the list was made
    if argv is None:
        return 0
    os.chdir(cwd)
    os.execvp(argv[0], argv)
    return 0  # not reached



def print_preview(path: Path, words: Optional[List[str]] = None) -> None:
    """The session's title, folder, branch and id, then your prompts; in text search, the messages that match."""
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
    if words:
        hits = snippets(path, words)
        if not hits:
            print(f"{DIM}Only the title matches.{RESET}")
        for ts, who, snippet in hits:
            label = "you   " if who == "you" else "Claude"
            print(f"{DIM}{fmt_time(ts)} {label}{RESET}  {highlight(snippet, words, HIT, RESET)}")
        return
    for i, (ts, text) in enumerate(prompts(path)):
        if i == 40:
            print(f"{DIM}…{RESET}")
            break
        print(f"{DIM}{fmt_time(ts)}{RESET}  {' '.join(text.split())[:300]}")
