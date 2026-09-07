"""Digital-week simulator tripwires — clock chronology, TRUE rework
injection, determinism, stability sanity.

Doctrine under test (`MAX/tools/cadence_sim2.py` adapted per FOCU5 D5):
execute the ended shift -> inject rework as REAL Task rows that join the
DAG -> roll the factory clock across the weekend seam -> replan with the
real engine -> measure. All fixtures are synthetic (mock_data: true).
"""

from __future__ import annotations

import json

import pytest

from ff.domain import shift_eligible
from ff.engine.validator import validate
from ff.sim.digital_week import (
    REWORK_DURATIONS,
    REWORK_ID_SUFFIX,
    SHIFT_CYCLE,
    advance_clock,
    run_digital_week,
)


@pytest.fixture(scope="module")
def sim3(fleet):
    """Three rounds on the mini fleet, with the final mutated state."""
    return run_digital_week(fleet, rounds=3, seed=7, return_state=True)


def _strip_wall(result: dict) -> dict:
    """JSON round-trip copy with the measured wall fields removed.

    (The round-trip doubles as the JSON-safety check: every simulator
    output must be serializable for the CLI --out path.)
    """
    copy = json.loads(json.dumps(result))
    copy.pop("wall_seconds_total")
    copy["baseline"].pop("wall_s")
    for row in copy["rounds"]:
        row.pop("wall_s")
    return copy


# ---------------------------------------------------------------------------
# clock chronology (the tripwire — calendar bugs live at the seams)
# ---------------------------------------------------------------------------


def test_clock_advances_3_1_2_with_weekend_seam():
    """OR-3 (quoted): "the week's 3rd shift begins Sunday night
    (WEEK_STARTS_SUNDAY_NIGHT)" — shift 3 on a non-working day d is
    plannable iff d+1 is a working day.

    Factory chronology is SHIFT_CYCLE = [3, 1, 2]: the work day's 3rd
    shift runs OVERNIGHT before its 1st. In FF_app slot labels the
    overnight shift of day d is (d, 3), so from Sunday night the clock
    must run 3 -> 1 -> 2 -> 3 -> 1 -> 2 ..., and the weekend seam must
    jump Friday-night S3 straight to Sunday-night S3 (Saturday-night S3 —
    Sunday follows — is NOT plannable)."""
    assert SHIFT_CYCLE == [3, 1, 2]

    # Sunday night (day 6, S3) -> Monday S1 -> S2 -> Monday night S3 -> ...
    day, shift = 6, 3
    seen = []
    for _ in range(6):
        day, shift, _rolled = advance_clock(day, shift)
        seen.append((day, shift))
    assert seen == [(7, 1), (7, 2), (7, 3), (8, 1), (8, 2), (8, 3)]

    # The weekend seam: Friday-night S3 advances straight to Sunday-night
    # S3; Saturday and Saturday-night are skipped entirely.
    assert advance_clock(4, 3) == (6, 3, True)
    assert advance_clock(6, 3) == (7, 1, True)
    assert shift_eligible(6, 3) is True, "Sunday-night S3 must be plannable (OR-3)"
    assert shift_eligible(5, 3) is False, (
        "Saturday-night S3 (Sunday follows) must NOT be plannable (OR-3)"
    )
    # Within a working day the cycle is contiguous.
    assert advance_clock(4, 1) == (4, 2, False)
    assert advance_clock(4, 2) == (4, 3, False)


def test_run_executes_slots_across_the_weekend_seam(fleet):
    """A run started Friday must execute (4,1) (4,2) (4,3=Fri night) then
    (6,3=Sun night) — never a Saturday slot (OR-3 weekend seam)."""
    result = run_digital_week(fleet, rounds=4, seed=7, start_day=4)
    slots = [(r["exec_day"], r["exec_shift"]) for r in result["rounds"]]
    assert slots == [(4, 1), (4, 2), (4, 3), (6, 3)]
    # The round that advanced off Friday night rolled the day.
    assert result["rounds"][2]["day_rolled"] is True
    assert (result["rounds"][2]["clock_day"], result["rounds"][2]["clock_shift"]) == (6, 3)


def test_bad_start_slot_and_rounds_raise(fleet):
    """The clock may only start on an eligible slot; rounds must be >= 1."""
    with pytest.raises(ValueError):
        run_digital_week(fleet, rounds=1, start_day=5, start_shift=1)  # Saturday
    with pytest.raises(ValueError):
        run_digital_week(fleet, rounds=0)


# ---------------------------------------------------------------------------
# rework joins the DAG — the production way, no proxies
# ---------------------------------------------------------------------------


