"""ccs (Claude Code Sessions): browse and resume every Claude Code session across all projects.

  Enter   resume the session in its project folder (claude --resume <id>)
  Type    to search titles, projects, branches, dates and conversations (your prompts and Claude's replies)
  Ctrl-R  rename the session, as /rename does
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from ccs import __version__
from ccs.index import load_sessions
from ccs.launch import ask_tty, resume_command, where
from ccs.search import highlight, query_words, search, snippets
from ccs.pricing import context_window, load_prices
from ccs.sessions import Live, Session, Turn, live_status, local_day, parse_session, rename_session, turns

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
LIVE_DIR = CLAUDE_DIR / "sessions"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cc-sessions"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
TIME_WIDTH, TITLE_WIDTH = 19, 60  # the date is 16 wide; 3 more keep it apart from PROJECT
# PROJECT fits the names of PROJECT_SHARE of the sessions, within these bounds, so a few sessions of a project with a
# long name do not push COST away for all the others; a narrow terminal takes it down to PROJECT_MIN so that TITLE
# keeps TITLE_MIN. COST is as wide as the largest cost, with COST_GAP in front of it.
PROJECT_MIN, PROJECT_MAX, TITLE_MIN, COST_GAP = 12, 28, 20, 0
PROJECT_SHARE = 0.95
HIT = "\033[1;33m"  # search words in the preview
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"
BRIGHT_GREEN, LIGHT_BLUE, PINK = "\033[38;5;41m", "\033[38;5;75m", "\033[1;38;5;213m"
# What a session is doing, in the order the legend lists the states; the names are what the legend says.
WORKING, NEEDS_YOU, WAITING, STOPPED, CLOSED = (
    "working", "needs you", "waiting", "stopped mid-turn", "closed")
SPINNER = "◐◓◑◒"  # the working mark turns a quarter each frame
FRAME_SECONDS = 0.3  # how often fzf reloads the open list (fzf 0.74.5 and later)
REREAD_SECONDS = 2.0  # a reload on the timer reads a changed transcript again at most this often
# The timer event every(N) came in fzf 0.73; before 0.74.5, fzf fired focus only when the cursor's row number
# changed, so after a reload the preview went on showing the session that was in that row before.
LIVE_FZF = (0, 74, 5)
STATUS_MARKS = {WORKING: SPINNER[0], NEEDS_YOU: "◉", WAITING: "○", STOPPED: "!", CLOSED: " "}
STATUS_COLORS = {WORKING: BRIGHT_GREEN, NEEDS_YOU: PINK, WAITING: LIGHT_BLUE, STOPPED: RED, CLOSED: ""}
BAR_WIDTH, EIGHTHS = 20, " ▏▎▍▌▋▊▉"
PROMPT_CELLS = 300  # of each prompt in the preview; longer ones end in …
REPLY_CELLS = 200  # of each reply of Claude in the conversation: its first sentences say what it did
# Markdown in Claude's replies, which reads as noise once the reply is on one line: code fences, bold, headings,
# the rule under a table's header and the bars at the ends of its rows
MARKDOWN = re.compile(r"```[\w+-]*|\*\*|^#{1,6} +|^\|?[ :|-]*-{3,}[ :|-]*\|?$|^\| *| *\|$", re.MULTILINE)
PREVIEW_TURNS = 40  # the newest exchanges in the preview; past them, how many are left out and the very first one
BREAK_SECONDS = 30 * 60  # a pause this long before your next prompt gets a line of its own in the preview
# fzf fields: the 7 visible columns, then hidden ones the bindings use
FILE_FIELD, CWD_FIELD, ID_FIELD = 8, 9, 10


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Browse and resume every Claude Code session across all your projects (newest first).",
        epilog="Type to search titles, projects, branches, dates and conversations. Keys: Enter resumes the session "
               "in its folder, Ctrl-R renames the session, Esc quits.",
    )
    parser.add_argument("-s", "--search", nargs="+", metavar="WORD",
                        help="only sessions that have every word in their title, project, branch, date, prompts or "
                             'replies; "two words" is a phrase')
    parser.add_argument("--list", action="store_true", help="print the session list without fzf")
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    parser.add_argument("--preview", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--find", metavar="QUERY", help=argparse.SUPPRESS)  # with --preview: show the hits
    parser.add_argument("--rows", metavar="QUERY", help=argparse.SUPPRESS)  # fzf reload on every keystroke
    parser.add_argument("--state", metavar="DIR", help=argparse.SUPPRESS)  # with --rows: what the open list shows
    parser.add_argument("--tick", action="store_true", help=argparse.SUPPRESS)  # with --rows: a reload on fzf's timer
    parser.add_argument("--rename", nargs=2, metavar=("ID", "FILE"), help=argparse.SUPPRESS)  # fzf Ctrl-R
    args = parser.parse_args(argv)

    if args.preview:
        print_preview(Path(args.preview), query_words(args.find or ""))
        return 0
    if args.rename:
        session_id, path = args.rename
        return rename(Path(path), session_id, live_status(LIVE_DIR).get(session_id))

    max_age = REREAD_SECONDS if args.tick else 0.0
    sessions = load_sessions(PROJECTS_DIR, CACHE_DIR, max_age)
    prices = load_prices(CACHE_DIR, download=args.rows is None)  # a reload never waits for the network
    live = live_status(LIVE_DIR)
    if args.rows is not None:
        shown = listed(sessions, args.rows, Path(args.state) if args.state else None, args.tick, max_age)
        cols = columns(sessions, prices, list_width(os.environ))
        print("\n".join(fzf_lines(shown, prices, live, cols, spinner_frame())))
        return 0
    query = search_query(args.search)
    if args.list:
        cols = columns(sessions, prices)
        print(header_row(cols))
        for s in search(sessions, query or "", CACHE_DIR):
            print(row(s, prices, live, cols))
        return 0
    return pick(sessions, prices, live, query)


def listed(sessions: List[Session], query: str, state: Optional[Path], tick: bool, max_age: float) -> List[Session]:
    """The sessions a reload of the open list shows. fzf keeps the cursor on a row number across reloads, so a row
    added, removed or moved above the cursor would put another session under it, and Enter would open that one.
    Typing lists the sessions that match in the order the list opened with, sessions started since then first, and
    keeps their ids in `state`; a reload on the timer shows those same sessions in the same rows, with what they
    are doing now. A session that is new, or has come to match the query, shows up with the next key you press."""
    if state is not None and tick:
        ids = read_ids(state / "shown")
        if ids is not None:
            by_id = {s.id: s for s in sessions}
            return [by_id[sid] for sid in ids if sid in by_id]
    shown = search(sessions, query, CACHE_DIR, max_age)
    if state is not None:
        place = {sid: i for i, sid in enumerate(read_ids(state / "order") or [])}
        shown = sorted(shown, key=lambda s: place.get(s.id, -1))  # a stable sort keeps new sessions newest first
        write_ids(state / "shown", [s.id for s in shown])
    return shown


def read_ids(path: Path) -> Optional[List[str]]:
    try:
        return path.read_text().split()
    except OSError:
        return None


def write_ids(path: Path, ids: List[str]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # whole or not at all: fzf kills a reload it no longer needs
    tmp.write_text("\n".join(ids))
    tmp.replace(path)


def search_query(words: Optional[List[str]]) -> Optional[str]:
    """`ccs -s "retry policy" refund` -> '"retry policy" refund': an argument with spaces stays a phrase."""
    if not words:
        return None
    return " ".join(f'"{w}"' if " " in w.strip() else w for w in words)


# --- list rows ---

# The title goes last: when the fzf pane is narrow, only the title gets cut.
class Columns(NamedTuple):
    project: int = PROJECT_MIN
    cost: int = len("$1,234.56")


# What a row takes besides PROJECT, COST and TITLE: the date, the tabs (one cell each with --tabstop=1), TIME, CTX
# and the status mark.
ROW_FRAME = TIME_WIDTH + 1 + 1 + COST_GAP + 1 + 4 + 1 + 4 + 1 + 1 + 1


def columns(sessions: List[Session], prices, width: Optional[int] = None) -> Columns:
    """PROJECT as wide as the names of nearly all sessions and COST as the largest cost. A name is cut when it is
    longer than that, beyond PROJECT_MAX, or when a list `width` cells wide would leave TITLE less than TITLE_MIN."""
    cost = max([len("COST")] + [len(cost_text(s, prices)) for s in sessions])
    names = sorted(len(s.project) for s in sessions)
    most = names[max(0, math.ceil(len(names) * PROJECT_SHARE) - 1)] if names else 0
    project = min(max(len("PROJECT"), most), PROJECT_MAX)
    if width is not None:
        project = min(project, width - ROW_FRAME - cost - TITLE_MIN)
    return Columns(max(project, PROJECT_MIN), cost)


def list_width(env) -> int:
    """Cells the list's rows have, from fzf's sizes in a reload; before fzf runs, from the terminal: the preview takes
    half of it less 4, and its borders and padding, the pointer and the scrollbar 7 more (measured in fzf 0.75)."""
    try:
        total, preview = int(env["FZF_COLUMNS"]), int(env["FZF_PREVIEW_COLUMNS"])
    except (KeyError, ValueError):
        total = shutil.get_terminal_size().columns
        preview = total // 2 - 4
    return total - preview - 7


def header_row(cols: Columns = Columns()) -> str:
    return "\t".join([pad("LAST ACTIVITY", TIME_WIDTH), pad("PROJECT", cols.project),
                      "COST".rjust(COST_GAP + cols.cost), "TIME", " CTX", " ", "TITLE"])


def cost_text(s: Session, prices) -> str:
    cost = s.usage.cost(prices)
    return fmt_money(cost) if cost is not None else "—"


def row(s: Session, prices, live: Dict[str, Live], cols: Columns = Columns(), color: bool = False,
        frame: int = 0) -> str:
    return "\t".join([
        pad(fmt_time(s.last_ts), TIME_WIDTH),
        pad(s.project, cols.project),
        cost_text(s, prices).rjust(COST_GAP + cols.cost),
        fmt_duration(span(s)).rjust(4),
        ctx_cell(s, prices, color),
        status_mark(status(s, live), frame, color),
        cut(s.title or "(untitled)", TITLE_WIDTH),
    ])


def context_share(s: Session, prices) -> Optional[float]:
    """How full the context window was at the last response, 0..1; None before the first response."""
    if not s.context:
        return None
    return min(s.context / context_window(s.model, prices, s.context), 1.0)


def ctx_cell(s: Session, prices, color: bool) -> str:
    share = context_share(s, prices)
    if share is None:
        return "    "
    text = f"{share:4.0%}"
    return f"{fill_color(share)}{text}{RESET}" if color else text


def fill_color(share: float) -> str:
    """Green while there is room, yellow from half full, red from 80 %, when Claude Code is close to compacting."""
    return GREEN if share < 0.5 else YELLOW if share < 0.8 else RED


def bar(share: float, color: str) -> str:
    """A bar BAR_WIDTH cells wide, precise to an eighth of a cell; the empty part dim."""
    eighths = int(share * BAR_WIDTH * 8)
    full, part = divmod(eighths, 8)
    filled = "█" * full + (EIGHTHS[part] if part else "")
    return f"{color}{filled}{RESET}{DIM}{'░' * (BAR_WIDTH - len(filled))}{RESET}"


def title_lines(s: Session, width: int) -> List[str]:
    """The preview's title in bold, with the lines Claude added (green) and removed (red) in the top right corner.
    A title that does not fit next to them goes on in the line below."""
    added, removed = f"+{s.lines_added:,}", f"-{s.lines_removed:,}"
    corner = len(added) + 1 + len(removed)
    first, rest = split_at_width(s.title or "(untitled)", width - corner - 1)  # at least one space before the corner
    gap = " " * (width - cells(first) - corner)
    lines = [f"{BOLD}{first}{RESET}{gap}{GREEN}{added}{RESET} {RED}{removed}{RESET}"]
    return lines + [f"{BOLD}{rest}{RESET}"] if rest else lines


def preview_width() -> int:
    """Columns of the preview: fzf gives them to the preview command; else the terminal's."""
    try:
        return int(os.environ["FZF_PREVIEW_COLUMNS"])
    except (KeyError, ValueError):
        return shutil.get_terminal_size().columns


