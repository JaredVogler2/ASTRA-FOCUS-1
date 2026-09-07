"""V1 point engine — THE single factor implementation + shift targets + leaderboard.

One factor module, two weight profiles (contract §ff/services/points.py):
``score_components(task_id, snap, weights)`` is the ONLY place factor math
lives. ``ff.services.candidates`` imports it with ``CANDIDATE_WEIGHTS``
(pure ranking — effort-weighted factor contributions only); this module's
``score_task`` uses ``POINT_WEIGHTS`` and additionally adds the effort term
itself and applies the out-of-sequence penalty ``P_OOS``.

Formula (GG-2 canon, GAMES: "Value is priority_score-weighted
(mechanic-minute x criticality), never raw task count" — recalibrated
2026-07-10, measured evidence in BUILD_LOG)::

    effort  = max(1, round(duration_minutes * mechanics_required
                           / EFFORT_CREW_MINUTES_PER_POINT))
    total   = max(0, effort + sum(factor contributions) - oos_penalty)
            = effort * value_multiplier            (before rounding), where
    value_multiplier = 1 + sum((W_f / 100) * norm_f) - (P_OOS / 100) * oos

Every factor contribution is ``effort * (weight / 100) * normalized
factor`` — points per crew-minute equal the value multiplier, so sheer
completion volume can never out-point working the right jobs (the previous
flat-points formula FAILED the correlation gate: 30-minute stubs paid
almost the same as keystones and the chaser policy won).

Factors (config §5 weights = percent-of-effort multipliers):

    critical    W_CRIT=60    on the critical path (cpm slack <= 0.5 days)
    urgency     W_URGENCY=25 deadline proximity (less slack = more urgent)
    downstream  W_DOWN=45    transitive successors unlocked
    risk        W_RISK=30    $-exposure of the task's aircraft (PLACEHOLDER
                             rate, config §4 — labeled, OR-5)
    scarcity    W_SCARCE=20  crew size vs qualified holders (team-pure, OR-2)
    recovery    W_RECOVER=30 aircraft already late OR blocked-now-clearable
                             (parts have arrived; clearing it is a recovery play)
    behind      W_BEHIND=35  bounded aging past the task's STATION gate
                             (docs/GATE_PRESSURE_DESIGN.md: behind schedule
                             even with delivery slack; cap BEHIND_CAP_DAYS)

Normalization: continuous factors (urgency/downstream/risk/scarcity) are
ROBUST-PERCENTILE scaled — ``clamp((raw - p_lo) / (p_hi - p_lo), 0, 1)``
with p_lo/p_hi the NORM_PCTL_LO/NORM_PCTL_HI (default 5th/95th) percentiles
of the factor across every task in the snapshot. Raw min-max was measured
to FLATTEN at 55k-task scale (one outlier — e.g. a task with a
multi-thousand-task downstream subtree — pushed every typical task's
normalized value to ~0, so the flat BASE dominated and the correlation gate
failed); percentile bounds are outlier-immune. Normalizers are PER-SNAPSHOT
and frozen on ``snap["normalizers"]`` on first use — every score computed
against one snapshot uses the identical scale, so scores are comparable and
replaying the same snapshot reproduces the same numbers. Pass-through factors
(critical/recovery 0/1, behind already bounded 0..1) skip normalization.

GG guardrails enforced here:
- GG-2: value is mechanic-minute x criticality (the formula above), and the
  leaderboard NEVER ranks by raw points (see ``leaderboard``).
- GG-4 (explainable): the ScoreBreakdown stays fully decomposed — effort is
  a named component and every factor contribution is itemized
  {raw, weight, points}; the explanation template renders it verbatim.
- Out-of-sequence work is PENALIZED (``P_OOS`` percent of effort), never
  rewarded — points cannot be farmed by jumping the DAG.
- Excused work: a shift's goal only counts what was actually planned into
  the slice, so an unstaffable (delayed) task never dilutes attainment.
- GG-3: planned tasks excused for an EXCUSABLE disruption cause (see
  ``ff.services.disruption``) leave the GOAL DENOMINATOR — visibly, never
  silently (``excused_points`` + ``excusals`` on the report). Tasks with
  only non-excusable causes stay in the denominator.

Deterministic throughout: sorted iteration, fixed factor order, tie-break
by id; explanations come from a fixed template.
"""

