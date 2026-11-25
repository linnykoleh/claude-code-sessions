"""Session index with an on-disk cache: only sessions whose transcript changed are read again."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List

from ccs.sessions import Session, find_session_files, parse_session

CACHE_VERSION = 7


def load_sessions(projects_dir: Path, cache_dir: Path, max_age: float = 0.0) -> List[Session]:
    """All sessions, newest first. A transcript that changed is read again; with max_age, only once its last
    reading is max_age seconds old, so a list redrawn several times a second does not read a busy session each time."""
    cache_file = cache_dir / f"index-v{CACHE_VERSION}.json"  # per version: two installed versions keep their own
    cached = _read_cache(cache_file)
    entries: Dict[str, dict] = {}
    now = time.time()

    for path in find_session_files(projects_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        key = [st.st_mtime_ns, st.st_size]
        entry = cached.get(str(path))
        if not entry or (entry.get("key") != key and now - entry.get("read", 0) >= max_age):
            entry = {"key": key, "read": now, "session": parse_session(path).to_json()}
        entries[str(path)] = entry

    if entries != cached:
        _write_cache(cache_file, entries)
    sessions = [Session.from_json(entry["session"]) for entry in entries.values()]
    sessions.sort(key=lambda s: s.last_ts or 0, reverse=True)
    return sessions


def _read_cache(path: Path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("sessions", {})


def _write_cache(path: Path, entries: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_leftovers(path)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # fzf may run two ccs at once while you type
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "sessions": entries}))
    tmp.replace(path)


def remove_leftovers(path: Path, older_than: float = 60.0) -> None:
    """Remove the temporary files of `path` that a writer left behind: fzf kills a reload it no longer needs,
    sometimes between writing the file and renaming it. A younger one may belong to a writer still running."""
    for tmp in path.parent.glob(f"{path.stem}.*.tmp"):
        try:
            if time.time() - tmp.stat().st_mtime > older_than:
                tmp.unlink()
        except OSError:
            pass  # another ccs removed it first
