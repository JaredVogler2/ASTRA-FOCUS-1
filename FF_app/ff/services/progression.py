"""GAMES progression layer — event stream, streaks, badges, recap cards, levels.

Adapts the FOCU5 X2/X3 canon (`FOCU5/04_games/02_progression_levels_seasons.md`
and `03_leaderboards_streaks_events.md`) to FF_app's snapshot read-model.
Everything here is DERIVED read-model state; nothing writes task/floor state.

Laws enforced in this module (each function's docstring names its own):

- **GG-1 (ambient / zero-click).** Every game event is derived from diffs
  between consecutive snapshot summaries plus the commitments change log —
  there is NO user-generated event and no API that accepts one. The only
  writes are the derived-events persistence (`persist_events`).
- **GG-2 (flow, not volume).** Event point values come from
  ``points.score_task`` (effort x value multiplier); recap "top plays" are
  ranked BY POINT VALUE, never task count.
- **GG-3 (fair by construction).** Streaks: a shift whose goal is 0 after
  excusals (excused-heavy) PAUSES the streak — it neither extends nor
  breaks it. Only an unexcused sub-100% shift breaks a streak.
- **GG-4 (explainable).** Every event carries a non-empty, data-backed
  ``evidence`` string; every badge names the real earning event(s) in
  ``subject.earning_event_ids``.
- **GG-6 (professional, never casino).** Deterministic throughout: no
  randomness, fixed derivation order, ids are pure functions of inputs.
- **GG-8 / OR-5 (honesty).** Every event carries the snapshot's
  ``mock_data`` flag; earn-rate budgets are labeled ``config-defaults``.
- **PSY-4 (mastery arc).** Team XP/levels are a pure fold over credited
  completion events — no code path decays or purchases XP.
- **PSY-8 (aircraft as characters).** ``recovery-moment`` fires ONLY from
  the commitments change log (an improved committed_day), never from a
  locally recomputed projection — the emotional peak IS the economic peak.
- **LB-8 (graceful LLM degradation).** ``build_recap`` produces the full
  deterministic recap card with NO LLM anywhere — this dict IS the
  fallback (and, in FF_app, the only) content.
- **§G8 privacy (team-first).** Nothing in this module aggregates or
  exposes per-mechanic statistics; the mechanic view's "my contribution"
  panel is assembled in the web layer strictly from the logged-in
  mechanic's own assignments.

All figures produced over the shipped fixtures are SYNTHETIC (fleet50
mock data) — callers label them (OR-5).
"""

from __future__ import annotations

import json
import os
import tempfile

import config
from ff.domain import DAY_WORK_MINUTES
from ff.services import disruption, points
from ff.services.snapshot import component, tasks_by_id

# The event vocabulary — VERBATIM ids from the mission spec (X3-adapted).
GAME_EVENT_TYPES: tuple[str, ...] = (
    "completion-credited",  # a task moved to done since the previous summary
    "unlock",               # a completion freed >=1 newly-ready successor
    "keystone-cleared",     # a single gate freed >= KEYSTONE_MIN_UNLOCKED
    "recovery-moment",      # committed_day improved in the commitments log
    "goal-crossed",         # team attainment crossed a §10 milestone
    "streak-extended",      # a shift closed at >= 100% attainment
    "badge-earned",         # emitted by the badge engine (evaluate_badges)
)

# The 4 canon badges (X2 catalog, adapted).
BADGE_IDS: tuple[str, ...] = ("firebreak", "recovery", "clean_sweep", "flow_keeper")

# "Self-caused" disruption causes for Flow Keeper (mission: zero self-caused
# SAME_TEAM_PREDECESSOR / DURATION_OVERRUN excusals in the week). Absorbed
# (excusable) causes are NEVER counted — those teams need support, not blame.
SELF_CAUSED_CAUSES: tuple[str, ...] = (
    disruption.SAME_TEAM_PREDECESSOR,
    disruption.DURATION_OVERRUN,
)

DAYS_PER_WEEK = 7  # week key = day // 7 (work-day based, OR-3 safe: S3 is
# already keyed to the work day it flows into by the scheduler's slot model)


# ---------------------------------------------------------------------------
# snapshot access helpers
# ---------------------------------------------------------------------------


def _snap_field(snap, name: str, default=None):
    """Read an optional snapshot field from a dict or object snapshot."""
    if isinstance(snap, dict):
        value = snap.get(name)
    else:
        value = getattr(snap, name, None)
    return default if value is None else value


