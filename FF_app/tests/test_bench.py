"""Tests for the benchmark suite: ff/bench/mutations.py + tools/bench.py.

Tripwires (docstrings quote the law under test):

- every mutation is PURE (input fleet untouched) and DETERMINISTIC
  (identical (fleet, params) => identical mutated fleet);
- every mutation preserves DAG acyclicity and the no-cross-aircraft-edges
  law (C12) — rework_wave in particular only adds parent-gated leaves;
- the runner passes end-to-end on a mini-fleet baseline scenario and its
  reproducibility hash is stable across two runs.

Runs only against the mini fleet (3 aircraft / 40 tasks / 4 teams /
30 mechanics) — never the fleet50 corpus — so the suite stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ff.bench.mutations import (
    MUTATIONS,
    apply_overrides,
    late_parts,
    mechanic_absence,
    pulse_slip,
    rework_wave,
    skill_shortage,
)
from ff.domain import Fleet

# One representative param set per mutation, sized for the mini fleet.
MUTATION_CASES = [
    ("mechanic_absence", {"team": None, "fraction": 0.10, "seed": 7}),
    ("skill_shortage", {"skill": "QUAL", "seed": 7}),
    ("late_parts", {"fraction": 0.05, "eta_day": 10, "seed": 7}),
    ("rework_wave", {"count": 15, "seed": 7}),
    ("pulse_slip", {"days": 7}),
]


# ---------------------------------------------------------------------------
# DAG helpers
# ---------------------------------------------------------------------------


def assert_dag_ok(fleet: Fleet) -> None:
    """Assert acyclicity (Kahn) + 'NO cross-aircraft edges' (C12) + unique ids."""
    by_id = {}
    for t in fleet.tasks:
        assert t.task_id not in by_id, f"duplicate task_id {t.task_id}"
        by_id[t.task_id] = t
    indeg = {tid: 0 for tid in by_id}
    succs = {tid: [] for tid in by_id}
    for t in fleet.tasks:
        for p in t.predecessors:
            assert p in by_id, f"{t.task_id} references missing pred {p}"
            assert by_id[p].aircraft == t.aircraft, (
                f"cross-aircraft edge {p} -> {t.task_id} (C12 violated)"
            )
            succs[p].append(t.task_id)
            indeg[t.task_id] += 1
    frontier = sorted(tid for tid, d in indeg.items() if d == 0)
    seen = 0
    while frontier:
        tid = frontier.pop()
        seen += 1
        for s in succs[tid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                frontier.append(s)
    assert seen == len(by_id), "cycle introduced by mutation"


# ---------------------------------------------------------------------------
# mutation laws
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn_name,params", MUTATION_CASES)
def test_mutation_deterministic(fleet, fn_name, params):
    """Determinism law: identical (fleet, params) => identical mutated fleet."""
    a = MUTATIONS[fn_name](fleet, **params)
    b = MUTATIONS[fn_name](fleet, **params)
    assert a.to_dict() == b.to_dict()


@pytest.mark.parametrize("fn_name,params", MUTATION_CASES)
def test_mutation_pure(fleet, fn_name, params):
    """Purity law: 'The input Fleet is NEVER mutated' — before == after."""
    before = fleet.to_dict()
    MUTATIONS[fn_name](fleet, **params)
    assert fleet.to_dict() == before


@pytest.mark.parametrize("fn_name,params", MUTATION_CASES)
def test_mutation_preserves_dag(fleet, fn_name, params):
    """DAG-safety law: no cycle, no cross-aircraft edge, ids stay unique."""
    mutated = MUTATIONS[fn_name](fleet, **params)
    assert_dag_ok(mutated)


@pytest.mark.parametrize("fn_name,params", MUTATION_CASES)
def test_mutation_stamps_provenance(fleet, fn_name, params):
    """Honesty law (OR-5): mutated fleets carry a bench_overrides stamp
    and keep mock_data: true."""
    mutated = MUTATIONS[fn_name](fleet, **params)
    stamps = mutated.meta.get("bench_overrides", [])
    assert [s["fn"] for s in stamps] == [fn_name]
    assert mutated.meta.get("mock_data") is True


def test_mechanic_absence_one_team_only(fleet):
    """'Remove fraction of ONE team's mechanics' — other rosters untouched,
    at least one mechanic removed from the chosen (largest) team."""
    mutated = mechanic_absence(fleet, team=None, fraction=0.10, seed=7)
    stamp = mutated.meta["bench_overrides"][0]["params"]
    team = stamp["team"]
    before = {m.mech_id for m in fleet.mechanics}
    after = {m.mech_id for m in mutated.mechanics}
    removed = before - after
    assert removed == set(stamp["removed"]) and len(removed) >= 1
    assert all(mid.startswith(team + "-") for mid in removed)
    # every removed mechanic belonged to `team`; the rest of the roster is intact
    assert after == before - removed


def test_skill_shortage_halves_holders(fleet):
    """'Strip skill from HALF of its holders' — exact floor-half count,
    holders keep their other skills, roster size unchanged."""
    skill = "QUAL"
    holders_before = [m for m in fleet.mechanics if skill in m.skills]
    mutated = skill_shortage(fleet, skill=skill, seed=7)
    holders_after = [m for m in mutated.mechanics if skill in m.skills]
    assert len(mutated.mechanics) == len(fleet.mechanics)
    assert len(holders_after) == len(holders_before) - len(holders_before) // 2
    by_id = {m.mech_id: m for m in fleet.mechanics}
    for m in mutated.mechanics:  # only `skill` may disappear, nothing else
        assert set(by_id[m.mech_id].skills) - {skill} <= set(m.skills)


def test_late_parts_marks_blocked(fleet):
    """'fraction of not-started tasks blocked with parts_eta_day' — exact
    count, state+ETA set, nothing else touched."""
    ns_before = sum(1 for t in fleet.tasks if t.state == "not_started")
    mutated = late_parts(fleet, fraction=0.05, eta_day=10, seed=7)
    blocked = [t for t in mutated.tasks if t.state == "blocked"]
    base_blocked = [t for t in fleet.tasks if t.state == "blocked"]
    assert len(blocked) - len(base_blocked) == max(1, int(ns_before * 0.05))
    assert all(t.parts_eta_day == 10 for t in blocked if t.task_id not in
               {b.task_id for b in base_blocked})


def test_rework_wave_production_pattern(fleet):
    """'rework task's predecessor is its parent' (generator rule): each
    injected task is a same-aircraft LEAF inheriting team/skill/crew."""
    mutated = rework_wave(fleet, count=15, seed=7)
    base_ids = {t.task_id for t in fleet.tasks}
    injected = [t for t in mutated.tasks if t.task_id not in base_ids]
    by_id = {t.task_id: t for t in mutated.tasks}
    assert len(injected) == 15
    referenced = {p for t in mutated.tasks for p in t.predecessors}
    for t in injected:
        assert t.is_rework and len(t.predecessors) == 1
        parent = by_id[t.predecessors[0]]
        assert parent.task_id in base_ids
        assert (t.aircraft, t.team, t.skill, t.mechanics_required) == (
            parent.aircraft, parent.team, parent.skill,
            parent.mechanics_required)
        assert 30 <= t.duration_minutes <= 240
        assert t.task_id not in referenced  # leaf: gates nothing


def test_pulse_slip_deadline_math(fleet):
    """'deadlines -7 days, floored at 1' on P-station aircraft + their tasks."""
    mutated = pulse_slip(fleet, days=7)
    before_ac = {a.aircraft: a for a in fleet.aircraft}
    slipped = set()
    for a in mutated.aircraft:
        old = before_ac[a.aircraft]
        if old.station.startswith("P") and old.station[1:].isdigit():
            slipped.add(a.aircraft)
            assert a.delivery_deadline_day == max(1, old.delivery_deadline_day - 7)
        else:
            assert a.delivery_deadline_day == old.delivery_deadline_day
    assert slipped  # mini-fleet stations are P-stations
    before_t = {t.task_id: t for t in fleet.tasks}
    for t in mutated.tasks:
        old_dl = before_t[t.task_id].deadline_day
        want = max(1, old_dl - 7) if t.aircraft in slipped else old_dl
        assert t.deadline_day == want


def test_apply_overrides_unknown_fn_raises(fleet):
    """'a typo in a manifest must never silently no-op' — unknown fn raises."""
    with pytest.raises(ValueError, match="unknown mutation"):
        apply_overrides(fleet, [{"fn": "nope", "params": {}}])


# ---------------------------------------------------------------------------
# runner end-to-end (mini fleet scenario)
# ---------------------------------------------------------------------------


# Measured mini-fleet baseline (2026-07-10): scheduled_rate 0.8083 — the
# uniform-mode mini fleet keeps some pools deliberately too thin
# (crew_exceeds_pool), so ~19% of tasks are honestly reason-coded.
MINI_EXPECTED = {
    "all_scheduled_or_reasoned": True,
    "zero_violations": True,
    "otd_range": [0, 3],
    "scheduled_rate_min": 0.75,
}


def _write_mini_scenario(bench_dir: Path, sid: str = "mini_baseline",
                         overrides: list | None = None,
                         expected: dict | None = None) -> None:
    d = bench_dir / sid
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "id": sid,
        "description": "mini fleet smoke scenario (tests only)",
        "recipe": {"aircraft": 3, "seed": 20260710, "tasks_per_aircraft": 40,
                   "teams": 4, "mechanics": 30,
                   "overrides": overrides or []},
        "budgets": {"schedule_wall_s": 30, "validate_violations": 0},
        "mock_data": True,
        "schema_version": 1,
    }))
    (d / "objective.json").write_text(json.dumps({"metrics": [
        "scheduled_rate", "violations", "fleet_lateness_days", "otd",
        "makespan", "controllable_usd", "wall_s", "reproducibility_hash"]}))
    (d / "expected_validation.json").write_text(
        json.dumps(expected or MINI_EXPECTED))
    (d / "README.md").write_text(f"# {sid}\n\nmock_data: true (tests only)\n")


def _run(bench_dir: Path, out: Path, report: Path, only: str) -> int:
    import tools.bench as bench

    return bench.main([
        "--bench-dir", str(bench_dir), "--out", str(out),
        "--report", str(report), "--only", only,
    ])


def test_runner_mini_baseline_passes(tmp_path):
    """Runner law: a clean scenario yields verdict PASS, exit 0, one
    mock_data-stamped JSONL row, and a report file."""
    bench_dir = tmp_path / "benchmarks"
    _write_mini_scenario(bench_dir)
    out = tmp_path / "results.jsonl"
    report = tmp_path / "report.md"
    assert _run(bench_dir, out, report, "mini_baseline") == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["verdict"] == "PASS"
    assert row["mock_data"] is True
    assert row["metrics"]["violations"] == 0
    assert row["trend"] is None  # first run: nothing to compare against
    assert "no optimality claims" in report.read_text()


def test_runner_hash_stable_and_trend(tmp_path):
    """Reproducibility law: two identical runs produce the SAME
    reproducibility hash; the second row carries a trend block with
    hash_changed=False and zero metric deltas."""
    bench_dir = tmp_path / "benchmarks"
    _write_mini_scenario(bench_dir)
    out = tmp_path / "results.jsonl"
    report = tmp_path / "report.md"
    assert _run(bench_dir, out, report, "mini_baseline") == 0
    assert _run(bench_dir, out, report, "mini_baseline") == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 2
    h1 = rows[0]["metrics"]["reproducibility_hash"]
    h2 = rows[1]["metrics"]["reproducibility_hash"]
    assert h1 == h2 and len(h1) == 64
    trend = rows[1]["trend"]
    assert trend is not None and trend["hash_changed"] is False
    assert all(v == 0 for k, v in trend["delta"].items() if k != "wall_s")


def test_runner_fails_on_broken_property(tmp_path):
    """Gate law: 'exit non-zero on any FAIL' — an impossible property bound
    flips the verdict and the exit code; zero_violations stays intact."""
    bench_dir = tmp_path / "benchmarks"
    _write_mini_scenario(
        bench_dir, sid="mini_impossible",
        expected={"all_scheduled_or_reasoned": True, "zero_violations": True,
                  "otd_range": [999, 1000]})
    out = tmp_path / "results.jsonl"
    rc = _run(bench_dir, out, tmp_path / "report.md", "mini_impossible")
    assert rc == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["verdict"] == "FAIL"
    bad = [c for c in row["properties"] if not c["ok"]]
    assert [c["property"] for c in bad] == ["otd_range"]


def test_runner_mutated_scenario_passes(tmp_path):
    """End-to-end with a mutation: overrides stamp is verified and a
    late_parts mini scenario still schedules validator-clean."""
    bench_dir = tmp_path / "benchmarks"
    _write_mini_scenario(
        bench_dir, sid="mini_late_parts",
        overrides=[{"fn": "late_parts",
                    "params": {"fraction": 0.05, "eta_day": 10, "seed": 7}}],
        expected={"all_scheduled_or_reasoned": True, "zero_violations": True,
                  "otd_range": [0, 3], "blocked_tasks_min": 1})
    out = tmp_path / "results.jsonl"
    rc = _run(bench_dir, out, tmp_path / "report.md", "mini_late_parts")
    row = json.loads(out.read_text().splitlines()[0])
    assert rc == 0 and row["verdict"] == "PASS"
    assert row["overrides_applied"] == ["late_parts"]
