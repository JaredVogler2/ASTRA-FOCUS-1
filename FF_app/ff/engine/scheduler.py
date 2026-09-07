"""Deterministic greedy named-placement scheduler — THE CORE.

``build_schedule(fleet, cpm, start_day=0, horizon_days=None) -> Schedule``
implements the 7 numbered behaviors of ARCHITECTURE.md §ff/engine/scheduler.py.

Owner rules enforced here (do not regress):
- OR-1 FULL CREW OR WAIT: a task is placed ONLY when ``mechanics_required``
  distinct qualified mechanics are simultaneously free for the whole
  duration. Work that cannot field a full crew is DELAYED (later slot) or
  reason-coded in ``Schedule.unscheduled`` — never short-crewed, and no
  fictional placements ever.
- OR-2 NO CROSS-TEAM BORROWING: candidate pools are built exclusively from
  mechanics whose ``team == task.team``.
- OR-3 SUNDAY-NIGHT S3: slot eligibility delegates to
  ``ff.domain.shift_eligible`` (shift 3 on a non-working day d is plannable
  iff d+1 is a working day).
- Committed-first flavor: ``in_progress`` work is pinned to ``start_day``
  (using ``remaining_minutes``) BEFORE any new work is dispatched.
- OR-4 commitment defense (§12, INCREMENT 5 — MAX config §10 mechanism):
  "Committed work dispatches before new work. Inside the 3-day commitment
  horizon, incumbent slots and mechanics are defended." When an
  ``incumbent`` map (see :func:`incumbent_from_schedule`) is supplied and
  ``COMMITMENT_ENABLED``, tasks whose incumbent slot lies within
  ``COMMITMENT_HORIZON_DAYS`` of ``start_day`` (a) dispatch before
  non-committed work (leading 0/1 heap-key component), (b) try their
  incumbent (day, shift) FIRST — at or after the precedence/parts floor;
  the floor always wins ("precedence is physics; stickiness is not"), and
  (c) prefer their incumbent crew, with a two-attempt fallback to free
  choice ("crew continuity must never cost slot continuity"). Beyond the
  horizon: free repack (left-pull preserved). ``incumbent=None`` is
  BYTE-IDENTICAL to the pre-commitment behavior (tripwire-tested).

Determinism (behavior 7): sorted iteration everywhere, ready-heap tie-break
by task_id, no wall-clock inputs to any decision. Two runs on the same
fleet+args produce identical assignments/unscheduled/stats — the single
measured, non-semantic field is ``stats["wall_seconds"]``.

Performance: iterative throughout, O(pool x intervals) per slot probe, slot
scans start at each task's precedence floor. 18,000 tasks schedule in well
under the 60 s budget in pure Python.
"""

from __future__ import annotations

import heapq
import math
import time
from bisect import insort
from collections import deque

import config
from ff.domain import (
    SHIFTS,
    Assignment,
    Fleet,
    Schedule,
    Task,
    TaskKey,
    shift_eligible,
    slot_index,
)

# Reason codes (contract behavior 6). These exact strings are the API other
# modules (validator, feasibility, web) key on.
R_NO_SKILL = "no_skill_holder"
R_CREW_POOL = "crew_exceeds_pool"
R_HORIZON = "no_crew_within_horizon"
UNSCHEDULED_REASONS = (R_NO_SKILL, R_CREW_POOL, R_HORIZON)


def _earliest_free(
    intervals: list[tuple[int, int]],
    floor: int,
    dur: int,
    latest_start: int,
    shift_max: int,
) -> int | None:
    """Earliest start >= floor with a free window of ``dur`` minutes.

    ``intervals`` is this mechanic's sorted, non-overlapping booked list for
    one (day, shift). Enforces behavior 4's window bounds: start <=
    ``latest_start`` (SHIFT_EFFECTIVE - NO_START_BUFFER) and end <=
    ``shift_max``. Returns None when no window fits in this shift.
    """
    start = floor if floor > 0 else 0
    for booked_start, booked_end in intervals:
        if booked_start >= start + dur:
            break  # gap before this booking fits the whole task
        if booked_end > start:
            start = booked_end
    if start > latest_start or start + dur > shift_max:
        return None
    return start


