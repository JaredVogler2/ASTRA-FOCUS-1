"""CPM engine — per-aircraft critical-path analysis in work-content minutes.

``compute_cpm(tasks) -> dict[TaskKey, CpmInfo]`` where ``CpmInfo`` is a plain
dict with EXACTLY these keys (contract, ARCHITECTURE.md §ff/engine/cpm.py):

    es               int    earliest start (work-content minutes)
    lf               int    latest finish  (work-content minutes)
    slack_minutes    float  lf - (es + duration); <= 0 means already infeasible
    cpm_priority     float  remaining critical-chain work THROUGH the task
                            (its own duration + longest successor chain);
                            higher = more work hangs behind it
    downstream_count int    number of TRANSITIVE successors (unlock count)

Frame: one working day of the fleet = ``sum(SHIFT_EFFECTIVE) = 1290`` minutes
(``ff.domain.DAY_WORK_MINUTES``), so day-valued anchors (``earliest_day``,
``deadline_day``) convert to minutes by multiplying by that constant.

Rules enforced:
- C12 (per-aircraft DAG): passes run per aircraft; a predecessor reference
  that is not a task of the SAME aircraft is treated as already satisfied
  (the generator guarantees no cross-aircraft edges exist).
- Determinism: sorted grouping + Kahn topological order driven by a min-heap
  on task_id; results are pure functions of the input task list.
- No recursion: every pass (Kahn forward, backward LF, cpm_priority,
  memoized transitive-successor bitsets) is iterative — safe at 18k+ tasks.
- Honesty about live state: ``effective_duration`` uses 0 for ``done`` work
  and ``remaining_minutes`` for ``in_progress`` work, so slack/priority
  reflect REMAINING work content, never work already burned down.
"""

from __future__ import annotations

import heapq

import config
from ff.domain import DAY_WORK_MINUTES, Task, TaskKey

# CpmInfo is a plain dict (JSON-safe by construction); alias for signatures.
CpmInfo = dict


def effective_duration(task: Task) -> int:
    """Return the REMAINING work content of a task in minutes.

    Enforces the live-state rule: ``done`` contributes 0 (nothing hangs
    behind finished work), ``in_progress`` contributes ``remaining_minutes``
    when known, everything else contributes the full ``duration_minutes``.
    """
    if task.state == "done":
        return 0
    if task.state == "in_progress" and task.remaining_minutes is not None:
        return max(0, int(task.remaining_minutes))
    return int(task.duration_minutes)


def is_critical(info: CpmInfo) -> bool:
    """Return the contract's criticality flag for one CpmInfo.

    Enforces ``isCritical <=> cpm_slack <= CRITICAL_SLACK_MIN`` with slack
    expressed in DAYS of work content (slack_minutes / DAY_WORK_MINUTES).
    """
    return (info["slack_minutes"] / DAY_WORK_MINUTES) <= config.CRITICAL_SLACK_MIN


def compute_cpm(tasks: list[Task]) -> dict[TaskKey, CpmInfo]:
    """Run the forward/backward CPM passes and return per-task CpmInfo.

    Pure and deterministic: identical task lists (any input order) yield an
    identical result mapping. Raises ``ValueError`` on a precedence cycle
    (the generator builds DAGs acyclic by construction, so a cycle means
    corrupted input, never a soft-skip).
    """
    groups: dict[int, list[Task]] = {}
    for task in sorted(tasks, key=lambda t: t.task_id):
        groups.setdefault(task.aircraft, []).append(task)

    out: dict[TaskKey, CpmInfo] = {}
    for aircraft in sorted(groups):
        _cpm_one_aircraft(groups[aircraft], out)
    return out


def _cpm_one_aircraft(tasks: list[Task], out: dict[TaskKey, CpmInfo]) -> None:
    """Compute CpmInfo for one aircraft's task DAG into ``out`` (in place).

    ``tasks`` is pre-sorted by task_id. Enforces C12 by resolving
    predecessor references only within this aircraft's task set; anything
    else (missing id, cross-aircraft, self-reference) counts as satisfied.
    """
    by_id: dict[TaskKey, Task] = {t.task_id: t for t in tasks}
    dur: dict[TaskKey, int] = {tid: effective_duration(t) for tid, t in by_id.items()}

    # --- edges (deduped, sorted for deterministic iteration) ---------------
    succs: dict[TaskKey, list[TaskKey]] = {tid: [] for tid in by_id}
    indeg: dict[TaskKey, int] = {tid: 0 for tid in by_id}
    for task in tasks:
        for pred in sorted(set(task.predecessors)):
            if pred == task.task_id or pred not in by_id:
                continue  # C12 / missing pred => treated as satisfied
            succs[pred].append(task.task_id)
            indeg[task.task_id] += 1
    for lst in succs.values():
        lst.sort()

    # --- forward pass (Kahn, min-heap on task_id => deterministic) ---------
    # ES floor: a task can never start before its own earliest_day.
    es: dict[TaskKey, int] = {
        tid: max(0, by_id[tid].earliest_day) * DAY_WORK_MINUTES for tid in by_id
    }
    heap: list[TaskKey] = sorted(tid for tid in by_id if indeg[tid] == 0)
    topo: list[TaskKey] = []
    while heap:
        tid = heapq.heappop(heap)
        topo.append(tid)
        finish = es[tid] + dur[tid]
        for succ in succs[tid]:
            if finish > es[succ]:
                es[succ] = finish
            indeg[succ] -= 1
            if indeg[succ] == 0:
                heapq.heappush(heap, succ)
    if len(topo) != len(by_id):
        stuck = sorted(tid for tid in by_id if tid not in set(topo))
        raise ValueError(
            "precedence cycle in aircraft %d DAG (unreached tasks: %s...)"
            % (tasks[0].aircraft, stuck[:5])
        )

    # --- backward pass + priority + downstream bitsets (reverse topo) ------
    # LF anchor: the task's own deadline in work-content minutes; tightened
    # by every successor's latest start (LF_s - dur_s).
    lf: dict[TaskKey, int] = {
        tid: by_id[tid].deadline_day * DAY_WORK_MINUTES for tid in by_id
    }
    prio: dict[TaskKey, float] = {}
    masks: dict[TaskKey, int] = {}  # memoized transitive-successor bitsets
    pos: dict[TaskKey, int] = {tid: i for i, tid in enumerate(topo)}
    for tid in reversed(topo):
        latest = lf[tid]
        best_chain = 0.0
        mask = 0
        for succ in succs[tid]:
            latest_start = lf[succ] - dur[succ]
            if latest_start < latest:
                latest = latest_start
            if prio[succ] > best_chain:
                best_chain = prio[succ]
            mask |= masks[succ] | (1 << pos[succ])
        lf[tid] = latest
        prio[tid] = float(dur[tid]) + best_chain
        masks[tid] = mask
        out[tid] = {
            "es": es[tid],
            "lf": latest,
            "slack_minutes": float(latest - (es[tid] + dur[tid])),
            "cpm_priority": prio[tid],
            "downstream_count": mask.bit_count(),
        }
