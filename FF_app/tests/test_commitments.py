"""Commitments tripwires — hysteresis, persistence, bad-news-fast (§9, OR-4).

Contract (ARCHITECTURE.md INCREMENT 2 addendum, MAX §11 doctrine):
"(1) first sighting commits immediately; (2) abs(delta) <
COMMIT_HYSTERESIS_DAYS = jitter -> hold, clear pending; (3) delta >=
COMMIT_WORSEN_IMMEDIATE_DAYS -> recommit at once with evidence ('bad news
fast'); (4) all else (small slips AND improvements) needs a pending streak
of COMMIT_PERSISTENCE_RUNS consecutive runs at the same target (±1 day) —
reason gains ' (sustained N replans)'; pending resets when target moves."

Defaults under test: COMMIT_HYSTERESIS_DAYS=2, COMMIT_PERSISTENCE_RUNS=2,
COMMIT_WORSEN_IMMEDIATE_DAYS=7.
"""

from __future__ import annotations

import config
from ff.services.commitments import (
    empty_state,
    load_state,
    persist_state,
    projections_from_stats,
    snapshot_block,
    update_commitments,
)


def _proj(day: int, aircraft: int = 1) -> list[dict]:
    return [{"aircraft": aircraft, "projected_day": day}]


def _committed(state: dict, aircraft: int = 1) -> int:
    return state["aircraft"][str(aircraft)]["committed_day"]


def test_initial_sighting_commits_immediately():
    """Rule 1: 'first sighting commits immediately, reason "initial
    commitment"' — no hysteresis, no streak, a promise exists from run 1."""
    state, changes = update_commitments(empty_state(), _proj(50), "run-1", {})
    assert _committed(state) == 50
    assert state["aircraft"]["1"]["pending"] is None
    assert len(changes) == 1
    row = changes[0]
    assert row["reason"] == "initial commitment"
    assert row["old"] is None and row["new"] == 50
    assert row["run"] == "run-1"
    assert state["log"] == changes


def test_one_day_jitter_holds_commitment():
    """Rule 2: 'abs(delta) < COMMIT_HYSTERESIS_DAYS = jitter -> hold, clear
    pending' — a 1-day wobble NEVER moves the committed date (and it wipes
    any pending streak, so jitter cannot help a slip sustain)."""
    assert config.COMMIT_HYSTERESIS_DAYS == 2
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    # Build a pending streak first, then jitter must clear it.
    state, changes = update_commitments(state, _proj(53), "r2", {})
    assert changes == [] and state["aircraft"]["1"]["pending"] is not None
    state, changes = update_commitments(state, _proj(51), "r3", {})  # |51-50|=1
    assert changes == [], "jitter must not recommit"
    assert _committed(state) == 50, "1-day jitter held the commitment"
    assert state["aircraft"]["1"]["pending"] is None, "jitter clears pending"


def test_nine_day_slip_recommits_immediately_with_evidence():
    """Rule 3: 'delta >= COMMIT_WORSEN_IMMEDIATE_DAYS -> recommit at once
    with evidence ("bad news fast")' — a 9-day slip moves the promise in
    ONE run and the log reason carries the evidence causes."""
    assert config.COMMIT_WORSEN_IMMEDIATE_DAYS == 7
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    evidence = {"1": {"rework_inserted": 2, "blocked_parts": 1}}
    state, changes = update_commitments(state, _proj(59), "r2", evidence)
    assert _committed(state) == 59, "bad news moves the commitment at once"
    assert len(changes) == 1
    row = changes[0]
    assert row["old"] == 50 and row["new"] == 59 and row["delta_days"] == 9
    assert "rework_inserted" in row["reason"], "evidence cause in the reason"
    assert "blocked_parts" in row["reason"]
    assert "sustained" not in row["reason"], "immediate recommit, not a streak"


def test_nine_day_slip_without_evidence_falls_back_to_global_reason():
    """Rule 3 reason fallback: 'fallback "global replan shift"' — a slip is
    never logged without a reason string."""
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    state, changes = update_commitments(state, _proj(59), "r2", {})
    assert changes[0]["reason"] == "global replan shift"