class _Engine:
    """Mutable scheduling state: ledgers, pools, quotas, placements."""

    def __init__(
        self,
        fleet: Fleet,
        start_day: int,
        horizon_days: int,
        incumbent: dict | None = None,
    ):
        self.start_day = start_day
        self.horizon_days = horizon_days
        self.end_slot = (start_day + horizon_days) * 3

        # §12 COMMITMENT (OR-4): the in-horizon incumbent map. Only entries
        # whose prior slot falls in [start_day, start_day +
        # COMMITMENT_HORIZON_DAYS) are defended; everything else is a free
        # repack. Empty when incumbent is None or COMMITMENT_ENABLED is off,
        # which makes every commitment branch below a no-op (byte-identical
        # to pre-commitment behavior — tripwire-tested).
        self.incumbent: dict[TaskKey, dict] = {}
        if incumbent and config.COMMITMENT_ENABLED:
            horizon_end = start_day + config.COMMITMENT_HORIZON_DAYS
            for tid in sorted(incumbent):
                entry = incumbent[tid]
                if start_day <= int(entry["day"]) < horizon_end:
                    self.incumbent[tid] = entry
        # Measured commitment stats (contract §12): offered / slot kept /
        # >=1 incumbent crew member kept.
        self.commit_in_horizon = 0
        self.commit_kept = 0
        self.commit_mech_kept = 0

        # (team, shift) -> mechanics, sorted by mech_id (OR-2: pools are
        # team-pure; a task can only ever see its own team's roster).
        self.pool: dict[tuple[str, int], list] = {}
        self._skills: dict[str, frozenset] = {}
        for mech in sorted(fleet.mechanics, key=lambda m: m.mech_id):
            if mech.mech_id in self._skills:
                continue  # defensive dedupe of roster ids
            self._skills[mech.mech_id] = frozenset(mech.skills)
            self.pool.setdefault((mech.team, mech.shift), []).append(mech)

        # Behavior 3 activation quota: at most floor(pool*UTILIZATION)
        # distinct mechanics used per (team, shift, day).
        self.quota: dict[tuple[str, int], int] = {
            key: math.floor(len(pool) * config.UTILIZATION)
            for key, pool in self.pool.items()
        }
        self._qualified_cache: dict[tuple[str, int, str], list[str]] = {}

        # Behavior 4 per-mechanic ledger, keyed (mech_id, day, shift):
        # load minutes + sorted interval list for overlap checks.
        self.load: dict[tuple[str, int, int], int] = {}
        self.intervals: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
        # (team, shift, day) -> set of activated mech_ids (quota ledger).
        self.activated: dict[tuple[str, int, int], set[str]] = {}

        self.assignments: dict[TaskKey, Assignment] = {}
        self.unscheduled: dict[TaskKey, str] = {}
        # task_id -> (slot_index, end_minute) for precedence floors (beh. 5).
        self.pred_end: dict[TaskKey, tuple[int, int]] = {}

    # -- pools ---------------------------------------------------------------

    def qualified(self, team: str, shift: int, skill: str) -> list[str]:
        """Sorted mech_ids of (team, shift) holding ``skill`` ("ANY" = all).

        Enforces OR-2 (team-pure pool) and behavior 3's skill matching.
        """
        key = (team, shift, skill)
        cached = self._qualified_cache.get(key)
        if cached is None:
            pool = self.pool.get((team, shift), [])
            if skill == "ANY":
                cached = [m.mech_id for m in pool]
            else:
                cached = [m.mech_id for m in pool if skill in self._skills[m.mech_id]]
            self._qualified_cache[key] = cached
        return cached

    def static_reason(self, task: Task) -> str | None:
        """Roster-level infeasibility that no amount of waiting cures.

        Enforces behavior 6's static reason codes: (team, skill) with no
        holder on ANY shift -> no_skill_holder; crew size larger than every
        shift's qualified pool -> crew_exceeds_pool.
        """
        sizes = [len(self.qualified(task.team, s, task.skill)) for s in SHIFTS]
        biggest = max(sizes)
        if biggest == 0:
            return R_NO_SKILL
        if task.mechanics_required > biggest:
            return R_CREW_POOL
        return None

    # -- placement -----------------------------------------------------------

    def try_slot(
        self,
        team: str,
        skill: str,
        crew_size: int,
        dur: int,
        day: int,
        shift: int,
        minute_floor: int,
        prefer: frozenset | None = None,
    ) -> tuple[int, list[str]] | None:
        """Try to book a FULL crew of ``crew_size`` in one (day, shift).

        Enforces OR-1 inside the slot: returns (start, mech_ids) ONLY when
        ``crew_size`` distinct qualified mechanics share a common free
        window [start, start+dur) that respects the no-start buffer,
        SHIFT_MAX, per-mechanic load cap and the activation quota; returns
        None otherwise (caller advances to the next slot — wait, never
        short-crew).

        Crew selection (deterministic): candidates sorted by
        (earliest-free-start, mech_id); take the first ``crew_size``
        quota-eligible; crew start = max of members' starts; if members
        disagree, re-probe with the floor raised to that crew start (the
        floor strictly increases, so this converges).

        ``prefer`` (§12 COMMITMENT mechanic preference, OR-4: "incumbent
        slots and mechanics are defended"): when non-empty, the candidate
        sort key becomes (incumbent members first, then earliest-free, then
        id). Preference can fail a slot that free choice would fill — the
        CALLER implements the two-attempt fallback ("crew continuity must
        never cost slot continuity") by retrying with ``prefer=None``.
        ``prefer=None`` is byte-identical to the pre-commitment sort.
        """
        effective = config.SHIFT_EFFECTIVE[shift]
        shift_max = config.SHIFT_MAX[shift]
        latest_start = effective - config.NO_START_BUFFER
        if latest_start < 0 or dur > shift_max:
            return None
        candidates = self.qualified(team, shift, skill)
        if len(candidates) < crew_size:
            return None
        activated = self.activated.get((team, shift, day), frozenset())
        budget = self.quota[(team, shift)] - len(activated)
        load_cap = effective + config.OVERTIME

        floor = minute_floor
        while True:
            avail: list[tuple[int, str]] = []
            for mech_id in candidates:
                key = (mech_id, day, shift)
                if self.load.get(key, 0) + dur > load_cap:
                    continue  # behavior 4 load cap: effective + overtime
                start = _earliest_free(
                    self.intervals.get(key, ()), floor, dur, latest_start, shift_max
                )
                if start is not None:
                    avail.append((start, mech_id))
            if len(avail) < crew_size:
                return None
            if prefer:
                # §12 mechanic preference: incumbent crew first, then
                # earliest-free, then id — still fully deterministic.
                avail.sort(key=lambda sm: (sm[1] not in prefer, sm[0], sm[1]))
            else:
                avail.sort()  # (earliest-free, mech_id) — deterministic order
            picked: list[tuple[int, str]] = []
            new_activations = 0
            for start, mech_id in avail:
                is_new = mech_id not in activated
                if is_new and new_activations >= budget:
                    continue  # activation quota (behavior 3)
                picked.append((start, mech_id))
                if is_new:
                    new_activations += 1
                if len(picked) == crew_size:
                    break
            if len(picked) < crew_size:
                return None
            if prefer:
                # Under ``prefer`` the picks are not start-sorted: take the
                # explicit max/min.
                crew_start = max(s for s, _ in picked)
                earliest = min(s for s, _ in picked)
            else:
                crew_start = picked[-1][0]  # max start (picked is sorted)
                earliest = picked[0][0]
            if earliest == crew_start:
                # Common window confirmed for the whole crew.
                return crew_start, [mech_id for _, mech_id in picked]
            # Members disagree — raise the floor and re-probe. crew_start >
            # floor here (else all starts would equal it), so this loop
            # strictly advances and terminates within the shift.
            floor = crew_start

    def place(
        self,
        task: Task,
        day: int,
        shift: int,
        start: int,
        mech_ids: list[str],
        dur: int,
        kept_incumbent: bool = False,
    ) -> None:
        """Commit a placement: book every crew member's ledger + quota set.

        The Assignment always carries the FULL named crew (OR-1) and
        ``uses_overtime`` iff end > SHIFT_EFFECTIVE (behavior 4).
        ``kept_incumbent`` (§12, OR-4) records that the commitment defense
        kept this task's incumbent (day, shift) — default False, so every
        non-commitment placement serializes exactly as before plus the
        constant-False field.
        """
        end = start + dur
        for mech_id in mech_ids:
            key = (mech_id, day, shift)
            self.load[key] = self.load.get(key, 0) + dur
            insort(self.intervals.setdefault(key, []), (start, end))
        self.activated.setdefault((task.team, shift, day), set()).update(mech_ids)
        self.assignments[task.task_id] = Assignment(
            task_id=task.task_id,
            day=day,
            shift=shift,
            start_minute=start,
            end_minute=end,
            mechanic_ids=list(mech_ids),
            team=task.team,
            skill=task.skill,
            uses_overtime=end > config.SHIFT_EFFECTIVE[shift],
            kept_incumbent=kept_incumbent,
        )
        self.pred_end[task.task_id] = (slot_index(day, shift), end)

    def schedule_task(self, task: Task, pinned: bool = False) -> str | None:
        """Place one ready task; return None on success or a reason code.

        Behavior 2 slot scan: from max(earliest_day floor, parts-ETA floor
        for blocked work, latest predecessor end) forward over eligible
        slots (working-day rule + OR-3) to the horizon. ``pinned`` is the
        behavior-6 in_progress path: the scan floor is start_day itself
        (work already physically underway ignores earliest_day/parts
        floors) and the duration is ``remaining_minutes``.

        §12 COMMITMENT slot defense (OR-4: "incumbent slots and mechanics
        are defended"): a task with an in-horizon incumbent tries its
        incumbent (day, shift) FIRST, at or after the precedence/parts
        floor — the floor always wins ("precedence is physics; stickiness
        is not"); on success the assignment records ``kept_incumbent=True``.
        Crew selection prefers the incumbent ``mechanic_ids`` with a
        two-attempt free-choice fallback per slot ("crew continuity must
        never cost slot continuity"). Tasks with no in-horizon incumbent
        run the pre-commitment path unchanged.
        """
        crew_size = task.mechanics_required
        if crew_size <= 0:
            raise ValueError(
                f"task {task.task_id}: mechanics_required must be >= 1, got {crew_size}"
            )
        if pinned and task.remaining_minutes is not None:
            dur = max(0, int(task.remaining_minutes))
        else:
            dur = int(task.duration_minutes)

        # Earliest slot floor (behavior 2 + my precedence records).
        if pinned:
            floor_day = self.start_day
        else:
            floor_day = max(self.start_day, task.earliest_day)
            if task.state == "blocked" and task.parts_eta_day is not None:
                floor_day = max(floor_day, task.parts_eta_day)
        best_slot = floor_day * 3
        minute_floor = 0
        if task.state != "in_progress":
            # VOID-EDGE rule: an in_progress task already started on the
            # floor — its incoming edges are void (see build_schedule graph
            # construction), so no predecessor floor applies to its
            # remaining minutes. Everything else floors on placed preds.
            for pred in sorted(set(task.predecessors)):
                end_info = self.pred_end.get(pred)
                if end_info is None:
                    continue  # done / missing pred: already satisfied
                pred_slot, pred_end_minute = end_info
                if pred_slot > best_slot:
                    best_slot, minute_floor = pred_slot, pred_end_minute
                elif pred_slot == best_slot:
                    minute_floor = max(minute_floor, pred_end_minute)

        # Cheap screen: some shift must be able to hold this task at all
        # (duration fits, pool large enough) or no scan can ever succeed.
        if not any(
            dur <= config.SHIFT_MAX[s]
            and config.SHIFT_EFFECTIVE[s] - config.NO_START_BUFFER >= 0
            and len(self.qualified(task.team, s, task.skill)) >= crew_size
            for s in SHIFTS
        ):
            return R_HORIZON

        # -- §12 commitment defense (OR-4) -----------------------------------
        inc = self.incumbent.get(task.task_id)
        prefer: frozenset | None = None
        if inc is not None:
            self.commit_in_horizon += 1  # offered defense
            prefer = frozenset(inc["mechanic_ids"])
            inc_day, inc_shift = int(inc["day"]), int(inc["shift"])
            inc_slot = slot_index(inc_day, inc_shift)
            if (
                inc_slot >= best_slot  # floor wins: precedence is physics;
                #                        stickiness is not (never pull a task
                #                        below its precedence/parts floor)
                and inc_slot < self.end_slot
                and shift_eligible(inc_day, inc_shift)
            ):
                floor = minute_floor if inc_slot == best_slot else 0
                found = self.try_slot(
                    task.team, task.skill, crew_size, dur,
                    inc_day, inc_shift, floor, prefer=prefer,
                )
                if found is None:
                    # Two-attempt rule: crew continuity must never cost
                    # slot continuity — retry the SAME slot free-choice.
                    found = self.try_slot(
                        task.team, task.skill, crew_size, dur,
                        inc_day, inc_shift, floor,
                    )
                if found is not None:
                    start, mech_ids = found
                    self.place(
                        task, inc_day, inc_shift, start, mech_ids, dur,
                        kept_incumbent=True,
                    )
                    self.commit_kept += 1
                    if prefer.intersection(mech_ids):
                        self.commit_mech_kept += 1
                    return None

        slot = best_slot
        while slot < self.end_slot:
            day, shift0 = divmod(slot, 3)
            shift = shift0 + 1
            if shift_eligible(day, shift):  # working-day rule + OR-3
                floor = minute_floor if slot == best_slot else 0
                found = self.try_slot(
                    task.team, task.skill, crew_size, dur, day, shift, floor,
                    prefer=prefer,
                )
                if found is None and prefer:
                    # Two-attempt rule again on the scan path: crew
                    # continuity must never cost slot continuity.
                    found = self.try_slot(
                        task.team, task.skill, crew_size, dur, day, shift, floor
                    )
                if found is not None:
                    start, mech_ids = found
                    self.place(task, day, shift, start, mech_ids, dur)
                    if prefer is not None and prefer.intersection(mech_ids):
                        self.commit_mech_kept += 1
                    return None
            slot += 1
        return R_HORIZON


