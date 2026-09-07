"""Points/GAMES layer tests — ordering invariant, goal math, GG-2 leaderboard.

Contract (ARCHITECTURE.md §ff/services/points.py): one factor
implementation with the §5 weights, frozen per-snapshot normalizers,
shift goal = points of the PLANNED slice (excused work never dilutes it),
and the leaderboard NEVER ranks by raw points (GG-2).
"""

from __future__ import annotations

import pytest

import config
from tests.conftest import mk_aircraft, mk_fleet, mk_mech, mk_task, run_schedule

from ff.services.points import leaderboard, score_task, shift_report
from ff.services.snapshot import build_snapshot

A1, A2, A3, A4 = (f"0001-T{n:05d}" for n in range(1, 5))
U = "0001-T00005"           # unstaffable (crew 5 > pool 3): excused work
Z1 = "0002-T00001"          # trivial task, on-time aircraft (team T01)
W1 = "0002-T00002"          # small team T02's single task


@pytest.fixture()
def game_snap():
    """Hand-built snapshot with one LATE aircraft and one on-time aircraft.

    Aircraft 1 (deadline day 0): a 4-task RARE-skill chain only mechanic M1
    can work — one task per day, completion day 3, lateness 3 => its tasks
    are critical AND recovery-boosted. Aircraft 2 (deadline day 60): trivial
    work. Z1 and W1 are marked done AFTER scheduling (the actuals flow), so
    earned-points math has something to count.
    """
    mechanics = [
        mk_mech("T01-S1-M001", skills=["RARE", "COMMON"]),
        mk_mech("T01-S1-M002", skills=["COMMON"]),
        mk_mech("T01-S1-M003", skills=["COMMON"]),
        mk_mech("T02-S1-M001", team="T02", skills=["COMMON"]),
        mk_mech("T02-S1-M002", team="T02", skills=["COMMON"]),  # quota floor(1.7)=1
    ]
    tasks = [
        mk_task(A1, dur=400, skill="RARE", deadline=0),
        mk_task(A2, dur=400, skill="RARE", deadline=0, preds=[A1]),
        mk_task(A3, dur=400, skill="RARE", deadline=0, preds=[A2]),
        mk_task(A4, dur=400, skill="RARE", deadline=0, preds=[A3]),
        mk_task(U, dur=60, crew=5, skill="COMMON", deadline=0),  # pool is 3
        mk_task(Z1, aircraft=2, dur=30, skill="COMMON", deadline=60),
        mk_task(W1, aircraft=2, team="T02", dur=60, skill="COMMON", deadline=60),
    ]
    fleet = mk_fleet(
        tasks,
        mechanics,
        aircraft=[mk_aircraft(1, deadline=0), mk_aircraft(2, deadline=60)],
    )
    cpm, sched = run_schedule(fleet)

    # Sanity on the constructed scenario.
    assert sched.assignments[A4].day == 3          # aircraft 1 is 3 days late
    assert sched.assignments[Z1].day == 0
    assert sched.assignments[W1].day == 0
    assert sched.unscheduled[U] == "crew_exceeds_pool"

    # Actuals flow: work completed after the plan was cut.
    by_id = {t.task_id: t for t in fleet.tasks}
    by_id[Z1].state = "done"
    by_id[W1].state = "done"
    return build_snapshot(fleet, sched, cpm)


# ---------------------------------------------------------------------------
# ordering invariant
# ---------------------------------------------------------------------------


