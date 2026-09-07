"""Digital-week simulator — rolling clock + TRUE rework injection + replan.

``run_digital_week(fleet, rounds=9, seed=7, ...)`` follows the cadence_sim2
doctrine (`MAX/tools/cadence_sim2.py`, FOCU5 D5) adapted to FF_app. Per
round:

  (a) EXECUTE the shift that just ended: every task planned in the current
      (day, shift) completes with probability ``exec_rate``
      (``state="done"``); the rest slide to ``in_progress`` with
      ``remaining_minutes = max(5, int(duration_minutes * slide_remain))``.
  (b) INJECT rework THE PRODUCTION WAY: mint new ``Task`` rows (id suffix
      ``RWK``) whose single predecessor is a sampled not-started parent on
      an active aircraft — same team/skill, duration from
      ``REWORK_DURATIONS`` — appended to the fleet's task list, so the DAG
      grows exactly like rework orders appearing in the next MES extract.
      No proxies: the next replan's CPM + scheduler see real rows.
  (c) ADVANCE the factory clock (see ``advance_clock`` below).
  (d) REPLAN with the real engine: ``compute_cpm`` + ``build_schedule`` on
      the mutated fleet, anchored at the new clock day. Ground truth is
      always a full engine replan — never feed-only bookkeeping. §12
      COMMITMENT (OR-4, INCREMENT 5): when ``thread_incumbent`` (default
      True) each replan threads ``incumbent_from_schedule(prior round's
      schedule)`` so surviving in-horizon work defends its incumbent slot
      and crew ("committed work dispatches before new work");
      ``thread_incumbent=False`` is the pre-commitment A/B arm.
  (e) MEASURE: executed / slid / injected counts, slot & mechanic stability
      over surviving unexecuted tasks (plus the in-horizon slice — see
      ``_stability``), fleet lateness, OTD, controllable-$, and replan
      wall seconds.

Clock adaptation (documented, honest): the doctrine's factory chronology is
``SHIFT_CYCLE = [3, 1, 2]`` — a work day's 3rd shift runs OVERNIGHT before
its 1st. FF_app's calendar (``ff.domain.shift_eligible``) labels the
overnight shift of day ``d`` as slot ``(d, 3)`` — the night d -> d+1 — so
walking eligible slots in ``slot_index`` order reproduces exactly that
chronology: ... (d,1) (d,2) (d,3=night) (d+1,1) ... with the weekend seam
per OR-3 ("the week's 3rd shift begins Sunday night",
``WEEK_STARTS_SUNDAY_NIGHT``): Friday-night S3 (day%7==4) is eligible
(Friday is a working day), Saturday-night S3 (Sunday follows) is NOT, and
Sunday-night S3 (Monday follows) IS — so Friday S3 advances straight to
Sunday S3, then Monday S1.

Determinism: one ``random.Random(seed)`` drives the execution coins and the
rework sampling in a fixed draw order over sorted structures; the engine is
deterministic by contract. Two runs with identical (fleet, args) produce
identical results except the measured ``wall_s`` fields.

Honesty (OR-5): the result carries ``mock_data`` from the fleet meta; all
figures are synthetic; no optimality claims.
"""

from __future__ import annotations

import time
from random import Random

import config
from ff.domain import Fleet, Schedule, Task, shift_eligible, slot_index
from ff.engine.cpm import compute_cpm, is_critical
from ff.engine.scheduler import build_schedule, incumbent_from_schedule

# Doctrine constants (cadence_sim2 verbatim; FOCU5 D5 "verify, then build on").
SHIFT_CYCLE = [3, 1, 2]  # factory chronology within a work day
EXEC_RATE = 0.90
SLIDE_REMAIN = 0.50
REWORK_PER_ROUND = 40
REWORK_DURATIONS = (60, 90, 120, 180, 240)
REWORK_CREW_CHOICES = (1, 1, 2)  # mirrors cadence_sim2's rng.choice([1, 1, 2])
REWORK_WINDOW_DAYS = 6  # parents preferred from the next ~week of the plan
REWORK_ID_SUFFIX = "RWK"
# Owner ruling (2026-07-11): rework drops with a parent_soi and the parent
# SOI's team OWNS it — origin is ALWAYS the parent task's team (paperwork
# rule, never sampled). What varies is WHO EXECUTES the fix: ~40% of
# injected rework is a cross-trade fix assigned to a DIFFERENT team
# (skill "ANY"), for whom the work is excusable (the parent's team owns
# the causation); the rest lands on the parent's own team (owned, not
# excusable). Synthetic placeholder rate.
REWORK_CROSS_TRADE_FIX_RATE = 0.40