def _topo_subset(
    task_ids: list[TaskKey], preds_in: dict[TaskKey, list[TaskKey]]
) -> list[TaskKey]:
    """Deterministic topological order of a subset (edges within the subset).

    Used to pin in_progress work in precedence order so an in_progress
    successor's same-day minute floor respects its in_progress predecessor.
    """
    id_set = set(task_ids)
    indeg = {tid: sum(1 for p in preds_in[tid] if p in id_set) for tid in task_ids}
    succs: dict[TaskKey, list[TaskKey]] = {tid: [] for tid in task_ids}
    for tid in task_ids:
        for pred in preds_in[tid]:
            if pred in id_set:
                succs[pred].append(tid)
    heap = sorted(tid for tid in task_ids if indeg[tid] == 0)
    order: list[TaskKey] = []
    while heap:
        tid = heapq.heappop(heap)
        order.append(tid)
        for succ in sorted(succs[tid]):
            indeg[succ] -= 1
            if indeg[succ] == 0:
                heapq.heappush(heap, succ)
    if len(order) != len(task_ids):  # defensive: cycles caught later anyway
        order.extend(sorted(id_set.difference(order)))
    return order


def incumbent_from_schedule(schedule: Schedule) -> dict[TaskKey, dict]:
    """Build the ``incumbent`` map for :func:`build_schedule` from a prior
    Schedule's assignments (§12 COMMITMENT, OR-4: "Committed work dispatches
    before new work. Inside the 3-day commitment horizon, incumbent slots
    and mechanics are defended.").

    Returns ``{task_id: {"day", "shift", "start_minute", "mechanic_ids"}}``
    over EVERY assignment (the engine itself filters to the in-horizon
    band). Deterministic: sorted task_id iteration, plain copies only.
    """
    return {
        tid: {
            "day": asg.day,
            "shift": asg.shift,
            "start_minute": asg.start_minute,
            "mechanic_ids": list(asg.mechanic_ids),
        }
        for tid, asg in sorted(schedule.assignments.items())
    }


