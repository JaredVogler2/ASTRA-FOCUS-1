"""ff.web.api — the /api/v1 blueprint (contract §ff/web + INCREMENT 2).

Routes: GET /state, GET /tasks/<id> (+feasibility), GET /candidates,
GET /explain/candidates (the ONE LLM surface — read-only, LB-1/LB-5,
deterministic-always with validated narrative garnish, LB-8),
GET /economics, GET /capacity, GET /points/shift, GET /points/leaderboard,
GET /progression/team/<team>, GET /recap, GET /progression/events
(GAMES §10 — ALL read-only, GG-1), POST /actuals, POST /replan,
POST+GET /excusals.

Laws enforced here:

- RS scope rules on EVERY route: the session scope resolved by
  ``ff.web.app.resolve_scope`` is applied server-side — a mechanic sees own
  tasks, lead/flm their team, super their team group, director/vp all.
  A tampered ``?team=`` naming an unauthorized team returns 403.
- OR-6 SINGLE WRITE-PATH: ``POST /actuals`` is the ONLY code path in the
  whole application that mutates task live-state; it updates the in-memory
  fleet, persists ``data/actuals.json`` atomically, and appends to the
  in-memory audit list. Nothing else writes. ``POST /excusals`` is the
  OR-6-ADJACENT, FOCU5-local capture path: it never touches task state —
  it only appends lead-attested disruption records (role lead+, own team,
  notes required for SAME_TEAM_PREDECESSOR / DURATION_OVERRUN) to
  ``data/excusals.json`` atomically.
- OR-5 honesty: economics/capacity payloads pass through their
  ``source: config-defaults`` placeholder labels untouched.
- Single-flight replan: ``POST /replan`` takes a non-blocking
  ``threading.Lock``; a concurrent replan gets 409, never a second engine
  run in parallel.
- JSON error envelope ``{"error": ..., "code": ...}`` on every failure.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

import config
from ff.domain import TASK_STATES

bp = Blueprint("api", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# error envelope
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """Raise anywhere in a route to emit the {error, code} JSON envelope."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


@bp.errorhandler(ApiError)
def _handle_api_error(err: ApiError):
    """Contract: JSON errors are always ``{"error", "code"}``."""
    return jsonify({"error": err.message, "code": err.code}), err.status


@bp.errorhandler(Exception)
def _handle_unexpected(err: Exception):
    """Last-resort JSON envelope — an API route never emits an HTML 500."""
    return (
        jsonify({"error": f"{type(err).__name__}: {err}", "code": "internal_error"}),
        500,
    )


# ---------------------------------------------------------------------------
# shared route plumbing
# ---------------------------------------------------------------------------


def _webapp():
    """Late import of ff.web.app helpers (avoids the module import cycle:
    app.py imports this blueprint at factory time)."""
    from ff.web import app as webapp

    return webapp


def _state() -> dict:
    return current_app.extensions["ff"]


def _auth() -> tuple[dict, dict]:
    """Resolve (state, scope) for the request; 401 when not logged in.

    RS rule: every API route calls this FIRST — scope comes from the
    server-side session only, never from request parameters.
    """
    state = _state()
    sc = _webapp().resolve_scope(state)
    if sc is None:
        raise ApiError(401, "unauthenticated", "login required (POST /login)")
    return state, sc


def _require_ready(state: dict) -> None:
    """503 until the boot/replan pipeline has built a snapshot (readyz law)."""
    if state.get("snapshot") is None:
        raise ApiError(
            503,
            "not_ready",
            f"snapshot not built: {state.get('boot_error') or 'still initializing'}",
        )


def _svc_or_503(name: str):
    """Import a service module or fail HONESTLY with 503 (never fake data)."""
    mod, err = _webapp()._svc(name)
    if mod is None:
        raise ApiError(503, "service_unavailable", f"ff.services.{name}: {err}")
    return mod


def _call_or_500(code: str, fn, *args, **kwargs):
    """Call a service; convert its exception to a labeled 500 envelope."""
    value, err = _webapp()._call(fn, *args, **kwargs)
    if err:
        raise ApiError(500, code, err)
    return value


