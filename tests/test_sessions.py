import json
from pathlib import Path

import pytest

from ccs.cli import fmt_duration, pad, resume_command
from ccs.index import load_sessions
from ccs.sessions import parse_session, prompts

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


def assistant(msg_id, ts, usage, session_id=SID, request_id="req_1"):
    return {"type": "assistant", "sessionId": session_id, "timestamp": ts, "requestId": request_id,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage}}


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
    assert resume_command("/my dir", "abc") == "cd '/my dir' && claude --resume abc"
