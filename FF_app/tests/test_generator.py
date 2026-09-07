"""Generator laws: determinism, per-aircraft acyclic DAG, solvability, OR-5.

Contract (ARCHITECTURE.md §ff/data/generator.py): deterministic under a
seed, layered per-aircraft DAG with NO cross-aircraft edges (C12), every
demanded (team, skill) pair has a holder (solvability guarantee), durations
fit one shift, and the Fleet is honestly labeled ``mock_data: True``.
"""

from __future__ import annotations

import re

from tests.conftest import MINI_ARGS, MINI_SEED

from ff.data.generator import generate_fleet
from ff.data.loader import load_json_gz, save_json_gz

TASK_ID_RE = re.compile(r"^\d{4}-T\d{5}$")
MECH_ID_RE = re.compile(r"^.+-S[123]-M\d{3}$")


def test_same_seed_identical(fleet):
    """Determinism law: identical arguments => identical Fleet (meta included)."""
    again = generate_fleet(*MINI_ARGS)
    assert again == fleet
    assert again.to_dict() == fleet.to_dict()


def test_same_seed_identical_bytes(fleet, tmp_path):
    """Determinism extends to disk: same seed => byte-identical json.gz."""
    a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    save_json_gz(fleet.to_dict(), str(a))
    save_json_gz(generate_fleet(*MINI_ARGS).to_dict(), str(b))
    assert a.read_bytes() == b.read_bytes()
    assert load_json_gz(str(a)) == fleet.to_dict()


def test_different_seed_differs():
    """Sanity: the seed actually drives the output (no hidden constants)."""
    small_a = generate_fleet(2, 10, 2, 8, MINI_SEED)
    small_b = generate_fleet(2, 10, 2, 8, MINI_SEED + 1)
    assert small_a.to_dict() != small_b.to_dict()


def test_dag_acyclic(fleet):
    """Per-aircraft DAG is acyclic (Kahn's algorithm consumes every task)."""
    by_ac: dict[int, list] = {}
    for t in fleet.tasks:
        by_ac.setdefault(t.aircraft, []).append(t)
    for ac, tasks in sorted(by_ac.items()):
        ids = {t.task_id for t in tasks}
        indeg = {t.task_id: sum(1 for p in set(t.predecessors) if p in ids) for t in tasks}
        succs: dict[str, list[str]] = {t.task_id: [] for t in tasks}
        for t in tasks:
            for p in set(t.predecessors):
                if p in ids:
                    succs[p].append(t.task_id)
        frontier = sorted(tid for tid, d in indeg.items() if d == 0)
        seen = 0
        while frontier:
            tid = frontier.pop()
            seen += 1
            for s in succs[tid]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    frontier.append(s)
        assert seen == len(tasks), f"cycle in aircraft {ac} DAG"


def test_no_cross_aircraft_edges(fleet):
    """C12: predecessors reference ONLY tasks of the same aircraft."""
    ids_by_ac: dict[int, set] = {}
    for t in fleet.tasks:
        ids_by_ac.setdefault(t.aircraft, set()).add(t.task_id)
    for t in fleet.tasks:
        for p in t.predecessors:
            assert p != t.task_id, f"{t.task_id} depends on itself"
            assert p in ids_by_ac[t.aircraft], (
                f"{t.task_id} (aircraft {t.aircraft}) has cross-aircraft/"
                f"dangling predecessor {p}"
            )


def test_solvability_every_demanded_pair_has_holder(fleet):
    """Solvability: every demanded (team, skill) has >=1 holder on >=1 shift,
    and some single-shift pool can field the largest crew demanded (OR-1
    'full crew or wait' must be satisfiable, never permanently infeasible)."""
    pair_max_crew: dict[tuple[str, str], int] = {}
    for t in fleet.tasks:
        key = (t.team, t.skill)
        pair_max_crew[key] = max(pair_max_crew.get(key, 0), t.mechanics_required)

    holders: dict[tuple[str, str, int], int] = {}
    for m in fleet.mechanics:
        for sk in m.skills:
            key = (m.team, sk, m.shift)
            holders[key] = holders.get(key, 0) + 1

    for (team, skill), max_crew in sorted(pair_max_crew.items()):
        per_shift = [holders.get((team, skill, s), 0) for s in (1, 2, 3)]
        assert max(per_shift) >= 1, f"no holder anywhere for ({team}, {skill})"
        assert max(per_shift) >= max_crew, (
            f"({team}, {skill}) demands crew {max_crew} but the biggest "
            f"qualified shift pool holds {max(per_shift)} — statically unstaffable"
        )


def test_shapes_and_mock_data_label(fleet):
    """OR-5 labeling + contract shapes: mock_data True, id formats, durations
    30..430 (fit one shift, no segmentation in v1), crew >= 1."""
    assert fleet.meta.get("mock_data") is True, "OR-5: fleet must be labeled mock_data"
    assert fleet.meta.get("seed") == MINI_SEED
    assert fleet.meta.get("schema") == 1
    assert len(fleet.aircraft) == 3
    assert len(fleet.tasks) == 3 * 40
    assert len(fleet.mechanics) == 30
    for t in fleet.tasks:
        assert TASK_ID_RE.match(t.task_id), t.task_id
        assert 30 <= t.duration_minutes <= 430, (
            f"{t.task_id}: duration {t.duration_minutes} does not fit one shift"
        )
        assert t.mechanics_required >= 1
        assert t.deadline_day >= 1
    for m in fleet.mechanics:
        assert MECH_ID_RE.match(m.mech_id), m.mech_id
        assert m.shift in (1, 2, 3)
        assert m.skills, f"{m.mech_id} has no skills"