def _int_arg(name: str, default, lo: int, hi: int):
    """Parse an int query param with bounds; 400 on garbage or out-of-range."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "invalid_param", f"{name!r} must be an integer")
    if not (lo <= value <= hi):
        raise ApiError(400, "invalid_param", f"{name!r} must be in [{lo}, {hi}]")
    return value


def _team_arg(state: dict, sc: dict, required_for_all_scope: bool = False):
    """Resolve+authorize the ``?team=`` param (THE tamper check, RS rules).

    - scoped roles default to their (first sorted) allowed team;
    - an explicit team outside the caller's scope -> 403;
    - an explicit team that does not exist -> 404.
    """
    webapp = _webapp()
    team = request.args.get("team", "") or None
    if team is None:
        if sc["teams"] is not None:
            return sorted(sc["teams"])[0]
        if required_for_all_scope:
            raise ApiError(400, "invalid_param", "'team' is required for this role")
        return None
    if team not in state.get("teams", []):
        raise ApiError(404, "unknown_team", f"team {team!r} does not exist")
    if not webapp.team_allowed(sc, team):
        raise ApiError(
            403, "forbidden_scope", f"team {team!r} is outside your scope"
        )
    return team


def _check_task_scope(state: dict, sc: dict, task) -> None:
    """Task-level scope check (RS rules): mechanic = own tasks ONLY (they are
    on the assignment's named crew); lead/flm/super = team(s); director/vp
    = all. 403 outside scope."""
    if sc["role"] == "mechanic":
        schedule = state.get("schedule")
        asg = schedule.assignments.get(task.task_id) if schedule else None
        if asg is None or sc["mech_id"] not in asg.mechanic_ids:
            raise ApiError(
                403,
                "forbidden_scope",
                "mechanic scope: only tasks on your own crew are visible",
            )
        return
    if sc["teams"] is not None and task.team not in sc["teams"]:
        raise ApiError(
            403, "forbidden_scope", f"task team {task.team!r} is outside your scope"
        )


# ---------------------------------------------------------------------------
# read routes
# ---------------------------------------------------------------------------


@bp.get("/state")
def api_state():
    """Snapshot identity + freshness + SCOPED counts for the caller."""
    state, sc = _auth()
    schedule = state.get("schedule")
    teams = sc["teams"]
    counts = {"tasks": 0, "done": 0, "in_progress": 0, "blocked": 0,
              "scheduled": 0, "unscheduled": 0}
    if state.get("fleet") is not None:
        for task in state["fleet"].tasks:
            if teams is not None and task.team not in teams:
                continue
            counts["tasks"] += 1
            if task.state in ("done", "in_progress", "blocked"):
                counts[task.state] += 1
        if schedule is not None:
            by_id = state["task_by_id"]
            counts["scheduled"] = sum(
                1
                for tid in schedule.assignments
                if teams is None
                or (by_id.get(tid) is not None and by_id[tid].team in teams)
            )
            counts["unscheduled"] = sum(
                1
                for tid in schedule.unscheduled
                if teams is None
                or (by_id.get(tid) is not None and by_id[tid].team in teams)
            )
    if sc["role"] == "mechanic" and schedule is not None:
        counts["my_assignments"] = sum(
            1
            for tid in schedule.assignments
            if sc["mech_id"] in schedule.assignments[tid].mechanic_ids
        )
    return jsonify(
        {
            "ok": True,
            "ready": state.get("snapshot") is not None,
            "snapshot_id": state.get("snapshot_id"),
            "built_at": state.get("built_at"),
            "build_count": state.get("build_count"),
            "mock_data": bool(state.get("mock_data", False)),
            "role": sc["role"],
            "scope": sc["scope"],
            "teams": sorted(teams) if teams is not None else None,
            "counts": counts,
            "validator_total": state.get("validator_total"),
        }
    )


@bp.get("/tasks/<task_id>")
def api_task(task_id: str):
    """One task + assignment/unscheduled status + feasibility (+cpm, +score)."""
    state, sc = _auth()
    _require_ready(state)
    task = state["task_by_id"].get(task_id)
    if task is None:
        raise ApiError(404, "unknown_task", f"task {task_id!r} does not exist")
    _check_task_scope(state, sc, task)
    webapp = _webapp()
    schedule = state["schedule"]
    asg = schedule.assignments.get(task_id)
    feas_mod = _svc_or_503("feasibility")
    feasibility = webapp._as_plain(
        _call_or_500("feasibility_error", feas_mod.evaluate, task_id, state["snapshot"])
    )
    score = None
    points_mod, _perr = webapp._svc("points")
    if points_mod is not None:
        value, err = webapp._call(points_mod.score_task, task_id, state["snapshot"])
        if not err:
            score = webapp._as_plain(value)
    return jsonify(
        {
            "task": task.to_dict(),
            "assignment": asg.to_dict() if asg is not None else None,
            "unscheduled_reason": schedule.unscheduled.get(task_id),
            "feasibility": feasibility,
            "cpm": webapp._as_plain(state.get("cpm", {}).get(task_id)),
            "score": score,
        }
    )


def _scoped_candidates(state: dict, sc: dict, team, shift, limit: int) -> list[dict]:
    """Fetch ranked candidates and re-filter them server-side (RS rules).

    ONE implementation shared by ``GET /candidates`` and
    ``GET /explain/candidates`` so the presented candidate set — the
    LB-3 whitelist — can never drift between the two surfaces. Defense in
    depth: results are re-filtered by the tasks' real teams; nothing
    outside the caller's scope leaves the API.
    """
    webapp = _webapp()
    cand_mod = _svc_or_503("candidates")
    scope_dict = {
        "role": sc["role"],
        "scope": sc["scope"],
        "team": team,
        "teams": [team] if team is not None
        else (sorted(sc["teams"]) if sc["teams"] is not None else None),
        "shift": shift,
        "mech_id": sc["mech_id"],
    }
    ranked = _call_or_500(
        "candidates_error", cand_mod.rank, state["snapshot"], scope_dict, limit
    )
    rows = []
    for cand in ranked or []:
        plain = webapp._as_plain(cand) or {}
        task = state["task_by_id"].get(plain.get("task_id", ""))
        if task is None:
            continue
        if team is not None and task.team != team:
            continue
        if not webapp.team_allowed(sc, task.team):
            continue  # defense in depth: nothing outside scope leaves the API
        if shift is not None:
            asg = state["schedule"].assignments.get(task.task_id)
            if asg is not None and asg.shift != shift:
                continue
        rows.append(plain)
        if len(rows) >= limit:
            break
    return rows


@bp.get("/candidates")
def api_candidates():
    """Ranked next-best candidates for a team, scope-checked and re-filtered.

    RS tamper check: ``?team=`` outside the caller's scope -> 403; results
    are additionally filtered server-side by the tasks' real teams.
    """
    state, sc = _auth()
    _require_ready(state)
    team = _team_arg(state, sc)  # None only for director/vp asking fleet-wide
    shift = _int_arg("shift", None, 1, 3)
    limit = _int_arg("limit", 25, 1, 100)
    rows = _scoped_candidates(state, sc, team, shift, limit)
    return jsonify({"team": team, "shift": shift, "count": len(rows),
                    "candidates": rows})


@bp.get("/explain/candidates")
def api_explain_candidates():
    """Candidate explanation — deterministic ALWAYS, LLM narrative if valid.

    THE one LLM surface in the application (LB-1/LB-5: read-only, no
    tools, no writes, no other route touches a provider). Behavior:

    - Builds the deterministic candidate explanation ALWAYS (the existing
      templated per-candidate explanations — this IS the LB-8 fallback).
    - IF a provider is configured (``state["llm_provider"]``, default
      MockLLMProvider in dev), renders the versioned
      ``explain_candidates`` template (LB-6 sanitized DATA block), calls
      ``complete_structured``, and runs the FULL validator pipeline
      (LB-3 whitelist / LB-4 grounding / LB-10 uncertainty).
    - The response marks ``source: 'deterministic' | 'llm-validated'``;
      ANY provider or validation failure degrades silently to
      deterministic (LB-8) — the HTTP status is 200 either way.
    - Every LLM call + verdict is appended to the audit log (LB-7).
    - RS rules: same team tamper check + server-side re-filter as
      ``GET /candidates`` (shared ``_scoped_candidates``).
    """
    state, sc = _auth()
    _require_ready(state)
    team = _team_arg(state, sc)
    shift = _int_arg("shift", None, 1, 3)
    limit = _int_arg("limit", 10, 1, 25)
    rows = _scoped_candidates(state, sc, team, shift, limit)

    # -- deterministic explanation: ALWAYS built, never skipped (LB-8) ------
    if rows:
        top = rows[0]
        summary = (
            f"{len(rows)} READY candidate(s) in scope for team {team}"
            + (f", shift {shift}" if shift is not None else "")
            + f"; top-ranked {top.get('task_id')} (score {top.get('score')})."
        )
    else:
        summary = (
            f"0 READY candidates in scope for team {team}"
            + (f", shift {shift}" if shift is not None else "")
            + "."
        )
    explanation = {
        "source": "deterministic",
        "summary": summary,
        "items": [
            {
                "candidate_id": r.get("task_id"),
                "rank": r.get("rank"),
                "explanation": r.get("explanation", ""),
            }
            for r in rows
        ],
    }

    # -- optional LLM garnish: validated or silently absent (LB-8) ----------
    provider = state.get("llm_provider")
    if provider is not None and rows:
        try:
            explanation = _attach_llm_narrative(state, team, shift, rows, explanation)
        except Exception:
            # LB-8 (cited law): LLM failure degrades gracefully — the
            # deterministic content above renders; nothing propagates.
            pass
    return jsonify(
        {
            "team": team,
            "shift": shift,
            "count": len(rows),
            "candidates": rows,
            "explanation": explanation,
            "snapshot_id": state.get("snapshot_id"),
            "mock_data": bool(state.get("mock_data", False)),  # OR-5
        }
    )


def _attach_llm_narrative(state: dict, team, shift, rows: list[dict],
                          explanation: dict) -> dict:
    """Render -> call -> validate -> audit; attach ONLY a fully-passed output.

    LB-3: the whitelist is EXACTLY the presented row ids. LB-7: every
    call (success, typed failure, or validation reject) appends an audit
    row with template version + input hash + output hash + verdicts.
    LB-8: any failure returns the deterministic ``explanation`` unchanged.
    """
    from ff.llm import prompts as llm_prompts
    from ff.llm import validators as llm_validators
    from ff.llm.provider import LLMError, request_input_hash

    provider = state["llm_provider"]
    template = llm_prompts.get_template("explain_candidates")
    task_names = {
        r["task_id"]: state["task_by_id"][r["task_id"]].name
        for r in rows
        if r.get("task_id") in state["task_by_id"]
    }
    context = llm_prompts.build_explain_candidates_context(
        team, shift, rows, state.get("snapshot_id"), task_names
    )
    req = llm_prompts.render("explain_candidates", context)
    allowed = frozenset(r["task_id"] for r in rows if r.get("task_id"))
    audit = {
        "surface": "GET /api/v1/explain/candidates",
        "template_id": req.template_id,
        "template_version": req.template_version,
        "input_hash": request_input_hash(req),
        "snapshot_id": state.get("snapshot_id"),
        "provider": type(provider).__name__,
    }
    try:
        output = provider.complete_structured(req, template.response_schema)
    except LLMError as exc:
        audit.update(ok=False, error=type(exc).__name__, output_hash=None,
                     verdicts=[])
        llm_validators.audit_append(state["llm_audit_path"], audit)
        return explanation
    result = llm_validators.validate_output(output, template, context, allowed)
    audit.update(
        ok=bool(result["ok"]),
        output_hash=llm_validators.canonical_hash(output),
        verdicts=result["verdicts"],
    )
    llm_validators.audit_append(state["llm_audit_path"], audit)
    if not result["ok"]:
        return explanation  # LB-8: reject -> deterministic fallback renders
    validated = dict(explanation)
    validated["source"] = "llm-validated"
    validated["narrative"] = {
        "summary": output["summary"],
        "candidate_notes": output["candidate_notes"],
        "data_basis": output["data_basis"],
        "confidence": output["confidence"],
        "template": f"{req.template_id}@{req.template_version}",
    }
    return validated


@bp.get("/economics")
def api_economics():
    """Fleet economics (placeholder rates, OR-5 label passed through).

    Fleet-wide dollars are an all-scope surface: director/vp only; scoped
    roles get 403 (their surfaces are team-level).
    """
    state, sc = _auth()
    _require_ready(state)
    if sc["teams"] is not None:
        raise ApiError(
            403,
            "forbidden_scope",
            "economics is fleet-wide; requires an all-scope role (director/vp)",
        )
    return jsonify(state["schedule"].stats.get("economics", {}))


@bp.get("/capacity")
def api_capacity():
    """Capacity pressure, filtered to the caller's teams (RS rules).

    All-scope roles get the full report (incl. late-aircraft list); scoped
    roles get their pools only, with totals recomputed from the filtered
    pools so numbers stay internally consistent.
    """
    state, sc = _auth()
    _require_ready(state)
    full = state["schedule"].stats.get("capacity_pressure", {}) or {}
    if sc["teams"] is None:
        return jsonify(full)
    pools = [p for p in full.get("pools", []) if p.get("team") in sc["teams"]]
    wait = sum(int(p.get("wait_days", 0)) for p in pools)
    dollars = sum(int(p.get("dollar_days", 0)) for p in pools)
    return jsonify(
        {
            "pools": pools,
            "total_wait_days": wait,
            "total_dollar_days": dollars,
            "penalty_usd_per_day": full.get("penalty_usd_per_day"),
            "source": full.get("source"),
            "method": full.get("method"),
            "scoped_to": sorted(sc["teams"]),
        }
    )


@bp.get("/points/shift")
def api_points_shift():
    """Shift attainment report for (team, day, shift); team scope-checked."""
    state, sc = _auth()
    _require_ready(state)
    team = _team_arg(state, sc, required_for_all_scope=True)
    day = _int_arg("day", state.get("start_day", 0), 0, 100000)
    shift = _int_arg("shift", 1, 1, 3)
    points_mod = _svc_or_503("points")
    report = _call_or_500(
        "points_error", points_mod.shift_report, state["snapshot"], team, day, shift
    )
    return jsonify(
        {"team": team, "day": day, "shift": shift,
         "report": _webapp()._as_plain(report)}
    )


@bp.get("/points/leaderboard")
def api_points_leaderboard():
    """Team leaderboard (attainment + efficiency — never raw points, GG-2).

    Readable by every logged-in role: it exposes only normalized team
    attainment/efficiency, no task or dollar detail.
    """
    state, sc = _auth()
    _require_ready(state)
    day = _int_arg("day", state.get("start_day", 0), 0, 100000)
    points_mod = _svc_or_503("points")
    rows = _call_or_500(
        "points_error", points_mod.leaderboard, state["snapshot"], day
    )
    return jsonify({"day": day, "leaderboard": _webapp()._as_plain(rows) or []})


@bp.get("/wall")
def api_wall():
    """Factory-wall payload: leaderboards, divisions, overdrive,
    progression bars, badge/recovery feed (owner directive 2026-07-11).

    Read-only, TEAM aggregates only (§G8 — no per-mechanic data).
    Sources: the FF_SCORECARD graded card + FF_SCORECARD_RECORDS stream
    (division mapping) + the derived game-event log. Honest 404 when the
    history feeds are absent. ``config.WALL_PUBLIC`` (default False)
    allows unauthenticated access for kiosk monitors on a trusted
    internal network; otherwise any logged-in role may read it.
    Competition law enforced in ff.services.wall: attainment primary and
    capped, overdrive strictly a tie-breaker (GG-2 preserved).
    """
    if config.WALL_PUBLIC:
        state = _state()  # kiosk mode: aggregates only, read-only (§G8)
    else:
        state, _sc = _auth()
    card = state.get("scorecard")
    records = state.get("scorecard_records")
    if not isinstance(card, dict) or not isinstance(records, list):
        raise ApiError(
            404,
            "no_wall_history",
            "wall needs FF_SCORECARD and FF_SCORECARD_RECORDS history feeds",
        )
    wall_mod = _svc_or_503("wall")
    payload = _call_or_500(
        "wall_error",
        wall_mod.build_wall,
        card,
        records,
        state.get("game_events") or [],
    )
    return jsonify(_webapp()._as_plain(payload))


@bp.get("/scorecard")
def api_scorecard():
    """Graded execution history: one (slice, period) table + its trends.

    Serves the prebuilt scorecard (``ff.services.scorecard
    .build_scorecard`` output) loaded at boot from ``FF_SCORECARD`` —
    executed-shift history is a DATA FEED (the digital-fortnight runner
    writes it today; a real actuals pipeline writes the identical shape
    tomorrow), because the live snapshot cannot re-derive plans that
    already executed. Read-only, any logged-in role: rows are
    fleet/team/group/shift/building AGGREGATES — no per-mechanic data
    (§G8). Query: ``slice`` in {fleet,team,group,shift,building}
    (default team), ``period`` in {shift,day,week} (default week).
    Honest 404 when no history is loaded; OR-5: ``mock_data`` and the
    grade rubric ride on every response.
    """
    state, _sc = _auth()
    card = state.get("scorecard")
    if not isinstance(card, dict):
        raise ApiError(
            404,
            "no_scorecard",
            "no execution history loaded (set FF_SCORECARD to a "
            "build_scorecard JSON file)",
        )
    slice_by = (request.args.get("slice") or "team").strip().lower()
    period = (request.args.get("period") or "week").strip().lower()
    tables = card.get("tables", {})
    if slice_by not in tables:
        raise ApiError(
            400, "bad_slice", f"slice must be one of {sorted(tables)}"
        )
    if period not in tables[slice_by]:
        raise ApiError(
            400, "bad_period", f"period must be one of {sorted(tables[slice_by])}"
        )
    return jsonify(
        {
            "mock_data": bool(card.get("meta", {}).get("mock_data", True)),
            "meta": card.get("meta", {}),
            "slice": slice_by,
            "period": period,
            "rows": tables[slice_by][period],
            "trends": card.get("trends", {}).get(slice_by, {}),
            "week_over_week": card.get("week_over_week", {}).get(slice_by, []),
        }
    )


# ---------------------------------------------------------------------------
# GAMES progression routes (§10) — ALL READ-ONLY (GG-1: the games API has
# no write endpoints; every game state is derived from envelopes + actuals)
# ---------------------------------------------------------------------------


def _progression_team(state: dict, sc: dict, team: str) -> str:
    """Validate + authorize a path-supplied team (same tamper check as
    ``_team_arg``): 404 unknown team, 403 outside the caller's scope."""
    if team not in state.get("teams", []):
        raise ApiError(404, "unknown_team", f"team {team!r} does not exist")
    if not _webapp().team_allowed(sc, team):
        raise ApiError(403, "forbidden_scope", f"team {team!r} is outside your scope")
    return team


@bp.get("/progression/team/<team>")
def api_progression_team(team: str):
    """Streak + badges + level for one team — read-only, role-scoped.

    GG-1: GET only, derived data only. GG-3: the streak pauses (never
    breaks) on excused-heavy shifts. GG-4: badges carry their earning
    event ids. PSY-4: level/XP is a pure fold over credited completions.
    §G8: TEAM aggregates only — no per-mechanic data on this route.
    """
    state, sc = _auth()
    _require_ready(state)
    team = _progression_team(state, sc, team)
    prog_mod = _svc_or_503("progression")
    events = state.get("game_events", [])
    streak = _call_or_500(
        "progression_error", prog_mod.compute_streak, state["snapshot"], team
    )
    badges = _call_or_500("progression_error", prog_mod.badges_for_team, events, team)
    level = _call_or_500("progression_error", prog_mod.team_level, events, team)
    webapp = _webapp()
    return jsonify(
        {
            "team": team,
            "streak": webapp._as_plain(streak),
            "badges": webapp._as_plain(badges) or [],
            "level": webapp._as_plain(level),
            "earn_rates": webapp._as_plain(state.get("earn_rates")),
            "mock_data": bool(state.get("mock_data", False)),  # OR-5
        }
    )


@bp.get("/recap")
def api_recap():
    """Deterministic shift recap card for (team, day, shift) — read-only.

    LB-8: this JSON IS the fallback content — no LLM exists in the recap
    path; the card is complete by construction. Team is scope-checked with
    THE tamper check (``_team_arg``); GG-1: GET only.
    """
    state, sc = _auth()
    _require_ready(state)
    team = _team_arg(state, sc, required_for_all_scope=True)
    day = _int_arg("day", state.get("start_day", 0), 0, 100000)
    shift = _int_arg("shift", 1, 1, 3)
    prog_mod = _svc_or_503("progression")
    recap = _call_or_500(
        "progression_error",
        prog_mod.build_recap,
        team,
        day,
        shift,
        state["snapshot"],
        state.get("game_events", []),
    )
    return jsonify({"recap": _webapp()._as_plain(recap)})


@bp.get("/progression/events")
def api_progression_events():
    """The derived game-event feed, newest first — read-only, role-scoped.

    GG-1 (cited law): events are derived only; this API has NO write
    counterpart — there is no endpoint that accepts an event. RS scoping:
    scoped roles see their own teams' events plus fleet-level rows
    (``team == ""`` — recovery moments are aircraft-level celebrations
    with no individual or team detail); director/vp see all. ``?limit=``
    caps the page (default 50, max 500).
    """
    state, sc = _auth()
    _require_ready(state)
    limit = _int_arg("limit", 50, 1, 500)
    webapp = _webapp()
    rows: list[dict] = []
    for ev in reversed(state.get("game_events", [])):
        team = ev.get("team", "")
        if team and not webapp.team_allowed(sc, team):
            continue  # server-side scope filter, always
        rows.append(ev)
        if len(rows) >= limit:
            break
    return jsonify(
        {
            "count": len(rows),
            "limit": limit,
            "events": webapp._as_plain(rows) or [],
            "read_only": True,  # GG-1: no POST/PUT/DELETE exists for events
        }
    )


# ---------------------------------------------------------------------------
# write routes
# ---------------------------------------------------------------------------


@bp.post("/actuals")
def api_actuals():
    """THE single actuals write-path (OR-6).

    Body: ``{"task_id", "state", "remaining_minutes"?, "force"?}``.
    Enforced, in order:

    - 401 not logged in; 503 no fleet loaded;
    - 400 malformed body / unknown state / bad remaining_minutes;
    - 404 unknown task;
    - 403 task outside the caller's scope (mechanic: own crewed tasks only);
    - 409 reopening a ``done`` task without ``force: true`` (legal-transition
      rule: any move between not_started/in_progress/blocked and INTO done
      is legal; OUT of done requires force);
    - persists ``data/actuals.json`` atomically; appends the in-memory audit
      entry; the schedule is NOT rebuilt here — the response says
      ``stale: true`` and POST /replan rebuilds.
    """
    state, sc = _auth()
    if state.get("fleet") is None:
        raise ApiError(503, "not_ready", "no fleet loaded")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "request body must be a JSON object")
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError(400, "invalid_param", "'task_id' (string) is required")
    new_state = body.get("state")
    if new_state not in TASK_STATES:
        raise ApiError(
            400, "invalid_state", f"'state' must be one of {list(TASK_STATES)}"
        )
    task = state["task_by_id"].get(task_id)
    if task is None:
        raise ApiError(404, "unknown_task", f"task {task_id!r} does not exist")
    _check_task_scope(state, sc, task)

    force = bool(body.get("force", False))
    old_state = task.state
    if old_state == "done" and new_state != "done" and not force:
        raise ApiError(
            409,
            "done_reopen_requires_force",
            f"task {task_id} is done; reopening requires force=true",
        )

    remaining = body.get("remaining_minutes")
    if remaining is not None:
        if new_state != "in_progress":
            raise ApiError(
                400,
                "remaining_not_applicable",
                "remaining_minutes is only valid with state='in_progress'",
            )
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            raise ApiError(400, "invalid_remaining", "remaining_minutes must be an int")
        if not (0 <= remaining <= task.duration_minutes):
            raise ApiError(
                400,
                "invalid_remaining",
                f"remaining_minutes must be in [0, {task.duration_minutes}]",
            )

    # -- apply (in-memory fleet is the live truth until the next replan) -----
    task.state = new_state
    if new_state == "in_progress":
        if remaining is not None:
            task.remaining_minutes = remaining
        elif task.remaining_minutes is None:
            task.remaining_minutes = task.duration_minutes
    else:
        task.remaining_minutes = None

    state["actuals"][task_id] = {
        "state": task.state,
        "remaining_minutes": task.remaining_minutes,
    }
    persisted_to = _webapp().persist_actuals(state)
    entry = {
        "seq": len(state["audit"]) + 1,
        "at": _webapp()._now_iso(),
        "role": sc["role"],
        "scope": sc["scope"],
        "task_id": task_id,
        "from_state": old_state,
        "to_state": new_state,
        "remaining_minutes": task.remaining_minutes,
        "forced": force,
    }
    state["audit"].append(entry)
    return jsonify(
        {
            "ok": True,
            "task_id": task_id,
            "previous_state": old_state,
            "state": task.state,
            "remaining_minutes": task.remaining_minutes,
            "forced": force,
            "persisted_to": persisted_to,
            "audit_seq": entry["seq"],
            "stale": True,
            "note": "schedule/snapshot are stale until POST /api/v1/replan",
        }
    )


