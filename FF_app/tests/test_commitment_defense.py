"""INCREMENT 5 tripwires — scheduler-side commitment defense (config §12).

Owner rule defended here, quoted verbatim (OR-4, FOCU5 foundation):

    "OR-4 Committed work dispatches before new work. Inside the 3-day
    commitment horizon, incumbent slots and mechanics are defended;
    committed dates move only through the hysteresis state machine."

MAX mechanism rules replicated (GROUNDING §1, config §10 commitment layer),
quoted verbatim where a test enforces them:

- "committed work dispatches before new work" (committed-first heap key);
- "crew continuity must never cost slot continuity" (the two-attempt rule);
- "precedence is physics; stickiness is not" (the precedence/parts floor
  always beats the incumbent slot).

If one of these fails, OR-4 has been regressed — do not weaken the test to
make it pass. ``incumbent=None`` must remain BYTE-IDENTICAL to the
pre-commitment engine, and the V1..V9 validator is never weakened.
"""

from __future__ import annotations

from tests.conftest import mk_fleet, mk_mech, mk_task, run_schedule

import config
from ff.domain import Assignment, Schedule
from ff.engine.scheduler import (
    R_HORIZON,
    build_schedule,
    incumbent_from_schedule,
)
from ff.engine.validator import validate

A_ID = "0001-T00001"
B_ID = "0001-T00002"


def _inc(task_id, day, shift=1, start=0, mechs=()):
    """Terse incumbent-map entry (the incumbent_from_schedule row shape)."""
    return {
        task_id: {
            "day": day,
            "shift": shift,
            "start_minute": start,
            "mechanic_ids": list(mechs),
        }
    }


def _strip_wall(schedule: Schedule) -> dict:
    d = schedule.to_dict()
    d["stats"]["wall_seconds"] = 0.0
    return d


# ---------------------------------------------------------------------------
# slot defense — in-horizon incumbents hold their slot
# ---------------------------------------------------------------------------


def test_in_horizon_task_keeps_incumbent_slot_even_when_earlier_slot_free():
    """OR-4: "Inside the 3-day commitment horizon, incumbent slots and
    mechanics are defended."

    A task whose incumbent slot is day 1 (inside COMMITMENT_HORIZON_DAYS=3
    of start_day 0) must KEEP day 1 even though day 0 is completely free —
    the incumbent slot is tried FIRST and wins. Without the incumbent the
    same task left-packs to day 0 (the A/B control).
    """
    assert config.COMMITMENT_ENABLED and config.COMMITMENT_HORIZON_DAYS == 3
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]
    tasks = [mk_task(A_ID, dur=100)]
    fleet = mk_fleet(tasks, mechanics)

    cpm, free = run_schedule(fleet)
    assert (free.assignments[A_ID].day, free.assignments[A_ID].shift) == (0, 1)
    assert free.assignments[A_ID].kept_incumbent is False

    sched = build_schedule(
        fleet, cpm, incumbent=_inc(A_ID, day=1, mechs=["T01-S1-M001"])
    )
    asg = sched.assignments[A_ID]
    assert (asg.day, asg.shift) == (1, 1), "OR-4 broken: incumbent slot lost"
    assert asg.kept_incumbent is True
    assert sched.stats["commitment_in_horizon"] == 1
    assert sched.stats["commitment_kept"] == 1
    assert sched.stats["commitment_mech_kept"] == 1


