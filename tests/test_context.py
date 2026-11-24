import json
import re
from pathlib import Path

from ccs.cli import row, usage_lines
from ccs.pricing import context_window
from ccs.sessions import Session, Usage, parse_session

SID = "11111111-2222-3333-4444-555555555555"
PRICES = {
    "claude-opus-5-5": {"input": 4e-6, "output": 2e-5, "cache_write": 5e-6, "cache_write_1h": 8e-6,
                        "cache_read": 2e-7, "context": 1_000_000},
    "claude-sonnet-4-5": {"input": 3e-6, "output": 1.5e-5, "cache_write": 3.75e-6, "cache_write_1h": 6e-6,
                          "cache_read": 3e-7, "context": 200_000},
}
ANSI = re.compile(r"\033\[[0-9;]*m")


def response(msg_id, input_tokens, written, read, model="claude-opus-5-5", **extra):
    return {"type": "assistant", "sessionId": SID, "timestamp": "2026-11-20T10:00:05Z", "requestId": msg_id,
            "message": {"id": msg_id, "model": model, "stop_reason": "end_turn", "content": [],
                        "usage": {"input_tokens": input_tokens, "cache_creation_input_tokens": written,
                                  "cache_read_input_tokens": read, "output_tokens": 50}}, **extra}


def test_context_is_what_the_last_main_response_had_in_its_window(tmp_path):
    path = tmp_path / "-code-api" / f"{SID}.jsonl"
    path.parent.mkdir()
    records = [
        {"type": "user", "sessionId": SID, "timestamp": "2026-11-20T10:00:00Z", "message": {"content": "go"}},
        response("m1", 10, 20_000, 0),
        response("m2", 5, 1_000, 300_000),
        response("m3", 1, 0, 900_000, model="claude-sonnet-4-5", isSidechain=True),  # a subagent's window
        response("m4", 0, 0, 0, model="<synthetic>"),  # an error message, no real response
    ]
    path.write_text("\n".join(json.dumps(r) for r in records))
    s = parse_session(path)
    assert (s.context, s.model) == (301_005, "claude-opus-5-5")


def test_context_window_comes_from_the_table_and_grows_when_a_session_proves_it():
    assert context_window("claude-opus-5-5", PRICES, 10) == 1_000_000
    assert context_window("claude-sonnet-4-5-20250929", PRICES, 10) == 200_000
    assert context_window("claude-unknown-9", {}, 150_000) == 200_000      # no table: the classic window
    assert context_window("claude-unknown-9", {}, 450_000) == 1_000_000    # past 200K, so it had 1M


def session(context, model="claude-opus-5-5"):
    return Session(id="a", file="f", cwd="/code/api", title="t", first_ts=1e9, last_ts=1e9,
                   context=context, model=model)


def test_ctx_column_is_coloured_by_how_full_the_window_is():
    cells = {pct: row(session(pct * 10_000), PRICES, {}, color=True).split("\t")[4] for pct in (12, 50, 79, 80, 96)}
    assert ANSI.sub("", cells[12]) == " 12%" and "\033[32m" in cells[12]   # green
    assert "\033[33m" in cells[50] and "\033[33m" in cells[79]            # yellow
    assert "\033[31m" in cells[80] and "\033[31m" in cells[96]            # red
    plain = row(session(120_000), PRICES, {}).split("\t")[4]
    assert plain == " 12%"                                                 # --list stays plain


def test_preview_shows_context_and_cache_bars():
    s = session(412_000)
    s.usage = Usage()
    s.usage.add("claude-opus-5-5", {"input_tokens": 1_000, "cache_creation_input_tokens": 3_000,
                                    "cache_read_input_tokens": 96_000})
    context, cache = (ANSI.sub("", line) for line in usage_lines(s, PRICES))
    assert context.startswith("🧠 ████████▏") and context.endswith("41%  412K of 1M context · claude-opus-5-5")
    assert cache.startswith("⚡ ███████████████████▏") and cache.endswith("96%  of input read from the prompt cache")
    assert usage_lines(session(0), PRICES) == []  # nothing known yet: no lines