def test_recovery_boosted_critical_outscores_10x_trivial(game_snap):
    """Ordering invariant: a recovery-boosted critical-path task scores more
    than 10x a trivial task — the game can never rank busywork above the
    work that protects a late aircraft. GG-2 recalibration: factor
    contributions are effort x weight% x normalized factor."""
    critical = score_task(A1, game_snap)
    trivial = score_task(Z1, game_snap)

    # A1 is genuinely critical + recovery-boosted (late aircraft); binary
    # factors at raw 1.0 contribute the FULL percent-of-effort weight.
    a1_effort = critical["effort"]
    assert a1_effort == round(400 * 1 / config.EFFORT_CREW_MINUTES_PER_POINT)
    assert critical["components"]["critical"]["raw"] == 1.0
    assert critical["components"]["critical"]["points"] == round(
        a1_effort * config.W_CRIT / 100
    )
    assert critical["components"]["recovery"]["points"] == round(
        a1_effort * config.W_RECOVER / 100
    )
    # Z1 is genuinely trivial: no critical/recovery/risk/downstream boost.
    for factor in ("critical", "recovery", "risk", "downstream"):
        assert trivial["components"][factor]["points"] == 0, factor

    assert trivial["total"] >= 1  # every completed task is worth something
    assert critical["total"] > 10 * trivial["total"], (
        f"critical {critical['total']} must exceed 10x trivial {trivial['total']}"
    )


def test_score_breakdown_shape_and_determinism(game_snap):
    """ScoreBreakdown carries the contract fields, components in the shared
    {raw, weight, points} decomposition with effort as a NAMED component
    (GG-4: fully decomposed and explainable); same snapshot => same
    numbers."""
    first = score_task(A1, game_snap)
    for key in ("task_id", "total", "effort", "oos_penalty", "components", "explanation"):
        assert key in first
    assert first["effort"] >= 1  # every completed task is worth something
    assert "effort" in first["components"], "effort must be a named component"
    assert first["components"]["effort"]["points"] == first["effort"]
    assert first["components"]["effort"]["raw"] == 400.0  # crew-minutes (400m x 1)
    for name, comp in first["components"].items():
        assert set(comp) == {"raw", "weight", "points"}, name
    assert first["explanation"].startswith(f"+{first['effort']} effort ")
    assert score_task(A1, game_snap) == first  # frozen normalizers: replayable


def test_out_of_sequence_penalized_never_rewarded(game_snap):
    """GG guardrail: work started ahead of an unfinished predecessor takes
    the P_OOS penalty — points cannot be farmed by jumping the DAG. The
    dock is effort-scaled (round(effort * P_OOS / 100)) so big jumped jobs
    lose proportionally."""
    tasks = {t.task_id: t for t in game_snap["fleet"].tasks}
    tasks[A3].state = "in_progress"  # but A2 is not done
    try:
        jumped = score_task(A3, game_snap)
        assert jumped["oos_penalty"] == round(jumped["effort"] * config.P_OOS / 100)
        assert jumped["oos_penalty"] > 0
        in_order = score_task(A2, game_snap)  # pred A1 not done but A2 not started
        assert in_order["oos_penalty"] == 0
    finally:
        tasks[A3].state = "not_started"


# ---------------------------------------------------------------------------
# shift goal / excused math
# ---------------------------------------------------------------------------


def test_shift_report_goal_and_excused_math(game_snap):
    """goal = sum of points of the PLANNED slice; earned counts only done
    work; attainment = earned/goal; the unstaffable (delayed) task U is
    EXCUSED — never planned into the slice, so it cannot dilute attainment."""
    report = shift_report(game_snap, "T01", 0, 1)

    planned_ids = {row["task_id"] for row in report["breakdown"]}
    assert planned_ids == {A1, Z1}  # day-0 shift-1 slice of team T01
    assert U not in planned_ids, "excused (unscheduled) work must not appear"
    assert U in game_snap["schedule"].unscheduled

    pts = {tid: score_task(tid, game_snap)["total"] for tid in (A1, Z1)}
    assert report["goal"] == pts[A1] + pts[Z1]
    assert report["earned"] == pts[Z1]  # only Z1 is done
    assert report["attainment"] == round(pts[Z1] / (pts[A1] + pts[Z1]), 4)
    assert 0.0 < report["attainment"] < 1.0
    assert report["difficulty"] == round(report["goal"] / 2, 4)


