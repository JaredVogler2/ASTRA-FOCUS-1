"""Capacity pressure — $-weighted attribution of lateness to (team, shift) pools.

MAX doctrine (simplified): this is ANALYTIC ATTRIBUTION, NOT PROOF. Walking
a late aircraft's completion-defining chain and blaming idle gaps on the
pool the waiting task needed is a defensible accounting of where lateness
accumulated — it does NOT prove that adding heads to that pool recovers
those days (interactions, quotas, and precedence can move the bottleneck).

Algorithm, per the contract (ARCHITECTURE.md §ff/services/capacity.py):

1. Compute per-aircraft completion vs ``delivery_deadline_day``; keep the
   LATE aircraft (lateness_days > 0).
2. For each late aircraft find the completion-defining assignment — the
   scheduled task with the latest (slot, end_minute), tie-break by task_id.
3. Walk back along the latest-finishing predecessor chain. At each task the
   earliest day it could legally have run is
   ``max(preds' end day, earliest_day, parts_eta_day or earliest_day)``;
   any POSITIVE gap between its scheduled day and that floor is CAPACITY
   WAIT (the task was ready; the crew was not) and is attributed to the
   (team, shift) pool of its assignment.
4. Dollar-weight each wait day by the marginal lateness penalty
   ``LATENESS_PENALTY_USD_PER_DAY`` (config §4 PLACEHOLDER — output is
   labeled, OR-5).
5. Pools are returned sorted by ``-dollar_days`` (tie-break team, shift):
   the most expensive bottleneck first.

Deterministic: sorted iteration everywhere; identical snapshots yield an
identical report.
"""

from __future__ import annotations

import config
from ff.domain import Fleet, Schedule, slot_index, SHIFTS


def _component(snap, name: str):
    """Extract ``fleet``/``schedule`` from a Snapshot object or plain dict."""
    if isinstance(snap, dict):
        value = snap.get(name)
    else:
        value = getattr(snap, name, None)
    if value is None:
        raise ValueError(f"snapshot has no {name!r} component")
    if name == "fleet" and isinstance(value, dict):
        value = Fleet.from_dict(value)
    if name == "schedule" and isinstance(value, dict):
        value = Schedule.from_dict(value)
    return value


def _asg_end_key(asg) -> tuple[int, int, str]:
    """Total order on assignment finish: (slot, end_minute, task_id)."""
    shift = asg.shift if asg.shift in SHIFTS else 1
    return (slot_index(asg.day, shift), asg.end_minute, asg.task_id)


def pressure(snap) -> dict:
    """Compute $-weighted capacity pressure per (team, shift) pool.

    ANALYTIC ATTRIBUTION, NOT PROOF (MAX doctrine): the report says where
    late aircraft WAITED and what those waits cost at the placeholder
    penalty rate; it does not claim that staffing that pool recovers the
    dollars. ``snap`` is a Snapshot (object or dict) exposing ``fleet``
    and ``schedule``.

    Returns::

        {"pools": [{"team", "shift", "wait_days", "dollar_days",
                    "aircraft_touched"}, ...],       # sorted by -dollar_days
         "late_aircraft": [{"aircraft", "lateness_days",
                            "completion_day", "deadline_day"}, ...],
         "total_wait_days": int, "total_dollar_days": int,
         "penalty_usd_per_day": int, "source": "config-defaults",
         "method": "completion-chain walk-back (analytic attribution, not proof)"}
    """
    fleet: Fleet = _component(snap, "fleet")
    schedule: Schedule = _component(snap, "schedule")
    rate = config.LATENESS_PENALTY_USD_PER_DAY

    tasks = {t.task_id: t for t in fleet.tasks}
    assignments = schedule.assignments

    # --- per-aircraft completion & lateness (from scheduled work only) -----
    completion: dict[int, int] = {}
    by_aircraft: dict[int, list[str]] = {}
    for tid in sorted(assignments):
        task = tasks.get(tid)
        if task is None:
            continue
        asg = assignments[tid]
        by_aircraft.setdefault(task.aircraft, []).append(tid)
        if task.aircraft not in completion or asg.day > completion[task.aircraft]:
            completion[task.aircraft] = asg.day

    late: list[dict] = []
    for ac in sorted(fleet.aircraft, key=lambda a: a.aircraft):
        if ac.aircraft not in completion:
            continue  # nothing scheduled -> no chain to walk
        lateness = completion[ac.aircraft] - ac.delivery_deadline_day
        if lateness > 0:
            late.append(
                {
                    "aircraft": ac.aircraft,
                    "lateness_days": lateness,
                    "completion_day": completion[ac.aircraft],
                    "deadline_day": ac.delivery_deadline_day,
                }
            )

    # --- walk each late aircraft's completion-defining chain ---------------
    pools: dict[tuple[str, int], dict] = {}
    total_wait = 0
    for entry in late:
        aircraft = entry["aircraft"]
        # Completion-defining assignment: latest finish, tie-break task_id.
        tail_tid = max(by_aircraft[aircraft], key=lambda t: _asg_end_key(assignments[t]))
        current = tail_tid
        visited: set[str] = set()
        while current is not None and current not in visited:
            visited.add(current)
            task = tasks[current]
            asg = assignments[current]

            # Non-capacity floors: own release date and parts arrival.
            floor_day = task.earliest_day
            if task.parts_eta_day is not None and task.parts_eta_day > floor_day:
                floor_day = task.parts_eta_day

            # Predecessor floor + next hop = latest-finishing scheduled pred.
            scheduled_preds = sorted(
                p for p in set(task.predecessors) if p in assignments and p != current
            )
            next_hop = None
            if scheduled_preds:
                next_hop = max(scheduled_preds, key=lambda p: _asg_end_key(assignments[p]))
                pred_end_day = max(assignments[p].day for p in scheduled_preds)
                if pred_end_day > floor_day:
                    floor_day = pred_end_day

            gap = asg.day - floor_day
            if gap > 0:
                # Task was ready on floor_day; the (team, shift) pool made it
                # wait `gap` days -> capacity wait, attributed here.
                key = (asg.team, asg.shift)
                pool = pools.setdefault(
                    key,
                    {"team": asg.team, "shift": asg.shift, "wait_days": 0,
                     "dollar_days": 0, "_aircraft": set()},
                )
                pool["wait_days"] += gap
                pool["dollar_days"] += gap * rate
                pool["_aircraft"].add(aircraft)
                total_wait += gap

            current = next_hop

    pool_rows = []
    for key in sorted(pools):
        pool = pools[key]
        pool_rows.append(
            {
                "team": pool["team"],
                "shift": pool["shift"],
                "wait_days": pool["wait_days"],
                "dollar_days": pool["dollar_days"],
                "aircraft_touched": len(pool["_aircraft"]),
            }
        )
    # Most expensive bottleneck first; deterministic tie-break (team, shift).
    pool_rows.sort(key=lambda p: (-p["dollar_days"], p["team"], p["shift"]))

    return {
        "pools": pool_rows,
        "late_aircraft": late,
        "total_wait_days": total_wait,
        "total_dollar_days": total_wait * rate,
        "penalty_usd_per_day": rate,
        # OR-5: placeholder-rate label — NEVER remove.
        "source": config.ECONOMICS_SOURCE,
        "method": "completion-chain walk-back (analytic attribution, not proof)",
    }