def test_beyond_horizon_incumbent_repacks_left():
    """OR-4 defends ONLY "inside the 3-day commitment horizon": an
    incumbent at start_day + COMMITMENT_HORIZON_DAYS (day 3) is beyond the
    band and gets a free repack — left-pull preserved, kept_incumbent
    False, no commitment counters. The boundary day 2 (= horizon - 1) is
    still defended."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]
    fleet = mk_fleet([mk_task(A_ID, dur=100)], mechanics)
    cpm, _ = run_schedule(fleet)

    beyond = build_schedule(
        fleet, cpm, incumbent=_inc(A_ID, day=config.COMMITMENT_HORIZON_DAYS)
    )
    asg = beyond.assignments[A_ID]
    assert (asg.day, asg.shift) == (0, 1), "beyond-horizon work must repack left"
    assert asg.kept_incumbent is False
    assert beyond.stats["commitment_in_horizon"] == 0
    assert beyond.stats["commitment_kept"] == 0

    edge = build_schedule(
        fleet, cpm, incumbent=_inc(A_ID, day=config.COMMITMENT_HORIZON_DAYS - 1)
    )
    assert edge.assignments[A_ID].day == config.COMMITMENT_HORIZON_DAYS - 1
    assert edge.assignments[A_ID].kept_incumbent is True


def test_infeasible_incumbent_slides_with_predecessor_floor_wins():
    """MAX rule (verbatim): "precedence is physics; stickiness is not."

    The incumbent (day, shift) is tried at or after the precedence/parts
    floor — the floor ALWAYS wins. A predecessor now blocked until parts
    ETA day 2 drags the successor's floor past its day-1 incumbent slot:
    the successor slides WITH the predecessor instead of keeping a slot
    that would break precedence (V2/V9 upstream)."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")]
    tasks = [
        mk_task(A_ID, dur=60, state="blocked", parts_eta=2),
        mk_task(B_ID, dur=60, preds=[A_ID]),
    ]
    fleet = mk_fleet(tasks, mechanics)
    cpm, _ = run_schedule(fleet)

    sched = build_schedule(
        fleet, cpm, incumbent=_inc(B_ID, day=1, mechs=["T01-S1-M001"])
    )
    pred = sched.assignments[A_ID]
    succ = sched.assignments[B_ID]
    assert pred.day >= 2, "blocked pred floors at parts_eta_day"
    assert succ.day >= pred.day, "floor wins: successor slides with pred"
    assert succ.day != 1 and succ.kept_incumbent is False
    # Offered defense (in-horizon incumbent exists) but the slot could not
    # be kept without breaking physics.
    assert sched.stats["commitment_in_horizon"] == 1
    assert sched.stats["commitment_kept"] == 0


# ---------------------------------------------------------------------------
# mechanic preference + the two-attempt rule
# ---------------------------------------------------------------------------


def test_incumbent_mechanic_preferred_over_equally_free_peer():
    """OR-4: "incumbent slots and mechanics are defended" — crew selection
    prefers incumbent mechanic_ids when qualified and free (sort key:
    incumbent members first, then earliest-free, then id).

    M001 and M002 are equally free; free choice ties by id and picks M001,
    but with M002 as the incumbent crew the task must keep M002."""
    mechanics = [
        mk_mech("T01-S1-M001"),
        mk_mech("T01-S1-M002"),
        mk_mech("T01-S1-M003", skills=["OTHER"]),  # pool padding: quota 2
    ]
    fleet = mk_fleet([mk_task(A_ID, dur=100)], mechanics)
    cpm, free = run_schedule(fleet)
    assert free.assignments[A_ID].mechanic_ids == ["T01-S1-M001"]

    sched = build_schedule(
        fleet, cpm, incumbent=_inc(A_ID, day=0, mechs=["T01-S1-M002"])
    )
    asg = sched.assignments[A_ID]
    assert asg.mechanic_ids == ["T01-S1-M002"], "incumbent mechanic not preferred"
    assert (asg.day, asg.shift) == (0, 1) and asg.kept_incumbent is True
    assert sched.stats["commitment_mech_kept"] == 1


