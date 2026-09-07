"""Reason-coded readiness — why a task can or cannot be worked RIGHT NOW.

``evaluate(task_id, snap)`` returns exactly::

    {"status": "DONE|IN_PROGRESS|READY|WAITING_PREDECESSOR|WAITING_PART|WAITING_CREW",
     "reason_codes": [...], "blocking_task": TaskKey|None, "blocking_team": str|None}

Deterministic and side-effect free: the verdict is a pure function of the
snapshot (task state, predecessor states, parts ETA, roster pools, and the
schedule's unscheduled reason codes). It MIRRORS the scheduler's rules —
it never invents a feasibility the engine would refuse to place:

- OR-1 (full crew or wait): a task whose (team, skill) pool cannot field
  ``mechanics_required`` distinct qualified mechanics is WAITING_CREW,
  never "ready with a partial crew".
- OR-2 (no borrowing): pool sizing uses ``snapshot.qualified_count``,
  which only ever counts the task's OWN team.
- Predecessor gates use live state (C12: a pred reference outside the
  task universe is treated as satisfied, matching the scheduler).

Check order (first hit wins): DONE -> IN_PROGRESS -> WAITING_PREDECESSOR
-> WAITING_PART -> WAITING_CREW -> READY.
"""

from __future__ import annotations

from ff.services.snapshot import component, max_pool_size, tasks_by_id

# Statuses (exact strings per ARCHITECTURE.md §ff/services/feasibility.py).
DONE = "DONE"
IN_PROGRESS = "IN_PROGRESS"
READY = "READY"
WAITING_PREDECESSOR = "WAITING_PREDECESSOR"
WAITING_PART = "WAITING_PART"
WAITING_CREW = "WAITING_CREW"
STATUSES = (DONE, IN_PROGRESS, READY, WAITING_PREDECESSOR, WAITING_PART, WAITING_CREW)

# Reason codes. The crew codes are the scheduler's exact strings — the
# validator, web layer, and tests key on them.
C_PRED_NOT_DONE = "predecessor_not_done"
C_AWAITING_PARTS = "awaiting_parts"
C_PARTS_ETA_UNKNOWN = "parts_eta_unknown"
C_PARTS_ARRIVED = "parts_arrived_clearable"
C_NO_SKILL = "no_skill_holder"
C_CREW_POOL = "crew_exceeds_pool"
C_POOL_BUSY = "pool_busy"


def _verdict(status, reason_codes=(), blocking_task=None, blocking_team=None) -> dict:
    """Assemble the contract's 4-key result shape (and nothing else)."""
    return {
        "status": status,
        "reason_codes": list(reason_codes),
        "blocking_task": blocking_task,
        "blocking_team": blocking_team,
    }


def evaluate(task_id: str, snap) -> dict:
    """Classify one task's live workability, mirroring scheduler rules.

    Rules enforced (see module docstring): OR-1 full-crew sizing, OR-2
    team-pure pools, live-state predecessor gates, parts-ETA gates.
    ``blocking_task`` is the FIRST unfinished predecessor (sorted by
    task_id — deterministic) and ``blocking_team`` is that predecessor's
    team; for crew waits ``blocking_team`` is the task's own team (the
    pool that cannot field the crew). Raises ``KeyError`` for an unknown
    task_id — an unknown task has no honest verdict.
    """
    tasks = tasks_by_id(snap)
    task = tasks.get(task_id)
    if task is None:
        raise KeyError(f"unknown task_id {task_id!r}")

    # 1) Terminal / already-running states.
    if task.state == "done":
        return _verdict(DONE)
    if task.state == "in_progress":
        return _verdict(IN_PROGRESS)

    # 2) Predecessor gate: first unfinished pred blocks (C12 — a reference
    #    outside the task universe counts as satisfied, like the scheduler).
    for pred_id in sorted(set(task.predecessors)):
        if pred_id == task.task_id:
            continue
        pred = tasks.get(pred_id)
        if pred is not None and pred.state != "done":
            return _verdict(
                WAITING_PREDECESSOR,
                [C_PRED_NOT_DONE],
                blocking_task=pred_id,
                blocking_team=pred.team,
            )

    # 3) Parts gate: a blocked task waits on material, not on people. The
    #    state stays "blocked" until an actuals write clears it (OR-6), so
    #    we report clearability honestly rather than flipping the status.
    if task.state == "blocked":
        codes = [C_AWAITING_PARTS]
        if task.parts_eta_day is None:
            codes.append(C_PARTS_ETA_UNKNOWN)
        else:
            schedule = component(snap, "schedule")
            today = int(schedule.meta.get("start_day", 0)) if schedule.meta else 0
            if task.parts_eta_day <= today:
                codes.append(C_PARTS_ARRIVED)
        return _verdict(WAITING_PART, codes)

    # 4) Crew gate (OR-1/OR-2): can this task's OWN team field the FULL
    #    crew at all? Static roster holes first, then the dynamic case —
    #    the pool exists but the engine could not book it within horizon.
    biggest_pool = max_pool_size(snap, task.team, task.skill)
    if biggest_pool == 0:
        return _verdict(WAITING_CREW, [C_NO_SKILL], blocking_team=task.team)
    if task.mechanics_required > biggest_pool:
        return _verdict(WAITING_CREW, [C_CREW_POOL], blocking_team=task.team)
    schedule = component(snap, "schedule")
    if task.task_id in schedule.unscheduled:
        # Preds are done and the pool is big enough, yet the scheduler
        # left it unplaced: every qualified crew window is taken.
        return _verdict(WAITING_CREW, [C_POOL_BUSY], blocking_team=task.team)

    # 5) Nothing blocks it.
    return _verdict(READY)
