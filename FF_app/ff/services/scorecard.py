"""Scorecard — grade execution by shift / day / week, sliced any way.

The fortnight increment's grading layer: a pure, deterministic fold over
per-task EXECUTION RECORDS (one row per planned task per executed shift,
plus flagged off-plan / out-of-sequence completions) into graded
performance tables at three period grains with trend classification.

Single source of truth: the record stream. The digital-fortnight runner
(`tools/sim_5day.py`) writes it today; a real actuals pipeline writes the
identical shape tomorrow — this module never cares which.

Record shape (all keys required; see ``make_record``)::

    {"round", "day", "shift",         # executed slot
     "workday", "week",               # derived calendar (OR-3: S3 -> day+1)
     "team", "aircraft", "station", "building",
     "points",                        # GG-2 effort-weighted point value
     "planned",                       # True = in the slot's issued plan
     "done", "excused",               # end-of-shift outcome (GG-3 excusal)
     "offplan", "oos"}                # non-compliance flags

Scoring semantics (mirrors ``points.shift_report`` exactly — GG-2/GG-3):

- goal        = sum of points over PLANNED, NOT-excused rows
- earned      = sum of points over PLANNED rows that finished (done)
- attainment  = earned / goal (0.0 on empty goal — no fake 100%)
- excused     = sum of points GG-3 removed from the goal, shown visibly
- offplan_pts = sum of points completed OUTSIDE the plan (deviations +
                OOS jumps); NEVER added to earned — instead surfaced as
- compliance  = earned / (earned + offplan_pts): the share of completed
                value that followed the plan (1.0 = perfect discipline)
- grade       = letter from config.GRADE_BANDS on attainment
                (PLACEHOLDER thresholds, OR-5)

Slices: ``fleet`` (one row), ``team``, ``group`` (superintendent position
— contiguous chunks of config.TEAM_GROUP_SIZE over the sorted team list,
the SAME rule the web layer uses for super auth scopes), ``shift`` (shift
number 1/2/3 across the calendar), ``building`` (derived from the
aircraft's line station — see ``building_of_station``).

Periods: ``shift`` (one executed slot), ``day`` (work day; the overnight
S3 belongs to the day it flows into, OR-3), ``week`` (work-day // 7).

Trends: per slice value at a period grain — least-squares slope over the
period series, first->last delta, and a direction classified against
config.TREND_FLAT_SLOPE ("improving" / "flat" / "declining"). Week grain
additionally reports week-over-week attainment deltas.

Honesty (OR-5): ``build_scorecard`` carries ``mock_data`` through from the
caller; grade bands ride in ``meta`` so every consumer can see the rubric.
Deterministic throughout: sorted iteration, pure arithmetic, no clock.
"""

from __future__ import annotations

import config

SLICES = ("fleet", "team", "group", "shift", "building", "team_shift")
PERIODS = ("shift", "day", "week")

WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# dimension derivations
# ---------------------------------------------------------------------------


def building_of_station(station: str) -> str:
    """Physical building derived from an aircraft's line station.

    Deterministic mapping over the generator's station vocabulary (the
    same rule a real facility feed would replace): factory pulse
    positions P01-P05 sit in FAL Building A, P06-P10 in FAL Building B,
    post-FAL residual work on the Flightline, aircraft at delivery in
    the Delivery Center. Unknown stations map to "UNASSIGNED" honestly
    rather than guessing.
    """
    s = (station or "").strip().upper()
    if s.startswith("P") and s[1:].isdigit():
        return "FAL-A" if int(s[1:]) <= 5 else "FAL-B"
    if s == "POST-FAL":
        return "FLIGHTLINE"
    if s == "LATE-DELIVERY":
        return "DELIVERY"
    return "UNASSIGNED"


def team_group(team: str, teams: list[str]) -> str:
    """Superintendent position (group id) for a team — the web layer's rule.

    Contiguous chunks of ``config.TEAM_GROUP_SIZE`` over the SORTED team
    list (identical to ``ff.web.app._index_fleet``; both read the size
    from config §10 so the scopes can never drift). Unknown team -> "G?".
    """
    ordered = sorted(set(teams))
    if team not in ordered:
        return "G?"
    return f"G{ordered.index(team) // config.TEAM_GROUP_SIZE + 1}"


