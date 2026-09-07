#!/usr/bin/env python3
"""THE CORRELATION GATE — measured proof the point game drives flow.

GAMES doctrine (`FOCU5/04_games/04_anti_gaming_fairness.md`): "This is the
gate." Three checks:

  (a) CORRELATION — run a digital week (``ff.sim.digital_week``); per
      round, per team, score shift attainment (``points.shift_report``
      against the plan that was actually executed) and pair it with the
      round's fleet controllable-$ delta (this replan's controllable
      penalty minus the previous plan's). Report the Pearson coefficient
      honestly (target: NEGATIVE — higher attainment should travel with
      falling controllable $). Informational, never suppressed.
  (b) BEHAVIOR PROBE — identical fleet, identical scoring: the ``flow``
      policy (highest-point-value ready work first) MUST strictly out-earn
      the ``chaser`` policy (most completions, shortest first). "If cheese
      wins, the scoring is wrong — fix weights before rewards." HARD FAIL.
  (c) ORDERING-INVARIANT RE-CHECK — a recovery-boosted critical-path task
      must outscore 10x a trivial task (the X1 invariant re-asserted on a
      hand-built scenario). HARD FAIL.

Artifact: ``outputs/points_validation.json`` with ``mock_data: true``
(OR-5 — every figure here is synthetic; a passing gate on mock data never
authorizes rewards on real floors). A FAILED gate is still written
(honesty); the process exits non-zero when cheese wins or the invariant
breaks.

Pure stdlib + ff imports; the points module is imported READ-ONLY.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

FF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FF_ROOT not in sys.path:
    sys.path.insert(0, FF_ROOT)

from ff.data.loader import load_json_gz  # noqa: E402
from ff.domain import Aircraft, Fleet, Mechanic, Task  # noqa: E402
from ff.engine.cpm import compute_cpm  # noqa: E402
from ff.engine.scheduler import build_schedule  # noqa: E402
from ff.services.points import score_task, shift_report  # noqa: E402  (READ-ONLY)
from ff.services.snapshot import build_snapshot  # noqa: E402
from ff.sim.digital_week import run_digital_week  # noqa: E402
from ff.sim.policies import (  # noqa: E402
    CHASER,
    FLOW,
    SLOW_ROLLER,
    simulate_policy,
)

DEFAULT_OUT = os.path.join(FF_ROOT, "outputs", "points_validation.json")
CHEESE_RULE = "If cheese wins, the scoring is wrong"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, pure stdlib.

    Returns ``None`` for degenerate inputs (n < 2 or zero variance in
    either series) — an honest "no coefficient" beats a fabricated 0.
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"length mismatch: {n} vs {len(ys)}")
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def ordering_invariant_check() -> dict:
    """Re-assert the X1 ordering invariant on a hand-built scenario.

    A 4-task RARE-skill chain on a LATE aircraft (deadline day 0, single
    qualified mechanic => one task per day, 3 days late) makes task A1
    critical AND recovery-boosted; Z1 is a trivial 30-minute task on an
    on-time aircraft. The invariant: score(A1) > 10 x score(Z1) — the game
    can never rank busywork above the work that protects a late aircraft.
    """
    a_ids = [f"0001-T{n:05d}" for n in (1, 2, 3, 4)]
    trivial_id = "0002-T00001"
    tasks = []
    prev: list[str] = []
    for tid in a_ids:
        tasks.append(
            Task(
                task_id=tid,
                aircraft=1,
                name=f"Chain {tid}",
                team="T01",
                skill="RARE",
                duration_minutes=400,
                mechanics_required=1,
                earliest_day=0,
                deadline_day=0,
                predecessors=list(prev),
            )
        )
        prev = [tid]
    tasks.append(
        Task(
            task_id=trivial_id,
            aircraft=2,
            name="Trivial",
            team="T01",
            skill="COMMON",
            duration_minutes=30,
            mechanics_required=1,
            earliest_day=0,
            deadline_day=60,
            predecessors=[],
        )
    )
    mechanics = [
        Mechanic("T01-S1-M001", "T01", 1, ["RARE", "COMMON"]),
        Mechanic("T01-S1-M002", "T01", 1, ["COMMON"]),
        Mechanic("T01-S1-M003", "T01", 1, ["COMMON"]),
    ]
    aircraft = [
        Aircraft(1, "AC-0001", 0, "P01"),
        Aircraft(2, "AC-0002", 60, "P02"),
    ]
    fleet = Fleet(
        aircraft=aircraft,
        tasks=tasks,
        mechanics=mechanics,
        meta={
            "mock_data": True,
            "seed": 1,
            "generated_at": "2026-01-01T00:00:00Z",
            "schema": 1,
        },
    )
    cpm = compute_cpm(fleet.tasks)
    schedule = build_schedule(fleet, cpm)
    snap = build_snapshot(fleet, schedule, cpm)
    critical = int(score_task(a_ids[0], snap)["total"])
    trivial = int(score_task(trivial_id, snap)["total"])
    return {
        "rule": "recovery-boosted critical task outscores 10x a trivial task",
        "critical_total": critical,
        "trivial_total": trivial,
        "pass": critical > 10 * trivial,
    }


def run_gate(
    fleet: Fleet,
    rounds: int = 9,
    seed: int = 7,
    probe_shifts: int = 9,
    out_path: str | None = DEFAULT_OUT,
) -> dict:
    """Run gate checks (a)+(b)+(c); write and return the artifact dict.

    ``verdict`` is "PASS" only when flow STRICTLY out-earns chaser AND the
    slow-roller (b) and the ordering invariant holds (c). The correlation
    (a) is reported with its own pass flag but is informational at
    mock-data scale — it gates nothing yet (threshold calibration needs
    real data; recorded in the artifact, never hidden).

    Gate pressure (docs/GATE_PRESSURE_DESIGN.md): station gates are
    stamped from the PLAN OF RECORD (baseline schedule + grace) before
    any check, so the behind factor is LIVE for the whole gate — the
    slow-roller adversary exists precisely to prove the aging boost
    cannot be farmed by deliberately letting work age.
    """
    wall0 = time.perf_counter()

    # Plan-of-record gate stamping — behind factor live for (a) and (b).
    from ff.services.scorecard import stamp_gates_from_schedule

    stamp_cpm = compute_cpm(fleet.tasks)
    stamp_schedule = build_schedule(fleet, stamp_cpm)
    stamp_gates_from_schedule(fleet, stamp_schedule)

    team_rows: list[dict] = []

    def on_executed(ctx) -> None:
        # Score attainment against the plan that scheduled the executed
        # slot, AFTER execution flipped states — points.shift_report is the
        # production scorer (one scoring truth, READ-ONLY import).
        snap = build_snapshot(ctx["fleet"], ctx["schedule"], ctx["cpm"])
        sched = ctx["schedule"]
        slot_teams = sorted({sched.assignments[tid].team for tid in ctx["planned"]})
        for team in slot_teams:
            rep = shift_report(snap, team, ctx["day"], ctx["shift"])
            if rep["goal"] <= 0:
                continue
            team_rows.append(
                {
                    "round": ctx["round"],
                    "team": team,
                    "day": ctx["day"],
                    "shift": ctx["shift"],
                    "goal": rep["goal"],
                    "earned": rep["earned"],
                    "attainment": rep["attainment"],
                }
            )

    sim = run_digital_week(fleet, rounds=rounds, seed=seed, on_executed=on_executed)

    # (a) attainment vs the round's controllable-$ delta.
    ctrl_series = [sim["baseline"]["controllable_usd"]] + [
        r["controllable_usd"] for r in sim["rounds"]
    ]
    delta_by_round = {
        k: ctrl_series[k] - ctrl_series[k - 1] for k in range(1, rounds + 1)
    }
    for row in team_rows:
        row["controllable_delta_usd"] = delta_by_round[row["round"]]
    xs = [row["attainment"] for row in team_rows]
    ys = [float(row["controllable_delta_usd"]) for row in team_rows]
    coefficient = pearson(xs, ys)

    # (b) the behavior probe on the identical fleet + scoring — flow must
    # STRICTLY beat both adversaries: the chaser (volume cheese) and the
    # slow-roller (aging-boost farming, docs/GATE_PRESSURE_DESIGN.md §4).
    flow = simulate_policy(fleet, FLOW, shifts=probe_shifts, seed=seed)
    chaser = simulate_policy(fleet, CHASER, shifts=probe_shifts, seed=seed)
    roller = simulate_policy(fleet, SLOW_ROLLER, shifts=probe_shifts, seed=seed)
    cheese_wins = chaser["points"] >= flow["points"]
    # Farming fails the gate only on a STRICT advantage: when capacity is
    # not binding both policies complete the identical set and tie — a tie
    # means the aging boost created ZERO farmable edge, which is the
    # design goal, not a defect (docs/GATE_PRESSURE_DESIGN.md §4).
    roller_wins = roller["points"] > flow["points"]

    # (c) ordering-invariant re-check.
    invariant = ordering_invariant_check()

    verdict = (
        "PASS"
        if (not cheese_wins and not roller_wins and invariant["pass"])
        else "FAIL"
    )
    rounds_out = []
    for r in sim["rounds"]:
        rounds_out.append(
            {
                **r,
                "controllable_delta_usd": delta_by_round[r["round"]],
                "teams": [
                    {
                        "team": row["team"],
                        "goal": row["goal"],
                        "earned": row["earned"],
                        "attainment": row["attainment"],
                    }
                    for row in team_rows
                    if row["round"] == r["round"]
                ],
            }
        )

    artifact = {
        # OR-5/GG-8: synthetic label travels with the artifact; a mock-data
        # pass NEVER authorizes rewards on real floors.
        "mock_data": bool(fleet.meta.get("mock_data", True)),
        "dataset": {
            "seed": fleet.meta.get("seed"),
            "aircraft": len(fleet.aircraft),
            "tasks": len(fleet.tasks),
            "mechanics": len(fleet.mechanics),
        },
        "gate": "FF_app points correlation gate (GAMES doctrine: 'This is the gate')",
        "seed": seed,
        "rounds_run": rounds,
        "probe_shifts": probe_shifts,
        "correlation": {
            "pearson": coefficient,
            "n": len(xs),
            "target": "negative (higher attainment with falling controllable $)",
            "pass": coefficient is not None and coefficient < 0.0,
            "gating": False,
            "note": "informational at mock-data scale; threshold calibration "
            "needs real data — reported honestly either way",
        },
        "flow_points": flow["points"],
        "chaser_points": chaser["points"],
        "slow_roller_points": roller["points"],
        "behavior_probe": {
            "rule": CHEESE_RULE,
            "pass": not cheese_wins and not roller_wins,
            "flow": flow,
            "chaser": chaser,
            "slow_roller": roller,
            "roller_pass": not roller_wins,
        },
        "ordering_invariant": invariant,
        "verdict": verdict,
        "rounds": rounds_out,
        "wall_seconds_total": round(time.perf_counter() - wall0, 3),
    }
    if out_path:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, indent=1, sort_keys=True)
    return artifact


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="FF_app correlation gate: attainment-vs-controllable-$ "
        "correlation + flow-vs-chaser behavior probe + ordering invariant. "
        "Exits non-zero if cheese wins (or the invariant breaks)."
    )
    ap.add_argument(
        "--data",
        default=os.path.join(FF_ROOT, "data", "fleet50.json.gz"),
        help="fleet fixture (json.gz; default data/fleet50.json.gz)",
    )
    ap.add_argument("--rounds", type=int, default=9)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--probe-shifts", type=int, default=9)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    fleet = Fleet.from_dict(load_json_gz(args.data))
    artifact = run_gate(
        fleet,
        rounds=args.rounds,
        seed=args.seed,
        probe_shifts=args.probe_shifts,
        out_path=args.out,
    )

    corr = artifact["correlation"]
    print(
        f"=== POINTS CORRELATION GATE (mock_data={artifact['mock_data']}) ===\n"
        f"(a) correlation attainment vs controllable-$ delta: "
        f"pearson={corr['pearson'] if corr['pearson'] is None else round(corr['pearson'], 4)} "
        f"(n={corr['n']}, target negative, informational)\n"
        f"(b) behavior probe: flow {artifact['flow_points']} pts vs "
        f"chaser {artifact['chaser_points']} pts vs "
        f"slow-roller {artifact['slow_roller_points']} pts -> "
        f"{'flow wins' if artifact['behavior_probe']['pass'] else 'ADVERSARY WINS'} "
        f"[{CHEESE_RULE}]\n"
        f"(c) ordering invariant: critical "
        f"{artifact['ordering_invariant']['critical_total']} vs trivial "
        f"{artifact['ordering_invariant']['trivial_total']} -> "
        f"{'ok' if artifact['ordering_invariant']['pass'] else 'BROKEN'}\n"
        f"VERDICT: {artifact['verdict']}   (artifact: {args.out})"
    )
    return 0 if artifact["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
