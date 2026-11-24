"""Session index with an on-disk cache: only sessions whose transcript changed are read again."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from ccs.sessions import Session, find_session_files, parse_session

CACHE_VERSION = 5


def load_sessions(projects_dir: Path, cache_dir: Path) -> List[Session]:
    """All sessions, newest first."""
    cache_file = cache_dir / "index.json"
    cached = _read_cache(cache_file)
    entries: Dict[str, dict] = {}
    sessions: List[Session] = []

    for path in find_session_files(projects_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        key = [st.st_mtime_ns, st.st_size]
        hit = cached.get(str(path))
        if hit and hit.get("key") == key:
            session = Session.from_json(hit["session"])
        else:
            session = parse_session(path)
        entries[str(path)] = {"key": key, "session": session.to_json()}
        sessions.append(session)

    _write_cache(cache_file, entries)
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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "sessions": entries}))
    tmp.replace(path)