def _mock_flag(snap) -> bool:
    """OR-5: propagate the snapshot's mock_data label (True until proven)."""
    fresh = _snap_field(snap, "freshness", {}) or {}
    if isinstance(fresh, dict) and "mock_data" in fresh:
        return bool(fresh["mock_data"])
    return True


def _today(snap) -> int:
    schedule = component(snap, "schedule")
    return int(schedule.meta.get("start_day", 0)) if schedule.meta else 0


def _successors(snap) -> dict:
    """task_id -> sorted successor ids (memoized; per-aircraft DAG, C12)."""
    if isinstance(snap, dict):
        cached = snap.get("_prog_succs")
        if cached is not None:
            return cached
    tasks = tasks_by_id(snap)
    succs: dict[str, list[str]] = {}
    for tid in sorted(tasks):
        for pred in sorted(set(tasks[tid].predecessors)):
            if pred != tid:
                succs.setdefault(pred, []).append(tid)
    if isinstance(snap, dict):
        snap["_prog_succs"] = succs
    return succs


def _excusable_slot_map(snap) -> dict:
    """task_id -> set of (team, day, shift) slots holding an EXCUSABLE record.

    Mirrors ``points._slice_excusals``'s rule (GG-3): only causes in
    ``disruption.EXCUSABLE`` can ever remove a task from a goal
    denominator; manual captures ride on ``snap['manual_excusals']``.
    """
    manual = _snap_field(snap, "manual_excusals", []) or []
    merged = disruption.merged_excusals(snap, list(manual))
    out: dict[str, set] = {}
    for slot in sorted(merged):
        for rec in merged[slot]:
            if rec["excusable"]:
                out.setdefault(rec["task_id"], set()).add(slot)
    return out


def _self_caused_records(snap) -> list[dict]:
    """Merged excusal records with a SELF-CAUSED cause (Flow Keeper input).

    Only SAME_TEAM_PREDECESSOR / DURATION_OVERRUN count — absorbed
    (excusable) causes never cost a badge (GG-3 carry-over; X2 gotcha:
    "you counted absorbed disruptions too").
    """
    manual = _snap_field(snap, "manual_excusals", []) or []
    merged = disruption.merged_excusals(snap, list(manual))
    rows: list[dict] = []
    for slot in sorted(merged):
        for rec in merged[slot]:
            if rec["cause"] in SELF_CAUSED_CAUSES:
                rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# slot aggregation — ONE deterministic pass (streaks, milestones, sweeps)
# ---------------------------------------------------------------------------


def slot_stats(snap) -> dict:
    """Per-(team, day, shift) goal/earned/critical aggregate, one pass.

    Memoized on the snapshot (``_prog_slots``). MIRRORS
    ``points.shift_report``'s GG-3 math (equality is pinned by
    ``tests/test_progression.py::test_slot_stats_matches_shift_report``):
    goal counts every planned task EXCEPT undone tasks holding an excusable
    disruption record for their slot; earned counts done planned tasks;
    every point value comes from the shared ``points.score_task`` (GG-2).

    Returns ``{(team, day, shift): {goal, earned, excused_points,
    attainment, planned, done, critical, critical_done}}`` with the id
    lists sorted (deterministic).
    """
    if isinstance(snap, dict):
        cached = snap.get("_prog_slots")
        if cached is not None:
            return cached
    schedule = component(snap, "schedule")
    cpm = component(snap, "cpm")
    tasks = tasks_by_id(snap)
    excusable = _excusable_slot_map(snap)

    slots: dict = {}
    for tid in sorted(schedule.assignments):
        asg = schedule.assignments[tid]
        task = tasks.get(tid)
        if task is None:
            continue
        key = (asg.team, int(asg.day), int(asg.shift))
        slot = slots.setdefault(
            key,
            {
                "goal": 0,
                "earned": 0,
                "excused_points": 0,
                "planned": [],
                "done": [],
                "critical": [],
                "critical_done": [],
            },
        )
        pts = points.score_task(tid, snap)["total"]
        done = task.state == "done"
        excused = (not done) and key in excusable.get(tid, ())
        slot["planned"].append(tid)
        if excused:
            slot["excused_points"] += pts
        else:
            slot["goal"] += pts
        if done:
            slot["earned"] += pts
            slot["done"].append(tid)
        info = cpm.get(tid) or {}
        slack_days = float(info.get("slack_minutes", 0.0)) / DAY_WORK_MINUTES
        if info and slack_days <= config.CRITICAL_SLACK_MIN:
            slot["critical"].append(tid)
            if done:
                slot["critical_done"].append(tid)
    for slot in slots.values():
        slot["attainment"] = (
            round(slot["earned"] / slot["goal"], 4) if slot["goal"] > 0 else 0.0
        )
    if isinstance(snap, dict):
        snap["_prog_slots"] = slots
    return slots


