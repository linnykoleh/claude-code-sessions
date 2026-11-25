"""The list while it is open: fzf reloads it on a timer, so statuses stay true and the working mark turns."""

import json
import re

import pytest

from ccs import cli
from ccs.sessions import parse_ts


def transcript(projects, sid, ts, prompt="hello"):
    path = projects / "-code-api" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "sessionId": sid, "timestamp": ts, "cwd": "/code/api",
                                "message": {"content": prompt}}) + "\n")
    return path


@pytest.fixture
def ccs_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cli, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("CCS_OFFLINE", "1")
    return tmp_path


def listed_ids(capsys):
    lines = capsys.readouterr().out.splitlines()[cli.FZF_HEADER_LINES:]
    return [line.split("\t")[cli.ID_FIELD - 1] for line in lines]


def state_dir(path, order, shown):
    path.mkdir()
    (path / "order").write_text("\n".join(order))
    (path / "shown").write_text("\n".join(shown))
    return path


def test_typing_lists_sessions_in_the_opening_order_new_ones_first_and_remembers_them(ccs_dirs, capsys):
    projects = ccs_dirs / "projects"
    for sid, ts in [("a", "2026-11-20T12:00:00Z"), ("b", "2026-11-20T11:00:00Z"), ("c", "2026-11-20T10:00:00Z"),
                    ("d", "2026-11-20T09:00:00Z")]:
        transcript(projects, sid, ts)
    # the list opened before a and b had their latest messages; d is new since then
    state = state_dir(ccs_dirs / "state", order=["c", "b", "a"], shown=["c", "b", "a"])
    assert cli.main(["--rows", "", "--state", str(state)]) == 0
    assert listed_ids(capsys) == ["d", "c", "b", "a"]
    assert (state / "shown").read_text().split() == ["d", "c", "b", "a"]  # what the timer reloads show next
    assert cli.main(["--rows", ""]) == 0
    assert listed_ids(capsys) == ["a", "b", "c", "d"]  # without a state: newest first


def test_a_timer_reload_shows_the_same_sessions_in_the_same_rows(ccs_dirs, capsys):
    """fzf keeps the cursor on a row number across reloads: a row added or removed above it would put another
    session under the cursor, and Enter would open that one. A new session, or one that has come to match the
    query, shows up with the next key you press."""
    projects = ccs_dirs / "projects"
    for sid, ts, said in [("a", "2026-11-20T12:00:00Z", "kafka lag"), ("b", "2026-11-20T11:00:00Z", "kafka"),
                          ("c", "2026-11-20T10:00:00Z", "kafka offsets")]:
        transcript(projects, sid, ts, said)
    state = state_dir(ccs_dirs / "state", order=["a", "b", "c"], shown=["b", "c"])  # as if "kafka" listed b and c
    transcript(projects, "d", "2026-11-20T13:00:00Z", "kafka again")  # a new session that matches too
    assert cli.main(["--rows", "kafka", "--state", str(state), "--tick"]) == 0
    assert listed_ids(capsys) == ["b", "c"]
    assert (state / "shown").read_text().split() == ["b", "c"]


def test_the_list_keeps_its_state_for_the_reloads_and_removes_it_after(ccs_dirs, monkeypatch):
    sessions = [cli.Session(id=sid, file=f"/p/{sid}.jsonl", cwd="/code/api", last_ts=t) for sid, t in
                [("new", 3e9), ("mid", 2e9), ("old", 1e9)]]
    seen = {}

    def fake_fzf(cmd, input=None, **kwargs):
        if cmd[1:] == ["--version"]:
            return cli.subprocess.CompletedProcess(cmd, 0, stdout="0.75.0 (c7bf2b8e)\n")
        reloads = [a for a in cmd if "--rows {q}" in a]
        dirs = {re.search(r"--state '?([^')\s]+)", a).group(1) for a in reloads}
        assert len(reloads) == 3 and len(dirs) == 1  # the timer, typing and Ctrl-R share one state
        seen["dir"] = cli.Path(dirs.pop())
        seen["order"] = (seen["dir"] / "order").read_text().split()
        seen["shown"] = (seen["dir"] / "shown").read_text().split()
        return cli.subprocess.CompletedProcess(cmd, 130, stdout="")

    monkeypatch.setenv("FZF", "fzf")
    monkeypatch.setattr(cli.subprocess, "run", fake_fzf)
    assert cli.pick(sessions, {}, {}) == 0
    assert seen["order"] == seen["shown"] == ["new", "mid", "old"]
    assert not seen["dir"].exists()


