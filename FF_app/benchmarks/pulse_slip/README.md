# Benchmark scenario: `pulse_slip`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

All IN_FACTORY (P-station) delivery deadlines pulled in by 7 days.

## What it proves

Deadline sensitivity: pulling every IN_FACTORY (P-station) delivery deadline in by 7 days must tighten CPM slack, reshuffle the ascending-slack dispatch, and show up as measured OTD/lateness degradation — while the schedule itself stays constraint-clean.

## Mutation (ff/bench/mutations.py — pure, deterministic)

pulse_slip(days=7): pure calendar edit on P-station aircraft + their tasks, floored at day 1; no randomness.

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

scheduled 55529/55529 (rate 1.0), violations 0, OTD 17/50 (vs baseline 19 — two in-factory ships lose their margin), fleet lateness 619 days (vs 574), makespan day 430, wall 2.3s.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
