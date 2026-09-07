#!/usr/bin/env python3
"""FF_app CLI — generate-data / run-schedule / validate / serve / gates.

All target modules are imported LAZILY inside each command handler so that
``run.py --help`` (and unrelated subcommands) keep working during partial
builds. Every command is deterministic given identical inputs and flags.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

MINI_FLEET = "data/mini/fleet.json.gz"
MINI_SCHEDULE = "data/mini/schedule.json.gz"


# ---------------------------------------------------------------------------
# command handlers (lazy imports inside)
# ---------------------------------------------------------------------------


def cmd_generate_data(args: argparse.Namespace) -> int:
    """Generate a synthetic fleet fixture (mock_data: true, OR-5)."""
    from ff.data import loader
    from ff.data.generator import generate_fleet

    fleet = generate_fleet(
        n_aircraft=args.aircraft,
        tasks_per_aircraft=args.tasks_per_aircraft,
        n_teams=args.teams,
        n_mechanics=args.mechanics,
        seed=args.seed,
        task_universe=args.task_universe,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    loader.save_json_gz(fleet.to_dict(), str(out))
    print(
        f"generate-data: wrote {out} "
        f"(aircraft={len(fleet.aircraft)} tasks={len(fleet.tasks)} "
        f"mechanics={len(fleet.mechanics)} seed={args.seed} mock_data=true)"
    )
    return 0


def _export_envelope_for(fleet, schedule, cpm, out_dir, commitments_state):
    """Shared envelope-export step (INCREMENT 6 Part A): update the
    commitments state (MAX doctrine — every engine run advances the
    projection state; §9 hysteresis rules in ff.services.commitments),
    then write the max_v1_*.json.gz envelope for the vendored dashboard."""
    from ff.export.envelope import export_envelope, update_commitments_for_cli
    from ff.services.snapshot import compute_snapshot_id

    run_id = compute_snapshot_id(
        fleet.meta.get("seed") if isinstance(fleet.meta, dict) else None, schedule
    )
    block = update_commitments_for_cli(fleet, schedule, run_id, commitments_state)
    return export_envelope(
        fleet, schedule, cpm, out_dir=out_dir, commitments_block=block
    )


def cmd_run_schedule(args: argparse.Namespace) -> int:
    """Run CPM + the deterministic greedy scheduler on a fleet fixture."""
    from ff.data import loader
    from ff.domain import Fleet, Schedule
    from ff.engine.cpm import compute_cpm
    from ff.engine.scheduler import build_schedule, incumbent_from_schedule

    fleet = Fleet.from_dict(loader.load_json_gz(args.data))
    cpm = compute_cpm(fleet.tasks)
    # §12 COMMITMENT (OR-4): an optional prior schedule whose in-horizon
    # slots/crews the engine defends ("committed work dispatches before new
    # work"). Omitted => incumbent=None => pre-commitment behavior.
    incumbent = None
    if getattr(args, "incumbent", None):
        prior = Schedule.from_dict(loader.load_json_gz(args.incumbent))
        incumbent = incumbent_from_schedule(prior)
    schedule = build_schedule(
        fleet,
        cpm,
        start_day=args.start_day,
        horizon_days=args.horizon_days,
        incumbent=incumbent,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    loader.save_json_gz(schedule.to_dict(), str(out))
    stats = schedule.stats
    commit_note = ""
    if incumbent is not None:
        commit_note = (
            f" commitment_in_horizon={stats.get('commitment_in_horizon')}"
            f" commitment_kept={stats.get('commitment_kept')}"
            f" commitment_mech_kept={stats.get('commitment_mech_kept')}"
        )
    print(
        f"run-schedule: wrote {out} "
        f"(scheduled={stats.get('scheduled')}/{stats.get('total_tasks')} "
        f"unscheduled={stats.get('unscheduled')} "
        f"otd={stats.get('otd_count')} makespan_day={stats.get('makespan_day')} "
        f"wall={stats.get('wall_seconds')}s{commit_note})"
    )
    # INCREMENT 6 Part A: auto-export the max_v1 envelope for the vendored
    # FOCUS dashboard (web_flask). --no-envelope skips; failure is honest
    # and non-fatal (the schedule artifact above is already written).
    if not getattr(args, "no_envelope", False):
        try:
            env_path = _export_envelope_for(
                fleet, schedule, cpm, args.envelope_dir, args.commitments_state
            )
            print(f"run-schedule: exported envelope {env_path}")
        except Exception as exc:
            print(f"run-schedule: WARNING envelope export failed: {exc}")
    return 0


def cmd_export_envelope(args: argparse.Namespace) -> int:
    """Export a max_v1_*.json.gz envelope from saved fleet + schedule."""
    from ff.data import loader
    from ff.domain import Fleet, Schedule
    from ff.engine.cpm import compute_cpm

    fleet = Fleet.from_dict(loader.load_json_gz(args.data))
    schedule = Schedule.from_dict(loader.load_json_gz(args.schedule))
    cpm = compute_cpm(fleet.tasks)
    env_path = _export_envelope_for(
        fleet, schedule, cpm, args.out_dir, args.commitments_state
    )
    print(f"export-envelope: wrote {env_path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run V1..V9 checks on a schedule; exit non-zero on ANY violation."""
    from ff.data import loader
    from ff.domain import Fleet, Schedule
    from ff.engine.validator import validate

    fleet = Fleet.from_dict(loader.load_json_gz(args.data))
    schedule = Schedule.from_dict(loader.load_json_gz(args.schedule))
    report = validate(schedule, fleet)
    violations = report.get("violations", [])
    for check in report.get("checks", []):
        print(f"  check: {check}")
    if violations:
        print(f"validate: FAIL — {len(violations)} violation(s)")
        for v in violations[:25]:
            print(f"  {v.get('id')}: {v.get('task_id')}: {v.get('msg')}")
        if len(violations) > 25:
            print(f"  ... and {len(violations) - 25} more")
        return 1
    print(f"validate: OK — 0 violations ({report.get('summary')})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the Flask app (dev server; use gunicorn for prod/Tanzu)."""
    import config
    from ff.web.app import create_app

    port = config.resolve_port(args.port)
    app = create_app()
    app.run(host="0.0.0.0", port=port)
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    """Quality gate: generate mini fixture -> run-schedule (+ envelope
    export, INCREMENT 6) -> validate -> pytest -q. Each step runs as a
    subprocess; the first non-zero exit fails the gate (exit non-zero).

    The gate's envelope + commitments state go to a THROWAWAY temp dir so
    mini-fleet runs never touch the repo's outputs/schedules or
    data/commitments.json (which hold fleet50-domain state)."""
    import tempfile

    gate_tmp = tempfile.mkdtemp(prefix="ff-gates-")
    gate_env_dir = str(Path(gate_tmp) / "schedules")
    steps: list[tuple[str, list[str]]] = [
        (
            "generate mini fixture",
            [
                sys.executable,
                "run.py",
                "generate-data",
                "--aircraft",
                "3",
                "--tasks-per-aircraft",
                "40",
                # Mini-fixture parameters (contract Tests section + committed
                # data/mini/fleet.json.gz): 4 teams / 30 mechanics. Without
                # these, CLI defaults (20 teams / 600 mechanics) would
                # overwrite the committed fixture with a different variant.
                "--teams",
                "4",
                "--mechanics",
                "30",
                "--seed",
                str(args.seed),
                "--out",
                MINI_FLEET,
            ],
        ),
        (
            "run-schedule (+envelope export)",
            [
                sys.executable,
                "run.py",
                "run-schedule",
                "--data",
                MINI_FLEET,
                "--out",
                MINI_SCHEDULE,
                # INCREMENT 6: envelope + commitments state to the gate's
                # throwaway dir — the repo's fleet50 state stays untouched.
                "--envelope-dir",
                gate_env_dir,
                "--commitments-state",
                str(Path(gate_tmp) / "commitments.json"),
            ],
        ),
        (
            "validate",
            [
                sys.executable,
                "run.py",
                "validate",
                MINI_SCHEDULE,
                "--data",
                MINI_FLEET,
            ],
        ),
        ("pytest -q", [sys.executable, "-m", "pytest", "-q"]),
    ]
    for name, cmd in steps:
        print(f"== gates: {name} ==")
        result = subprocess.run(cmd, cwd=str(APP_ROOT))
        if result.returncode != 0:
            print(f"gates: FAIL at step '{name}' (exit {result.returncode})")
            return result.returncode
        if name.startswith("run-schedule"):
            # INCREMENT 6 export gate: the envelope file must exist and
            # match the vendored dashboard's first-class discovery glob.
            found = sorted(Path(gate_env_dir).glob("max_v1_*.json.gz"))
            if not found:
                print("gates: FAIL — run-schedule exported no max_v1_*.json.gz")
                return 1
            print(f"gates: envelope exported ({found[-1].name})")
    print("gates: ALL PASS")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Defaults are read lazily from config where cheap; config imports only
    # stdlib so importing it here is safe even in partial builds.
    import config

    parser = argparse.ArgumentParser(
        prog="run.py",
        description="FF_app CLI — FOCU5 scheduling application (mock data).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser(
        "generate-data", help="Generate a synthetic fleet fixture (json.gz)."
    )
    p_gen.add_argument("--aircraft", type=int, default=config.GEN_AIRCRAFT)
    p_gen.add_argument(
        "--tasks-per-aircraft",
        type=int,
        default=None,
        help=(
            "Uniform tasks per aircraft (tests/mini fixture). Omit for the "
            "default PULSED-LINE MATURITY mode: aircraft enter at takt and "
            "carry open work by maturity — late-to-delivery punch lists "
            "(0-120 tasks), post-FAL residual (120-900), in-factory ladder "
            "up to --task-universe (6,000+)."
        ),
    )
    p_gen.add_argument(
        "--task-universe", type=int, default=config.GEN_TASK_UNIVERSE
    )
    p_gen.add_argument("--teams", type=int, default=config.GEN_TEAMS)
    p_gen.add_argument("--mechanics", type=int, default=config.GEN_MECHANICS)
    p_gen.add_argument("--seed", type=int, default=config.GEN_SEED)
    p_gen.add_argument("--out", required=True, help="Output path (.json.gz)")
    p_gen.set_defaults(func=cmd_generate_data)

    p_run = sub.add_parser(
        "run-schedule", help="Run CPM + deterministic scheduler on a fixture."
    )
    p_run.add_argument("--data", required=True, help="Fleet fixture (.json.gz)")
    p_run.add_argument("--out", required=True, help="Schedule output (.json.gz)")
    p_run.add_argument("--start-day", type=int, default=0)
    p_run.add_argument(
        "--horizon-days",
        type=int,
        default=None,
        help=f"Scheduling horizon (default: engine minimum {config.HORIZON_MIN_DAYS}+)",
    )
    p_run.add_argument(
        "--incumbent",
        default=None,
        help=(
            "Prior schedule (.json.gz) whose in-horizon slots and crews are "
            "defended (config section 12 COMMITMENT, OR-4). Omit for the "
            "byte-identical no-incumbent behavior."
        ),
    )
    p_run.add_argument(
        "--no-envelope",
        action="store_true",
        help="Skip the automatic max_v1 envelope export after scheduling.",
    )
    p_run.add_argument(
        "--envelope-dir",
        default="outputs/schedules",
        help="Directory for the auto-exported max_v1_*.json.gz envelope.",
    )
    p_run.add_argument(
        "--commitments-state",
        default=os.environ.get("FF_COMMITMENTS", "") or "data/commitments.json",
        help=(
            "Commitments state file advanced by the export step (section 9, "
            "OR-4). Default: FF_COMMITMENTS env or data/commitments.json."
        ),
    )
    p_run.set_defaults(func=cmd_run_schedule)

    p_env = sub.add_parser(
        "export-envelope",
        help=(
            "Export a max_v1_*.json.gz dashboard envelope (INCREMENT 6 Part "
            "A) from a saved fleet + schedule."
        ),
    )
    p_env.add_argument("--data", required=True, help="Fleet fixture (.json.gz)")
    p_env.add_argument("--schedule", required=True, help="Schedule (.json.gz)")
    p_env.add_argument(
        "--out-dir",
        default="outputs/schedules",
        help="Output directory (default outputs/schedules).",
    )
    p_env.add_argument(
        "--commitments-state",
        default=os.environ.get("FF_COMMITMENTS", "") or "data/commitments.json",
        help=(
            "Commitments state file advanced by the export (section 9, "
            "OR-4). Default: FF_COMMITMENTS env or data/commitments.json."
        ),
    )
    p_env.set_defaults(func=cmd_export_envelope)

    p_val = sub.add_parser(
        "validate", help="V1..V9 constraint checks; non-zero exit on violation."
    )
    p_val.add_argument("schedule", help="Schedule file (.json.gz)")
    p_val.add_argument("--data", required=True, help="Fleet fixture (.json.gz)")
    p_val.set_defaults(func=cmd_validate)

    p_serve = sub.add_parser("serve", help="Serve the Flask app (dev server).")
    p_serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Listen port (PORT and FF_PORT env vars take precedence)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_gates = sub.add_parser(
        "gates",
        help="Gate: generate mini fixture -> run-schedule -> validate -> pytest -q.",
    )
    p_gates.add_argument("--seed", type=int, default=config.GEN_SEED)
    p_gates.set_defaults(func=cmd_gates)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