from __future__ import annotations

import config
from ff.domain import DAY_WORK_MINUTES, Fleet, Schedule, Task
from ff.services.snapshot import (
    component,
    qualified_count,
    tasks_by_id,
)

# Fixed factor order — drives components, explanations, and normalizers.
FACTOR_ORDER = (
    "critical", "urgency", "downstream", "risk", "scarcity", "recovery",
    "behind",
)
# Pass-through factors skip percentile normalization: binary flags plus the
# behind-schedule aging raw, which is ALREADY bounded 0..1 by design
# (min(days_past_gate / BEHIND_CAP_DAYS, 1)) and is zero for most of the
# fleet most of the time — a p5/p95 normalizer over a mostly-zero factor
# degenerates (hi <= lo) and would silence it (docs/GATE_PRESSURE_DESIGN.md
# §2.2).
PASSTHROUGH_FACTORS = frozenset({"critical", "recovery", "behind"})

# Weight profiles (ONE implementation, TWO profiles). Both draw the §5
# weights and both are effort-weighted via score_components; the points
# profile differs by ALSO adding the effort term itself and the P_OOS
# penalty inside score_task (candidates ranking never does).
CANDIDATE_WEIGHTS: dict[str, int] = {
    "critical": config.W_CRIT,
    "urgency": config.W_URGENCY,
    "downstream": config.W_DOWN,
    "risk": config.W_RISK,
    "scarcity": config.W_SCARCE,
    "recovery": config.W_RECOVER,
    "behind": config.W_BEHIND,
}
POINT_WEIGHTS: dict[str, int] = dict(CANDIDATE_WEIGHTS)


# ---------------------------------------------------------------------------
# snapshot-scoped context (lateness map, today) — memoized on the snapshot
# ---------------------------------------------------------------------------


def _ctx(snap) -> dict:
    """Build/memoize {today, lateness-per-aircraft} for factor math.

    Lateness is computed from the SCHEDULE (latest assignment day vs the
    aircraft's delivery deadline), mirroring the engine's stats math —
    aircraft with nothing scheduled carry lateness 0. Deterministic:
    sorted iteration only.
    """
    if isinstance(snap, dict):
        cached = snap.get("_points_ctx")
        if cached is not None:
            return cached
    fleet: Fleet = component(snap, "fleet")
    schedule: Schedule = component(snap, "schedule")
    tasks = tasks_by_id(snap)

    completion: dict[int, int] = {}
    for tid in sorted(schedule.assignments):
        task = tasks.get(tid)
        if task is None:
            continue
        asg = schedule.assignments[tid]
        if asg.day > completion.get(task.aircraft, -1):
            completion[task.aircraft] = asg.day

    lateness: dict[int, int] = {}
    for ac in sorted(fleet.aircraft, key=lambda a: a.aircraft):
        done_day = completion.get(ac.aircraft)
        lateness[ac.aircraft] = (
            max(0, done_day - ac.delivery_deadline_day) if done_day is not None else 0
        )

    ctx = {
        "today": int(schedule.meta.get("start_day", 0)) if schedule.meta else 0,
        "lateness": lateness,
    }
    if isinstance(snap, dict):
        snap["_points_ctx"] = ctx
    return ctx