def test_crew_continuity_never_costs_slot_continuity_two_attempt():
    """MAX rule (verbatim): "crew continuity must never cost slot
    continuity" — the two-attempt rule.

    Geometry (all in day-0 shift 1, effective 460 / max 520 / no-start
    430; VOID-EDGE era: pinned in_progress work books earliest-free with
    no predecessor floors, so ledgers are sculpted with per-mechanic
    pinned CHAINS — task_id order fixes the within-mechanic sequence):

    - M001 (incumbent): pinned [0,220] + [220,460] -> its free window is
      60 minutes starting at 460 (> 430 no-start): every crew containing
      M001 FAILS the slot for the 120-minute committed job (the preferred
      attempt dies).
    - M002 / M003: pinned [0,300] + [300,400] -> free [400,520] = exactly
      120 minutes, start 400 <= 430: the free-choice retry fits.

    The engine must retry the SAME slot free-choice and keep the incumbent
    SLOT with a non-incumbent crew, sacrificing crew continuity, never the
    slot."""
    mechanics = [
        mk_mech("T01-S1-M001", skills=["S", "PA"]),
        mk_mech("T01-S1-M002", skills=["S", "HB"]),
        mk_mech("T01-S1-M003", skills=["S", "HC"]),
        mk_mech("T01-S1-M004", skills=["GD"]),
        mk_mech("T01-S1-M005", skills=["OTHER"]),  # pool 5 -> quota 4
    ]
    tasks = [
        # Pinned in_progress bookings sculpt the ledgers BEFORE dispatch
        # (committed-first pinning precedes the ready heap). No preds —
        # positioning comes purely from per-mechanic booking order.
        mk_task("0001-T00010", skill="PA", dur=220, state="in_progress",
                remaining=220),                       # M001 [0, 220]
        mk_task("0001-T00011", skill="GD", dur=300, state="in_progress",
                remaining=300),                       # M004 [0, 300]
        mk_task("0001-T00012", skill="HB", dur=300, state="in_progress",
                remaining=300),                       # M002 [0, 300]
        mk_task("0001-T00013", skill="HC", dur=300, state="in_progress",
                remaining=300),                       # M003 [0, 300]
        mk_task("0001-T00014", skill="PA", dur=240, state="in_progress",
                remaining=240),                       # M001 [220, 460]
        mk_task("0001-T00015", skill="HB", dur=100, state="in_progress",
                remaining=100),                       # M002 [300, 400]
        mk_task("0001-T00016", skill="HC", dur=100, state="in_progress",
                remaining=100),                       # M003 [300, 400]
        mk_task("0001-T00020", skill="S", dur=120, crew=2),  # the committed task
    ]
    fleet = mk_fleet(tasks, mechanics)
    cpm, _ = run_schedule(fleet)

    sched = build_schedule(
        fleet, cpm, incumbent=_inc("0001-T00020", day=0, mechs=["T01-S1-M001"])
    )
    # Ledger sanity: the sculpted bookings landed where the geometry needs.
    assert sched.assignments["0001-T00010"].start_minute == 0
    assert sched.assignments["0001-T00010"].end_minute == 220
    assert sched.assignments["0001-T00014"].start_minute == 220
    assert sched.assignments["0001-T00014"].end_minute == 460
    assert sched.assignments["0001-T00015"].start_minute == 300
    assert sched.assignments["0001-T00016"].start_minute == 300

    asg = sched.assignments["0001-T00020"]
    assert (asg.day, asg.shift) == (0, 1), (
        "two-attempt rule broken: preferring the incumbent crew cost the "
        "incumbent SLOT (crew continuity must never cost slot continuity)"
    )
    assert asg.start_minute == 400 and asg.kept_incumbent is True
    assert "T01-S1-M001" not in asg.mechanic_ids, (
        "geometry broke: the incumbent mechanic fit after all, so the "
        "two-attempt fallback was not exercised"
    )
    assert set(asg.mechanic_ids) == {"T01-S1-M002", "T01-S1-M003"}
    assert sched.stats["commitment_kept"] == 1
    assert sched.stats["commitment_mech_kept"] == 0  # crew continuity given up


# ---------------------------------------------------------------------------
# committed-first dispatch
# ---------------------------------------------------------------------------


