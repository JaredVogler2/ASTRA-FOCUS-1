"""ff.data.generator — deterministic synthetic fleet builder.

Produces the SYNTHETIC problem instance the whole app runs on. Honesty
labeling (OR-5): every Fleet built here carries ``meta["mock_data"] = True``
plus the seed and generator parameters — nothing produced by this module may
ever be presented as real production data.

Hard properties (each enforced by construction, not by post-hoc fixup):

- **Determinism.** A single ``random.Random(seed)`` drives every choice in a
  fixed call order over sorted/ordered structures; identical arguments yield
  an identical Fleet, byte-for-byte after ``loader.save_json_gz``. Even the
  ``generated_at`` stamp is derived from the seed (see
  ``_deterministic_stamp``) so full-object equality holds across runs.
- **Acyclic per-aircraft DAG (C12 / no cross-aircraft edges).** Tasks are
  created layer by layer; a task's predecessors are drawn ONLY from earlier
  layers of the SAME aircraft, so the graph is acyclic by construction and
  no edge can ever cross aircraft.
- **Solvability guarantee.** Every (team, skill) pair demanded by any task
  has at least one holder on at least one shift — and, stronger, at least
  one shift pool large enough to field the maximum crew demanded for that
  pair (crew sizes are clamped to the team's biggest shift pool at task
  creation). Scarcity stays real: many pools hold exactly the minimum, so
  contention produces genuine delays without permanent infeasibility.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from random import Random

import config
from ff.domain import DAY_WORK_MINUTES, Aircraft, Fleet, Mechanic, Task

# ---------------------------------------------------------------------------
# Generator constants (shape knobs; config §6 supplies the default sizes)
# ---------------------------------------------------------------------------

#: The 8 skill codes (contract: "skills from a pool of 8 codes").
SKILL_CODES: tuple[str, ...] = (
    "AVION",
    "CABIN",
    "ELECT",
    "FUELS",
    "HYDRO",
    "POWER",
    "QUAL",
    "STRUCT",
)

INSPECTION_SKILL = "QUAL"  # inspections are quality gates
INSPECTION_DURATION = 45  # contract: inspections are 45 minutes

# Durations must fit ONE shift with no segmentation (v1): the tightest slot
# is shift 3 (SHIFT_MAX[3] == 430), so 430 is the hard ceiling.
DURATION_MIN = 30
DURATION_MAX = 430

REWORK_RATE = 0.08  # ~8% rework tasks
INSPECTION_RATE = 0.10  # ~10% inspection tasks
TIGHT_DEADLINE_RATE = 0.30  # ~30% of aircraft get infeasibly tight deadlines

ZONE_NAMES: tuple[str, ...] = (
    "Fuselage",
    "Wing",
    "Empennage",
    "Cabin",
    "Systems",
    "Powerplant",
)
OP_NAMES: tuple[str, ...] = (
    "Install",
    "Rig",
    "Torque",
    "Seal",
    "Bond",
    "Route",
    "Test",
    "Adjust",
    "Fit",
    "Close-out",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deterministic_stamp(seed: int) -> str:
    """Return a deterministic ``generated_at`` label for the fleet meta.

    Enforces the determinism law (same seed ⇒ identical Fleet, including
    meta): the stamp is a pure function of the seed, never wall-clock time.
    A seed shaped like YYYYMMDD (the config default 20260710 is one) becomes
    that date; anything else gets a fixed epoch label. Live freshness for
    the UI comes from the snapshot service, not from this field.
    """
    s = str(seed)
    if len(s) == 8 and s.isdigit():
        year, month, day = int(s[:4]), int(s[4:6]), int(s[6:])
        try:
            date(year, month, day)
            return f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"
        except ValueError:
            pass
    return "2026-01-01T00:00:00Z"


def _allocate_seats(
    rng: Random, teams: list[str], n_mechanics: int
) -> dict[tuple[str, int], int]:
    """Distribute mechanic headcount across (team, shift) pools.

    Enforces: every team gets at least one mechanic (on shift 1) so no task
    team is unstaffable, while weighted largest-remainder allocation keeps
    some pools genuinely thin (thin teams, night shift lighter). Fully
    deterministic: weights are drawn in sorted team/shift order and ties in
    the remainder pass break by (team, shift) id.
    """
    if n_mechanics < len(teams):
        raise ValueError(
            f"n_mechanics={n_mechanics} must be >= n_teams={len(teams)} "
            "(every team needs at least one mechanic)"
        )
    team_weight = {team: 0.5 + rng.random() for team in teams}  # sorted order
    shift_weight = {1: 1.0, 2: 0.85, 3: 0.6}
    weights: dict[tuple[str, int], float] = {}
    for team in teams:
        for shift in (1, 2, 3):
            weights[(team, shift)] = (
                team_weight[team] * shift_weight[shift] * (0.75 + 0.5 * rng.random())
            )

    seats = {pair: 0 for pair in weights}
    for team in teams:  # staffing floor: one mechanic per team
        seats[(team, 1)] = 1
    remaining = n_mechanics - len(teams)
    total_weight = sum(weights.values())
    quotas = {pair: remaining * w / total_weight for pair, w in weights.items()}
    floors = {pair: int(math.floor(q)) for pair, q in quotas.items()}
    for pair, count in floors.items():
        seats[pair] += count
    leftover = remaining - sum(floors.values())
    by_fraction = sorted(
        quotas, key=lambda pair: (-(quotas[pair] - floors[pair]), pair[0], pair[1])
    )
    for pair in by_fraction[:leftover]:
        seats[pair] += 1
    return seats


def _critical_chain_calendar_days(tasks: list[Task]) -> int:
    """Lower-bound calendar days to finish an aircraft's DAG.

    Longest path in minutes (tasks arrive in topological creation order),
    converted to working days at full 3-shift capacity, then to calendar
    days (5 working days per 7). Used to place deadlines: a "tight"
    deadline set BELOW this bound is infeasible by construction, which is
    what makes lateness/economics non-trivial.
    """
    finish: dict[str, int] = {}
    for task in tasks:
        start = max((finish[p] for p in task.predecessors), default=0)
        finish[task.task_id] = start + task.duration_minutes
    chain_minutes = max(finish.values(), default=0)
    workdays = chain_minutes / DAY_WORK_MINUTES
    return max(1, math.ceil(workdays * 7 / 5) + 1)


def _build_aircraft_dag(
    rng: Random,
    ac_idx: int,
    ac_no: int,
    n_tasks: int,
    teams: list[str],
    team_max_pool: dict[str, int],
) -> list[Task]:
    """Build one aircraft's layered random DAG (deadlines backfilled later).

    Enforces, by construction:
    - acyclicity / no cross-aircraft edges (C12): predecessors are sampled
      only from EARLIER layers of THIS aircraft's task list;
    - 0-3 predecessors per task (inspection gating replaces, never exceeds);
    - ~8% rework tasks whose single predecessor is their parent task;
    - ~10% inspection tasks (45 min, skill QUAL) that gate 1-3 successors
      in the next layer (never marked in the final layer, so every
      inspection actually gates something);
    - durations 30-430 so every task fits one shift (no segmentation, v1);
    - crew 1-3, clamped to the team's largest shift pool so OR-1
      (full crew or wait) can always be satisfied eventually;
    - teams cluster by zone: contiguous bands of the task list map to one
      zone, each zone to one team.
    """
    n_zones = max(1, min(len(teams), len(ZONE_NAMES)))
    zone_skills = {
        z: sorted(rng.sample(SKILL_CODES, 3)) for z in range(n_zones)
    }
    all_earlier: list[Task] = []
    prev_layer: list[Task] = []
    pending_gates: list[Task] = []  # inspections from the previous layer
    inspection_ids: set[str] = set()
    layer_no = 0
    created = 0
    while created < n_tasks:
        width = min(rng.randint(4, 12), n_tasks - created)
        is_final_layer = created + width == n_tasks
        layer: list[Task] = []
        for _ in range(width):
            local_n = created + 1
            zone = min(n_zones - 1, (local_n - 1) * n_zones // n_tasks)
            zone_name = ZONE_NAMES[zone % len(ZONE_NAMES)]
            task_id = f"{ac_no:04d}-T{local_n:05d}"

            is_rework = layer_no > 0 and bool(all_earlier) and rng.random() < REWORK_RATE
            if is_rework:
                # Rework rule: the rework task's predecessor is its parent
                # (same team, same skill — the crew that owns the defect).
                parent = rng.choice(all_earlier)
                team = parent.team
                skill = parent.skill
                predecessors = [parent.task_id]
                duration = rng.randint(DURATION_MIN, 240)
                is_inspection = False
                name = f"Rework: {parent.name}"
            else:
                team = teams[(ac_idx * 3 + zone) % len(teams)]
                is_inspection = (not is_final_layer) and rng.random() < INSPECTION_RATE
                if is_inspection:
                    skill = INSPECTION_SKILL
                    duration = INSPECTION_DURATION
                    name = f"Inspect {zone_name} {local_n:03d}"
                else:
                    if rng.random() < 0.85:  # zone-clustered skill demand
                        skill = rng.choice(zone_skills[zone])
                    else:
                        skill = rng.choice(SKILL_CODES)
                    duration = rng.randint(DURATION_MIN, DURATION_MAX)
                    name = f"{rng.choice(OP_NAMES)} {zone_name} {local_n:03d}"
                if all_earlier:
                    n_preds = rng.choices((0, 1, 2, 3), weights=(12, 40, 33, 15))[0]
                else:
                    n_preds = 0
                chosen: set[str] = set()
                for _ in range(n_preds):
                    source = (
                        prev_layer
                        if prev_layer and rng.random() < 0.7
                        else all_earlier
                    )
                    chosen.add(rng.choice(source).task_id)
                predecessors = sorted(chosen)

            crew = rng.choice((1, 1, 1, 2, 2, 3))
            crew = max(1, min(crew, team_max_pool[team]))  # solvability clamp
            earliest = 0 if rng.random() < 0.9 else rng.randint(1, 6)
            task = Task(
                task_id=task_id,
                aircraft=ac_no,
                name=name,
                team=team,
                skill=skill,
                duration_minutes=duration,
                mechanics_required=crew,
                earliest_day=earliest,
                deadline_day=0,  # backfilled by generate_fleet
                predecessors=predecessors,
                is_rework=is_rework,
                is_inspection=is_inspection,
            )
            if is_inspection:
                inspection_ids.add(task_id)
            layer.append(task)
            created += 1

        # Gate each previous-layer inspection into 1-3 tasks of this layer.
        for inspection in pending_gates:
            targets = [
                t
                for t in layer
                if not t.is_rework and inspection.task_id not in t.predecessors
            ]
            open_targets = [t for t in targets if len(t.predecessors) < 3]
            if open_targets:
                gate_count = min(rng.randint(1, 3), len(open_targets))
                for target in rng.sample(open_targets, gate_count):
                    target.predecessors = sorted(
                        set(target.predecessors) | {inspection.task_id}
                    )
            elif targets:
                # Every candidate already has 3 preds: swap one out so the
                # inspection still gates >=1 successor without exceeding 3.
                target = targets[0]
                droppable = [
                    p for p in target.predecessors if p not in inspection_ids
                ] or list(target.predecessors)
                keep = set(target.predecessors) - {sorted(droppable)[-1]}
                target.predecessors = sorted(keep | {inspection.task_id})

        pending_gates = [t for t in layer if t.is_inspection]
        all_earlier.extend(layer)
        prev_layer = layer
        layer_no += 1
    return all_earlier


def _build_mechanics(
    rng: Random,
    seats: dict[tuple[str, int], int],
    demand_by_team: dict[str, list[str]],
) -> list[Mechanic]:
    """Create the named roster: 1-3 skills each, biased toward team demand.

    Iterates seats in sorted (team, shift) order — deterministic ids
    f"{team}-S{shift}-M{i:03d}". Skills lean 75% toward what the team's
    tasks actually demand, so pools are plausible before the solvability
    repair pass tops them up.
    """
    mechanics: list[Mechanic] = []
    for (team, shift) in sorted(seats):
        demanded = demand_by_team.get(team, [])
        for i in range(1, seats[(team, shift)] + 1):
            skill_count = rng.randint(1, 3)
            chosen: set[str] = set()
            for _ in range(skill_count):
                if demanded and rng.random() < 0.75:
                    chosen.add(rng.choice(demanded))
                else:
                    chosen.add(rng.choice(SKILL_CODES))
            mechanics.append(
                Mechanic(
                    mech_id=f"{team}-S{shift}-M{i:03d}",
                    team=team,
                    shift=shift,
                    skills=sorted(chosen),
                )
            )
    return mechanics


def _repair_solvability(
    mechanics: list[Mechanic], pair_max_crew: dict[tuple[str, str], int]
) -> None:
    """Top up skills so every demanded (team, skill) is actually workable.

    Enforces the solvability guarantee: (a) each demanded pair has >=1
    holder somewhere in the team; (b) at least one shift pool holds enough
    qualified mechanics for the LARGEST crew demanded on that pair (crew
    was clamped to the team's biggest pool, so a big-enough pool always
    exists). Adds skills to the least-skilled mechanics first (tie-break by
    mech_id) and never moves anyone across teams (OR-2). Pools are topped
    up only to the bare minimum — scarcity stays real.
    """
    by_team: dict[str, list[Mechanic]] = defaultdict(list)
    for mech in mechanics:
        by_team[mech.team].append(mech)

    for (team, skill) in sorted(pair_max_crew):
        max_crew = pair_max_crew[(team, skill)]
        team_mechs = by_team[team]
        # (a) at least one holder somewhere in the team
        if not any(skill in m.skills for m in team_mechs):
            candidate = min(team_mechs, key=lambda m: (len(m.skills), m.mech_id))
            candidate.skills = sorted(set(candidate.skills) | {skill})
        # (b) one shift pool can field the full crew (OR-1 satisfiable)
        pools = {s: [m for m in team_mechs if m.shift == s] for s in (1, 2, 3)}
        holder_counts = {
            s: sum(1 for m in pools[s] if skill in m.skills) for s in (1, 2, 3)
        }
        if max(holder_counts.values()) >= max_crew:
            continue
        eligible = [s for s in (1, 2, 3) if len(pools[s]) >= max_crew]
        if not eligible:  # cannot happen: crew clamped to max pool size
            raise RuntimeError(
                f"unrepairable pool for {(team, skill)}: crew {max_crew} "
                f"exceeds every shift pool"
            )
        shift_star = max(eligible, key=lambda s: (holder_counts[s], -s))
        need = max_crew - holder_counts[shift_star]
        non_holders = sorted(
            (m for m in pools[shift_star] if skill not in m.skills),
            key=lambda m: (len(m.skills), m.mech_id),
        )
        for mech in non_holders[:need]:
            mech.skills = sorted(set(mech.skills) | {skill})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_fleet(
    n_aircraft: int,
    tasks_per_aircraft: int | None,
    n_teams: int,
    n_mechanics: int,
    seed: int,
    task_universe: int | None = None,
) -> Fleet:
    """Generate a complete synthetic Fleet (aircraft, task DAGs, roster).

    Deterministic: a single ``random.Random(seed)`` in a fixed call order —
    identical arguments produce an identical Fleet (meta included). The
    result is labeled ``mock_data: True`` (OR-5); per-aircraft DAGs are
    acyclic with no cross-aircraft edges (C12); every demanded (team, skill)
    is workable by a full crew on some shift (solvability guarantee) while
    some pools stay genuinely thin. ~30% of aircraft get deadlines below
    their critical-chain lower bound (infeasibly tight) so lateness,
    OTD, and economics are non-trivial.
    """
    if n_aircraft < 1:
        raise ValueError("n_aircraft must be >= 1")
    if tasks_per_aircraft is not None and tasks_per_aircraft < 1:
        raise ValueError("tasks_per_aircraft must be >= 1 (or None for maturity mode)")
    if n_teams < 1:
        raise ValueError("n_teams must be >= 1")

    rng = Random(seed)
    teams = [f"T{t:02d}" for t in range(1, n_teams + 1)]

    # Roster seats first: task crew sizes are clamped against pool sizes.
    seats = _allocate_seats(rng, teams, n_mechanics)
    # Crew sizes are clamped against the ACTIVATION QUOTA capacity, not the
    # raw pool: the scheduler activates at most floor(pool * UTILIZATION)
    # distinct mechanics per (team, shift, day), so a 2-person pool can
    # never field a 2-crew (quota 1). Measured pre-fix: 2,082 tasks
    # no_crew_within_horizon at 55k scale purely from this interaction.
    team_max_pool = {
        team: max(
            1,
            max(
                int(seats[(team, s)] * config.UTILIZATION) for s in (1, 2, 3)
            ),
        )
        for team in teams
    }

    # -- pulsed-line maturity ladder (owner directive 2026-07-10) -----------
    # Aircraft enter the pulsed line at takt and sit at different maturity:
    # a ship about to deliver holds a punch list (as few as 0-1 open tasks);
    # one that just entered carries the near-full open universe (6,000+).
    # Mirrors the MAX fleet50 shape: ~40% late-to-delivery (punch lists),
    # ~40% post-FAL (residual work), ~20% in-factory (heavy, takt-staggered).
    # Uniform mode (tasks_per_aircraft given) is kept for tests/mini fixture.
    universe = task_universe if task_universe is not None else config.GEN_TASK_UNIVERSE
    maturity_class: list[str] = []
    counts: list[int] = []
    if tasks_per_aircraft is None:
        n_late = max(1, round(n_aircraft * 0.4))
        n_postfal = max(1, round(n_aircraft * 0.4))
        n_factory = max(1, n_aircraft - n_late - n_postfal)
        for ac_idx in range(n_aircraft):
            if ac_idx < n_late:  # oldest: late-to-delivery punch lists
                maturity_class.append("LATE_TO_DELIVERY")
                # First two ships carry near-empty punch lists (0-2 open
                # tasks) per the owner note "as few as 0, 1".
                # Ships 1 and 2 carry exactly 0 and 1 open tasks (owner
                # note 2026-07-10: "as few as 0, 1").
                counts.append(ac_idx if ac_idx < 2 else rng.randint(3, 120))
            elif ac_idx < n_late + n_postfal:  # post-FAL residual
                maturity_class.append("POST_FAL")
                counts.append(rng.randint(120, 900))
            else:  # in-factory: ladder up to the full open universe (6,000+)
                maturity_class.append("IN_FACTORY")
                k = ac_idx - n_late - n_postfal  # 0 = oldest in factory
                if n_factory == 1:
                    frac = 1.0
                else:
                    frac = 0.30 + 0.70 * k / (n_factory - 1)
                counts.append(int(round(universe * frac)))
    else:
        maturity_class = ["UNIFORM"] * n_aircraft
        counts = [tasks_per_aircraft] * n_aircraft

    aircraft_list: list[Aircraft] = []
    tasks: list[Task] = []
    all_ac_tasks: list[list[Task]] = []
    for ac_idx in range(n_aircraft):
        ac_no = ac_idx + 1
        if counts[ac_idx] == 0:
            all_ac_tasks.append([])
            continue
        ac_tasks = _build_aircraft_dag(
            rng, ac_idx, ac_no, counts[ac_idx], teams, team_max_pool
        )
        all_ac_tasks.append(ac_tasks)

    # Deadlines must respect FLEET capacity, not just each aircraft's own
    # chain: 50 aircraft compete for one shared mechanic supply, so the
    # roomy ramp is staggered across the capacity-feasible completion
    # horizon (with headroom), while ~30% stay infeasibly tight below
    # their own critical-chain bound (lateness/economics signal, OR-5
    # honesty: tight ones are late BY DESIGN).
    total_mech_min = sum(
        t.duration_minutes * t.mechanics_required
        for ac_tasks in all_ac_tasks
        for t in ac_tasks
    )
    eff_min_per_mech = (
        sum(config.SHIFT_EFFECTIVE.values()) / 3
    ) * config.UTILIZATION
    capacity_workdays = total_mech_min / max(1.0, n_mechanics * eff_min_per_mech)
    capacity_cal_base = capacity_workdays * 7 / 5  # aggregate lower bound
    # MEASURED fragmentation factor (calibration run 2026-07-10, fleet50
    # maturity mode, 55,427 tasks / 680 mechanics): real makespan 350 days
    # vs aggregate bound ~143 -> ~2.45x. Team/skill/quota fragmentation
    # means the aggregate bound is never achievable; deadline ramps use
    # this measured factor, not a guess.
    FRAG_FACTOR = 2.4
    capacity_cal_days = math.ceil(capacity_cal_base * FRAG_FACTOR)
    horizon_span = max(config.HORIZON_MIN_DAYS, capacity_cal_days)
    spacing = horizon_span / max(1, n_aircraft)

    # In-factory takt: heavy ships pulse out at a capacity-derived cadence.
    n_factory_total = sum(1 for c in maturity_class if c == "IN_FACTORY")
    n_postfal_total = sum(1 for c in maturity_class if c == "POST_FAL")
    factory_seen = 0
    postfal_seen = 0

    for ac_idx, ac_tasks in enumerate(all_ac_tasks):
        ac_no = ac_idx + 1
        chain_cal = _critical_chain_calendar_days(ac_tasks)
        mclass = maturity_class[ac_idx]
        if mclass == "LATE_TO_DELIVERY":
            # Already at/near delivery: near-term deadline; ships with real
            # residual work are late by construction (economics signal;
            # measured completion curve 0-15 days -> a handful make it).
            deadline = rng.randint(1, 5)
            station = "LATE-DELIVERY"
        elif mclass == "POST_FAL":
            # Punch-down phase: rank-staggered across the measured
            # punch-down band (calibration: completions 23-134). Ascending-
            # slack dispatch aligns service order to this ramp.
            postfal_seen += 1
            frac = 0.07 + 0.35 * postfal_seen / max(1, n_postfal_total)
            deadline = max(
                int(round(chain_cal * 1.2)),
                int(round(capacity_cal_base * FRAG_FACTOR * frac)),
            ) + rng.randint(0, 4)
            station = "POST-FAL"
        elif mclass == "IN_FACTORY":
            # Pulsed line: takt ladder across the measured heavy band
            # (calibration: completions 120-350); oldest pulses out first;
            # floored at the ship's own critical chain (never below physics).
            factory_seen += 1
            frac = 0.33 + 0.72 * factory_seen / max(1, n_factory_total)
            deadline = max(
                int(round(chain_cal * 1.2)),
                int(round(capacity_cal_base * FRAG_FACTOR * frac)),
            ) + rng.randint(0, 3)
            station = f"P{min(10, factory_seen):02d}"
        else:  # UNIFORM (legacy/tests): original tight/roomy mix.
            if rng.random() < TIGHT_DEADLINE_RATE:
                deadline = max(1, int(chain_cal * rng.uniform(0.35, 0.8)))
            else:
                deadline = max(
                    int(round(chain_cal * 1.2)),
                    int(round((ac_idx + 1) * spacing)),
                ) + 2 + rng.randint(0, 3)
            station = f"P{(ac_idx % 10) + 1:02d}"
        for task in ac_tasks:
            task.deadline_day = deadline
        aircraft_list.append(
            Aircraft(
                aircraft=ac_no,
                name=f"AC-{ac_no:04d}",
                delivery_deadline_day=deadline,
                station=station,
            )
        )
        tasks.extend(ac_tasks)

    # Demand map: which (team, skill) pairs exist, and the largest crew each
    # must field — drives demand-aware skills plus the solvability repair.
    pair_max_crew: dict[tuple[str, str], int] = {}
    for task in tasks:
        key = (task.team, task.skill)
        if task.mechanics_required > pair_max_crew.get(key, 0):
            pair_max_crew[key] = task.mechanics_required
    demand_by_team: dict[str, list[str]] = {}
    for (team, skill) in sorted(pair_max_crew):
        demand_by_team.setdefault(team, []).append(skill)

    mechanics = _build_mechanics(rng, seats, demand_by_team)
    _repair_solvability(mechanics, pair_max_crew)

    # Defensive verification of the solvability guarantee (must never fire).
    holders: set[tuple[str, str]] = set()
    for mech in mechanics:
        for skill in mech.skills:
            holders.add((mech.team, skill))
    missing = sorted(set(pair_max_crew) - holders)
    if missing:
        raise RuntimeError(f"solvability guarantee violated for {missing}")

    meta = {
        "mock_data": True,  # OR-5: synthetic data, always labeled
        "seed": seed,
        "generated_at": _deterministic_stamp(seed),
        "schema": 1,
        "maturity": {"classes": {c: maturity_class.count(c) for c in sorted(set(maturity_class))}, "open_task_counts": counts, "task_universe": universe if tasks_per_aircraft is None else None},
            "generator": {
            "n_aircraft": n_aircraft,
            "tasks_per_aircraft": tasks_per_aircraft,
            "n_teams": n_teams,
            "n_mechanics": n_mechanics,
            "skill_codes": list(SKILL_CODES),
            "duration_minutes": [DURATION_MIN, DURATION_MAX],
            "rework_rate": REWORK_RATE,
            "inspection_rate": INSPECTION_RATE,
            "tight_deadline_rate": TIGHT_DEADLINE_RATE,
        },
    }
    return Fleet(aircraft=aircraft_list, tasks=tasks, mechanics=mechanics, meta=meta)
