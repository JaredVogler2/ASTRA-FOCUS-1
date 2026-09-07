"""Schedule validator — V1..V9 constraint walker over a Schedule + Fleet.

``validate(schedule, fleet)`` returns::

    {"violations": [{"id": "V3", "task_id": "...", "msg": "..."}],
     "summary":    {"V1": 0, ..., "V9": 2, "total": 2},
     "checks":     ["V1", ..., "V9"]}

Zero-tolerance doctrine: EVERY assignment is checked by EVERY applicable
V-check; no task flag (``is_rework``, ``is_inspection``, live ``state``)
exempts a task from any check. A clean schedule yields zero violations; any
violation means the engine (or hand-edited data) broke a law.

The nine checks (contract, ARCHITECTURE.md §ff/engine/validator.py):

    V1 duration identity        end-start == duration (remaining_minutes for
                                in_progress); also catches unknown task ids.
    V2 precedence order         pred end (slot, minute) <= succ start,
                                incl. the same-slot minute rule; a scheduled
                                task with a non-done, unplaced predecessor
                                is a violation too.
    V3 working-day / OR-3       placement only on eligible slots; shift 3 on
                                a non-working day is legal iff the next day
                                works (Sunday-night rule).
    V4 shift fit                start <= SHIFT_EFFECTIVE - NO_START_BUFFER,
                                end <= SHIFT_MAX, start >= 0, positive span,
                                uses_overtime flag honest.
    V5 full crew (OR-1/OR-2)    len(mechanic_ids) == mechanics_required,
                                distinct, real roster ids, same team as the
                                task (no borrowing), correct shift, skill
                                held ("ANY" matches all).
    V6 mechanic non-overlap     one mechanic, one place at a time.
    V7 per-mechanic load cap    sum of booked minutes per (mech, day, shift)
                                <= SHIFT_EFFECTIVE + OVERTIME.
    V8 activation quota         distinct mechanics per (team, shift, day)
                                <= floor(pool_size * UTILIZATION).
    V9 parts-ETA floor          a task with parts_eta_day set never starts
                                before that day (no state exempts it).

Deterministic: violations are emitted in check order, then sorted iteration
order within each check — identical inputs yield identical reports.
"""

from __future__ import annotations

import math

import config
from ff.domain import (
    Assignment,
    Fleet,
    Schedule,
    shift_eligible,
    slot_index,
    SHIFTS,
)

CHECK_IDS: tuple[str, ...] = ("V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9")


class _Ctx:
    """Precomputed lookup context shared by all V-checks (read-only)."""

    def __init__(self, schedule: Schedule, fleet: Fleet):
        self.schedule = schedule
        self.fleet = fleet
        self.tasks = {t.task_id: t for t in fleet.tasks}
        self.mechanics = {m.mech_id: m for m in fleet.mechanics}
        # Roster pool sizes per (team, shift) — denominator of the V8 quota.
        self.pool_size: dict[tuple[str, int], int] = {}
        for m in fleet.mechanics:
            key = (m.team, m.shift)
            self.pool_size[key] = self.pool_size.get(key, 0) + 1
        # Assignments in deterministic (task_id) order.
        self.assignments: list[tuple[str, Assignment]] = sorted(
            schedule.assignments.items()
        )

    def shift_valid(self, asg: Assignment) -> bool:
        return asg.shift in SHIFTS


def _v(check_id: str, task_id: str, msg: str) -> dict:
    return {"id": check_id, "task_id": task_id, "msg": msg}


def _expected_span(ctx: _Ctx, task_id: str) -> int | None:
    """Minutes an assignment for ``task_id`` must span (V1 identity).

    Enforces the live-state rule: an ``in_progress`` task with a known
    ``remaining_minutes`` must be booked for exactly the remaining work;
    everything else for its full ``duration_minutes``.
    """
    task = ctx.tasks.get(task_id)
    if task is None:
        return None
    if task.state == "in_progress" and task.remaining_minutes is not None:
        return max(0, int(task.remaining_minutes))
    return int(task.duration_minutes)


