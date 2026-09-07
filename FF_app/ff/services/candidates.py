"""Ranked next-best candidates — decomposed, explainable, scope-filtered.

``rank(snap, scope, limit=25) -> [Candidate]`` where Candidate is::

    {"task_id", "rank", "score",
     "components": {name: {"raw", "weight", "points"}},
     "reason_codes": [...], "explanation": str}

Rules enforced here:

- **One factor module (contract).** Scores come from
  ``ff.services.points.score_components`` with the CANDIDATE_WEIGHTS
  profile — this module implements NO factor math of its own, so the
  candidate list and the points layer can never disagree on why a task
  matters. Candidate score = sum of the EFFORT-WEIGHTED factor
  contributions (GG-2: each is effort x weight% x normalized factor; no
  standalone effort term, no OOS penalty — those belong to the game
  profile).
- **Feasibility gate.** Only tasks ``ff.services.feasibility`` classifies
  READY are ranked: a candidate list never suggests work that is waiting
  on a predecessor, on parts, or on a crew (OR-1: a task that cannot field
  its full crew is not a candidate, it is a wait).
- **Scope filtering** ``{team?, shift?, aircraft?}`` — unknown/None values
  mean "no filter". Team scope is exact-match (OR-2: a lead never sees
  another team's work as their candidate). Shift scope keeps tasks whose
  booked assignment sits on that shift; a READY task with no booking is
  kept only if the team's shift pool can field its FULL crew (never
  suggest a shift that cannot staff it).
- **Determinism.** Sort by (-score, task_id); rank is 1-based over the
  returned page.
"""

from __future__ import annotations

from ff.domain import Fleet, Schedule
from ff.services import feasibility, points
from ff.services.snapshot import component, qualified_count

DEFAULT_LIMIT = 25


def _coerce_int(value) -> int | None:
    """Parse an optional scope value ('2', 2, '', None) to int or None."""
    if value is None or value == "":
        return None
    return int(value)


def rank(snap, scope: dict | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Return the top ``limit`` READY tasks in scope, best first.

    Enforces the module-docstring rules: feasibility-gated (READY only),
    scope-filtered (team exact / aircraft exact / shift bookable),
    scored by the SHARED effort-weighted factor module (CANDIDATE_WEIGHTS
    profile), and deterministically ordered by (-score, task_id) with
    1-based ranks. ``limit <= 0`` returns an empty list.
    """
    scope = scope or {}
    team = scope.get("team") or None
    shift = _coerce_int(scope.get("shift"))
    aircraft = _coerce_int(scope.get("aircraft"))
    limit = int(limit)
    if limit <= 0:
        return []

    fleet: Fleet = component(snap, "fleet")
    schedule: Schedule = component(snap, "schedule")

    rows: list[dict] = []
    for task in sorted(fleet.tasks, key=lambda t: t.task_id):
        # -- scope filters (cheap screens before feasibility) ---------------
        if team is not None and task.team != team:
            continue  # OR-2: never surface another team's work
        if aircraft is not None and task.aircraft != aircraft:
            continue

        # -- feasibility gate: READY or it is not a candidate ---------------
        verdict = feasibility.evaluate(task.task_id, snap)
        if verdict["status"] != feasibility.READY:
            continue

        # -- shift scope: booked shift wins; else full-crew pool check ------
        if shift is not None:
            assignment = schedule.assignments.get(task.task_id)
            if assignment is not None:
                if assignment.shift != shift:
                    continue
            elif (
                qualified_count(snap, task.team, shift, task.skill)
                < task.mechanics_required
            ):
                continue  # OR-1: that shift can never field the full crew

        # -- score via THE shared factor module (candidates profile) --------
        components = points.score_components(
            task.task_id, snap, points.CANDIDATE_WEIGHTS
        )
        score = sum(c["points"] for c in components.values())
        rows.append(
            {
                "task_id": task.task_id,
                "rank": 0,  # assigned after the deterministic sort
                "score": score,
                "components": components,
                "reason_codes": verdict["reason_codes"],
                "explanation": points.build_explanation(task, components),
            }
        )

    rows.sort(key=lambda r: (-r["score"], r["task_id"]))
    page = rows[:limit]
    for position, row in enumerate(page, start=1):
        row["rank"] = position
    return page