def _factor_raws(task: Task, snap) -> dict[str, float]:
    """Raw (pre-normalization) factor values for one task.

    Enforces the factor definitions in the module docstring; scarcity uses
    the team-pure pool (OR-2 — borrowing can never dilute scarcity), and
    risk uses the PLACEHOLDER §4 rate (OR-5: callers label the output).
    """
    cpm = component(snap, "cpm")
    info = cpm.get(task.task_id) or {}
    slack_minutes = float(info.get("slack_minutes", 0.0))
    slack_days = slack_minutes / DAY_WORK_MINUTES

    ctx = _ctx(snap)
    late_days = ctx["lateness"].get(task.aircraft, 0)

    holders = sum(qualified_count(snap, task.team, s, task.skill) for s in (1, 2, 3))
    scarcity = float(task.mechanics_required) / float(max(1, holders))

    clearable_block = (
        task.state == "blocked"
        and task.parts_eta_day is not None
        and task.parts_eta_day <= ctx["today"]
    )
    # Gate pressure (docs/GATE_PRESSURE_DESIGN.md): bounded aging vs the
    # task's STATION gate — behind schedule even with delivery slack left.
    # No gate (legacy fixtures) => 0.0, factor inert. Done work never ages.
    behind = 0.0
    if task.gate_day is not None and task.state != "done":
        behind = min(
            max(0, ctx["today"] - int(task.gate_day))
            / float(max(1, config.BEHIND_CAP_DAYS)),
            1.0,
        )
    return {
        "critical": 1.0 if (info and slack_days <= config.CRITICAL_SLACK_MIN) else 0.0,
        "urgency": -slack_minutes,  # less slack (or negative) = more urgent
        "downstream": float(info.get("downstream_count", 0)),
        "risk": float(late_days * config.LATENESS_PENALTY_USD_PER_DAY),
        "scarcity": scarcity,
        "recovery": 1.0 if (late_days > 0 or clearable_block) else 0.0,
        "behind": behind,
    }


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted value list.

    Deterministic pure math (position = pct/100 * (n-1), interpolate
    between the bracketing values); n == 1 returns the single value.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = (pct / 100.0) * (n - 1)
    lo_i = int(pos)
    hi_i = min(lo_i + 1, n - 1)
    frac = pos - lo_i
    return float(sorted_vals[lo_i]) * (1.0 - frac) + float(sorted_vals[hi_i]) * frac


def get_normalizers(snap) -> dict[str, dict]:
    """Return the per-snapshot ROBUST-PERCENTILE normalizers, frozen on first use.

    Enforces the frozen-normalizer rule: computed ONCE over every task in
    the snapshot and stored on ``snap["normalizers"]`` — later state edits
    to the same snapshot object cannot silently rescale scores mid-shift.
    Shape: ``{factor: {"lo": float, "hi": float, "method": "pctl",
    "pct_lo": float, "pct_hi": float}}`` for all factors (binary ones
    included for transparency, though they pass through raw).

    Choice documented (measured, fleet50 synthetic mock data): raw min-max
    collapsed at 55k-task scale — heavy-tailed factors (downstream subtree
    counts, $-risk on the latest aircraft) put the max thousands of times
    above the typical value, normalizing typical tasks to ~0 and letting
    the flat base term dominate (the correlation-gate FAIL). Percentile
    bounds (NORM_PCTL_LO/NORM_PCTL_HI, default p5/p95) are outlier-immune:
    the factor's working body spreads across [0, 1] and whales clamp at 1.
    """
    if isinstance(snap, dict):
        frozen = snap.get("normalizers")
        if frozen:
            return frozen
    fleet: Fleet = component(snap, "fleet")
    values: dict[str, list[float]] = {name: [] for name in FACTOR_ORDER}
    for task in sorted(fleet.tasks, key=lambda t: t.task_id):
        raws = _factor_raws(task, snap)
        for name in FACTOR_ORDER:
            values[name].append(raws[name])
    pct_lo = float(config.NORM_PCTL_LO)
    pct_hi = float(config.NORM_PCTL_HI)
    normalizers: dict[str, dict] = {}
    for name in FACTOR_ORDER:
        vals = sorted(values[name])
        normalizers[name] = {
            "lo": _percentile(vals, pct_lo),
            "hi": _percentile(vals, pct_hi),
            "method": "pctl",
            "pct_lo": pct_lo,
            "pct_hi": pct_hi,
        }
    if isinstance(snap, dict):
        snap["normalizers"] = normalizers
    return normalizers