def workday_of_slot(day: int, shift: int) -> int:
    """The work day a slot belongs to (OR-3: overnight S3 -> day + 1)."""
    return day + 1 if int(shift) == 3 else day


def week_of_workday(workday: int) -> int:
    """Calendar work-week index (day 0 = a Monday => week = workday // 7)."""
    return workday // 7


def grade_of(attainment: float) -> str:
    """Letter grade from config.GRADE_BANDS (descending floors; below all
    floors is 'F'). PLACEHOLDER thresholds — OR-5, calibrate on real data."""
    for floor, letter in config.GRADE_BANDS:
        if attainment >= float(floor):
            return str(letter)
    return "F"


def stamp_gates_from_schedule(fleet, schedule, grace_days: int | None = None) -> int:
    """Stamp ``Task.gate_day`` from the PLAN OF RECORD (baseline schedule).

    Gate-pressure increment (docs/GATE_PRESSURE_DESIGN.md §2.1): with no
    CS-gate calendar feed, the published baseline IS the gate ladder —
    ``gate_day = baseline slot day + GATE_GRACE_DAYS``. A task slipping
    more than the grace past where the plan of record put it is BEHIND
    SCHEDULE even with delivery slack remaining. Tasks absent from the
    schedule (none, on a full plan) keep gate_day None. Idempotent and
    deterministic; returns the number of tasks stamped. Real data replaces
    this writer with the station-gate feed — the reader never knows.
    """
    grace = config.GATE_GRACE_DAYS if grace_days is None else int(grace_days)
    stamped = 0
    for task in sorted(fleet.tasks, key=lambda t: t.task_id):
        asg = schedule.assignments.get(task.task_id)
        if asg is not None:
            task.gate_day = int(asg.day) + grace
            stamped += 1
    return stamped


CASCADE_KINDS = ("", "primary", "same_team", "cross_team")


