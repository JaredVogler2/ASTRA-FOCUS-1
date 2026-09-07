"""Scheduler tripwires — owner rules OR-1/OR-2/OR-3, quota, determinism.

These are regression tripwires for the hard behaviors of
ARCHITECTURE.md §ff/engine/scheduler.py. Each owner-rule test quotes the
rule it defends in its docstring; if one of these fails, an owner rule has
been regressed — do not weaken the test to make it pass.
"""

from __future__ import annotations

from tests.conftest import mk_fleet, mk_mech, mk_task, run_schedule

import config
from ff.domain import shift_eligible, slot_index
from ff.engine.scheduler import (
    R_CREW_POOL,
    R_HORIZON,
    R_NO_SKILL,
    UNSCHEDULED_REASONS,
    build_schedule,
)

A_ID = "0001-T00001"
B_ID = "0001-T00002"
C_ID = "0001-T00003"


# ---------------------------------------------------------------------------
# OR-1 — full crew or wait
# ---------------------------------------------------------------------------


def test_full_crew_or_wait_tripwire():
    """Owner rule OR-1: full crew or wait — no short bookings ever.

    A 2-crew task whose skill pool has only 1 mechanic free early must be
    placed LATER with 2 distinct named mechanics (or reason-coded
    unscheduled) — NEVER placed with 1 mechanic.
    """
    mechanics = [
        mk_mech("T01-S1-M001"),
        mk_mech("T01-S1-M002"),
        mk_mech("T01-S1-M003", skills=["OTHER"]),  # pool padding: quota floor(3*.85)=2
    ]
    tasks = [
        mk_task(A_ID, dur=400, crew=1),  # placed first (lower id, equal priority)
        mk_task(B_ID, dur=400, crew=2),  # needs BOTH skill-S mechanics at once
    ]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))

    a = sched.assignments[A_ID]
    assert a.day == 0 and len(a.mechanic_ids) == 1

    # B must wait for a slot where a FULL crew of 2 is simultaneously free.
    assert B_ID in sched.assignments or B_ID in sched.unscheduled
    if B_ID in sched.assignments:
        b = sched.assignments[B_ID]
        assert len(b.mechanic_ids) == 2, "OR-1 broken: short crew booked"
        assert len(set(b.mechanic_ids)) == 2, "OR-1 broken: duplicated mechanic"
        assert slot_index(b.day, b.shift) > slot_index(a.day, a.shift), (
            "B cannot share day-0 shift 1: only one qualified mechanic was "
            "free for its whole duration there"
        )
        assert set(b.mechanic_ids) == {"T01-S1-M001", "T01-S1-M002"}


def test_full_crew_never_short_globally(fleet, schedule):
    """Owner rule OR-1: full crew or wait — no short bookings ever.

    Zero tolerance across the whole generated mini-fleet schedule: every
    assignment carries exactly mechanics_required DISTINCT mechanic ids.
    """
    by_id = {t.task_id: t for t in fleet.tasks}
    assert schedule.assignments, "mini fleet produced an empty schedule"
    for tid, asg in schedule.assignments.items():
        need = by_id[tid].mechanics_required
        assert len(asg.mechanic_ids) == need, f"{tid}: short/padded crew"
        assert len(set(asg.mechanic_ids)) == need, f"{tid}: duplicate crew ids"


# ---------------------------------------------------------------------------
# OR-2 — no cross-team borrowing
# ---------------------------------------------------------------------------


def test_no_borrow_tripwire():
    """Owner rule OR-2: no borrowing across teams — ever.

    A T01 task whose skill exists ONLY on T02's roster must be reason-coded
    unscheduled (no_skill_holder); T02's mechanic must never appear on it.
    """
    mechanics = [
        mk_mech("T01-S1-M001", team="T01", skills=["X"]),
        mk_mech("T01-S1-M002", team="T01", skills=["X"]),
        mk_mech("T02-S1-M001", team="T02", skills=["Y"]),
        mk_mech("T02-S1-M002", team="T02", skills=["Y"]),
    ]
    tasks = [
        mk_task(A_ID, team="T01", skill="Y", dur=60),  # only T02 holds Y
        mk_task("0001-T00002", team="T02", skill="Y", dur=60),
    ]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))

    assert A_ID not in sched.assignments, "OR-2 broken: crew borrowed across teams"
    assert sched.unscheduled[A_ID] == R_NO_SKILL
    own = sched.assignments["0001-T00002"]
    assert own.mechanic_ids == ["T02-S1-M001"]
    assert all(m.startswith("T02-") for m in own.mechanic_ids)


def test_no_borrow_globally(fleet, schedule):
    """Owner rule OR-2: no borrowing across teams — ever (whole mini fleet).

    Every mechanic on every assignment belongs to the task's own team.
    """
    mech_team = {m.mech_id: m.team for m in fleet.mechanics}
    task_team = {t.task_id: t.team for t in fleet.tasks}
    for tid, asg in schedule.assignments.items():
        assert asg.team == task_team[tid]
        for mid in asg.mechanic_ids:
            assert mid in mech_team, f"{tid}: fictional mechanic {mid}"
            assert mech_team[mid] == task_team[tid], (
                f"{tid}: mechanic {mid} borrowed from {mech_team[mid]}"
            )