# ---------------------------------------------------------------------------
# snapshot summary + event derivation (GG-1: derived only, deterministic)
# ---------------------------------------------------------------------------


def snapshot_summary(snap) -> dict:
    """Condense a snapshot into the diffable summary ``derive_events`` takes.

    Pure function of the snapshot: ``{snapshot_id, done, blocked,
    attainment}`` with sorted id lists and string slot keys
    (``"team|day|shift"``) so the summary is JSON-safe. GG-1: this summary
    (not user input) is the ONLY thing events are ever diffed against.
    """
    tasks = tasks_by_id(snap)
    done = sorted(tid for tid in tasks if tasks[tid].state == "done")
    blocked = sorted(tid for tid in tasks if tasks[tid].state == "blocked")
    attainment = {
        f"{team}|{day}|{shift}": slot["attainment"]
        for (team, day, shift), slot in sorted(slot_stats(snap).items())
        if slot["goal"] > 0
    }
    return {
        "snapshot_id": str(_snap_field(snap, "snapshot_id", "")),
        "done": done,
        "blocked": blocked,
        "attainment": attainment,
    }


def _event(
    snap,
    event_type: str,
    subject_key: str,
    *,
    team: str = "",
    day: int = 0,
    shift: int = 0,
    aircraft: int | None = None,
    subject: dict | None = None,
    points_delta: float = 0.0,
    evidence: str,
) -> dict:
    """Assemble one GameEvent row (deterministic id, evidence never empty).

    GG-4: ``evidence`` is required and asserted non-empty; the id is the
    pure function ``f"{snapshot_id}:{event_type}:{subject_key}"`` so
    re-deriving over the same snapshot pair is idempotent (X3-1).
    """
    assert evidence, "GG-4: every game event must carry data-backed evidence"
    sid = str(_snap_field(snap, "snapshot_id", ""))
    return {
        "event_id": f"{sid}:{event_type}:{subject_key}",
        "event_type": event_type,
        "team": team,
        "day": int(day),
        "shift": int(shift),
        "aircraft": aircraft,
        "subject": dict(subject or {}),
        "points_delta": float(points_delta),
        "evidence": evidence,
        "snapshot_id": sid,
        "created_at": str(_snap_field(snap, "created_at", "")),
        "mock_data": _mock_flag(snap),  # OR-5 label rides on every event
    }


