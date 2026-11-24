import json
import os
from pathlib import Path

import pytest

from ccs import cli
from ccs.sessions import Live, parse_session, rename_session

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def transcript(tmp_path) -> Path:
    path = tmp_path / "-code-api" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "sessionId": SID, "timestamp": "2026-11-20T10:00:00Z",
                                "cwd": "/code/api", "message": {"content": "Fix the refund worker"}}))  # no final newline
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return path


def test_rename_writes_what_claude_code_writes_and_keeps_the_mtime(transcript):
    rename_session(transcript, SID, "refund-retries")
    lines = transcript.read_text().splitlines()
    assert lines[-2:] == [
        '{"type":"custom-title","customTitle":"refund-retries","sessionId":"%s"}' % SID,
        '{"type":"agent-name","agentName":"refund-retries","sessionId":"%s"}' % SID,
    ]
    assert json.loads(lines[0])["type"] == "user"  # the old last line was finished, not glued to ours
    side = transcript.with_suffix("") / "custom-title.json"
    assert side.read_text() == '{"customTitle":"refund-retries"}'
    assert oct(side.stat().st_mode & 0o777) == "0o600"
    assert transcript.stat().st_mtime_ns == 1_000_000_000
    assert parse_session(transcript).title == "refund-retries"


def answer(reply):
    asked = []

    def ask(question):
        asked.append(question)
        return reply
    ask.asked = asked
    return ask


def test_ctrl_r_renames_a_closed_session(transcript):
    ask = answer("  refund   retries ")
    assert cli.rename(transcript, SID, None, ask=ask) == 0
    assert parse_session(transcript).title == "refund retries"
    assert 'Enter keeps "Fix the refund worker"' in ask.asked[0]


@pytest.mark.parametrize("reply", ["", "Fix the refund worker"])
def test_empty_or_same_name_writes_nothing(transcript, reply):
    before = transcript.read_text()
    assert cli.rename(transcript, SID, None, ask=answer(reply)) == 0
    assert transcript.read_text() == before


@pytest.mark.parametrize("live", [Live(pid=4242, busy=False), Live(pid=4242, busy=True, job_id="c0ffee12")])
def test_an_open_session_is_not_renamed(transcript, live):
    before = transcript.read_text()
    ask = answer("anything")
    assert cli.rename(transcript, SID, live, ask=ask) == 1
    assert transcript.read_text() == before
    assert "/rename" in ask.asked[0]  # the message says how to do it instead


def test_reload_after_rename_keeps_the_mode(monkeypatch):
    monkeypatch.setenv("FZF_PROMPT", cli.LIST_PROMPT)
    monkeypatch.setenv("FZF_QUERY", "refund")
    assert cli.refresh_query() == ""          # the list filters itself
    monkeypatch.setenv("FZF_PROMPT", cli.TEXT_PROMPT)
    assert cli.refresh_query() == "refund"    # text search asks ccs again