def build_schedule(
    fleet: Fleet,
    cpm: dict,
    start_day: int = 0,
    horizon_days: int | None = None,
    incumbent: dict | None = None,
) -> Schedule:
    """Deterministic greedy named placement (contract behaviors 1-7).

    1. Ready heap keyed (-cpm_priority, task_id); ready = all preds placed.
    2. Slot scan from max(earliest_day, preds' end) over eligible slots.
    3. Team+shift+skill pools; FULL CREW OR WAIT (OR-1); no borrowing
       (OR-2); activation quota floor(pool*UTILIZATION) per (team,shift,day).
    4. Per-mechanic ledger: overlap intervals, load cap EFFECTIVE+OVERTIME,
       start <= EFFECTIVE-NO_START_BUFFER, end <= SHIFT_MAX (uses_overtime
       when end > EFFECTIVE).
    5. Precedence: pred end (slot, minute) <= succ start; same-slot requires
       succ.start_minute >= pred.end_minute.
    6. done excluded (preds satisfied); in_progress pinned to start_day with
       remaining_minutes; unschedulable -> reason-coded, NEVER faked.
    7. Deterministic: identical fleet+args => identical schedule.

    ``incumbent`` (§12 COMMITMENT, OR-4 — see module docstring and
    :func:`incumbent_from_schedule`): a prior schedule's placements to
    defend inside ``COMMITMENT_HORIZON_DAYS`` of ``start_day``. Committed
    (in-horizon) tasks dispatch before new work ("committed work dispatches
    before new work") and defend their incumbent slot/crew; beyond the
    horizon is a free repack. ``incumbent=None`` (the default) is
    BYTE-IDENTICAL to the pre-commitment engine (tripwire-tested).
    Stats gain ``commitment_in_horizon`` / ``commitment_kept`` /
    ``commitment_mech_kept`` (all 0 without an incumbent).

    ``horizon_days`` defaults to ``HORIZON_MIN_DAYS * 4``. Raises
    ``ValueError`` on duplicate task ids or a precedence cycle.
    """
    wall_start = time.perf_counter()
    if horizon_days is None:
        # Workload-scaled horizon: the fixed HORIZON_MIN_DAYS*4 default
        # starved large fleets (measured: 8,262 no_crew_within_horizon at
        # 55k tasks / 240-day horizon). Estimate calendar days from total
        # effective mechanic-minutes vs supply, with 3x fragmentation
        # headroom, floored at the old default.
        total_mech_min = sum(
            (t.remaining_minutes if t.state == "in_progress" and t.remaining_minutes else t.duration_minutes)
            * t.mechanics_required
            for t in fleet.tasks
            if t.state != "done"
        )
        eff_per_mech = (sum(config.SHIFT_EFFECTIVE.values()) / 3) * config.UTILIZATION
        workdays = total_mech_min / max(1.0, len(fleet.mechanics) * eff_per_mech)
        # 4x measured: 3x left the last-served heavy ship's tail (2,082
        # tasks funneling through a deliberately-thin skill pool) just past
        # the horizon at 55k-task scale.
        horizon_days = max(
            config.HORIZON_MIN_DAYS * 4, int(workdays * 7 / 5 * 4) + 30
        )
    engine = _Engine(fleet, start_day, horizon_days, incumbent)

    tasks = sorted(fleet.tasks, key=lambda t: t.task_id)
    by_id: dict[TaskKey, Task] = {}
    for task in tasks:
        if task.task_id in by_id:
            raise ValueError(f"duplicate task_id {task.task_id!r}")
        by_id[task.task_id] = task

    # Behavior 6: done tasks are excluded from the graph entirely — a pred
    # pointing at a done task is a satisfied pred.
    graph_ids = [tid for tid in by_id if by_id[tid].state != "done"]  # sorted
    succs: dict[TaskKey, list[TaskKey]] = {tid: [] for tid in graph_ids}
    preds_in: dict[TaskKey, list[TaskKey]] = {tid: [] for tid in graph_ids}
    indeg: dict[TaskKey, int] = {tid: 0 for tid in graph_ids}
    for tid in graph_ids:
        if by_id[tid].state == "in_progress":
            # VOID-EDGE rule (reality outranks the plan): work that already
            # STARTED on the floor voids its incoming precedence edges — the
            # out-of-sequence violation already happened physically and the
            # plan cannot un-happen it (the points engine charges P_OOS for
            # exactly this). Its remaining minutes are therefore not gated
            # behind unfinished predecessors; validator V2 mirrors the rule.
            continue
        for pred in sorted(set(by_id[tid].predecessors)):
            if pred == tid:
                continue
            pred_task = by_id.get(pred)
            if pred_task is None or pred_task.state == "done":
                continue  # satisfied
            succs[pred].append(tid)
            preds_in[tid].append(pred)
            indeg[tid] += 1

    priority: dict[TaskKey, float] = {
        tid: float(cpm.get(tid, {}).get("cpm_priority", 0.0)) for tid in graph_ids
    }
    slack: dict[TaskKey, float] = {
        tid: float(cpm.get(tid, {}).get("slack_minutes", 0.0)) for tid in graph_ids
    }

    committed = engine.incumbent  # in-horizon incumbents (§12, may be empty)

    def _heap_key(tid: TaskKey) -> tuple[int, float, float, TaskKey]:
        # Committed-first dispatch (§12, OR-4 / MAX rule: "committed work
        # dispatches before new work"): a ready task with an in-horizon
        # incumbent always pops before non-committed work (leading 0/1).
        # With no incumbent the component is constantly 1, leaving the
        # pre-commitment ordering untouched (byte-identical tripwire).
        # Non-committed component: ascending-slack dispatch (MAX
        # REBUILD_PROMPT section 6.2 seed rule): most deadline-urgent work
        # first, longest-chain cpm_priority as the tie-break, task_id last
        # for determinism. Deadline-blind dispatch ignored the delivery
        # ramp (measured: OTD 5/50 at 44k tasks).
        return (0 if tid in committed else 1, slack[tid], -priority[tid], tid)

    resolved: set[TaskKey] = set()
    resolve_queue: deque[TaskKey] = deque()

    # -- static roster screens (apply to ALL live tasks, incl. in_progress) --
    for tid in graph_ids:
        reason = engine.static_reason(by_id[tid])
        if reason is not None:
            engine.unscheduled[tid] = reason
            resolved.add(tid)
            resolve_queue.append(tid)

    # -- committed-first: pin in_progress work at start_day (behavior 6) ----
    # Ordered topologically among themselves so same-slot minute floors hold
    # between two in_progress tasks. Input assumption (generator/actuals
    # guarantee): a task is in_progress only when its predecessors are done
    # or themselves in_progress — reality outranks the plan, so pending
    # not-started preds do NOT hold pinned work back.
    in_progress = [
        tid
        for tid in graph_ids
        if by_id[tid].state == "in_progress" and tid not in resolved
    ]
    for tid in _topo_subset(in_progress, preds_in):
        reason = engine.schedule_task(by_id[tid], pinned=True)
        if reason is not None:
            engine.unscheduled[tid] = reason
        resolved.add(tid)
        resolve_queue.append(tid)

    # -- behavior 1: ready heap, committed-first + ascending-slack key -------
    ready_heap: list[tuple[int, float, float, TaskKey]] = [
        _heap_key(tid)
        for tid in graph_ids
        if tid not in resolved and indeg[tid] == 0
    ]
    heapq.heapify(ready_heap)

    while resolve_queue or ready_heap:
        # Drain resolutions first so every newly-ready task competes in the
        # heap before the next dispatch (pure bookkeeping, no resources).
        while resolve_queue:
            done_tid = resolve_queue.popleft()
            for succ in succs.get(done_tid, ()):
                indeg[succ] -= 1
                if indeg[succ] == 0 and succ not in resolved:
                    # A task below an unplaced pred can never be placed:
                    # cascade the (smallest-id) blocking pred's reason.
                    blocked_by = next(
                        (p for p in preds_in[succ] if p in engine.unscheduled), None
                    )
                    if blocked_by is not None:
                        engine.unscheduled[succ] = engine.unscheduled[blocked_by]
                        resolved.add(succ)
                        resolve_queue.append(succ)
                    else:
                        heapq.heappush(ready_heap, _heap_key(succ))
        if ready_heap:
            tid = heapq.heappop(ready_heap)[-1]
            if tid in resolved:
                continue
            reason = engine.schedule_task(by_id[tid])
            if reason is not None:
                engine.unscheduled[tid] = reason
            resolved.add(tid)
            resolve_queue.append(tid)

    unreached = [tid for tid in graph_ids if tid not in resolved]
    if unreached:
        raise ValueError(
            f"precedence cycle: {len(unreached)} tasks unreachable "
            f"(first: {unreached[:5]})"
        )

    stats = _build_stats(fleet, by_id, engine, start_day, wall_start)
    meta = {
        "engine": "ff-greedy-v1",
        "start_day": start_day,
        "horizon_days": horizon_days,
        # OR-5 honesty: propagate the synthetic-data label onto the output.
        "mock_data": bool(fleet.meta.get("mock_data", True)),
        "seed": fleet.meta.get("seed"),
    }
    schedule = Schedule(
        assignments=engine.assignments,
        unscheduled=engine.unscheduled,
        stats=stats,
        meta=meta,
    )
    # Contract: Schedule.stats MUST carry ``economics`` (ff.engine.economics)
    # and ``capacity_pressure`` (ff.services.capacity). Wired here, after the
    # Schedule object exists, because the capacity walk-back consumes the
    # finished schedule. Lazy import keeps the engine->services edge confined
    # to this one call (no import cycle: capacity imports only config+domain).
    # Both are pure functions of (fleet, schedule, start_day) — determinism
    # (behavior 7) is preserved.
    from ff.engine.economics import fleet_economics
    from ff.services.capacity import pressure

    stats["economics"] = fleet_economics(stats["aircraft"], today_day=start_day)
    stats["capacity_pressure"] = pressure({"fleet": fleet, "schedule": schedule})
    return schedule