# ---------------------------------------------------------------------------
# OR-3 — Sunday-night third shift
# ---------------------------------------------------------------------------


def test_sunday_night_s3_tripwire():
    """Owner rule OR-3: the week's 3rd shift begins Sunday night
    (WEEK_STARTS_SUNDAY_NIGHT) — shift 3 is plannable on a non-working day
    d iff d+1 is a working day.

    Hand-built fleet whose only mechanics work shift 3, with work released
    on Saturday (day 5): Saturday-night S3 (Sunday follows) must be
    skipped, and the task must land on SUNDAY night (day 6, shift 3) —
    the first eligible slot before Monday.
    """
    # Calendar law sanity (day 0 = Monday; days 5/6 = Sat/Sun).
    assert shift_eligible(5, 3) is False, "Saturday-night S3 must not be plannable"
    assert shift_eligible(6, 3) is True, "Sunday-night S3 must be plannable (OR-3)"
    assert shift_eligible(6, 1) is False and shift_eligible(6, 2) is False

    mechanics = [mk_mech("T01-S3-M001", shift=3), mk_mech("T01-S3-M002", shift=3)]
    tasks = [mk_task(A_ID, dur=100, earliest=5)]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))

    asg = sched.assignments[A_ID]
    assert (asg.day, asg.shift) == (6, 3), (
        f"expected the Sunday-night S3 slot (day 6), got day {asg.day} "
        f"shift {asg.shift}"
    )
    assert asg.day % 7 == 6  # a Sunday — the non-working day before Monday


# ---------------------------------------------------------------------------
# activation quota
# ---------------------------------------------------------------------------


def test_activation_quota():
    """At most floor(pool_size * UTILIZATION) DISTINCT mechanics per
    (team, shift, day): with 4 mechanics (quota 3) and 4 shift-filling
    tasks, exactly 3 run on day 0 and the 4th is delayed to day 1."""
    assert config.UTILIZATION == 0.85, "test math assumes the default quota"
    mechanics = [mk_mech(f"T01-S1-M{i:03d}") for i in range(1, 5)]  # quota floor(3.4)=3
    tasks = [mk_task(f"0001-T{n:05d}", dur=460, crew=1) for n in range(1, 5)]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))

    per_day: dict[int, set] = {}
    for asg in sched.assignments.values():
        per_day.setdefault(asg.day, set()).update(asg.mechanic_ids)
    assert len(sched.assignments) == 4
    assert len(per_day[0]) == 3, "quota should allow exactly 3 distinct on day 0"
    late = sched.assignments["0001-T00004"]
    assert late.day >= 1, "4th task must be delayed by the activation quota"


# ---------------------------------------------------------------------------
# precedence, live state, reason codes
# ---------------------------------------------------------------------------


def test_precedence_honored_globally(fleet, schedule):
    """pred.end (slot, minute) <= succ.start; same slot requires
    succ.start_minute >= pred.end_minute; a live (non-done) unplaced pred
    forbids placing the successor at all."""
    by_id = {t.task_id: t for t in fleet.tasks}
    for tid, asg in schedule.assignments.items():
        succ_slot = slot_index(asg.day, asg.shift)
        for pred_id in set(by_id[tid].predecessors):
            pred = by_id.get(pred_id)
            if pred is None or pred.state == "done":
                continue
            assert pred_id not in schedule.unscheduled, (
                f"{tid} scheduled above unplaced predecessor {pred_id}"
            )
            p = schedule.assignments[pred_id]
            pred_slot = slot_index(p.day, p.shift)
            assert pred_slot <= succ_slot, f"{tid} starts before pred {pred_id}"
            if pred_slot == succ_slot:
                assert asg.start_minute >= p.end_minute, (
                    f"{tid} same-slot minute rule broken vs {pred_id}"
                )


def test_slots_eligible_and_windows_fit_globally(fleet, schedule):
    """Every placement sits on an eligible slot (working day / OR-3) with a
    window inside [0, SHIFT_MAX], start within the no-start buffer, and an
    honest uses_overtime flag."""
    for tid, asg in schedule.assignments.items():
        assert shift_eligible(asg.day, asg.shift), f"{tid} on ineligible slot"
        eff = config.SHIFT_EFFECTIVE[asg.shift]
        assert 0 <= asg.start_minute <= eff - config.NO_START_BUFFER, tid
        assert asg.end_minute <= config.SHIFT_MAX[asg.shift], tid
        assert asg.end_minute > asg.start_minute, tid
        assert asg.uses_overtime == (asg.end_minute > eff), tid