def split_at_width(text: str, width: int) -> Tuple[str, str]:
    """`text` split at a space so that the first part takes at most `width` cells; mid-word if a word is longer."""
    used = 0
    for i, ch in enumerate(text):
        used += cells(ch)
        if used > width:
            space = text.rfind(" ", 0, i + 1)
            at = space if space > 0 else i
            return text[:at], text[at:].lstrip()
    return text, ""


def wrap(text: str, width: int) -> List[str]:
    """`text` in lines of at most `width` cells, broken at spaces; only a word longer than a line is cut."""
    lines = []
    while True:
        line, text = split_at_width(text, width)
        if not line:  # not even one character fits
            line, text = text[:1], text[1:]
        lines.append(line)
        if not text:
            return lines


def message_lines(head: str, text: str, width: int, words: Optional[List[str]] = None) -> List[str]:
    """`head` (time, who) dim, then `text` wrapped at spaces, each line going on under the text's first word."""
    indent = cells(head)
    lines = wrap(text, max(width - indent, 20))
    return [(f"{DIM}{head}{RESET}" if i == 0 else " " * indent) + highlight(line, words or [], HIT, RESET)
            for i, line in enumerate(lines)]


def cells(text: str) -> int:
    """How many terminal cells the text takes: wide characters (CJK, most emoji) take two, combining marks none."""
    return sum(0 if unicodedata.category(ch) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def caption(name: str) -> str:
    """A dim name in front of a preview line, padded so the values line up."""
    return f"{DIM}{name:<11}{RESET}"


def usage_lines(s: Session, prices) -> List[str]:
    """The preview's context and cache lines; none before the first response."""
    share = context_share(s, prices)
    if share is None:
        return []
    window = context_window(s.model, prices, s.context)
    lines = [f"{caption('Context')}{bar(share, fill_color(share))} {share:4.0%}  {fmt_tokens(s.context)} of {fmt_tokens(window)} tokens"
             f" · {s.model}"]
    t = {k: sum(m.get(k, 0) for m in s.usage.tokens.values())
         for k in ("input", "cache_write", "cache_write_1h", "cache_read")}
    total_input = sum(t.values())
    if total_input:
        hit = t["cache_read"] / total_input  # high is good: cached input costs a tenth
        color = GREEN if hit >= 0.8 else YELLOW if hit >= 0.5 else RED
        lines.append(f"{caption('Cache')}{bar(hit, color)} {hit:4.0%}  of input read from the prompt cache")
    return lines


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{round(n / 1000)}K" if n >= 1000 else str(n)


def status(s: Session, live: Dict[str, Live]) -> str:
    """What the session is doing: an open session says so itself; a closed one, by how its transcript ends."""
    if s.id in live:
        return {"busy": WORKING, "waiting": NEEDS_YOU}.get(live[s.id].status, WAITING)
    return STOPPED if s.interrupted else CLOSED


def status_mark(state: str, frame: int = 0, color: bool = False) -> str:
    mark = SPINNER[frame % len(SPINNER)] if state == WORKING else STATUS_MARKS[state]
    return f"{STATUS_COLORS[state]}{mark}{RESET}" if color and STATUS_COLORS[state] else mark


def spinner_frame() -> int:
    return int(time.time() / FRAME_SECONDS)


def legend(sessions: List[Session], live: Dict[str, Live], frame: int = 0, color: bool = False) -> str:
    """How many of the sessions are in each state: "◐ 1 working · ◉ 0 needs you · … · 412 closed"."""
    states = [status(s, live) for s in sessions]
    return " · ".join(f"{status_mark(state, frame, color)} {states.count(state)} {state}".strip()
                      for state in STATUS_MARKS)


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
    periods = [("Today", day(0)), ("Last 7 days", day(6)), ("Last 30 days", day(29)), (f"All (since {oldest})", "")]

    lines = [f"{'':<24}{'SESSIONS':>9}{'PROJECTS':>10}{'COST':>13}{'CLAUDE TIME':>13}"]
    for label, since in periods:
        active = [s for s in sessions if local_day(s.last_ts) >= since]
        cost = sum(_cost_since(s, prices, since) for s in active)
        work = sum(t for s in active for d, t in s.daily_work.items() if d >= since)
        projects = len({s.project for s in active})
        lines.append(f"{label:<24}{len(active):>9,}{projects:>10,}{fmt_money(cost):>13}{fmt_total(work):>13}")

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
    return f"${dollars:,.2f}"


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

def rename(path: Path, session_id: str, live: Optional[Live], ask=None) -> int:
    """Ctrl-R: ask for a new name on the terminal and write it the way /rename does."""
    ask = ask or ask_tty
    if live is not None:
        ask(f"This session is open in a running Claude ({where(live)}), which would write its old name back.\n"
            "Rename it there with /rename. Press Enter to go back. ")
        return 1
    current = parse_session(path, with_usage=False).title
    name = " ".join(ask(f'New name (Enter keeps "{current}"): ').split())
    if name and name != current:
        rename_session(path, session_id, name)
    return 0


def me_command() -> str:
    return f"{shlex.quote(sys.executable)} -m ccs"


def fzf_version(fzf: str) -> Tuple[int, ...]:
    """(0, 74, 5) for fzf 0.74.5; () when it cannot tell."""
    try:
        out = subprocess.run([fzf, "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    found = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    return tuple(int(part or 0) for part in found.groups()) if found else ()


FZF_HEADER_LINES = 3  # the legend, a blank line, the column titles: fzf reads them again with each reload


def fzf_lines(sessions: List[Session], prices, live: Dict[str, Live], cols: Columns = Columns(),
              frame: int = 0) -> List[str]:
    return [legend(sessions, live, frame, color=True), "", header_row(cols)] + [
        f"{row(s, prices, live, cols, color=True, frame=frame)}\t{s.file}\t{s.cwd}\t{s.id}" for s in sessions]


def fzf_command(fzf: str, header: str, state: Path, query: Optional[str], live_list: bool) -> List[str]:
    me = me_command()
    rows_cmd = f"{me} --rows {{q}} --state {shlex.quote(str(state))}"
    preview_cmd = f"{me} --preview {{{FILE_FIELD}}} --find {{q}}"
    rename_cmd = f"{me} --rename {{{ID_FIELD}}} {{{FILE_FIELD}}}"
    cmd = [
        fzf, "--ansi", "--layout=reverse", f"--header-lines={FZF_HEADER_LINES}", "--delimiter=\t",
        f"--with-nth=1..{FILE_FIELD - 1}",
        "--tabstop=1",  # columns are padded already; a tab adds one space, not up to eight
        "--prompt=sessions> ", f"--header={header}",
        "--preview-window=right,50%,wrap,noinfo",  # no scroll position over the lines added and removed
        # fzf's own filter sees only the row; ccs also looks at what was said, so it does the searching
        "--disabled",
    ]
    if live_list:
        # The list reloads itself on a timer, so statuses stay true and the working mark turns. fzf cuts a reload
        # short when the next one starts, and a list whose reloads never finish never changes: each reload pauses
        # the timer until the list has loaded. reload-sync, not reload: fzf shows a list that is still being read,
        # and pulls the cursor up into the rows read so far. --track would drop the keys typed during a reload, so
        # the cursor keeps its row number, and the rows stay where they are (see listed). With --id-nth, the same
        # session under the cursor after a reload is no new focus, so the preview runs when the session or the
        # query changes, not with every frame as --preview would. FZF_DEFAULT_OPTS comes before these options:
        # --no-preview and --no-track keep a --preview or --track from it off.
        timer = f"every({FRAME_SECONDS})"
        pause, preview = f"unbind({timer})", f"preview({preview_cmd})"
        cmd += [
            "--no-preview", "--no-track",
            f"--bind={timer}:{pause}+reload-sync({rows_cmd} --tick)", f"--bind=load:rebind({timer})",
            f"--id-nth={ID_FIELD}", f"--bind=focus:{preview}",
            f"--bind=change:{pause}+reload({rows_cmd})+{preview}",
            f"--bind=ctrl-r:{pause}+execute({rename_cmd})+reload-sync({rows_cmd})+{preview}",
            # The row under the cursor, as macOS selects a row: a band of the system blue (#0a84ff, 33 here, which
            # every 256-colour terminal shows) across the whole line, all its text white; strip drops the row's own
            # colours there, which would not read on the blue. The pointer takes the band's colour, so it does not
            # show, and the other rows get no gutter marks.
            "--highlight-line", "--gutter= ", "--color=bg+:33,fg+:231:regular:strip,pointer:33",
            # a long list leaves the scrollbar a single row: a half block is easier to find than a thin line,
            # in fzf's own grey of the other lines
            "--scrollbar=▐",
            "--header-lines-border=bottom",  # a line between the column titles and the rows
        ]
    else:
        cmd += [
            f"--bind=change:reload:{rows_cmd}", f"--preview={preview_cmd}",
            f"--bind=ctrl-r:execute({rename_cmd})+reload({rows_cmd})",
        ]
    if query:
        cmd.append(f"--query={query}")
    return cmd


def _hang_up(signum, frame):
    raise SystemExit(128 + signum)


def pick(sessions: List[Session], prices, live: Dict[str, Live], query: Optional[str] = None) -> int:
    """The list, searched as you type; it opens with `query` typed in, if one is given."""
    fzf = os.environ.get("FZF") or shutil.which("fzf")
    if not fzf:
        print("ccs: fzf is required (https://github.com/junegunn/fzf). Use --list for a plain list.", file=sys.stderr)
        return 1

    table, *rows, top = stats_lines(sessions, prices, time.time())
    stats = "\n".join([f"{DIM}{table}{RESET}", *rows, f"{DIM}{top}{RESET}"])
    keys = "Type to search · Enter: resume · Ctrl-R: rename · Esc: quit"
    header = f"{stats}\n\n{keys}"  # a blank line sets the parts apart
    shown = search(sessions, query or "", CACHE_DIR)
    lines = fzf_lines(shown, prices, live, columns(sessions, prices, list_width({})), spinner_frame())
    live_list = fzf_version(fzf) >= LIVE_FZF
    state = Path(tempfile.mkdtemp(prefix="ccs-"))  # what the open list shows, for its reloads (see listed)
    hang_up = signal.signal(signal.SIGHUP, _hang_up)  # a closed terminal tab still removes the state
    try:
        write_ids(state / "order", [s.id for s in sessions])
        write_ids(state / "shown", [s.id for s in shown])
        result = subprocess.run(fzf_command(fzf, header, state, query, live_list), input="\n".join(lines),
                                stdout=subprocess.PIPE, text=True)
    finally:
        signal.signal(signal.SIGHUP, hang_up)
        shutil.rmtree(state, ignore_errors=True)
    if result.returncode != 0 or not result.stdout.strip():
        return 0  # Esc or Ctrl-C

    fields = result.stdout.rstrip("\n").split("\t")
    cwd, session_id = fields[CWD_FIELD - 1], fields[ID_FIELD - 1]
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
    """The session's title with the lines Claude changed, its folder, branch and id, then the conversation, newest
    first; while you search, the messages that have the words, if any do, newest first too."""
    s = parse_session(path) if path.exists() else None
    if s is None:
        print(f"{path} not found")
        return
    for line in title_lines(s, preview_width()):
        print(line)
    print(f"{caption('Folder')}{s.cwd}")
    if s.branch:
        print(f"{caption('Branch')}{s.branch}")
    print(f"{caption('Session ID')}{s.id}")
    for line in usage_lines(s, load_prices(CACHE_DIR, download=False)):  # the list itself fetches the table
        print(line)
    print()
    width = preview_width()
    hits = snippets(path, words) if words else []
    if hits:
        print(f"{caption('Found')}{plural(len(hits), 'message')}, newest first")
        for ts, who, snippet in hits[:-PREVIEW_TURNS - 1:-1]:
            print("\n".join(message_lines(message_head(ts, who), snippet, width, words)))
        if len(hits) > PREVIEW_TURNS:
            print(f"{DIM}… {plural(len(hits) - PREVIEW_TURNS, 'more message')}{RESET}")
        return
    # nothing typed, or only the title, project, branch or date matched
    exchanges = turns(path)
    if exchanges:
        print(f"{caption('Prompts')}{len(exchanges)}, newest first")
    for line in conversation_lines(exchanges, width):
        print(line)


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def conversation_lines(exchanges: List[Turn], width: int) -> List[str]:
    """The newest PREVIEW_TURNS exchanges, newest first: your prompt, then Claude's last reply to it. A pause of
    BREAK_SECONDS or more before a prompt gets a line. When more are left out than the first one, a line says how
    many, and the very first exchange closes the preview: what the session set out to do."""
    shown, first = exchanges[::-1], None
    if len(exchanges) > PREVIEW_TURNS + 1:
        shown, first = shown[:PREVIEW_TURNS], exchanges[0]
    lines: List[str] = []
    for newer, t in zip([None] + shown, shown):
        if newer and newer.ts and t.end_ts and newer.ts - t.end_ts >= BREAK_SECONDS:
            lines.append(f"{' ' * cells(fmt_time(None))}{DIM}── {fmt_duration(newer.ts - t.end_ts)} break ──{RESET}")
        lines += turn_lines(t, width)
    if first:
        more = len(exchanges) - PREVIEW_TURNS - 1
        lines.append(f"{DIM}… {plural(more, 'more prompt')} · the first one:{RESET}")
        lines += turn_lines(first, width)
    return lines


def turn_lines(t: Turn, width: int) -> List[str]:
    lines = message_lines(message_head(t.ts, "you"), clip(t.prompt, PROMPT_CELLS), width)
    if t.reply:
        lines += message_lines(message_head(t.reply_ts, "claude"), reply_line(t.reply), width)
    return lines


def reply_line(reply: str) -> str:
    """Claude's reply on one line, its first REPLY_CELLS cells, without the Markdown that would read as noise there."""
    return clip(MARKDOWN.sub("", "\n".join(line.strip() for line in reply.splitlines())), REPLY_CELLS)


def message_head(ts: Optional[float], who: str) -> str:
    return f"{fmt_time(ts)} {'You   ' if who == 'you' else 'Claude'}  "


def clip(text: str, width: int) -> str:
    """The message on one line, its first `width` cells; a longer one ends in …"""
    shown, rest = split_at_width(" ".join(text.split()), width)
    return shown + ("…" if rest else "")
