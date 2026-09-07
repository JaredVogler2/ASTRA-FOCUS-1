"""Validator tests — clean schedule passes; every injected V-class is caught.

Contract (ARCHITECTURE.md §ff/engine/validator.py): ``validate(schedule,
fleet)`` walks V1..V9 with zero tolerance. Strategy: a hand-built fleet and
a hand-built CLEAN schedule (0 violations), then one surgical mutation per
V-check on a fresh copy — the validator must catch each injected class.
"""

from __future__ import annotations

import config
from tests.conftest import mk_fleet, mk_mech, mk_task

from ff.domain import Assignment, Schedule
from ff.engine.validator import validate

T1, T2, T3, T4, T5, T6 = (f"0001-T{n:05d}" for n in range(1, 7))
M1, M2, M3 = "T01-S1-M001", "T01-S1-M002", "T01-S1-M003"
N1, N2 = "T01-S3-M001", "T01-S3-M002"  # shift-3 pool (for the OR-3 cases)
B1 = "T02-S1-M001"  # other team's mechanic (for the borrow injection)


def _fleet():
    mechanics = [
        mk_mech(M1), mk_mech(M2), mk_mech(M3),          # T01 shift 1, quota 2
        mk_mech(N1, shift=3), mk_mech(N2, shift=3),     # T01 shift 3, quota 1
        mk_mech(B1, team="T02"),
    ]
    tasks = [
        mk_task(T1, dur=100),
        mk_task(T2, dur=100, crew=2, preds=[T1]),
        mk_task(T3, dur=50, preds=[T2]),
        mk_task(T4, dur=300),
        mk_task(T5, dur=60, state="blocked", parts_eta=3),
        mk_task(T6, dur=300),
    ]
    return mk_fleet(tasks, mechanics)


def _asg(tid, day, shift, start, end, mechs, team="T01", skill="S"):
    return Assignment(
        task_id=tid,
        day=day,
        shift=shift,
        start_minute=start,
        end_minute=end,
        mechanic_ids=list(mechs),
        team=team,
        skill=skill,
        uses_overtime=end > config.SHIFT_EFFECTIVE[shift],
    )


def _clean_schedule() -> Schedule:
    """A hand-built schedule that satisfies every V1..V9 law."""
    assignments = {
        T1: _asg(T1, 0, 1, 0, 100, [M1]),
        T2: _asg(T2, 0, 1, 100, 200, [M1, M2]),  # back-to-back after T1 (legal)
        T3: _asg(T3, 1, 1, 0, 50, [M1]),
        T4: _asg(T4, 2, 1, 0, 300, [M1]),
        T5: _asg(T5, 3, 1, 0, 60, [M1]),          # parts_eta 3 honored
        T6: _asg(T6, 4, 1, 0, 300, [M2]),
    }
    return Schedule(assignments=assignments, unscheduled={}, stats={}, meta={})


def _mutated(mutator) -> dict:
    """Fresh copy of the clean schedule, mutated, then validated."""
    sched = Schedule.from_dict(_clean_schedule().to_dict())
    mutator(sched)
    return validate(sched, _fleet())


def _ids_for(report: dict, tid: str) -> set:
    return {v["id"] for v in report["violations"] if v["task_id"] == tid}


# ---------------------------------------------------------------------------
# clean baselines
# ---------------------------------------------------------------------------


def test_clean_hand_schedule_zero_violations():
    """The hand-built lawful schedule yields exactly zero violations."""
    report = validate(_clean_schedule(), _fleet())
    assert report["violations"] == []
    assert report["summary"]["total"] == 0
    assert report["checks"] == ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"]
    assert all(report["summary"][c] == 0 for c in report["checks"])


def test_clean_engine_schedule_zero_violations(fleet, schedule):
    """The scheduler's own output on the generated mini fleet is law-abiding:
    the engine and the validator agree on every V1..V9 rule."""
    report = validate(schedule, fleet)
    assert report["summary"]["total"] == 0, report["violations"][:5]


# ---------------------------------------------------------------------------
# injected violations — one per V-check
# ---------------------------------------------------------------------------


def test_v1_duration_identity():
    """V1: a padded booking (span != duration) is caught."""
    def mutate(s):
        s.assignments[T6].end_minute = 350  # task requires 300
    report = _mutated(mutate)
    assert "V1" in _ids_for(report, T6)
    assert report["summary"]["V1"] >= 1


def test_v2_same_slot_minute_rule():
    """V2: successor starting before its predecessor's end in the SAME slot."""
    def mutate(s):
        s.assignments[T3] = _asg(T3, 0, 1, 0, 50, [M2])  # T2 ends at 200 in d0/s1
    report = _mutated(mutate)
    assert "V2" in _ids_for(report, T3)


def test_v2_pred_after_successor():
    """V2: predecessor placed in a later slot than its successor."""
    def mutate(s):
        s.assignments[T2] = _asg(T2, 3, 1, 100, 200, [M2, M3])  # succ T3 is day 1
    report = _mutated(mutate)
    assert "V2" in _ids_for(report, T3)


def test_v3_weekend_day():
    """V3: shift 1 on a Saturday (day 5) breaks the working-day rule."""
    def mutate(s):
        s.assignments[T6] = _asg(T6, 5, 1, 0, 300, [M2])
    report = _mutated(mutate)
    assert "V3" in _ids_for(report, T6)


