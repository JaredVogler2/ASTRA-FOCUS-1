# Benchmark scenario: `skill_shortage`

> **mock label:** everything this scenario produces is SYNTHETIC
> (`mock_data: true`, stamped from the fleet meta into every results row).
> Economics figures use PLACEHOLDER config rates. Deterministic greedy
> engine — **no optimality claims**.

AVION skill stripped from half its holders fleet-wide (certification lapse).

## What it proves

Reason-coded degradation under a certification lapse: with half the AVION holders gone, unstaffable AVION tasks and their transitive successors leave the schedule with honest reason codes (no_skill_holder / crew_exceeds_pool / cascade) instead of fictional placements. OTD 31 > baseline 19 is the OPTIMISM artifact of unplaceable work not counting toward completion — documented, not hidden.

## Mutation (ff/bench/mutations.py — pure, deterministic)

skill_shortage(skill='AVION', seed=20260710): strips the skill from len(holders)//2 seeded-sampled holders.

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

scheduled 28427/55529 (rate 0.5119), 27102 reason-coded unscheduled, violations 0, OTD 31/50, wall 1.3s. AVION has 168 holders fleet-wide (measured); stripping 84 leaves several teams with no/undersized AVION pools, and every DAG descendant of an unstaffable task cascades the reason.

## Budgets

`schedule_wall_s` <= 60 (engine build
wall, pure Python); `validate_violations` <= 0
(zero tolerance, V1-V9 via `ff.engine.validator` — reused, never rebuilt).
