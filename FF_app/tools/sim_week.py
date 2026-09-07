#!/usr/bin/env python3
"""Digital-week runner CLI — ``python3 tools/sim_week.py --data ... --out ...``

Runs ``ff.sim.digital_week.run_digital_week`` on a fleet fixture and prints
a per-round summary table (cadence_sim2 house style). All figures are
synthetic (``mock_data`` is stamped from the fleet meta); no optimality
claims. The measured wall seconds are the only non-deterministic fields.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

FF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FF_ROOT not in sys.path:
    sys.path.insert(0, FF_ROOT)

from ff.data.loader import load_json_gz  # noqa: E402
from ff.domain import Fleet  # noqa: E402
from ff.sim.digital_week import (  # noqa: E402
    EXEC_RATE,
    REWORK_PER_ROUND,
    SLIDE_REMAIN,
    run_digital_week,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="FF_app digital-week simulator (execute -> inject rework "
        "-> roll clock -> replan -> measure). Synthetic data only."
    )
    ap.add_argument(
        "--data",
        default=os.path.join(FF_ROOT, "data", "fleet50.json.gz"),
        help="fleet fixture (json.gz; default data/fleet50.json.gz)",
    )
    ap.add_argument("--rounds", type=int, default=9)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--exec-rate", type=float, default=EXEC_RATE)
    ap.add_argument("--slide-remain", type=float, default=SLIDE_REMAIN)
    ap.add_argument("--rework-per-round", type=int, default=REWORK_PER_ROUND)
    ap.add_argument("--start-day", type=int, default=0)
    ap.add_argument("--start-shift", type=int, default=1)
    ap.add_argument(
        "--no-incumbent",
        action="store_true",
        help=(
            "Disable incumbent threading between rounds (the pre-commitment "
            "A/B control arm; default threads each round's prior schedule "
            "per config section 12 COMMITMENT / OR-4)"
        ),
    )
    ap.add_argument("--out", default=None, help="write the result JSON here")
    args = ap.parse_args(argv)

    fleet = Fleet.from_dict(load_json_gz(args.data))
    result = run_digital_week(
        fleet,
        rounds=args.rounds,
        seed=args.seed,
        exec_rate=args.exec_rate,
        slide_remain=args.slide_remain,
        rework_per_round=args.rework_per_round,
        start_day=args.start_day,
        start_shift=args.start_shift,
        thread_incumbent=not args.no_incumbent,
    )

    base = result["baseline"]
    print(
        f"=== digital week (mock_data={result['mock_data']}, "
        f"seed={result['seed']}) ===\n"
        f"baseline: lateness {base['fleet_lateness']} d, otd {base['otd']}, "
        f"controllable ${base['controllable_usd']:,}, "
        f"{base['scheduled']}/{base['total_tasks']} scheduled, "
        f"wall {base['wall_s']}s"
    )
    print(
        f"incumbent threading: "
        f"{'ON' if result['params']['thread_incumbent'] else 'OFF'} "
        f"(config section 12 COMMITMENT, OR-4)"
    )
    print(
        "rnd exec    day? exec slid inj | surv  slot%  mech% ihsurv ihslot% | "
        "lateness otd  controllable$ unsched tasks  wall"
    )
    for r in result["rounds"]:
        print(
            f"{r['round']:>3} d{r['exec_day']:>3}S{r['exec_shift']} "
            f"{'DAY' if r['day_rolled'] else '   '} "
            f"{r['executed']:>4} {r['slid']:>4} {r['injected']:>3} | "
            f"{r['surviving']:>5} {r['slot_stable_pct']:>6} "
            f"{r['mech_stable_pct']:>6} {r['in_horizon_survivors']:>6} "
            f"{r['in_horizon_slot_stable_pct']:>7} | "
            f"{r['fleet_lateness']:>8} {r['otd']:>3} "
            f"{r['controllable_usd']:>14,} {r['unscheduled']:>7} "
            f"{r['total_tasks']:>6} {r['wall_s']:>5.1f}s"
        )
    print(f"total wall: {result['wall_seconds_total']}s")

    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, sort_keys=True)
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
