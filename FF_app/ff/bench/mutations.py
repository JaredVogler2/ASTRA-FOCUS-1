"""ff.bench.mutations — pure, deterministic fleet-mutation functions.

Benchmark scenarios (``benchmarks/<id>/manifest.json``) describe their
disruption as a list of overrides ``[{"fn": <name>, "params": {...}}]``;
``apply_overrides`` maps each ``fn`` through the ``MUTATIONS`` registry
below. Doctrine (B1 governing prompt + cadence_sim2 lineage): disruptions
are injected THROUGH the same structural patterns the generator uses
("the production way"), never via ad-hoc engine hooks.

Laws every mutation obeys (tripwire-tested in tests/test_bench.py):

- **Purity.** The input Fleet is NEVER mutated: each function deep-copies
  via ``Fleet.from_dict(fleet.to_dict())`` and returns the copy. Callers
  may therefore cache and reuse base fleets across scenarios.
- **Determinism.** All randomness comes from a single ``random.Random(seed)``
  drawn over SORTED collections; identical (fleet, params) produce an
  identical mutated fleet, byte-for-byte after ``loader.save_json_gz``.
- **DAG safety.** No mutation may introduce a cycle or a cross-aircraft
  edge (C12). ``rework_wave`` only ever adds LEAF tasks whose single
  predecessor is their same-aircraft parent — the generator's own rework
  rule — so acyclicity is preserved by construction.
- **Honesty (OR-5).** Every mutation appends a provenance record to
  ``fleet.meta["bench_overrides"]`` so a mutated fixture can never pass as
  a pristine one; the runner cross-checks this stamp against the manifest.
"""

from __future__ import annotations

from random import Random

from ff.domain import Fleet, Task

