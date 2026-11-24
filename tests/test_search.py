import json
import os
from pathlib import Path

import pytest

from ccs import cli
from ccs.search import TEXT_CACHE_VERSION, search, snippets
from ccs.sessions import Session, messages

SID = "11111111-2222-3333-4444-555555555555"


def you(text, ts="2026-11-20T10:00:00Z", **extra):
    return {"type": "user", "sessionId": SID, "timestamp": ts, "cwd": "/code/api", "message": {"content": text}, **extra}


def claude(*blocks, ts="2026-11-20T10:00:05Z", **extra):
    return {"type": "assistant", "sessionId": SID, "timestamp": ts,
            "message": {"id": "m", "model": "claude-opus-5-5", "usage": {}, "content": list(blocks)}, **extra}


def text(t):
    return {"type": "text", "text": t}


def transcript(folder: Path, name: str, records) -> Path:
    path = folder / "-code-api" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def session(path: Path, title: str) -> Session:
    return Session(id=path.stem, file=str(path), cwd="/code/api", title=title, last_ts=1e9)


@pytest.fixture
def projects(tmp_path):
    return tmp_path / "projects"


def test_messages_are_your_prompts_and_claudes_text_only(projects):
    f = transcript(projects, "a", [
        you("<command-name>/model</command-name>"),
        you("Why does the refund retry forever?"),
        claude({"type": "thinking", "thinking": "hidden"}, text("Because of the 409."),
               {"type": "tool_use", "name": "Bash", "input": {"command": "grep secret"}}),
        you([{"type": "tool_result", "content": "secret output"}]),
        claude(text("subagent text"), isSidechain=True),
        claude(text("Fixed.")),
    ])
    assert [(who, t) for _, who, t in messages(f)] == [
        ("you", "Why does the refund retry forever?"),
        ("claude", "Because of the 409."),
        ("claude", "Fixed."),
    ]


@pytest.fixture
def three(projects):
    a = transcript(projects, "a", [you("Налаштуй кешування цін"), claude(text("Готово, кеш на тиждень."))])
    b = transcript(projects, "b", [you("Fix the refund worker"), claude(text("The retry policy was wrong."))])
    c = transcript(projects, "c", [you("hello"), claude(text("Hi! Ask me about Kafka lag."))])
    return [session(a, "Ціни"), session(b, "Refund worker"), session(c, "Greeting")]


def test_search_needs_every_word_as_a_substring_in_any_case(three, tmp_path):
    ids = lambda q: [s.id for s in search(three, q, tmp_path / "cache")]
    assert ids("кеш") == ["a"]                  # finds "кешування" and "кеш"
    assert ids("REFUND policy") == ["b"]        # words from the prompt and the reply
    assert ids("refund kafka") == []
    assert ids("kafka") == ["c"]                # only Claude said it
    assert ids("greeting") == ["c"]             # the title counts too
    assert ids("") == ["a", "b", "c"]           # no query: everything, order kept


def test_quotes_make_a_phrase(three, tmp_path):
    ids = lambda q: [s.id for s in search(three, q, tmp_path / "cache")]
    assert ids('"retry policy"') == ["b"]
    assert ids('"policy retry"') == []
    assert ids('"retry policy') == ["b"]        # an unfinished quote while typing still works


def test_text_cache_is_reused_until_the_transcript_changes(three, tmp_path):
    cache = tmp_path / "cache"
    search(three, "x", cache)
    cache_file = cache / f"text-v{TEXT_CACHE_VERSION}.json"
    data = json.loads(cache_file.read_text())
    a_file = three[0].file
    data["sessions"][a_file]["text"] = "planted"
    cache_file.write_text(json.dumps(data))
    assert [s.id for s in search(three, "planted", cache)] == ["a"]

    with open(a_file, "a") as f:
        f.write(json.dumps(you("one more")) + "\n")
    os.utime(a_file, ns=(1, 1))  # a different mtime
    assert [s.id for s in search(three, "planted", cache)] == []


def test_snippets_show_the_hit_with_some_context(projects):
    long_reply = "x " * 200 + "the retry policy was wrong" + " y" * 200
    f = transcript(projects, "a", [you("Fix the refund worker"), claude(text(long_reply)), you("thanks")])
    hits = snippets(f, ["retry"])
    assert len(hits) == 1
    _, who, snippet = hits[0]
    assert who == "claude" and "the retry policy was wrong" in snippet
    assert snippet.startswith("…") and snippet.endswith("…") and len(snippet) < 250


def test_ccs_s_list_prints_only_matching_sessions(three, projects, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCS_OFFLINE", "1")
    monkeypatch.setattr(cli, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cli, "LIVE_DIR", tmp_path / "live")
    assert cli.main(["-s", "retry", "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("LAST ACTIVITY") and len(lines) == 2 and "Fix the refund worker" in lines[1]


def test_ctrl_s_switches_between_the_list_and_text_search():
    to_text = cli.switch_actions(cli.LIST_PROMPT, "ccs")
    assert to_text.startswith(f"change-prompt({cli.TEXT_PROMPT})+disable-search+")
    assert "reload(ccs --rows {q})" in to_text and "--find {q}" in to_text and to_text.endswith("rebind(change)")
    back = cli.switch_actions(cli.TEXT_PROMPT, "ccs")
    assert back.startswith(f"change-prompt({cli.LIST_PROMPT})+enable-search+")
    assert "reload(ccs --rows '')" in back and "--find" not in back and back.endswith("unbind(change)")
