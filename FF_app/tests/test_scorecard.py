"""Scorecard service — grading by shift/day/week, slices, trends (§10).

Hand-built record fixtures with known arithmetic so every assertion is a
check against a number computed by hand, never against the code under
test. Covers: GG-3 excused-leaves-goal, GG-2 off-plan-never-earns (via
compliance), grade bands, all five slices (fleet/team/group/shift/
building), all three periods, trend directions, week-over-week deltas,
and the shared-with-web-layer group rule.
"""

from __future__ import annotations

import pytest

import config
from ff.services import scorecard as sc


def R(**kw) -> dict:
    """Record factory with small-fixture defaults."""
    base = dict(
        round_no=1,
        day=7,
        shift=1,
        team="T01",
        aircraft=1,
        station="P01",
        points=100,
        planned=True,
        done=True,
        excused=False,
    )
    base.update(kw)
    return sc.make_record(**base)


# ---------------------------------------------------------------------------
# dimension derivations
# ---------------------------------------------------------------------------


def test_building_of_station_mapping():
    assert sc.building_of_station("P01") == "FAL-A"
    assert sc.building_of_station("P05") == "FAL-A"
    assert sc.building_of_station("P06") == "FAL-B"
    assert sc.building_of_station("P10") == "FAL-B"
    assert sc.building_of_station("POST-FAL") == "FLIGHTLINE"
    assert sc.building_of_station("LATE-DELIVERY") == "DELIVERY"
    assert sc.building_of_station("???") == "UNASSIGNED"


def test_team_group_matches_web_layer_rule():
    """Same contiguous-chunk rule as ff.web.app._index_fleet (config §10)."""
    from ff.web.app import TEAM_GROUP_SIZE

    assert TEAM_GROUP_SIZE == config.TEAM_GROUP_SIZE
    teams = [f"T{i:02d}" for i in range(1, 21)]
    assert sc.team_group("T01", teams) == "G1"
    assert sc.team_group("T05", teams) == "G1"
    assert sc.team_group("T06", teams) == "G2"
    assert sc.team_group("T20", teams) == "G4"
    assert sc.team_group("TXX", teams) == "G?"


def test_workday_and_week_derivation():
    # OR-3: the overnight S3 belongs to the day it flows into.
    assert sc.workday_of_slot(6, 3) == 7  # Sunday night -> Monday
    assert sc.workday_of_slot(7, 1) == 7
    assert sc.workday_of_slot(11, 3) == 12  # Friday night -> Saturday
    assert sc.week_of_workday(7) == 1
    assert sc.week_of_workday(11) == 1
    assert sc.week_of_workday(14) == 2


def test_grade_bands():
    assert sc.grade_of(1.0) == "A"
    assert sc.grade_of(0.95) == "A"
    assert sc.grade_of(0.90) == "B"
    assert sc.grade_of(0.80) == "C"
    assert sc.grade_of(0.60) == "D"
    assert sc.grade_of(0.10) == "F"


def test_make_record_rejects_done_and_excused():
    with pytest.raises(ValueError):
        R(done=True, excused=True)


# ---------------------------------------------------------------------------
# aggregation semantics
# ---------------------------------------------------------------------------


def _fixture() -> list[dict]:
    """Two teams, one slot: hand-computable goal/earned/excused/offplan.

    T01: planned 100 done + 50 not-done + 30 excused-not-done
         -> goal 150, earned 100, attainment 100/150, excused 30
    T02: planned 200 done, plus offplan 40 done (an OOS jump)
         -> goal 200, earned 200, attainment 1.0,
            compliance 200/(200+40) = 0.8333
    """
    return [
        R(team="T01", points=100),
        R(team="T01", aircraft=2, points=50, done=False),
        R(team="T01", aircraft=3, points=30, done=False, excused=True),
        R(team="T02", station="P07", points=200),
        R(
            team="T02",
            station="P07",
            aircraft=4,
            points=40,
            planned=False,
            offplan=True,
            oos=True,
        ),
    ]


def test_aggregate_team_shift_semantics():
    rows = sc.aggregate(_fixture(), "team", "shift")
    by = {r["slice"]: r for r in rows}
    t1, t2 = by["T01"], by["T02"]
    assert t1["goal"] == 150 and t1["earned"] == 100
    assert t1["attainment"] == round(100 / 150, 4)
    assert t1["excused_points"] == 30 and t1["excused_n"] == 1
    assert t1["compliance"] == 1.0  # no off-plan work
    assert t2["goal"] == 200 and t2["earned"] == 200
    assert t2["attainment"] == 1.0 and t2["grade"] == "A"
    assert t2["offplan_points"] == 40 and t2["oos_n"] == 1
    assert t2["compliance"] == round(200 / 240, 4)  # GG-2: offplan never earns