def test_closing_the_terminal_still_removes_the_lists_state(ccs_dirs, monkeypatch):
    import os
    import signal
    seen = {}

    def fake_fzf(cmd, input=None, **kwargs):
        if cmd[1:] == ["--version"]:
            return cli.subprocess.CompletedProcess(cmd, 0, stdout="0.75.0\n")
        seen["dir"] = cli.Path(re.search(r"--state '?([^')\s]+)", " ".join(cmd)).group(1))
        os.kill(os.getpid(), signal.SIGHUP)  # the terminal tab closes while the list is open
        return cli.subprocess.CompletedProcess(cmd, 129, stdout="")

    monkeypatch.setenv("FZF", "fzf")
    monkeypatch.setattr(cli.subprocess, "run", fake_fzf)
    with pytest.raises(SystemExit):
        cli.pick([cli.Session(id="a", file="/p/a.jsonl", cwd="/code/api", last_ts=1e9)], {}, {})
    assert not seen["dir"].exists()
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL


def test_reloads_never_wait_for_the_price_table_to_download(ccs_dirs, monkeypatch, capsys):
    """Without a fresh table every reload would try the download, three times a second, offline for 10 s each."""
    from ccs import pricing
    monkeypatch.delenv("CCS_OFFLINE")
    downloads = []
    monkeypatch.setattr(pricing, "_download", downloads.append)
    path = transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z")
    for args in (["--rows", ""], ["--rows", "", "--tick"], ["--preview", str(path)]):
        assert cli.main(args) == 0
    assert downloads == []
    assert cli.main(["--list"]) == 0  # the list itself still fetches a table that is out of date
    assert len(downloads) == 1


def test_a_cache_write_cut_short_leaves_no_file_behind_for_long(ccs_dirs):
    """fzf kills a reload it no longer needs, sometimes between writing a cache's temporary file and renaming it."""
    import os
    import time
    from ccs import index, search
    cache = ccs_dirs / "cache"
    cache.mkdir()
    old, young = time.time() - 120, time.time()
    leftovers = {}
    for name in (f"index-v{index.CACHE_VERSION}", f"text-v{search.TEXT_CACHE_VERSION}"):
        for pid, when in ((111, old), (222, young)):
            tmp = cache / f"{name}.{pid}.tmp"
            tmp.write_text("{}")
            os.utime(tmp, (when, when))
            leftovers[tmp] = when == young  # a young one may belong to a writer still running
    path = transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z", "kafka")
    index.load_sessions(ccs_dirs / "projects", cache)
    search.load_texts([path], cache)
    assert {tmp: tmp.exists() for tmp in leftovers} == leftovers


def append_prompt(path, text, ts="2026-11-20T13:00:00Z"):
    with open(path, "a") as f:
        f.write(json.dumps({"type": "user", "sessionId": path.stem, "timestamp": ts, "cwd": "/code/api",
                            "message": {"content": text}}) + "\n")