def test_rework_rows_join_the_dag_validator_clean(fleet, sim3):
    """Injected rework must be REAL Task rows appended to the fleet's task
    list — single predecessor = a sampled not-started parent, same
    team/skill — and the post-injection replan must still be
    validator-clean (V1..V9 = 0 violations)."""
    result, state = sim3
    work, schedule = state["fleet"], state["schedule"]
    by_id = {t.task_id: t for t in work.tasks}

    rework = [t for t in work.tasks if t.task_id.endswith(REWORK_ID_SUFFIX)]
    assert len(rework) == sum(r["injected"] for r in result["rounds"]) > 0
    for task in rework:
        assert task.is_rework is True
        assert len(task.predecessors) == 1, "rework has exactly one predecessor"
        parent = by_id.get(task.predecessors[0])
        assert parent is not None, "the parent must be a real task in the fleet"
        assert not parent.task_id.endswith(REWORK_ID_SUFFIX), (
            "rework never parents more rework"
        )
        # Owner ruling (parent-SOI ownership): origin is ALWAYS the
        # parent's team; the FIX is either the parent's own trade
        # (same team + skill) or a cross-trade fix (other team, "ANY").
        assert task.rework_origin_team == parent.team
        if task.team == parent.team:
            assert task.skill == parent.skill
        else:
            assert task.skill == "ANY"
        assert task.aircraft == parent.aircraft
        assert task.duration_minutes in REWORK_DURATIONS
        assert 1 <= task.mechanics_required <= parent.mechanics_required

    # The DAG grew like a real MES extract and the engine placed the rows.
    placed = [t for t in rework if t.task_id in schedule.assignments]
    assert placed, "at least some rework must be schedulable (it joined the DAG)"
    # THE tripwire: the post-injection replan is validator-clean.
    report = validate(schedule, work)
    assert report["summary"]["total"] == 0, report["violations"][:5]
    # The original fleet object was never mutated (isolated work copy).
    assert not any(t.task_id.endswith(REWORK_ID_SUFFIX) for t in fleet.tasks)
    assert all(t.state == "not_started" for t in fleet.tasks)


# ---------------------------------------------------------------------------
# execution semantics + determinism + metric sanity
# ---------------------------------------------------------------------------


def test_execution_slide_rule(fleet):
    """exec_rate=0 slides EVERY planned task to in_progress with
    remaining_minutes = max(5, int(duration * slide_remain)); exec_rate=1
    completes every planned task (slid == 0)."""
    result, state = run_digital_week(
        fleet, rounds=1, seed=7, exec_rate=0.0, return_state=True
    )
    row = result["rounds"][0]
    assert row["executed"] == 0 and row["slid"] > 0
    slid = [
        t
        for t in state["fleet"].tasks
        if t.state == "in_progress"
    ]
    assert len(slid) == row["slid"]
    for task in slid:
        assert task.remaining_minutes == max(5, int(task.duration_minutes * 0.5))

    result_full = run_digital_week(fleet, rounds=1, seed=7, exec_rate=1.0)
    assert result_full["rounds"][0]["slid"] == 0
    assert result_full["rounds"][0]["executed"] > 0


def test_determinism_two_runs_identical(fleet):
    """Same (fleet, args) => identical results; the measured wall fields
    are the ONLY non-deterministic output (seeded Random, sorted iteration)."""
    a = run_digital_week(fleet, rounds=2, seed=11)
    b = run_digital_week(fleet, rounds=2, seed=11)
    assert _strip_wall(a) == _strip_wall(b)
    # A different seed must be allowed to diverge (the coins are real).
    c = run_digital_week(fleet, rounds=2, seed=12)
    assert c["seed"] != a["seed"]


def test_stability_metrics_sane_and_labeled(fleet, sim3):
    """Stability percentages live in 0..100 over surviving unexecuted
    tasks; counters are consistent; every output is stamped mock_data
    from the fleet meta (OR-5)."""
    result, _state = sim3
    assert result["mock_data"] is True
    assert result["baseline"]["total_tasks"] == len(fleet.tasks)
    n_aircraft = len(fleet.aircraft)
    total_prev = result["baseline"]["total_tasks"]
    for row in result["rounds"]:
        assert 0.0 <= row["slot_stable_pct"] <= 100.0
        assert 0.0 <= row["mech_stable_pct"] <= 100.0
        assert row["surviving"] >= 0
        assert row["executed"] >= 0 and row["slid"] >= 0
        assert row["fleet_lateness"] >= 0
        assert 0 <= row["otd"] <= n_aircraft
        # the DAG grows by exactly the injected rework rows
        assert row["total_tasks"] == total_prev + row["injected"]
        total_prev = row["total_tasks"]
        assert row["scheduled"] + row["unscheduled"] <= row["total_tasks"]
