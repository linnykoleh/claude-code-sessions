"""Search sessions: their title, project, branch and day, and what was said in them, your prompts and Claude's replies.

The text of every session is kept in a cache next to the index and read again only for transcripts that changed.
Matching is plain case-insensitive substrings, so a word stem finds its forms (`кеш` finds `кешування`)."""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ccs.index import remove_leftovers
from ccs.sessions import Session, local_day, messages

TEXT_CACHE_VERSION = 1
SNIPPET_CONTEXT = 70  # characters shown on each side of a hit


def search(sessions: List[Session], query: str, cache_dir: Path, max_age: float = 0.0) -> List[Session]:
    """The sessions that have every word of the query somewhere: in the title, project, branch, the day of the last
    message ("2026-11-24"), your prompts or Claude's replies; in the order given. "Words in quotes" are one phrase.
    An empty query keeps every session. max_age: see load_texts."""
    words = query_words(query)
    if not words:
        return sessions
    texts = load_texts([Path(s.file) for s in sessions], cache_dir, max_age)
    found = []
    for s in sessions:
        about = "\n".join([s.title, s.project, s.branch, local_day(s.last_ts)]).lower()
        said = texts.get(s.file, "")
        if all(w in about or w in said for w in words):
            found.append(s)
    return found


def query_words(query: str) -> List[str]:
    try:
        parts = shlex.split(query)
    except ValueError:  # an unfinished quote while typing
        parts = shlex.split(query + '"') if query.count('"') % 2 else query.split()
    return [" ".join(p.lower().split()) for p in parts if p.strip()]


def snippets(path: Path, words: List[str], limit: int = 40) -> List[Tuple[Optional[float], str, str]]:
    """(timestamp, who, snippet) of the messages that contain one of the words: the text around the first hit."""
    hits = []
    for ts, who, text in messages(path):
        flat = " ".join(text.split())
        lower = flat.lower()
        at = min((i for i in (lower.find(w) for w in words) if i >= 0), default=-1)
        if at < 0:
            continue
        hit_end = at + max(len(w) for w in words if lower.startswith(w, at))
        start, end = max(0, at - SNIPPET_CONTEXT), max(hit_end, at + SNIPPET_CONTEXT * 2)
        if start and flat[start - 1] != " ":  # begin and end on whole words, unless one word is longer than that
            space = flat.find(" ", start, at)
            start = space + 1 if space >= 0 else start
        if end < len(flat) and flat[end] != " ":
            space = flat.rfind(" ", hit_end, end)
            end = space if space >= 0 else end
        hits.append((ts, who, ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")))
        if len(hits) == limit:
            break
    return hits


def highlight(text: str, words: List[str], on: str, off: str) -> str:
    if not words:
        return text
    pattern = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.IGNORECASE)
    return pattern.sub(lambda m: f"{on}{m.group(0)}{off}", text)


def load_texts(paths: List[Path], cache_dir: Path, max_age: float = 0.0) -> Dict[str, str]:
    """{transcript: its prompts and replies, lowercased, whitespace collapsed}. A transcript that changed is read
    again; with max_age, only once its last reading is max_age seconds old, as in index.load_sessions."""
    cache_file = cache_dir / f"text-v{TEXT_CACHE_VERSION}.json"
    cached = _read_cache(cache_file)
    entries: Dict[str, dict] = {}
    now = time.time()
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        key = [st.st_mtime_ns, st.st_size]
        hit = cached.get(str(path))
        if not hit or (hit.get("key") != key and now - hit.get("read", 0) >= max_age):
            text = "\n".join(" ".join(t.split()) for _, _, t in messages(path)).lower()
            hit = {"key": key, "read": now, "text": text}
        entries[str(path)] = hit
    if entries != cached:
        _write_cache(cache_file, entries)
    return {path: entry["text"] for path, entry in entries.items()}


def _read_cache(path: Path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("sessions", {}) if data.get("version") == TEXT_CACHE_VERSION else {}


def _write_cache(path: Path, entries: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_leftovers(path)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # fzf may run two searches at once
    tmp.write_text(json.dumps({"version": TEXT_CACHE_VERSION, "sessions": entries}, ensure_ascii=False))
    tmp.replace(path)