__all__ = [
    "MUTATIONS",
    "apply_overrides",
    "mechanic_absence",
    "skill_shortage",
    "late_parts",
    "rework_wave",
    "pulse_slip",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _copy(fleet: Fleet) -> Fleet:
    """Deep-copy a Fleet through its own serde (purity law)."""
    return Fleet.from_dict(fleet.to_dict())


def _stamp(fleet: Fleet, fn: str, params: dict) -> None:
    """Append a provenance record (honesty law — mutated fleets are labeled)."""
    fleet.meta.setdefault("bench_overrides", []).append(
        {"fn": fn, "params": dict(sorted(params.items()))}
    )


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def mechanic_absence(
    fleet: Fleet, team: str | None = None, fraction: float = 0.10, seed: int = 0
) -> Fleet:
    """Remove ``fraction`` (default 10%) of ONE team's mechanics.

    Models a call-out wave on a single crew before scheduling. ``team=None``
    picks the team with the largest roster (tie-break: smallest team id) so
    the default hits real capacity, not a one-person pool. At least one
    mechanic is always removed; the victims are a seeded sample over the
    team's roster sorted by mech_id (determinism law). The removal may
    legitimately make some (team, skill) demands unstaffable — the engine
    must then DELAY or reason-code that work (OR-1: full crew or wait),
    never short-crew it.
    """
    if not (0.0 < fraction < 1.0):
        raise ValueError(f"fraction must be in (0, 1), got {fraction!r}")
    out = _copy(fleet)
    rosters: dict[str, list] = {}
    for mech in out.mechanics:
        rosters.setdefault(mech.team, []).append(mech)
    if team is None:
        team = max(sorted(rosters), key=lambda t: len(rosters[t]))
    if team not in rosters:
        raise ValueError(f"unknown team {team!r} (no mechanics)")
    members = sorted(rosters[team], key=lambda m: m.mech_id)
    n_remove = max(1, int(len(members) * fraction))
    rng = Random(seed)
    removed = sorted(m.mech_id for m in rng.sample(members, n_remove))
    removed_set = set(removed)
    out.mechanics = [m for m in out.mechanics if m.mech_id not in removed_set]
    _stamp(
        out,
        "mechanic_absence",
        {"team": team, "fraction": fraction, "seed": seed, "removed": removed},
    )
    return out


def skill_shortage(fleet: Fleet, skill: str = "AVION", seed: int = 0) -> Fleet:
    """Strip ``skill`` from HALF of its holders, fleet-wide.

    Models a certification lapse / license expiry wave on one skill code.
    Holders are collected sorted by mech_id; ``len(holders) // 2`` of them
    lose the skill (seeded sample — determinism law). Mechanics stay on the
    roster (they still hold their other skills, possibly none); pools that
    drop below a task's crew size force the engine to delay or reason-code
    (``no_skill_holder`` / ``crew_exceeds_pool``) — never to borrow across
    teams (OR-2).
    """
    out = _copy(fleet)
    holders = sorted(
        (m for m in out.mechanics if skill in m.skills), key=lambda m: m.mech_id
    )
    if not holders:
        raise ValueError(f"skill {skill!r} has no holders in this fleet")
    n_strip = len(holders) // 2
    rng = Random(seed)
    stripped = sorted(m.mech_id for m in rng.sample(holders, n_strip))
    stripped_set = set(stripped)
    for mech in out.mechanics:
        if mech.mech_id in stripped_set:
            mech.skills = sorted(s for s in mech.skills if s != skill)
    _stamp(
        out,
        "skill_shortage",
        {
            "skill": skill,
            "seed": seed,
            "holders_before": len(holders),
            "stripped": stripped,
        },
    )
    return out


def late_parts(
    fleet: Fleet, fraction: float = 0.05, eta_day: int = 10, seed: int = 0
) -> Fleet:
    """Mark ``fraction`` (default 5%) of not-started tasks parts-blocked.

    Models a supplier slip: each victim task gets ``state="blocked"`` and
    ``parts_eta_day=eta_day`` (default day 10 = start_day 0 + 10 — the
    "+10" of the scenario spec). The scheduler floors those tasks at the
    ETA day (behavior 2) and the validator's V9 check enforces the floor on
    the output. Victims are a seeded sample over not-started tasks sorted
    by task_id (determinism law).
    """
    if not (0.0 < fraction < 1.0):
        raise ValueError(f"fraction must be in (0, 1), got {fraction!r}")
    if eta_day < 0:
        raise ValueError(f"eta_day must be >= 0, got {eta_day!r}")
    out = _copy(fleet)
    eligible = sorted(
        (t for t in out.tasks if t.state == "not_started"), key=lambda t: t.task_id
    )
    if not eligible:
        raise ValueError("no not_started tasks to block")
    n_block = max(1, int(len(eligible) * fraction))
    rng = Random(seed)
    victims = sorted(t.task_id for t in rng.sample(eligible, n_block))
    victim_set = set(victims)
    for task in out.tasks:
        if task.task_id in victim_set:
            task.state = "blocked"
            task.parts_eta_day = eta_day
    _stamp(
        out,
        "late_parts",
        {
            "fraction": fraction,
            "eta_day": eta_day,
            "seed": seed,
            "blocked_count": n_block,
        },
    )
    return out


def rework_wave(fleet: Fleet, count: int = 200, seed: int = 0) -> Fleet:
    """Inject ``count`` rework tasks via the generator's own rework pattern.

    "The production way" (cadence_sim2 doctrine: rework rows gate their
    parent through the real DAG, no proxies): each injected task's SINGLE
    predecessor is its parent task, and it inherits the parent's team,
    skill, crew size, earliest_day and deadline_day — the crew that owns
    the defect fixes it. Because the parent's crew size was already clamped
    to a feasible pool by the generator, solvability is preserved; because
    every injected task is a LEAF on the same aircraft as its parent, the
    DAG stays acyclic with no cross-aircraft edges (C12) by construction.

    Parents are a seeded sample over non-done tasks sorted by task_id;
    durations are 30-240 minutes (the generator's rework band). New ids
    continue each aircraft's ``-T{n:05d}`` sequence past its current max.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count!r}")
    out = _copy(fleet)
    eligible = sorted(
        (t for t in out.tasks if t.state != "done"), key=lambda t: t.task_id
    )
    if not eligible:
        raise ValueError("no live tasks to attach rework to")
    n_inject = min(count, len(eligible))
    rng = Random(seed)
    parents = sorted(rng.sample(eligible, n_inject), key=lambda t: t.task_id)

    # Next free per-aircraft task number (ids are f"{ac:04d}-T{n:05d}").
    next_n: dict[int, int] = {}
    for task in out.tasks:
        n = int(task.task_id.rsplit("-T", 1)[1])
        if n >= next_n.get(task.aircraft, 0):
            next_n[task.aircraft] = n + 1

    injected: list[str] = []
    for parent in parents:
        n = next_n[parent.aircraft]
        next_n[parent.aircraft] = n + 1
        task_id = f"{parent.aircraft:04d}-T{n:05d}"
        out.tasks.append(
            Task(
                task_id=task_id,
                aircraft=parent.aircraft,
                name=f"Rework: {parent.name}",
                team=parent.team,
                skill=parent.skill,
                duration_minutes=rng.randint(30, 240),
                mechanics_required=parent.mechanics_required,
                earliest_day=parent.earliest_day,
                deadline_day=parent.deadline_day,
                predecessors=[parent.task_id],
                is_rework=True,
                is_inspection=False,
            )
        )
        injected.append(task_id)
    _stamp(
        out,
        "rework_wave",
        {"count": count, "seed": seed, "injected_count": len(injected)},
    )
    return out


def pulse_slip(fleet: Fleet, days: int = 7) -> Fleet:
    """Pull every IN_FACTORY aircraft's delivery deadline in by ``days``.

    Models a pulse-date acceleration on the factory line: aircraft whose
    station is a pulse position (``P01``..``P10`` — the generator's
    IN_FACTORY/UNIFORM station grammar; LATE-DELIVERY and POST-FAL stations
    are untouched) get ``delivery_deadline_day`` (and every task's
    ``deadline_day``, which drives CPM slack) reduced by ``days``, floored
    at day 1. No randomness — this mutation is a pure calendar edit.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days!r}")
    out = _copy(fleet)
    slipped: list[int] = []
    for ac in out.aircraft:
        station = ac.station
        if station.startswith("P") and station[1:].isdigit():
            ac.delivery_deadline_day = max(1, ac.delivery_deadline_day - days)
            slipped.append(ac.aircraft)
    slipped_set = set(slipped)
    if not slipped_set:
        raise ValueError("no IN_FACTORY (P-station) aircraft to slip")
    for task in out.tasks:
        if task.aircraft in slipped_set:
            task.deadline_day = max(1, task.deadline_day - days)
    _stamp(out, "pulse_slip", {"days": days, "aircraft": sorted(slipped)})
    return out


# ---------------------------------------------------------------------------
# registry + dispatcher
# ---------------------------------------------------------------------------

MUTATIONS = {
    "mechanic_absence": mechanic_absence,
    "skill_shortage": skill_shortage,
    "late_parts": late_parts,
    "rework_wave": rework_wave,
    "pulse_slip": pulse_slip,
}


def apply_overrides(fleet: Fleet, overrides: list[dict]) -> Fleet:
    """Apply a manifest's override list in order; returns the mutated fleet.

    Each entry is ``{"fn": <MUTATIONS key>, "params": {...}}``. Unknown
    function names raise (a typo in a manifest must never silently no-op —
    the B1 gotcha "variant passes without doing anything"). With an empty
    list the input fleet is returned unchanged (and unstamped).
    """
    out = fleet
    for override in overrides:
        fn_name = override.get("fn")
        if fn_name not in MUTATIONS:
            raise ValueError(
                f"unknown mutation {fn_name!r}; known: {sorted(MUTATIONS)}"
            )
        out = MUTATIONS[fn_name](out, **override.get("params", {}))
    return out
