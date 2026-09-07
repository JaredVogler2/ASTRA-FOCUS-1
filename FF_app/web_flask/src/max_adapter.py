"""MAX (Fable) envelope adapter — normalizes the schedule JSON the MAX
4-stage engine exports (max/export/dashboard_export.py) into the FGI
envelope shape this Flask dashboard was built against
(dashboard_3stage_export.py's export_standalone_schedule format).

    MAX                                     FGI (this dashboard)
    ---                                     --------------------
    mechanic_timelines: dict                mechanic_timelines: list of pools
    teamCapacities[team]:                   teamCapacities:
      {total_mechanics,                       {"TEAM S1 (ANY)": count}
       mechanics_by_shift, skills}
    utilization: {"TEAM|S1|D5": ratio}      utilization: {"TEAM S1 (ANY)":
                                              {pct, work, capacity}}
    products: {line_number, name,           products: FGI delivery cards
      task_count, completion_iso,             (onTime, latenessDays, ...)
      completion_day, deadline_iso}
    aircraft_status: {completion_day,       aircraft_status: {days_late,
      deadline_day, lateness_days,            cs_744_latest, completion_pct,
      on_time}                                status}
    tasks: BEMS crews, cpm_slack,           tasks: priority/priority_score,
      cpm_priority, state, deadlineDay        teamSkill "T S1 (ANY)", ...

Adaptation happens once at load time, so the on-disk format stays exactly
what the MAX engine writes (max_v1_*.json.gz) and richer MAX-only fields
(mechanicIds, cpm_slack, state, stage stats) ride along on every task.
The adapter is idempotent: FGI-shaped envelopes pass through untouched.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import src.paths  # noqa: F401  — anchors ROOT/SCHEDULES_DIR path config

# Shift parameters — VENDOR CHANGE (see web_flask/VENDOR_CHANGES.md):
# upstream imported these from max.core.config; this vendored copy has no
# MAX engine tree, so the identical values come from the local shim.
from src.ff_constants import (
    DAY_WORK_MINUTES, SHIFT_EFFECTIVE, SHIFT_MAX, SHIFT_PAID,
)

# FGI wall-clock shift starts (hours from midnight) used for the
# midnight-crossing completion-day bump on product cards.
_SHIFT_START_HOURS = {1: 6 + 10 / 60, 2: 14 + 40 / 60, 3: 23 + 10 / 60}

# Minute-level frame for the product cards' absolute-minute fields.
# The FGI export used a 2-shift 920-min frame, but MAX schedules 3rd-shift
# work, whose offset would overflow a 2-shift day and manufacture phantom
# lateness — so this adapter uses the engine's full 3-shift working day
# (S1+S2+S3 effective minutes) with per-shift offsets.
_SHIFT_OFFSET = {
    1: 0,
    2: SHIFT_EFFECTIVE[1],
    3: SHIFT_EFFECTIVE[1] + SHIFT_EFFECTIVE[2],
}
_MINUTES_PER_DAY = DAY_WORK_MINUTES

_TYPE_MAP = {"Inspection": "Quality Inspection"}


def is_max_envelope(env: Dict) -> bool:
    """MAX envelopes carry mechanic_timelines as a {mechanic_id: [...]} dict;
    FGI envelopes carry a list of pool objects."""
    return isinstance(env, dict) and isinstance(env.get("mechanic_timelines"), dict)


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _adapt_tasks(env: Dict) -> List[Dict]:
    tasks = env.get("tasks") or []
    successors = env.get("successors_map") or {}

    # Global priority rank: most critical first (highest CPM priority,
    # then least slack). Matches the FGI export's 1 = highest convention.
    def _rank_key(t):
        prio = t.get("cpm_priority")
        slack = t.get("cpm_slack")
        return (
            -(float(prio) if prio is not None else 0.0),
            float(slack) if slack is not None else float("inf"),
            t.get("taskId") or "",
        )

    for rank, t in enumerate(sorted(tasks, key=_rank_key), start=1):
        t["priority"] = rank

    for t in tasks:
        team = t.get("team") or t.get("resource_team") or ""
        shift = int(t.get("shift") or 1)
        skill = t.get("skill") or "ANY"
        tid = t.get("taskId") or ""

        if t.get("type") in _TYPE_MAP:
            t["type"] = _TYPE_MAP[t["type"]]
        t["teamSkill"] = f"{team} S{shift} ({skill})"

        prio = t.get("cpm_priority")
        t.setdefault("priority_score",
                     round(float(prio), 4) if prio is not None else 0.0)
        t.setdefault("successor_count", len(successors.get(tid, [])))
        t.setdefault("slack_time", t.get("cpm_slack", 0.0))
        t.setdefault("delivery_deadline_day", t.get("deadlineDay"))
        t.setdefault("cs_start_urgency_days", None)
        t.setdefault("cs_end_urgency_days", None)
        deps = t.get("dependencies") or []
        t.setdefault("predecessor_readiness", f"{len(deps)}/{len(deps)}")
        is_fallback = bool(t.get("isFallback"))
        shortfall = int(t.get("crewShortfall") or 0)
        t.setdefault("requires_manual_review", is_fallback or shortfall > 0)
        t.setdefault("scheduling_mode", "fallback" if is_fallback else "optimal")

        end_min = int(t.get("end_minute") or 0)
        t.setdefault("overtime_amount",
                     max(0, end_min - SHIFT_EFFECTIVE.get(shift, 460)))
        t.setdefault("overtime_violation", end_min > SHIFT_MAX.get(shift, 520))
        t.setdefault("spans_multiple_shifts", False)
        t.setdefault("num_shifts_spanned", 1)
        t.setdefault("skill_mismatch", False)
        t.setdefault("required_skill", skill)
        t.setdefault("assigned_skill", skill)

    # The FGI contract carries an integer mechanic_id per task (the client
    # auto-assign sorts them numerically to build stable worker numbers).
    # MAX Stage 3 binds BEMS strings — map each pool's distinct BEMS ids to
    # stable 1-based integers and keep the originals under bems_id (and in
    # mechanicIds, untouched).
    pool_bems: Dict[str, set] = defaultdict(set)
    for t in tasks:
        mid = t.get("mechanic_id")
        if mid not in (None, ""):
            pool_bems[t["teamSkill"]].add(str(mid))
    pool_index = {
        pool: {b: i for i, b in enumerate(sorted(ids), start=1)}
        for pool, ids in pool_bems.items()
    }
    for t in tasks:
        mid = t.get("mechanic_id")
        if mid in (None, ""):
            continue
        t["bems_id"] = mid
        t["mechanic_id"] = pool_index[t["teamSkill"]][str(mid)]
    return tasks


def _adapt_mechanic_timelines(env: Dict) -> List[Dict]:
    """{mechanic_id: [entries]} -> FGI list of per-mechanic pool objects."""
    out = []
    for mid, entries in (env.get("mechanic_timelines") or {}).items():
        entries = entries or []
        first = entries[0] if entries else {}
        work = sum(float(e.get("duration_minutes") or 0) for e in entries)
        days = {int(e.get("day") or 0) for e in entries}
        shift = int(first.get("shift") or 1)
        capacity = max(1, len(days)) * SHIFT_PAID.get(shift, 480)
        out.append({
            "mechanic_id": str(mid),
            "team": first.get("team"),
            "skill": first.get("skill") or "ANY",
            "shift": shift,
            "task_count": len(entries),
            "total_workload": round(work, 1),
            "utilization": round(100.0 * work / capacity, 1) if capacity else 0.0,
            "tasks": [{
                "task_key": [e.get("soi"), e.get("line_number")],
                "soi": e.get("soi"),
                "line_number": e.get("line_number"),
                "start_minute": e.get("start_minute"),
                "end_minute": e.get("end_minute"),
                "duration": e.get("duration_minutes"),
            } for e in entries],
        })
    return out


def _adapt_team_capacities(env: Dict, tasks: List[Dict]) -> Dict[str, int]:
    """Per-pool worker counts as '{team} S{shift} ({skill})': headcount.

    Uses the roster headcount the MAX envelope already carries
    (teamCapacities[team].mechanics_by_shift), NOT distinct assigned
    mechanic ids: MAX Stage 3 synthesizes per-day BEMS crews, so distinct
    ids over a multi-month horizon count the same seat many times — and
    the dashboard renders one worker <option> per counted head, so
    seat-days instead of seats explodes the Team Lead assignment
    dropdowns (observed: 63,839 "workers" on a 93-aircraft plan whose
    roster is 926).

    Skill tag: the team's single skill when it has exactly one, else ANY
    (the MAX envelope doesn't preserve the per-shift skill split)."""
    caps: Dict[str, int] = {}
    for team, rec in (env.get("teamCapacities") or {}).items():
        if not isinstance(rec, dict):
            continue
        skills = [s for s, n in (rec.get("skills") or {}).items() if n]
        skill = skills[0] if len(skills) == 1 else "ANY"
        for shift, count in (rec.get("mechanics_by_shift") or {}).items():
            if int(count or 0) > 0:
                caps[f"{team} S{shift} ({skill})"] = int(count)
    # Ensure every scheduled pool exists even when the roster record is
    # missing (e.g. unlimited-capacity QA/customer pools).
    for t in tasks:
        caps.setdefault(t.get("teamSkill"), 1)
    return caps


def _adapt_team_metadata(tasks: List[Dict]) -> Dict[str, Dict]:
    team_types: Dict[str, Dict] = {}
    for t in tasks:
        team = t.get("team") or "UNKNOWN"
        if team in team_types:
            continue
        if t.get("is_customer_inspection") or t.get("isCustomerTask"):
            team_types[team] = {"resource_type": "customer"}
        elif t.get("is_inspection") or str(team).upper().startswith("QA-"):
            team_types[team] = {"resource_type": "quality"}
        elif t.get("is_unlimited_capacity") and "VENDOR" in str(team).upper():
            team_types[team] = {"resource_type": "vendor"}
        else:
            team_types[team] = {"resource_type": "mechanic"}
    return team_types


def _adapt_utilization(tasks: List[Dict]) -> Dict[str, Dict]:
    """Per-pool {pct, work, capacity} with the FGI counting rules:
    capacity = workers x shift-paid-minutes per working day;
    unlimited-capacity pools (QA/customer inspections — taken from the
    tasks' own is_unlimited_capacity flag, the exporter's verdict) use
    peak concurrency for the worker count."""
    shift_durations = SHIFT_PAID
    stats = defaultdict(lambda: defaultdict(lambda: {
        "work": 0.0, "ids": set(), "shift": 1, "events": []}))
    unlimited_pools = set()
    for t in tasks:
        key = t.get("teamSkill")
        day = int(t.get("day") or 0)
        sm = int(t.get("start_minute") or 0)
        em = int(t.get("end_minute") or sm)
        crew = max(1, int(t.get("mechanics") or 1))
        entry = stats[key][day]
        entry["work"] += (em - sm) * crew          # mechanic-minutes
        entry["shift"] = int(t.get("shift") or 1)
        # Count every bound crew member (Stage 3 binds full BEMS crews in
        # mechanicIds); mechanic_id alone would credit only the crew lead.
        ids = t.get("mechanicIds") or (
            [t["mechanic_id"]] if t.get("mechanic_id") not in (None, "") else [])
        entry["ids"].update(str(i) for i in ids)
        entry["events"].append((sm, crew))
        entry["events"].append((em, -crew))
        if t.get("is_unlimited_capacity"):
            unlimited_pools.add(key)

    utilization = {}
    for key, day_map in stats.items():
        total_work = total_cap = 0.0
        is_unlimited = key in unlimited_pools
        for _day, s in day_map.items():
            total_work += s["work"]
            if is_unlimited:
                current = peak = 0
                for _, delta in sorted(s["events"], key=lambda e: (e[0], e[1])):
                    current += delta
                    peak = max(peak, current)
                workers = max(1, peak)
            else:
                workers = max(1, len(s["ids"]))
            total_cap += workers * shift_durations.get(s["shift"], 480)
        utilization[key] = {
            "pct": round(100.0 * total_work / total_cap, 1) if total_cap else 0.0,
            "work": round(total_work, 1),
            "capacity": round(total_cap, 1),
        }
    return utilization


def _adapt_staffing_requirements(env: Dict, tasks: List[Dict],
                                 team_caps: Dict[str, int]) -> List[Dict]:
    """FGI staffing rows {team, shift, skill, current, required, gap,
    status} from the MAX plan: required = the pool's peak concurrent
    mechanic demand on its busiest day (sweep-line over the tasks,
    weighted by each task's crew size); current = roster headcount."""
    events = defaultdict(list)   # (team, shift, day) -> [(minute, +/-crew)]
    for t in tasks:
        if t.get("is_unlimited_capacity"):
            continue
        team = t.get("team") or "UNKNOWN"
        shift = int(t.get("shift") or 1)
        day = int(t.get("day") or 0)
        crew = max(1, int(t.get("mechanics") or 1))
        sm = int(t.get("start_minute") or 0)
        em = int(t.get("end_minute") or sm)
        events[(team, shift, day)].append((sm, crew))
        events[(team, shift, day)].append((em, -crew))

    required: Dict[tuple, int] = defaultdict(int)
    for (team, shift, _day), evs in events.items():
        current = peak = 0
        for _, delta in sorted(evs, key=lambda e: (e[0], e[1])):
            current += delta
            peak = max(peak, current)
        required[(team, shift)] = max(required[(team, shift)], peak)

    staffing = []
    for (team, shift), req in required.items():
        cur = 0
        for key, count in team_caps.items():
            if key.startswith(f"{team} S{shift} "):
                cur += count
        gap = cur - req
        staffing.append({
            "team": team,
            "shift": shift,
            "skill": "ANY",
            "current": cur,
            "required": req,
            "gap": gap,
            "status": ("surplus" if gap > 0
                       else "balanced" if gap == 0 else "shortage"),
        })
    staffing.sort(key=lambda x: x["gap"])
    return staffing


def _completion_day(line_tasks: List[Dict]) -> int:
    """Latest scheduled day with the FGI midnight-crossing bump (a 3rd-shift
    task ending past midnight belongs to the next calendar day)."""
    completion = 0
    for t in line_tasks:
        d = int(t.get("day") or 0)
        s = int(t.get("shift") or 1)
        em = int(t.get("end_minute") or t.get("start_minute") or 0)
        end_hour = _SHIFT_START_HOURS.get(s, 0) + em / 60.0
        completion = max(completion, d + 1 if end_hour >= 24 else d)
    return completion


def _adapt_products_and_status(env: Dict, tasks: List[Dict]):
    ref_date = _parse_date((env.get("metadata") or {}).get("reference_date"))
    by_line: Dict[int, List[Dict]] = defaultdict(list)
    for t in tasks:
        line = t.get("line_number")
        if line is not None:
            by_line[int(line)].append(t)

    max_status = {int(a.get("line_number")): a
                  for a in (env.get("aircraft_status") or [])
                  if a.get("line_number") is not None}
    max_products = {int(p.get("line_number")): p
                    for p in (env.get("products") or [])
                    if p.get("line_number") is not None}

    products, status_list = [], []
    today = date.today()
    for line, line_tasks in sorted(by_line.items()):
        m_stat = max_status.get(line, {})
        m_prod = max_products.get(line, {})
        deadline_day = m_stat.get("deadline_day")
        deadline_date = _parse_date(m_prod.get("deadline_iso"))
        completion_day = _completion_day(line_tasks)

        days_late = 0
        on_time = True
        if deadline_day is not None and completion_day > int(deadline_day):
            days_late = completion_day - int(deadline_day)
            on_time = False

        projected = (ref_date + timedelta(days=completion_day)) if ref_date else None
        customer_code = next(
            (t.get("customer_code") for t in line_tasks if t.get("customer_code")), "")

        production = rework = customer = inspection = vendor = 0
        for t in line_tasks:
            ttype = t.get("type", "")
            if t.get("isVendorTask") or ttype == "Vendor":
                vendor += 1
            elif t.get("is_customer_inspection") or t.get("isCustomerTask") or ttype == "Customer":
                customer += 1
            elif t.get("is_inspection") or t.get("isQualityTask") or ttype == "Quality Inspection":
                inspection += 1
            elif t.get("isReworkTask") or ttype == "Rework":
                rework += 1
            else:
                production += 1

        completion_abs = 0
        for t in line_tasks:
            shift = int(t.get("shift") or 1)
            end_abs = (int(t.get("day") or 0) * _MINUTES_PER_DAY
                       + _SHIFT_OFFSET.get(shift, 0)
                       + int(t.get("start_minute") or 0)
                       + int(t.get("duration_minutes") or 0))
            completion_abs = max(completion_abs, end_abs)
        deadline_abs = ((int(deadline_day) + 1) * _MINUTES_PER_DAY
                        if deadline_day is not None else None)
        # Guard on the day-level verdict: a task's overtime tail can poke
        # past its shift's frame slot, and minute arithmetic must never
        # call an on-time line late.
        lateness_minutes = (max(0, completion_abs - deadline_abs)
                            if deadline_abs is not None and not on_time else 0)
        if not on_time and lateness_minutes == 0:
            # Keep the minute figure consistent with the day-level verdict
            # (the midnight-crossing bump can make completion_day exceed
            # the deadline while the frame arithmetic does not).
            lateness_minutes = days_late * _MINUTES_PER_DAY
        lateness_hours = round(lateness_minutes / 60.0, 1)
        lateness_days_decimal = round(lateness_minutes / _MINUTES_PER_DAY, 1)
        if lateness_minutes == 0:
            lateness_display = "On time"
        elif lateness_hours < 24:
            lateness_display = f"{lateness_hours}h"
        else:
            lateness_display = f"{lateness_days_decimal}d"

        if deadline_day and int(deadline_day) > 0:
            progress = min(100, max(0, int(100 * completion_day / int(deadline_day))))
        elif line_tasks:
            progress = 50
        else:
            progress = 0

        max_raw_day = max((int(t.get("day") or 0) for t in line_tasks), default=0)
        products.append({
            "name": f"Line {line}",
            "line_number": line,
            "customer_code": customer_code,
            "onTime": on_time,
            "latenessDays": lateness_days_decimal if lateness_minutes else 0.0,
            "latenessDisplay": lateness_display,
            "latenessHours": lateness_hours,
            "totalTasks": len(line_tasks),
            "product": f"Line {line}",
            "deliveryDate": (deadline_date.isoformat() + "T12:00:00")
                            if deadline_date else None,
            "projectedCompletion": (projected.isoformat() + "T12:00:00")
                                   if projected else None,
            "daysRemaining": (projected - today).days if projected else None,
            "criticalPath": sum(1 for t in line_tasks
                                if int(t.get("day") or 0) == max_raw_day),
            "completionAbsMinutes": completion_abs,
            "deadlineAbsMinutes": deadline_abs,
            "latenessMinutes": lateness_minutes,
            "progress": progress,
            "productionCount": production,
            "reworkCount": rework,
            "customerCount": customer,
            "inspectionCount": inspection,
            "vendorCount": vendor,
            "latePartsCount": sum(1 for t in line_tasks if t.get("isLatePartTask")),
            "taskBreakdown": {
                "Production": production,
                "Rework": rework,
                "Quality Inspection": inspection,
                "Customer": customer,
                "Vendor": vendor,
            },
        })
        status_list.append({
            "line_number": line,
            "task_count": len(line_tasks),
            "days_late": days_late,
            "cs_744_latest": deadline_date.strftime("%Y-%m-%d")
                             if deadline_date else "Unknown",
            "completion_pct": 100.0,
            "status": ("critical" if days_late > 100 else
                       "high" if days_late > 50 else
                       "moderate" if days_late > 0 else "on_time"),
        })

    products.sort(key=lambda p: -p["latenessDays"])
    status_list.sort(key=lambda a: -a["days_late"])
    return products, status_list


def adapt_max_envelope(env: Dict) -> Dict:
    """Convert a MAX engine dashboard export into the FGI envelope in place."""
    tasks = _adapt_tasks(env)

    team_caps = _adapt_team_capacities(env, tasks)
    env["mechanic_timelines"] = _adapt_mechanic_timelines(env)
    env["staffing_requirements"] = _adapt_staffing_requirements(
        env, tasks, team_caps)
    env["teamCapacities"] = team_caps
    env["teamMetadata"] = _adapt_team_metadata(tasks)

    utilization = _adapt_utilization(tasks)
    env["utilization"] = utilization
    total_work = sum(u["work"] for u in utilization.values())
    total_cap = sum(u["capacity"] for u in utilization.values())
    env["avgUtilization"] = round(100.0 * total_work / total_cap, 1) if total_cap else 0.0
    # Roster headcount (seats), matching the FGI dashboard's semantics —
    # not the count of per-day synthetic BEMS crew ids.
    env["totalWorkforce"] = sum(team_caps.values())
    env["makespan"] = max((int(t.get("day") or 0) for t in tasks), default=0)

    products, aircraft_status = _adapt_products_and_status(env, tasks)
    if products:
        env["products"] = products
        env["aircraft_status"] = aircraft_status
        on_time = sum(1 for p in products if p["onTime"])
        env["onTimeRate"] = round(100.0 * on_time / len(products), 1)

    summary = env.get("summary") or {}
    rate = summary.get("success_rate")
    if isinstance(rate, (int, float)) and rate <= 1.0:
        summary["success_rate"] = round(rate * 100.0, 2)
    summary.setdefault("shift_identifier", f"S{env.get('shift_number', 1)}")
    env["summary"] = summary

    meta = env.setdefault("metadata", {})
    ref = _parse_date(meta.get("reference_date"))
    if ref:
        meta["reference_date"] = ref.strftime("%Y-%m-%d")
    # The MAX export builds ISO timestamps from the real reference date, so
    # display == data reference here (no forward-shifted display frame).
    meta.setdefault("display_reference_date", meta.get("reference_date"))
    meta.setdefault("generated_at", summary.get("run_timestamp"))
    meta.setdefault("shift_identifier", f"S{env.get('shift_number', 1)}")
    meta.setdefault("version", "1.0")
    meta["adapted_from"] = "max"

    name = env.get("name")
    if not name:
        env["name"] = "MAX Fable Schedule"
    elif name == meta.get("schedule_label"):
        env["name"] = f"MAX Fable Schedule ({name})"
    return env


def normalize_envelope(env: Optional[Dict]) -> Optional[Dict]:
    """Entry point: FGI envelopes pass through untouched; MAX envelopes
    are adapted."""
    if env is None:
        return None
    if is_max_envelope(env):
        return adapt_max_envelope(env)
    return env


# ----------------------------------------------------------------------
# Shared envelope loading — every dashboard read of a schedule file goes
# through here so normalization is applied uniformly, and repeated reads
# of the same file (e.g. per-keystroke task search) hit an mtime-keyed
# cache instead of re-gunzipping a multi-MB export.
# ----------------------------------------------------------------------

_ENVELOPE_CACHE: Dict[str, tuple] = {}
_ENVELOPE_CACHE_MAX = 4


def load_envelope(filepath) -> Optional[Dict]:
    """Load + normalize a schedule envelope (.json / .json.gz), cached by
    (path, mtime). Returns None if the file is unreadable."""
    import gzip
    import json
    import os

    path = str(filepath)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _ENVELOPE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                env = json.load(f)
        else:
            with open(path, "r", encoding="utf-8") as f:
                env = json.load(f)
    except Exception as exc:
        print(f"Error loading schedule from {path}: {exc}")
        return None
    env = normalize_envelope(env)
    if len(_ENVELOPE_CACHE) >= _ENVELOPE_CACHE_MAX:
        _ENVELOPE_CACHE.pop(next(iter(_ENVELOPE_CACHE)))
    _ENVELOPE_CACHE[path] = (mtime, env)
    return env