def derive_events(prev_snapshot_summary, snap) -> list[dict]:
    """Derive the append-only game events between two consecutive states.

    GG-1 (cited law): events are DERIVED ONLY — diffs of the previous
    summary vs the current snapshot plus the commitments change log; no
    user-generated event exists and nothing here mutates task state.
    Deterministic given inputs (GG-6): fixed derivation order, sorted
    iteration, ids pure functions of (snapshot_id, type, subject); the only
    timestamp is the snapshot's own ``created_at`` (an input, not a clock
    read). ``prev_snapshot_summary is None`` is the baseline: the first
    sighting of a stream has nothing to diff, so it emits NO events.

    Emits, in fixed order:

    - ``completion-credited`` — task done now, not done in prev; carries
      the task's point value (GG-2) and a ``recovery`` flag (the aircraft
      was late, or the task was blocked entering the shift — PSY-8 input
      for the Recovery badge).
    - ``unlock`` — a completion whose task was the LAST unfinished
      predecessor of >=1 incomplete successor (those successors are newly
      ready because of exactly this gate).
    - ``keystone-cleared`` — the unlock count reached
      ``KEYSTONE_MIN_UNLOCKED`` (single gate freeing >= 3).
    - ``recovery-moment`` — a commitments change-log row OF THIS RUN whose
      committed_day IMPROVED (delta_days < 0). PSY-8 (cited law): the
      emotional peak IS the economic peak — the projection/commitments
      layer's own change log is the trigger, never a local recomputation.
    - ``goal-crossed`` — a slot's attainment crossed a §10 milestone
      relative to the previous summary (fires once per crossing).
    - ``streak-extended`` — a slot's attainment reached >= 1.0 (from
      below); subject carries the streak length from ``compute_streak``.
    """
    if prev_snapshot_summary is None:
        return []
    prev = prev_snapshot_summary
    prev_done = set(prev.get("done") or [])
    prev_blocked = set(prev.get("blocked") or [])
    prev_att = prev.get("attainment") or {}

    schedule = component(snap, "schedule")
    tasks = tasks_by_id(snap)
    today = _today(snap)
    succs = _successors(snap)
    now_done = {tid for tid in tasks if tasks[tid].state == "done"}
    newly_done = sorted(now_done - prev_done)

    events: list[dict] = []

    # 1) completion-credited (+ recovery flag for the badge engine).
    for tid in newly_done:
        task = tasks[tid]
        asg = schedule.assignments.get(tid)
        day, shift = (asg.day, asg.shift) if asg is not None else (today, 0)
        score = points.score_task(tid, snap)
        recovery_raw = score["components"].get("recovery", {}).get("raw", 0.0)
        recovery = bool(recovery_raw >= 1.0 or tid in prev_blocked)
        events.append(
            _event(
                snap,
                "completion-credited",
                tid,
                team=task.team,
                day=day,
                shift=shift,
                aircraft=task.aircraft,
                subject={"task_id": tid, "recovery": recovery},
                points_delta=score["total"],
                evidence=(
                    f"completed {tid} (+{score['total']} pts"
                    f"{', recovery play' if recovery else ''})"
                ),
            )
        )

    # 2) unlock + keystone-cleared (LAST-gate attribution: the completed
    #    task was the only predecessor still unfinished in prev).
    for tid in newly_done:
        freed: list[str] = []
        for succ_id in succs.get(tid, []):
            succ = tasks.get(succ_id)
            if succ is None or succ.state == "done":
                continue  # X3 gotcha: never count already-done successors
            preds = sorted(set(succ.predecessors))
            if any(p not in now_done for p in preds if p in tasks):
                continue  # not ready yet
            unfinished_prev = [p for p in preds if p in tasks and p not in prev_done]
            if unfinished_prev == [tid]:
                freed.append(succ_id)
        if not freed:
            continue
        task = tasks[tid]
        asg = schedule.assignments.get(tid)
        day, shift = (asg.day, asg.shift) if asg is not None else (today, 0)
        sample = ", ".join(freed[:3]) + ("…" if len(freed) > 3 else "")
        events.append(
            _event(
                snap,
                "unlock",
                tid,
                team=task.team,
                day=day,
                shift=shift,
                aircraft=task.aircraft,
                subject={"task_id": tid, "unlocked": freed, "count": len(freed)},
                evidence=f"{tid} freed {len(freed)} successor(s): {sample}",
            )
        )
        if len(freed) >= config.KEYSTONE_MIN_UNLOCKED:
            events.append(
                _event(
                    snap,
                    "keystone-cleared",
                    tid,
                    team=task.team,
                    day=day,
                    shift=shift,
                    aircraft=task.aircraft,
                    subject={"task_id": tid, "unlocked": freed, "count": len(freed)},
                    evidence=(
                        f"keystone {tid} cleared — {len(freed)} downstream job(s) "
                        f"now ready (threshold {config.KEYSTONE_MIN_UNLOCKED})"
                    ),
                )
            )

    # 3) recovery-moment — the commitments change log of THIS run (PSY-8).
    commitments = _snap_field(snap, "commitments", {}) or {}
    run_id = commitments.get("run_id")
    for row in commitments.get("changes", []) or []:
        if not isinstance(row, dict) or row.get("run") != run_id:
            continue
        if row.get("old") is None or int(row.get("delta_days", 0)) >= 0:
            continue  # only IMPROVEMENTS are recovery moments
        aircraft = int(row["aircraft"])
        events.append(
            _event(
                snap,
                "recovery-moment",
                f"ac{aircraft}",
                team="",  # fleet-level: aircraft belong to everyone (PSY-8)
                day=today,
                shift=0,
                aircraft=aircraft,
                subject={
                    "aircraft": aircraft,
                    "old": row["old"],
                    "new": row["new"],
                    "delta_days": row["delta_days"],
                    "reason": row.get("reason", ""),
                },
                evidence=(
                    f"Recovery secured on aircraft {aircraft}: committed day "
                    f"{row['old']} -> {row['new']} ({row['delta_days']}d, "
                    f"{row.get('reason', 'schedule improvement')})"
                ),
            )
        )

    # 4) goal-crossed + streak-extended, from the slot aggregate.
    for (team, day, shift), slot in sorted(slot_stats(snap).items()):
        if slot["goal"] <= 0:
            continue
        att = slot["attainment"]
        before = float(prev_att.get(f"{team}|{day}|{shift}", 0.0))
        for milestone in config.GOAL_MILESTONES:
            if before * 100 < milestone <= att * 100:
                events.append(
                    _event(
                        snap,
                        "goal-crossed",
                        f"{team}|{day}|{shift}|{milestone}",
                        team=team,
                        day=day,
                        shift=shift,
                        subject={"milestone": milestone, "attainment": att},
                        evidence=(
                            f"team {team} crossed {milestone}% of the day {day} "
                            f"S{shift} goal (attainment {att})"
                        ),
                    )
                )
        if att >= 1.0 and before < 1.0:
            streak = compute_streak(snap, team, upto=(day, shift))
            events.append(
                _event(
                    snap,
                    "streak-extended",
                    f"{team}|{day}|{shift}",
                    team=team,
                    day=day,
                    shift=shift,
                    subject={"streak": streak["streak"], "attainment": att},
                    evidence=(
                        f"team {team} closed day {day} S{shift} at full "
                        f"attainment — streak {streak['streak']}"
                    ),
                )
            )
    return events