def _build_stats(
    fleet: Fleet,
    by_id: dict[TaskKey, Task],
    engine: _Engine,
    start_day: int,
    wall_start: float,
) -> dict:
    """Assemble the contract-mandated Schedule.stats block.

    ``economics`` and ``capacity_pressure`` are initialized as {} here and
    filled by ``build_schedule`` (from ``ff.engine.economics`` and
    ``ff.services.capacity``) once the Schedule object exists — the capacity
    walk-back consumes the finished schedule.
    """
    completion: dict[int, int] = {}
    unsched_per_ac: dict[int, int] = {}
    for tid, assignment in engine.assignments.items():
        aircraft = by_id[tid].aircraft
        if assignment.day > completion.get(aircraft, -1):
            completion[aircraft] = assignment.day
    for tid in engine.unscheduled:
        aircraft = by_id[tid].aircraft
        unsched_per_ac[aircraft] = unsched_per_ac.get(aircraft, 0) + 1

    aircraft_stats = []
    fleet_lateness = 0
    otd_count = 0
    for ac in sorted(fleet.aircraft, key=lambda a: a.aircraft):
        completion_day = completion.get(ac.aircraft, start_day)
        lateness = max(0, completion_day - ac.delivery_deadline_day)
        on_time = lateness == 0
        fleet_lateness += lateness
        if on_time:
            otd_count += 1
        aircraft_stats.append(
            {
                "aircraft": ac.aircraft,
                "completion_day": completion_day,
                "deadline_day": ac.delivery_deadline_day,
                "lateness_days": lateness,
                "on_time": on_time,
                # extra (honesty): a completion_day computed while work is
                # still unplaceable is optimistic — surface the count.
                "unscheduled_tasks": unsched_per_ac.get(ac.aircraft, 0),
            }
        )

    done_count = sum(1 for t in by_id.values() if t.state == "done")
    makespan = max((a.day for a in engine.assignments.values()), default=start_day)
    return {
        "total_tasks": len(fleet.tasks),
        "scheduled": len(engine.assignments),
        "unscheduled": len(engine.unscheduled),
        "done_tasks": done_count,  # extra: total = scheduled+unscheduled+done
        "fleet_lateness_days": fleet_lateness,
        "otd_count": otd_count,
        "aircraft": aircraft_stats,
        "makespan_day": makespan,
        # §12 COMMITMENT (OR-4) measured counters: tasks offered defense
        # (in-horizon incumbent), incumbent slot kept, >=1 incumbent crew
        # member kept. All 0 when no incumbent map was supplied.
        "commitment_in_horizon": engine.commit_in_horizon,
        "commitment_kept": engine.commit_kept,
        "commitment_mech_kept": engine.commit_mech_kept,
        "wall_seconds": round(time.perf_counter() - wall_start, 3),
        "economics": {},  # filled by build_schedule from ff.engine.economics
        "capacity_pressure": {},  # filled by build_schedule from ff.services.capacity
    }