def test_three_day_slip_needs_two_sustained_runs():
    """Rule 4: a 3-day slip (inside the worsen-immediate band) 'needs a
    pending streak of COMMIT_PERSISTENCE_RUNS consecutive runs at the same
    target (±1 day)' — held on run 1, committed on run 2 with the reason
    gaining ' (sustained 2 replans)'."""
    assert config.COMMIT_PERSISTENCE_RUNS == 2
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    evidence = {"1": {"capacity": 4}}

    state, changes = update_commitments(state, _proj(53), "r2", evidence)
    assert changes == [], "first divergent run only opens a pending streak"
    assert _committed(state) == 50
    assert state["aircraft"]["1"]["pending"] == {"target": 53, "streak": 1}

    # ±1 day of the pending target still counts as the same target.
    state, changes = update_commitments(state, _proj(54), "r3", evidence)
    assert len(changes) == 1, "second sustained run recommits"
    assert _committed(state) == 54
    assert changes[0]["reason"].endswith("(sustained 2 replans)")
    assert "capacity" in changes[0]["reason"], "slip reason carries evidence"
    assert state["aircraft"]["1"]["pending"] is None


def test_improvement_also_needs_persistence():
    """Rule 4 applies to good news too: 'all else (small slips AND
    improvements) needs a pending streak' — a 5-day improvement holds on
    run 1 and commits on run 2 with reason 'schedule improvement
    (sustained 2 replans)'. Improvements never use slip evidence."""
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    state, changes = update_commitments(state, _proj(45), "r2", {"1": {"capacity": 9}})
    assert changes == [] and _committed(state) == 50
    state, changes = update_commitments(state, _proj(45), "r3", {"1": {"capacity": 9}})
    assert _committed(state) == 45
    assert changes[0]["reason"] == "schedule improvement (sustained 2 replans)"
    assert changes[0]["delta_days"] == -5


def test_pending_resets_when_target_moves():
    """Rule 4 reset: 'pending resets when target moves' — a streak at day 53
    does NOT carry over to a new target of day 56 (>±1 away); the streak
    restarts at 1 and the commitment holds until the NEW target sustains."""
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    state, _ = update_commitments(state, _proj(53), "r2", {})
    assert state["aircraft"]["1"]["pending"] == {"target": 53, "streak": 1}
    state, changes = update_commitments(state, _proj(56), "r3", {})
    assert changes == [], "moved target must not inherit the old streak"
    assert _committed(state) == 50
    assert state["aircraft"]["1"]["pending"] == {"target": 56, "streak": 1}
    state, changes = update_commitments(state, _proj(56), "r4", {})
    assert _committed(state) == 56, "new target sustained 2 runs -> commits"
    assert changes[0]["reason"].endswith("(sustained 2 replans)")


def test_log_accumulates_across_runs_and_persists(tmp_path):
    """'Log rows: {"run", "aircraft", "old", "new", "delta_days", "reason"}'
    — the log accumulates across runs (never truncated by an update) and
    round-trips through the atomic data/commitments.json persistence."""
    state, _ = update_commitments(empty_state(), _proj(50), "r1", {})
    state, _ = update_commitments(state, _proj(59), "r2", {})   # bad news fast
    state, _ = update_commitments(state, _proj(62), "r3", {})   # streak 1
    state, _ = update_commitments(state, _proj(62), "r4", {})   # sustained
    assert [row["run"] for row in state["log"]] == ["r1", "r2", "r4"]
    for row in state["log"]:
        assert set(row) == {"run", "aircraft", "old", "new", "delta_days", "reason"}

    path = str(tmp_path / "commitments.json")
    persist_state(state, path)
    reloaded = load_state(path)
    assert reloaded["aircraft"]["1"]["committed_day"] == 62
    assert reloaded["log"] == state["log"]


def test_projections_and_snapshot_block_shapes():
    """Integration shapes: projections come from Schedule.stats.aircraft
    completion days, and the snapshot block is {committed:[{aircraft,
    committed_day, projected_day, deadline_day, delta}], changes:[last 50],
    run_id} with delta = projected - committed."""
    stats = {
        "aircraft": [
            {"aircraft": 2, "completion_day": 40, "deadline_day": 45},
            {"aircraft": 1, "completion_day": 50, "deadline_day": 48},
        ]
    }
    projections = projections_from_stats(stats)
    assert projections == [
        {"aircraft": 1, "projected_day": 50},
        {"aircraft": 2, "projected_day": 40},
    ]
    state, _ = update_commitments(empty_state(), projections, "r1", {})
    # Simulate a drifted projection without a recommit (pending streak).
    projections2 = [
        {"aircraft": 1, "projected_day": 53},
        {"aircraft": 2, "projected_day": 40},
    ]
    state, _ = update_commitments(state, projections2, "r2", {})
    block = snapshot_block(state, projections2, {1: 48, 2: 45}, "r2")
    assert block["run_id"] == "r2"
    assert block["committed"] == [
        {"aircraft": 1, "committed_day": 50, "projected_day": 53,
         "deadline_day": 48, "delta": 3},
        {"aircraft": 2, "committed_day": 40, "projected_day": 40,
         "deadline_day": 45, "delta": 0},
    ]
    assert block["changes"] == state["log"][-50:]