def make_record(
    *,
    round_no: int,
    day: int,
    shift: int,
    team: str,
    aircraft: int,
    station: str,
    points: int,
    planned: bool,
    done: bool,
    excused: bool,
    offplan: bool = False,
    oos: bool = False,
    behind_days: int = 0,
    cascade: str = "",
) -> dict:
    """Build one canonical execution record (derives workday/week/building).

    Enforces the docstring's shape so every writer (sim today, actuals
    pipeline tomorrow) emits identical rows. An excused row must not be
    done (GG-3: finished work needs no excuse) — raises ValueError.

    Gate-pressure fields (docs/GATE_PRESSURE_DESIGN.md; owner directive on
    out-of-control causes): ``behind_days`` = days past the task's station
    gate at execution time (0 = on schedule / no gate); ``cascade``
    classifies WHY a behind task is behind — ``primary`` (its own
    predecessors are done: the aging is owned outright), ``same_team``
    (waiting on its OWN team's unfinished predecessor: owned at the root,
    counted separately so one slipped root is not read as N independent
    failures), ``cross_team`` (waiting on ANOTHER team's predecessor: the
    needs-support lane). Empty string = not behind.
    """
    if excused and done:
        raise ValueError(f"record for {team} r{round_no}: done rows cannot be excused")
    if cascade not in CASCADE_KINDS:
        raise ValueError(f"cascade must be one of {CASCADE_KINDS}, got {cascade!r}")
    wd = workday_of_slot(day, shift)
    return {
        "round": int(round_no),
        "day": int(day),
        "shift": int(shift),
        "workday": wd,
        "week": week_of_workday(wd),
        "team": str(team),
        "aircraft": int(aircraft),
        "station": str(station),
        "building": building_of_station(station),
        "points": int(points),
        "planned": bool(planned),
        "done": bool(done),
        "excused": bool(excused),
        "offplan": bool(offplan),
        "oos": bool(oos),
        "behind_days": max(0, int(behind_days)),
        "cascade": cascade,
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _slice_key(rec: dict, slice_by: str, teams: list[str]) -> str:
    if slice_by == "fleet":
        return "FLEET"
    if slice_by == "team":
        return rec["team"]
    if slice_by == "group":
        return team_group(rec["team"], teams)
    if slice_by == "shift":
        return f"S{rec['shift']}"
    if slice_by == "building":
        return rec["building"]
    if slice_by == "team_shift":
        # Factory-wall competition unit (owner directive 2026-07-11): a
        # team's crew on ONE shift number — "T01 first shift" competes
        # with "T05 second shift" as distinct units.
        return f"{rec['team']}-S{rec['shift']}"
    raise ValueError(f"unknown slice {slice_by!r} (want one of {SLICES})")


def _period_key(rec: dict, period: str):
    if period == "shift":
        return (rec["day"], rec["shift"])
    if period == "day":
        return rec["workday"]
    if period == "week":
        return rec["week"]
    raise ValueError(f"unknown period {period!r} (want one of {PERIODS})")


def period_label(period: str, key) -> str:
    """Human label for a period key ('Mon d7 S1' / 'Tue d8' / 'Week 1')."""
    if period == "shift":
        day, shift = key
        wd = WEEKDAY[workday_of_slot(day, shift) % 7]
        night = " (night)" if shift == 3 else ""
        return f"{wd} d{day} S{shift}{night}"
    if period == "day":
        return f"{WEEKDAY[key % 7]} d{key}"
    return f"Week {key}"


def aggregate(records: list[dict], slice_by: str, period: str) -> list[dict]:
    """Grade every (slice value, period) cell — the scorecard's core fold.

    Returns rows sorted by (period_key, slice_value)::

        {"slice": str, "period_key": ..., "period": str-label,
         "goal", "earned", "excused_points", "offplan_points",
         "attainment", "compliance", "grade",
         "planned_n", "done_n", "excused_n", "offplan_n", "oos_n"}

    Enforces the module-docstring semantics: excused rows leave the goal
    (GG-3, visibly — the points still ride along), off-plan value never
    enters earned (GG-2: the plan defines the goal) but is graded via
    ``compliance``. Empty-goal cells report attainment 0.0 honestly.
    """
    teams = sorted({r["team"] for r in records})
    cells: dict[tuple, dict] = {}
    for rec in sorted(
        records, key=lambda r: (r["round"], r["team"], r["aircraft"], -r["points"])
    ):
        sk = _slice_key(rec, slice_by, teams)
        pk = _period_key(rec, period)
        cell = cells.setdefault(
            (pk, sk),
            {
                "goal": 0,
                "earned": 0,
                "excused_points": 0,
                "offplan_points": 0,
                "planned_n": 0,
                "done_n": 0,
                "excused_n": 0,
                "offplan_n": 0,
                "oos_n": 0,
                # Gate health (docs/GATE_PRESSURE_DESIGN.md §3): the aging
                # backlog and its burn-down, with cascade attribution so
                # one slipped root never reads as N independent failures.
                "behind_open_points": 0,
                "behind_done_points": 0,
                "_behind_ages": [],
                "behind_primary_n": 0,
                "behind_same_team_n": 0,
                "behind_cross_team_n": 0,
            },
        )
        if rec["planned"]:
            cell["planned_n"] += 1
            if rec["excused"]:
                cell["excused_points"] += rec["points"]
                cell["excused_n"] += 1
            else:
                cell["goal"] += rec["points"]
            if rec["done"]:
                cell["earned"] += rec["points"]
                cell["done_n"] += 1
        else:
            cell["offplan_points"] += rec["points"]
            cell["offplan_n"] += 1
            if rec["oos"]:
                cell["oos_n"] += 1
        if rec.get("behind_days", 0) > 0:
            if rec["done"]:
                cell["behind_done_points"] += rec["points"]  # burned down
            else:
                cell["behind_open_points"] += rec["points"]
                cell["_behind_ages"].append(rec["behind_days"])
                kind = rec.get("cascade", "") or "primary"
                cell[f"behind_{kind}_n"] = cell.get(f"behind_{kind}_n", 0) + 1
    rows = []
    # Chronological period order: keys are ints (day/week) or (day, shift)
    # tuples — normalize to tuples so 7 < 10 sorts numerically, never as
    # strings ("10" < "7" would reverse every trend series).
    def _order(key: tuple) -> tuple:
        pk = key[0]
        return (pk if isinstance(pk, tuple) else (pk,), key[1])

    for (pk, sk) in sorted(cells, key=_order):
        cell = cells[(pk, sk)]
        att = cell["earned"] / cell["goal"] if cell["goal"] else 0.0
        denom = cell["earned"] + cell["offplan_points"]
        ages = cell.pop("_behind_ages")
        rows.append(
            {
                "slice": sk,
                "period_key": pk,
                "period": period_label(period, pk),
                **cell,
                "behind_avg_age": (
                    round(sum(ages) / len(ages), 2) if ages else 0.0
                ),
                "attainment": round(att, 4),
                "compliance": round(cell["earned"] / denom, 4) if denom else 1.0,
                "grade": grade_of(att),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------


def _slope(series: list[float]) -> float:
    """Least-squares slope per period step (0.0 for < 2 points)."""
    n = len(series)
    if n < 2:
        return 0.0
    xm = (n - 1) / 2.0
    ym = sum(series) / n
    num = sum((i - xm) * (y - ym) for i, y in enumerate(series))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den else 0.0


def trend(rows: list[dict], slice_value: str) -> dict:
    """Trend for one slice value over its period series (rows from
    ``aggregate``): slope, first->last delta, classified direction.

    Direction: |slope| < config.TREND_FLAT_SLOPE -> "flat", positive ->
    "improving", negative -> "declining". Cells with zero goal are
    excluded (an empty slice-period says nothing about performance).
    """
    series = [
        r["attainment"]
        for r in rows
        if r["slice"] == slice_value and r["goal"] > 0
    ]
    slope = _slope(series)
    if abs(slope) < config.TREND_FLAT_SLOPE:
        direction = "flat"
    elif slope > 0:
        direction = "improving"
    else:
        direction = "declining"
    return {
        "slice": slice_value,
        "periods": len(series),
        "first": series[0] if series else 0.0,
        "last": series[-1] if series else 0.0,
        "delta": round(series[-1] - series[0], 4) if series else 0.0,
        "slope": round(slope, 5),
        "direction": direction,
    }


def build_scorecard(records: list[dict], mock_data: bool = True) -> dict:
    """The full graded scorecard: every slice x every period + trends.

    Structure (JSON-safe)::

        {"meta": {mock_data, grade_bands, slices, periods,
                  records, weeks, workdays, slots},
         "tables": {slice: {period: [rows]}},
         "trends": {slice: {"day": [trend...], "week": [trend...]}},
         "week_over_week": {slice: [{"slice", "from_week", "to_week",
                                     "delta"}...]}}

    OR-5: ``mock_data`` rides in meta; grade bands are surfaced so every
    consumer sees the rubric it was graded against.
    """
    tables: dict[str, dict[str, list[dict]]] = {}
    trends: dict[str, dict[str, list[dict]]] = {}
    wow: dict[str, list[dict]] = {}
    for slice_by in SLICES:
        tables[slice_by] = {}
        for period in PERIODS:
            tables[slice_by][period] = aggregate(records, slice_by, period)
        values = sorted({r["slice"] for r in tables[slice_by]["day"]})
        trends[slice_by] = {
            grain: [trend(tables[slice_by][grain], v) for v in values]
            for grain in ("day", "week")
        }
        week_rows = tables[slice_by]["week"]
        weeks = sorted({r["period_key"] for r in week_rows})
        deltas = []
        for value in values:
            by_week = {
                r["period_key"]: r["attainment"]
                for r in week_rows
                if r["slice"] == value and r["goal"] > 0
            }
            for a, b in zip(weeks, weeks[1:]):
                if a in by_week and b in by_week:
                    deltas.append(
                        {
                            "slice": value,
                            "from_week": a,
                            "to_week": b,
                            "delta": round(by_week[b] - by_week[a], 4),
                        }
                    )
        wow[slice_by] = deltas
    return {
        "meta": {
            "mock_data": bool(mock_data),
            "grade_bands": [list(b) for b in config.GRADE_BANDS],
            "trend_flat_slope": config.TREND_FLAT_SLOPE,
            "slices": list(SLICES),
            "periods": list(PERIODS),
            "records": len(records),
            "weeks": sorted({r["week"] for r in records}),
            "workdays": sorted({r["workday"] for r in records}),
            "slots": sorted({(r["day"], r["shift"]) for r in records}),
        },
        "tables": tables,
        "trends": trends,
        "week_over_week": wow,
    }
