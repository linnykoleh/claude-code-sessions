"""`ccs stats`: how you work with Claude, from the cached index alone.

An activity heatmap of Claude's working time, the last 14 days, the models and the projects. Cost and time count by
the local day they were spent, as in the list's statistics. `--json` prints the same numbers for scripts."""

from __future__ import annotations

import bisect
import json
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from ccs.cli import fmt_money, fmt_tokens, fmt_total
from ccs.pricing import Prices, price_for
from ccs.sessions import Session, Usage

DAY = 86400
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
# GitHub's greens in the 256-colour palette: no activity, then four levels
LEVELS = ["\033[38;5;237m", "\033[38;5;22m", "\033[38;5;28m", "\033[38;5;34m", "\033[38;5;46m"]
ACCENT = "\033[38;5;75m"
CELL = "■"


@dataclass
class DayStats:
    day: str
    sessions: int = 0
    cost: float = 0.0
    seconds: float = 0.0  # Claude's working time


@dataclass
class ModelStats:
    model: str
    cost: float = 0.0
    sessions: int = 0
    input: int = 0  # all input: fresh, written to and read from the prompt cache
    output: int = 0
    cache_read: int = 0

    @property
    def cache_hit(self) -> float:
        return self.cache_read / self.input if self.input else 0.0


@dataclass
class ProjectStats:
    project: str
    sessions: int = 0
    cost: float = 0.0
    seconds: float = 0.0


# --- numbers ---

def by_day(sessions: List[Session], prices: Prices) -> List[DayStats]:
    """Every day something happened, oldest first."""
    days: Dict[str, DayStats] = {}
    for s in sessions:
        for day in set(s.daily_usage) | set(s.daily_work):
            d = days.setdefault(day, DayStats(day))
            d.sessions += 1
            d.cost += _cost(s.daily_usage.get(day), prices)
            d.seconds += s.daily_work.get(day, 0.0)
    return [days[k] for k in sorted(days)]


def by_model(sessions: List[Session], prices: Prices) -> List[ModelStats]:
    """Models by cost, most expensive first."""
    models: Dict[str, ModelStats] = {}
    for s in sessions:
        for model, t in s.usage.tokens.items():
            if not any(t.values()):
                continue
            m = models.setdefault(model, ModelStats(model))
            m.sessions += 1
            m.input += t["input"] + t["cache_write"] + t["cache_write_1h"] + t["cache_read"]
            m.output += t["output"]
            m.cache_read += t["cache_read"]
            m.cost += _cost(Usage({model: t}), prices)
    return sorted(models.values(), key=lambda m: m.cost, reverse=True)


def by_project(sessions: List[Session], prices: Prices) -> List[ProjectStats]:
    """Projects by cost, most expensive first."""
    projects: Dict[str, ProjectStats] = {}
    for s in sessions:
        p = projects.setdefault(s.project, ProjectStats(s.project))
        p.sessions += 1
        p.cost += _cost(s.usage, prices)
        p.seconds += sum(s.daily_work.values())
    return sorted(projects.values(), key=lambda p: p.cost, reverse=True)


