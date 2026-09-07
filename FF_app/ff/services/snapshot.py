"""Snapshot assembly — the immutable read-model every service consumes.

``build_snapshot(fleet, schedule, cpm) -> Snapshot`` where Snapshot is a
plain dict::

    {"snapshot_id": str,      # sha256(seed + sorted assignment tuples)[:16]
     "created_at": str,       # UTC ISO-8601 freshness stamp
     "fleet": Fleet,          # domain objects (not serialized copies)
     "schedule": Schedule,
     "cpm": dict[TaskKey, CpmInfo],
     "freshness": {"mock_data": bool, "seed": int}}

Rules enforced here:

- **Determinism.** ``snapshot_id`` is a pure function of the generator seed
  and the schedule's assignments (sorted by task_id, crews sorted by
  mech_id) — identical fleet+schedule always hash to the identical id.
  ``created_at`` is a wall-clock freshness stamp and is deliberately
  EXCLUDED from the hash.
- **OR-5 honesty labeling.** ``freshness.mock_data`` propagates the
  fleet's synthetic-data label so every page rendered from a snapshot can
  show the mock-data banner + freshness stamp.
- **Runtime caches.** Services memoize derived indexes directly on the
  snapshot dict under keys starting with ``_`` (plus the contract's
  ``normalizers`` key, frozen by ``ff.services.points``). Serializers must
  drop ``_``-prefixed keys and use ``to_dict()`` on the components.

Shared accessors (``component``, ``tasks_by_id``, ``qualified_count``) live
here so feasibility/candidates/points mirror ONE definition of the roster
pool (OR-2: a pool is team-pure — team + shift + skill, never borrowed).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ff.domain import Fleet, Mechanic, Schedule, SHIFTS, Task, TaskKey

# ---------------------------------------------------------------------------
# component accessors
# ---------------------------------------------------------------------------


def component(snap, name: str):
    """Return the ``fleet`` / ``schedule`` / ``cpm`` component of a snapshot.

    Accepts a Snapshot dict (canonical) or any object exposing the
    attribute; dict-encoded fleet/schedule components are inflated to
    dataclasses once and written back so every service shares one instance.
    Raises ``ValueError`` when the component is missing — a snapshot
    without its parts is corrupt, never soft-skipped.
    """
    is_dict = isinstance(snap, dict)
    value = snap.get(name) if is_dict else getattr(snap, name, None)
    if value is None:
        raise ValueError(f"snapshot has no {name!r} component")
    if name == "fleet" and isinstance(value, dict):
        value = Fleet.from_dict(value)
        if is_dict:
            snap[name] = value
    if name == "schedule" and isinstance(value, dict):
        value = Schedule.from_dict(value)
        if is_dict:
            snap[name] = value
    return value


def tasks_by_id(snap) -> dict[TaskKey, Task]:
    """Return (and memoize on the snapshot) the task_id -> Task index.

    Deterministic: built from tasks sorted by task_id; the FIRST occurrence
    of a duplicated id wins (mirrors the scheduler's defensive dedupe).
    """
    if isinstance(snap, dict):
        cached = snap.get("_index_tasks")
        if cached is not None:
            return cached
    fleet: Fleet = component(snap, "fleet")
    index: dict[TaskKey, Task] = {}
    for task in sorted(fleet.tasks, key=lambda t: t.task_id):
        index.setdefault(task.task_id, task)
    if isinstance(snap, dict):
        snap["_index_tasks"] = index
    return index


def qualified_count(snap, team: str, shift: int, skill: str) -> int:
    """Count the (team, shift) mechanics holding ``skill`` ("ANY" = all).

    THE shared pool definition (mirrors ``scheduler._Engine.qualified``):
    OR-2 — only mechanics with ``mech.team == team`` are ever counted, so
    no service can even express a borrowed crew. Memoized on the snapshot.
    """
    cache_key = f"{team}|{shift}|{skill}"
    cache: dict[str, int] | None = None
    if isinstance(snap, dict):
        cache = snap.setdefault("_pool_counts", {})
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    fleet: Fleet = component(snap, "fleet")
    seen: set[str] = set()
    count = 0
    for mech in sorted(fleet.mechanics, key=lambda m: m.mech_id):
        if mech.mech_id in seen:
            continue  # defensive roster dedupe (mirrors scheduler)
        seen.add(mech.mech_id)
        if mech.team != team or mech.shift != shift:
            continue
        if skill == "ANY" or skill in mech.skills:
            count += 1
    if cache is not None:
        cache[cache_key] = count
    return count


def max_pool_size(snap, team: str, skill: str) -> int:
    """Largest single-shift qualified pool for (team, skill) across shifts.

    Mirrors ``scheduler._Engine.static_reason``'s sizing: a crew larger
    than this can NEVER be fielded (OR-1 full-crew law makes the task
    statically unstaffable), and 0 means no skill holder exists at all.
    """
    return max(qualified_count(snap, team, s, skill) for s in SHIFTS)


# ---------------------------------------------------------------------------
# snapshot_id + assembly
# ---------------------------------------------------------------------------


def compute_snapshot_id(seed, schedule: Schedule) -> str:
    """sha256 over ``seed`` + sorted assignment tuples, first 16 hex chars.

    Enforces the contract's id rule: the digest folds in the seed then
    every assignment as a canonical tuple (task_id, day, shift,
    start_minute, end_minute, sorted crew, team, skill, uses_overtime) in
    task_id order — identical schedules hash identically regardless of
    dict insertion order or crew list order.
    """
    digest = hashlib.sha256()
    digest.update(repr(seed).encode("utf-8"))
    for tid in sorted(schedule.assignments):
        asg = schedule.assignments[tid]
        canonical = (
            asg.task_id,
            int(asg.day),
            int(asg.shift),
            int(asg.start_minute),
            int(asg.end_minute),
            tuple(sorted(asg.mechanic_ids)),
            asg.team,
            asg.skill,
            bool(asg.uses_overtime),
        )
        digest.update(repr(canonical).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_snapshot(fleet: Fleet, schedule: Schedule, cpm: dict) -> dict:
    """Assemble the Snapshot read-model for the services + web layer.

    Enforces OR-5: the freshness block carries the fleet's ``mock_data``
    label (defaulting to True — synthetic until proven otherwise, never
    the reverse) and the generator ``seed``, and ``created_at`` gives every
    page an honest staleness stamp. ``snapshot_id`` is deterministic (see
    ``compute_snapshot_id``); ``created_at`` is not part of the hash.
    """
    seed = fleet.meta.get("seed") if isinstance(fleet.meta, dict) else None
    mock = bool(fleet.meta.get("mock_data", True)) if isinstance(fleet.meta, dict) else True
    return {
        "snapshot_id": compute_snapshot_id(seed, schedule),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fleet": fleet,
        "schedule": schedule,
        "cpm": cpm,
        "freshness": {"mock_data": mock, "seed": seed},
    }
