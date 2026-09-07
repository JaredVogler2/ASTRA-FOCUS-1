"""Dispatch policies for the behavior probe — ``flow`` vs ``chaser`` vs
``slow_roller``.

The X4/GAMES probe set (FOCU5 `04_games/04_anti_gaming_fairness.md`):

- ``flow``   — complete the highest-POINT-VALUE ready tasks first (the
  behavior the game is supposed to reward). Point values come from
  ``ff.services.points.score_task`` — the production scoring module,
  imported READ-ONLY; this module never reimplements factor math.
- ``chaser`` — complete the MOST tasks: shortest duration first (the
  point-chasing cheese). "If cheese wins, the scoring is wrong."
- ``slow_roller`` — the GATE-PRESSURE adversary (docs/
  GATE_PRESSURE_DESIGN.md §4): deliberately WITHHOLDS work whose
  behind-schedule boost is still appreciating (past its station gate but
  below BEHIND_CAP_DAYS), does everything else first, then harvests the
  aged jobs at maximum boost in the last ``HARVEST_SHIFTS`` slots. If
  slow-rolling ever out-earns flow, the aging boost is miscalibrated —
  HARD FAIL in the correlation gate.

Day-aware scoring: the behind factor is the ONE component whose raw
depends on "today", and it is a pass-through (no percentile normalizer),
so per-slot scores are computed EXACTLY as static-components-minus-
static-behind-plus-behind(today) against the frozen baseline snapshot —
every other factor is day-independent under frozen normalizers. All
three policies are graded on this identical dynamic scale (flow banks
aged work promptly at today's boost; the roller gambles on tomorrow's).

``simulate_policy(fleet, policy, shifts=9, seed=7)`` executes ready tasks
per policy within per-(team, shift) capacity-minutes across ``shifts``
consecutive eligible slots, then replans with the REAL engine to measure
the fleet outcome (lateness recovered vs the baseline plan).

Policies only pick EXECUTION ORDER within capacity — they never move
slots, never invent placements, never jump the DAG (both policies complete
READY work only: all predecessors done). The engine replan is the
scheduler; the probe compares what each behavior does to the fleet.

Determinism: execution under a policy is fully deterministic (sorted
iteration, tie-break by task_id, frozen per-snapshot score normalizers).
``seed`` is recorded for provenance/parity with ``run_digital_week`` —
no stochastic surface exists in this probe. Honesty (OR-5): the result
carries ``mock_data`` from the fleet meta.
"""

from __future__ import annotations

import time

import config
from ff.domain import Fleet, Task, shift_eligible
from ff.engine.cpm import compute_cpm
from ff.engine.scheduler import build_schedule
from ff.services.points import score_task  # READ-ONLY import (one scoring truth)
from ff.services.snapshot import build_snapshot
from ff.sim.digital_week import advance_clock

FLOW = "flow"
CHASER = "chaser"
SLOW_ROLLER = "slow_roller"
POLICIES = (FLOW, CHASER, SLOW_ROLLER)

# Slow-roller harvest window: in the final HARVEST_SHIFTS of the probe the
# roller stops withholding and cashes out its deliberately-aged work.
HARVEST_SHIFTS = 2


def _capacity_minutes(pool_count: int, shift: int) -> int:
    """Per-(team, shift) execution budget in crew-minutes.

    Mirrors the scheduler's supply frame: headcount x SHIFT_EFFECTIVE x
    UTILIZATION (the activation-quota fraction) — the minutes a team can
    actually burn in one shift.
    """
    return int(pool_count * config.SHIFT_EFFECTIVE[shift] * config.UTILIZATION)