def heatmap(seconds: Dict[str, float], today: str, weeks: int) -> Tuple[List[str], List[List[Optional[int]]]]:
    """(the Monday of each week, 7 rows Monday..Sunday of levels): 0 = no work, 1..4 by quartile of the active
    days, None = after today. It starts at the oldest active week, at most `weeks` back."""
    end = _parse(today)
    last_monday = end - _weekday(end) * DAY
    first_monday = last_monday - (weeks - 1) * 7 * DAY
    active = sorted(d for d, v in seconds.items() if v > 0)
    if active:
        oldest = _parse(active[0])
        first_monday = max(first_monday, oldest - _weekday(oldest) * DAY)
    mondays = [_day(first_monday + i * 7 * DAY) for i in range(int((last_monday - first_monday) // (7 * DAY)) + 1)]

    values = sorted(v for v in seconds.values() if v > 0)
    rows: List[List[Optional[int]]] = [[] for _ in range(7)]
    for monday in mondays:
        for wd in range(7):
            t = _parse(monday) + wd * DAY
            v = seconds.get(_day(t), 0.0)
            level = None if t > end else 0 if v <= 0 else 1 + min(3, 4 * bisect.bisect_left(values, v) // len(values))
            rows[wd].append(level)
    return mondays, rows


# --- text ---

def render(sessions: List[Session], prices: Prices, today: str, weeks: int, color: bool) -> List[str]:
    paint = (lambda code, text: f"{code}{text}{RESET}") if color else (lambda code, text: text)
    days = by_day(sessions, prices)
    total_cost = sum(d.cost for d in days)
    total_time = sum(d.seconds for d in days)
    out = [paint(BOLD, f"{len(sessions):,} sessions · {fmt_money(total_cost)} · {fmt_total(total_time)} of Claude time"),
           ""]

    # activity
    seconds = {d.day: d.seconds for d in days}
    mondays, rows = heatmap(seconds, today, weeks)
    out.append(paint(BOLD, f"Activity: Claude's working time per day, {len(mondays)} weeks"))
    out.append("     " + _month_labels(mondays))
    for wd, (name, row) in enumerate(zip(["Mon", "", "Wed", "", "Fri", "", "Sun"], rows)):
        cells = " ".join(" " if level is None else paint(LEVELS[level], CELL) for level in row)
        out.append(f"{name:<4} {cells}")
    legend = " ".join(paint(code, CELL) for code in LEVELS)
    out.append(f"     {paint(DIM, 'less')} {legend} {paint(DIM, 'more')}")
    active = [d for d in days if d.seconds > 0]
    if active:
        busiest = max(active, key=lambda d: d.seconds)
        span = (_parse(today) - _parse(days[0].day)) // DAY + 1
        out.append(f"     Active on {len(active)} of {span:.0f} days · busiest {_label(busiest.day)}, "
                   f"{fmt_total(busiest.seconds)} · streak {_streak(seconds, today)} days")
    out.append("")

    # last 14 days
    recent = {d.day: d for d in days}
    last = [recent.get(_day(_parse(today) - i * DAY), DayStats(_day(_parse(today) - i * DAY))) for i in range(14)]
    top = max((d.cost for d in last), default=0.0)
    out.append(paint(BOLD, f"{'LAST 14 DAYS':<16}{'SESSIONS':>9}{'COST':>11}{'CLAUDE TIME':>13}"))
    for d in last:
        line = f"{_label(d.day):<16}{d.sessions or '':>9}{fmt_money(d.cost) if d.sessions else '':>11}" \
               f"{fmt_total(d.seconds) if d.seconds else '':>13}  {paint(ACCENT, hbar(d.cost, top))}"
        out.append(line.rstrip())
    out.append("")

    # models
    models = by_model(sessions, prices)
    out.append(paint(BOLD, f"{'MODELS':<22}{'COST':>11}  {'SHARE':<16}{'SESSIONS':>9}{'OUTPUT':>9}{'CACHE HIT':>11}"))
    for m in models:
        share = m.cost / total_cost if total_cost else 0.0
        out.append(f"{m.model:<22}{fmt_money(m.cost):>11}  {paint(ACCENT, hbar(share, 1.0, 10).ljust(10))}{share:>5.0%} "
                   f"{m.sessions:>9}{fmt_tokens(m.output):>9}{m.cache_hit:>11.0%}")
    out.append("")

    # projects
    projects = by_project(sessions, prices)[:10]
    top = max((p.cost for p in projects), default=0.0)
    out.append(paint(BOLD, f"{'PROJECTS':<22}{'SESSIONS':>9}{'COST':>11}{'CLAUDE TIME':>13}"))
    for p in projects:
        out.append(f"{p.project[:21]:<22}{p.sessions:>9}{fmt_money(p.cost):>11}{fmt_total(p.seconds):>13}  "
                   f"{paint(ACCENT, hbar(p.cost, top))}")
    return out


def as_json(sessions: List[Session], prices: Prices) -> str:
    days = by_day(sessions, prices)
    data = {
        "totals": {"sessions": len(sessions), "cost": sum(d.cost for d in days),
                   "claude_seconds": sum(d.seconds for d in days)},
        "days": [{"day": d.day, "sessions": d.sessions, "cost": d.cost, "claude_seconds": d.seconds} for d in days],
        "models": [{**asdict(m), "cache_hit": m.cache_hit} for m in by_model(sessions, prices)],
        "projects": [{"project": p.project, "sessions": p.sessions, "cost": p.cost, "claude_seconds": p.seconds}
                     for p in by_project(sessions, prices)],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def hbar(value: float, top: float, width: int = 20) -> str:
    """A bar `width` cells for `top`, precise to an eighth of a cell; nothing for zero."""
    if top <= 0 or value <= 0:
        return ""
    full, part = divmod(int(value / top * width * 8), 8)
    return "█" * full + (" ▏▎▍▌▋▊▉"[part] if part else "") or "▏"


def _cost(usage: Optional[Usage], prices: Prices) -> float:
    """Cost of the models with a known price; the others count as 0."""
    if usage is None:
        return 0.0
    return sum(Usage({m: t}).cost(prices) or 0.0 for m, t in usage.tokens.items() if price_for(m, prices))


def _month_labels(mondays: List[str]) -> str:
    """The month's name over the first week that starts in it, two characters per week, as GitHub does."""
    line = ""
    for i, monday in enumerate(mondays):
        if i == 0 or monday[5:7] != mondays[i - 1][5:7]:
            if len(line) < 2 * i or i == 0:
                line = line.ljust(2 * i) + time.strftime("%b", time.localtime(_parse(monday)))
    return line


def _streak(seconds: Dict[str, float], today: str) -> int:
    """Days in a row with work, up to today (or yesterday, if today has none yet)."""
    t = _parse(today)
    if seconds.get(today, 0) <= 0:
        t -= DAY
    n = 0
    while seconds.get(_day(t), 0) > 0:
        n, t = n + 1, t - DAY
    return n


def _label(day: str) -> str:
    return time.strftime("%a ", time.localtime(_parse(day))) + day


def _parse(day: str) -> float:
    return time.mktime(time.strptime(day + " 12", "%Y-%m-%d %H"))  # noon: a DST change cannot move the date


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _weekday(t: float) -> int:
    return time.localtime(t).tm_wday  # Monday = 0
