import io
import json
import re

from ccs import cli
from ccs.limits import read_limits, save_limits

NOW = 1_764_072_000.0
STATUS_LINE = {
    "model": {"display_name": "Opus 5.5"},
    "rate_limits": {
        "five_hour": {"used_percentage": 19, "resets_at": NOW + 2 * 3600 + 53 * 60},
        "seven_day": {"used_percentage": 32.4, "resets_at": NOW + 3 * 86400 + 5 * 3600},
    },
}


def plain(line):
    return re.sub(r"\033\[[0-9;]*m", "", line)


def test_save_keeps_the_limits_and_when_they_were_seen(tmp_path):
    path = tmp_path / "cache" / "limits.json"
    assert save_limits(json.dumps(STATUS_LINE), path, NOW)
    assert read_limits(path) == {"saved": NOW, "rate_limits": STATUS_LINE["rate_limits"]}
    assert [p.name for p in path.parent.iterdir()] == ["limits.json"]  # no temporary file left behind


def test_a_status_line_without_limits_keeps_the_last_snapshot(tmp_path):
    path = tmp_path / "limits.json"
    save_limits(json.dumps(STATUS_LINE), path, NOW)
    for raw in ('{"model": {}}', "not json", "[]", ""):  # an API key has no plan limits; broken input
        assert not save_limits(raw, path, NOW + 60)
    assert read_limits(path)["saved"] == NOW


def test_no_snapshot_or_a_broken_one_shows_nothing(tmp_path):
    assert read_limits(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_text("{")
    assert read_limits(tmp_path / "broken.json") is None
    assert cli.limits_line(None, NOW) == ""
    assert cli.limits_line({"saved": NOW, "rate_limits": {}}, NOW) == ""


def line(now, limits=STATUS_LINE["rate_limits"]):
    return plain(cli.limits_line({"saved": NOW, "rate_limits": limits}, now))


def test_one_line_shows_use_and_time_to_reset():
    assert line(NOW) == ("5h ██▍░░░░░░░░░░ 19% · resets 2h 53m   "
                         "Week ████▏░░░░░░░░ 32% · resets 3d 5h")
    assert len(line(NOW)) <= 77  # fits the footer of a 160-column terminal: its half, less the indent and scrollbar


def test_an_old_snapshot_says_how_old_in_the_title_and_counts_down_from_now():
    assert "resets 2h 50m" in line(NOW + 3 * 60) and "ago" not in line(NOW + 3 * 60)  # no room left in the line
    snapshot = {"saved": NOW, "rate_limits": STATUS_LINE["rate_limits"]}
    assert cli.limits_label(snapshot, NOW + 30) == " Usage limits "  # fresh enough to say nothing
    assert cli.limits_label(snapshot, NOW + 3 * 60) == " Usage limits · as of 3m ago "


def test_a_window_that_has_reset_since_the_snapshot_shows_no_old_use():
    after = line(NOW + 3 * 3600)
    assert after.startswith("5h reset") and "19%" not in after and "32%" in after


def test_color_follows_how_much_is_used():
    def colored(used):
        return cli.limits_line({"saved": NOW, "rate_limits": {"five_hour": {"used_percentage": used, "resets_at": NOW + 60}}}, NOW)
    assert cli.GREEN in colored(49) and cli.YELLOW in colored(50) and cli.RED in colored(80)


def test_only_the_windows_claude_code_gave_are_shown():
    limits = {"seven_day": {"used_percentage": 10, "resets_at": None}}  # weekly only, no reset time
    assert line(NOW, limits) == "Week █▎░░░░░░░░░░░ 10%"


def test_save_limits_option_reads_the_status_line_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(STATUS_LINE)))
    assert cli.main(["--save-limits"]) == 0
    assert read_limits(tmp_path / cli.LIMITS_NAME)["rate_limits"] == STATUS_LINE["rate_limits"]


def test_each_reload_leaves_the_limits_for_the_footer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cli, "LIVE_DIR", tmp_path / "live")
    state = tmp_path / "state"
    state.mkdir()

    monkeypatch.setenv("FZF_COLUMNS", "160")  # fzf gives a reload its sizes
    monkeypatch.setenv("FZF_PREVIEW_COLUMNS", "76")

    def reload():
        assert cli.main(["--rows", "", "--state", str(state), "--tick"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert len(out) == cli.FZF_HEADER_LINES  # the limits are not among the header lines
        return plain((state / "limits").read_text()).split("\n")

    assert reload() == [""]  # no snapshot yet: an empty footer, which fzf does not show
    save_limits(json.dumps(STATUS_LINE), tmp_path / cli.LIMITS_NAME, cli.time.time() - 600)
    footer = reload()  # the status line saved them since the list opened, ten minutes ago
    assert footer[1].startswith("─ Usage limits · as of 10m ago ─") and footer[3].startswith("5h ")
    assert len(footer[1]) == 160 - 76 - cli.FOOTER_FRAME  # the rule runs to the scrollbar's column


def fzf_cmd(monkeypatch, tmp_path, version):
    seen = {}

    def run(cmd, input=None, **kwargs):
        if cmd[1:] == ["--version"]:
            return cli.subprocess.CompletedProcess(cmd, 0, stdout=version)
        seen["cmd"] = cmd
        return cli.subprocess.CompletedProcess(cmd, 130, stdout="")

    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("FZF", "fzf")
    monkeypatch.setattr(cli.subprocess, "run", run)
    save_limits(json.dumps(STATUS_LINE), tmp_path / cli.LIMITS_NAME, cli.time.time())
    assert cli.pick([], {}, {}) == 0
    return seen["cmd"]


def test_the_limits_are_a_section_pinned_under_the_list(tmp_path, monkeypatch):
    cmd = fzf_cmd(monkeypatch, tmp_path, "0.75.0\n")
    footer = plain(next(a for a in cmd if a.startswith("--footer=")))[len("--footer="):].split("\n")
    # a blank line apart from the rows, the title on a rule, the limits with a blank line above and below
    assert footer[0] == "" and footer[1].startswith("─ Usage limits ──") and footer[2] == ""
    assert footer[3].startswith("5h ") and footer[4] == " " and len(footer) == 5
    assert "--footer-border=none" in cmd  # fzf's own border would touch the rows
    assert f"--scroll-off={cli.SCROLL_OFF}" in cmd and cli.SCROLL_OFF >= 3  # the cursor stays clear of them
    load = next(a for a in cmd if a.startswith("--bind=load:"))
    assert "+transform-footer(cat " in load and "limits" in load.split("transform-footer(cat ")[1]


def test_a_static_list_still_gets_the_footer_and_an_old_fzf_none(tmp_path, monkeypatch):
    assert any(a.startswith("--footer=") for a in fzf_cmd(monkeypatch, tmp_path, "0.63.0\n"))
    assert not any(a.startswith("--footer") for a in fzf_cmd(monkeypatch, tmp_path, "0.62.0\n"))


def test_the_first_frame_works_out_the_footer_width_from_the_terminal(monkeypatch):
    """Before fzf runs there are no FZF_ sizes; the preview takes half of the terminal, less 4 (measured in fzf 0.75)."""
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: cli.os.terminal_size((160, 40)))
    assert cli.footer_width({}) == cli.footer_width({"FZF_COLUMNS": "160", "FZF_PREVIEW_COLUMNS": "76"}) == 77
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: cli.os.terminal_size((101, 40)))
    assert cli.footer_width({}) == cli.footer_width({"FZF_COLUMNS": "101", "FZF_PREVIEW_COLUMNS": "46"})
