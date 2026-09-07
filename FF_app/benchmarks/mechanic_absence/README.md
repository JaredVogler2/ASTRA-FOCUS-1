# Benchmark scenario: `mechanic_absence`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

10% of the largest team's mechanics removed before scheduling (call-out wave).

## What it proves

Full-crew-or-wait under a call-out wave: removing 10% of the largest team's roster (T10, 48 -> 44 mechanics, measured) makes some (team, skill) demands unstaffable. The engine must DELAY or reason-code that work (plus its DAG descendants) — never short-crew or borrow across teams — and stay validator-clean. OTD/lateness here are optimistic: completion days ignore unplaceable tasks (the per-aircraft `unscheduled_tasks` count in stats surfaces this honestly).

## Mutation (ff/bench/mutations.py — pure, deterministic)

mechanic_absence(team=None -> largest roster, fraction=0.10, seed=20260710): seeded sample over the team's sorted roster.

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

scheduled 49284/55529 (rate 0.8875), 6245 reason-coded unscheduled, violations 0, OTD 22/50, fleet lateness 536 days, wall 2.2s.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
