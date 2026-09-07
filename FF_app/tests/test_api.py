"""Web/API tests — boot, health, scope enforcement, actuals write-path, replan.

Contract (ARCHITECTURE.md §ff/web): ``create_app()`` factory; ``/healthz``
liveness and ``/readyz`` readiness; demo ``POST /login`` persona+scope with
server-side scope filtering on every API (RS rules); ``POST /api/v1/actuals``
is the SINGLE actuals write-path (OR-6) with a JSON error envelope
``{error, code}``; ``POST /api/v1/replan`` re-runs the scheduler on current
state (done tasks drop out — behavior 6).

The app under test boots from a generated mini fleet via the FF_DATA env
override; FF_ENV=dev supplies the dev secret key (config §7).
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import MINI_ARGS

from ff.data.loader import save_json_gz

# Roles a write-capable persona may plausibly be named; the login helper
# walks these until one logs in. Team-scoped roles come first so a writer
# logged in for a task's team has that task in scope (RS rules).
WRITE_ROLES = ("lead", "flm", "director", "vp", "super", "all", "admin", "mechanic")


# ---------------------------------------------------------------------------
# app fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(fleet, tmp_path_factory):
    """Boot the Flask app on the generated mini fleet (FF_DATA override).

    Runs with cwd inside a temp dir AND redirects EVERY persistence file
    (data/actuals.json via FF_ACTUALS, data/commitments.json via
    FF_COMMITMENTS, data/excusals.json via FF_EXCUSALS) there, so tests
    never pollute the repo and every boot starts from a clean slate.
    """
    base = tmp_path_factory.mktemp("ff_api")
    fixture_path = base / "fleet-mini.json.gz"
    save_json_gz(fleet.to_dict(), str(fixture_path))
    (base / "data").mkdir(exist_ok=True)

    old_cwd = os.getcwd()
    old_env = {
        k: os.environ.get(k)
        for k in (
            "FF_DATA", "FF_ENV", "FF_SCHEDULE", "FF_ACTUALS",
            "FF_COMMITMENTS", "FF_EXCUSALS", "FF_GAME_EVENTS",
            "FF_ENVELOPE_DIR",
        )
    }
    os.chdir(base)
    os.environ["FF_DATA"] = str(fixture_path)
    os.environ["FF_ACTUALS"] = str(base / "data" / "actuals.json")
    os.environ["FF_COMMITMENTS"] = str(base / "data" / "commitments.json")
    os.environ["FF_EXCUSALS"] = str(base / "data" / "excusals.json")
    os.environ["FF_GAME_EVENTS"] = str(base / "data" / "game_events.jsonl")
    # INCREMENT 6: replans auto-export max_v1 envelopes — keep them in the
    # temp base too (same never-pollute-the-repo rule as the state files).
    os.environ["FF_ENVELOPE_DIR"] = str(base / "schedules")
    os.environ["FF_ENV"] = "dev"  # dev secret-key fallback is legal (config §7)
    os.environ.pop("FF_SCHEDULE", None)
    try:
        from ff.web.app import create_app

        application = create_app()
        application.config["TESTING"] = True
        yield application
    finally:
        os.chdir(old_cwd)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _login(client, payload: dict):
    """POST /login as JSON, falling back to form encoding."""
    resp = client.post("/login", json=payload, follow_redirects=True)
    if resp.status_code in (400, 415, 422):
        resp = client.post(
            "/login",
            data={k: str(v) for k, v in payload.items() if v is not None},
            follow_redirects=True,
        )
    return resp


def _login_role(client, role: str, *, team=None, mechanic_id=None):
    """Login with a contract-shaped persona payload.

    Contract (§ff/web): POST /login picks persona + scope, where scope is a
    mechanic id (mechanic role), a team (lead-ish roles) or 'all'. Extra
    aliases (persona/team/mechanic_id) are included for tolerance.
    """
    if role == "mechanic" and mechanic_id is not None:
        scope = mechanic_id
    elif team is not None and role in ("lead", "flm"):
        scope = team
    else:
        scope = "all"
    payload = {"role": role, "persona": role, "scope": scope}
    if team is not None:
        payload["team"] = team
    if mechanic_id is not None:
        payload["mechanic_id"] = mechanic_id
        payload["mech_id"] = mechanic_id
    return _login(client, payload)


def _login_writer(client, team, mechanic_id=None):
    """Login as SOME persona whose scope covers ``team`` (role-gated writes)."""
    for role in WRITE_ROLES:
        resp = _login_role(client, role, team=team, mechanic_id=mechanic_id)
        if resp.status_code < 400:
            return role
    pytest.fail("no persona could log in — /login rejected every role")


def _extract_rows(data, key_hint: str):
    """Pull the list payload out of a JSON response, list or enveloped."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (key_hint, "items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            if isinstance(value, list) and all(
                isinstance(x, dict) and "task_id" in x for x in value
            ):
                return value
    return []