def _normalize(raw: float, norm: dict) -> float:
    """Robust-percentile scale ``raw`` into [0, 1] against a frozen normalizer.

    ``clamp((raw - lo) / (hi - lo), 0, 1)`` with lo/hi the frozen p_lo/p_hi
    percentile bounds. Degenerate band (hi <= lo: the factor's working body
    is identical across the snapshot) yields 0.0 — a factor that cannot
    discriminate awards no points.
    """
    lo, hi = float(norm["lo"]), float(norm["hi"])
    if hi <= lo:
        return 0.0
    scaled = (raw - lo) / (hi - lo)
    return 0.0 if scaled < 0.0 else (1.0 if scaled > 1.0 else scaled)


# ---------------------------------------------------------------------------
# THE shared factor implementation (candidates imports this)
# ---------------------------------------------------------------------------


def effort_points(task: Task) -> int:
    """Effort term: crew-minutes scaled into the workable point band.

    Enforces GG-2 ("value is priority_score-weighted — mechanic-minute x
    criticality — never raw task count"): points are PROPORTIONAL to
    ``duration_minutes * mechanics_required``, so a shift's earnable points
    are bounded by the crew-minutes it burns and completion-count volume
    can never out-point value. ``EFFORT_CREW_MINUTES_PER_POINT`` (config
    §5, default 5) maps the generator's 30..1440 crew-minute range to
    6..288 points. Always >= 1 — every completed task is worth something.
    """
    crew_minutes = task.duration_minutes * task.mechanics_required
    return max(1, int(round(crew_minutes / float(config.EFFORT_CREW_MINUTES_PER_POINT))))


def score_components(task_id: str, snap, weights: dict[str, int]) -> dict[str, dict]:
    """Decomposed effort-weighted factor scores — the ONE factor implementation.

    Returns ``{name: {"raw": float, "weight": int, "points": int}}`` in
    fixed FACTOR_ORDER for every factor present in ``weights``. Enforces
    the shared-module rule (one implementation, BOTH profiles): candidates
    ranking and the point engine call THIS function with their own weight
    profile, so the two surfaces can never drift apart on factor math.
    Each contribution enforces the GG-2 recalibration:
    ``points = round(effort * (weight / 100) * norm)`` — effort times the
    percent-of-effort weight times the normalized factor — with
    per-snapshot frozen robust-percentile normalizers (binary factors pass
    raw 0/1). Raises ``KeyError`` on an unknown task_id.
    """
    task = tasks_by_id(snap).get(task_id)
    if task is None:
        raise KeyError(f"unknown task_id {task_id!r}")
    effort = effort_points(task)
    raws = _factor_raws(task, snap)
    normalizers = get_normalizers(snap)
    components: dict[str, dict] = {}
    for name in FACTOR_ORDER:
        if name not in weights:
            continue
        weight = int(weights[name])
        raw = raws[name]
        norm = raw if name in PASSTHROUGH_FACTORS else _normalize(raw, normalizers[name])
        components[name] = {
            "raw": raw,
            "weight": weight,
            "points": int(round(effort * (weight / 100.0) * norm)),
        }
    return components


