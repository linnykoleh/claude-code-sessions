"""Read Claude Code session transcripts.

Claude Code writes each session to ~/.claude/projects/<project>/<session-id>.jsonl, one JSON
record per line. Subagents of a session write to <session-id>/subagents/**/agent-*.jsonl.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from ccs.pricing import Prices, price_for

_KINDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")

# Prompts that start with a tag are commands, hook output or pasted system text, not something you typed.
_NOT_A_PROMPT = re.compile(r"^\s*<")


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
    usage: Usage = field(default_factory=Usage)

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else "?"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Session":
        data = dict(data)
        data["usage"] = Usage(**data.get("usage", {}))
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
        elif kind in ("user", "assistant"):
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None:
                s.first_ts = ts if s.first_ts is None else min(s.first_ts, ts)
                s.last_ts = ts if s.last_ts is None else max(s.last_ts, ts)
            if kind == "user" and not first_prompt:
                first_prompt = prompt_text(rec) or ""
            elif kind == "assistant" and with_usage and _belongs(rec, s.id):
                _add_usage(rec, s.usage, seen)

    sub_dir = path.with_suffix("") / "subagents"
    if with_usage and sub_dir.is_dir():
        for agent_file in sub_dir.rglob("*.jsonl"):
            for rec in _records(agent_file):
                if rec.get("type") == "assistant" and _belongs(rec, s.id):
                    _add_usage(rec, s.usage, seen)

    s.title = " ".join((custom_title or ai_title or first_prompt).split())
    return s


def prompts(path: Path) -> Iterator[Tuple[Optional[float], str]]:
    """(timestamp, text) of every prompt you typed in a session."""
    for rec in _records(path):
        if rec.get("type") == "user":
            text = prompt_text(rec)
            if text:
                yield parse_ts(rec.get("timestamp")), text


def prompt_text(rec: dict) -> Optional[str]:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str) and content.strip() and not _NOT_A_PROMPT.match(content):
        return content
    return None


def parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _belongs(rec: dict, session_id: str) -> bool:
    # A resumed or forked session can carry records copied from the session it came from;
    # those are counted in their own session, not here.
    return rec.get("sessionId") in (None, session_id)


def _add_usage(rec: dict, usage: Usage, seen: set) -> None:
    msg = rec.get("message") or {}
    if not msg.get("usage") or not msg.get("model"):
        return
    # one API response is often logged several times (streaming, retries): count it once
    key = (msg.get("id"), rec.get("requestId"))
    if key in seen:
        return
    seen.add(key)
    usage.add(msg["model"], msg["usage"])


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