def test_shift_report_empty_slice_no_fake_attainment(game_snap):
    """A slice with nothing planned reports goal 0 and attainment 0.0 —
    never a fake 100%."""
    report = shift_report(game_snap, "T01", 0, 3)  # nobody works shift 3
    assert report["goal"] == 0
    assert report["earned"] == 0
    assert report["attainment"] == 0.0
    assert report["breakdown"] == []


# ---------------------------------------------------------------------------
# GG-3 — excused planned tasks leave the goal denominator (INCREMENT 2)
# ---------------------------------------------------------------------------

E1 = "0003-T00001"  # blocked w/ parts ETA (LATE_PART, excusable) — planned
E2 = "0003-T00002"  # plain planned task
E4 = "0003-T00003"  # waits on own team's E2 (SAME_TEAM_PREDECESSOR, NOT excusable)


@pytest.fixture()
def excusal_snap():
    """One team, one day-0 shift-1 slice with all three GG-3 shapes planned:
    E1 blocked-on-parts (auto LATE_PART, excusable), E2 plain, E4 gated on
    its OWN team's E2 (auto SAME_TEAM_PREDECESSOR, not excusable)."""
    mechanics = [
        mk_mech("T01-S1-M001", skills=["COMMON"]),
        mk_mech("T01-S1-M002", skills=["COMMON"]),
        mk_mech("T01-S1-M003", skills=["COMMON"]),
    ]
    tasks = [
        mk_task(E1, aircraft=3, dur=60, skill="COMMON", state="blocked", parts_eta=0),
        mk_task(E2, aircraft=3, dur=60, skill="COMMON"),
        mk_task(E4, aircraft=3, dur=60, skill="COMMON", preds=[E2]),
    ]
    fleet = mk_fleet(tasks, mechanics, aircraft=[mk_aircraft(3, deadline=30)])
    cpm, sched = run_schedule(fleet)
    # The whole slice must land on day 0 shift 1 for the test to bite.
    for tid in (E1, E2, E4):
        assert (sched.assignments[tid].day, sched.assignments[tid].shift) == (0, 1)
    return build_snapshot(fleet, sched, cpm)


def test_excused_task_leaves_goal_denominator(excusal_snap):
    """GG-3: 'Excused planned tasks (excusable causes only) leave the GOAL
    DENOMINATOR; report gains excused_points, excusals — visible, never
    silent.' E1 (blocked, auto LATE_PART) drops out of goal but shows up in
    excused_points + excusals + a flagged breakdown row."""
    report = shift_report(excusal_snap, "T01", 0, 1)
    pts = {tid: score_task(tid, excusal_snap)["total"] for tid in (E1, E2, E4)}

    assert report["goal"] == pts[E2] + pts[E4], "E1 left the denominator"
    assert report["excused_points"] == pts[E1], "…but never silently"
    causes = {(row["task_id"], row["cause"]) for row in report["excusals"]}
    assert (E1, "LATE_PART") in causes
    for row in report["excusals"]:
        assert set(row) == {"task_id", "cause", "evidence", "source"}
    rows = {row["task_id"]: row for row in report["breakdown"]}
    assert rows[E1]["excused"] is True
    assert rows[E2]["excused"] is False and rows[E4]["excused"] is False
    assert len(report["breakdown"]) == 3, "excused work stays VISIBLE"