# ---------------------------------------------------------------------------
# streaks (GG-3: excused shifts PAUSE, never break)
# ---------------------------------------------------------------------------


def compute_streak(snap, team: str, upto: tuple | None = None) -> dict:
    """Consecutive shifts at attainment >= 1.0 for one team.

    GG-3 (cited law, tripwire-tested): "excused-heavy shifts (goal 0 after
    excusals) PAUSE never break" — a planned shift whose ENTIRE remaining
    goal was excused (goal == 0 with excused_points > 0) neither extends
    nor breaks the streak; the canon fixture 100%, 100%, excused-miss,
    100% yields streak 3 and alive (X3-3 / GAMES catalog mechanic 2:
    "Excused shifts don't break a streak"). Only an UNEXCUSED sub-100%
    shift resets the streak to 0. Slots with no planned work are not
    shifts (streaks skip non-plannable slots, OR-3-safe: the slot model
    already keys S3 to its work day).

    ``upto=(day, shift)`` bounds the walk (inclusive). Deterministic:
    sorted slot order. Returns ``{team, streak, alive, paused_shifts,
    shifts_counted}``.
    """
    streak = 0
    paused = 0
    counted = 0
    for (slot_team, day, shift), slot in sorted(slot_stats(snap).items()):
        if slot_team != team:
            continue
        if upto is not None and (day, shift) > tuple(upto):
            break
        if slot["goal"] <= 0:
            if slot["excused_points"] > 0:
                paused += 1  # GG-3: pause, never break
            continue
        counted += 1
        if slot["attainment"] >= 1.0:
            streak += 1
        else:
            streak = 0
    return {
        "team": team,
        "streak": streak,
        "alive": streak > 0,
        "paused_shifts": paused,
        "shifts_counted": counted,
    }


# ---------------------------------------------------------------------------
# badge engine (GG-4: every badge names its earning event(s))
# ---------------------------------------------------------------------------