# ---------------------------------------------------------------------------
# V1 — duration identity
# ---------------------------------------------------------------------------


def _check_v1(ctx: _Ctx) -> list[dict]:
    """V1: end_minute - start_minute equals the task's booked work content.

    Catches wrong durations (padded or shaved bookings) and assignments for
    task ids that do not exist in the fleet at all.
    """
    out = []
    for tid, asg in ctx.assignments:
        expected = _expected_span(ctx, tid)
        if expected is None:
            out.append(_v("V1", tid, "assignment for unknown task id"))
            continue
        span = asg.end_minute - asg.start_minute
        if span != expected:
            out.append(
                _v(
                    "V1",
                    tid,
                    f"duration identity broken: booked {span} min "
                    f"({asg.start_minute}->{asg.end_minute}), task requires "
                    f"{expected} min",
                )
            )
        if asg.task_id != tid:
            out.append(
                _v("V1", tid, f"assignment key {tid!r} != payload task_id {asg.task_id!r}")
            )
    return out


# ---------------------------------------------------------------------------
# V2 — precedence order (incl. same-slot minute rule)
# ---------------------------------------------------------------------------


def _check_v2(ctx: _Ctx) -> list[dict]:
    """V2: every predecessor finishes at or before its successor starts.

    Enforces pred.end (slot, minute) <= succ.start: an earlier slot_index
    suffices; the SAME slot requires ``succ.start_minute >= pred.end_minute``.
    A scheduled task whose non-done predecessor has no assignment is also a
    violation (work cannot start on top of unplaced prerequisite work).
    Predecessor references outside the fleet are treated as satisfied
    (mirrors the C12 resolution rule in cpm/scheduler).

    VOID-EDGE exemption (mirrors the scheduler): a successor whose state is
    ``in_progress`` already STARTED on the floor — reality outranks the
    plan, its incoming precedence edges are void, and rebooking its
    REMAINING minutes ahead of a still-unfinished predecessor is honest
    bookkeeping, not a plan defect (the points engine charges P_OOS for the
    out-of-sequence start itself). The exemption is keyed strictly on task
    STATE (floor data), never on anything the engine decided, so genuine
    ordering bugs on not_started work are still caught.
    """
    out = []
    for tid, asg in ctx.assignments:
        task = ctx.tasks.get(tid)
        if task is None:
            continue  # reported by V1
        if task.state == "in_progress":
            continue  # void-edge exemption: started work owns its sequence
        if not ctx.shift_valid(asg):
            continue  # reported by V3; slot_index undefined
        succ_slot = slot_index(asg.day, asg.shift)
        for pred_id in sorted(set(task.predecessors)):
            pred = ctx.tasks.get(pred_id)
            if pred is None or pred_id == tid:
                continue  # missing / self reference: treated as satisfied
            if pred.state == "done":
                continue  # finished work satisfies precedence
            pred_asg = ctx.schedule.assignments.get(pred_id)
            if pred_asg is None:
                out.append(
                    _v(
                        "V2",
                        tid,
                        f"predecessor {pred_id} is {pred.state} and unplaced "
                        f"but successor is scheduled",
                    )
                )
                continue
            if not ctx.shift_valid(pred_asg):
                continue
            pred_slot = slot_index(pred_asg.day, pred_asg.shift)
            if pred_slot > succ_slot:
                out.append(
                    _v(
                        "V2",
                        tid,
                        f"predecessor {pred_id} in slot d{pred_asg.day}/s{pred_asg.shift} "
                        f"after successor slot d{asg.day}/s{asg.shift}",
                    )
                )
            elif pred_slot == succ_slot and asg.start_minute < pred_asg.end_minute:
                out.append(
                    _v(
                        "V2",
                        tid,
                        f"same-slot minute rule broken: starts at minute "
                        f"{asg.start_minute} before predecessor {pred_id} ends "
                        f"at {pred_asg.end_minute}",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# V3 — working-day / Sunday-night (OR-3) rule
# ---------------------------------------------------------------------------


def _check_v3(ctx: _Ctx) -> list[dict]:
    """V3: assignments occupy only plannable slots.

    Enforces the calendar law + OR-3 (WEEK_STARTS_SUNDAY_NIGHT): shifts 1-2
    need a working day; shift 3 on a non-working day ``d`` is permitted iff
    ``d+1`` is a working day (Sunday night yes, Saturday night no). Negative
    days and unknown shift numbers are violations too.
    """
    out = []
    for tid, asg in ctx.assignments:
        if not ctx.shift_valid(asg):
            out.append(_v("V3", tid, f"unknown shift {asg.shift!r} (must be 1|2|3)"))
            continue
        if asg.day < 0:
            out.append(_v("V3", tid, f"negative day {asg.day}"))
            continue
        if not shift_eligible(asg.day, asg.shift):
            out.append(
                _v(
                    "V3",
                    tid,
                    f"slot d{asg.day}/s{asg.shift} not plannable "
                    f"(working-day/Sunday-night rule, day%7={asg.day % 7})",
                )
            )
    return out


# ---------------------------------------------------------------------------
# V4 — shift fit + no-start buffer
# ---------------------------------------------------------------------------


def _check_v4(ctx: _Ctx) -> list[dict]:
    """V4: the minute window fits the shift.

    Enforces: start >= 0; start <= SHIFT_EFFECTIVE - NO_START_BUFFER (no
    late starts); end <= SHIFT_MAX (overtime headroom is the hard wall);
    end > start; and the ``uses_overtime`` flag is honest
    (True iff end > SHIFT_EFFECTIVE).
    """
    out = []
    for tid, asg in ctx.assignments:
        if not ctx.shift_valid(asg):
            continue  # reported by V3
        eff = config.SHIFT_EFFECTIVE[asg.shift]
        hard_max = config.SHIFT_MAX[asg.shift]
        start_limit = eff - config.NO_START_BUFFER
        if asg.start_minute < 0:
            out.append(_v("V4", tid, f"negative start minute {asg.start_minute}"))
        if asg.end_minute <= asg.start_minute:
            out.append(
                _v(
                    "V4",
                    tid,
                    f"non-positive span: start {asg.start_minute} >= end {asg.end_minute}",
                )
            )
        if asg.start_minute > start_limit:
            out.append(
                _v(
                    "V4",
                    tid,
                    f"start minute {asg.start_minute} past no-start buffer "
                    f"limit {start_limit} (shift {asg.shift})",
                )
            )
        if asg.end_minute > hard_max:
            out.append(
                _v(
                    "V4",
                    tid,
                    f"end minute {asg.end_minute} past SHIFT_MAX {hard_max} "
                    f"(shift {asg.shift})",
                )
            )
        honest_ot = asg.end_minute > eff
        if bool(asg.uses_overtime) != honest_ot:
            out.append(
                _v(
                    "V4",
                    tid,
                    f"uses_overtime={asg.uses_overtime} inconsistent with end "
                    f"{asg.end_minute} vs SHIFT_EFFECTIVE {eff}",
                )
            )
    return out


# ---------------------------------------------------------------------------
# V5 — full crew, real roster, same team, right skill (OR-1 / OR-2)
# ---------------------------------------------------------------------------


def _check_v5(ctx: _Ctx) -> list[dict]:
    """V5: the FULL named crew law.

    Enforces OR-1 (full crew or wait: exactly ``mechanics_required`` DISTINCT
    named mechanics — a short OR padded crew is a violation), OR-2 (every
    crew member belongs to the task's team — no borrowing), roster reality
    (unknown mechanic ids are violations), shift consistency (each crew
    member's roster shift equals the assignment shift), and skill match
    (task skill "ANY" matches any mechanic; otherwise the mechanic must
    hold the skill). Assignment team/skill fields must match the task's.
    """
    out = []
    for tid, asg in ctx.assignments:
        task = ctx.tasks.get(tid)
        if task is None:
            continue  # reported by V1
        crew = list(asg.mechanic_ids)
        if len(crew) != task.mechanics_required:
            out.append(
                _v(
                    "V5",
                    tid,
                    f"short/padded crew: {len(crew)} booked, "
                    f"{task.mechanics_required} required (OR-1: full crew or wait)",
                )
            )
        if len(set(crew)) != len(crew):
            dupes = sorted({m for m in crew if crew.count(m) > 1})
            out.append(_v("V5", tid, f"duplicate mechanic ids in crew: {dupes}"))
        if asg.team != task.team:
            out.append(
                _v("V5", tid, f"assignment team {asg.team!r} != task team {task.team!r}")
            )
        if asg.skill != task.skill:
            out.append(
                _v(
                    "V5",
                    tid,
                    f"assignment skill {asg.skill!r} != task skill {task.skill!r}",
                )
            )
        for mech_id in sorted(set(crew)):
            mech = ctx.mechanics.get(mech_id)
            if mech is None:
                out.append(_v("V5", tid, f"unknown mechanic id {mech_id!r}"))
                continue
            if mech.team != task.team:
                out.append(
                    _v(
                        "V5",
                        tid,
                        f"cross-team mechanic {mech_id} (team {mech.team!r}) on "
                        f"task of team {task.team!r} (OR-2: no borrowing)",
                    )
                )
            if mech.shift != asg.shift:
                out.append(
                    _v(
                        "V5",
                        tid,
                        f"mechanic {mech_id} works shift {mech.shift}, "
                        f"assignment is shift {asg.shift}",
                    )
                )
            if task.skill != "ANY" and task.skill not in mech.skills:
                out.append(
                    _v(
                        "V5",
                        tid,
                        f"mechanic {mech_id} lacks skill {task.skill!r} "
                        f"(holds {sorted(mech.skills)})",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# V6 — mechanic non-overlap
# ---------------------------------------------------------------------------


def _mech_bookings(ctx: _Ctx) -> dict[tuple[str, int, int], list[tuple[int, int, str]]]:
    """Group bookings as (mech, day, shift) -> sorted [(start, end, task_id)]."""
    booked: dict[tuple[str, int, int], list[tuple[int, int, str]]] = {}
    for tid, asg in ctx.assignments:
        if not ctx.shift_valid(asg):
            continue
        for mech_id in sorted(set(asg.mechanic_ids)):
            booked.setdefault((mech_id, asg.day, asg.shift), []).append(
                (asg.start_minute, asg.end_minute, tid)
            )
    for lst in booked.values():
        lst.sort()
    return booked


def _check_v6(ctx: _Ctx) -> list[dict]:
    """V6: one mechanic, one place at a time.

    Enforces non-overlap of a mechanic's booked [start, end) windows within
    a (day, shift); back-to-back (next.start == prev.end) is legal.
    """
    out = []
    for (mech_id, day, shift), windows in sorted(_mech_bookings(ctx).items()):
        prev_end, prev_tid = -1, ""
        for start, end, tid in windows:
            if start < prev_end:
                out.append(
                    _v(
                        "V6",
                        tid,
                        f"mechanic {mech_id} double-booked in d{day}/s{shift}: "
                        f"overlaps task {prev_tid} (starts {start} < prior end {prev_end})",
                    )
                )
            if end > prev_end:
                prev_end, prev_tid = end, tid
    return out


# ---------------------------------------------------------------------------
# V7 — per-mechanic load cap
# ---------------------------------------------------------------------------


def _check_v7(ctx: _Ctx) -> list[dict]:
    """V7: per-mechanic booked minutes per (day, shift) <= SHIFT_EFFECTIVE + OVERTIME.

    Zero tolerance: the first assignment (in start-minute, task_id order)
    that pushes a mechanic's cumulative booked minutes over the cap is
    flagged; the cap is the effective shift length plus the overtime
    allowance, never more.
    """
    out = []
    for (mech_id, day, shift), windows in sorted(_mech_bookings(ctx).items()):
        cap = config.SHIFT_EFFECTIVE[shift] + config.OVERTIME
        load = 0
        flagged = False
        for start, end, tid in windows:
            load += max(0, end - start)
            if load > cap and not flagged:
                out.append(
                    _v(
                        "V7",
                        tid,
                        f"mechanic {mech_id} load {load} min exceeds cap {cap} "
                        f"in d{day}/s{shift}",
                    )
                )
                flagged = True
    return out


# ---------------------------------------------------------------------------
# V8 — activation quota
# ---------------------------------------------------------------------------


def _check_v8(ctx: _Ctx) -> list[dict]:
    """V8: distinct mechanics activated per (team, shift, day) stay within quota.

    Enforces the utilization law: at most ``floor(pool_size * UTILIZATION)``
    DISTINCT mechanics of a (team, shift) pool may be used on any one day
    (pool_size = roster head-count of that team+shift). The assignment that
    first grows the distinct-activation set past the quota is flagged.
    """
    out = []
    groups: dict[tuple[str, int, int], list[tuple[int, str, Assignment]]] = {}
    for tid, asg in ctx.assignments:
        if not ctx.shift_valid(asg):
            continue
        groups.setdefault((asg.team, asg.shift, asg.day), []).append(
            (asg.start_minute, tid, asg)
        )
    for (team, shift, day), entries in sorted(groups.items()):
        quota = math.floor(ctx.pool_size.get((team, shift), 0) * config.UTILIZATION)
        active: set[str] = set()
        for _start, tid, asg in sorted(entries, key=lambda e: (e[0], e[1])):
            grown = active | set(asg.mechanic_ids)
            if len(grown) > quota >= len(active):
                out.append(
                    _v(
                        "V8",
                        tid,
                        f"activation quota breach: {len(grown)} distinct mechanics "
                        f"of ({team}, shift {shift}) used on day {day}, quota "
                        f"{quota} (pool {ctx.pool_size.get((team, shift), 0)} x "
                        f"UTILIZATION {config.UTILIZATION})",
                    )
                )
            active = grown
    return out


# ---------------------------------------------------------------------------
# V9 — parts-ETA floor
# ---------------------------------------------------------------------------


def _check_v9(ctx: _Ctx) -> list[dict]:
    """V9: a task with a parts ETA never starts before the parts arrive.

    Zero tolerance: ANY task carrying ``parts_eta_day`` (regardless of its
    live state — no flag exempts it) must be assigned to a day >= that ETA.
    """
    out = []
    for tid, asg in ctx.assignments:
        task = ctx.tasks.get(tid)
        if task is None or task.parts_eta_day is None:
            continue
        if asg.day < task.parts_eta_day:
            out.append(
                _v(
                    "V9",
                    tid,
                    f"parts-ETA floor breach: scheduled day {asg.day} before "
                    f"parts_eta_day {task.parts_eta_day}",
                )
            )
    return out


_CHECKS = {
    "V1": _check_v1,
    "V2": _check_v2,
    "V3": _check_v3,
    "V4": _check_v4,
    "V5": _check_v5,
    "V6": _check_v6,
    "V7": _check_v7,
    "V8": _check_v8,
    "V9": _check_v9,
}


def validate(schedule, fleet) -> dict:
    """Run all V1..V9 checks and return the violation report.

    Accepts ``Schedule``/``Fleet`` dataclasses or their ``to_dict()`` forms
    (dicts are normalized first). Returns::

        {"violations": [{"id", "task_id", "msg"}, ...],
         "summary": {"V1": n1, ..., "V9": n9, "total": N},
         "checks": ["V1", ..., "V9"]}

    Deterministic: identical inputs produce an identical report. Zero
    violations is the ONLY passing state — there is no severity tiering
    and no exemption flag.
    """
    if isinstance(schedule, dict):
        schedule = Schedule.from_dict(schedule)
    if isinstance(fleet, dict):
        fleet = Fleet.from_dict(fleet)
    ctx = _Ctx(schedule, fleet)

    violations: list[dict] = []
    checks_run: list[str] = []
    for check_id in CHECK_IDS:
        violations.extend(_CHECKS[check_id](ctx))
        checks_run.append(check_id)

    summary = {check_id: 0 for check_id in CHECK_IDS}
    for violation in violations:
        summary[violation["id"]] += 1
    summary["total"] = len(violations)

    return {"violations": violations, "summary": summary, "checks": checks_run}
