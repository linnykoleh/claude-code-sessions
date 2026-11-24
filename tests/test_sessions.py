import json
from pathlib import Path

import pytest

from ccs.cli import fmt_duration, pad, row, stats_lines
from ccs.index import load_sessions
from ccs.sessions import Session, Usage, live_status, parse_session, prompts

PRICES = {
    "claude-opus-5-5": {
        "input": 4e-6, "output": 2e-5, "cache_write": 5e-6, "cache_write_1h": 8e-6, "cache_read": 2e-7,
    }
}
SID = "11111111-2222-3333-4444-555555555555"


def write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def user(text, ts, **extra):
    return {"type": "user", "sessionId": SID, "timestamp": ts, "cwd": "/code/payments-api",
            "gitBranch": "PAY-512-fix", "message": {"role": "user", "content": text}, **extra}


def assistant(msg_id, ts, usage=None, session_id=SID, request_id="req_1", stop="end_turn", content=()):
    return {"type": "assistant", "sessionId": session_id, "timestamp": ts, "requestId": request_id,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage or {}, "stop_reason": stop,
                        "content": list(content)}}


def tool_result(ts):
    return {"type": "user", "sessionId": SID, "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_creation_input_tokens": 1000,
         "cache_creation": {"ephemeral_1h_input_tokens": 1000}, "cache_read_input_tokens": 5000}


@pytest.fixture
def projects(tmp_path):
    return tmp_path / "projects"


def make_session(projects, records, name=SID):
    return write_jsonl(projects / "-code-payments-api" / f"{name}.jsonl", records)


def test_reads_cwd_branch_times_and_first_prompt_as_title(projects):
    f = make_session(projects, [
        user("<command-name>/clear</command-name>", "2026-11-20T10:00:00Z"),
        user("Fix the sync job", "2026-11-20T10:01:00Z"),
        assistant("msg_1", "2026-11-20T12:01:00Z", USAGE),
    ])
    s = parse_session(f)
    assert s.cwd == "/code/payments-api"
    assert s.project == "payments-api"
    assert s.branch == "PAY-512-fix"
    assert s.title == "Fix the sync job"
    assert s.last_ts - s.first_ts == 2 * 3600 + 60


def test_custom_title_wins_over_ai_title(projects):
    f = make_session(projects, [
        user("hello", "2026-11-20T10:00:00Z"),
        {"type": "ai-title", "aiTitle": "Greeting", "sessionId": SID},
        {"type": "custom-title", "customTitle": "my-name", "sessionId": SID},
        {"type": "ai-title", "aiTitle": "Later AI title", "sessionId": SID},
    ])
    assert parse_session(f).title == "my-name"


def test_cost_counts_each_response_once_and_prices_1h_cache_writes(projects):
    f = make_session(projects, [
        user("go", "2026-11-20T10:00:00Z"),
        assistant("msg_1", "2026-11-20T10:00:01Z", USAGE),
        assistant("msg_1", "2026-11-20T10:00:02Z", USAGE),  # same response logged twice
    ])
    expected = 10 * 4e-6 + 100 * 2e-5 + 1000 * 8e-6 + 5000 * 2e-7
    assert parse_session(f).usage.cost(PRICES) == pytest.approx(expected)


def test_subagents_count_but_records_of_other_sessions_do_not(projects):
    f = make_session(projects, [user("go", "2026-11-20T10:00:00Z")])
    write_jsonl(f.with_suffix("") / "subagents" / "agent-a1.jsonl", [
        assistant("msg_2", "2026-11-20T10:00:05Z", USAGE),
        assistant("msg_3", "2026-11-20T10:00:06Z", USAGE, session_id="other-session"),
    ])
    s = parse_session(f)
    assert s.usage.tokens["claude-opus-5-5"]["output"] == 100


def test_unknown_model_has_no_cost(projects):
    f = make_session(projects, [assistant("msg_1", "2026-11-20T10:00:00Z", USAGE)])
    assert parse_session(f).usage.cost({}) is None


