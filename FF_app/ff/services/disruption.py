"""Disruption attribution + excusals — WHY work is not moving, honestly coded.

The 8 causes (verbatim ids, fixed order). ``+`` = EXCUSABLE (outside the
crew's control — GG-3 lets these leave a shift-goal denominator), ``-`` =
NOT excusable (stays in the denominator; the team owns it), ``±`` =
record-level conditional (owner rulings 2026-07-11):

    LATE_PART               +  material not here
    CROSS_TEAM_PREDECESSOR  +  waiting on ANOTHER team's unfinished work
    SAME_TEAM_PREDECESSOR   ±  waiting on the team's OWN unfinished work:
                               owned — UNLESS the own-team chain's ROOT
                               cause is external (parts/capacity/skill or
                               a cross-team wait upstream): root-cause
                               pass-through excuses the successor
    CAPACITY_SHORTAGE       +  full crew exists but no window in horizon
    SKILL_SHORTAGE          +  roster cannot field the crew at all
    DURATION_OVERRUN        ±  work ran long: excusable while actual burn
                               <= OVERRUN_EXCUSE_FACTOR x standard
                               (needs an actuals feed; without one a
                               capture stays owned), OWNED beyond it
    REWORK_INJECTION        ±  rework drops with a parent_soi; the parent
                               SOI's team OWNS it — excusable only for a
                               DIFFERENT team executing the fix
    PLAN_CHURN              -  plan moved under the team (manual only)

``attribute(snap)`` derives auto ExcusalRecords deterministically from the
snapshot (task states, feasibility verdicts, unscheduled reason codes);
``merged_excusals(snap, manual)`` folds in lead-captured records — manual
EXTENDS auto, never replaces it, and a duplicate (same task + cause)
collapses to the auto record.

ExcusalRecord (plain dict): ``{task_id, team, day, shift, cause,
excusable, evidence: str, source: "auto"|"manual"}`` — day/shift come from
the task's assignment slot, or ``(today, 0)`` when unplaced (shift 0 =
unslotted, so it can never collide with a real planned slice).
"""

from __future__ import annotations

import config
from ff.services import feasibility
from ff.services.snapshot import component, tasks_by_id

# The 8 causes — VERBATIM ids, fixed order (drives dropdowns and pareto).
LATE_PART = "LATE_PART"
CROSS_TEAM_PREDECESSOR = "CROSS_TEAM_PREDECESSOR"
SAME_TEAM_PREDECESSOR = "SAME_TEAM_PREDECESSOR"
CAPACITY_SHORTAGE = "CAPACITY_SHORTAGE"
SKILL_SHORTAGE = "SKILL_SHORTAGE"
DURATION_OVERRUN = "DURATION_OVERRUN"
REWORK_INJECTION = "REWORK_INJECTION"
PLAN_CHURN = "PLAN_CHURN"

CAUSES: tuple[str, ...] = (
    LATE_PART,
    CROSS_TEAM_PREDECESSOR,
    SAME_TEAM_PREDECESSOR,
    CAPACITY_SHORTAGE,
    SKILL_SHORTAGE,
    DURATION_OVERRUN,
    REWORK_INJECTION,
    PLAN_CHURN,
)

# The 5 excusable (+) causes — EXACT set (tripwire-tested).
EXCUSABLE: frozenset = frozenset(
    {
        LATE_PART,
        CROSS_TEAM_PREDECESSOR,
        CAPACITY_SHORTAGE,
        SKILL_SHORTAGE,
        REWORK_INJECTION,
    }
)

# Manual capture: these causes REQUIRE lead notes (they point at the team's
# own execution, so the claim must carry an explanation).
NOTES_REQUIRED: frozenset = frozenset({SAME_TEAM_PREDECESSOR, DURATION_OVERRUN})

# Unscheduled reason codes -> causes (scheduler's exact strings).
_CAPACITY_REASONS = ("no_crew_within_horizon",)
_SKILL_REASONS = ("no_skill_holder", "crew_exceeds_pool")