def test_aggregate_fleet_and_building_and_group_slices():
    recs = _fixture()
    fleet = sc.aggregate(recs, "fleet", "shift")
    assert len(fleet) == 1 and fleet[0]["slice"] == "FLEET"
    assert fleet[0]["goal"] == 350 and fleet[0]["earned"] == 300

    bld = {r["slice"]: r for r in sc.aggregate(recs, "building", "shift")}
    assert bld["FAL-A"]["goal"] == 150  # T01 rows on P01
    assert bld["FAL-B"]["goal"] == 200  # T02 rows on P07

    grp = {r["slice"]: r for r in sc.aggregate(recs, "group", "shift")}
    assert grp["G1"]["goal"] == 350  # both teams in the first chunk


def test_aggregate_periods_roll_up():
    recs = [
        R(day=7, shift=1, points=100),  # Mon
        R(day=7, shift=2, aircraft=2, points=100, done=False),  # Mon
        R(day=8, shift=1, aircraft=3, points=100),  # Tue
        R(round_no=2, day=14, shift=1, aircraft=4, points=100),  # Mon week 2
    ]
    days = {r["period_key"]: r for r in sc.aggregate(recs, "fleet", "day")}
    assert days[7]["goal"] == 200 and days[7]["earned"] == 100
    assert days[8]["attainment"] == 1.0
    weeks = {r["period_key"]: r for r in sc.aggregate(recs, "fleet", "week")}
    assert weeks[1]["goal"] == 300 and weeks[1]["earned"] == 200
    assert weeks[2]["attainment"] == 1.0


def test_empty_goal_is_honest_zero():
    rows = sc.aggregate(
        [R(points=50, done=False, excused=True)], "team", "shift"
    )
    assert rows[0]["goal"] == 0
    assert rows[0]["attainment"] == 0.0  # never a fake 100%
    assert rows[0]["excused_points"] == 50


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------


def _day_series(atts: list[float]) -> list[dict]:
    """Records producing the given per-day fleet attainment series."""
    recs = []
    for i, att in enumerate(atts):
        day = 7 + i
        recs.append(R(day=day, aircraft=2 * i, points=100, done=True))
        # a second task sized so attainment = 100*? — simpler: one done row
        # worth att*100 and one not-done row worth (1-att)*100
    # rebuild precisely: one done row of att*1000 and one open row of rest
    recs = []
    for i, att in enumerate(atts):
        day = 7 + i
        done_pts = int(round(att * 1000))
        open_pts = 1000 - done_pts
        recs.append(R(day=day, aircraft=2 * i, points=done_pts, done=True))
        if open_pts:
            recs.append(
                R(day=day, aircraft=2 * i + 1, points=open_pts, done=False)
            )
    return recs


def test_trend_directions():
    up = sc.aggregate(_day_series([0.6, 0.7, 0.8, 0.9]), "fleet", "day")
    t = sc.trend(up, "FLEET")
    assert t["direction"] == "improving" and t["delta"] == pytest.approx(0.3)

    down = sc.aggregate(_day_series([0.9, 0.8, 0.7]), "fleet", "day")
    assert sc.trend(down, "FLEET")["direction"] == "declining"

    flat = sc.aggregate(_day_series([0.8, 0.8, 0.8]), "fleet", "day")
    assert sc.trend(flat, "FLEET")["direction"] == "flat"


def test_build_scorecard_shape_and_week_over_week():
    recs = _day_series([0.6, 0.7]) + [
        # week 2: perfect day
        R(round_no=2, day=14, aircraft=99, points=1000, done=True)
    ]
    card = sc.build_scorecard(recs, mock_data=True)
    assert card["meta"]["mock_data"] is True
    assert set(card["tables"]) == set(sc.SLICES)
    assert set(card["tables"]["team"]) == set(sc.PERIODS)
    wow = card["week_over_week"]["fleet"]
    assert len(wow) == 1
    # week 1 attainment = 1300/2000 = 0.65 -> week 2 = 1.0
    assert wow[0]["from_week"] == 1 and wow[0]["to_week"] == 2
    assert wow[0]["delta"] == pytest.approx(1.0 - 0.65, abs=1e-4)
    # grade rubric rides in meta (OR-5: the rubric is visible)
    assert card["meta"]["grade_bands"][0][1] == "A"


def test_determinism():
    recs = _fixture()
    a = sc.build_scorecard(recs)
    b = sc.build_scorecard(list(reversed(recs)))
    assert a == b
