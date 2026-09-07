"""Development Board API — design v3.2 §9 (P2, first increment).

Endpoints (all served from the ACTIVE schedule; every join keys on
BEMS ids, never the adapter's pool-local integer mechanic_id):

    GET  /api/development/board        pairs + cert blocks + ledger join
    POST /api/development/checkoff     lead check-off -> ojt_ledger.jsonl
    GET  /api/development/bench        bench depth per family
    GET  /api/development/free-blocks  per-person gaps from task intervals
    GET  /api/development/hours        direct/development/reserve/contingency

The OJT ledger is append-only JSONL beside the IE queue; the dashboard
is its SOLE writer (the engine only emits bookings in the envelope).
Read rule: latest ts per booking_id wins. Unknown/stale booking ids are
accepted with orphan=true — replans must never lose floor check-offs.
The cached envelope is never mutated; all board state is computed
per-request (envelope + ledger).
"""

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

import src.paths as paths
# VENDOR CHANGE (web_flask/VENDOR_CHANGES.md): max.core.config ->
# src.ff_constants local shim (identical values, no MAX engine tree here).
from src.ff_constants import SHIFT_EFFECTIVE, SHIFT_PAID

development_bp = Blueprint("development", __name__,
                           url_prefix="/api/development")

OJT_LEDGER_FILE = str(paths.WEBAPP_ROOT / "ojt_ledger.jsonl")
_LEDGER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def read_ledger():
    """Latest row per booking_id wins (ts ordering)."""
    rows = {}
    if not os.path.exists(OJT_LEDGER_FILE):
        return rows
    try:
        with open(OJT_LEDGER_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                      # torn trailing line: skip
                bid = r.get("booking_id")
                if not bid:
                    continue
                prev = rows.get(bid)
                if prev is None or str(r.get("ts", "")) >= str(prev.get("ts", "")):
                    rows[bid] = r
    except IOError:
        pass
    return rows


def append_ledger(row):
    with _LEDGER_LOCK:
        with open(OJT_LEDGER_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# lead overrides (design v3.2 §9): envelope ⊕ accepted-override overlay
# ---------------------------------------------------------------------------

OVERRIDES_FILE = str(paths.WEBAPP_ROOT / "assignment_overrides.jsonl")
_OVERRIDE_LOCK = threading.Lock()


def _schedule_ref():
    f = getattr(current_app, "current_schedule_file", None)
    return os.path.basename(f) if f else "unknown"


def read_overrides(schedule_ref):
    """Latest row per task_id for this schedule ref."""
    rows = {}
    if not os.path.exists(OVERRIDES_FILE):
        return rows
    try:
        with open(OVERRIDES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("schedule_ref") != schedule_ref:
                    continue
                tid = r.get("task_id")
                if not tid:
                    continue
                prev = rows.get(tid)
                if prev is None or str(r.get("ts", "")) >= str(prev.get("ts", "")):
                    rows[tid] = r
    except IOError:
        pass
    return rows


def overlay_crew(task, overrides):
    """Task's crew with any accepted override applied (never mutates)."""
    crew = list(task.get("mechanicIds") or [])
    ov = overrides.get(task.get("taskId"))
    if ov and ov.get("bems_out") in crew:
        crew = [ov["bems_in"] if m == ov["bems_out"] else m for m in crew]
    return crew


def _token(schedule_ref, overrides):
    return f"{schedule_ref}|{len(overrides)}"


# ---------------------------------------------------------------------------
# envelope access
# ---------------------------------------------------------------------------

def _env():
    data = current_app.current_schedule_data \
        if getattr(current_app, "active_schedule", "current") == "current" \
        else getattr(current_app, "previous_schedule_data", None)
    return data or current_app.current_schedule_data or {}


def _bookings(env):
    return env.get("development_bookings") or []


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@development_bp.route("/board", methods=["GET"])
def board():
    env = _env()
    ledger = read_ledger()
    live_ids = set()
    pairs, cert_time = [], []
    for b in _bookings(env):
        bid = b.get("bookingId")
        live_ids.add(bid)
        row = dict(b)
        row["ledger"] = ledger.get(bid)
        (pairs if b.get("role") == "shadow" else cert_time).append(row)
    orphaned = [r for bid, r in sorted(ledger.items())
                if bid not in live_ids]
    week_caps = defaultdict(int)
    for b in pairs:
        if b.get("mentorBemsId"):
            week_caps[b["mentorBemsId"]] += 1
    meta = env.get("metadata") or {}
    return jsonify({
        "pairs": pairs,
        "certTime": cert_time,
        "mentorLoad": dict(week_caps),
        "orphaned": orphaned,
        "developmentEffective": bool(meta.get("development_effective")),
        "developmentDegraded": bool(meta.get("development_degraded")),
        "mockData": bool(meta.get("mock_data")),
    })


@development_bp.route("/checkoff", methods=["POST"])
def checkoff():
    payload = request.get_json(silent=True) or {}
    bid = (payload.get("booking_id") or "").strip()
    status = (payload.get("status") or "").strip()
    lead = (payload.get("lead") or "").strip()
    if not bid or status not in ("completed", "skipped") or not lead:
        return jsonify({"error": "booking_id, status "
                        "(completed|skipped) and lead are required"}), 400
    env = _env()
    live = {b.get("bookingId"): b for b in _bookings(env)}
    b = live.get(bid)
    row = {
        "booking_id": bid,
        "status": status,
        "reason": (payload.get("reason") or "").strip(),
        "lead": lead,
        "ts": datetime.now(timezone.utc).isoformat(),
        "orphan": b is None,
        # denormalized so P3 credit conversion never parses ids or
        # scrapes rotated envelopes (design §6.2):
        "bems_id": (b or {}).get("bemsId"),
        "task_id": (b or {}).get("taskId"),
        "family": (b or {}).get("family"),
        "minutes": (b or {}).get("minutes"),
        "role": (b or {}).get("role"),
        "cert_req_id": (b or {}).get("certReqId"),
        "day": (b or {}).get("day"),
        "shift": (b or {}).get("shift"),
    }
    append_ledger(row)
    return jsonify({"ok": True, "orphan": row["orphan"]})


@development_bp.route("/bench", methods=["GET"])
def bench():
    env = _env()
    stats = (env.get("metadata") or {}).get("stats") or {}
    return jsonify({"benchDepth": stats.get("bench_depth") or {},
                    "familyMapVersion": (env.get("metadata") or {})
                    .get("family_map_version")})


@development_bp.route("/free-blocks", methods=["GET"])
def free_blocks():
    """Per-person gaps rebuilt from task intervals ∪ booking intervals
    (round-3 basis — never from mechanic_timelines)."""
    env = _env()
    try:
        day = int(request.args.get("day"))
    except (TypeError, ValueError):
        return jsonify({"error": "day query param required"}), 400
    team = request.args.get("team")
    busy = defaultdict(list)
    for t in env.get("tasks") or []:
        if int(t.get("day") or -1) != day:
            continue
        if team and t.get("team") != team:
            continue
        s = int(t.get("shift") or 1)
        t0 = int(t.get("start_minute") or 0)
        t1 = int(t.get("end_minute") or t0)
        for mid in (t.get("mechanicIds") or []):
            busy[(str(mid), s)].append((t0, t1))
    for b in _bookings(env):
        if int(b.get("day") or -1) != day:
            continue
        busy[(str(b.get("bemsId")), int(b.get("shift") or 1))].append(
            (int(b.get("startMinute") or 0), int(b.get("endMinute") or 0)))
    out = []
    for (bems, s), iv in sorted(busy.items()):
        window = SHIFT_EFFECTIVE.get(s, 460)
        cur, gaps = 0, []
        for a0, a1 in sorted(iv):
            if a0 > cur:
                gaps.append([cur, min(a0, window)])
            cur = max(cur, a1)
        if cur < window:
            gaps.append([cur, window])
        out.append({"bemsId": bems, "shift": s,
                    "freeBlocks": [g for g in gaps if g[1] - g[0] > 0]})
    return jsonify({"day": day, "people": out})


@development_bp.route("/hours", methods=["GET"])
def hours():
    """direct / development / reserve / contingency per (team, shift)
    for one day (design §9 formulas, round-3 pinned)."""
    env = _env()
    try:
        day = int(request.args.get("day"))
    except (TypeError, ValueError):
        return jsonify({"error": "day query param required"}), 400
    caps = env.get("teamCapacities") or {}
    direct = defaultdict(int)
    for t in env.get("tasks") or []:
        if int(t.get("day") or -1) != day:
            continue
        s = int(t.get("shift") or 1)
        mm = t.get("memberMinutes") or {}
        dur = int(t.get("duration_minutes") or t.get("duration") or 0)
        for mid in (t.get("mechanicIds") or []):
            direct[(t.get("team"), s)] += int(mm.get(mid, dur))
    dev = defaultdict(int)
    for b in _bookings(env):
        if int(b.get("day") or -1) != day:
            continue
        team = str(b.get("bemsId") or "").split("-S")[0]
        if team.startswith("DEMO-"):
            team = team[len("DEMO-"):]
        dev[(team, int(b.get("shift") or 1))] += int(b.get("minutes") or 0)
    rows = []
    for tskill, count in sorted(caps.items()):
        if not isinstance(count, (int, float)):
            continue                             # adapted flat form only
        # adapted key: "TEAM S{n} (SKILL)"
        try:
            team, rest = tskill.rsplit(" S", 1)
            shift = int(rest.split(" ")[0])
        except (ValueError, IndexError):
            continue
        capacity = int(count) * SHIFT_PAID.get(shift, 480)
        contingency = int(round(0.15 * capacity))
        d = direct.get((team, shift), 0)
        v = dev.get((team, shift), 0)
        rows.append({"team": team, "shift": shift, "pool": tskill,
                     "capacity": capacity, "contingency": contingency,
                     "direct": d, "development": v,
                     "reserve": max(0, capacity - contingency - d - v)})
    return jsonify({"day": day, "rows": rows})


# ---------------------------------------------------------------------------
# swap candidates + overrides (design v3.2 §9, one-sided reassign v1)
# ---------------------------------------------------------------------------

_BAND_ORDER = {"apprentice": 0, "competent": 1, "expert": 2}


def _band_of(entry, family):
    """Roster-block banding: thresholds on eByFamily; any-history ->
    apprentice on untouched families; zero-history -> competent
    (protective data-gap default, mirrors the engine)."""
    e = (entry.get("eByFamily") or {}).get(family)
    if e is not None:
        ev = e / 100.0
        if ev >= 10.0:
            return "expert"
        if ev >= 3.0:
            return "competent"
        return "apprentice"
    return "apprentice" if entry.get("eByFamily") else "competent"


def _basis(env, overrides, day):
    """busy/load per (bems, shift) for one day, from overlay crews ∪
    bookings; memberMinutes-aware (single accounting source per id)."""
    busy, load = defaultdict(list), defaultdict(int)
    seq = defaultdict(list)
    for t in env.get("tasks") or []:
        if int(t.get("day") or -1) != day:
            continue
        sft = int(t.get("shift") or 1)
        t0 = int(t.get("start_minute") or 0)
        t1 = int(t.get("end_minute") or t0)
        mm = t.get("memberMinutes") or {}
        dur = int(t.get("duration_minutes") or t.get("duration") or 0)
        for mid in overlay_crew(t, overrides):
            busy[(mid, sft)].append((t0, t1, t.get("taskId")))
            load[(mid, sft)] += int(mm.get(mid, dur))
            seq[mid].append((sft, t0, t.get("line_number")))
    for b in env.get("development_bookings") or []:
        if int(b.get("day") or -1) != day:
            continue
        mid, sft = str(b.get("bemsId")), int(b.get("shift") or 1)
        busy[(mid, sft)].append((int(b.get("startMinute") or 0),
                                 int(b.get("endMinute") or 0),
                                 b.get("bookingId")))
        load[(mid, sft)] += int(b.get("minutes") or 0)
    return busy, load, seq


def _changeover_delta(seq, bems_out, bems_in, task):
    def count(entries):
        entries = sorted(entries)
        return sum(1 for i in range(1, len(entries))
                   if entries[i][2] != entries[i - 1][2])
    key = (int(task.get("shift") or 1), int(task.get("start_minute") or 0),
           task.get("line_number"))
    out_pre = list(seq.get(bems_out, []))
    in_pre = list(seq.get(bems_in, []))
    out_post = [e for e in out_pre if e != key]
    in_post = in_pre + [key]
    return (count(out_post) + count(in_post)) - (count(out_pre) + count(in_pre))


def _gate_candidate(entry, task, family, busy, load, cert_by_family,
                    teams_with_certs, dev_members):
    """Returns None if legal, else the failing-gate reason."""
    day = int(task.get("day") or 0)
    sft = int(task.get("shift") or 1)
    if entry.get("shift") != sft:
        return "different shift"
    if day in set(entry.get("leaveDays") or []):
        return "on leave that day"
    skill = (task.get("skill") or "ANY").upper()
    if skill not in ("ANY", "NO_SKILL", "") and             skill not in (entry.get("skills") or []):
        return f"lacks skill {skill}"
    cert = (cert_by_family or {}).get(family)
    if cert and entry.get("team") in teams_with_certs and             cert not in (entry.get("certs") or []):
        return f"lacks cert {cert}"
    if entry["bemsId"] in dev_members:
        return "development member of this task"
    t0 = int(task.get("start_minute") or 0)
    t1 = int(task.get("end_minute") or t0)
    for b0, b1, _ref in busy.get((entry["bemsId"], sft), []):
        if t0 < b1 and b0 < t1:
            return "interval conflict (C15/booking)"
    mm = task.get("memberMinutes") or {}
    dur = int(task.get("duration_minutes") or task.get("duration") or 0)
    cap = SHIFT_EFFECTIVE.get(sft, 460) + 60
    if load.get((entry["bemsId"], sft), 0) + int(mm.get(entry["bemsId"], dur)) > cap:
        return "would exceed remaining shift minutes (C18)"
    if task.get("devCritical") and _band_of(entry, family) == "apprentice":
        return "apprentice on critical task (C23)"
    return None


@development_bp.route("/swap-candidates", methods=["GET"])
def swap_candidates():
    env = _env()
    tid = request.args.get("task")
    if not tid:
        return jsonify({"error": "task query param required"}), 400
    task = next((t for t in env.get("tasks") or []
                 if t.get("taskId") == tid), None)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    ref = _schedule_ref()
    overrides = read_overrides(ref)
    token = _token(ref, overrides)
    crew = overlay_crew(task, overrides)
    base = {"token": token, "taskId": tid, "candidates": [], "excluded": []}
    if task.get("isFallback") or task.get("is_unlimited_capacity"):
        base["reason"] = "fallback/unlimited assignments are not swappable"
        return jsonify(base)
    if task.get("is_duration_segment"):
        base["reason"] = "duration segments are excluded from overrides (v1)"
        return jsonify(base)
    prior = task.get("priorMechanicId") or task.get("prior_mechanic_id")
    if (prior and prior in crew) or task.get("state") == "in_progress":
        base["reason"] = "mid-flow task — continuity is absolute"
        return jsonify(base)
    replaces = request.args.get("replaces") or (crew[0] if crew else None)
    if not replaces or replaces not in crew:
        base["reason"] = "no replaceable crew member"
        return jsonify(base)
    roster = env.get("roster") or []
    if not roster:
        base["reason"] = "envelope carries no roster block (schema v2 required)"
        return jsonify(base)
    family = task.get("family")
    cert_by_family = env.get("certByFamily") or {}
    teams_with_certs = {r["team"] for r in roster if r.get("certs")}
    dev_members = {e.get("bemsId") for e in (task.get("development") or [])}
    day = int(task.get("day") or 0)
    busy, load, seq = _basis(env, overrides, day)
    for entry in roster:
        bems = entry["bemsId"]
        if bems in crew or entry.get("team") != task.get("team"):
            continue
        reason = _gate_candidate(entry, task, family, busy, load,
                                 cert_by_family, teams_with_certs,
                                 dev_members)
        if reason:
            base["excluded"].append({"bemsId": bems, "reason": reason})
            continue
        sft = int(task.get("shift") or 1)
        cap = SHIFT_EFFECTIVE.get(sft, 460) + 60
        dur = int(task.get("duration_minutes") or task.get("duration") or 0)
        base["candidates"].append({
            "bemsId": bems,
            "replacesBemsId": replaces,
            "band": _band_of(entry, family),
            "kind": "reassign",
            "changeoverDelta": _changeover_delta(seq, replaces, bems, task),
            "netMinutes": cap - load.get((bems, sft), 0) - dur,
        })
    base["candidates"].sort(
        key=lambda c: (-_BAND_ORDER.get(c["band"], 0), -c["netMinutes"],
                       c["bemsId"]))
    base["excluded"] = base["excluded"][:25]
    return jsonify(base)


@development_bp.route("/override", methods=["POST"])
def override():
    payload = request.get_json(silent=True) or {}
    tid = (payload.get("task_id") or "").strip()
    bems_in = (payload.get("bems_in") or "").strip()
    bems_out = (payload.get("bems_out") or "").strip()
    lead = (payload.get("lead") or "").strip()
    token = (payload.get("token") or "").strip()
    if not all((tid, bems_in, bems_out, lead, token)):
        return jsonify({"error": "task_id, bems_in, bems_out, lead and "
                        "token are required"}), 400
    env = _env()
    with _OVERRIDE_LOCK:
        ref = _schedule_ref()
        overrides = read_overrides(ref)
        if token != _token(ref, overrides):
            return jsonify({"error": "stale board — refresh candidates",
                            "token": _token(ref, overrides)}), 409
        task = next((t for t in env.get("tasks") or []
                     if t.get("taskId") == tid), None)
        if task is None:
            return jsonify({"error": "unknown task"}), 404
        crew = overlay_crew(task, overrides)
        if bems_out not in crew:
            return jsonify({"error": f"{bems_out} is not on this task"}), 409
        roster = {r["bemsId"]: r for r in env.get("roster") or []}
        entry = roster.get(bems_in)
        if entry is None:
            return jsonify({"error": f"{bems_in} not in roster"}), 409
        cert_by_family = env.get("certByFamily") or {}
        teams_with_certs = {r["team"] for r in roster.values()
                            if r.get("certs")}
        dev_members = {e.get("bemsId")
                       for e in (task.get("development") or [])}
        busy, load, _seq = _basis(env, overrides, int(task.get("day") or 0))
        reason = _gate_candidate(entry, task, task.get("family"), busy,
                                 load, cert_by_family, teams_with_certs,
                                 dev_members)
        if reason:
            return jsonify({"error": f"no longer legal: {reason}",
                            "token": _token(ref, overrides)}), 409
        row = {"task_id": tid, "bems_in": bems_in, "bems_out": bems_out,
               "lead": lead, "ts": datetime.now(timezone.utc).isoformat(),
               "schedule_ref": ref, "source": "lead"}
        with open(OVERRIDES_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
        overrides[tid] = row
        return jsonify({"ok": True, "token": _token(ref, overrides)})