def test_a_growing_transcript_is_read_again_only_once_its_last_reading_is_max_age_old(ccs_dirs, monkeypatch):
    """A busy session's transcript changes all the time; reading a big one on every frame would eat a CPU."""
    from ccs import index
    path = transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z", "first")
    cache = ccs_dirs / "cache"
    now = 1_000_000.0
    monkeypatch.setattr(index.time, "time", lambda: now)
    assert [s.last_ts for s in index.load_sessions(ccs_dirs / "projects", cache)] == [parse_ts("2026-11-20T12:00:00Z")]
    append_prompt(path, "second")
    newest = lambda **kw: index.load_sessions(ccs_dirs / "projects", cache, **kw)[0].last_ts
    assert newest(max_age=5) == parse_ts("2026-11-20T12:00:00Z")      # read a moment ago: the cache will do
    now += 6
    assert newest(max_age=5) == parse_ts("2026-11-20T13:00:00Z")      # 6 s later it is read again
    append_prompt(path, "third", "2026-11-20T14:00:00Z")
    assert newest() == parse_ts("2026-11-20T14:00:00Z")               # without max_age, every change is read


def test_the_index_is_not_written_again_when_nothing_changed(ccs_dirs):
    from ccs import index
    transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z")
    index.load_sessions(ccs_dirs / "projects", ccs_dirs / "cache")
    cache_file = next((ccs_dirs / "cache").glob("index-v*.json"))
    written = cache_file.stat().st_ino  # a new write replaces the file, so it gets a new inode
    index.load_sessions(ccs_dirs / "projects", ccs_dirs / "cache")
    assert cache_file.stat().st_ino == written


def test_search_text_of_a_growing_transcript_follows_the_same_max_age(ccs_dirs, monkeypatch):
    from ccs import search
    path = transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z", "kafka lag")
    cache = ccs_dirs / "cache"
    now = 1_000_000.0
    monkeypatch.setattr(search.time, "time", lambda: now)
    assert "kafka" in search.load_texts([path], cache)[str(path)]
    append_prompt(path, "refund worker")
    assert "refund" not in search.load_texts([path], cache, max_age=5)[str(path)]
    now += 6
    assert "refund" in search.load_texts([path], cache, max_age=5)[str(path)]
    fresh = transcript(ccs_dirs / "projects", "b", "2026-11-20T12:00:00Z", "never seen before")
    assert "never seen" in search.load_texts([fresh], cache, max_age=5)[str(fresh)]  # nothing cached: read it


def test_a_timer_reload_reads_cached_rows_while_typing_reads_fresh_ones(ccs_dirs, capsys, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)  # no time passes: whatever is cached is fresh enough
    path = transcript(ccs_dirs / "projects", "a", "2026-11-20T12:00:00Z", "first")
    assert cli.main(["--rows", "first"]) == 0  # reads the transcript for the index and for search
    capsys.readouterr()
    append_prompt(path, "second", "2026-11-20T13:00:00Z")

    def rows(*args):
        assert cli.main(["--rows", *args]) == 0
        return [line.split("\t") for line in capsys.readouterr().out.splitlines()[cli.FZF_HEADER_LINES:]]

    at = lambda ts: cli.fmt_time(parse_ts(ts)).ljust(cli.TIME_WIDTH)
    assert [r[0] for r in rows("", "--tick")] == [at("2026-11-20T12:00:00Z")]
    assert rows("second", "--tick") == []
    assert [r[0] for r in rows("second")] == [at("2026-11-20T13:00:00Z")]


@pytest.fixture
def fzf(monkeypatch):
    """pick() against a fake fzf of a given version; returns {"version": ..., "cmd": [...]} once pick() ran it."""
    seen = {"version": "0.75.0 (c7bf2b8e)\n"}

    def run(cmd, input=None, **kwargs):
        if cmd[1:] == ["--version"]:
            return cli.subprocess.CompletedProcess(cmd, 0, stdout=seen["version"])
        seen["cmd"] = cmd
        return cli.subprocess.CompletedProcess(cmd, 130, stdout="")  # Esc

    monkeypatch.setenv("FZF", "fzf")
    monkeypatch.setattr(cli.subprocess, "run", run)
    return seen


def bindings(cmd, event):
    """The actions bound to an event, e.g. bindings(cmd, "change") -> ["unbind(every(0.3))", "reload(...)", ...]."""
    found = [a.split(":", 1)[1] for a in cmd if a.startswith(f"--bind={event}:")]
    assert len(found) == 1, f"{event}: {found}"
    return split_actions(found[0])