def build_explanation(
    task: Task, components: dict[str, dict], effort: int = 0, oos_penalty: int = 0
) -> str:
    """Deterministic why-these-points template, e.g.
    ``'+80 effort (400m x 1 mech), +48 critical path, +36 unlocks 6 downstream'``.

    GG-4 (explainable): fixed phrase per factor, fixed FACTOR_ORDER,
    zero-point factors omitted; effort/penalty terms (points profile only)
    bracket the factors. Same components always yield the same string
    (chip-stable UI).
    """
    parts: list[str] = []
    if effort:
        parts.append(
            f"+{effort} effort ({task.duration_minutes}m x "
            f"{task.mechanics_required} mech)"
        )
    phrases = {
        "critical": lambda c: f"+{c['points']} critical path",
        "urgency": lambda c: f"+{c['points']} deadline urgency",
        "downstream": lambda c: f"+{c['points']} unlocks {int(c['raw'])} downstream",
        "risk": lambda c: f"+{c['points']} protects ${int(c['raw']):,} at risk",
        "scarcity": lambda c: f"+{c['points']} scarce skill {task.skill}",
        "recovery": lambda c: f"+{c['points']} recovery play",
        "behind": lambda c: f"+{c['points']} behind station gate",
    }
    for name in FACTOR_ORDER:
        comp = components.get(name)
        if comp and comp["points"] > 0:
            parts.append(phrases[name](comp))
    if oos_penalty:
        parts.append(f"-{oos_penalty} out of sequence")
    return ", ".join(parts) if parts else "baseline task, no boost factors"


# ---------------------------------------------------------------------------
# points profile: ScoreBreakdown, shift report, leaderboard
# ---------------------------------------------------------------------------


def _oos_penalty(task: Task, snap) -> int:
    """P_OOS percent of effort when a task ran ahead of an unfinished pred.

    GG guardrail: out-of-sequence work is penalized, never rewarded — you
    cannot farm points by jumping the precedence DAG. The dock scales with
    the task (``round(effort * P_OOS / 100)``) so a big job jumped out of
    sequence loses proportionally, exactly like the factors it forfeits.
    Applies only to work actually begun (in_progress/done); planned work
    cannot be out of sequence (the scheduler forbids it, behavior 5).
    """
    if task.state not in ("in_progress", "done"):
        return 0
    tasks = tasks_by_id(snap)
    for pred_id in sorted(set(task.predecessors)):
        if pred_id == task.task_id:
            continue
        pred = tasks.get(pred_id)
        if pred is not None and pred.state != "done":
            return int(round(effort_points(task) * (config.P_OOS / 100.0)))
    return 0


def score_task(task_id: str, snap) -> dict:
    """ScoreBreakdown for the GAMES layer (points profile).

    Enforces the GG-2 recalibrated formula:
    ``total = max(0, effort + sum(factor points) - oos_penalty)``
    (= effort x value_multiplier) — same factor module as candidates
    (score_components), plus the effort term itself and the effort-scaled
    out-of-sequence penalty P_OOS. GG-4: fully decomposed — the effort term
    appears as the named component ``"effort"`` (raw = crew-minutes,
    weight = the crew-minutes-per-point divisor) alongside every factor.
    Returns ``{task_id, total, effort, oos_penalty, components,
    explanation}``. Deterministic per snapshot (frozen normalizers).
    """
    task = tasks_by_id(snap).get(task_id)
    if task is None:
        raise KeyError(f"unknown task_id {task_id!r}")
    factor_components = score_components(task_id, snap, POINT_WEIGHTS)
    effort = effort_points(task)
    oos = _oos_penalty(task, snap)
    total = max(0, effort + sum(c["points"] for c in factor_components.values()) - oos)
    components: dict[str, dict] = {
        "effort": {
            "raw": float(task.duration_minutes * task.mechanics_required),
            "weight": int(config.EFFORT_CREW_MINUTES_PER_POINT),
            "points": effort,
        }
    }
    components.update(factor_components)
    return {
        "task_id": task_id,
        "total": total,
        "effort": effort,
        "oos_penalty": oos,
        "components": components,
        "explanation": build_explanation(
            task, factor_components, effort=effort, oos_penalty=oos
        ),
    }


