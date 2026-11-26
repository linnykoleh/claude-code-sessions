"""The status column and its legend: what each session is doing right now."""

import json
import os
import re

from ccs import cli
from ccs.sessions import Live, Session

BUSY, WAITING, IDLE = Live(pid=1, status="busy"), Live(pid=1, status="waiting"), Live(pid=1, status="idle")


def session(sid="a", interrupted=False) -> Session:
    return Session(id=sid, file="f", cwd="/code/api", title="t", first_ts=1e9, last_ts=1e9 + 60, interrupted=interrupted)


def status_field(s, live, **kwargs) -> str:
    return cli.row(s, {}, live, **kwargs).split("\t")[5]


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_status_column_tells_the_five_states_apart():
    assert {
        "working": status_field(session(), {"a": BUSY}),
        "needs you": status_field(session(), {"a": WAITING}),
        "waiting for a prompt": status_field(session(), {"a": IDLE}),
        "stopped mid-turn": status_field(session(interrupted=True), {}),
        "closed": status_field(session(), {}),
    } == {"working": "◐", "needs you": "◉", "waiting for a prompt": "○", "stopped mid-turn": "!", "closed": " "}


def test_an_open_session_shows_what_claude_code_says_not_how_its_transcript_ends():
    """While Claude works, the transcript ends mid-turn; that is not a stopped session."""
    assert status_field(session(interrupted=True), {"a": BUSY}) == "◐"


def test_working_mark_turns_a_quarter_each_frame():
    assert [status_field(session(), {"a": BUSY}, frame=f) for f in range(5)] == ["◐", "◓", "◑", "◒", "◐"]


def test_each_state_but_closed_has_its_own_colour():
    live = {"w": BUSY, "n": WAITING, "i": IDLE}
    sessions = [session("w"), session("n"), session("i"), session("x", interrupted=True)]
    colours = set()
    for s in sessions:
        mark, coloured = status_field(s, live), status_field(s, live, color=True)
        assert coloured != mark and plain(coloured) == mark and coloured.endswith(cli.RESET)
        colours.add(coloured[: coloured.index(mark)])
    assert len(colours) == 4
    assert status_field(session(), {}, color=True) == " "


def test_legend_counts_the_sessions_in_each_state():
    sessions = [session(sid) for sid in "wnijyz"] + [session("x", interrupted=True)]
    live = {"w": BUSY, "n": WAITING, "i": IDLE, "j": IDLE}
    assert cli.legend(sessions, live) == (
        "◐ 1 working · ◉ 1 needs you · ○ 2 waiting · ! 1 stopped mid-turn · 2 closed")
    assert cli.legend(sessions, live, frame=2).startswith("◑ 1 working")


def test_every_reload_gives_fzf_the_legend_and_the_spinner_of_that_moment(tmp_path, monkeypatch, capsys):
    """fzf reads its header lines again with each reload, so the legend's counts stay true while the list is open."""
    sid = "11111111-2222-3333-4444-555555555555"
    transcript = tmp_path / "projects" / "-code-api" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "sessionId": sid, "timestamp": "2026-11-20T10:00:00Z",
                                      "cwd": "/code/api", "message": {"content": "Fix the refund worker"}}) + "\n")
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (live_dir / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "status": "busy"}))
    monkeypatch.setattr(cli, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cli, "LIVE_DIR", live_dir)
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("CCS_OFFLINE", "1")

    def reload_at(now):
        monkeypatch.setattr(cli.time, "time", lambda: now)
        assert cli.main(["--rows", ""]) == 0
        return [plain(line) for line in capsys.readouterr().out.splitlines()]

    start = 4000.5 * cli.FRAME_SECONDS  # the middle of a frame where the spinner shows its first mark
    first, next_frame = reload_at(start), reload_at(start + cli.FRAME_SECONDS)
    assert first[0] == "◐ 1 working · ◉ 0 needs you · ○ 0 waiting · ! 0 stopped mid-turn · 0 closed"
    assert first[1].strip() == "" and first[2].startswith("LAST ACTIVITY")
    assert [first[3].split("\t")[5], next_frame[3].split("\t")[5]] == ["◐", "◓"]
    assert next_frame[0].startswith("◓ 1 working")