def split_actions(chain):
    actions, depth, start = [], 0, 0
    for i, ch in enumerate(chain):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "+" and depth == 0:
            actions.append(chain[start:i])
            start = i + 1
    return actions + [chain[start:]]


@pytest.mark.parametrize("version", ["0.74.5\n", "0.75.0 (c7bf2b8e)\n", "1.0.0 (brew)\n"])
def test_with_fzf_0_74_5_or_later_the_list_reloads_itself_on_a_timer(ccs_dirs, fzf, version):
    fzf["version"] = version
    cli.pick([], {}, {})
    cmd = fzf["cmd"]
    timer = f"every({cli.FRAME_SECONDS})"
    pause = f"unbind({timer})"
    on_timer = bindings(cmd, timer)
    # reload-sync: a plain reload shows the list while it is still being read, and fzf pulls the cursor up into
    # the rows read so far; in a long list the cursor would end up on another session
    assert on_timer[0] == pause and on_timer[1].startswith("reload-sync(") and on_timer[1].endswith(" --tick)")
    # fzf cuts a reload short when the next one starts, and a list whose reloads never finish never changes:
    # every reload pauses the timer, and the timer comes back once the list has loaded
    assert bindings(cmd, "load") == [f"rebind({timer})"]
    change, ctrl_r = bindings(cmd, "change"), bindings(cmd, "ctrl-r")
    assert change[0] == pause and ctrl_r[0] == pause
    assert [a for a in ctrl_r if a.startswith("reload")][0].startswith("reload-sync(")
    assert not [a for a in change + ctrl_r if "--tick" in a]  # what you typed or renamed is read fresh
    # --track would drop the keys you type while a reload runs; the cursor keeps its row instead.
    # FZF_DEFAULT_OPTS comes first: its --track or --preview must not stay on
    assert "--track" not in cmd and "--no-track" in cmd and "--no-preview" in cmd


def test_the_preview_runs_when_the_session_or_the_query_changes_not_on_every_frame(ccs_dirs, fzf):
    cli.pick([], {}, {})
    cmd = fzf["cmd"]
    preview = f"preview({cli.me_command()} --preview {{{cli.FILE_FIELD}}} --find {{q}})"
    assert not any(a.startswith("--preview=") for a in cmd)  # fzf runs --preview again after each reload
    assert f"--id-nth={cli.ID_FIELD}" in cmd  # the same session under the cursor after a reload is not a new focus
    assert bindings(cmd, "focus") == [preview]
    assert bindings(cmd, "change")[-1] == preview and bindings(cmd, "ctrl-r")[-1] == preview


def test_the_row_under_the_cursor_stands_out_across_the_whole_line(ccs_dirs, fzf):
    cli.pick([], {}, {})
    assert "--highlight-line" in fzf["cmd"]
    colors = dict(c.split(":", 1) for a in fzf["cmd"] if a.startswith("--color=") for c in a[8:].split(","))
    assert colors["bg+"] != "236"  # fzf's own dark grey hardly shows on a dark terminal
    # the whole row turns white on the band, as a selected row does on macOS: its own colours would not read on it
    assert "strip" in colors["fg+"].split(":")


@pytest.mark.parametrize("version", ["0.74.4 (brew)\n", "0.73.0\n", "0.72.1\n", "0.60.0\n", "",
                                     "fzf: not a version\n"])
def test_an_older_fzf_gets_the_list_it_understands(ccs_dirs, fzf, version):
    """An option fzf does not know makes it exit at once, and the list would not open at all. Before 0.74.5, fzf
    does not tell a new session under the cursor after a reload, so the preview would show the one before."""
    fzf["version"] = version
    cli.pick([], {}, {})
    cmd = fzf["cmd"]
    assert any(a.startswith("--preview=") for a in cmd)
    new_options = ("--id-nth", "--highlight-line", "--gutter", "--header-lines-border", "--bind=every(",
                   "--bind=load:", "--bind=focus:")
    assert not [a for a in cmd if a.startswith(new_options)]