@bp.post("/excusals")
def api_excusals_post():
    """Lead capture of a disruption excusal (OR-6-adjacent, FOCU5-local).

    Body: ``{"task_id", "cause", "notes"?}``. Enforced, in order:

    - 401 not logged in; 403 role below lead (mechanics report actuals,
      they do not attest excusals); 503 not ready;
    - 400 malformed body / unknown cause / unknown task (contract: 400,
      not 404 — a capture against a nonexistent task is a bad capture);
    - 403 task outside the caller's team scope (lead+ scoped to OWN team);
    - 400 missing notes for SAME_TEAM_PREDECESSOR / DURATION_OVERRUN
      (self-owned causes must carry an explanation);
    - appends ``{..., source: "manual", entered_by: <session id>, ts}``
      and persists ``data/excusals.json`` atomically. Task state is NEVER
      mutated here — manual captures EXTEND auto attribution, they cannot
      overwrite it (duplicates collapse to auto in the merge).
    """
    state, sc = _auth()
    if sc["role"] == "mechanic":
        raise ApiError(
            403, "forbidden_role", "excusal capture requires role lead or above"
        )
    _require_ready(state)
    disr_mod = _svc_or_503("disruption")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "request body must be a JSON object")
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError(400, "invalid_param", "'task_id' (string) is required")
    cause = body.get("cause")
    if cause not in disr_mod.CAUSES:
        raise ApiError(
            400, "unknown_cause", f"'cause' must be one of {list(disr_mod.CAUSES)}"
        )
    task = state["task_by_id"].get(task_id)
    if task is None:
        raise ApiError(400, "unknown_task", f"task {task_id!r} does not exist")
    if not _webapp().team_allowed(sc, task.team):
        raise ApiError(
            403, "forbidden_scope", f"task team {task.team!r} is outside your scope"
        )
    notes = body.get("notes")
    notes = notes.strip() if isinstance(notes, str) else ""
    if cause in disr_mod.NOTES_REQUIRED and not notes:
        raise ApiError(
            400,
            "notes_required",
            f"cause {cause} requires notes (self-owned causes must carry an explanation)",
        )

    schedule = state["schedule"]
    asg = schedule.assignments.get(task_id)
    if asg is not None:
        day, shift = asg.day, asg.shift
    else:
        day, shift = state.get("start_day", 0), 0  # unslotted (see disruption)
    entered_by = f"{sc['role']}:{sc['scope']}"
    record = {
        "task_id": task_id,
        "team": task.team,
        "day": int(day),
        "shift": int(shift),
        "cause": cause,
        "excusable": cause in disr_mod.EXCUSABLE,
        "evidence": notes or f"manual capture by {entered_by}",
        "notes": notes,
        "source": "manual",
        "entered_by": entered_by,
        "ts": _webapp()._now_iso(),
    }
    state["excusals"].append(record)
    persisted_to = _webapp().persist_excusals(state)
    return jsonify(
        {
            "ok": True,
            "excusal": record,
            "persisted_to": persisted_to,
            "manual_count": len(state["excusals"]),
        }
    )