def test_prompts_skip_commands_and_tool_results(projects):
    f = make_session(projects, [
        user("<command-name>/model</command-name>", "2026-11-20T10:00:00Z"),
        {"type": "user", "timestamp": "2026-11-20T10:00:01Z",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        user("real question", "2026-11-20T10:00:02Z"),
    ])
    assert [text for _, text in prompts(f)] == ["real question"]


def test_index_is_newest_first_and_reuses_cache(projects, tmp_path):
    make_session(projects, [user("old", "2026-11-01T10:00:00Z")], name="old")
    make_session(projects, [user("new", "2026-11-20T10:00:00Z")], name="new")
    cache = tmp_path / "cache"
    assert [s.id for s in load_sessions(projects, cache)] == ["new", "old"]

    # the cached entry is used as long as the transcript is unchanged
    index = json.loads((cache / "index.json").read_text())
    entry = next(v for k, v in index["sessions"].items() if k.endswith("old.jsonl"))
    entry["session"]["title"] = "from cache"
    (cache / "index.json").write_text(json.dumps(index))
    assert {s.id: s.title for s in load_sessions(projects, cache)}["old"] == "from cache"


def test_formatting_helpers():
    assert fmt_duration(59) == "0m"
    assert fmt_duration(3 * 3600) == "3h"
    assert fmt_duration(2 * 86400) == "2d"
    assert pad("abcdef", 4) == "abc…"
    assert pad("ab", 4) == "ab  "


T0, T1, T2, T3 = (f"2026-11-20T10:00:0{i}Z" for i in range(4))
BASH = {"type": "tool_use", "name": "Bash"}


@pytest.mark.parametrize("ending, interrupted", [
    ([assistant("m1", T1)], False),                                   # Claude answered
    ([assistant("m1", T1, stop="tool_use", content=[BASH]), tool_result(T2),
      assistant("m2", T3)], False),                                   # answered after using a tool
    ([assistant("m1", T1), user("<command-name>/exit</command-name>", T2)], False),  # commands do not count
    ([assistant("m1", T1, stop="tool_use", content=[BASH]), tool_result(T2)], True),  # closed mid-tool
    ([assistant("m1", T1, stop="tool_use", content=[BASH]),
      {**tool_result(T2), "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}],
     True),                                                           # Esc
    ([assistant("m1", T1), user("and now?", T2)], True),              # prompt never answered
    ([assistant("m1", T1, stop="tool_use", content=[{"type": "tool_use", "name": "StructuredOutput"}]),
      tool_result(T2)], False),                                       # claude -p --json-schema finished
])
def test_interrupted_when_the_last_turn_did_not_finish(projects, ending, interrupted):
    f = make_session(projects, [user("go", T0), *ending])
    assert parse_session(f).interrupted is interrupted


def test_live_status_keeps_running_processes_only(tmp_path):
    import os, subprocess, sys
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    (tmp_path / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "a", "status": "busy"}))
    (tmp_path / "2.json").write_text(json.dumps({"pid": dead.pid, "sessionId": "b", "status": "idle"}))
    (tmp_path / "3.json").write_text("not json")
    assert live_status(tmp_path) == {"a": "busy"}
    assert live_status(tmp_path / "missing") == {}


def test_row_puts_title_last_and_marks_status():
    s = Session(id="a", file="f", cwd="/code/payments-api", title="Fix it", first_ts=1e9, last_ts=1e9 + 3 * 3600)
    fields = row(s, PRICES, {}).split("\t")
    assert [f.strip() for f in fields[2:]] == ["$0", "3h", "", "Fix it"]
    assert row(s, PRICES, {"a": "busy"}).split("\t")[4] == "●"
    assert row(s, PRICES, {"a": "idle"}).split("\t")[4] == "○"
    s.interrupted = True
    assert row(s, PRICES, {}).split("\t")[4] == "!"



def test_usage_and_claude_time_are_split_by_day(projects):
    f = make_session(projects, [
        user("go", "2026-11-20T10:00:00"),
        assistant("m1", "2026-11-20T10:00:05", {"output_tokens": 100}, request_id="r1"),
        {"type": "system", "subtype": "turn_duration", "durationMs": 90_000, "timestamp": "2026-11-20T10:01:30"},
        user("more", "2026-11-21T09:00:00"),
        assistant("m2", "2026-11-21T09:00:05", {"output_tokens": 300}, request_id="r2"),
        {"type": "system", "subtype": "turn_duration", "durationMs": 30_000, "timestamp": "2026-11-21T09:00:30"},
    ])
    s = parse_session(f)
    assert {d: u.tokens["claude-opus-5-5"]["output"] for d, u in s.daily_usage.items()} == {
        "2026-11-20": 100, "2026-11-21": 300}
    assert s.daily_work == {"2026-11-20": 90.0, "2026-11-21": 30.0}


def test_stats_table_counts_each_period_by_the_days_it_covers():
    import time
    now = time.mktime((2026, 11, 24, 15, 0, 0, 0, 0, -1))

    def session(cwd, first, last, usage, work):
        s = Session(id=cwd, file="f", cwd=cwd,
                    first_ts=time.mktime(time.strptime(first, "%Y-%m-%d")) + 3600,
                    last_ts=time.mktime(time.strptime(last, "%Y-%m-%d")) + 3600)
        for day, output_tokens in usage.items():  # 50k output tokens = $1
            s.daily_usage.setdefault(day, Usage()).add("claude-opus-5-5", {"output_tokens": output_tokens})
        s.daily_work = work
        return s

    sessions = [
        session("/code/api", "2026-11-24", "2026-11-24", {"2026-11-24": 100_000}, {"2026-11-24": 3600}),
        session("/code/web", "2026-11-23", "2026-11-24", {"2026-11-23": 50_000, "2026-11-24": 50_000},
                {"2026-11-23": 1800, "2026-11-24": 1800}),
        session("/code/api", "2026-11-10", "2026-11-10", {"2026-11-10": 50_000}, {"2026-11-10": 600}),
        session("/code/old", "2026-10-01", "2026-10-01", {"2026-10-01": 50_000}, {}),
    ]
    assert stats_lines(sessions, PRICES, now) == [
        "                   SESSIONS  PROJECTS      COST  CLAUDE TIME",
        "Today                     2         2        $3       1h 30m",
        "Last 7 days               2         2        $4           2h",
        "Last 30 days              3         2        $5       2h 10m",
        "Since 2026-10-01          4         3        $6       2h 10m",
        "Top projects: api $3 · web $2 · old $1",
    ]
