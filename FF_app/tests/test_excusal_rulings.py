"""Owner excusal rulings (2026-07-11) — the three policy calls, tested.

1. Rework drops with a parent_soi; the parent SOI's team OWNS it (origin
   = parent team by paperwork rule; excusable only for a different team
   executing the fix).
2. Own-team predecessor waits are owned — UNLESS the chain's ROOT cause
   is external (parts / capacity / skill / cross-team upstream):
   root-cause pass-through.
3. Duration overruns are excusable while actual burn <= 3x standard
   (OVERRUN_EXCUSE_FACTOR), owned beyond it.
"""

from __future__ import annotations

import config
from ff.engine.cpm import compute_cpm
from ff.engine.scheduler import build_schedule
from ff.services import disruption
from ff.services.snapshot import build_snapshot
from tests.conftest import mk_fleet, mk_mech, mk_task


def _snap(tasks, mechanics=None):
    # Two mechanics per team: a one-mech pool has activation quota
    # floor(1 x 0.85) = 0 and everything cascades unscheduled.
    fleet = mk_fleet(
        tasks,
        mechanics
        or [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002"),
            mk_mech("T02-S1-M001", team="T02"),
            mk_mech("T02-S1-M002", team="T02")],
    )
    cpm = compute_cpm(fleet.tasks)
    schedule = build_schedule(fleet, cpm)
    return build_snapshot(fleet, schedule, cpm)


def _recs(snap, tid, cause):
    return [
        r
        for r in disruption.attribute(snap)
        if r["task_id"] == tid and r["cause"] == cause
    ]


# ---------------------------------------------------------------------------
# Ruling 2 — root-cause pass-through on own-team chains
# ---------------------------------------------------------------------------


def test_own_team_pred_with_parts_blocked_root_is_excused():
    """A -> B(own team, blocked on parts): A's wait is excused — the root
    cause is the vendor, not the team, even through an own-team pred."""
    root = mk_task("0001-T00001", dur=60)
    root.state = "blocked"
    root.parts_eta_day = 30
    succ = mk_task("0001-T00002", dur=60, preds=["0001-T00001"])
    snap = _snap([root, succ])
    recs = _recs(snap, "0001-T00002", disruption.SAME_TEAM_PREDECESSOR)
    assert recs and recs[0]["excusable"] is True
    assert "LATE_PART" in recs[0]["evidence"]


def test_own_team_chain_to_cross_team_root_is_excused():
    """A -> B(own) -> C(other team, open): the own-team intermediary does
    not launder away the cross-team root cause."""
    c = mk_task("0001-T00001", dur=60, team="T02")
    b = mk_task("0001-T00002", dur=60, preds=["0001-T00001"])
    a = mk_task("0001-T00003", dur=60, preds=["0001-T00002"])
    snap = _snap([c, b, a])
    recs = _recs(snap, "0001-T00003", disruption.SAME_TEAM_PREDECESSOR)
    assert recs and recs[0]["excusable"] is True
    assert "team T02" in recs[0]["evidence"]


def test_own_team_chain_with_owned_root_stays_inexcusable():
    """A -> B(own, simply not done, no external condition): owned."""
    b = mk_task("0001-T00001", dur=60)
    a = mk_task("0001-T00002", dur=60, preds=["0001-T00001"])
    snap = _snap([b, a])
    recs = _recs(snap, "0001-T00002", disruption.SAME_TEAM_PREDECESSOR)
    assert recs and recs[0]["excusable"] is False
    assert "root cause external" not in recs[0]["evidence"]


# ---------------------------------------------------------------------------
# Ruling 3 — overrun tolerance factor
# ---------------------------------------------------------------------------


def _overrun_snap(actual: int, standard: int = 100):
    t = mk_task("0001-T00001", dur=standard)
    t.state = "in_progress"
    t.actual_minutes = actual
    t.remaining_minutes = 20
    return _snap([t])


def test_overrun_within_tolerance_is_excusable():
    recs = _recs(_overrun_snap(250), "0001-T00001", disruption.DURATION_OVERRUN)
    assert recs and recs[0]["excusable"] is True  # 2.5x <= 3.0x
    assert "2.5x" in recs[0]["evidence"]


def test_overrun_beyond_tolerance_is_owned():
    recs = _recs(_overrun_snap(350), "0001-T00001", disruption.DURATION_OVERRUN)
    assert recs and recs[0]["excusable"] is False  # 3.5x > 3.0x
    assert config.OVERRUN_EXCUSE_FACTOR == 3.0


def test_no_overrun_record_without_actuals_or_within_standard():
    t = mk_task("0001-T00001", dur=100)
    t.state = "in_progress"
    t.remaining_minutes = 50  # no actual_minutes feed
    assert not _recs(_snap([t]), "0001-T00001", disruption.DURATION_OVERRUN)
    assert not _recs(
        _overrun_snap(90), "0001-T00001", disruption.DURATION_OVERRUN
    )  # under standard: not an overrun at all


def test_manual_overrun_capture_cannot_flip_a_blowout():
    snap = _overrun_snap(400)  # 4x
    manual = [
        {
            "task_id": "0001-T00001",
            "cause": disruption.DURATION_OVERRUN,
            "team": "T01",
            "day": 0,
            "shift": 2,
            "evidence": "lead: it ran long, excuse it",
            "notes": "n/a",
        }
    ]
    merged = disruption.merged_excusals(snap, manual)
    all_recs = [r for g in merged.values() for r in g
                if r["cause"] == disruption.DURATION_OVERRUN]
    assert all_recs and all(r["excusable"] is False for r in all_recs)


# ---------------------------------------------------------------------------
# Ruling 1 — parent-SOI ownership travels through the sim writer
# ---------------------------------------------------------------------------


def test_sim_rework_origin_is_always_parent_team():
    from ff.data.generator import generate_fleet
    from ff.sim.digital_week import run_digital_week
    from tests.conftest import MINI_ARGS

    seen = {"self": 0, "cross": 0}

    def check(ctx):
        by_id = {t.task_id: t for t in ctx["fleet"].tasks}
        for tid in ctx["injected"]:
            rwk = by_id[tid]
            parent = by_id[rwk.predecessors[0]]
            # paperwork rule: origin is ALWAYS the parent SOI's team
            assert rwk.rework_origin_team == parent.team
            if rwk.team == parent.team:
                assert rwk.skill == parent.skill
                seen["self"] += 1
            else:
                assert rwk.skill == "ANY"  # cross-trade fix
                seen["cross"] += 1

    run_digital_week(
        generate_fleet(*MINI_ARGS), rounds=4, seed=7, on_executed=check
    )
    assert seen["self"] > 0 and seen["cross"] > 0