@bp.get("/excusals")
def api_excusals_get():
    """Merged excusals (auto attribution + manual captures) — role-scoped.

    ``?team=&day=&shift=`` filters; RS rules via the same tamper check as
    every other route: a team outside the caller's scope -> 403, and an
    all-scope role omitting ``team`` gets every team it may see. Manual
    records EXTEND auto ones; duplicates already collapsed by the service.
    """
    state, sc = _auth()
    _require_ready(state)
    disr_mod = _svc_or_503("disruption")
    team = _team_arg(state, sc)  # None only for all-scope roles (director/vp)
    day = _int_arg("day", None, 0, 100000)
    shift = _int_arg("shift", None, 0, 3)  # 0 = unslotted records
    merged = _call_or_500(
        "disruption_error",
        disr_mod.merged_excusals,
        state["snapshot"],
        state.get("excusals", []),
    )
    rows: list[dict] = []
    for (rec_team, rec_day, rec_shift) in sorted(merged):
        if team is not None and rec_team != team:
            continue
        if team is None and not _webapp().team_allowed(sc, rec_team):
            continue  # defense in depth: nothing outside scope leaves the API
        if day is not None and rec_day != day:
            continue
        if shift is not None and rec_shift != shift:
            continue
        rows.extend(merged[(rec_team, rec_day, rec_shift)])
    return jsonify(
        {
            "team": team,
            "day": day,
            "shift": shift,
            "count": len(rows),
            "excusals": rows,
        }
    )


