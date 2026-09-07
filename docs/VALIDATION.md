# Measured validation — 2026-09-07

All figures below were measured in this build session against synthetic inputs. No production aircraft or employee data was used.

| Check | Result |
|---|---|
| Original FF_app regression suite | **293 passed** |
| Original `run.py gates` | **PASS**: generation, schedule, envelope export, V1–V9 validation, 293 tests |
| ASTRA integration suite | **23 passed** |
| JavaScript syntax check | **PASS** |
| Original points correlation/adversary gate | **PASS** |
| Original disruption benchmark suite | **6/6 PASS**, zero validator violations |
| Combined app: full-fleet schedule | **50 aircraft; 55,529 tasks; 680 mechanics; all 55,529 scheduled; zero validator violations** |
| Full-fleet pin → actuals → replan → performance | **PASS**: all requests 200, completed work retained in baseline, zero validator violations |
| Preserved FOCUS shells | `/dashboard` and `/dashboard/classic` return 200 |
| Preserved FOCUS schedule feed and authenticated read bridge | Return 200 |

## Integration cases

The 23 tests cover real-state summaries, unauthenticated requests, CSRF and cross-origin rejection, lead scope tampering, mechanic task isolation, actuals/replan/pinned performance, wrong-shift credit rejection, stale revisions, idempotency, guarded done reopening, team-ownership constraints on roster edits, scenario isolation, persisted notes/audit/baselines, formula-safe CSV export, busy-state serialization, invalid parameters, failed actuals persistence rollback, forward-only planning day, restart recovery, server-owned production roles, preserved legacy access boundaries, and rejection of a failed validator result.

## Full-fleet observations

The first combined application run, including original dashboard loading and persistent plan capture:

- Boot: **35.525 seconds**.
- Peak resident memory: **546.6 MiB**.
- Overview: **0.232 seconds**, approximately 9.6 KB JSON.
- First 50-task page: **0.545 seconds**, approximately 21 KB JSON.
- Original full-fleet comparison feed: approximately **16.9 MB JSON**. This inherited payload is why the modern workspace uses server pagination.

A separate full-size closed-loop run pinned a shift baseline, completed one task through the original actuals endpoint, replanned, refreshed the preserved comparison, and confirmed that the completed task still earned baseline credit. The operation sequence took **41.599 seconds**; the new plan had zero validator violations. The task disappeared from the new open-work envelope as expected and remained in the immutable performance baseline.

These are single-environment measurements, not a latency or capacity guarantee.

## Benchmark results

| Original scenario | Scheduled rate | Violations | Runtime |
|---|---:|---:|---:|
| baseline_fleet50 | 1.000000 | 0 | 4.037 s |
| late_parts | 1.000000 | 0 | 3.977 s |
| mechanic_absence | 0.887536 | 0 | 3.411 s |
| pulse_slip | 1.000000 | 0 | 3.923 s |
| rework_wave | 1.000000 | 0 | 3.872 s |
| skill_shortage | 0.511931 | 0 | 2.144 s |

Unscheduled work under shortages is reported honestly; passing the benchmark means its expected constraints and outcome bounds passed, not that every task remained feasible.

The points gate measured flow **236,539** points, chaser **218,524**, slow-roller **236,539**; ordering invariant passed. The attainment/economic correlation was **0.0444**, informational only. It is not evidence of improved real-world performance.

## Limits of verification

- Browser visual and end-to-end UI testing were not requested and were not performed. HTTP integration, rendered HTML responses, static syntax and application-level behavior were checked.
- Docker was unavailable in the execution environment. The container configuration was authored but not built here. The Python application itself was executed and tested.
- No Python cloud host, enterprise SSO, live MES/Teradata/SQL Server feed, or production dataset was connected.
- The repository owner authorized public publication of the integration code and documentation on 2026-09-07. The original FF_app engine remains a pinned private submodule; its source is not included in the public repository.