def test_committed_first_beats_higher_priority_new_task_for_last_seat():
    """OR-4 / MAX rule (verbatim): "committed work dispatches before new
    work."

    One seat exists (single skill holder, quota 1, 1-day horizon) and two
    shift-filling tasks want it. The NEW task has the more urgent deadline
    (lower slack — it wins the pre-commitment ascending-slack key), but the
    committed task holds an in-horizon incumbent and must dispatch FIRST,
    taking the seat; the new task is honestly reason-coded. The control arm
    (no incumbent) shows the opposite outcome."""
    mechanics = [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002", skills=["OTHER"])]
    committed_id, new_id = A_ID, B_ID
    tasks = [
        mk_task(committed_id, dur=460, deadline=30),
        mk_task(new_id, dur=460, deadline=1),  # more urgent => lower slack
    ]
    fleet = mk_fleet(tasks, mechanics)
    cpm, _ = run_schedule(fleet)

    control = build_schedule(fleet, cpm, horizon_days=1)
    assert new_id in control.assignments, "control: urgent NEW task wins the seat"
    assert control.unscheduled[committed_id] == R_HORIZON

    sched = build_schedule(
        fleet, cpm, horizon_days=1,
        incumbent=_inc(committed_id, day=0, mechs=["T01-S1-M001"]),
    )
    asg = sched.assignments[committed_id]
    assert (asg.day, asg.shift) == (0, 1) and asg.kept_incumbent is True
    assert sched.unscheduled[new_id] == R_HORIZON, (
        "OR-4 broken: new work dispatched ahead of committed work"
    )


# ---------------------------------------------------------------------------
# incumbent=None byte-identical + config gate
# ---------------------------------------------------------------------------


def test_incumbent_none_byte_identical(fleet, cpm):
    """Contract tripwire: ``incumbent=None`` must be BYTE-IDENTICAL to the
    pre-commitment behavior. Two FULL build_schedule runs over the mini
    fleet — one with the default, one with an explicit ``incumbent=None`` —
    must be dict-equal after zeroing the measured wall_seconds, and every
    commitment counter must be exactly 0."""
    s1 = build_schedule(fleet, cpm)
    s2 = build_schedule(fleet, cpm, incumbent=None)
    assert _strip_wall(s1) == _strip_wall(s2)
    for stats in (s1.stats, s2.stats):
        assert stats["commitment_in_horizon"] == 0
        assert stats["commitment_kept"] == 0
        assert stats["commitment_mech_kept"] == 0
    assert not any(a.kept_incumbent for a in s1.assignments.values())


def test_commitment_enabled_flag_gates_the_defense(fleet, cpm, monkeypatch):
    """Config §12: COMMITMENT_ENABLED=False turns the whole mechanism off —
    an incumbent map is ignored and the output equals the incumbent=None
    schedule exactly (dict-equal after zeroing wall_seconds)."""
    baseline = build_schedule(fleet, cpm)
    incumbent = incumbent_from_schedule(baseline)
    monkeypatch.setattr(config, "COMMITMENT_ENABLED", False)
    gated = build_schedule(fleet, cpm, incumbent=incumbent)
    assert _strip_wall(gated) == _strip_wall(baseline)
    assert gated.stats["commitment_in_horizon"] == 0


# ---------------------------------------------------------------------------
# serde tolerance + validator + determinism with commitments active
# ---------------------------------------------------------------------------


def test_assignment_from_dict_tolerates_missing_kept_incumbent():
    """Contract: kept_incumbent is optional (default False) and serialized;
    ``from_dict`` tolerates its absence so fixtures written before
    INCREMENT 5 still load."""
    old_row = {
        "task_id": A_ID, "day": 0, "shift": 1, "start_minute": 0,
        "end_minute": 60, "mechanic_ids": ["T01-S1-M001"], "team": "T01",
        "skill": "S", "uses_overtime": False,
    }  # no kept_incumbent key — an old fixture row
    asg = Assignment.from_dict(old_row)
    assert asg.kept_incumbent is False
    assert asg.to_dict()["kept_incumbent"] is False  # serialized going forward
    sched = Schedule.from_dict({"assignments": {A_ID: old_row}, "unscheduled": {}})
    assert sched.assignments[A_ID].kept_incumbent is False
    # Round trip preserves an explicit True.
    asg2 = Assignment.from_dict({**old_row, "kept_incumbent": True})
    assert Assignment.from_dict(asg2.to_dict()) == asg2


def test_validator_clean_and_deterministic_with_commitments_active(fleet, cpm):
    """The validator is never weakened: a full mini-fleet replan that
    threads its own prior schedule as the incumbent (commitments ACTIVE,
    defenses exercised) still passes V1..V9 with zero violations, and two
    such runs are identical except wall_seconds (behavior 7 holds under
    OR-4)."""
    first = build_schedule(fleet, cpm)
    incumbent = incumbent_from_schedule(first)
    second = build_schedule(fleet, cpm, incumbent=incumbent)
    assert second.stats["commitment_in_horizon"] > 0, "defense never exercised"
    assert second.stats["commitment_kept"] > 0
    kept_flags = sum(1 for a in second.assignments.values() if a.kept_incumbent)
    assert kept_flags == second.stats["commitment_kept"]
    report = validate(second, fleet)
    assert report["violations"] == [], "V1..V9 must stay clean under OR-4"
    third = build_schedule(fleet, cpm, incumbent=incumbent)
    assert _strip_wall(second) == _strip_wall(third)