def simulate_policy(
    fleet: Fleet,
    policy: str,
    shifts: int = 9,
    seed: int = 7,
    start_day: int = 0,
    start_shift: int = 1,
) -> dict:
    """Execute ``shifts`` slots under ``policy``; return points + fleet outcome.

    Per slot, per team (sorted): the policy orders the team's READY tasks
    (state ``not_started``, all predecessors done — no DAG jumping, so the
    out-of-sequence penalty never applies to either policy) and completes
    them greedily while ``duration x crew`` fits the remaining capacity
    budget; completions unlock successors within the same slot (ready sets
    are maintained incrementally and re-ordered until a full pass completes
    nothing). Earned points are scored BEFORE completion via the production
    ``score_task`` against ONE snapshot with frozen normalizers, so both
    policies are graded on the identical scale.

    Returns::

        {"policy", "seed", "shifts", "slots": [[day, shift], ...],
         "points", "completions", "effort_minutes",
         "baseline_fleet_lateness", "final_fleet_lateness",
         "lateness_recovered", "baseline_otd", "final_otd",
         "mock_data", "wall_s"}

    ``lateness_recovered = baseline - final`` (positive = the behavior
    helped the fleet), both measured by REAL engine replans. Raises
    ``ValueError`` on an unknown policy or ineligible start slot.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")
    if shifts < 1:
        raise ValueError(f"shifts must be >= 1, got {shifts}")
    if not shift_eligible(start_day, start_shift):
        raise ValueError(
            f"start slot (day={start_day}, shift={start_shift}) is not eligible"
        )
    wall0 = time.perf_counter()

    # Isolated work copy — the caller's fleet is never mutated.
    work = Fleet.from_dict(fleet.to_dict())
    by_id: dict[str, Task] = {t.task_id: t for t in work.tasks}

    cpm = compute_cpm(work.tasks)
    baseline_schedule = build_schedule(work, cpm, start_day=start_day)
    snap = build_snapshot(work, baseline_schedule, cpm)
    baseline_lateness = baseline_schedule.stats["fleet_lateness_days"]
    baseline_otd = baseline_schedule.stats["otd_count"]

    # Live-precedence graph over non-done tasks (done pred = satisfied,
    # missing/cross-universe pred = satisfied — mirrors the scheduler).
    pending: dict[str, int] = {}
    succs: dict[str, list[str]] = {}
    for tid in sorted(by_id):
        task = by_id[tid]
        if task.state == "done":
            continue
        preds = [
            p
            for p in sorted(set(task.predecessors))
            if p != tid and p in by_id and by_id[p].state != "done"
        ]
        pending[tid] = len(preds)
        for p in preds:
            succs.setdefault(p, []).append(tid)

    ready: dict[str, set[str]] = {}
    for tid in sorted(pending):
        task = by_id[tid]
        if pending[tid] == 0 and task.state == "not_started":
            ready.setdefault(task.team, set()).add(tid)

    # Roster headcount per (team, shift) — deduped like the scheduler.
    pool_count: dict[tuple[str, int], int] = {}
    seen_mechs: set[str] = set()
    for mech in sorted(work.mechanics, key=lambda m: m.mech_id):
        if mech.mech_id in seen_mechs:
            continue
        seen_mechs.add(mech.mech_id)
        key = (mech.team, mech.shift)
        pool_count[key] = pool_count.get(key, 0) + 1
    teams = sorted({team for team, _shift in pool_count})

    # Static score components cached once (frozen normalizers); the behind
    # term — the only day-dependent, pass-through component — is recomputed
    # exactly per slot day (module docstring: day-aware scoring).
    score_cache: dict[str, tuple[int, int, int]] = {}  # (total, behind_pts, effort)

    def _static(tid: str) -> tuple[int, int, int]:
        cached = score_cache.get(tid)
        if cached is None:
            s = score_task(tid, snap)
            cached = (
                int(s["total"]),
                int(s["components"].get("behind", {}).get("points", 0)),
                int(s["effort"]),
            )
            score_cache[tid] = cached
        return cached

    def _behind_pts(tid: str, today: int) -> int:
        task = by_id[tid]
        if task.gate_day is None:
            return 0
        raw = min(
            max(0, today - int(task.gate_day))
            / float(max(1, config.BEHIND_CAP_DAYS)),
            1.0,
        )
        _total, _b, effort = _static(tid)
        return int(round(effort * (config.W_BEHIND / 100.0) * raw))

    def score_of(tid: str, today: int) -> int:
        total, behind0, _effort = _static(tid)
        return total - behind0 + _behind_pts(tid, today)

    def _appreciating(tid: str, today: int) -> bool:
        """True while deferring ``tid`` would grow its behind boost — the
        roller's withhold set. Includes PRE-gate work (a true farmer lets
        fresh work age INTO the boost, not just ride an existing one):
        any gated task whose boost has not yet saturated appreciates."""
        gate = by_id[tid].gate_day
        return gate is not None and (today - int(gate)) < config.BEHIND_CAP_DAYS

    points = 0
    completions = 0
    effort_minutes = 0
    slots: list[list[int]] = []
    day, shift = start_day, start_shift

    for k in range(shifts):
        slots.append([day, shift])
        harvest = (shifts - k) <= HARVEST_SHIFTS  # roller cash-out window
        for team in teams:
            budget = _capacity_minutes(pool_count.get((team, shift), 0), shift)
            if budget <= 0:
                continue
            while budget > 0:
                avail = ready.get(team)
                if not avail:
                    break
                if policy == FLOW:
                    order = sorted(
                        avail, key=lambda t: (-score_of(t, day), t)
                    )
                elif policy == SLOW_ROLLER and not harvest:
                    # Withhold appreciating aged work (sorted LAST); do the
                    # non-appreciating work first, by value.
                    order = sorted(
                        avail,
                        key=lambda t: (
                            1 if _appreciating(t, day) else 0,
                            -score_of(t, day),
                            t,
                        ),
                    )
                elif policy == SLOW_ROLLER:
                    order = sorted(  # harvest: cash out by value
                        avail, key=lambda t: (-score_of(t, day), t)
                    )
                else:  # CHASER: most tasks — shortest first
                    order = sorted(
                        avail, key=lambda t: (by_id[t].duration_minutes, t)
                    )
                progressed = False
                for tid in order:
                    task = by_id[tid]
                    # The roller REORDERS (appreciating work sorted last) but
                    # never idles bookable capacity — an idling farmer loses
                    # trivially; the interesting adversary reorders so aged
                    # work is harvested at max boost while capacity stays
                    # full. No hard skip here by design.
                    cost = task.duration_minutes * task.mechanics_required
                    if cost > budget:
                        continue  # does not fit; try the next in policy order
                    earned = score_of(tid, day)  # scored BEFORE completion
                    task.state = "done"
                    task.remaining_minutes = None
                    avail.discard(tid)
                    points += earned
                    completions += 1
                    effort_minutes += cost
                    budget -= cost
                    for succ in succs.get(tid, ()):
                        pending[succ] -= 1
                        s_task = by_id[succ]
                        if pending[succ] == 0 and s_task.state == "not_started":
                            ready.setdefault(s_task.team, set()).add(succ)
                    progressed = True
                if not progressed:
                    break  # nothing else fits this slot's remaining budget
        day, shift, _rolled = advance_clock(day, shift)

    # Fleet outcome: a REAL engine replan on the mutated fleet.
    final_cpm = compute_cpm(work.tasks)
    final_schedule = build_schedule(work, final_cpm, start_day=day)
    final_lateness = final_schedule.stats["fleet_lateness_days"]
    final_otd = final_schedule.stats["otd_count"]

    return {
        "policy": policy,
        "seed": seed,  # provenance; execution is deterministic (see module doc)
        "shifts": shifts,
        "slots": slots,
        "points": points,
        "completions": completions,
        "effort_minutes": effort_minutes,
        "baseline_fleet_lateness": baseline_lateness,
        "final_fleet_lateness": final_lateness,
        "lateness_recovered": baseline_lateness - final_lateness,
        "baseline_otd": baseline_otd,
        "final_otd": final_otd,
        # OR-5 honesty label from the fleet meta.
        "mock_data": bool(work.meta.get("mock_data", True)),
        "wall_s": round(time.perf_counter() - wall0, 3),
    }