def _slice_excusals(snap, team: str, day: int, shift: int) -> dict:
    """Excusable-cause excusal records for one slice, keyed by task_id.

    GG-3 plumbing: merges the deterministic auto attribution with any
    lead-captured records riding on the snapshot (``manual_excusals``,
    attached by the web layer's replan pipeline). ONLY records whose cause
    is in ``disruption.EXCUSABLE`` are returned — non-excusable causes can
    never remove a task from a goal denominator.
    """
    from ff.services import disruption

    if isinstance(snap, dict):
        manual = snap.get("manual_excusals")
    else:
        manual = getattr(snap, "manual_excusals", None)
    merged = disruption.merged_excusals(snap, list(manual or []))
    by_task: dict[str, list[dict]] = {}
    for rec in merged.get((team, day, shift), []):
        if rec["excusable"]:
            by_task.setdefault(rec["task_id"], []).append(rec)
    return by_task


def shift_report(snap, team: str, day: int, shift: int) -> dict:
    """Goal vs earned points for one (team, day, shift) planned slice.

    Slice = assignments booked to exactly that team/day/shift (delayed or
    unstaffable work was never planned in, so it cannot dilute
    attainment). Enforces the GAMES math:

    - goal            = sum of point values of every COUNTED planned task
    - earned          = sum of point values of planned tasks now done
    - attainment      = earned / goal (0.0 when goal is 0 — no fake 100%)
    - difficulty      = average point value of the counted planned slice
    - value_efficiency= earned / max(1, sum of done duration*crew minutes)
      (points per crew-minute actually burned — rewards working the RIGHT
      tasks, not just many minutes)
    - breakdown       = per-task rows sorted by task_id

    GG-3 (excused planned tasks leave the GOAL DENOMINATOR): a planned,
    not-yet-done task carrying an EXCUSABLE disruption cause (LATE_PART,
    CROSS_TEAM_PREDECESSOR, CAPACITY_SHORTAGE, SKILL_SHORTAGE,
    REWORK_INJECTION — auto-attributed or lead-captured) is excluded from
    ``goal`` and surfaced VISIBLY on the report as ``excused_points`` plus
    ``excusals: [{task_id, cause, evidence, source}]`` — never silently.
    Non-excusable causes (SAME_TEAM_PREDECESSOR, DURATION_OVERRUN,
    PLAN_CHURN) stay in the denominator: the team owns that work.
    """
    schedule: Schedule = component(snap, "schedule")
    tasks = tasks_by_id(snap)
    day, shift = int(day), int(shift)
    excused_by_task = _slice_excusals(snap, team, day, shift)

    goal = 0
    earned = 0
    done_effort = 0
    excused_points = 0
    excusals: list[dict] = []
    breakdown: list[dict] = []
    for tid in sorted(schedule.assignments):
        asg = schedule.assignments[tid]
        if asg.team != team or asg.day != day or asg.shift != shift:
            continue
        task = tasks.get(tid)
        if task is None:
            continue
        score = score_task(tid, snap)
        points = score["total"]
        done = task.state == "done"
        # GG-3: an excused (excusable-cause) planned task leaves the goal
        # denominator UNLESS it actually got done — finished work needs no
        # excuse and counts normally.
        excused = (not done) and tid in excused_by_task
        if excused:
            excused_points += points
            for rec in excused_by_task[tid]:
                excusals.append(
                    {
                        "task_id": tid,
                        "cause": rec["cause"],
                        "evidence": rec["evidence"],
                        "source": rec["source"],
                    }
                )
        else:
            goal += points
        if done:
            earned += points
            done_effort += task.duration_minutes * task.mechanics_required
        breakdown.append(
            {
                "task_id": tid,
                "points": points,
                "state": task.state,
                "done": done,
                "excused": excused,
                "duration_minutes": task.duration_minutes,
                "mechanics_required": task.mechanics_required,
                "explanation": score["explanation"],
            }
        )

    counted = sum(1 for row in breakdown if not row["excused"])
    return {
        "team": team,
        "day": day,
        "shift": shift,
        "goal": goal,
        "earned": earned,
        "attainment": round(earned / goal, 4) if goal > 0 else 0.0,
        "difficulty": round(goal / counted, 4) if counted > 0 else 0.0,
        "value_efficiency": round(earned / max(1, done_effort), 4),
        "done_effort_minutes": done_effort,
        "excused_points": excused_points,
        "excusals": excusals,
        "breakdown": breakdown,
    }