def advance_clock(day: int, shift: int) -> tuple[int, int, bool]:
    """Next (day, shift, day_rolled) in factory chronology.

    Walks ``slot_index`` order over ``ff.domain.shift_eligible`` slots —
    the FF_app rendering of ``SHIFT_CYCLE = [3, 1, 2]`` with the weekend
    seam (OR-3, Sunday-night rule): slot ``(d, 3)`` is day ``d``'s
    overnight shift, so Friday S3 -> Sunday S3 (Saturday night skipped:
    Sunday is not a working day) -> Monday S1. Raises ``ValueError`` on a
    bad shift number. Always terminates (every week has eligible slots).
    """
    slot = slot_index(day, shift) + 1  # slot_index validates the shift
    while True:
        d, s0 = divmod(slot, 3)
        s = s0 + 1
        if shift_eligible(d, s):
            return d, s, d != day
        slot += 1


def _offplan_pools(
    fleet: Fleet,
    by_id: dict[str, Task],
    cpm: dict,
    planned_set: set[str],
) -> dict[str, list[Task]]:
    """Per-team pools of READY, NON-CRITICAL, unplanned-this-slot tasks.

    The deviation menu ("we can't assume teams always follow the plan"):
    a crew that walks off the plan completes one of THESE instead — a task
    whose predecessors are all done (no DAG jump), on its own team, that
    is NOT critical (cpm ``is_critical`` false; deviating crews grab easy
    work, not keystones). Sorted construction — deterministic.
    """
    pools: dict[str, list[Task]] = {}
    for t in sorted(fleet.tasks, key=lambda t: t.task_id):
        if t.state != "not_started" or t.task_id in planned_set:
            continue
        info = cpm.get(t.task_id)
        if info is not None and is_critical(info):
            continue
        if any(
            p != t.task_id and p in by_id and by_id[p].state != "done"
            for p in t.predecessors
        ):
            continue
        pools.setdefault(t.team, []).append(t)
    return pools


