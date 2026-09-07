# Benchmark scenario: `baseline_fleet50`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

Plain fleet50 maturity-mode run, no overrides; anchors every trend line.

## What it proves

The un-disrupted fleet50 anchor: every open task places with a full named crew, the V1-V9 walker is clean, and the OTD / lateness / economics numbers form the trend baseline every disruption scenario is compared against.

## Mutation (ff/bench/mutations.py — pure, deterministic)

None — the raw generator recipe.

## Recipe

Generator: `generate_fleet(aircraft=50, tasks_per_aircraft=None (maturity
mode), teams=20, mechanics=680, seed=20260710, task_universe=6500)`, then
the manifest's `recipe.overrides` applied in order.

## Expected properties (never exact placements)

PROPERTIES only — the engine may legitimately re-place tasks between
versions. `zero_violations` and `all_scheduled_or_reasoned` are absolute
laws and are never weakened. Band policy: `otd_range` = measured +/- 3
aircraft; `fleet_lateness_range` = measured +/- 25%; rate/count bounds
carry modest headroom off the measured value.

## Measured reference

Measured 2026-07-10 (SYNTHETIC mock data, fleet50 maturity mode, seed 20260710, deterministic greedy engine — no optimality claims).

scheduled 55529/55529 (rate 1.0), violations 0, OTD 19/50, fleet lateness 574 days, makespan day 429, controllable $57.4M (placeholder rates), wall 2.6s.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