def _support_causes(snap, team: str, day: int) -> list[dict]:
    """Top absorbed (EXCUSABLE) causes for one team on one day — PSY-5 input.

    Excusal context for the needs-support framing: counts the team's
    merged excusal records (auto + manual) whose cause is excusable and
    whose day matches (or which are unslotted, day-agnostic delays owned
    by the whole horizon). Record COUNTS only — no point values leak into
    the leaderboard row (GG-2).
    """
    from ff.services import disruption

    if isinstance(snap, dict):
        manual = snap.get("manual_excusals")
    else:
        manual = getattr(snap, "manual_excusals", None)
    merged = disruption.merged_excusals(snap, list(manual or []))
    counts: dict[str, int] = {}
    for (rec_team, rec_day, _shift), records in sorted(merged.items()):
        if rec_team != team or rec_day != day:
            continue
        for rec in records:
            if rec["excusable"]:
                counts[rec["cause"]] = counts.get(rec["cause"], 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"cause": cause, "count": n} for cause, n in top[:3]]


def leaderboard(snap, day: int) -> list[dict]:
    """Rank all teams for one day (3 shifts summed) — NEVER by raw points.

    GG-2 (hard guardrail): teams are ranked by (attainment, then
    value_efficiency), never by raw point totals — a big team with a big
    goal cannot outrank a small team that actually hit its target, so the
    game measures execution quality, not headcount.

    Difficulty-adjusted (X3-2): each row carries the team's own plan
    ``difficulty`` index (avg point value of its counted planned slice —
    the index shift_report already computes) and difficulty is the FINAL
    tie-breaker after attainment and efficiency, so between teams with
    identical execution the harder plan wins; ties beyond that break by
    team id (deterministic).

    PSY-5 needs-support framing (cited law: "bottom of table is framed as
    'needs support' with its excusal context shown — never shamed"): a
    team with a nonzero goal below ``NEEDS_SUPPORT_ATTAINMENT`` gets
    ``needs_support: true`` plus ``support`` = its top absorbed excusable
    causes (framing = help, not blame; no elimination, no shaming copy).

    Returns ``[{team, attainment, efficiency, difficulty, rank,
    needs_support, support}]`` — raw goal/earned point totals NEVER appear
    in the rows.
    """
    fleet: Fleet = component(snap, "fleet")
    schedule: Schedule = component(snap, "schedule")
    day = int(day)
    teams = sorted(
        {m.team for m in fleet.mechanics}
        | {a.team for a in schedule.assignments.values()}
    )

    rows: list[dict] = []
    for team in teams:
        goal = earned = done_effort = counted = 0
        for shift in (1, 2, 3):
            report = shift_report(snap, team, day, shift)
            goal += report["goal"]
            earned += report["earned"]
            done_effort += report["done_effort_minutes"]
            counted += sum(1 for r in report["breakdown"] if not r["excused"])
        attainment = round(earned / goal, 4) if goal > 0 else 0.0
        needs_support = goal > 0 and attainment < config.NEEDS_SUPPORT_ATTAINMENT
        rows.append(
            {
                "team": team,
                "attainment": attainment,
                "efficiency": round(earned / max(1, done_effort), 4),
                "difficulty": round(goal / counted, 4) if counted > 0 else 0.0,
                "needs_support": needs_support,
                # PSY-5: excusal context attaches ONLY where the framing
                # applies — supportive, never a public deficiency list.
                "support": (
                    {"framing": "needs support", "top_causes": _support_causes(snap, team, day)}
                    if needs_support
                    else None
                ),
            }
        )

    # GG-2: sort key is (attainment, efficiency, difficulty) — raw points
    # NEVER appear; difficulty only separates identical execution.
    rows.sort(
        key=lambda r: (-r["attainment"], -r["efficiency"], -r["difficulty"], r["team"])
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows
