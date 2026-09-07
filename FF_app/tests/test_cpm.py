"""CPM engine tests — slack/priority on a hand-built 5-task DAG + criticality.

Contract (ARCHITECTURE.md §ff/engine/cpm.py): per-aircraft forward/backward
pass in MINUTES of work content (day = sum(SHIFT_EFFECTIVE)), slack vs
deadline_day * day-minutes, cpm_priority = remaining critical-chain work
THROUGH the task, downstream_count = transitive successor count.
isCritical <=> slack <= CRITICAL_SLACK_MIN days.
"""

from __future__ import annotations

from tests.conftest import mk_task

import config
from ff.domain import DAY_WORK_MINUTES as D
from ff.engine.cpm import compute_cpm, is_critical

T1, T2, T3, T4, T5 = (f"0001-T{n:05d}" for n in range(1, 6))


def _hand_dag():
    """Diamond T1 -> (T2, T3) -> T4 plus independent tight-deadline T5."""
    return [
        mk_task(T1, dur=100, deadline=1),
        mk_task(T2, dur=200, deadline=1, preds=[T1]),
        mk_task(T3, dur=50, deadline=1, preds=[T1]),
        mk_task(T4, dur=100, deadline=1, preds=[T2, T3]),
        mk_task(T5, dur=30, deadline=0),
    ]


def test_forward_backward_pass_exact():
    """es/lf/slack computed exactly on the hand-built diamond DAG."""
    info = compute_cpm(_hand_dag())
    assert set(info) == {T1, T2, T3, T4, T5}

    assert info[T1]["es"] == 0
    assert info[T2]["es"] == 100
    assert info[T3]["es"] == 100
    assert info[T4]["es"] == 300
    assert info[T5]["es"] == 0

    assert info[T4]["lf"] == D  # deadline_day 1 in work-content minutes
    assert info[T2]["lf"] == D - 100
    assert info[T3]["lf"] == D - 100
    assert info[T1]["lf"] == D - 300  # tightened through T2 (the long branch)
    assert info[T5]["lf"] == 0

    assert info[T1]["slack_minutes"] == float(D - 400)
    assert info[T2]["slack_minutes"] == float(D - 400)  # on the long branch
    assert info[T3]["slack_minutes"] == float(D - 250)  # short branch has spare
    assert info[T4]["slack_minutes"] == float(D - 400)
    assert info[T5]["slack_minutes"] == float(-30)  # already infeasible


def test_cpm_priority_is_chain_through_task():
    """cpm_priority = own duration + longest successor chain (more work
    hanging behind => higher priority)."""
    info = compute_cpm(_hand_dag())
    assert info[T4]["cpm_priority"] == 100.0
    assert info[T2]["cpm_priority"] == 300.0  # 200 + 100
    assert info[T3]["cpm_priority"] == 150.0  # 50 + 100
    assert info[T1]["cpm_priority"] == 400.0  # 100 + 300 (via T2)
    assert info[T5]["cpm_priority"] == 30.0
    # Ordering invariant: the chain head outranks everything below it.
    assert (
        info[T1]["cpm_priority"]
        > info[T2]["cpm_priority"]
        > info[T3]["cpm_priority"]
        > info[T4]["cpm_priority"]
    )


def test_downstream_count_transitive():
    """downstream_count counts TRANSITIVE successors, not direct ones."""
    info = compute_cpm(_hand_dag())
    assert info[T1]["downstream_count"] == 3  # T2, T3, T4
    assert info[T2]["downstream_count"] == 1
    assert info[T3]["downstream_count"] == 1
    assert info[T4]["downstream_count"] == 0
    assert info[T5]["downstream_count"] == 0


def test_critical_flag():
    """isCritical <=> slack <= CRITICAL_SLACK_MIN days: T5 (negative slack)
    is critical; T1 (D-400 minutes of slack, ~0.69 days under default
    config) is not."""
    info = compute_cpm(_hand_dag())
    assert is_critical(info[T5]) is True
    assert (info[T5]["slack_minutes"] / D) <= config.CRITICAL_SLACK_MIN
    # Under the default calendar (D=1290, threshold 0.5) T1 has spare slack.
    assert is_critical(info[T1]) == (
        (info[T1]["slack_minutes"] / D) <= config.CRITICAL_SLACK_MIN
    )
    assert is_critical(info[T1]) is False


def test_earliest_day_floors_es():
    """A task's es never precedes its own earliest_day (in day-minutes)."""
    solo = "0001-T00001"
    info = compute_cpm([mk_task(solo, dur=60, earliest=2, deadline=10)])
    assert info[solo]["es"] == 2 * D
    assert info[solo]["slack_minutes"] == float(10 * D - (2 * D + 60))


def test_pure_and_deterministic():
    """Input order never matters: reversed task list => identical result."""
    tasks = _hand_dag()
    assert compute_cpm(tasks) == compute_cpm(list(reversed(tasks)))


def test_per_aircraft_independence():
    """Passes run per aircraft: adding another aircraft's tasks does not
    perturb the first aircraft's numbers (C12: no cross-aircraft coupling)."""
    tasks = _hand_dag()
    other = [
        mk_task("0002-T00001", aircraft=2, dur=400, deadline=1),
        mk_task("0002-T00002", aircraft=2, dur=400, deadline=1, preds=["0002-T00001"]),
    ]
    solo_info = compute_cpm(tasks)
    both_info = compute_cpm(tasks + other)
    for tid in (T1, T2, T3, T4, T5):
        assert both_info[tid] == solo_info[tid]
