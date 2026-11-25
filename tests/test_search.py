import json
import os
import re
import subprocess
import time
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


def test_search_also_finds_the_project_branch_and_day(three, tmp_path):
    three[1].cwd, three[1].branch = "/code/payments-api", "PAY-512-retries"
    three[2].last_ts = time.mktime((2026, 11, 24, 15, 0, 0, 0, 0, -1))
    ids = lambda q: [s.id for s in search(three, q, tmp_path / "cache")]
    assert ids("payments") == ["b"]                  # the project folder, not the whole path
    assert ids("code") == []
    assert ids("pay-512") == ["b"]                   # the branch
    assert ids("2026-11-2") == ["c"]                 # the day of the last message
    assert ids("payments policy") == ["b"]           # the words may come from different places
    assert ids("payments kafka") == []


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


@pytest.fixture
def fzf(tmp_path, monkeypatch):
    """Run pick() against a fake fzf; returns what it was given: {"cmd": [...], "rows": [...]}."""
    seen = {}

    def run(cmd, input=None, **kwargs):
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0.75.0 (c7bf2b8e)\n")
        header_lines = int(next(a for a in cmd if a.startswith("--header-lines=")).split("=")[1])
        seen["cmd"], seen["rows"] = cmd, input.splitlines()[header_lines:]  # the sessions, without the header lines
        return subprocess.CompletedProcess(cmd, 130, stdout="")  # Esc

    monkeypatch.setenv("FZF", "fzf")
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cli.subprocess, "run", run)
    return seen


def test_one_search_box_looks_everywhere(three, fzf):
    assert cli.pick(three, {}, {}) == 0
    cmd = fzf["cmd"]
    assert "--disabled" in cmd                                   # fzf does not filter, ccs does
    change = next(a for a in cmd if a.startswith("--bind=change:"))
    assert f"reload:{cli.me_command()} --rows {{q}}" in change or f"reload({cli.me_command()} --rows {{q}}" in change
    assert any(f"--preview {{{cli.FILE_FIELD}}} --find {{q}}" in a for a in cmd)  # the preview shows the hits
    ctrl_r = next(a for a in cmd if a.startswith("--bind=ctrl-r:"))
    assert re.search(r"\+reload(-sync)?\(" + re.escape(f"{cli.me_command()} --rows {{q}}"), ctrl_r)  # same query
    assert not any("ctrl-s" in a for a in cmd)
    assert len(fzf["rows"]) == 3


def test_fzf_draws_nothing_over_the_corner_of_the_preview(three, fzf):
    """When the preview is longer than its window, fzf shows the scroll position in its top right corner,
    over the lines added and removed; the scrollbar shows the position anyway."""
    cli.pick(three, {}, {})
    window = next(a for a in fzf["cmd"] if a.startswith("--preview-window="))
    assert "noinfo" in window.split("=", 1)[1].split(",")


def test_ccs_s_opens_with_the_query_and_its_sessions(three, fzf):
    assert cli.pick(three, {}, {}, '"retry policy"') == 0
    assert '--query="retry policy"' in fzf["cmd"]
    assert [r.split("\t")[-1] for r in fzf["rows"]] == ["b"]


def test_the_keys_line_names_exactly_the_ctrl_keys_ccs_binds(three, fzf):
    """A key the line names but nothing binds does nothing; a key ccs binds but the line leaves out is never found."""
    cli.pick(three, {}, {})
    header = next(a for a in fzf["cmd"] if a.startswith("--header="))
    named = set(re.findall(r"Ctrl-([A-Z])\b", header))
    bound = {a[len("--bind=ctrl-"):].split(":", 1)[0].upper() for a in fzf["cmd"] if a.startswith("--bind=ctrl-")}
    assert named == bound


def test_preview_shows_the_hits_or_your_prompts_when_only_the_title_or_project_matches(three, tmp_path, monkeypatch,
                                                                                      capsys):
    monkeypatch.setenv("CCS_OFFLINE", "1")
    monkeypatch.setenv("FZF_PREVIEW_COLUMNS", "60")
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    refund = Path(three[1].file)

    def body(words):
        cli.print_preview(refund, words)
        return capsys.readouterr().out.split("\n\n", 1)[1]  # what comes after the title, folder and id

    assert f"{cli.HIT}retry{cli.RESET}" in body(["retry"]) and "refund worker" not in body(["retry"])
    assert "Fix the refund worker" in body(["api"]) and "retry" not in body(["api"])  # the project matched
    assert "Fix the refund worker" in body([])


def test_snippets_start_and_end_on_whole_words(projects):
    reply = "alpha " * 30 + "the retry policy" + " omega" * 40
    f = transcript(projects, "a", [claude(text(reply))])
    ((_, _, snippet),) = snippets(f, ["retry"])
    assert snippet.startswith("…") and snippet.endswith("…")
    assert set(snippet.strip("…").split()) <= {"alpha", "the", "retry", "policy", "omega"}


def preview_body(path, words, capsys):
    cli.print_preview(path, words)
    body = capsys.readouterr().out.split("\n\n", 1)[1]  # what comes after the title, folder and id
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in body.rstrip("\n").split("\n")]


@pytest.mark.parametrize("words", [[], ["tooling"]])
def test_preview_wraps_messages_at_spaces_under_their_text(words, projects, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCS_OFFLINE", "1")
    monkeypatch.setenv("FZF_PREVIEW_COLUMNS", "50")
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    prompt = "The CDN team develops deploy verification and its other AI tooling in the CDN AI hub"
    lines = preview_body(transcript(projects, "a", [you(prompt)]), words, capsys)
    col = lines[0].index("The CDN")
    assert len(lines) > 2 and all(cli.cells(line) <= 50 for line in lines)
    assert all(not line[:col].strip() and line[col] != " " for line in lines[1:])  # each line goes on under the text
    assert " ".join(line[col:] for line in lines) == prompt                         # no word is cut


def test_preview_cuts_a_long_prompt_after_a_whole_word(projects, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCS_OFFLINE", "1")
    monkeypatch.setenv("FZF_PREVIEW_COLUMNS", "80")
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    lines = preview_body(transcript(projects, "a", [you("abcdefg " * 60)]), [], capsys)
    col = lines[0].index("abcdefg")
    shown = " ".join(line[col:] for line in lines)
    assert shown.endswith("abcdefg…") and set(shown.rstrip("…").split()) == {"abcdefg"}