def rework_origin_of(task, tasks: dict | None = None) -> str | None:
    """The defect-origin team under the parent-SOI paperwork rule.

    Explicit ``rework_origin_team`` wins (the writer recorded the
    paperwork). When the field is absent (rows written before the field
    existed, e.g. generator punch work), the SAME rule is applied by
    derivation: the parent SOI is the task's predecessor, and the parent
    SOI's team owns the rework. None only when no parent is resolvable —
    genuinely unattributed paperwork.
    """
    origin = getattr(task, "rework_origin_team", None)
    if origin and str(origin).strip():
        return str(origin)
    if tasks:
        for p in task.predecessors:
            parent = tasks.get(p)
            if parent is not None and p != task.task_id:
                return parent.team
    return None


def rework_excusable(task, tasks: dict | None = None) -> bool:
    """Origin-aware REWORK_INJECTION excusability (owner ruling: the
    parent SOI's team OWNS the rework).

    Excusable ONLY when the defect origin (explicit field, or derived
    from the parent SOI via ``rework_origin_of``) is a DIFFERENT team —
    true injected work. Origin == own team: self-caused, owned. Origin
    unresolvable: ``config.REWORK_UNATTRIBUTED_EXCUSABLE`` decides
    (default False — own it until the paperwork says otherwise).
    """
    origin = rework_origin_of(task, tasks)
    if origin is None:
        return bool(config.REWORK_UNATTRIBUTED_EXCUSABLE)
    return origin != task.team


def _record(
    task, day: int, shift: int, cause: str, evidence: str,
    excusable: bool | None = None,
) -> dict:
    """Assemble one auto ExcusalRecord (contract shape, nothing else).

    ``excusable`` overrides the static cause-set membership for the one
    cause whose excusability is record-level: REWORK_INJECTION under the
    origin rule (``rework_excusable``).
    """
    return {
        "task_id": task.task_id,
        "team": task.team,
        "day": int(day),
        "shift": int(shift),
        "cause": cause,
        "excusable": (cause in EXCUSABLE) if excusable is None else bool(excusable),
        "evidence": evidence,
        "source": "auto",
    }


# Owner-tunable (config §5, ruling update 2026-07-11): 5 upstream hops.


def _own_chain_root_excuse(task, first_blocker: str, snap, tasks, schedule):
    """Walk an OWN-TEAM blocking chain to its root; return an evidence
    string when the root cause is EXTERNAL, else None (owned).

    Owner ruling 2's pass-through: "if the upstream predecessor task that
    delayed is caused by another team, then excused" — parts vendors,
    capacity/skill shortages, and cross-team waits further up the chain
    are all another party's causation even when reached through own-team
    intermediaries. A chain that bottoms out on an own-team task that is
    simply not done (no external condition) is owned. Deterministic;
    cycle-guarded; capped at config.CHAIN_WALK_CAP hops (a chain that deep is
    owned by construction — the team has had every chance to work it).
    """
    seen: set = set()
    current = first_blocker
    for _ in range(config.CHAIN_WALK_CAP):
        if current in seen:
            return None  # cycle: no external root found
        seen.add(current)
        blocker = tasks.get(current)
        if blocker is None:
            return None
        # External condition ON the blocker itself?
        if blocker.state == "blocked" and blocker.parts_eta_day is not None:
            return f"{blocker.task_id} LATE_PART (ETA day {blocker.parts_eta_day})"
        reason = schedule.unscheduled.get(blocker.task_id)
        if reason in _CAPACITY_REASONS:
            return f"{blocker.task_id} CAPACITY_SHORTAGE ({reason})"
        if reason in _SKILL_REASONS:
            return f"{blocker.task_id} SKILL_SHORTAGE ({reason})"
        # Otherwise: is the blocker itself waiting on a predecessor?
        if not blocker.predecessors:
            return None  # ready-but-not-done own work: owned
        verdict = feasibility.evaluate(blocker.task_id, snap)
        if verdict["status"] != feasibility.WAITING_PREDECESSOR:
            return None  # blocker is workable now: owned
        if verdict["blocking_team"] != task.team:
            return (
                f"{blocker.task_id} waiting on {verdict['blocking_task']} "
                f"(team {verdict['blocking_team']})"
            )
        current = verdict["blocking_task"]  # continue up the own-team chain
    return None


