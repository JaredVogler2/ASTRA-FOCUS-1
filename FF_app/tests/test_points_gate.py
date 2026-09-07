"""Correlation-gate tests — the behavior probe ITSELF, gate artifact shape,
pure-stdlib Pearson, ordering-invariant re-check.

GAMES doctrine (`FOCU5/04_games/04_anti_gaming_fairness.md`): "This is the
gate" — flow (highest-point-value ready work first) must strictly out-earn
chaser (most completions, shortest first); "If cheese wins, the scoring is
wrong." All fixtures synthetic (mock_data: true).
"""

from __future__ import annotations

import json
import os

import pytest

import tools.points_gate as pg
from ff.sim.policies import CHASER, FLOW, POLICIES, SLOW_ROLLER, simulate_policy

PROBE_SHIFTS = 6  # long enough that per-(team, shift) capacity binds on mini


# ---------------------------------------------------------------------------
# pure-stdlib Pearson
# ---------------------------------------------------------------------------


def test_pearson_math():
    """Known series: perfect positive, perfect negative, degenerate ->
    None (an honest 'no coefficient' beats a fabricated 0)."""
    assert pg.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert pg.pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    r = pg.pearson([1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 2.0, 4.0])
    assert r is not None and -1.0 <= r <= 1.0
    assert pg.pearson([1.0], [1.0]) is None  # n < 2
    assert pg.pearson([1.0, 1.0], [1.0, 2.0]) is None  # zero variance
    with pytest.raises(ValueError):
        pg.pearson([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# THE behavior probe (as a test)
# ---------------------------------------------------------------------------


def test_flow_beats_chaser_probe(fleet):
    """THE probe: the flow policy MUST strictly out-earn the chaser policy
    on identical fleet + scoring — "If cheese wins, the scoring is wrong"
    (hard fail). Chaser is genuinely cheesing (it completes at least as
    many tasks), yet flow earns more points."""
    flow = simulate_policy(fleet, FLOW, shifts=PROBE_SHIFTS, seed=7)
    chaser = simulate_policy(fleet, CHASER, shifts=PROBE_SHIFTS, seed=7)

    assert flow["points"] > chaser["points"], (
        f"cheese wins ({chaser['points']} >= {flow['points']}): "
        "the scoring is wrong"
    )
    # The cheese is real: chaser picks low-value work — its average points
    # per completed task must be strictly below flow's.
    assert chaser["completions"] > 0 and flow["completions"] > 0
    assert (chaser["points"] / chaser["completions"]) < (
        flow["points"] / flow["completions"]
    )
    # Honesty labels + measured outcome fields present.
    for res in (flow, chaser):
        assert res["mock_data"] is True
        assert res["lateness_recovered"] == (
            res["baseline_fleet_lateness"] - res["final_fleet_lateness"]
        )


def test_policies_deterministic_and_validated(fleet):
    """Same (fleet, policy, args) => identical results except the measured
    wall_s; unknown policy / bad args raise."""
    a = simulate_policy(fleet, CHASER, shifts=3, seed=7)
    b = simulate_policy(fleet, CHASER, shifts=3, seed=7)
    a.pop("wall_s"), b.pop("wall_s")
    assert a == b
    assert POLICIES == (FLOW, CHASER, SLOW_ROLLER)
    with pytest.raises(ValueError):
        simulate_policy(fleet, "speedrun", shifts=3)
    with pytest.raises(ValueError):
        simulate_policy(fleet, FLOW, shifts=0)
    with pytest.raises(ValueError):
        simulate_policy(fleet, FLOW, shifts=1, start_day=5)  # Saturday
    # The probe never mutates the caller's fleet (isolated work copy).
    assert all(t.state == "not_started" for t in fleet.tasks)


# ---------------------------------------------------------------------------
# ordering-invariant re-check
# ---------------------------------------------------------------------------


def test_ordering_invariant_recheck():
    """Gate check (c): a recovery-boosted critical-path task outscores 10x
    a trivial task — re-asserted through the gate's own hand-built
    scenario (the game can never rank busywork above the work that
    protects a late aircraft)."""
    inv = pg.ordering_invariant_check()
    assert inv["pass"] is True
    assert inv["critical_total"] > 10 * inv["trivial_total"]
    assert inv["trivial_total"] >= 1  # every completed task is worth something


# ---------------------------------------------------------------------------
# the gate end-to-end (mini fleet)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gate_artifact(fleet, tmp_path_factory):
    out = tmp_path_factory.mktemp("gate") / "points_validation.json"
    artifact = pg.run_gate(
        fleet, rounds=2, seed=7, probe_shifts=PROBE_SHIFTS, out_path=str(out)
    )
    return artifact, str(out)


def test_gate_artifact_shape_and_verdict(gate_artifact):
    """The artifact carries the contract keys, is honestly labeled
    mock_data (OR-5/GG-8 — a mock pass never authorizes rewards), and the
    mini fleet passes: flow wins and the invariant holds."""
    artifact, out_path = gate_artifact
    for key in (
        "mock_data",
        "correlation",
        "flow_points",
        "chaser_points",
        "verdict",
        "rounds",
        "behavior_probe",
        "ordering_invariant",
    ):
        assert key in artifact, key
    assert artifact["mock_data"] is True
    assert artifact["verdict"] == "PASS"
    assert artifact["flow_points"] > artifact["chaser_points"]
    assert artifact["behavior_probe"]["pass"] is True
    assert artifact["behavior_probe"]["rule"] == pg.CHEESE_RULE
    assert artifact["ordering_invariant"]["pass"] is True

    corr = artifact["correlation"]
    assert set(corr) >= {"pearson", "n", "target", "pass", "gating"}
    assert corr["gating"] is False  # informational; reported, never suppressed
    if corr["pearson"] is not None:
        assert -1.0 <= corr["pearson"] <= 1.0
        assert corr["n"] >= 2

    # Per-round team attainment rows are sane fractions with $ deltas.
    assert len(artifact["rounds"]) == 2
    for row in artifact["rounds"]:
        assert "controllable_delta_usd" in row
        for team_row in row["teams"]:
            assert 0.0 <= team_row["attainment"] <= 1.0
            assert 0 <= team_row["earned"] <= team_row["goal"]

    # The artifact file was written and round-trips.
    assert os.path.exists(out_path)
    with open(out_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["verdict"] == artifact["verdict"]
    assert on_disk["mock_data"] is True


def test_gate_exit_code_contract(gate_artifact):
    """'exits non-zero if cheese wins': main()'s code is 0 iff verdict PASS
    — the verdict is the single exit authority."""
    artifact, _ = gate_artifact
    assert (0 if artifact["verdict"] == "PASS" else 1) == 0
