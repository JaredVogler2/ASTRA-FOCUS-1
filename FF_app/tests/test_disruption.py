"""Disruption attribution tripwires — auto rules, merge law, excusable set.

Contract (ARCHITECTURE.md INCREMENT 2 addendum): 8 verbatim cause ids with
EXCUSABLE = the 5 '+' causes; deterministic auto attribution ("blocked task
w/ parts_eta -> LATE_PART ('parts ETA day N'); feasibility
WAITING_PREDECESSOR with blocking_team != team -> CROSS_TEAM_PREDECESSOR
..., same team -> SAME_TEAM_PREDECESSOR; unscheduled
'no_crew_within_horizon' -> CAPACITY_SHORTAGE; 'no_skill_holder'/
'crew_exceeds_pool' -> SKILL_SHORTAGE; is_rework not_started ->
REWORK_INJECTION for the RECEIVING team"); and the merge law: "manual
captures EXTEND auto attribution (never replace); duplicates (same
task+cause) collapse to auto".
"""

from __future__ import annotations

import pytest

from tests.conftest import mk_fleet, mk_mech, mk_task

from ff.domain import Assignment, Schedule
from ff.services import disruption
from ff.services.snapshot import build_snapshot

B1 = "0001-T00001"  # blocked w/ parts ETA           -> LATE_PART
C1 = "0001-T00002"  # waiting on T02's task P2       -> CROSS_TEAM_PREDECESSOR
P2 = "0001-T00003"  # the T02 blocking predecessor   -> (no record itself)
S1 = "0001-T00004"  # waiting on own-team task B1    -> SAME_TEAM_PREDECESSOR
K1 = "0001-T00005"  # unscheduled: no_crew_within_horizon -> CAPACITY_SHORTAGE
K2 = "0001-T00006"  # unscheduled: no_skill_holder   -> SKILL_SHORTAGE
K3 = "0001-T00007"  # unscheduled: crew_exceeds_pool -> SKILL_SHORTAGE
R1 = "0001-T00008"  # not-started rework             -> REWORK_INJECTION
D1 = "0001-T00009"  # done                           -> nothing, ever


@pytest.fixture()
def disr_snap():
    """Hand-built snapshot exercising every auto-attribution rule once.

    The schedule is hand-crafted (not engine-run) so each unscheduled
    reason code is exactly what the rule under test needs; B1 carries an
    assignment so its records inherit the REAL slot (day 5, shift 2), the
    unplaced tasks fall back to (today=0, shift 0 = unslotted).
    """
    tasks = [
        mk_task(B1, state="blocked", parts_eta=5),
        mk_task(C1, preds=[P2]),
        mk_task(P2, team="T02"),
        mk_task(S1, preds=[B1]),
        mk_task(K1),
        mk_task(K2),
        mk_task(K3),
        mk_task(R1, rework=True),
        mk_task(D1, state="done"),
    ]
    mechanics = [
        mk_mech("T01-S1-M001"),
        mk_mech("T01-S2-M001", shift=2),
        mk_mech("T02-S1-M001", team="T02"),
    ]
    fleet = mk_fleet(tasks, mechanics)
    schedule = Schedule(
        assignments={
            B1: Assignment(
                task_id=B1, day=5, shift=2, start_minute=0, end_minute=60,
                mechanic_ids=["T01-S2-M001"], team="T01", skill="S",
                uses_overtime=False,
            )
        },
        unscheduled={
            K1: "no_crew_within_horizon",
            K2: "no_skill_holder",
            K3: "crew_exceeds_pool",
        },
        stats={},
        meta={"start_day": 0},
    )
    return build_snapshot(fleet, schedule, cpm={})


def _by_task(records):
    out = {}
    for rec in records:
        out.setdefault(rec["task_id"], []).append(rec)
    return out