@bp.post("/replan")
def api_replan():
    """Recompute the schedule + snapshot from live state (single-flight).

    A non-blocking ``threading.Lock`` guarantees at most one engine run at a
    time: a concurrent caller gets 409 ``replan_in_flight``, never a second
    parallel run. The commitments update (§9, OR-4) runs INSIDE the lock as
    part of ``rebuild_state``. Returns the NEW snapshot_id + headline stats
    + the snapshot ``commitments`` block (committed vs projected, last
    change-log rows, run_id).
    """
    state, sc = _auth()
    if state.get("fleet") is None:
        raise ApiError(503, "not_ready", "no fleet loaded")
    lock = state["replan_lock"]
    if not lock.acquire(blocking=False):
        raise ApiError(409, "replan_in_flight", "a replan is already running")
    try:
        try:
            _webapp().rebuild_state(state)
        except Exception as exc:
            state["boot_error"] = f"{type(exc).__name__}: {exc}"
            raise ApiError(500, "replan_failed", f"{type(exc).__name__}: {exc}")
    finally:
        lock.release()
    stats = state["schedule"].stats
    snap = state.get("snapshot")
    commitments = snap.get("commitments") if isinstance(snap, dict) else None
    return jsonify(
        {
            "ok": True,
            "snapshot_id": state.get("snapshot_id"),
            "built_at": state.get("built_at"),
            "build_count": state.get("build_count"),
            "commitments": _webapp()._as_plain(commitments)
            or {"committed": [], "changes": [], "run_id": None},
            "stats": {
                "total_tasks": stats.get("total_tasks"),
                "scheduled": stats.get("scheduled"),
                "unscheduled": stats.get("unscheduled"),
                "otd_count": stats.get("otd_count"),
                "fleet_lateness_days": stats.get("fleet_lateness_days"),
                "makespan_day": stats.get("makespan_day"),
                "wall_seconds": stats.get("wall_seconds"),
                # §12 COMMITMENT (OR-4: "committed work dispatches before
                # new work"): measured defense counters — offered /
                # slot-kept / crew-kept. 0/0/0 on the first build (no
                # previous plan => incumbent=None, byte-identical path);
                # >0 once a replan threads the previous schedule.
                "commitment_in_horizon": stats.get("commitment_in_horizon", 0),
                "commitment_kept": stats.get("commitment_kept", 0),
                "commitment_mech_kept": stats.get("commitment_mech_kept", 0),
            },
            "validator_total": state.get("validator_total"),
        }
    )