def test_v3_saturday_night_s3_illegal_sunday_night_legal():
    """OR-3 in the validator: Saturday-night S3 (day 5, Sunday follows) is a
    V3 violation; Sunday-night S3 (day 6, Monday follows) is clean —
    'the week's 3rd shift begins Sunday night'."""
    def saturday(s):
        s.assignments[T6] = _asg(T6, 5, 3, 0, 300, [N1])
    report = _mutated(saturday)
    assert "V3" in _ids_for(report, T6)

    def sunday(s):
        s.assignments[T6] = _asg(T6, 6, 3, 0, 300, [N1])
    report = _mutated(sunday)
    assert report["summary"]["total"] == 0, report["violations"]


def test_v4_shift_fit_and_buffer():
    """V4: start past the no-start buffer and end past SHIFT_MAX are caught."""
    def mutate(s):
        s.assignments[T6] = _asg(T6, 4, 1, 440, 740, [M2])  # limits: 430 / 520
    report = _mutated(mutate)
    assert "V4" in _ids_for(report, T6)
    assert report["summary"]["V4"] >= 2  # both the start and the end breach


def test_v4_dishonest_overtime_flag():
    """V4: uses_overtime must be True iff end > SHIFT_EFFECTIVE."""
    def mutate(s):
        s.assignments[T6].uses_overtime = True  # ends at 300 <= 460: a lie
    report = _mutated(mutate)
    assert "V4" in _ids_for(report, T6)


def test_v5_short_crew():
    """V5 / OR-1: a short crew (1 booked where 2 are required) is caught —
    'full crew or wait — no short bookings ever'."""
    def mutate(s):
        s.assignments[T2].mechanic_ids = [M1]
    report = _mutated(mutate)
    assert "V5" in _ids_for(report, T2)


def test_v5_cross_team_borrowing():
    """V5 / OR-2: a mechanic borrowed from another team is caught —
    'no borrowing across teams'."""
    def mutate(s):
        s.assignments[T6].mechanic_ids = [B1]  # B1 belongs to T02
    report = _mutated(mutate)
    assert "V5" in _ids_for(report, T6)


def test_v5_unknown_mechanic_and_duplicates():
    """V5: fictional roster ids and duplicated crew members are caught."""
    def ghost(s):
        s.assignments[T6].mechanic_ids = ["GHOST-S1-M001"]
    assert "V5" in _ids_for(_mutated(ghost), T6)

    def dupes(s):
        s.assignments[T2].mechanic_ids = [M1, M1]
    assert "V5" in _ids_for(_mutated(dupes), T2)


def test_v6_mechanic_double_booked():
    """V6: one mechanic in two places at once (overlap within a slot)."""
    def mutate(s):
        s.assignments[T3] = _asg(T3, 2, 1, 100, 150, [M1])  # T4 holds M1 0-300
    report = _mutated(mutate)
    assert "V6" in _ids_for(report, T3)
    # No other check class should fire for this surgical overlap.
    assert report["summary"]["total"] == report["summary"]["V6"]


def test_v7_load_cap():
    """V7: a mechanic's booked minutes past SHIFT_EFFECTIVE + OVERTIME are
    caught (600 > 520 here; the overlap needed to exceed the cap also
    triggers V6 — both must fire)."""
    def mutate(s):
        s.assignments[T6] = _asg(T6, 2, 1, 0, 300, [M1])  # M1 already has 300
    report = _mutated(mutate)
    assert "V7" in _ids_for(report, T6)
    assert report["summary"]["V7"] >= 1


def test_v8_activation_quota():
    """V8: a third distinct mechanic on (T01, shift 1, day 0) exceeds the
    floor(3 * 0.85) = 2 quota. The flagged task is whichever assignment
    first grows the distinct-activation set past the quota (walk order:
    start_minute, then task_id) — the class fires exactly once here."""
    def mutate(s):
        s.assignments[T6] = _asg(T6, 0, 1, 0, 300, [M3])  # day 0 already uses M1, M2
    report = _mutated(mutate)
    assert report["summary"]["V8"] == 1
    assert report["summary"]["total"] == report["summary"]["V8"]
    (violation,) = [v for v in report["violations"] if v["id"] == "V8"]
    assert violation["task_id"] in {T1, T2, T6}  # a day-0 (T01, s1) assignment


def test_v9_parts_eta_floor():
    """V9: a task with parts_eta_day=3 scheduled on day 2 is caught — no
    state flag exempts it."""
    def mutate(s):
        s.assignments[T5] = _asg(T5, 2, 1, 0, 60, [M2])
    report = _mutated(mutate)
    assert "V9" in _ids_for(report, T5)
    assert report["summary"]["total"] == report["summary"]["V9"]


def test_summary_is_consistent():
    """summary counts equal the emitted violations, per check and in total."""
    def mutate(s):
        s.assignments[T6].end_minute = 350          # V1
        s.assignments[T2].mechanic_ids = [M1]       # V5
    report = _mutated(mutate)
    assert report["summary"]["total"] == len(report["violations"])
    for check in report["checks"]:
        assert report["summary"][check] == sum(
            1 for v in report["violations"] if v["id"] == check
        )