def _execute_slot(
    by_id: dict[str, Task],
    schedule: Schedule,
    day: int,
    shift: int,
    rng: Random,
    exec_rate: float,
    slide_remain: float,
    deviate_rate: float = 0.0,
    fleet: Fleet | None = None,
    cpm: dict | None = None,
    strict: bool = False,
    offplan_value_fn=None,
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    """Execute the (day, shift) slice of ``schedule`` — doctrine step (a).

    Every planned task flips: done with probability ``exec_rate``, else
    in_progress with ``remaining_minutes = max(5, int(duration *
    slide_remain))`` (the cadence_sim2 slide rule — never leave
    remaining_minutes unset, or the replan re-books the FULL duration).

    DEVIATION (5-day demo realism): when ``deviate_rate > 0`` each planned
    task first draws a deviation coin — on success the crew SKIPS it (it
    stays ``not_started`` and must be replanned) and instead completes one
    ready, non-critical, same-team task that was NOT planned in this slot
    (see ``_offplan_pools``); the pool shrinks as picks complete so no
    task completes twice. Off-plan completions earn NOTHING toward the
    slot's goal (they were never planned into it — GG-2/GG-3 handle the
    scoreboard consequences downstream). With ``deviate_rate == 0`` (the
    default) NO deviation coin is drawn, so the rng stream — and therefore
    every previously measured digital-week result — is byte-identical to
    the pre-deviation implementation.

    STRICT execution physics (``strict=True`` — the 5-day demo mode):
    planned tasks execute in the shift's REAL chronology (ascending
    start_minute, task_id tie-break) with live dependency state — a
    planned task whose predecessor is not done AT ITS START MOMENT cannot
    be worked at all (no coins drawn): it stays ``not_started`` and is
    reported in the sixth returned list (``blocked_ids``). This restores
    the engine's documented input assumption ("a task is in_progress only
    when its predecessors are done or themselves in_progress") that
    independent per-task coins would otherwise break the moment a
    predecessor is skipped by a deviating crew. Legacy mode
    (``strict=False``) keeps the original sorted-task_id iteration and
    draw order — byte-identical streams for all previously measured runs.

    Deterministic either way: fixed visit order, fixed draw order
    (deviation coin, then execution coin), pool picks via
    ``rng.randrange``. Returns (planned_ids, done_ids, slid_ids,
    skipped_ids, offplan_done_ids, blocked_ids).
    """
    planned = [
        tid
        for tid in sorted(schedule.assignments)
        if schedule.assignments[tid].day == day
        and schedule.assignments[tid].shift == shift
    ]
    visit = planned
    if strict:
        visit = sorted(
            planned,
            key=lambda tid: (schedule.assignments[tid].start_minute, tid),
        )
    pools: dict[str, list[Task]] = {}
    if deviate_rate > 0 and fleet is not None and cpm is not None:
        pools = _offplan_pools(fleet, by_id, cpm, set(planned))
    done_ids: list[str] = []
    slid_ids: list[str] = []
    skipped_ids: list[str] = []
    offplan_ids: list[str] = []
    blocked_ids: list[str] = []
    for tid in visit:
        task = by_id[tid]
        if strict and any(
            p != tid and p in by_id and by_id[p].state != "done"
            for p in task.predecessors
        ):
            # Physics, not a coin: the crew cannot start on top of an
            # unfinished predecessor. GG-3 downstream: same-team causes
            # stay in the goal denominator, cross-team causes are
            # auto-excused by the disruption service.
            blocked_ids.append(tid)
            continue
        if deviate_rate > 0 and rng.random() < deviate_rate:
            # Crew walked off the plan: the planned task is untouched
            # (state stays not_started) and a ready non-critical same-team
            # task completes in its place, if one exists.
            skipped_ids.append(tid)
            pool = pools.get(task.team)
            if pool:
                if offplan_value_fn is not None:
                    # POINTS-GREEDY deviation (the GAMES thesis made
                    # operational: crews chase value) — the deviating crew
                    # grabs the highest-scoring ready task, so whatever the
                    # scoring boosts is what off-plan energy burns down.
                    # Deterministic tie-break by task_id.
                    pick = max(
                        pool,
                        key=lambda t: (offplan_value_fn(t.task_id), t.task_id),
                    )
                    pool.remove(pick)
                else:
                    pick = pool.pop(rng.randrange(len(pool)))
                pick.state = "done"
                pick.remaining_minutes = None
                offplan_ids.append(pick.task_id)
            continue
        # Actual-burn accounting (owner ruling 3: overrun tolerance is
        # measured against ACTUAL minutes): this session books the task's
        # remaining minutes (or the full standard on first touch); both
        # outcomes consume the session — a slide burns the time AND leaves
        # remaining work.
        session = (
            task.remaining_minutes
            if task.remaining_minutes is not None
            else task.duration_minutes
        )
        if rng.random() < exec_rate:
            task.state = "done"
            task.actual_minutes = (task.actual_minutes or 0) + session
            task.remaining_minutes = None
            done_ids.append(tid)
        else:
            task.state = "in_progress"
            task.actual_minutes = (task.actual_minutes or 0) + session
            task.remaining_minutes = max(
                5, int(task.duration_minutes * slide_remain)
            )
            slid_ids.append(tid)
    return planned, done_ids, slid_ids, skipped_ids, offplan_ids, blocked_ids


def _jump_out_of_sequence(
    fleet: Fleet,
    by_id: dict[str, Task],
    schedule: Schedule,
    day: int,
    rng: Random,
    n: int,
) -> list[str]:
    """Complete ``n`` tasks OUT OF SEQUENCE — the discipline-failure lever.

    Models a crew finishing a job whose last gate hadn't cleared: eligible
    tasks are not_started, non-rework, planned within the next
    ``REWORK_WINDOW_DAYS`` days, with at least one unfinished predecessor.
    Preferred tier: every predecessor at least STARTED (done or
    in_progress — the jump is over an almost-cleared gate, not a cold
    subtree); when that tier can't fill ``n`` (tiny fleets, first rounds)
    the pool falls back to any >=1-unfinished-pred candidate. The points
    engine penalizes exactly this (``P_OOS``, "out-of-sequence work is
    PENALIZED, never rewarded"), which is the behavior the 5-day demo
    proves. Deterministic: sorted candidates, seeded ``rng.sample``.
    """
    if n <= 0:
        return []
    planned_day = {tid: asg.day for tid, asg in schedule.assignments.items()}
    near_gate: list[Task] = []  # every pred at least started (preferred)
    any_jump: list[Task] = []  # >=1 unfinished pred (fallback tier)
    for t in sorted(fleet.tasks, key=lambda t: t.task_id):
        if t.state != "not_started" or t.task_id.endswith(REWORK_ID_SUFFIX):
            continue
        if not (day <= planned_day.get(t.task_id, -1) <= day + REWORK_WINDOW_DAYS):
            continue
        preds = [
            by_id[p] for p in t.predecessors if p != t.task_id and p in by_id
        ]
        unfinished = [p for p in preds if p.state != "done"]
        if not unfinished:
            continue
        any_jump.append(t)
        if all(p.state in ("done", "in_progress") for p in preds):
            near_gate.append(t)
    cands = near_gate if len(near_gate) >= n else any_jump
    picks = rng.sample(cands, min(n, len(cands)))
    out: list[str] = []
    for t in picks:
        t.state = "done"
        t.remaining_minutes = None
        out.append(t.task_id)
    return out


def _inject_rework(
    fleet: Fleet,
    by_id: dict[str, Task],
    schedule: Schedule,
    day: int,
    rng: Random,
    n: int,
    seq: int,
) -> tuple[list[Task], int]:
    """Mint rework Task rows onto the fleet — doctrine step (b), no proxies.

    Parents are sampled (seeded rng, sorted candidate list) from
    NOT-STARTED tasks on active aircraft, preferring parents planned in
    the next ``REWORK_WINDOW_DAYS`` days (cadence_sim2's near-window
    priority); existing rework rows never parent more rework. Each new row:
    id suffix ``RWK``, single predecessor = the parent (FF_app rework
    convention: rework follows its parent), same team/skill/aircraft,
    duration from ``REWORK_DURATIONS``, crew from ``REWORK_CREW_CHOICES``
    clamped to the parent's crew (the parent is staffable, so the rework
    stays staffable — OR-1 full-crew law can always be met eventually),
    ``earliest_day`` = the current clock day, deadline = the parent's.
    The rows are APPENDED to ``fleet.tasks`` so the DAG grows like a real
    MES extract. Returns (new_tasks, next_seq).
    """
    active = {t.aircraft for t in fleet.tasks if t.state != "done"}
    planned_day = {tid: asg.day for tid, asg in schedule.assignments.items()}
    candidates = [
        t
        for t in sorted(fleet.tasks, key=lambda t: t.task_id)
        if t.state == "not_started"
        and t.aircraft in active
        and not t.task_id.endswith(REWORK_ID_SUFFIX)
    ]
    near = [
        t
        for t in candidates
        if day + 1 <= planned_day.get(t.task_id, -1) <= day + REWORK_WINDOW_DAYS
    ]
    pool = near if len(near) >= n else candidates
    parents = rng.sample(pool, min(n, len(pool)))

    all_teams = sorted({t.team for t in fleet.tasks})
    new_tasks: list[Task] = []
    for parent in parents:  # sample order — deterministic given the seed
        seq += 1
        duration = rng.choice(REWORK_DURATIONS)
        crew = min(rng.choice(REWORK_CREW_CHOICES), parent.mechanics_required)
        # Owner ruling: origin = the parent SOI's team, ALWAYS. The fix is
        # usually executed by that same team (owned); sometimes by another
        # trade (cross-trade fix, skill "ANY" — excusable for the fixer).
        origin = parent.team
        if len(all_teams) > 1 and rng.random() < REWORK_CROSS_TRADE_FIX_RATE:
            others = [t for t in all_teams if t != parent.team]
            fix_team, fix_skill = rng.choice(others), "ANY"
        else:
            fix_team, fix_skill = parent.team, parent.skill
        task = Task(
            task_id=f"{parent.aircraft:04d}-T{90000 + seq:05d}{REWORK_ID_SUFFIX}",
            aircraft=parent.aircraft,
            name=f"Rework of {parent.task_id}",
            team=fix_team,
            skill=fix_skill,
            duration_minutes=duration,
            mechanics_required=crew,
            earliest_day=day,
            deadline_day=parent.deadline_day,
            predecessors=[parent.task_id],
            is_rework=True,
            rework_origin_team=origin,
        )
        new_tasks.append(task)
        by_id[task.task_id] = task
    fleet.tasks.extend(new_tasks)
    return new_tasks, seq


def _primary_mech(mech_ids: list[str]) -> str | None:
    """Deterministic primary crew member: smallest mech_id of the crew."""
    return min(mech_ids) if mech_ids else None


def _stability(prev: Schedule, cur: Schedule) -> dict:
    """Slot/mechanic stability over SURVIVING UNEXECUTED tasks.

    Survivors = tasks assigned in BOTH plans (tasks executed done left the
    graph; slid in_progress tasks survive and count — cadence_sim
    semantics: stability is over surviving unexecuted tasks only).
    ``slot_stable_pct`` keeps (day, shift); ``mech_stable_pct`` keeps the
    primary mechanic. An empty survivor set reports 100.0 (nothing could
    churn). Percentages are rounded to 0.1.

    ``in_horizon_slot_stable_pct`` (§12 COMMITMENT, OR-4 measured band):
    same slot-kept metric restricted to survivors whose PREV (incumbent)
    slot lies within ``COMMITMENT_HORIZON_DAYS`` of the new plan's
    start_day (``cur.meta['start_day']``) — the band the commitment
    defense promises to hold. ``in_horizon_survivors`` is the denominator,
    surfaced so a 100.0 over an empty band is visibly vacuous.
    """
    surviving = sorted(set(prev.assignments) & set(cur.assignments))
    if not surviving:
        return {
            "surviving": 0,
            "slot_stable_pct": 100.0,
            "mech_stable_pct": 100.0,
            "in_horizon_survivors": 0,
            "in_horizon_slot_stable_pct": 100.0,
        }
    start_day = int(cur.meta.get("start_day", 0))
    horizon_end = start_day + config.COMMITMENT_HORIZON_DAYS
    slot_same = 0
    mech_same = 0
    ih_n = 0
    ih_same = 0
    for tid in surviving:
        a, b = prev.assignments[tid], cur.assignments[tid]
        same_slot = (a.day, a.shift) == (b.day, b.shift)
        if same_slot:
            slot_same += 1
        if _primary_mech(a.mechanic_ids) == _primary_mech(b.mechanic_ids):
            mech_same += 1
        if start_day <= a.day < horizon_end:
            ih_n += 1
            if same_slot:
                ih_same += 1
    n = len(surviving)
    return {
        "surviving": n,
        "slot_stable_pct": round(100.0 * slot_same / n, 1),
        "mech_stable_pct": round(100.0 * mech_same / n, 1),
        "in_horizon_survivors": ih_n,
        "in_horizon_slot_stable_pct": (
            round(100.0 * ih_same / ih_n, 1) if ih_n else 100.0
        ),
    }


def _plan_stats(schedule: Schedule, wall_s: float) -> dict:
    """Extract the measured per-plan row from Schedule.stats (contract keys)."""
    stats = schedule.stats
    return {
        "fleet_lateness": stats["fleet_lateness_days"],
        "otd": stats["otd_count"],
        "controllable_usd": stats["economics"]["controllable_penalty_usd"],
        "scheduled": stats["scheduled"],
        "unscheduled": stats["unscheduled"],
        "total_tasks": stats["total_tasks"],
        # §12 COMMITMENT counters (0 on non-incumbent plans).
        "commitment_in_horizon": stats.get("commitment_in_horizon", 0),
        "commitment_kept": stats.get("commitment_kept", 0),
        "commitment_mech_kept": stats.get("commitment_mech_kept", 0),
        "wall_s": round(wall_s, 3),
    }


def run_digital_week(
    fleet: Fleet,
    rounds: int = 9,
    seed: int = 7,
    exec_rate: float = EXEC_RATE,
    slide_remain: float = SLIDE_REMAIN,
    rework_per_round: int = REWORK_PER_ROUND,
    start_day: int = 0,
    start_shift: int = 1,
    on_executed=None,
    return_state: bool = False,
    thread_incumbent: bool = True,
    deviate_rate: float = 0.0,
    oos_per_round: int = 0,
    on_replanned=None,
    strict_execution: bool | None = None,
    offplan_value_fn=None,
):
    """Run ``rounds`` digital-week rounds; return the measured result dict.

    Doctrine steps (a)-(e) per round — see the module docstring. The input
    ``fleet`` is NEVER mutated: the simulation runs on a deep copy
    (``Fleet.from_dict(fleet.to_dict())``), the FF_app rendering of
    cadence_sim2's copytree-into-a-work-dir rule.

    ``on_executed`` (optional) is called after steps (a)+(b), BEFORE the
    clock advances and the replan runs, with a context dict::

        {"round", "day", "shift",         # the slot that just executed
         "fleet",                          # the mutated working fleet
         "schedule", "cpm",                # the plan that scheduled the slot
         "planned", "executed", "slid",    # task-id lists
         "injected"}                       # new rework task ids

    (the correlation gate uses it to score per-team shift attainment
    against the plan that was actually executed).

    ``return_state=True`` additionally returns
    ``{"fleet", "schedule", "cpm"}`` — the final mutated fleet and last
    replan — so callers can run the validator on the post-injection plan.

    Result shape (all JSON-safe)::

        {"mock_data": bool, "seed": int, "params": {...},
         "start": {"day", "shift"},
         "baseline": {fleet_lateness, otd, controllable_usd, scheduled,
                      unscheduled, total_tasks, wall_s},
         "rounds": [{round, exec_day, exec_shift, executed, slid, injected,
                     clock_day, clock_shift, day_rolled, surviving,
                     slot_stable_pct, mech_stable_pct,
                     in_horizon_survivors, in_horizon_slot_stable_pct,
                     commitment_in_horizon, commitment_kept,
                     commitment_mech_kept, fleet_lateness, otd,
                     controllable_usd, scheduled, unscheduled, total_tasks,
                     wall_s}, ...],
         "wall_seconds_total": float}

    ``thread_incumbent`` (§12 COMMITMENT, OR-4): True (default) threads
    each round's prior schedule into the replan as the incumbent to
    defend; False is the pre-commitment behavior (the A/B control arm).

    NON-COMPLIANCE LEVERS (5-day demo; both default OFF, leaving the rng
    stream and all previously measured results byte-identical):

    - ``deviate_rate``: probability a planned task is SKIPPED by its crew,
      which instead completes one ready non-critical same-team unplanned
      task (see ``_execute_slot``). Skipped work stays not_started and
      must be replanned; off-plan completions never earn toward the
      skipped slot's goal.
    - ``oos_per_round``: after each executed shift, this many tasks
      complete OUT OF SEQUENCE (an unfinished-but-started predecessor
      jumped — see ``_jump_out_of_sequence``), exercising the P_OOS
      penalty path end to end.

    Round rows gain ``deviated`` / ``offplan_done`` / ``oos_done`` /
    ``blocked_pred`` counts; the ``on_executed`` context gains ``skipped``
    / ``offplan`` / ``oos`` / ``blocked`` id lists.

    ``strict_execution`` (None = auto): strict dependency physics inside
    the executed slot — see ``_execute_slot``. Auto resolves to True
    whenever a non-compliance lever is on (they create the skipped-pred
    states that make loose per-task coins physically impossible) and
    False otherwise, which keeps every legacy rng stream byte-identical.

    ``on_replanned`` (optional) is called after every round's replan with
    ``{"round", "exec_day", "exec_shift", "clock_day", "clock_shift",
    "fleet", "schedule", "cpm", "prev_schedule", "row"}`` — the hook the
    5-day demo uses to export envelopes and roll commitments/progression
    off the REAL post-replan state.

    ``wall_s`` / ``wall_seconds_total`` are the only non-deterministic
    fields. Raises ``ValueError`` for rounds < 1 or an ineligible start
    slot (the clock must start on a plannable shift).
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")
    if not shift_eligible(start_day, start_shift):
        raise ValueError(
            f"start slot (day={start_day}, shift={start_shift}) is not an "
            "eligible slot (working-day rule + OR-3 Sunday-night S3)"
        )
    strict = (
        strict_execution
        if strict_execution is not None
        else (deviate_rate > 0 or oos_per_round > 0)
    )
    rng = Random(seed)
    wall_total0 = time.perf_counter()

    # Work on a clone — the caller's fleet is never mutated (doctrine:
    # simulations run in isolated work copies).
    work = Fleet.from_dict(fleet.to_dict())
    by_id: dict[str, Task] = {t.task_id: t for t in work.tasks}

    t0 = time.perf_counter()
    cpm = compute_cpm(work.tasks)
    schedule = build_schedule(work, cpm, start_day=start_day)
    baseline = _plan_stats(schedule, time.perf_counter() - t0)

    day, shift = start_day, start_shift
    seq = 0
    round_rows: list[dict] = []

    for k in range(1, rounds + 1):
        # (a) execute the shift that just ended (with optional deviation)
        (
            planned, done_ids, slid_ids, skipped_ids, offplan_ids, blocked_ids
        ) = _execute_slot(
            by_id, schedule, day, shift, rng, exec_rate, slide_remain,
            deviate_rate=deviate_rate, fleet=work, cpm=cpm, strict=strict,
            offplan_value_fn=offplan_value_fn,
        )
        # (a2) optional discipline failure: out-of-sequence completions
        oos_ids = _jump_out_of_sequence(
            work, by_id, schedule, day, rng, oos_per_round
        )
        # (b) inject rework the production way (rows join the DAG)
        new_tasks, seq = _inject_rework(
            work, by_id, schedule, day, rng, rework_per_round, seq
        )
        if on_executed is not None:
            on_executed(
                {
                    "round": k,
                    "day": day,
                    "shift": shift,
                    "fleet": work,
                    "schedule": schedule,
                    "cpm": cpm,
                    "planned": planned,
                    "executed": done_ids,
                    "slid": slid_ids,
                    "skipped": skipped_ids,
                    "offplan": offplan_ids,
                    "oos": oos_ids,
                    "blocked": blocked_ids,
                    "injected": [t.task_id for t in new_tasks],
                }
            )
        # (c) advance the clock in factory chronology
        new_day, new_shift, day_rolled = advance_clock(day, shift)
        # (d) replan with the REAL engine on the mutated fleet. §12/OR-4:
        # thread the just-executed plan as the incumbent to defend
        # ("committed work dispatches before new work") unless the caller
        # runs the pre-commitment A/B arm.
        incumbent = incumbent_from_schedule(schedule) if thread_incumbent else None
        t0 = time.perf_counter()
        new_cpm = compute_cpm(work.tasks)
        new_schedule = build_schedule(
            work, new_cpm, start_day=new_day, incumbent=incumbent
        )
        wall = time.perf_counter() - t0
        # (e) measure
        row = {
            "round": k,
            "exec_day": day,
            "exec_shift": shift,
            "executed": len(done_ids),
            "slid": len(slid_ids),
            "deviated": len(skipped_ids),
            "offplan_done": len(offplan_ids),
            "oos_done": len(oos_ids),
            "blocked_pred": len(blocked_ids),
            "injected": len(new_tasks),
            "clock_day": new_day,
            "clock_shift": new_shift,
            "day_rolled": day_rolled,
        }
        row.update(_stability(schedule, new_schedule))
        row.update(_plan_stats(new_schedule, wall))
        round_rows.append(row)

        if on_replanned is not None:
            on_replanned(
                {
                    "round": k,
                    "exec_day": day,
                    "exec_shift": shift,
                    "clock_day": new_day,
                    "clock_shift": new_shift,
                    "fleet": work,
                    "schedule": new_schedule,
                    "cpm": new_cpm,
                    "prev_schedule": schedule,
                    "row": row,
                }
            )

        schedule, cpm = new_schedule, new_cpm
        day, shift = new_day, new_shift

    result = {
        # OR-5 honesty: the label travels with every simulator output.
        "mock_data": bool(work.meta.get("mock_data", True)),
        "seed": seed,
        "params": {
            "rounds": rounds,
            "exec_rate": exec_rate,
            "slide_remain": slide_remain,
            "rework_per_round": rework_per_round,
            "deviate_rate": deviate_rate,
            "oos_per_round": oos_per_round,
            "strict_execution": bool(strict),
            "greedy_offplan": offplan_value_fn is not None,
            "shift_cycle": list(SHIFT_CYCLE),
            "thread_incumbent": bool(thread_incumbent),
        },
        "start": {"day": start_day, "shift": start_shift},
        "baseline": baseline,
        "rounds": round_rows,
        "wall_seconds_total": round(time.perf_counter() - wall_total0, 3),
    }
    if return_state:
        return result, {"fleet": work, "schedule": schedule, "cpm": cpm}
    return result
