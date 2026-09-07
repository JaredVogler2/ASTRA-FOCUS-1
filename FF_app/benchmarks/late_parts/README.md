# Benchmark scenario: `late_parts`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

5% of not-started tasks parts-blocked with ETA day 10 (supplier slip).

## What it proves

The parts-ETA floor end to end: 5% of not-started tasks become blocked with parts_eta_day=10; the scheduler floors them (and their successors) at day 10, V9 confirms no assignment starts earlier, and the measured lateness delta prices the slip.

## Mutation (ff/bench/mutations.py — pure, deterministic)

late_parts(fraction=0.05, eta_day=10, seed=20260710): seeded sample over sorted not-started tasks.

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

scheduled 55529/55529 (rate 1.0), violations 0 (V9 parts-ETA floor clean), 2776 tasks blocked with ETA day 10, OTD 19/50, fleet lateness 664 days (vs baseline 574 — the supplier slip costs ~90 aircraft-days), wall 2.4s.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