def test_causes_and_excusable_set_exact():
    """The 8 cause ids are VERBATIM and ordered; EXCUSABLE is EXACTLY the
    5 '+' causes — nothing may creep in or out (GG-3 keys on this set)."""
    assert disruption.CAUSES == (
        "LATE_PART",
        "CROSS_TEAM_PREDECESSOR",
        "SAME_TEAM_PREDECESSOR",
        "CAPACITY_SHORTAGE",
        "SKILL_SHORTAGE",
        "DURATION_OVERRUN",
        "REWORK_INJECTION",
        "PLAN_CHURN",
    )
    assert disruption.EXCUSABLE == frozenset(
        {
            "LATE_PART",
            "CROSS_TEAM_PREDECESSOR",
            "CAPACITY_SHORTAGE",
            "SKILL_SHORTAGE",
            "REWORK_INJECTION",
        }
    )
    assert disruption.NOTES_REQUIRED == frozenset(
        {"SAME_TEAM_PREDECESSOR", "DURATION_OVERRUN"}
    )


def test_late_part_attribution(disr_snap):
    """'blocked task w/ parts_eta -> LATE_PART ("parts ETA day N")' — and
    the record carries the task's REAL assignment slot."""
    recs = _by_task(disruption.attribute(disr_snap))[B1]
    late = [r for r in recs if r["cause"] == "LATE_PART"]
    assert len(late) == 1
    rec = late[0]
    assert rec["evidence"] == "parts ETA day 5"
    assert rec["excusable"] is True and rec["source"] == "auto"
    assert (rec["team"], rec["day"], rec["shift"]) == ("T01", 5, 2)


def test_cross_team_predecessor_attribution(disr_snap):
    """'feasibility WAITING_PREDECESSOR with blocking_team != team ->
    CROSS_TEAM_PREDECESSOR (evidence names blocking task+team)' — waiting
    on ANOTHER team is excusable (OR-2 means you cannot go work it)."""
    recs = _by_task(disruption.attribute(disr_snap))[C1]
    assert [r["cause"] for r in recs] == ["CROSS_TEAM_PREDECESSOR"]
    rec = recs[0]
    assert P2 in rec["evidence"] and "T02" in rec["evidence"]
    assert rec["excusable"] is True


def test_same_team_predecessor_attribution(disr_snap):
    """'... same team -> SAME_TEAM_PREDECESSOR' with the owner-ruling
    ROOT-CAUSE PASS-THROUGH: S1 waits on own-team B1, but B1 is blocked
    on PARTS — an external root cause, so the successor's wait is
    excused with the root named in the evidence. The genuinely-owned
    case (pred simply not done) is covered in tests/test_excusal_rulings
    ::test_own_team_chain_with_owned_root_stays_inexcusable."""
    recs = _by_task(disruption.attribute(disr_snap))[S1]
    assert [r["cause"] for r in recs] == ["SAME_TEAM_PREDECESSOR"]
    assert recs[0]["excusable"] is True
    assert "root cause external" in recs[0]["evidence"]
    assert "LATE_PART" in recs[0]["evidence"]
    assert B1 in recs[0]["evidence"]


def test_capacity_and_skill_shortage_attribution(disr_snap):
    """'unscheduled "no_crew_within_horizon" -> CAPACITY_SHORTAGE;
    "no_skill_holder"/"crew_exceeds_pool" -> SKILL_SHORTAGE' — the engine's
    honest delay verdicts (OR-1: delayed, never short-crewed) map to
    excusable causes."""
    by_task = _by_task(disruption.attribute(disr_snap))
    assert [r["cause"] for r in by_task[K1]] == ["CAPACITY_SHORTAGE"]
    assert by_task[K1][0]["evidence"] == "unscheduled: no_crew_within_horizon"
    assert [r["cause"] for r in by_task[K2]] == ["SKILL_SHORTAGE"]
    assert [r["cause"] for r in by_task[K3]] == ["SKILL_SHORTAGE"]
    for tid in (K1, K2, K3):
        rec = by_task[tid][0]
        assert rec["excusable"] is True
        assert (rec["day"], rec["shift"]) == (0, 0), "unplaced -> unslotted"


