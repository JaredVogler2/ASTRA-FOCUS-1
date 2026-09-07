"""Economics tests — the floor (unavoidable vs controllable) split math.

Contract (ARCHITECTURE.md §ff/engine/economics.py), per aircraft:
    penalty_usd  = lateness_days * LATENESS_PENALTY_USD_PER_DAY
    floor_days   = min(max(0, today_day - deadline_day), lateness_days)
    controllable = lateness_days - floor_days
Rates are PLACEHOLDERS (OR-5): the rollup must carry the
``"source": "config-defaults"`` label.
"""

from __future__ import annotations

import config
from ff.engine.economics import fleet_economics

RATE = config.LATENESS_PENALTY_USD_PER_DAY


def _rows():
    return [
        # late, deadline recently passed: 2 days already locked in, 3 recoverable
        {"aircraft": 1, "deadline_day": 10, "lateness_days": 5},
        # late, deadline long past: all 3 lateness days are unavoidable
        {"aircraft": 2, "deadline_day": 0, "lateness_days": 3},
        # on time: contributes nothing
        {"aircraft": 3, "deadline_day": 20, "lateness_days": 0},
        # late but deadline still in the future: fully controllable
        {"aircraft": 4, "deadline_day": 15, "lateness_days": 2},
    ]


def test_floor_split_math_exact():
    """floor = min(max(0, today - deadline), lateness); controllable = rest."""
    econ = fleet_economics(_rows(), today_day=12)
    per = {row["aircraft"]: row for row in econ["aircraft"]}

    assert per[1]["floor_days"] == 2                # min(12-10, 5)
    assert per[1]["controllable_days"] == 3
    assert per[1]["penalty_usd"] == 5 * RATE
    assert per[1]["unavoidable_penalty_usd"] == 2 * RATE
    assert per[1]["controllable_penalty_usd"] == 3 * RATE

    assert per[2]["floor_days"] == 3                # min(12-0, 3) capped at lateness
    assert per[2]["controllable_days"] == 0

    assert per[3]["floor_days"] == 0
    assert per[3]["penalty_usd"] == 0

    assert per[4]["floor_days"] == 0                # max(0, 12-15) = 0
    assert per[4]["controllable_days"] == 2         # a replan can still save it


def test_fleet_rollup():
    """Fleet totals are the sums of the per-aircraft split; the split is a
    partition (unavoidable + controllable == total)."""
    econ = fleet_economics(_rows(), today_day=12)
    assert econ["total_penalty_usd"] == (5 + 3 + 0 + 2) * RATE
    assert econ["unavoidable_penalty_usd"] == (2 + 3) * RATE
    assert econ["controllable_penalty_usd"] == (3 + 2) * RATE
    assert (
        econ["unavoidable_penalty_usd"] + econ["controllable_penalty_usd"]
        == econ["total_penalty_usd"]
    )
    assert econ["late_count"] == 3


def test_placeholder_source_label():
    """OR-5 honesty: economics derived from config placeholders must be
    labeled 'config-defaults' — never presentable as negotiated rates."""
    econ = fleet_economics(_rows(), today_day=12)
    assert econ["source"] == "config-defaults"


def test_floor_never_negative_or_above_lateness():
    """today before every deadline => zero unavoidable days everywhere."""
    econ = fleet_economics(_rows(), today_day=0)
    assert econ["unavoidable_penalty_usd"] == 0
    assert econ["controllable_penalty_usd"] == econ["total_penalty_usd"]
    for row in econ["aircraft"]:
        assert 0 <= row["floor_days"] <= row["lateness_days"]


def test_deterministic_and_sorted():
    """Identical inputs (any order) => identical output, aircraft sorted."""
    rows = _rows()
    a = fleet_economics(rows, today_day=12)
    b = fleet_economics(list(reversed(rows)), today_day=12)
    assert a == b
    numbers = [row["aircraft"] for row in a["aircraft"]]
    assert numbers == sorted(numbers)


def test_engine_stats_plug_compatibility(schedule):
    """The real Schedule.stats['aircraft'] block feeds fleet_economics
    unchanged (the wiring contract between engine and economics)."""
    econ = fleet_economics(schedule.stats["aircraft"], today_day=0)
    assert econ["late_count"] == sum(
        1 for row in schedule.stats["aircraft"] if row["lateness_days"] > 0
    )
    assert econ["total_penalty_usd"] == schedule.stats["fleet_lateness_days"] * RATE
