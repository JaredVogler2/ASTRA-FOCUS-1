"""ff.export.envelope — max_v1-compatible envelope exporter (INCREMENT 6 Part A).

Writes ``max_v1_{YYYYMMDD}_S{shift}_{HHMMSS}_UTC.json.gz`` files that the
vendored FOCUS Flask dashboard (``FF_app/web_flask``) consumes through its
``src/max_adapter.py`` — the CONTRACT this module satisfies, verified field
by field against that adapter's source (and GROUNDING.md section 2):

- detection: ``mechanic_timelines`` is a DICT keyed by mechanic id (the
  is_max_envelope signal; FGI envelopes carry a list);
- per-task: the adapter ranks by ``cpm_priority``/``cpm_slack`` (priority
  1 = most critical), rewrites ``teamSkill`` to "{team} S{shift} ({skill})",
  maps ``mechanic_id`` to a per-pool integer + ``bems_id``, and reads
  ``day/shift/start_minute/end_minute/duration_minutes/mechanics/
  mechanicIds/is_unlimited_capacity/isFallback/crewShortfall`` for
  utilization/staffing/products math;
- ``teamCapacities[team] = {total_mechanics, mechanics_by_shift, skills}``
  (roster headcounts — the adapter's seat-count source);
- products/aircraft_status: ``deadline_iso`` / ``deadline_day`` looked up
  per line; completion is recomputed from tasks by the adapter;
- ``metadata.stats`` blocks read by the vendored insight blueprints
  (projection.py, shiftbook.py): ``economics`` (fleet + per_aircraft),
  ``capacity_pressure`` (camelCase pools + linesAnalyzed), ``projection``
  (committed/earlyFlow/delivered/changes, stationFeed:false),
  ``final_lateness``, ``otd``, ``commitment_kept``/``commitment_in_horizon``.

Identifier convention (verified against the adapter's key usage):
``soi = task_id`` (e.g. "0021-T00049"), ``line_number = aircraft`` (int),
``taskId = f"{soi}_{line_number}"`` — matching the MAX writer's
``taskId = f"{soi}_{line}"`` convention the adapter/blueprints assume.

Mechanic identity: the vendored My Day tab lists only ALL-DIGIT mechanic
ids (``_is_real_mech`` = ``str.isdigit`` — the FGI "real BEMS" grammar).
FF roster ids ("T01-S1-M001") are therefore exported as their digit
projection ("011001": team digits + shift digit + member digits), which is
unique for the generator's fixed-width id grammar; collisions (only
possible with hand-built odd ids) are disambiguated deterministically.
The original FF ids ride along per task under ``ff_mechanic_ids`` (and the
BEMS->FF map under top-level ``ff_mechanic_map``) so the Part-C write-back
can translate back losslessly.

Honesty rules carried through (OR-5): ``metadata.mock_data`` from
fleet.meta; economics labeled ``source: config-defaults``; unscheduled
work is exported at horizon end with ``isFallback: true`` and a full
``crewShortfall`` — the C20-style honest flag — NEVER as a normal placement.

Determinism: identical inputs + identical ``now`` produce byte-identical
files (loader.save_json_gz: sorted keys, gzip mtime=0). ``now`` defaults to
wall clock — a freshness stamp, like Schedule.stats["wall_seconds"].
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import config
from ff.data import loader
from ff.domain import (
    DAY_WORK_MINUTES,
    Fleet,
    Schedule,
    is_working_day,
)
from ff.engine.cpm import is_critical

# ---------------------------------------------------------------------------
# frame constants
# ---------------------------------------------------------------------------

# FF calendar law: day 0 is a Monday. 2026-01-26 IS a Monday — the fixed,
# deterministic reference date that anchors every exported ISO timestamp.
REFERENCE_DATE = date(2026, 1, 26)

# FGI wall-clock shift starts (INCREMENT 6 addendum): S1 06:00, S2 14:30,
# S3 22:30. Matches the MAX writer's _shift_start_iso mapping.
SHIFT_WALL_START = {1: (6, 0), 2: (14, 30), 3: (22, 30)}

# Paid minutes per shift (utilization denominators). Mirrors MAX
# SHIFT_PAID = {1:480, 2:480, 3:390} = effective + 20-minute paid buffer;
# derived from config so an env-overridden SHIFT_EFFECTIVE flows through.
SHIFT_PAID = {s: int(config.SHIFT_EFFECTIVE[s]) + 20 for s in (1, 2, 3)}

# R0 org mapping (mirrors ff.web.app TEAM_GROUP_SIZE): superintendent
# groups are contiguous chunks of 5 over the SORTED team list -> "G1"...
TEAM_GROUP_SIZE = 5

SCHEDULE_LABEL = "ff_v1"
ENVELOPE_NAME = "FF_app Fable Schedule"

# Honest provenance labels (never reuse MAX's cs744/max_cs_window values —
# FF deadlines come from the synthetic fleet fixture).
DEADLINE_SOURCE = "fleet_fixture"
PLACED_BY = "greedy_named"
PLACED_BY_FALLBACK = "delay_unplaced"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _day_date(day: int) -> date:
    return REFERENCE_DATE + timedelta(days=int(day))


def _slot_iso(day: int, shift: int, minute: int) -> str:
    """Wall-clock ISO timestamp for (day, shift, minute-into-shift)."""
    hour, mins = SHIFT_WALL_START.get(int(shift), (6, 0))
    base = datetime.combine(_day_date(day), dtime(hour=hour, minute=mins))
    return (base + timedelta(minutes=int(minute))).isoformat()


def build_bems_map(fleet: Fleet) -> dict[str, str]:
    """Deterministic FF-mech-id -> all-digit BEMS string map.

    The digit projection of the roster grammar f"{team}-S{shift}-M{i:03d}"
    ("T01-S1-M001" -> "011001") is unique for fixed-width ids; any
    collision (hand-built odd ids only) is disambiguated by appending a
    sorted-order sequence digit — deterministic for a given roster.
    """
    out: dict[str, str] = {}
    taken: set[str] = set()
    for mech in sorted(fleet.mechanics, key=lambda m: m.mech_id):
        digits = "".join(ch for ch in str(mech.mech_id) if ch.isdigit())
        if not digits:
            # No digits at all: deterministic ordinal-code fallback.
            digits = "".join(f"{ord(ch):03d}" for ch in str(mech.mech_id))
        candidate = digits
        seq = 0
        while candidate in taken:
            candidate = f"{digits}{seq}"
            seq += 1
        out[mech.mech_id] = candidate
        taken.add(candidate)
    return out


def team_group_map(fleet: Fleet) -> dict[str, str]:
    """team -> superintendent group id ("G1".."Gn"), mirroring ff.web.app."""
    teams = sorted(
        {t.team for t in fleet.tasks} | {m.team for m in fleet.mechanics}
    )
    groups: dict[str, str] = {}
    for i, team in enumerate(teams):
        groups[team] = f"G{i // TEAM_GROUP_SIZE + 1}"
    return groups


def _cpm_fields(cpm: dict, task_id: str) -> tuple[float | None, float | None, bool]:
    """(cpm_slack in DAYS of work content, cpm_priority, isCritical)."""
    info = cpm.get(task_id)
    if not info:
        return None, None, False
    slack_days = round(float(info.get("slack_minutes", 0.0)) / DAY_WORK_MINUTES, 2)
    prio = round(float(info.get("cpm_priority", 0.0)), 4)
    return slack_days, prio, bool(is_critical(info))


# ---------------------------------------------------------------------------
# per-task records
# ---------------------------------------------------------------------------


def _base_record(task, cpm: dict, groups: dict, station: str) -> dict:
    """Fields shared by scheduled and fallback records for one Task."""
    soi = task.task_id
    line = int(task.aircraft)
    slack_days, prio, critical = _cpm_fields(cpm, soi)
    if task.is_rework:
        ttype = "Rework"
    elif task.is_inspection:
        ttype = "Inspection"
    else:
        ttype = "Production"
    blocked = task.state == "blocked"
    rec = {
        "taskId": f"{soi}_{line}",
        "task_key": [soi, line],
        "soi": soi,
        "product": f"AC-{line:04d}",
        "line_number": line,
        "name": task.name,
        "type": ttype,
        "team": task.team,
        "teams": [task.team],
        "teamSkill": f"{task.team} ({task.skill})",
        "skill": task.skill,
        "resource_team": task.team,
        "superintendent": groups.get(task.team, "G1"),
        "customer_code": "",
        "lane": 0,
        "cs": station,
        "is_inspection": bool(task.is_inspection),
        "is_customer_inspection": False,
        "is_unlimited_capacity": False,
        "is_duration_segment": False,
        "parent_soi": None,
        "segment_id": None,
        "total_segments": None,
        "state": task.state,
        "partsEta": task.parts_eta_day if blocked else None,
        "ecdDay": task.parts_eta_day if blocked else None,
        "remainingMinutes": task.remaining_minutes,
        "priorMechanicId": None,
        "isLatePartTask": blocked,
        "memberMinutes": {},
        "development": [],
        "crewBands": {},
        "devCritical": False,
        "family": None,
        "deadlineDay": int(task.deadline_day),
        "deadlineSource": DEADLINE_SOURCE,
        "csDeadlineDay": None,
        "isReworkTask": ttype == "Rework",
        "isCritical": critical,
        "isCustomerTask": False,
        "isQualityTask": bool(task.is_inspection),
        "isVendorTask": False,
        "workGroup": "quality" if task.is_inspection else "mechanic",
        "dependencies": [f"{p}_{line}" for p in sorted(set(task.predecessors))],
        # FF ride-along identity (Part-C write-back translates through these).
        "ff_task_id": soi,
    }
    if slack_days is not None:
        rec["cpm_slack"] = slack_days
    if prio is not None:
        rec["cpm_priority"] = prio
    return rec


def _scheduled_record(task, asn, cpm, groups, station, bems_map) -> dict:
    rec = _base_record(task, cpm, groups, station)
    duration = int(asn.end_minute) - int(asn.start_minute)
    crew_bems = sorted(bems_map[m] for m in asn.mechanic_ids)
    rec.update(
        {
            "day": int(asn.day),
            "shift": int(asn.shift),
            "start_minute": int(asn.start_minute),
            "end_minute": int(asn.end_minute),
            "duration": duration,
            "duration_minutes": duration,
            "startTime": _slot_iso(asn.day, asn.shift, asn.start_minute),
            "endTime": _slot_iso(asn.day, asn.shift, asn.end_minute),
            "mechanics": len(asn.mechanic_ids),
            "mechanic_id": crew_bems[0] if crew_bems else None,
            "mechanicIds": crew_bems,
            "uses_overtime": bool(asn.uses_overtime),
            "isFallback": False,
            "crewShortfall": 0,
            "placedBy": PLACED_BY,
            "kept_incumbent": bool(getattr(asn, "kept_incumbent", False)),
            "ff_mechanic_ids": sorted(asn.mechanic_ids),
        }
    )
    return rec


def _fallback_record(task, reason, fallback_day, cpm, groups, station) -> dict:
    """HONEST export of unscheduled work (addendum: the adapter coerces a
    missing day to 0 — smearing unplaceable work onto day 0 — so instead
    it is exported at horizon end with the C20-style flags: isFallback +
    full crewShortfall, requires_manual_review via the adapter)."""
    rec = _base_record(task, cpm, groups, station)
    duration = int(
        task.remaining_minutes
        if task.state == "in_progress" and task.remaining_minutes is not None
        else task.duration_minutes
    )
    rec.update(
        {
            "day": int(fallback_day),
            "shift": 1,
            "start_minute": 0,
            "end_minute": duration,
            "duration": duration,
            "duration_minutes": duration,
            "startTime": _slot_iso(fallback_day, 1, 0),
            "endTime": _slot_iso(fallback_day, 1, duration),
            "mechanics": int(task.mechanics_required),
            "mechanic_id": None,
            "mechanicIds": [],
            "uses_overtime": False,
            "isFallback": True,
            "crewShortfall": int(task.mechanics_required),
            "placedBy": PLACED_BY_FALLBACK,
            "kept_incumbent": False,
            "ff_mechanic_ids": [],
            "unscheduledReason": reason,
        }
    )
    return rec


# ---------------------------------------------------------------------------
# metadata.stats blocks (consumed by the vendored projection.py blueprint)
# ---------------------------------------------------------------------------


def _economics_block(stats: dict) -> dict:
    """FF fleet_economics -> the {source, fleet, per_aircraft} shape the
    vendored /api/projection/economics endpoint + partial consume
    (fields: line_number, lateness_days, floor_days, controllable_days,
    penalty_usd, controllable_penalty_usd; fleet: aircraft_count,
    late_count, total/unavoidable/controllable_penalty_usd,
    guard_breaches)."""
    econ = stats.get("economics") or {}
    rows = econ.get("aircraft") or []
    per_aircraft = []
    for r in rows:
        row = dict(r)
        row["line_number"] = int(row.pop("aircraft"))
        per_aircraft.append(row)
    return {
        "source": econ.get("source", config.ECONOMICS_SOURCE),
        "fleet": {
            "aircraft_count": len(rows),
            "late_count": econ.get("late_count", 0),
            "total_penalty_usd": econ.get("total_penalty_usd", 0),
            "unavoidable_penalty_usd": econ.get("unavoidable_penalty_usd", 0),
            "controllable_penalty_usd": econ.get("controllable_penalty_usd", 0),
            "guard_breaches": 0,  # FF has no guard model — honestly zero
        },
        "per_aircraft": per_aircraft,
    }


def _capacity_block(stats: dict, tasks: list[dict]) -> dict:
    """FF capacity.pressure -> camelCase pools + linesAnalyzed (the shape
    the vendored /api/capacity endpoint + _capacity_view partial consume:
    team, shift, waitDays, dollarDays, aircraftTouched, topSkills)."""
    cp = stats.get("capacity_pressure") or {}
    skills_by_pool: dict[tuple, dict] = {}
    for t in tasks:
        key = (t.get("team"), int(t.get("shift") or 0))
        counts = skills_by_pool.setdefault(key, {})
        skill = t.get("skill") or "ANY"
        counts[skill] = counts.get(skill, 0) + 1
    pools = []
    for p in cp.get("pools") or []:
        counts = skills_by_pool.get((p.get("team"), int(p.get("shift") or 0)), {})
        top = [s for s, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        pools.append(
            {
                "team": p.get("team"),
                "shift": p.get("shift"),
                "waitDays": p.get("wait_days"),
                "dollarDays": p.get("dollar_days"),
                "aircraftTouched": p.get("aircraft_touched"),
                "topSkills": top,
            }
        )
    return {
        "pools": pools,
        "linesAnalyzed": len(cp.get("late_aircraft") or []),
        "totalWaitDays": cp.get("total_wait_days"),
        "totalDollarDays": cp.get("total_dollar_days"),
        "penalty_usd_per_day": cp.get("penalty_usd_per_day"),
        "source": cp.get("source"),
        "method": cp.get("method"),
    }


def _projection_block(
    fleet: Fleet, schedule: Schedule, commitments_block: dict | None, run_id: str
) -> dict:
    """FF commitments block + aircraft stats -> the MAX-style projection
    block (rows: line, station, projectedDay/Date, deadlineDay,
    latenessDays [+ committedDay/Date]; changes rows: run, line, old, new,
    delta_days, reason; stationFeed: false — FF has no station feed)."""
    stats_rows = {int(r["aircraft"]): r for r in schedule.stats.get("aircraft", [])}
    stations = {a.aircraft: a.station for a in fleet.aircraft}

    open_aircraft: set[int] = set()
    for t in fleet.tasks:
        if t.state != "done":
            open_aircraft.add(int(t.aircraft))

    def _row(aircraft: int, projected_day: int, committed_day=None) -> dict:
        srow = stats_rows.get(aircraft, {})
        deadline = srow.get("deadline_day")
        row = {
            "line": aircraft,
            "station": stations.get(aircraft, "?"),
            "projectedDay": int(projected_day),
            "projectedDate": _day_date(projected_day).isoformat(),
            "deadlineDay": int(deadline) if deadline is not None else None,
            "latenessDays": int(srow.get("lateness_days", 0)),
        }
        if committed_day is not None:
            row["committedDay"] = int(committed_day)
            row["committedDate"] = _day_date(committed_day).isoformat()
        return row

    cb = commitments_block or {}
    committed_rows = []
    committed_acs: set[int] = set()
    for r in cb.get("committed") or []:
        ac = int(r["aircraft"])
        committed_acs.add(ac)
        committed_rows.append(_row(ac, r["projected_day"], r["committed_day"]))

    delivered = [
        {"line": int(a.aircraft), "station": a.station}
        for a in sorted(fleet.aircraft, key=lambda a: a.aircraft)
        if int(a.aircraft) not in open_aircraft
    ]
    early = [
        _row(ac, stats_rows.get(ac, {}).get("completion_day", 0))
        for ac in sorted(open_aircraft - committed_acs)
    ]
    changes = []
    for c in cb.get("changes") or []:
        changes.append(
            {
                "run": c.get("run"),
                "line": int(c.get("aircraft", 0)),
                "old": c.get("old"),
                "new": c.get("new"),
                "delta_days": c.get("delta_days"),
                "reason": c.get("reason"),
            }
        )
    return {
        "runId": (cb.get("run_id") or run_id),
        "committed": committed_rows,
        "earlyFlow": early,
        "delivered": delivered,
        "changes": changes,
        "stationFeed": False,
    }


def _otd_block(stats: dict) -> dict:
    """MAX compute_otd_kpis shape from FF aircraft stats (honest subset)."""
    rows = stats.get("aircraft", [])
    late = [r for r in rows if not r.get("on_time", False)]
    return {
        "aircraft_count": len(rows),
        "on_time_count": len(rows) - len(late),
        "otd_rate": round((len(rows) - len(late)) / len(rows), 4) if rows else 1.0,
        "max_lateness_days": max((r.get("lateness_days", 0) for r in rows), default=0),
        "late_aircraft": [
            {
                "line_number": int(r["aircraft"]),
                "lateness_days": int(r.get("lateness_days", 0)),
                "deadline_source": DEADLINE_SOURCE,
            }
            for r in sorted(late, key=lambda r: -r.get("lateness_days", 0))
        ],
    }


# ---------------------------------------------------------------------------
# commitments block (CLI path — the web path passes snapshot["commitments"])
# ---------------------------------------------------------------------------


def update_commitments_for_cli(
    fleet: Fleet, schedule: Schedule, run_id: str, commitments_path: str
) -> dict:
    """Load -> update -> persist the commitments state (MAX doctrine: every
    engine run advances the projection state) and return the snapshot-shaped
    commitments block. Mirrors ff.web.app.rebuild_state's wiring exactly
    (§9, OR-4 hysteresis rules live in ff.services.commitments)."""
    from ff.services import commitments as commitments_svc

    state = commitments_svc.load_state(commitments_path)
    projections = commitments_svc.projections_from_stats(schedule.stats)
    evidence = commitments_svc.build_evidence(fleet, schedule)
    state, _changes = commitments_svc.update_commitments(
        state, projections, run_id, evidence
    )
    commitments_svc.persist_state(state, commitments_path)
    deadlines = {a.aircraft: a.delivery_deadline_day for a in fleet.aircraft}
    return commitments_svc.snapshot_block(state, projections, deadlines, run_id)


# ---------------------------------------------------------------------------
# envelope assembly + export
# ---------------------------------------------------------------------------


def build_envelope(
    fleet: Fleet,
    schedule: Schedule,
    cpm: dict,
    snapshot: dict | None = None,
    shift_number: int = 1,
    now: datetime | None = None,
    commitments_block: dict | None = None,
) -> dict:
    """Assemble the max_v1 envelope dict (pure; no I/O).

    ``snapshot`` (the ff.services.snapshot dict) supplies ``snapshot_id``
    (run identity) and — on the web path — the ``commitments`` block;
    ``commitments_block`` overrides it (CLI path). Both optional: without
    either, the projection block carries every open aircraft as earlyFlow
    (honest: nothing has been committed).
    """
    now = now or datetime.now(timezone.utc)
    stats = schedule.stats or {}

    run_id = None
    if isinstance(snapshot, dict):
        run_id = snapshot.get("snapshot_id")
        if commitments_block is None:
            cblock = snapshot.get("commitments")
            commitments_block = cblock if isinstance(cblock, dict) else None
    if run_id is None:
        from ff.services.snapshot import compute_snapshot_id

        run_id = compute_snapshot_id(
            fleet.meta.get("seed") if isinstance(fleet.meta, dict) else None,
            schedule,
        )

    by_id = {t.task_id: t for t in fleet.tasks}
    stations = {a.aircraft: a.station for a in fleet.aircraft}
    groups = team_group_map(fleet)
    bems_map = build_bems_map(fleet)

    # ---- tasks (scheduled first, then honest fallback rows) --------------
    tasks: list[dict] = []
    makespan = max((a.day for a in schedule.assignments.values()), default=0)
    for tid in sorted(schedule.assignments):
        asn = schedule.assignments[tid]
        task = by_id.get(tid)
        if task is None:  # defensive: never invent a record
            continue
        tasks.append(
            _scheduled_record(
                task, asn, cpm, groups, stations.get(task.aircraft, "?"), bems_map
            )
        )
    fallback_day = makespan + 1
    for tid in sorted(schedule.unscheduled):
        task = by_id.get(tid)
        if task is None:
            continue
        tasks.append(
            _fallback_record(
                task,
                schedule.unscheduled[tid],
                fallback_day,
                cpm,
                groups,
                stations.get(task.aircraft, "?"),
            )
        )

    # ---- precedence maps (over the OPEN graph the tasks list covers) -----
    exported = {t["taskId"] for t in tasks}
    predecessors_map: dict[str, list[str]] = {}
    successors_map: dict[str, list[str]] = {}
    for t in tasks:
        tid = t["taskId"]
        deps = [d for d in t["dependencies"] if d in exported]
        if deps:
            predecessors_map[tid] = deps
        for d in deps:
            successors_map.setdefault(d, []).append(tid)
    for v in successors_map.values():
        v.sort()

    # ---- products + aircraft_status ---------------------------------------
    lines_with_tasks: dict[int, list[dict]] = {}
    for t in tasks:
        lines_with_tasks.setdefault(int(t["line_number"]), []).append(t)
    deadline_by_line = {
        int(a.aircraft): int(a.delivery_deadline_day) for a in fleet.aircraft
    }
    products = []
    for line in sorted(lines_with_tasks):
        ltasks = lines_with_tasks[line]
        completion_day = max(int(t["day"]) for t in ltasks)
        deadline_day = deadline_by_line.get(line)
        products.append(
            {
                "line_number": line,
                "name": f"Line {line}",
                "task_count": len(ltasks),
                "completion_day": completion_day,
                "completion_iso": _slot_iso(
                    completion_day, 1, config.SHIFT_EFFECTIVE[1]
                ),
                "deadline_iso": (
                    _day_date(deadline_day).isoformat()
                    if deadline_day is not None
                    else None
                ),
            }
        )
    aircraft_status = [
        {
            "line_number": int(r["aircraft"]),
            "completion_day": int(r.get("completion_day", 0)),
            "deadline_day": int(r["deadline_day"]),
            "lateness_days": int(r.get("lateness_days", 0)),
            "on_time": bool(r.get("on_time", False)),
        }
        for r in stats.get("aircraft", [])
    ]

    # ---- team capacities (roster seats — the adapter's headcount source) --
    team_caps: dict[str, dict] = {}
    for mech in sorted(fleet.mechanics, key=lambda m: m.mech_id):
        rec = team_caps.setdefault(
            mech.team,
            {
                "total_mechanics": 0,
                "mechanics_by_shift": {"1": 0, "2": 0, "3": 0},
                "skills": {},
            },
        )
        rec["total_mechanics"] += 1
        rec["mechanics_by_shift"][str(int(mech.shift))] += 1
        for skill in mech.skills:
            rec["skills"][skill] = rec["skills"].get(skill, 0) + 1

    # ---- mechanic timelines (dict = the MAX-detection signal) ------------
    mech_timelines: dict[str, list[dict]] = {}
    for tid in sorted(schedule.assignments):
        asn = schedule.assignments[tid]
        task = by_id.get(tid)
        if task is None:
            continue
        entry = {
            "taskId": f"{tid}_{task.aircraft}",
            "soi": tid,
            "line_number": int(task.aircraft),
            "day": int(asn.day),
            "shift": int(asn.shift),
            "start_minute": int(asn.start_minute),
            "end_minute": int(asn.end_minute),
            "duration_minutes": int(asn.end_minute) - int(asn.start_minute),
            "team": asn.team,
            "skill": asn.skill,
        }
        for mid in asn.mechanic_ids:  # FULL crew (OR-1: every seat is real)
            mech_timelines.setdefault(bems_map[mid], []).append(dict(entry))
    for entries in mech_timelines.values():
        entries.sort(key=lambda e: (e["day"], e["shift"], e["start_minute"], e["taskId"]))

    # ---- staffing_requirements + utilization ("{team}|S{s}|D{d}") --------
    staffing_req: dict[str, int] = {}
    util_minutes: dict[str, int] = {}
    for t in tasks:
        if t["isFallback"]:
            continue  # unstaffable work adds no real staffing demand row
        key = f"{t['team']}|S{t['shift']}|D{t['day']}"
        staffing_req[key] = staffing_req.get(key, 0) + int(t["mechanics"])
        util_minutes[key] = util_minutes.get(key, 0) + (
            int(t["duration_minutes"]) * int(t["mechanics"])
        )
    utilization: dict[str, float] = {}
    for key, minutes in util_minutes.items():
        team, s_tag, _d_tag = key.split("|")
        shift = int(s_tag[1:])
        heads = int(
            (team_caps.get(team, {}).get("mechanics_by_shift") or {}).get(
                str(shift), 0
            )
        )
        denom = heads * SHIFT_PAID.get(shift, 480)
        if denom > 0:
            utilization[key] = round(minutes / denom, 4)

    # ---- summary ----------------------------------------------------------
    scheduled_n = len(schedule.assignments)
    unscheduled_n = len(schedule.unscheduled)
    total_overtime = sum(
        max(0, int(a.end_minute) - config.SHIFT_EFFECTIVE.get(int(a.shift), 460))
        for a in schedule.assignments.values()
    )
    summary = {
        "total_tasks": len(tasks),
        "scheduled_tasks": scheduled_n,
        "unscheduled_tasks": unscheduled_n,
        "success_rate": (
            scheduled_n / (scheduled_n + unscheduled_n)
            if (scheduled_n + unscheduled_n)
            else 0.0
        ),
        "num_aircraft": len(lines_with_tasks),
        "num_segments": 0,
        "num_inspections": sum(1 for t in tasks if t["is_inspection"]),
        "reference_date": REFERENCE_DATE.isoformat(),
        "run_timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "shift_identifier": f"S{shift_number}",
        "total_overtime_minutes": total_overtime,
        "makespan_day": makespan,
        "critical_task_count": sum(1 for t in tasks if t.get("isCritical")),
        "has_cpm_data": bool(cpm),
    }

    # ---- metadata.stats (the vendored insight blueprints' feed) ----------
    today_day = int((stats.get("economics") or {}).get("today_day", 0) or 0)
    meta_stats = {
        "economics": _economics_block(stats),
        "capacity_pressure": _capacity_block(stats, tasks),
        "projection": _projection_block(fleet, schedule, commitments_block, run_id),
        "final_lateness": stats.get("fleet_lateness_days"),
        "otd": _otd_block(stats),
        "commitment_kept": stats.get("commitment_kept", 0),
        "commitment_in_horizon": stats.get("commitment_in_horizon", 0),
        "commitment_mech_kept": stats.get("commitment_mech_kept", 0),
        "scheduled": scheduled_n,
        "unscheduled": unscheduled_n,
        "total_tasks": stats.get("total_tasks"),
        "otd_count": stats.get("otd_count"),
        "makespan_day": stats.get("makespan_day"),
        "wall_seconds": stats.get("wall_seconds"),
        "mock_data": bool(
            fleet.meta.get("mock_data", True) if isinstance(fleet.meta, dict) else True
        ),
    }

    horizon = max(makespan, fallback_day if unscheduled_n else makespan)
    working_days = [d for d in range(horizon + 1) if is_working_day(d)]

    return {
        "scenario_id": SCHEDULE_LABEL,
        "name": ENVELOPE_NAME,
        "metadata": {
            "soi_filename": "",
            "reference_date": REFERENCE_DATE.isoformat(),
            "schedule_label": SCHEDULE_LABEL,
            "schema_version": 2,
            "mock_data": meta_stats["mock_data"],
            "generated_at": summary["run_timestamp"],
            "snapshot_id": run_id,
            "stats": meta_stats,
        },
        "tasks": tasks,
        "mechanic_timelines": mech_timelines,  # dict => is_max_envelope
        "staffing_requirements": staffing_req,
        "aircraft_status": aircraft_status,
        "summary": summary,
        "teamCapacities": team_caps,
        "teamMetadata": {team: {"name": team} for team in sorted(team_caps)},
        "products": products,
        "utilization": utilization,
        "totalWorkforce": sum(c["total_mechanics"] for c in team_caps.values()),
        "makespan": makespan,
        "onTimeRate": (
            sum(1 for a in aircraft_status if a["on_time"])
            / max(1, len(aircraft_status))
        ),
        "avgUtilization": (
            round(sum(utilization.values()) / len(utilization), 4)
            if utilization
            else 0.0
        ),
        "otd": meta_stats["otd"],
        "predecessors_map": predecessors_map,
        "successors_map": successors_map,
        "shift_number": int(shift_number),
        "work_day": _day_date(today_day).isoformat(),
        "shift_label": config.SHIFT_LABELS.get(int(shift_number), "?"),
        "aircraft_line_numbers": sorted(lines_with_tasks),
        "workingDays": working_days,
        "todaySlot": today_day * 3 + (int(shift_number) - 1),
        # FF ride-along: BEMS -> FF mech id (Part-C write-back translation).
        "ff_mechanic_map": {v: k for k, v in bems_map.items()},
    }


def export_envelope(
    fleet: Fleet,
    schedule: Schedule,
    cpm: dict,
    snapshot: dict | None = None,
    out_dir: str | Path = "outputs/schedules",
    shift_number: int = 1,
    now: datetime | None = None,
    commitments_block: dict | None = None,
) -> Path:
    """Build + atomically write the envelope; return the written path.

    Filename: ``max_v1_{YYYYMMDD}_S{shift}_{HHMMSS}_UTC.json.gz`` — matches
    the vendored dashboard's first-class discovery glob (``max_v1_*``);
    discovery itself is by file MTIME, newest first (owner rule — never
    re-sorted by name/metadata). Atomic tmp+replace + deterministic bytes
    via ff.data.loader.save_json_gz.
    """
    now = now or datetime.now(timezone.utc)
    envelope = build_envelope(
        fleet,
        schedule,
        cpm,
        snapshot=snapshot,
        shift_number=shift_number,
        now=now,
        commitments_block=commitments_block,
    )
    out_dir = Path(out_dir)
    stamp = now.strftime(f"%Y%m%d_S{int(shift_number)}_%H%M%S_UTC")
    out_path = out_dir / f"max_v1_{stamp}.json.gz"
    loader.save_json_gz(envelope, str(out_path))
    return out_path