def evaluate_badges(events: list[dict], snap) -> tuple[list[dict], dict]:
    """Deterministic badge rules over the event stream + slot aggregate.

    The 4 canon badges (X2 catalog, mission-adapted):

    - **firebreak** — a ``keystone-cleared`` event unlocking
      >= ``FIREBREAK_MIN_UNLOCKED`` downstream jobs.
    - **recovery** — a ``completion-credited`` event whose subject carries
      ``recovery: true`` (the aircraft was late, or the task was blocked
      entering the shift).
    - **clean_sweep** — a (team, day, shift) slot with
      >= ``CLEAN_SWEEP_MIN_CRITICAL`` planned critical tasks, ALL done
      (the >= 1 floor blocks the vacuous-100% gotcha).
    - **flow_keeper** — a (team, week) with >= 1 credited completion and
      ZERO self-caused (SAME_TEAM_PREDECESSOR / DURATION_OVERRUN) excusal
      records; absorbed causes never count (GG-3 carry-over).

    GG-4 (cited law): every badge row stores ``subject.earning_event_ids``
    — the concrete events that earned it; a badge whose earning events are
    not in the log is NOT awarded. Dedupe is by ``subject.badge_key``
    (badge + scope), so re-evaluating over a grown log never re-awards.
    GG-6: fully deterministic — fixed rule order, sorted iteration.

    Returns ``(new_badge_events, earn_rate_report)`` — the earn-rate
    counters are the badge-inflation monitor (X2: over-earning is a design
    failure, surfaced, never silently accepted).
    """
    awarded_keys = {
        ev["subject"].get("badge_key")
        for ev in events
        if ev.get("event_type") == "badge-earned"
    }
    new_events: list[dict] = []

    def _award(badge, key, team, day, shift, earning_ids, evidence, extra=None):
        if key in awarded_keys or not earning_ids:
            return  # GG-4: no badge without a named earning event
        awarded_keys.add(key)
        subject = {
            "badge": badge,
            "badge_key": key,
            "earning_event_ids": list(earning_ids),
        }
        subject.update(extra or {})
        new_events.append(
            _event(
                snap,
                "badge-earned",
                key,
                team=team,
                day=day,
                shift=shift,
                subject=subject,
                evidence=evidence,
            )
        )

    # -- firebreak + recovery: straight event-triggered rules ----------------
    for ev in events:
        etype = ev.get("event_type")
        if (
            etype == "keystone-cleared"
            and int(ev["subject"].get("count", 0)) >= config.FIREBREAK_MIN_UNLOCKED
        ):
            tid = ev["subject"].get("task_id", "")
            _award(
                "firebreak",
                f"firebreak:{ev['team']}:{tid}",
                ev["team"],
                ev["day"],
                ev["shift"],
                [ev["event_id"]],
                (
                    f"Firebreak: keystone {tid} unlocked "
                    f"{ev['subject'].get('count')} downstream jobs "
                    f"(threshold {config.FIREBREAK_MIN_UNLOCKED})"
                ),
            )
        elif etype == "completion-credited" and ev["subject"].get("recovery"):
            tid = ev["subject"].get("task_id", "")
            _award(
                "recovery",
                f"recovery:{ev['team']}:{tid}",
                ev["team"],
                ev["day"],
                ev["shift"],
                [ev["event_id"]],
                (
                    f"Recovery: closed {tid} on an aircraft that was "
                    f"late/blocked entering the shift"
                ),
            )

    # -- clean_sweep + flow_keeper: slot/week aggregates ----------------------
    completion_ids = {
        ev["subject"].get("task_id"): ev["event_id"]
        for ev in events
        if ev.get("event_type") == "completion-credited"
    }
    slots = slot_stats(snap)
    for (team, day, shift), slot in sorted(slots.items()):
        crit = slot["critical"]
        if len(crit) < config.CLEAN_SWEEP_MIN_CRITICAL:
            continue  # vacuous-100% gotcha: require planned critical work
        if set(crit) - set(slot["critical_done"]):
            continue  # not all planned critical tasks are done
        earning = sorted(completion_ids[t] for t in crit if t in completion_ids)
        _award(
            "clean_sweep",
            f"clean_sweep:{team}:{day}:{shift}",
            team,
            day,
            shift,
            earning,
            (
                f"Clean Sweep: 100% of the {len(crit)} planned critical "
                f"task(s) done on day {day} S{shift}"
            ),
            extra={"critical_done": sorted(crit)},
        )

    self_caused: dict = {}
    for rec in _self_caused_records(snap):
        self_caused.setdefault((rec["team"], rec["day"] // DAYS_PER_WEEK), []).append(
            rec["cause"]
        )
    completions_by_week: dict = {}
    for ev in events:
        if ev.get("event_type") == "completion-credited" and ev.get("team"):
            key = (ev["team"], ev["day"] // DAYS_PER_WEEK)
            completions_by_week.setdefault(key, []).append(ev["event_id"])
    for (team, week) in sorted(completions_by_week):
        if self_caused.get((team, week)):
            continue  # a self-caused excusal in the week forfeits the badge
        earning = sorted(completions_by_week[(team, week)])[:10]
        _award(
            "flow_keeper",
            f"flow_keeper:{team}:w{week}",
            team,
            week * DAYS_PER_WEEK,
            0,
            earning,
            (
                f"Flow Keeper: week {week} closed with zero self-caused "
                f"(SAME_TEAM_PREDECESSOR/DURATION_OVERRUN) excusals — "
                f"absorbed causes never counted (GG-3)"
            ),
            extra={"week": week},
        )

    return new_events, earn_rate_report(events + new_events, snap)


def earn_rate_report(events: list[dict], snap) -> dict:
    """Badge earn-rate counters vs the §10 budgets (inflation monitor).

    X2 obligation (cited): "earn-rate budgets are monitored ... badge
    inflation is a design failure, surfaced, never silently accepted."
    ``alert`` is True when a badge's observed earns exceed
    ``budget_per_team_week * teams * weeks * BADGE_INFLATION_ALERT_FACTOR``.
    Budgets are PLACEHOLDERS (``source: config-defaults``, OR-5).
    """
    slots = slot_stats(snap)
    teams = {team for (team, _d, _s) in slots}
    weeks = {day // DAYS_PER_WEEK for (_t, day, _s) in slots}
    n_teams = max(1, len(teams))
    n_weeks = max(1, len(weeks))
    counts = {badge: 0 for badge in BADGE_IDS}
    for ev in events:
        if ev.get("event_type") == "badge-earned":
            badge = ev["subject"].get("badge")
            if badge in counts:
                counts[badge] += 1
    report: dict = {"teams": n_teams, "weeks": n_weeks, "source": "config-defaults"}
    badges: dict = {}
    for badge in BADGE_IDS:
        budget = float(config.BADGE_EARN_BUDGET_PER_TEAM_WEEK.get(badge, 1.0))
        expected = budget * n_teams * n_weeks
        badges[badge] = {
            "count": counts[badge],
            "budget_per_team_week": budget,
            "expected": expected,
            "alert": counts[badge] > expected * config.BADGE_INFLATION_ALERT_FACTOR,
        }
    report["badges"] = badges
    return report


def badges_for_team(events: list[dict], team: str) -> list[dict]:
    """The team's earned badges, in log (earn) order — GG-4 fields verbatim.

    Each row: ``{badge, badge_key, earning_event_ids, day, shift,
    evidence, event_id}`` — the earning events stay attached so every
    badge remains traceable to the real work that earned it.
    """
    rows: list[dict] = []
    for ev in events:
        if ev.get("event_type") != "badge-earned" or ev.get("team") != team:
            continue
        rows.append(
            {
                "badge": ev["subject"].get("badge"),
                "badge_key": ev["subject"].get("badge_key"),
                "earning_event_ids": list(ev["subject"].get("earning_event_ids", [])),
                "day": ev["day"],
                "shift": ev["shift"],
                "evidence": ev["evidence"],
                "event_id": ev["event_id"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# XP / levels (PSY-4: pure fold over credited completions; no decay path)
# ---------------------------------------------------------------------------


def team_level(events: list[dict], team: str) -> dict:
    """Team XP + level from the credited completion events — PSY-4.

    PSY-4 (cited law): XP is a PURE FOLD over ``completion-credited``
    events for the team — there is no code path that decays XP and no code
    path that adds XP from anything but credited completions (never
    purchasable). Levels come from the monotone §10 ``LEVEL_CURVE``
    (placeholder thresholds, mock-scale). Team-first (§G8): this is TEAM
    XP; no per-mechanic XP exists anywhere in the module.
    """
    xp = sum(
        int(round(ev.get("points_delta", 0.0)))
        for ev in events
        if ev.get("event_type") == "completion-credited" and ev.get("team") == team
    )
    curve = config.LEVEL_CURVE or (0,)
    level = sum(1 for threshold in curve if xp >= threshold)
    level = max(1, level)  # curve[0] == 0 => everyone starts at level 1
    floor = curve[level - 1] if level - 1 < len(curve) else curve[-1]
    next_threshold = curve[level] if level < len(curve) else None
    return {
        "team": team,
        "xp": xp,
        "level": level,
        "xp_into_level": xp - floor,
        "next_level_xp": next_threshold,
        "curve_source": "config-defaults",  # OR-5: placeholder curve
    }


# ---------------------------------------------------------------------------
# recap cards (LB-8: deterministic content IS the product)
# ---------------------------------------------------------------------------


def build_recap(team: str, day: int, shift: int, snap, events: list | None = None) -> dict:
    """Deterministic end-of-shift recap card for (team, day, shift).

    LB-8 (cited law): there is NO LLM here — this deterministic dict IS
    the fallback (and only) recap content; every surface renders complete
    without any model call. Contents (X2-5, adapted): attainment vs goal,
    top plays ranked BY POINT VALUE (GG-2 — never task count), badges
    earned that day, streak state (GG-3 pause semantics), recovery moments
    (fleet-level, from the commitments-log-derived events only — PSY-8),
    points banked (team XP), and the excused-shortfall summary with causes
    (GG-3: visible, never silent). OR-5: carries the snapshot's mock_data
    flag so renderers show the banner.
    """
    if events is None:
        events = _snap_field(snap, "game_events", []) or []
    day, shift = int(day), int(shift)
    report = points.shift_report(snap, team, day, shift)
    top_plays = sorted(
        (row for row in report["breakdown"] if row["done"]),
        key=lambda r: (-r["points"], r["task_id"]),
    )
    badges = [
        row for row in badges_for_team(events, team) if row["day"] == day
    ]
    recovery_moments = [
        {
            "aircraft": ev.get("aircraft"),
            "evidence": ev["evidence"],
            "delta_days": ev["subject"].get("delta_days"),
        }
        for ev in events
        if ev.get("event_type") == "recovery-moment"
    ][-3:]
    return {
        "team": team,
        "day": day,
        "shift": shift,
        "attainment": report["attainment"],
        "goal": report["goal"],
        "earned": report["earned"],
        "excused_points": report["excused_points"],
        "excusals": report["excusals"],
        "top_plays": [
            {
                "task_id": row["task_id"],
                "points": row["points"],
                "explanation": row["explanation"],
            }
            for row in top_plays[:5]
        ],
        "badges": badges,
        "streak": compute_streak(snap, team, upto=(day, shift)),
        "recovery_moments": recovery_moments,
        "points_banked": team_level(events, team)["xp"],
        "snapshot_id": str(_snap_field(snap, "snapshot_id", "")),
        "mock_data": _mock_flag(snap),
        "generated_by": "deterministic",  # LB-8: no LLM, by construction
    }


# ---------------------------------------------------------------------------
# persistence — append-only data/game_events.jsonl (atomic)
# ---------------------------------------------------------------------------


def load_events(path: str) -> list[dict]:
    """Read the JSONL event log; tolerant (bad lines skipped, order kept).

    A missing or unreadable file yields an empty log — the stream then
    restarts from the next baseline, which is honest (we never invent an
    event history we cannot read back).
    """
    if not path or not os.path.exists(path):
        return []
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("event_id"):
                    events.append(row)
    except OSError:
        return []
    return events


def append_events(log: list[dict], new_events: list[dict]) -> list[dict]:
    """Append new events to the in-memory log, idempotently.

    APPEND-ONLY law (GG-1/X3-1): existing rows are never mutated or
    removed; a new event whose ``event_id`` is already present is skipped
    (re-deriving the same snapshot pair is a no-op). Returns the events
    actually appended.
    """
    seen = {ev.get("event_id") for ev in log}
    appended: list[dict] = []
    for ev in new_events:
        if ev.get("event_id") in seen:
            continue
        seen.add(ev["event_id"])
        log.append(ev)
        appended.append(ev)
    return appended


def persist_events(events: list[dict], path: str) -> str:
    """Atomically persist the whole event log to ``data/game_events.jsonl``.

    Same atomic pattern as actuals/commitments (tmp file + fsync +
    ``os.replace``) so a crash or concurrent boot never sees a truncated
    log. The FILE is rewritten whole but the LOG is append-only: rows keep
    their original order and are never mutated (append-only is a property
    of the data flow — ``append_events`` is the only writer into the
    list). One JSON object per line, sorted keys (deterministic bytes for
    identical content).
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ff-events-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# composed pipeline (web layer entry point)
# ---------------------------------------------------------------------------


def advance(prev_summary, log: list[dict], snap) -> tuple[list[dict], dict, dict]:
    """One derivation step: diff-derive events, run badges, summarize.

    The web layer's single entry point (boot + every replan): derives the
    new events against ``prev_summary`` (None = baseline, no events),
    appends them to the append-only ``log`` (idempotent), runs the badge
    engine over the FULL log (so earning events from earlier derivations
    stay nameable — GG-4), appends any new badges, and returns
    ``(appended_events, new_summary, earn_rate_report)``. GG-1: reads the
    snapshot, writes only the derived log.
    """
    appended = append_events(log, derive_events(prev_summary, snap))
    badge_events, earn_rates = evaluate_badges(list(log), snap)
    appended += append_events(log, badge_events)
    return appended, snapshot_summary(snap), earn_rates
