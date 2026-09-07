"""Non-compliance levers of the digital-week sim (5-day demo increment).

Covers the two new ``run_digital_week`` parameters:

- ``deviate_rate``: planned tasks skipped by their crew, which completes a
  ready NON-critical same-team unplanned task instead;
- ``oos_per_round``: out-of-sequence completions (an in-progress
  predecessor jumped), the P_OOS-penalized behavior;

plus the two hard compatibility guarantees: defaults leave the rng stream
(and therefore every previously measured result) untouched, and the new
``on_replanned`` hook fires once per round with the post-replan state.
"""

from __future__ import annotations

from ff.domain import Fleet
from ff.services.points import score_task
from ff.services.snapshot import build_snapshot
from ff.sim.digital_week import run_digital_week

from tests.conftest import MINI_ARGS
from ff.data.generator import generate_fleet


def _mini() -> Fleet:
    return generate_fleet(*MINI_ARGS)


def test_defaults_unchanged_vs_legacy_stream():
    """deviate_rate=0 / oos_per_round=0 must reproduce the legacy result
    exactly (identical rng draw order) — measured history stays valid."""
    a = run_digital_week(_mini(), rounds=3, seed=7)
    b = run_digital_week(
        _mini(), rounds=3, seed=7, deviate_rate=0.0, oos_per_round=0
    )
    for ra, rb in zip(a["rounds"], b["rounds"]):
        for key in ("executed", "slid", "injected", "fleet_lateness", "otd"):
            assert ra[key] == rb[key]
        assert ra["deviated"] == 0 and rb["deviated"] == 0
        assert ra["offplan_done"] == 0 and rb["oos_done"] == 0


def test_deviation_skips_planned_and_completes_offplan():
    """State checks run INSIDE the callback — a task skipped this round is
    legitimately replanned and may complete in a LATER round."""
    saw_skip = {"n": 0}

    def check(c):
        by_id = {t.task_id: t for t in c["fleet"].tasks}
        planned = set(c["planned"])
        saw_skip["n"] += len(c["skipped"])
        for tid in c["skipped"]:
            # the planned task was never worked THIS shift
            assert by_id[tid].state == "not_started"
        for tid in c["offplan"]:
            task = by_id[tid]
            assert task.state == "done"
            assert tid not in planned, "replacement must be unplanned work"
            # no DAG jump on a deviation: every predecessor is done
            for p in task.predecessors:
                if p in by_id and p != tid:
                    assert by_id[p].state == "done"
        # a skipped task pairs with at most one replacement
        assert len(c["offplan"]) <= len(c["skipped"])

    run_digital_week(
        _mini(), rounds=3, seed=7, deviate_rate=0.5, on_executed=check
    )
    assert saw_skip["n"] > 0, "expected deviations at 50%"


def test_offplan_replacement_same_team():
    ctxs: list[dict] = []
    run_digital_week(
        _mini(), rounds=2, seed=7, deviate_rate=0.6, on_executed=ctxs.append
    )
    for c in ctxs:
        by_id = {t.task_id: t for t in c["fleet"].tasks}
        planned_teams = {by_id[t].team for t in c["planned"]}
        for tid in c["offplan"]:
            assert by_id[tid].team in planned_teams


def test_oos_completion_has_unfinished_pred_and_is_penalized():
    """Checks run at execution time (inside the callback): the jumped
    predecessor may legitimately finish in a later round."""
    hits = {"n": 0, "penalized": 0}

    def check(c):
        by_id = {t.task_id: t for t in c["fleet"].tasks}
        for tid in c["oos"]:
            hits["n"] += 1
            task = by_id[tid]
            assert task.state == "done"
            assert any(
                p in by_id and by_id[p].state != "done"
                for p in task.predecessors
                if p != tid
            ), "OOS completion must have jumped an unfinished predecessor"
            snap = build_snapshot(c["fleet"], c["schedule"], c["cpm"])
            score = score_task(tid, snap)
            if score["oos_penalty"] > 0:
                hits["penalized"] += 1

    run_digital_week(
        _mini(), rounds=4, seed=7, oos_per_round=2, on_executed=check
    )
    assert hits["n"] > 0, "expected at least one out-of-sequence completion"
    assert hits["penalized"] == hits["n"], "P_OOS must dock every OOS jump"


def test_on_replanned_fires_with_post_replan_state():
    seen: list[dict] = []
    result = run_digital_week(
        _mini(),
        rounds=3,
        seed=7,
        deviate_rate=0.2,
        oos_per_round=1,
        on_replanned=seen.append,
    )
    assert [c["round"] for c in seen] == [1, 2, 3]
    for c in seen:
        assert c["schedule"] is not c["prev_schedule"]
        assert c["row"]["round"] == c["round"]
        # the hook's schedule is the round's measured plan
        assert c["row"]["scheduled"] == c["schedule"].stats["scheduled"]
    assert result["params"]["deviate_rate"] == 0.2
    assert result["params"]["oos_per_round"] == 1


def test_strict_execution_blocks_dependent_successors():
    """Strict physics: a planned task whose pred is not done at its start
    moment is BLOCKED (no coins) — it stays not_started and is reported."""
    total_blocked = {"n": 0}

    def check(c):
        by_id = {t.task_id: t for t in c["fleet"].tasks}
        total_blocked["n"] += len(c["blocked"])
        # a pred may legitimately finish DURING the slot after the block
        # decision (e.g. an off-plan catch-up completion) — the invariant
        # is "not done at the task's start moment", observable here as:
        # still not done at end of slot OR completed within this slot.
        done_this_slot = (
            set(c["executed"]) | set(c["offplan"]) | set(c["oos"])
        )
        for tid in c["blocked"]:
            task = by_id[tid]
            assert task.state == "not_started"
            assert any(
                p in by_id
                and (by_id[p].state != "done" or p in done_this_slot)
                for p in task.predecessors
                if p != tid
            )
        # strict mode guarantee: nothing goes in_progress/done on top of an
        # unfinished predecessor except explicit OOS jumps
        oos = set(c["oos"])
        for t in c["fleet"].tasks:
            if t.state == "in_progress" and t.task_id not in oos:
                for p in t.predecessors:
                    if p != t.task_id and p in by_id:
                        assert by_id[p].state in ("done", "in_progress")

    run_digital_week(
        _mini(), rounds=6, seed=7, deviate_rate=0.4, on_executed=check
    )
    assert total_blocked["n"] > 0, "40% deviation must block some successors"


def test_week_with_levers_validates_clean():
    """The demo configuration must yield ZERO V1-V9 violations on every
    replan — the void-edge rule plus strict execution close the V2 hole."""
    from ff.engine.validator import validate

    totals = []

    def check(c):
        rep = validate(c["schedule"], c["fleet"])
        totals.append(rep["summary"]["total"])

    run_digital_week(
        _mini(),
        rounds=6,
        seed=11,
        deviate_rate=0.4,
        oos_per_round=2,
        on_replanned=check,
    )
    assert totals and sum(totals) == 0, f"violations per round: {totals}"


def test_determinism_with_levers_on():
    a = run_digital_week(
        _mini(), rounds=3, seed=11, deviate_rate=0.3, oos_per_round=1
    )
    b = run_digital_week(
        _mini(), rounds=3, seed=11, deviate_rate=0.3, oos_per_round=1
    )
    for ra, rb in zip(a["rounds"], b["rounds"]):
        for key in (
            "executed",
            "slid",
            "deviated",
            "offplan_done",
            "oos_done",
            "injected",
            "fleet_lateness",
            "otd",
        ):
            assert ra[key] == rb[key]
