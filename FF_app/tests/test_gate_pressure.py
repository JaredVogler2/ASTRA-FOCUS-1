"""Gate pressure — behind-schedule factor + plan-of-record stamping.

docs/GATE_PRESSURE_DESIGN.md: jobs past their STATION gate gain bounded
value so non-critical work still gets done. Covers: inert-without-gate
(legacy fixtures byte-identical), bounded aging arithmetic (hand
computed), done-work-never-ages, cap saturation, the explanation chip,
and ``stamp_gates_from_schedule`` semantics.
"""

from __future__ import annotations

import config
from ff.data.generator import generate_fleet
from ff.engine.cpm import compute_cpm
from ff.engine.scheduler import build_schedule
from ff.services import points
from ff.services.scorecard import stamp_gates_from_schedule
from ff.services.snapshot import build_snapshot

from tests.conftest import MINI_ARGS


def _world():
    fleet = generate_fleet(*MINI_ARGS)
    cpm = compute_cpm(fleet.tasks)
    schedule = build_schedule(fleet, cpm)
    return fleet, cpm, schedule


def test_no_gate_means_factor_inert():
    """Legacy fixtures (gate_day None): behind raw is 0 for every task and
    every score is byte-identical to the pre-gate implementation."""
    fleet, cpm, schedule = _world()
    snap = build_snapshot(fleet, schedule, cpm)
    for tid in sorted(schedule.assignments)[:25]:
        score = points.score_task(tid, snap)
        assert score["components"]["behind"]["points"] == 0
        assert score["components"]["behind"]["raw"] == 0.0
        assert "behind station gate" not in score["explanation"]


def test_behind_boost_is_bounded_and_hand_computable():
    fleet, cpm, schedule = _world()
    tid = sorted(schedule.assignments)[0]
    task = {t.task_id: t for t in fleet.tasks}[tid]
    today = int(schedule.meta.get("start_day", 0))

    # 3 days past gate with cap 5 -> raw 0.6 -> points = round(effort*.35*.6)
    task.gate_day = today - 3
    snap = build_snapshot(fleet, schedule, cpm)
    score = points.score_task(tid, snap)
    effort = score["effort"]
    cap = config.BEHIND_CAP_DAYS
    expect = int(round(effort * (config.W_BEHIND / 100.0) * min(3 / cap, 1)))
    assert score["components"]["behind"]["points"] == expect
    assert "behind station gate" in score["explanation"]

    # 30 days past gate -> saturates at raw 1.0 (aging never dominates
    # keystones: the cap is the whole anti-farming design, doc §4)
    task.gate_day = today - 30
    snap2 = build_snapshot(fleet, schedule, cpm)
    score2 = points.score_task(tid, snap2)
    cap_expect = int(round(effort * (config.W_BEHIND / 100.0)))
    assert score2["components"]["behind"]["points"] == cap_expect

    # ahead of gate -> zero
    task.gate_day = today + 10
    snap3 = build_snapshot(fleet, schedule, cpm)
    assert points.score_task(tid, snap3)["components"]["behind"]["points"] == 0


def test_done_work_never_ages():
    fleet, cpm, schedule = _world()
    tid = sorted(schedule.assignments)[0]
    task = {t.task_id: t for t in fleet.tasks}[tid]
    today = int(schedule.meta.get("start_day", 0))
    task.gate_day = today - 10
    task.state = "done"
    snap = build_snapshot(fleet, schedule, cpm)
    assert points.score_task(tid, snap)["components"]["behind"]["raw"] == 0.0


def test_weight_ordering_keystone_beats_aged_filler():
    """Doc §4: an aged filler must never outrank a keystone — the weight
    ordering W_CRIT > W_DOWN > W_BEHIND is the guarantee."""
    assert config.W_CRIT > config.W_DOWN > config.W_BEHIND


def test_stamp_gates_from_schedule():
    fleet, cpm, schedule = _world()
    n = stamp_gates_from_schedule(fleet, schedule, grace_days=2)
    assert n == len(schedule.assignments)
    by_id = {t.task_id: t for t in fleet.tasks}
    for tid in sorted(schedule.assignments)[:10]:
        assert by_id[tid].gate_day == schedule.assignments[tid].day + 2
    # idempotent
    assert stamp_gates_from_schedule(fleet, schedule, grace_days=2) == n