def test_rework_injection_for_receiving_team(disr_snap):
    """'is_rework not_started -> REWORK_INJECTION for the RECEIVING team'
    — the record lands on the task's own team (the one absorbing the
    injected work). Excusability is ORIGIN-AWARE (owner directive,
    docs/GATE_PRESSURE_DESIGN.md: "rework caused by the team" is not an
    excuse): this fixture's rework is UNATTRIBUTED, so the receiving team
    owns it (config.REWORK_UNATTRIBUTED_EXCUSABLE default False).
    Cross-team-origin excusability is covered in tests/test_rework_origin.
    """
    recs = _by_task(disruption.attribute(disr_snap))[R1]
    assert [r["cause"] for r in recs] == ["REWORK_INJECTION"]
    assert recs[0]["team"] == "T01" and recs[0]["excusable"] is False
    assert "unattributed" in recs[0]["evidence"]


def test_done_and_untriggered_tasks_get_no_records(disr_snap):
    """No rule fires for done work or a plain ready task: attribution never
    invents a disruption (honesty — a cause without a trigger is fiction)."""
    by_task = _by_task(disruption.attribute(disr_snap))
    assert D1 not in by_task
    assert P2 not in by_task  # ready, unblocked, not rework: no record


def test_attribution_deterministic(disr_snap):
    """Identical snapshot => identical records (sorted iteration, fixed
    intra-task rule order; memoized on the snapshot)."""
    first = disruption.attribute(disr_snap)
    assert disruption.attribute(disr_snap) == first
    # Rebuilt snapshot (fresh memo) reproduces the identical list.
    rebuilt = build_snapshot(
        disr_snap["fleet"], disr_snap["schedule"], disr_snap["cpm"]
    )
    assert disruption.attribute(rebuilt) == first


def test_merged_manual_extends_never_replaces(disr_snap):
    """Merge law: 'manual captures EXTEND auto attribution (never
    replace)' — every auto record survives the merge and the new manual
    record is added alongside."""
    auto = disruption.attribute(disr_snap)
    manual = [
        {
            "task_id": K1, "team": "T01", "day": 0, "shift": 0,
            "cause": "PLAN_CHURN", "evidence": "slot moved 3x this week",
            "source": "manual", "entered_by": "lead:T01",
        }
    ]
    merged = disruption.merged_excusals(disr_snap, manual)
    flat = [rec for group in merged.values() for rec in group]
    for rec in auto:
        assert rec in flat, "auto attribution must survive the merge"
    k1_causes = sorted(r["cause"] for r in flat if r["task_id"] == K1)
    assert k1_causes == ["CAPACITY_SHORTAGE", "PLAN_CHURN"]
    churn = next(r for r in flat if r["cause"] == "PLAN_CHURN")
    assert churn["source"] == "manual" and churn["excusable"] is False


def test_merged_duplicate_collapses_to_auto(disr_snap):
    """Merge law: 'duplicates (same task+cause) collapse to auto' — a
    manual capture repeating an auto-attributed cause changes NOTHING (the
    auto record wins; no double counting in the pareto or GG-3 math)."""
    baseline = disruption.merged_excusals(disr_snap, [])
    manual = [
        {
            "task_id": B1, "team": "T01", "day": 5, "shift": 2,
            "cause": "LATE_PART", "evidence": "lead says parts late",
            "source": "manual",
        }
    ]
    merged = disruption.merged_excusals(disr_snap, manual)
    assert merged == baseline, "duplicate manual capture must collapse to auto"
    b1_group = merged[("T01", 5, 2)]
    late = [r for r in b1_group if r["cause"] == "LATE_PART"]
    assert len(late) == 1 and late[0]["source"] == "auto"
