"""FF_app domain model — dataclasses, serialization helpers, and time helpers.

Exact shapes per ARCHITECTURE.md. Serialization contract: every dataclass
has ``to_dict()`` (generic, ``dataclasses.asdict``-based, recursive) and a
per-class ``from_dict()`` that rebuilds nested dataclasses. Round-trip
``Cls.from_dict(obj.to_dict()) == obj`` holds for all classes here.

Time-model helpers (``slot_index``, ``is_working_day``, ``shift_eligible``)
implement the calendar law including OR-3 (Sunday-night 3rd shift).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, fields

import config

# ---------------------------------------------------------------------------
# Type aliases / derived constants
# ---------------------------------------------------------------------------

TaskKey = str  # f"{aircraft:04d}-T{n:05d}"

SHIFTS: tuple[int, int, int] = (1, 2, 3)

# Total effective work minutes available in one full working day (all shifts).
# Derived from config §1 so an env override of SHIFT_EFFECTIVE flows through.
DAY_WORK_MINUTES: int = sum(config.SHIFT_EFFECTIVE.values())

TASK_STATES: tuple[str, ...] = ("not_started", "in_progress", "blocked", "done")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def slot_index(day: int, shift: int) -> int:
    """Return the total order index of slot (day, shift): day*3 + (shift-1).

    Enforces the contract's slot ordering so precedence comparisons across
    days/shifts are a single integer comparison.
    """
    if shift not in SHIFTS:
        raise ValueError(f"shift must be in {SHIFTS}, got {shift!r}")
    return day * 3 + (shift - 1)


def is_working_day(day: int) -> bool:
    """Return True for Mon-Fri: day % 7 in 0..4 with day 0 = a Monday.

    Enforces the calendar law: shifts 1 and 2 are only plannable on
    working days.
    """
    return day % 7 in (0, 1, 2, 3, 4)


def shift_eligible(day: int, shift: int) -> bool:
    """Return True iff (day, shift) is a plannable slot.

    Enforces OR-3 (WEEK_STARTS_SUNDAY_NIGHT): shift 3 on a NON-working day
    ``d`` is plannable iff ``d+1`` is a working day — so Sunday-night S3
    (day % 7 == 6, Monday follows) is plannable while Friday-night... is a
    working day anyway, and Saturday-night S3 (Sunday follows) is NOT.
    Shifts 1 and 2 require ``day`` itself to be a working day. If the
    WEEK_STARTS_SUNDAY_NIGHT flag is disabled, shift 3 also requires a
    working day.
    """
    if shift not in SHIFTS:
        raise ValueError(f"shift must be in {SHIFTS}, got {shift!r}")
    if shift in (1, 2):
        return is_working_day(day)
    # shift 3
    if is_working_day(day):
        return True
    if config.WEEK_STARTS_SUNDAY_NIGHT:
        return is_working_day(day + 1)
    return False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _filtered_kwargs(cls, d: dict) -> dict:
    """Keep only keys that are fields of ``cls`` (forward-compatible loads)."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