def attribute(snap) -> list[dict]:
    """Derive the deterministic auto-attribution ExcusalRecords.

    Rules (contract addendum, each tripwire-tested; one task may match
    several — the records are independent):

    - blocked task WITH a parts ETA -> LATE_PART ("parts ETA day N");
    - feasibility WAITING_PREDECESSOR with blocking_team != own team ->
      CROSS_TEAM_PREDECESSOR (evidence names the blocking task + team);
      same team -> SAME_TEAM_PREDECESSOR (not excusable — own work);
    - unscheduled 'no_crew_within_horizon' -> CAPACITY_SHORTAGE;
    - unscheduled 'no_skill_holder'/'crew_exceeds_pool' -> SKILL_SHORTAGE;
    - is_rework and not_started -> REWORK_INJECTION for the RECEIVING team
      (the task's own team is the one absorbing the injected work).

    Deterministic: tasks iterate sorted by task_id with a fixed intra-task
    rule order; memoized on the snapshot (``_excusals_auto``).
    """
    if isinstance(snap, dict):
        cached = snap.get("_excusals_auto")
        if cached is not None:
            return cached
    schedule = component(snap, "schedule")
    tasks = tasks_by_id(snap)
    today = int(schedule.meta.get("start_day", 0)) if schedule.meta else 0

    records: list[dict] = []
    for tid in sorted(tasks):
        task = tasks[tid]
        if task.state == "done":
            continue
        asg = schedule.assignments.get(tid)
        day, shift = (asg.day, asg.shift) if asg is not None else (today, 0)

        # LATE_PART — material wait with a recorded ETA.
        if task.state == "blocked" and task.parts_eta_day is not None:
            records.append(
                _record(task, day, shift, LATE_PART,
                        f"parts ETA day {task.parts_eta_day}")
            )

        # Predecessor waits (mirrors feasibility, which mirrors the engine).
        # Owner ruling 2 + root-cause pass-through: a wait on ANOTHER
        # team's predecessor is excused; a wait on the OWN team's
        # predecessor is owned — UNLESS the own-team chain's ROOT cause is
        # external (parts / capacity / skill shortage, or a cross-team
        # wait further up): "caused by another team/party" excuses the
        # successor even through own-team intermediaries.
        if task.predecessors:
            verdict = feasibility.evaluate(tid, snap)
            if verdict["status"] == feasibility.WAITING_PREDECESSOR:
                blocking_task = verdict["blocking_task"]
                blocking_team = verdict["blocking_team"]
                if blocking_team != task.team:
                    records.append(
                        _record(
                            task, day, shift, CROSS_TEAM_PREDECESSOR,
                            f"waiting on {blocking_task} (team {blocking_team})",
                        )
                    )
                else:
                    root = _own_chain_root_excuse(
                        task, blocking_task, snap, tasks, schedule
                    )
                    if root is not None:
                        records.append(
                            _record(
                                task, day, shift, SAME_TEAM_PREDECESSOR,
                                f"waiting on {blocking_task} (own team) — "
                                f"root cause external: {root}",
                                excusable=True,
                            )
                        )
                    else:
                        records.append(
                            _record(
                                task, day, shift, SAME_TEAM_PREDECESSOR,
                                f"waiting on {blocking_task} (own team)",
                            )
                        )

        # Unscheduled reason codes (the engine's honest delay verdicts).
        reason = schedule.unscheduled.get(tid)
        if reason in _CAPACITY_REASONS:
            records.append(
                _record(task, day, shift, CAPACITY_SHORTAGE, f"unscheduled: {reason}")
            )
        elif reason in _SKILL_REASONS:
            records.append(
                _record(task, day, shift, SKILL_SHORTAGE, f"unscheduled: {reason}")
            )

        # DURATION_OVERRUN — owner ruling 3: work that has burned MORE than
        # its allotted standard. Excusable while actual <= OVERRUN_EXCUSE_
        # FACTOR x standard (normal variation / standards noise — the team
        # is not punished for the estimate); OWNED beyond the factor (a 3x
        # blowout has a story the team must tell). Auto-fires only when an
        # actuals feed exists (actual_minutes is not None) and the task is
        # still open — finished work needs no excuse (GG-3).
        actual = getattr(task, "actual_minutes", None)
        if (
            actual is not None
            and task.state == "in_progress"
            and task.duration_minutes > 0
            and actual > task.duration_minutes
        ):
            ratio = actual / task.duration_minutes
            records.append(
                _record(
                    task, day, shift, DURATION_OVERRUN,
                    f"actual {actual}m vs standard {task.duration_minutes}m "
                    f"({ratio:.1f}x; tolerance {config.OVERRUN_EXCUSE_FACTOR:.1f}x)",
                    excusable=ratio <= config.OVERRUN_EXCUSE_FACTOR,
                )
            )

        # REWORK_INJECTION — new punch work absorbed by the receiving team.
        # Origin-aware (owner directive): self-caused rework is OWNED, not
        # excused; only cross-team-origin rework leaves the denominator.
        if task.is_rework and task.state == "not_started":
            origin = rework_origin_of(task, tasks)
            derived = (
                ""
                if getattr(task, "rework_origin_team", None)
                else " [derived from parent SOI]"
            )
            if origin and origin != task.team:
                why = (
                    f"rework injected onto team {task.team} "
                    f"(origin {origin}{derived})"
                )
            elif origin:
                why = f"self-caused rework (origin {origin} = own team{derived})"
            else:
                why = f"rework injected onto team {task.team} (origin unattributed)"
            records.append(
                _record(task, day, shift, REWORK_INJECTION, why,
                        excusable=rework_excusable(task, tasks))
            )

    if isinstance(snap, dict):
        snap["_excusals_auto"] = records
    return records


