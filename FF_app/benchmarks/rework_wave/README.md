# Benchmark scenario: `rework_wave`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

200 rework tasks injected via the generator's rework pattern (parent-gated leaves).

## What it proves

Rework injection the production way (cadence_sim2 doctrine, generator rework rule): each injected task is a LEAF gated by its same-aircraft parent, inheriting the parent's team/skill/crew — so the DAG stays acyclic with no cross-aircraft edges, solvability is preserved, and the wave schedules clean.

## Mutation (ff/bench/mutations.py — pure, deterministic)

rework_wave(count=200, seed=20260710): seeded parent sample over sorted live tasks; ids continue each aircraft's -T sequence.

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

scheduled 55729/55729 (rate 1.0; 55529 + 200 injected), violations 0, OTD 19/50, fleet lateness 570 days, wall 2.3s. Lateness ~= baseline: 200 parent-gated leaves are absorbed by pool slack at this scale.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