def test_project_and_cost_stay_apart_even_when_both_are_as_wide_as_they_get():
    """fzf shows a tab as one space (--tabstop=1); the COST column adds COST_GAP more in front of the widest cost."""
    s = Session(id="a", file="f", cwd="/code/data-pipelines", title="t", first_ts=1e9, last_ts=1e9)
    prices = {"claude-opus-5-5": {"input": 1.0, "output": 0.0, "cache_write": 0.0, "cache_write_1h": 0.0,
                                  "cache_read": 0.0}}
    s.usage.add("claude-opus-5-5", {"input_tokens": 1234.56})
    line = cli.row(s, prices, {}).replace("\t", " ")
    assert re.search(r"data-pipeli… {%d}\$1,234\.56 " % (cli.COST_GAP + 1), line)
    header = cli.header_row().replace("\t", " ")
    assert header.index("COST") + len("COST") == line.index("$1,234.56") + len("$1,234.56")  # titles stay aligned


def in_project(name: str, n: int = 1):
    return [Session(id=f"{name}{i}", file="f", cwd=f"/code/{name}", title="t", first_ts=1e9, last_ts=1e9)
            for i in range(n)]


def test_project_fits_the_names_of_nearly_all_sessions():
    """A few sessions of a project with a long name do not push COST away for all the others."""
    sessions = in_project("claude-code-sessions", 19) + in_project("employee-performance-monitoring")
    cols = cli.columns(sessions, {})
    assert cols.project == len("claude-code-sessions")
    assert cli.row(sessions[0], {}, {}, cols).split("\t")[1] == "claude-code-sessions"
    assert cli.row(sessions[-1], {}, {}, cols).split("\t")[1] == "employee-performanc…"
    assert cli.columns(in_project("employee-performance-monitoring"), {}).project == cli.PROJECT_MAX
    assert cli.columns(in_project("api"), {}).project == cli.PROJECT_MIN  # short names keep the usual width


def test_a_narrow_list_gives_title_its_room_first():
    sessions = in_project("claude-code-sessions", 5)
    wide = cli.columns(sessions, {}, width=200)
    assert wide.project == len("claude-code-sessions")
    narrow = cli.columns(sessions, {}, width=77)  # the list's half of a 160-column terminal
    assert narrow.project == 77 - cli.ROW_FRAME - narrow.cost - cli.TITLE_MIN
    assert cli.columns(sessions, {}, width=40).project == cli.PROJECT_MIN  # a tiny terminal still names the project


def test_columns_stay_aligned_whatever_their_widths():
    sessions = in_project("claude-code-sessions", 3)
    cols = cli.columns(sessions, {})
    header, line = cli.header_row(cols).replace("\t", " "), cli.row(sessions[0], {}, {}, cols).replace("\t", " ")
    cost = cli.cost_text(sessions[0], {})
    assert header.index("COST") + len("COST") == line.index(cost) + len(cost)
    # the largest cost is COST_GAP and the tab's cell away from the name
    assert line.index(cost) - line.index("claude-code-sessions") - len("claude-code-sessions") == cli.COST_GAP + 1


def test_list_width_matches_fzf_and_the_terminal(monkeypatch):
    """Before fzf runs there are no FZF_ sizes; the preview takes half of the terminal, less 4 (measured in fzf 0.75)."""
    for total, preview in ((160, 76), (101, 46)):
        monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda t=total: os.terminal_size((t, 40)))
        assert cli.list_width({}) == cli.list_width({"FZF_COLUMNS": str(total), "FZF_PREVIEW_COLUMNS": str(preview)})
    assert cli.list_width({"FZF_COLUMNS": "160", "FZF_PREVIEW_COLUMNS": "76"}) == 77