def _normalize_manual(rec: dict) -> dict | None:
    """Coerce a stored manual capture into the ExcusalRecord shape.

    Extra keys (notes, entered_by, ts) are preserved — the capture trail
    stays visible; ``excusable`` is recomputed from the cause so a stored
    file can never smuggle in a wrong excusability flag.
    """
    if not isinstance(rec, dict):
        return None
    task_id = rec.get("task_id")
    cause = rec.get("cause")
    if not task_id or cause not in CAUSES:
        return None
    out = dict(rec)
    out["task_id"] = str(task_id)
    out["cause"] = cause
    out["excusable"] = cause in EXCUSABLE
    out["source"] = "manual"
    out["team"] = str(rec.get("team", ""))
    out["day"] = int(rec.get("day", 0))
    out["shift"] = int(rec.get("shift", 0))
    out.setdefault("evidence", "")
    return out


def merged_excusals(snap, manual: list) -> dict:
    """Merge auto attribution with lead-captured records, grouped by slot.

    Returns ``{(team, day, shift): [ExcusalRecord, ...]}``. Rules enforced:

    - manual captures EXTEND auto attribution, NEVER replace it — every
      auto record survives the merge untouched;
    - duplicates (same task_id + cause) collapse to the AUTO record (and a
      repeated manual capture collapses to the first);
    - deterministic output: groups sorted internally by
      (task_id, cause, source).
    """
    auto = attribute(snap)
    seen: set = {(r["task_id"], r["cause"]) for r in auto}
    rows = list(auto)
    tasks = tasks_by_id(snap)
    for raw in manual or []:
        rec = _normalize_manual(raw)
        if rec is None:
            continue
        key = (rec["task_id"], rec["cause"])
        if key in seen:
            continue  # duplicate collapses to auto (or to the first manual)
        # Origin rule applies to MANUAL captures too — a lead cannot excuse
        # self-caused rework by typing the cause in (owner directive).
        if rec["cause"] == REWORK_INJECTION:
            task = tasks.get(rec["task_id"])
            if task is not None:
                rec["excusable"] = rework_excusable(task, tasks)
        # Ruling 3 applies to manual overrun captures the same way: when an
        # actuals feed exists, the tolerance factor decides — a lead's note
        # adds context, never flips a >3x blowout back to excused.
        elif rec["cause"] == DURATION_OVERRUN:
            task = tasks.get(rec["task_id"])
            actual = getattr(task, "actual_minutes", None) if task else None
            if task is not None and actual is not None and task.duration_minutes > 0:
                rec["excusable"] = (
                    actual / task.duration_minutes <= config.OVERRUN_EXCUSE_FACTOR
                )
        seen.add(key)
        rows.append(rec)

    groups: dict = {}
    for rec in rows:
        groups.setdefault((rec["team"], rec["day"], rec["shift"]), []).append(rec)
    for group in groups.values():
        group.sort(key=lambda r: (r["task_id"], r["cause"], r["source"]))
    return groups