def _assignment_maps(obj, out=None):
    """Recursively collect every dict stored under an 'assignments' key."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "assignments" and isinstance(value, dict):
                out.append(value)
            _assignment_maps(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _assignment_maps(value, out)
    return out


def _pick_root_tasks(fleet, n: int):
    """Deterministically pick n predecessor-free not-started tasks."""
    roots = sorted(
        t.task_id
        for t in fleet.tasks
        if not t.predecessors and t.state == "not_started"
    )
    assert len(roots) >= n, "mini fleet lacks predecessor-free tasks"
    return roots[:n]


# ---------------------------------------------------------------------------
# boot + health
# ---------------------------------------------------------------------------


def test_healthz_liveness(client):
    """GET /healthz answers 200 without auth (platform liveness probe)."""
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_readyz_ready_after_boot(client):
    """GET /readyz answers 200 once fleet + schedule are loaded at boot."""
    resp = client.get("/readyz")
    assert resp.status_code == 200


def test_state_endpoint(client, fleet):
    """GET /api/v1/state returns JSON reflecting the loaded mini fleet."""
    _login_writer(client, team=fleet.mechanics[0].team, mechanic_id=fleet.mechanics[0].mech_id)
    resp = client.get("/api/v1/state")
    assert resp.status_code == 200
    assert resp.get_json() is not None


def test_error_envelope_on_unknown_task(client, fleet):
    """JSON error envelope {error, code} on a bad task id (contract)."""
    _login_writer(client, team=fleet.mechanics[0].team, mechanic_id=fleet.mechanics[0].mech_id)
    resp = client.get("/api/v1/tasks/9999-T99999")
    assert resp.status_code >= 400
    body = resp.get_json(silent=True)
    assert isinstance(body, dict), "errors must be JSON, not HTML"
    assert "error" in body


# ---------------------------------------------------------------------------
# scope enforcement (RS rules)
# ---------------------------------------------------------------------------


def test_mechanic_scope_cannot_read_other_teams_candidates(client, fleet):
    """Server-side scope filtering: a mechanic persona asking for ANOTHER
    team's candidates gets either a 401/403 refusal or a list containing
    none of that team's tasks — never the other team's work."""
    mech = sorted(fleet.mechanics, key=lambda m: m.mech_id)[0]
    other_teams = sorted({t.team for t in fleet.tasks} - {mech.team})
    assert other_teams, "mini fleet must have more than one team"
    other_team = other_teams[0]

    resp = _login_role(client, "mechanic", team=mech.team, mechanic_id=mech.mech_id)
    assert resp.status_code < 400, "mechanic persona must be able to log in"

    resp = client.get(f"/api/v1/candidates?team={other_team}&limit=10")
    if resp.status_code >= 400:
        assert resp.status_code in (401, 403), (
            f"scope refusal must be 401/403, got {resp.status_code}"
        )
        body = resp.get_json(silent=True)
        assert isinstance(body, dict) and "error" in body
    else:
        team_of = {t.task_id: t.team for t in fleet.tasks}
        rows = _extract_rows(resp.get_json(), "candidates")
        leaked = [r["task_id"] for r in rows if team_of.get(r["task_id"]) == other_team]
        assert leaked == [], (
            f"mechanic of {mech.team} was shown {other_team}'s tasks: {leaked[:5]}"
        )


# ---------------------------------------------------------------------------
# actuals write-path (OR-6) + replan
# ---------------------------------------------------------------------------


def test_actuals_409_on_done_reopen(client, fleet):
    """OR-6 write-path integrity: once a task is reported done, a write that
    reopens it conflicts — 409 with the JSON error envelope."""
    tid = _pick_root_tasks(fleet, 2)[0]
    team = next(t.team for t in fleet.tasks if t.task_id == tid)
    _login_writer(client, team=team)  # persona whose scope covers the task

    done = client.post("/api/v1/actuals", json={"task_id": tid, "state": "done"})
    assert done.status_code < 300, (
        f"marking a root task done must succeed, got {done.status_code}: "
        f"{done.get_data(as_text=True)[:200]}"
    )

    reopen = client.post(
        "/api/v1/actuals",
        json={"task_id": tid, "state": "in_progress", "remaining_minutes": 10},
    )
    assert reopen.status_code == 409, (
        f"reopening a done task must 409, got {reopen.status_code}"
    )
    body = reopen.get_json(silent=True)
    assert isinstance(body, dict)
    assert "error" in body and "code" in body, "contract error envelope {error, code}"


def test_actuals_then_replan_drops_done_task(client, fleet):
    """Actuals write -> replan drops the done task: after POST /actuals
    (state=done) and POST /replan, the rebuilt schedule contains no
    assignment for the task (scheduler behavior 6: done work is excluded,
    its successors treat it as satisfied)."""
    tid = _pick_root_tasks(fleet, 2)[1]
    team = next(t.team for t in fleet.tasks if t.task_id == tid)
    _login_writer(client, team=team)  # persona whose scope covers the task

    done = client.post("/api/v1/actuals", json={"task_id": tid, "state": "done"})
    assert done.status_code < 300

    replan = client.post("/api/v1/replan")
    assert replan.status_code < 300, (
        f"replan must succeed, got {replan.status_code}: "
        f"{replan.get_data(as_text=True)[:200]}"
    )

    # The rebuilt state must not book the finished task anywhere.
    state = client.get("/api/v1/state")
    assert state.status_code == 200
    maps = _assignment_maps(state.get_json())
    for mapping in maps:
        assert tid not in mapping, f"done task {tid} still has an assignment"

    # And the task endpoint must report it as done / DONE-feasible.
    task_resp = client.get(f"/api/v1/tasks/{tid}")
    assert task_resp.status_code == 200
    text = task_resp.get_data(as_text=True)
    assert "done" in text.lower(), "task detail must reflect the done actual"


def test_replan_deterministic_signal(client, fleet):
    """Two replans with no state change in between leave /readyz healthy and
    /api/v1/state serving (single-flight lock never wedges the app)."""
    mech = fleet.mechanics[0]
    _login_writer(client, team=mech.team, mechanic_id=mech.mech_id)
    first = client.post("/api/v1/replan")
    second = client.post("/api/v1/replan")
    assert first.status_code < 300
    # A concurrent-guard refusal (409/423/429) is acceptable for the second
    # call only if the app stays healthy; sequential calls normally succeed.
    assert second.status_code < 300 or second.status_code in (409, 423, 429)
    assert client.get("/readyz").status_code == 200
    assert client.get("/api/v1/state").status_code == 200


# ---------------------------------------------------------------------------
# excusal capture (INCREMENT 2) — authz, notes-required, persistence
# ---------------------------------------------------------------------------


def _task_team(fleet, tid: str) -> str:
    return next(t.team for t in fleet.tasks if t.task_id == tid)


def test_excusal_capture_mechanic_403(client, fleet):
    """Excusal authz (addendum): 'role lead+' — a MECHANIC persona posting
    an excusal (even on their own team's task) is refused 403: mechanics
    report actuals, they do not attest excusals."""
    mech = sorted(fleet.mechanics, key=lambda m: m.mech_id)[0]
    tid = next(
        t.task_id
        for t in sorted(fleet.tasks, key=lambda t: t.task_id)
        if t.team == mech.team
    )
    resp = _login_role(client, "mechanic", team=mech.team, mechanic_id=mech.mech_id)
    assert resp.status_code < 400
    post = client.post("/api/v1/excusals", json={"task_id": tid, "cause": "LATE_PART"})
    assert post.status_code == 403
    body = post.get_json(silent=True)
    assert isinstance(body, dict) and "error" in body and "code" in body


def test_excusal_capture_lead_wrong_team_403(client, fleet):
    """Excusal authz (addendum): 'scoped to own team ... 403 out-of-scope
    team' — a lead of team B posting an excusal against team A's task is
    refused 403, and so is a GET naming team A."""
    teams = sorted({t.team for t in fleet.tasks})
    assert len(teams) >= 2
    task_team, lead_team = teams[0], teams[1]
    tid = next(
        t.task_id
        for t in sorted(fleet.tasks, key=lambda t: t.task_id)
        if t.team == task_team
    )
    resp = _login_role(client, "lead", team=lead_team)
    assert resp.status_code < 400
    post = client.post("/api/v1/excusals", json={"task_id": tid, "cause": "LATE_PART"})
    assert post.status_code == 403
    get = client.get(f"/api/v1/excusals?team={task_team}")
    assert get.status_code == 403, "GET is role-scoped with the same tamper check"


def test_excusal_notes_required_400(client, fleet):
    """Addendum: 'notes REQUIRED for SAME_TEAM_PREDECESSOR and
    DURATION_OVERRUN' — captures for the self-owned causes are 400 without
    notes and succeed once notes are supplied."""
    tid = _pick_root_tasks(fleet, 4)[2]
    team = _task_team(fleet, tid)
    resp = _login_role(client, "lead", team=team)
    assert resp.status_code < 400
    for cause in ("SAME_TEAM_PREDECESSOR", "DURATION_OVERRUN"):
        bare = client.post("/api/v1/excusals", json={"task_id": tid, "cause": cause})
        assert bare.status_code == 400, f"{cause} without notes must 400"
        assert bare.get_json()["code"] == "notes_required"
    noted = client.post(
        "/api/v1/excusals",
        json={"task_id": tid, "cause": "DURATION_OVERRUN",
              "notes": "sealant cure ran 2h over"},
    )
    assert noted.status_code == 200
    body = noted.get_json()
    assert body["excusal"]["cause"] == "DURATION_OVERRUN"
    assert body["excusal"]["excusable"] is False
    assert body["excusal"]["source"] == "manual"
    assert body["excusal"]["notes"] == "sealant cure ran 2h over"


def test_excusal_unknown_task_and_cause_400(client, fleet):
    """Addendum: '400 unknown task/cause' — a capture naming a fictional
    task or a cause outside CAUSES is a bad capture, never stored."""
    tid = _pick_root_tasks(fleet, 4)[2]
    team = _task_team(fleet, tid)
    _login_role(client, "lead", team=team)
    bad_cause = client.post(
        "/api/v1/excusals", json={"task_id": tid, "cause": "DOG_ATE_IT"}
    )
    assert bad_cause.status_code == 400
    bad_task = client.post(
        "/api/v1/excusals", json={"task_id": "9999-T99999", "cause": "LATE_PART"}
    )
    assert bad_task.status_code == 400


def test_excusal_capture_roundtrip_get(client, fleet, app):
    """Happy path: a lead captures an excusable cause on an own-team task;
    GET /api/v1/excusals returns it (source 'manual', entered_by stamped)
    and the append-only data/excusals.json exists on disk (atomic write)."""
    tid = _pick_root_tasks(fleet, 4)[3]
    team = _task_team(fleet, tid)
    resp = _login_role(client, "lead", team=team)
    assert resp.status_code < 400
    post = client.post(
        "/api/v1/excusals",
        json={"task_id": tid, "cause": "LATE_PART", "notes": "AOG spare due"},
    )
    assert post.status_code == 200, post.get_data(as_text=True)[:200]
    body = post.get_json()
    assert os.path.exists(body["persisted_to"])

    get = client.get(f"/api/v1/excusals?team={team}")
    assert get.status_code == 200
    rows = get.get_json()["excusals"]
    mine = [
        r for r in rows
        if r["task_id"] == tid and r["cause"] == "LATE_PART" and r["source"] == "manual"
    ]
    assert len(mine) == 1, "capture must round-trip through the merged view"
    assert mine[0]["entered_by"] == f"lead:{team}"


def test_replan_updates_commitments_block(client, fleet):
    """Addendum: 'replan updates commitments block' — the POST /replan
    response carries the snapshot commitments block ({committed, changes,
    run_id}); every fleet aircraft holds a committed date (rule 1: first
    sighting commits immediately, at boot) and run_id tracks the snapshot."""
    mech = fleet.mechanics[0]
    _login_writer(client, team=mech.team, mechanic_id=mech.mech_id)
    resp = client.post("/api/v1/replan")
    assert resp.status_code < 300, resp.get_data(as_text=True)[:200]
    body = resp.get_json()
    assert "commitments" in body, "replan response must carry the block"
    block = body["commitments"]
    assert set(block) >= {"committed", "changes", "run_id"}
    assert block["run_id"] == body["snapshot_id"]
    committed_ac = {row["aircraft"] for row in block["committed"]}
    assert committed_ac == {a.aircraft for a in fleet.aircraft}
    for row in block["committed"]:
        assert set(row) == {
            "aircraft", "committed_day", "projected_day", "deadline_day", "delta"
        }
    reasons = [ch["reason"] for ch in block["changes"]]
    assert "initial commitment" in reasons, "boot run 1 committed on first sighting"