class _Serde:
    """Mixin: generic asdict-based ``to_dict`` + overridable ``from_dict``."""

    def to_dict(self) -> dict:
        """Return a plain-dict (JSON-safe) recursive copy of this dataclass."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict):
        """Rebuild an instance from ``to_dict()`` output (extra keys ignored)."""
        return cls(**_filtered_kwargs(cls, d))


# ---------------------------------------------------------------------------
# Dataclasses (exact per contract)
# ---------------------------------------------------------------------------


@dataclass
class Task(_Serde):
    """A unit of work on one aircraft. Predecessors are same-aircraft only (C12)."""

    task_id: TaskKey
    aircraft: int
    name: str
    team: str
    skill: str
    duration_minutes: int
    mechanics_required: int
    earliest_day: int
    deadline_day: int
    predecessors: list[TaskKey]
    is_rework: bool = False
    is_inspection: bool = False
    state: str = "not_started"  # not_started|in_progress|blocked|done
    parts_eta_day: int | None = None
    remaining_minutes: int | None = None
    # Station-gate day (gate-pressure increment, docs/GATE_PRESSURE_DESIGN
    # .md): the day the PLAN OF RECORD expects this task done by — CS-gate
    # calendar on real data, baseline-slot + grace when stamped by the demo
    # runner. None (legacy fixtures) leaves the behind-schedule factor
    # inert. Distinct from deadline_day (the delivery-relative clock).
    gate_day: int | None = None
    # Rework defect origin (owner ruling 2026-07-11: rework drops with a
    # parent_soi; the parent SOI's team OWNS the rework). Set to the parent
    # task's team by every writer — the paperwork rule, not a sampled
    # attribution. None = malformed/legacy data; excusability then follows
    # config.REWORK_UNATTRIBUTED_EXCUSABLE (default: the receiving team
    # owns it until the paperwork says otherwise). Only meaningful when
    # is_rework is True.
    rework_origin_team: str | None = None
    # Cumulative minutes actually burned on this task across execution
    # sessions (owner ruling 3: overruns are excusable up to
    # OVERRUN_EXCUSE_FACTOR x the standard, owned beyond it). None = never
    # started / no actuals feed. The sim accumulates booked session
    # minutes; real data brings clocked labor.
    actual_minutes: int | None = None


@dataclass
class Mechanic(_Serde):
    """A named roster member. id f"{team}-S{shift}-M{i:03d}"; never lent
    across teams (OR-2)."""

    mech_id: str
    team: str
    shift: int
    skills: list[str]


@dataclass
class Aircraft(_Serde):
    """One aircraft position with its committed delivery deadline."""

    aircraft: int
    name: str
    delivery_deadline_day: int
    station: str


@dataclass
class Fleet(_Serde):
    """The full synthetic problem instance. ``meta`` MUST carry
    ``mock_data: True`` (OR-5 honesty labeling)."""

    aircraft: list[Aircraft]
    tasks: list[Task]
    mechanics: list[Mechanic]
    meta: dict = field(default_factory=dict)
    # meta: {"mock_data": True, "seed": int, "generated_at": str, "schema": 1}

    @classmethod
    def from_dict(cls, d: dict) -> "Fleet":
        d = _filtered_kwargs(cls, d)
        return cls(
            aircraft=[Aircraft.from_dict(a) for a in d.get("aircraft", [])],
            tasks=[Task.from_dict(t) for t in d.get("tasks", [])],
            mechanics=[Mechanic.from_dict(m) for m in d.get("mechanics", [])],
            meta=dict(d.get("meta", {})),
        )


@dataclass
class Assignment(_Serde):
    """A placed task: slot, minute window, and the FULL named crew.

    ``len(mechanic_ids) == mechanics_required`` always (OR-1: full crew or
    wait — a short crew is never represented, so it can never be booked).

    ``kept_incumbent`` (§12 COMMITMENT, OR-4): True iff the scheduler's
    commitment defense kept this task's incumbent (day, shift) from a prior
    schedule. Optional with default False and serialized; ``from_dict`` is
    tolerant of its absence (``_filtered_kwargs`` + the default), so old
    fixtures written before INCREMENT 5 still load.
    """

    task_id: TaskKey
    day: int
    shift: int
    start_minute: int
    end_minute: int
    mechanic_ids: list[str]
    team: str
    skill: str
    uses_overtime: bool
    kept_incumbent: bool = False


@dataclass
class Schedule(_Serde):
    """Engine output: placements plus reason-coded unplaced work.

    ``unscheduled`` maps task_id -> reason code — NO fictional placements
    ever (a task with no full crew in horizon is DELAYED, not short-crewed).
    ``stats`` must contain: total_tasks, scheduled, unscheduled,
    fleet_lateness_days, otd_count, aircraft:[...], makespan_day,
    wall_seconds, economics, capacity_pressure.
    """

    assignments: dict[TaskKey, Assignment]
    unscheduled: dict[TaskKey, str]  # task_id -> reason code
    stats: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        d = _filtered_kwargs(cls, d)
        return cls(
            assignments={
                k: Assignment.from_dict(v)
                for k, v in sorted(d.get("assignments", {}).items())
            },
            unscheduled=dict(sorted(d.get("unscheduled", {}).items())),
            stats=dict(d.get("stats", {})),
            meta=dict(d.get("meta", {})),
        )
