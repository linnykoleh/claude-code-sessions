"""Read Claude Code session transcripts.

Claude Code writes each session to ~/.claude/projects/<project>/<session-id>.jsonl, one JSON
record per line. Subagents of a session write to <session-id>/subagents/**/agent-*.jsonl.
A running Claude Code registers its session in ~/.claude/sessions/<pid>.json.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from ccs.pricing import Prices, price_for

_KINDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")

# Prompts that start with a tag are commands, hook output or pasted system text, not something you typed.
_NOT_A_PROMPT = re.compile(r"^\s*<")

# Stop reasons of a response that ends Claude's turn.
_TURN_ENDED = ("end_turn", "stop_sequence")


@dataclass
class Usage:
    """Token counts per model."""

    tokens: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, usage: dict) -> None:
        t = self.tokens.setdefault(model, dict.fromkeys(_KINDS, 0))
        written = usage.get("cache_creation_input_tokens") or 0
        written_1h = min((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, written)
        t["input"] += usage.get("input_tokens") or 0
        t["output"] += usage.get("output_tokens") or 0
        t["cache_write"] += written - written_1h
        t["cache_write_1h"] += written_1h
        t["cache_read"] += usage.get("cache_read_input_tokens") or 0

    def cost(self, prices: Prices) -> Optional[float]:
        """API-equivalent cost in USD; None when a used model has no known price."""
        total = 0.0
        for model, t in self.tokens.items():
            if not any(t.values()):
                continue
            p = price_for(model, prices)
            if p is None:
                return None
            total += sum(t.get(k, 0) * p.get(k, p.get("cache_write", 0.0)) for k in _KINDS)
        return total


@dataclass
class Session:
    id: str
    file: str
    cwd: str = ""
    branch: str = ""
    title: str = ""
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    interrupted: bool = False  # closed before Claude finished its turn
    context: int = 0  # tokens in the context window at the last response (subagents have their own)
    model: str = ""  # the model of that response
    usage: Usage = field(default_factory=Usage)
    # by local day "YYYY-MM-DD", so a session that spans days counts toward each of them
    daily_usage: Dict[str, Usage] = field(default_factory=dict)
    daily_work: Dict[str, float] = field(default_factory=dict)  # seconds Claude spent on its turns

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else "?"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Session":
        data = dict(data)
        data["usage"] = Usage(**data.get("usage", {}))
        data["daily_usage"] = {day: Usage(**u) for day, u in data.get("daily_usage", {}).items()}
        return cls(**data)


def find_session_files(projects_dir: Path) -> List[Path]:
    """Top-level session transcripts; subagent transcripts are counted inside their session."""
    return sorted(projects_dir.glob("*/*.jsonl"))


def parse_session(path: Path, with_usage: bool = True) -> Session:
    """Read one session: where it ran, its title, when, and (unless with_usage=False) its token
    usage, subagents included."""
    s = Session(id=path.stem, file=str(path))
    custom_title = ai_title = first_prompt = ""
    seen: set = set()

    for rec in _records(path):
        kind = rec.get("type")
        if not s.cwd and rec.get("cwd"):
            s.cwd = rec["cwd"]
        if not s.branch and rec.get("gitBranch"):
            s.branch = rec["gitBranch"]
        if kind == "custom-title" and rec.get("customTitle"):
            custom_title = rec["customTitle"]
        elif kind == "ai-title" and rec.get("aiTitle"):
            ai_title = rec["aiTitle"]
        elif kind == "system" and rec.get("subtype") == "turn_duration" and not rec.get("isSidechain"):
            day = local_day(parse_ts(rec.get("timestamp")))
            if day:
                s.daily_work[day] = s.daily_work.get(day, 0.0) + (rec.get("durationMs") or 0) / 1000
        elif kind in ("user", "assistant"):
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None:
                s.first_ts = ts if s.first_ts is None else min(s.first_ts, ts)
                s.last_ts = ts if s.last_ts is None else max(s.last_ts, ts)
            if not rec.get("isSidechain") and not rec.get("isMeta"):
                left_open = _leaves_turn_open(rec)
                if left_open is not None:
                    s.interrupted = left_open
            if kind == "user" and not first_prompt:
                first_prompt = prompt_text(rec) or ""
            elif kind == "assistant":
                _note_context(rec, s)
                if with_usage and _belongs(rec, s.id):
                    _add_usage(rec, s, seen)

    sub_dir = path.with_suffix("") / "subagents"
    if with_usage and sub_dir.is_dir():
        for agent_file in sub_dir.rglob("*.jsonl"):
            for rec in _records(agent_file):
                if rec.get("type") == "assistant" and _belongs(rec, s.id):
                    _add_usage(rec, s, seen)

    s.title = " ".join((custom_title or ai_title or first_prompt).split())
    return s


def prompts(path: Path) -> Iterator[Tuple[Optional[float], str]]:
    """(timestamp, text) of every prompt you typed in a session."""
    for ts, who, text in messages(path):
        if who == "you":
            yield ts, text


def messages(path: Path) -> Iterator[Tuple[Optional[float], str, str]]:
    """(timestamp, "you" or "claude", text) of what you typed and what Claude wrote back:
    no thinking, tool calls, tool output or subagents."""
    for rec in _records(path):
        kind = rec.get("type")
        if rec.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        if kind == "user":
            text = prompt_text(rec)
        else:
            content = (rec.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            text = "\n".join(b["text"] for b in blocks
                             if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
        if text and text.strip():
            yield parse_ts(rec.get("timestamp")), "you" if kind == "user" else "claude", text


def prompt_text(rec: dict) -> Optional[str]:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str) and content.strip() and not _NOT_A_PROMPT.match(content):
        return content
    return None


def rename_session(path: Path, session_id: str, name: str) -> None:
    """Name the session the way `/rename` does: a custom-title and an agent-name record at the end of the transcript
    and `<id>/custom-title.json` next to it. The transcript keeps its mtime, so Claude's own picker keeps its order."""
    st = path.stat()
    records = [{"type": "custom-title", "customTitle": name, "sessionId": session_id},
               {"type": "agent-name", "agentName": name, "sessionId": session_id}]
    text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records)
    with path.open("rb") as f:
        if st.st_size:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                text = "\n" + text  # finish the last line first, or the two would be glued into one
    with path.open("a", encoding="utf-8") as f:
        f.write(text)  # one write, so a line is never split
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    side_dir = path.with_suffix("")
    side_dir.mkdir(exist_ok=True)
    fd = os.open(side_dir / "custom-title.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"customTitle": name}, ensure_ascii=False, separators=(",", ":")))


@dataclass(frozen=True)
class Live:
    """A running Claude Code that has a session open."""
    pid: int
    busy: bool  # Claude is working on a turn; else it waits for you
    job_id: Optional[str] = None  # only for a background session (`claude --bg`): what `claude attach` takes


def live_status(sessions_dir: Path) -> Dict[str, Live]:
    """{session id: Live} of the sessions open in a running Claude Code."""
    live: Dict[str, Live] = {}
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            pid, session_id = int(data["pid"]), str(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _is_running(pid):  # a crashed Claude Code leaves its file behind
            job_id = data.get("jobId") if data.get("kind") == "bg" else None
            live[session_id] = Live(pid=pid, busy=data.get("status") == "busy", job_id=job_id or None)
    return live


def local_day(ts: Optional[float]) -> str:
    """"YYYY-MM-DD" in local time, or "" without a timestamp."""
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _leaves_turn_open(rec: dict) -> Optional[bool]:
    """Whether a session that ends on this record was left before Claude finished its turn;
    None for records that do not change that (tool results, commands)."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if rec.get("type") == "assistant":
        if any(b.get("type") == "tool_use" and b.get("name") == "StructuredOutput" for b in blocks):
            return False  # `claude -p --json-schema` ends on its structured output
        return msg.get("stop_reason") not in _TURN_ENDED
    texts = [content] if isinstance(content, str) else [b.get("text") for b in blocks if b.get("type") == "text"]
    # a prompt waits for an answer; "[Request interrupted by user]" marks an Esc
    return True if any(isinstance(t, str) and t.strip() and not _NOT_A_PROMPT.match(t) for t in texts) else None


def _is_running(pid: int) -> bool:
    if pid <= 0:  # 0 and negative pids address process groups
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def _belongs(rec: dict, session_id: str) -> bool:
    # A resumed or forked session can carry records copied from the session it came from;
    # those are counted in their own session, not here.
    return rec.get("sessionId") in (None, session_id)


def _note_context(rec: dict, s: Session) -> None:
    msg = rec.get("message") or {}
    usage, model = msg.get("usage"), msg.get("model")
    if rec.get("isSidechain") or not usage or not model or model == "<synthetic>":
        return
    s.context = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    s.model = model


def _add_usage(rec: dict, s: Session, seen: set) -> None:
    msg = rec.get("message") or {}
    if not msg.get("usage") or not msg.get("model"):
        return
    # one API response is often logged several times (streaming, retries): count it once
    key = (msg.get("id"), rec.get("requestId"))
    if key in seen:
        return
    seen.add(key)
    s.usage.add(msg["model"], msg["usage"])
    day = local_day(parse_ts(rec.get("timestamp")))
    if day:
        s.daily_usage.setdefault(day, Usage()).add(msg["model"], msg["usage"])


def _records(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return
