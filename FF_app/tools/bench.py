#!/usr/bin/env python3
"""FF_app benchmark runner — executes the ``benchmarks/`` scenario corpus.

Per the B1 governing prompt (adapted to FF_app): "did the engine get better
or worse?" must be a measured, reproducible question. A scenario PASSES only
when it is valid AND constraint-clean AND property-thresholded AND
runtime-bounded — never merely because code executed.

Per scenario (all of ``benchmarks/`` or ``--only ID[,ID...]``):

1. Build the fleet from the manifest's generator recipe
   (``ff.data.generator.generate_fleet`` — deterministic, mock_data:true)
   and apply its overrides through ``ff.bench.mutations`` (pure functions).
2. ``compute_cpm`` + ``build_schedule`` (wall-clocked).
3. ``ff.engine.validator.validate`` — REUSED verbatim, never rebuilt.
4. Score the objective metrics (scheduled_rate, violations,
   fleet_lateness_days, otd, makespan, controllable_usd, wall_s,
   reproducibility_hash = sha256 over the sorted assignment tuples).
5. Check ``expected_validation.json`` PROPERTIES (never exact placements)
   and the manifest budgets -> PASS/FAIL.
6. Append one mock_data-stamped JSONL row to ``outputs/bench_results.jsonl``
   including a trend block vs the previous row for the same scenario id,
   and write a human report to ``outputs/bench_report.md``.

Exit code: non-zero if ANY scenario FAILs. Honesty (OR-5): every row and
the report are stamped ``mock_data`` verbatim from the fleet meta; economics
figures are placeholder-rate derived; NO optimality claims anywhere.

Determinism: the runner itself uses no randomness; all seeds come from the
manifests. Two identical invocations differ only in wall-clock fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import config  # noqa: E402
from ff.bench.mutations import apply_overrides  # noqa: E402
from ff.data.generator import generate_fleet  # noqa: E402
from ff.domain import Fleet, Schedule  # noqa: E402
from ff.engine.cpm import compute_cpm  # noqa: E402
from ff.engine.scheduler import UNSCHEDULED_REASONS, build_schedule  # noqa: E402
from ff.engine.validator import validate  # noqa: E402

DEFAULT_BENCH_DIR = APP_ROOT / "benchmarks"
DEFAULT_OUT = APP_ROOT / "outputs" / "bench_results.jsonl"
DEFAULT_REPORT = APP_ROOT / "outputs" / "bench_report.md"

#: Property keys expected_validation.json may use. Unknown keys FAIL the
#: scenario (a typo must never silently pass — B1 gotcha).
KNOWN_PROPERTIES = (
    "all_scheduled_or_reasoned",
    "zero_violations",
    "otd_range",
    "total_tasks",
    "scheduled_rate_min",
    "unscheduled_max",
    "fleet_lateness_range",
    "blocked_tasks_min",
)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def reproducibility_hash(schedule: Schedule) -> str:
    """sha256 over the SORTED per-task assignment tuples.

    Hashes only semantic placement fields (task, slot, minute window, named
    crew, team, skill, overtime flag) — never wall-clock-bearing stats — so
    identical engine decisions hash identically across runs (B1 gotcha:
    "reproducibility hash flakes").
    """
    rows = sorted(
        (
            a.task_id,
            a.day,
            a.shift,
            a.start_minute,
            a.end_minute,
            ",".join(a.mechanic_ids),
            a.team,
            a.skill,
            bool(a.uses_overtime),
        )
        for a in schedule.assignments.values()
    )
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def score_metrics(fleet: Fleet, schedule: Schedule, report: dict, wall_s: float) -> dict:
    """Assemble the objective metric row from engine stats + validator report.

    Everything is REUSED from Schedule.stats (economics/capacity are wired
    by the engine itself) and the validator summary — nothing re-derived.
    ``scheduled_rate`` is over LIVE tasks (total - done): done work needs no
    placement and must not inflate the rate.
    """
    stats = schedule.stats
    live = stats["total_tasks"] - stats.get("done_tasks", 0)
    return {
        "total_tasks": stats["total_tasks"],
        "scheduled": stats["scheduled"],
        "unscheduled": stats["unscheduled"],
        "scheduled_rate": round(stats["scheduled"] / live, 6) if live else 1.0,
        "violations": report["summary"]["total"],
        "fleet_lateness_days": stats["fleet_lateness_days"],
        "otd": stats["otd_count"],
        "aircraft_count": len(fleet.aircraft),
        "makespan": stats["makespan_day"],
        "controllable_usd": stats["economics"].get("controllable_penalty_usd"),
        "economics_source": stats["economics"].get("source"),
        "wall_s": round(wall_s, 3),
        "engine_wall_s": stats.get("wall_seconds"),
        "reproducibility_hash": reproducibility_hash(schedule),
    }


def check_properties(
    expected: dict, fleet: Fleet, schedule: Schedule, metrics: dict
) -> list[dict]:
    """Evaluate expected_validation PROPERTIES against measured results.

    Properties describe qualities (zero violations, OTD band, everything
    scheduled-or-reasoned), never exact placements — the engine may
    legitimately re-place tasks between versions.
    """
    checks: list[dict] = []

    def add(name: str, expected_val, measured, ok: bool) -> None:
        checks.append(
            {"property": name, "expected": expected_val, "measured": measured,
             "ok": bool(ok)}
        )

    stats = schedule.stats
    for name in sorted(expected):
        want = expected[name]
        if name not in KNOWN_PROPERTIES:
            add(name, want, None, False)  # unknown property never passes
            continue
        if name == "all_scheduled_or_reasoned":
            accounted = (
                stats["scheduled"] + stats["unscheduled"] + stats.get("done_tasks", 0)
                == stats["total_tasks"]
            )
            reasons_ok = all(
                r in UNSCHEDULED_REASONS for r in schedule.unscheduled.values()
            )
            measured = {
                "accounted": accounted,
                "unknown_reasons": sorted(
                    set(schedule.unscheduled.values()) - set(UNSCHEDULED_REASONS)
                ),
            }
            add(name, want, measured, (accounted and reasons_ok) == want)
        elif name == "zero_violations":
            add(name, want, metrics["violations"],
                (metrics["violations"] == 0) == want)
        elif name == "otd_range":
            lo, hi = want
            add(name, want, metrics["otd"], lo <= metrics["otd"] <= hi)
        elif name == "total_tasks":
            add(name, want, metrics["total_tasks"], metrics["total_tasks"] == want)
        elif name == "scheduled_rate_min":
            add(name, want, metrics["scheduled_rate"],
                metrics["scheduled_rate"] >= want)
        elif name == "unscheduled_max":
            add(name, want, metrics["unscheduled"], metrics["unscheduled"] <= want)
        elif name == "fleet_lateness_range":
            lo, hi = want
            add(name, want, metrics["fleet_lateness_days"],
                lo <= metrics["fleet_lateness_days"] <= hi)
        elif name == "blocked_tasks_min":
            blocked = sum(1 for t in fleet.tasks if t.state == "blocked")
            add(name, want, blocked, blocked >= want)
    return checks


def check_budgets(budgets: dict, metrics: dict) -> list[dict]:
    """Budget gates from the manifest: wall-time bound + zero-violation law.

    ``validate_violations`` must be 0 in every manifest (the zero-violations
    property is never weakened); the runner enforces whatever the manifest
    states as a <= bound.
    """
    checks: list[dict] = []
    if "schedule_wall_s" in budgets:
        limit = budgets["schedule_wall_s"]
        checks.append(
            {"budget": "schedule_wall_s", "limit": limit,
             "measured": metrics["wall_s"], "ok": metrics["wall_s"] <= limit}
        )
    if "validate_violations" in budgets:
        limit = budgets["validate_violations"]
        checks.append(
            {"budget": "validate_violations", "limit": limit,
             "measured": metrics["violations"],
             "ok": metrics["violations"] <= limit}
        )
    return checks


def trend_vs_previous(out_path: Path, scenario_id: str, metrics: dict) -> dict | None:
    """Delta block vs the LAST prior results line for the same scenario id."""
    if not out_path.exists():
        return None
    previous = None
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("scenario_id") == scenario_id:
                previous = row
    if previous is None:
        return None
    prev_metrics = previous.get("metrics", {})
    delta = {}
    for key in ("scheduled_rate", "violations", "fleet_lateness_days", "otd",
                "makespan", "controllable_usd", "wall_s"):
        old = prev_metrics.get(key)
        new = metrics.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta[key] = round(new - old, 6)
    return {
        "prev_ts_utc": previous.get("ts_utc"),
        "prev_verdict": previous.get("verdict"),
        "delta": delta,
        "hash_changed": prev_metrics.get("reproducibility_hash")
        != metrics.get("reproducibility_hash"),
    }


# ---------------------------------------------------------------------------
# scenario execution
# ---------------------------------------------------------------------------


def load_scenario(scenario_dir: Path) -> dict:
    """Load and minimally validate one scenario directory."""
    manifest = json.loads((scenario_dir / "manifest.json").read_text())
    objective = json.loads((scenario_dir / "objective.json").read_text())
    expected = json.loads((scenario_dir / "expected_validation.json").read_text())
    for key in ("id", "description", "recipe", "budgets"):
        if key not in manifest:
            raise ValueError(f"{scenario_dir.name}: manifest missing {key!r}")
    if manifest["id"] != scenario_dir.name:
        raise ValueError(
            f"{scenario_dir.name}: manifest id {manifest['id']!r} != dir name"
        )
    return {"manifest": manifest, "objective": objective, "expected": expected}


def build_fleet(recipe: dict, cache: dict) -> Fleet:
    """Generator recipe -> (cached) base fleet -> mutated copy via overrides.

    The base fleet is cached per recipe tuple within one invocation —
    mutations are pure (they deep-copy), so sharing the base is safe.
    """
    key = (
        recipe["aircraft"],
        recipe["seed"],
        recipe.get("tasks_per_aircraft"),
        recipe.get("teams", config.GEN_TEAMS),
        recipe.get("mechanics", config.GEN_MECHANICS),
        recipe.get("task_universe", config.GEN_TASK_UNIVERSE),
    )
    if key not in cache:
        cache[key] = generate_fleet(
            n_aircraft=key[0],
            tasks_per_aircraft=key[2],
            n_teams=key[3],
            n_mechanics=key[4],
            seed=key[1],
            task_universe=key[5],
        )
    return apply_overrides(cache[key], recipe.get("overrides", []))


def run_scenario(scenario_dir: Path, out_path: Path, cache: dict) -> dict:
    """Execute one scenario end-to-end; return its results row (not yet written)."""
    bundle = load_scenario(scenario_dir)
    manifest = bundle["manifest"]
    recipe = manifest["recipe"]

    fleet = build_fleet(recipe, cache)

    # Structural honesty check: the mutations actually ran (B1 gotcha —
    # "variant passes without doing anything").
    wanted_fns = [o["fn"] for o in recipe.get("overrides", [])]
    applied_fns = [o["fn"] for o in fleet.meta.get("bench_overrides", [])]
    overrides_ok = wanted_fns == applied_fns

    cpm = compute_cpm(fleet.tasks)
    wall_start = time.perf_counter()
    schedule = build_schedule(fleet, cpm)
    wall_s = time.perf_counter() - wall_start
    report = validate(schedule, fleet)  # REUSED validator, never rebuilt

    metrics = score_metrics(fleet, schedule, report, wall_s)
    prop_checks = check_properties(bundle["expected"], fleet, schedule, metrics)
    budget_checks = check_budgets(manifest["budgets"], metrics)
    verdict = (
        "PASS"
        if overrides_ok
        and all(c["ok"] for c in prop_checks)
        and all(c["ok"] for c in budget_checks)
        else "FAIL"
    )

    # Record only the metrics the objective declares (plus the always-on
    # identity fields), so objective.json stays the single scoring contract.
    declared = objective_metrics(bundle["objective"], metrics)
    row = {
        "scenario_id": manifest["id"],
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # OR-5: stamped VERBATIM from the fleet meta, never assumed.
        "mock_data": bool(fleet.meta.get("mock_data")),
        "seed": recipe["seed"],
        "overrides_applied": applied_fns,
        "overrides_ok": overrides_ok,
        "metrics": declared,
        "properties": prop_checks,
        "budgets": budget_checks,
        "verdict": verdict,
        "trend": trend_vs_previous(out_path, manifest["id"], declared),
        "notes": "synthetic mock data; placeholder economics rates; "
                 "deterministic greedy engine — no optimality claims",
    }
    return row


def objective_metrics(objective: dict, metrics: dict) -> dict:
    """Project the measured metrics onto the objective's declared list."""
    names = objective.get("metrics", [])
    unknown = [n for n in names if n not in metrics]
    if unknown:
        raise ValueError(f"objective declares unknown metrics: {unknown}")
    declared = {name: metrics[name] for name in names}
    # Identity/accounting fields always ride along for the trend + report.
    for extra in ("total_tasks", "scheduled", "unscheduled", "aircraft_count",
                  "economics_source", "engine_wall_s"):
        declared.setdefault(extra, metrics[extra])
    return declared


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def write_report(rows: list[dict], report_path: Path) -> None:
    """Write the human-readable measured report for THIS invocation's rows."""
    lines = [
        "# FF_app benchmark report",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        "by `tools/bench.py`.",
        "",
        "**Honesty:** all figures below are SYNTHETIC (`mock_data: true`, "
        "stamped per row from the fleet meta); economics use PLACEHOLDER "
        "config rates (`source: config-defaults`); the engine is a "
        "deterministic greedy scheduler — **no optimality claims**.",
        "",
        "| scenario | verdict | tasks | scheduled_rate | violations | otd | "
        "lateness_d | makespan | controllable_usd | wall_s | hash |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        m = row["metrics"]
        lines.append(
            f"| {row['scenario_id']} | {row['verdict']} | {m['total_tasks']} "
            f"| {m['scheduled_rate']} | {m['violations']} | {m['otd']}"
            f"/{m['aircraft_count']} | {m['fleet_lateness_days']} "
            f"| {m['makespan']} | {m['controllable_usd']} | {m['wall_s']} "
            f"| {m['reproducibility_hash'][:12]} |"
        )
    lines.append("")
    for row in rows:
        lines.append(f"## {row['scenario_id']} — {row['verdict']}")
        lines.append("")
        lines.append(f"- mock_data: {row['mock_data']}; seed: {row['seed']}; "
                     f"overrides: {row['overrides_applied'] or 'none'}")
        for c in row["properties"]:
            lines.append(
                f"- property `{c['property']}`: expected {c['expected']}, "
                f"measured {c['measured']} -> {'ok' if c['ok'] else 'FAIL'}"
            )
        for c in row["budgets"]:
            lines.append(
                f"- budget `{c['budget']}`: limit {c['limit']}, measured "
                f"{c['measured']} -> {'ok' if c['ok'] else 'FAIL'}"
            )
        trend = row.get("trend")
        if trend:
            lines.append(
                f"- trend vs {trend['prev_ts_utc']}: delta {trend['delta']}; "
                f"hash_changed: {trend['hash_changed']}"
            )
        lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description="FF_app benchmark runner (synthetic mock data; "
        "no optimality claims).",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated scenario id(s) to run (default: all).",
    )
    parser.add_argument(
        "--bench-dir",
        default=str(DEFAULT_BENCH_DIR),
        help="Scenario corpus directory (default: benchmarks/).",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Results JSONL path (default: outputs/bench_results.jsonl).",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help="Markdown report path (default: outputs/bench_report.md).",
    )
    args = parser.parse_args(argv)

    bench_dir = Path(args.bench_dir)
    out_path = Path(args.out)
    scenario_dirs = sorted(
        d for d in bench_dir.iterdir() if (d / "manifest.json").is_file()
    ) if bench_dir.is_dir() else []
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        by_name = {d.name: d for d in scenario_dirs}
        missing = [w for w in wanted if w not in by_name]
        if missing:
            print(f"bench: unknown scenario id(s): {missing}", file=sys.stderr)
            return 2
        scenario_dirs = [by_name[w] for w in wanted]
    if not scenario_dirs:
        print(f"bench: no scenarios found under {bench_dir}", file=sys.stderr)
        return 2

    cache: dict = {}
    rows: list[dict] = []
    any_fail = False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for scenario_dir in scenario_dirs:
        row = run_scenario(scenario_dir, out_path, cache)
        rows.append(row)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        m = row["metrics"]
        print(
            f"bench: {row['scenario_id']}: {row['verdict']} "
            f"(scheduled_rate={m['scheduled_rate']} violations={m['violations']} "
            f"otd={m['otd']}/{m['aircraft_count']} "
            f"lateness={m['fleet_lateness_days']} wall={m['wall_s']}s "
            f"hash={m['reproducibility_hash'][:12]} mock_data={row['mock_data']})"
        )
        if row["trend"]:
            print(f"       trend: {row['trend']['delta']} "
                  f"hash_changed={row['trend']['hash_changed']}")
        if row["verdict"] != "PASS":
            any_fail = True
            for c in row["properties"] + row["budgets"]:
                if not c["ok"]:
                    print(f"       FAIL detail: {c}", file=sys.stderr)
            if not row["overrides_ok"]:
                print("       FAIL detail: overrides stamp mismatch",
                      file=sys.stderr)

    write_report(rows, Path(args.report))
    print(f"bench: {'FAIL' if any_fail else 'ALL PASS'} — "
          f"{len(rows)} scenario(s); results -> {out_path}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