def test_non_excusable_cause_stays_in_denominator(excusal_snap):
    """GG-3: 'Non-excusable causes stay in the denominator.' E4 carries an
    auto SAME_TEAM_PREDECESSOR record (waiting on its OWN team) — the goal
    keeps its points; a manual DURATION_OVERRUN capture (also non-excusable)
    must not remove it either."""
    from ff.services.disruption import attribute

    e4_causes = [r["cause"] for r in attribute(excusal_snap) if r["task_id"] == E4]
    assert e4_causes == ["SAME_TEAM_PREDECESSOR"], "scenario precondition"

    asg = excusal_snap["schedule"].assignments[E4]
    excusal_snap["manual_excusals"] = [
        {
            "task_id": E4, "team": "T01", "day": asg.day, "shift": asg.shift,
            "cause": "DURATION_OVERRUN", "evidence": "ran long", "notes": "ran long",
            "source": "manual", "entered_by": "lead:T01",
        }
    ]
    try:
        report = shift_report(excusal_snap, "T01", 0, 1)
        pts = {tid: score_task(tid, excusal_snap)["total"] for tid in (E1, E2, E4)}
        assert report["goal"] == pts[E2] + pts[E4], (
            "non-excusable causes never shrink the goal denominator"
        )
        assert report["excused_points"] == pts[E1]
    finally:
        excusal_snap.pop("manual_excusals", None)


def test_manual_excusable_capture_excuses_via_snapshot(excusal_snap):
    """Lead capture path: a MANUAL record with an EXCUSABLE cause riding on
    ``snap['manual_excusals']`` excuses a planned task exactly like auto
    attribution (manual EXTENDS auto — merge law), and the excusal row is
    reported with source 'manual'."""
    asg = excusal_snap["schedule"].assignments[E2]
    excusal_snap["manual_excusals"] = [
        {
            "task_id": E2, "team": "T01", "day": asg.day, "shift": asg.shift,
            "cause": "CROSS_TEAM_PREDECESSOR",
            "evidence": "waiting on wing-join handoff", "source": "manual",
            "entered_by": "lead:T01",
        }
    ]
    try:
        report = shift_report(excusal_snap, "T01", 0, 1)
        pts = {tid: score_task(tid, excusal_snap)["total"] for tid in (E1, E2, E4)}
        assert report["goal"] == pts[E4], "auto (E1) AND manual (E2) excused"
        assert report["excused_points"] == pts[E1] + pts[E2]
        sources = {row["task_id"]: row["source"] for row in report["excusals"]}
        assert sources[E2] == "manual" and sources[E1] == "auto"
    finally:
        excusal_snap.pop("manual_excusals", None)


# ---------------------------------------------------------------------------
# GG-2 — leaderboard never ranks by raw points
# ---------------------------------------------------------------------------


def test_leaderboard_not_raw_points(game_snap):
    """GG-2: the leaderboard ranks by (attainment, efficiency), NEVER raw
    points. Small team T02 finished its whole (tiny) goal; big team T01 has
    a far larger raw goal but almost nothing earned — T02 must rank first,
    and raw point totals must not even appear in the rows."""
    t01_goal = sum(shift_report(game_snap, "T01", 0, s)["goal"] for s in (1, 2, 3))
    t02_goal = sum(shift_report(game_snap, "T02", 0, s)["goal"] for s in (1, 2, 3))
    assert t01_goal > t02_goal, "scenario requires the losing team to have more raw points"

    rows = leaderboard(game_snap, 0)
    assert [r["team"] for r in rows][:2] == ["T02", "T01"]
    assert rows[0]["rank"] == 1
    assert rows[0]["attainment"] == 1.0  # T02 finished everything it planned
    assert rows[1]["attainment"] < 1.0
    for row in rows:
        # Progression upgrade: difficulty index + PSY-5 needs-support framing
        # joined the row — but raw point totals STILL never leak (GG-2).
        assert set(row) == {
            "team", "attainment", "efficiency", "difficulty", "rank",
            "needs_support", "support",
        }, "GG-2: raw point totals must not leak into leaderboard rows"
        assert not ({"goal", "earned", "points", "raw_points"} & set(row))
    # Rank order follows attainment (then efficiency), monotonically —
    # difficulty only separates identical execution.
    pairs = [(r["attainment"], r["efficiency"]) for r in rows]
    assert pairs == sorted(pairs, reverse=True)


def test_leaderboard_deterministic(game_snap):
    """Identical snapshot => identical leaderboard (sorted iteration,
    tie-break by team id)."""
    assert leaderboard(game_snap, 0) == leaderboard(game_snap, 0)
