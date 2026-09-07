"""Shared pytest fixtures + hand-built-fleet factories for FF_app tests.

sys.path bootstrap: inserting the FF_app root (this file's parent's parent)
makes ``import config`` / ``import ff`` / ``from tests.conftest import ...``
work when the suite is run as ``python -m pytest -q`` from
/home/user/FABLE_FOCUS_REVIEW/FF_app (or from anywhere else).

The ``fleet`` fixture builds the mini fleet DIRECTLY via
``generate_fleet(3, 40, 4, 30, seed)`` — it deliberately does NOT require
the committed ``data/mini/fleet.json.gz`` fixture, so the suite is
self-sufficient on a fresh checkout.

IMPORTANT: the session-scoped ``fleet`` / ``cpm`` / ``schedule`` / ``snap``
fixtures are shared read-only state. Tests that need to mutate a fleet or
schedule must work on a copy (``Fleet.from_dict(fleet.to_dict())``) or on a
hand-built fleet from the ``mk_*`` factories below.
"""

from __future__ import annotations

import os
import sys

# --- sys.path bootstrap (must precede any ff/config import) ----------------
FF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FF_ROOT not in sys.path:
    sys.path.insert(0, FF_ROOT)

# Web-layer test prerequisites (config §7): the dev secret-key fallback is
# only legal with FF_ENV=dev; setting a secret key directly also satisfies
# stricter implementations. setdefault keeps any caller-provided env intact.
os.environ.setdefault("FF_ENV", "dev")
os.environ.setdefault("FF_SECRET_KEY", "ff-test-secret")

import pytest  # noqa: E402

from ff.data.generator import generate_fleet  # noqa: E402
from ff.domain import Aircraft, Fleet, Mechanic, Task  # noqa: E402
from ff.engine.cpm import compute_cpm  # noqa: E402
from ff.engine.scheduler import build_schedule  # noqa: E402

# Mini-fleet parameters (contract Tests section): 3 aircraft x 40 tasks,
# 4 teams, 30 mechanics, fixed seed => fully deterministic suite.
MINI_SEED = 20260710
MINI_ARGS = (3, 40, 4, 30, MINI_SEED)


# ---------------------------------------------------------------------------
# session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fleet() -> Fleet:
    """Mini fleet built via generate_fleet(3, 40, 4, 30, seed) — READ-ONLY."""
    return generate_fleet(*MINI_ARGS)


@pytest.fixture(scope="session")
def cpm(fleet) -> dict:
    """CPM info for the mini fleet (contract: compute_cpm(tasks))."""
    return compute_cpm(fleet.tasks)


@pytest.fixture(scope="session")
def schedule(fleet, cpm):
    """Deterministic greedy schedule of the mini fleet (default args)."""
    return build_schedule(fleet, cpm)


@pytest.fixture(scope="session")
def snap(fleet, schedule, cpm) -> dict:
    """Snapshot read-model over the mini fleet (services layer input)."""
    from ff.services.snapshot import build_snapshot

    return build_snapshot(fleet, schedule, cpm)


# ---------------------------------------------------------------------------
# hand-built-fleet factories (importable: from tests.conftest import mk_task)
# ---------------------------------------------------------------------------


def mk_task(
    task_id: str,
    *,
    aircraft: int = 1,
    team: str = "T01",
    skill: str = "S",
    dur: int = 60,
    crew: int = 1,
    earliest: int = 0,
    deadline: int = 30,
    preds: tuple | list = (),
    state: str = "not_started",
    parts_eta: int | None = None,
    remaining: int | None = None,
    rework: bool = False,
    inspection: bool = False,
    name: str | None = None,
) -> Task:
    """Terse Task factory for hand-built tripwire fleets."""
    return Task(
        task_id=task_id,
        aircraft=aircraft,
        name=name or f"Task {task_id}",
        team=team,
        skill=skill,
        duration_minutes=dur,
        mechanics_required=crew,
        earliest_day=earliest,
        deadline_day=deadline,
        predecessors=list(preds),
        is_rework=rework,
        is_inspection=inspection,
        state=state,
        parts_eta_day=parts_eta,
        remaining_minutes=remaining,
    )


def mk_mech(mech_id: str, *, team: str = "T01", shift: int = 1, skills=("S",)) -> Mechanic:
    """Terse Mechanic factory (id convention f\"{team}-S{shift}-M{i:03d}\")."""
    return Mechanic(mech_id=mech_id, team=team, shift=shift, skills=list(skills))


def mk_aircraft(no: int, *, deadline: int = 30, station: str | None = None) -> Aircraft:
    """Terse Aircraft factory."""
    return Aircraft(
        aircraft=no,
        name=f"AC-{no:04d}",
        delivery_deadline_day=deadline,
        station=station or f"P{no:02d}",
    )


def mk_fleet(tasks, mechanics, aircraft=None, seed: int = 1) -> Fleet:
    """Assemble a hand-built Fleet, honoring OR-5 (meta.mock_data=True).

    When ``aircraft`` is omitted, one Aircraft per distinct task.aircraft is
    derived with delivery deadline = max deadline_day of its tasks.
    """
    if aircraft is None:
        deadlines: dict[int, int] = {}
        for t in tasks:
            deadlines[t.aircraft] = max(deadlines.get(t.aircraft, 0), t.deadline_day)
        aircraft = [mk_aircraft(n, deadline=d) for n, d in sorted(deadlines.items())]
    return Fleet(
        aircraft=list(aircraft),
        tasks=list(tasks),
        mechanics=list(mechanics),
        meta={
            "mock_data": True,  # OR-5: hand-built test data is synthetic too
            "seed": seed,
            "generated_at": "2026-01-01T00:00:00Z",
            "schema": 1,
        },
    )


def run_schedule(fleet: Fleet, **kwargs):
    """cpm + build_schedule in one call; returns (cpm, schedule)."""
    cpm_info = compute_cpm(fleet.tasks)
    return cpm_info, build_schedule(fleet, cpm_info, **kwargs)