def test_done_excluded_and_satisfies_precedence():
    """state=='done' tasks are excluded from the schedule entirely and count
    as satisfied predecessors (behavior 6)."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]
    tasks = [
        mk_task(A_ID, dur=100, state="done"),
        mk_task(B_ID, dur=100, preds=[A_ID]),
    ]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))
    assert A_ID not in sched.assignments
    assert A_ID not in sched.unscheduled
    assert sched.assignments[B_ID].day == 0  # done pred gates nothing


def test_in_progress_pinned_committed_first():
    """Committed-first dispatch: in_progress work is pinned to start_day
    with remaining_minutes BEFORE any new work — a bigger new task cannot
    take the incumbent's slot.

    (Pool of 2 — quota floor(2*0.85)=1 — with a single skill-S holder, so
    both tasks compete for the SAME mechanic.)"""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002", skills=["OTHER"])]
    tasks = [
        # earliest_day is ignored for work physically underway.
        mk_task(A_ID, dur=400, earliest=4, state="in_progress", remaining=100),
        mk_task(B_ID, dur=460, crew=1),  # higher cpm priority than remaining=100
    ]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))
    a = sched.assignments[A_ID]
    assert a.day == 0, "in_progress work must be pinned to start_day"
    assert a.end_minute - a.start_minute == 100, "must book remaining_minutes"
    b = sched.assignments[B_ID]
    assert slot_index(b.day, b.shift) > slot_index(a.day, a.shift) or (
        b.day == a.day and b.shift == a.shift and b.start_minute >= a.end_minute
    )


def test_blocked_waits_for_parts_eta():
    """A blocked task floors its slot scan at parts_eta_day (V9 upstream)."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]
    tasks = [mk_task(A_ID, dur=60, state="blocked", parts_eta=3)]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))
    assert sched.assignments[A_ID].day >= 3


def test_reason_codes():
    """Unschedulable work is reason-coded — NO fictional placements ever.
    Static holes: no_skill_holder / crew_exceeds_pool (cascading to
    successors); dynamic exhaustion: no_crew_within_horizon."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]  # quota 1
    tasks = [
        mk_task(A_ID, skill="Z", dur=60),              # nobody holds Z
        mk_task(B_ID, crew=3, dur=60),                  # crew 3 > pool 2
        mk_task(C_ID, crew=1, dur=60, preds=[B_ID]),    # below an unplaceable pred
    ]
    _, sched = run_schedule(mk_fleet(tasks, mechanics))
    assert sched.assignments == {}
    assert sched.unscheduled[A_ID] == R_NO_SKILL
    assert sched.unscheduled[B_ID] == R_CREW_POOL
    assert sched.unscheduled[C_ID] == R_CREW_POOL, "reason must cascade to successors"

    # Horizon exhaustion: two shift-filling tasks, one-day horizon, and an
    # activation quota of 1 — the second task cannot run on day 0.
    tasks2 = [mk_task(A_ID, dur=400), mk_task(B_ID, dur=400)]
    _, sched2 = run_schedule(mk_fleet(tasks2, mechanics), horizon_days=1)
    assert sched2.assignments[A_ID].day == 0
    assert sched2.unscheduled[B_ID] == R_HORIZON


def test_unscheduled_reasons_are_contract_codes(schedule):
    """Only the three contract reason codes ever appear."""
    assert set(schedule.unscheduled.values()) <= set(UNSCHEDULED_REASONS)


# ---------------------------------------------------------------------------
# determinism + stats contract
# ---------------------------------------------------------------------------


def test_determinism_two_runs_identical(fleet, cpm):
    """Behavior 7: identical fleet+args => identical schedule. Two fresh
    build_schedule runs agree on every field except the measured
    wall_seconds."""
    s1 = build_schedule(fleet, cpm)
    s2 = build_schedule(fleet, cpm)
    d1, d2 = s1.to_dict(), s2.to_dict()
    d1["stats"].pop("wall_seconds")
    d2["stats"].pop("wall_seconds")
    assert d1 == d2


def test_stats_contract_keys(fleet, schedule):
    """Schedule.stats carries every contract-mandated key and consistent
    counts; per-aircraft rows carry the mandated fields."""
    stats = schedule.stats
    for key in (
        "total_tasks",
        "scheduled",
        "unscheduled",
        "fleet_lateness_days",
        "otd_count",
        "aircraft",
        "makespan_day",
        "wall_seconds",
        "economics",
        "capacity_pressure",
    ):
        assert key in stats, f"stats missing {key!r}"
    assert stats["total_tasks"] == len(fleet.tasks)
    assert stats["scheduled"] == len(schedule.assignments)
    assert stats["unscheduled"] == len(schedule.unscheduled)
    done = sum(1 for t in fleet.tasks if t.state == "done")
    assert stats["scheduled"] + stats["unscheduled"] + done == stats["total_tasks"]
    assert len(stats["aircraft"]) == len(fleet.aircraft)
    for row in stats["aircraft"]:
        for key in ("aircraft", "completion_day", "deadline_day", "lateness_days", "on_time"):
            assert key in row
        assert row["on_time"] == (row["lateness_days"] == 0)
    assert schedule.meta.get("mock_data") is True  # OR-5 propagates to output
