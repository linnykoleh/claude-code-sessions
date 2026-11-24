import json
import re
import time

from ccs import cli
from ccs.sessions import Session, Usage
from ccs.stats import _month_labels, by_day, by_model, by_project, heatmap, render

PRICES = {  # 50k output tokens = $1 on opus, $0.5 on sonnet; input is free here so sums stay round
    "claude-opus-5-5": {"input": 0, "output": 2e-5, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0},
    "claude-sonnet-5-5": {"input": 0, "output": 1e-5, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0},
}
ANSI = re.compile(r"\033\[[0-9;]*m")


def ts(day: str) -> float:
    return time.mktime(time.strptime(day + " 12", "%Y-%m-%d %H"))


def session(cwd, usage, work):
    """usage: {day: {model: output tokens}}, work: {day: seconds}"""
    days = sorted(set(usage) | set(work))
    s = Session(id=cwd + days[0], file="f", cwd=cwd, first_ts=ts(days[0]), last_ts=ts(days[-1]))
    for day, models in usage.items():
        for model, out in models.items():
            for u in (s.daily_usage.setdefault(day, Usage()), s.usage):
                u.add(model, {"output_tokens": out, "input_tokens": 1_000, "cache_read_input_tokens": 9_000})
    s.daily_work = dict(work)
    return s


SESSIONS = [
    session("/code/api", {"2026-11-24": {"claude-opus-5-5": 100_000}}, {"2026-11-24": 3600}),
    session("/code/web", {"2026-11-23": {"claude-opus-5-5": 50_000}, "2026-11-24": {"claude-sonnet-5-5": 100_000}},
            {"2026-11-23": 1800, "2026-11-24": 600}),
    session("/code/api", {"2026-11-10": {"claude-opus-5-5": 50_000}}, {"2026-11-10": 7200}),
]


def test_by_day_counts_what_was_spent_that_day():
    days = {d.day: (d.sessions, round(d.cost, 2), d.seconds) for d in by_day(SESSIONS, PRICES)}
    assert days == {"2026-11-24": (2, 3.0, 4200), "2026-11-23": (1, 1.0, 1800), "2026-11-10": (1, 1.0, 7200)}


def test_by_model_has_cost_share_and_cache_hits():
    models = by_model(SESSIONS, PRICES)
    assert [(m.model, round(m.cost, 2), m.sessions) for m in models] == [
        ("claude-opus-5-5", 4.0, 3), ("claude-sonnet-5-5", 1.0, 1)]
    assert models[0].output == 200_000 and round(models[0].cache_hit, 2) == 0.9


def test_by_project_sorts_by_cost():
    assert [(p.project, p.sessions, round(p.cost, 2), p.seconds) for p in by_project(SESSIONS, PRICES)] == [
        ("api", 2, 3.0, 10800), ("web", 1, 2.0, 2400)]


def test_heatmap_has_weeks_as_columns_monday_on_top_and_four_levels():
    seconds = {"2026-11-10": 7200, "2026-11-23": 1800, "2026-11-24": 4200, "2026-11-20": 60}
    weeks, rows = heatmap(seconds, today="2026-11-25", weeks=26)  # a Wednesday
    assert weeks[0] == "2026-11-09"            # starts at the Monday of the oldest active week, not 26 weeks back
    assert weeks[-1] == "2026-11-23"
    assert len(rows) == 7 and all(len(r) == 3 for r in rows)
    mon, tue, wed, thu, fri, sat, sun = rows
    assert tue[0] == 4                         # the busiest day
    assert mon[2] == 2 and tue[2] == 3 and fri[1] == 1
    assert wed[1] == 0                         # no work that day
    assert thu[2] is None                      # in the future


def test_render_and_json(monkeypatch, tmp_path, capsys):
    text = ANSI.sub("", "\n".join(render(SESSIONS, PRICES, today="2026-11-25", weeks=26, color=False)))
    assert "Activity" in text and "Mon" in text and "less" in text and "more" in text
    assert "claude-opus-5-5" in text and "80%" in text  # its share of the cost
    assert "Tue 2026-11-24" in text

    monkeypatch.setattr(cli, "load_sessions", lambda *a: SESSIONS)
    monkeypatch.setattr(cli, "load_prices", lambda *a: PRICES)
    assert cli.main(["stats", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"totals", "days", "models", "projects"}
    assert data["totals"]["sessions"] == 3 and round(data["totals"]["cost"], 2) == 5.0
    assert data["days"][0]["day"] == "2026-11-10" and data["models"][0]["model"] == "claude-opus-5-5"


def test_month_names_sit_over_the_first_week_of_the_month():
    assert _month_labels(["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]) == "Oct Nov"
    assert _month_labels(["2026-10-26", "2026-11-02"]) == "Oct"  # no room for Nov two characters later


def test_ctrl_t_shows_stats_in_colour_even_through_a_pager(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_sessions", lambda *a: SESSIONS)
    monkeypatch.setattr(cli, "load_prices", lambda *a: PRICES)
    assert cli.main(["stats", "--color"]) == 0  # stdout is not a terminal here, as with `| less -R`
    assert "\033[" in capsys.readouterr().out
