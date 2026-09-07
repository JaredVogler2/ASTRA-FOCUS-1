"""INCREMENT 6 Part C — FF bridge tests: BOTH apps booted, loop closed.

The vendored FOCUS dashboard (web_flask) gets its live intelligence from
the FF_app backend through the /api/ff bridge blueprint.  These tests
boot the REAL FF backend over HTTP (werkzeug dev server, ephemeral port,
mini fleet, all persistence redirected into pytest tmp) and the vendored
Flask app in-process (test client, MAX_SCHEDULES_DIR + FF_API_BASE
pointed at the test fixtures), then drive the contract:

- read proxies (points/shift, points/leaderboard, progression, recap,
  explain/candidates) pass FF payloads through verbatim — leaderboard
  rows still never carry raw points (GG-2), explain always carries the
  deterministic content (LB-8);
- the actuals write-back round trip: mark done through the bridge ->
  FF replan -> NEW envelope exported -> local refresh -> the task is
  gone from the dashboard's plan (OR-6: the FF endpoint was the only
  write-path involved);
- done-reopen 409 passes through and force=true resolves it;
- excusal capture: notes-required 400 passthrough, then a successful
  manual capture visible in the merged GET;
- an unreachable FF backend is an honest 502 {error, code} — never a
  fake 200.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone

import pytest

from tests.conftest import FF_ROOT

from ff.data.loader import save_json_gz
from ff.export.envelope import export_envelope

WEB_FLASK_ROOT = os.path.join(FF_ROOT, "web_flask")

# Fixed wall-clock for the INITIAL envelope only (deterministic name);
# the replan-exported envelope uses the FF server's real clock, which is
# fine — dashboard discovery is by file MTIME, newest first (owner rule).
NOW = datetime(2026, 7, 11, 6, 0, 0, tzinfo=timezone.utc)

_FF_ENV_KEYS = (
    "FF_DATA", "FF_SCHEDULE", "FF_ACTUALS", "FF_COMMITMENTS", "FF_EXCUSALS",
    "FF_GAME_EVENTS", "FF_ENVELOPE_DIR", "FF_LLM_AUDIT", "FF_ENV",
)


# ---------------------------------------------------------------------------
# fixtures — one shared temp base: FF state + the schedules dir both live
# in pytest tmp so the repo is never polluted (house rule).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("ff_bridge")


@pytest.fixture(scope="module")
def schedules_dir(base_dir, fleet, schedule, cpm):
    """The shared envelope dir, seeded with ONE initial mini-fleet envelope
    (the dashboard needs a loadable plan at boot; FF replans add more)."""
    out = base_dir / "schedules"
    out.mkdir()
    export_envelope(fleet, schedule, cpm, out_dir=out, now=NOW)
    return out


@pytest.fixture(scope="module")
def ff_server(base_dir, schedules_dir, fleet):
    """The REAL FF backend over HTTP: create_app() + werkzeug dev server on
    an ephemeral port, every persistence path redirected into tmp."""
    from werkzeug.serving import make_server

    fixture_path = base_dir / "fleet-mini.json.gz"
    save_json_gz(fleet.to_dict(), str(fixture_path))

    old_env = {k: os.environ.get(k) for k in _FF_ENV_KEYS}
    os.environ["FF_ENV"] = "dev"
    os.environ["FF_DATA"] = str(fixture_path)
    os.environ["FF_ACTUALS"] = str(base_dir / "actuals.json")
    os.environ["FF_COMMITMENTS"] = str(base_dir / "commitments.json")
    os.environ["FF_EXCUSALS"] = str(base_dir / "excusals.json")
    os.environ["FF_GAME_EVENTS"] = str(base_dir / "game_events.jsonl")
    os.environ["FF_LLM_AUDIT"] = str(base_dir / "llm_audit.jsonl")
    os.environ["FF_ENVELOPE_DIR"] = str(schedules_dir)
    os.environ.pop("FF_SCHEDULE", None)
    try:
        from ff.web.app import create_app

        ff_app = create_app()
        server = make_server("127.0.0.1", 0, ff_app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=10)
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def web_app(ff_server, schedules_dir):
    """The vendored dashboard app, discovery + bridge pointed at the test
    fixtures.  src.* is re-imported so src.paths.SCHEDULES_DIR (frozen at
    import time) picks up MAX_SCHEDULES_DIR; prior module objects are
    restored afterwards so other test files see their own imports."""
    old_env = {k: os.environ.get(k) for k in ("MAX_SCHEDULES_DIR", "FF_API_BASE")}
    os.environ["MAX_SCHEDULES_DIR"] = str(schedules_dir)
    os.environ["FF_API_BASE"] = ff_server
    # Runtime artifacts the vendored app may create on boot (never leave
    # them behind in the repo — house rule).
    artifacts = [
        os.path.join(WEB_FLASK_ROOT, "data", "shift_performance.db"),
        os.path.join(WEB_FLASK_ROOT, "ie_review_queue.json"),
    ]
    preexisting = {p for p in artifacts if os.path.exists(p)}
    if WEB_FLASK_ROOT not in sys.path:
        sys.path.insert(0, WEB_FLASK_ROOT)
    saved_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "src" or name.startswith("src.")
    }
    for name in saved_modules:
        del sys.modules[name]
    try:
        from src.app import create_app as create_dashboard_app

        app = create_dashboard_app()
        app.config["TESTING"] = True
        yield app
    finally:
        for name in [n for n in sys.modules
                     if n == "src" or n.startswith("src.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in artifacts:
            if path not in preexisting and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


@pytest.fixture()
def client(web_app):
    return web_app.test_client()


def _initial_envelope(schedules_dir):
    files = sorted(schedules_dir.glob("max_v1_*.json.gz"),
                   key=lambda p: p.stat().st_mtime)
    with gzip.open(str(files[0]), "rt", encoding="utf-8") as f:
        return json.load(f)


def _pick_writable_task(schedules_dir):
    """A scheduled, not_started, real-crew task from the INITIAL envelope
    (the write-back target), plus its identity fields."""
    raw = _initial_envelope(schedules_dir)
    for t in raw["tasks"]:
        if not t.get("isFallback") and t.get("state") == "not_started" \
                and t.get("mechanicIds"):
            return t
    raise AssertionError("mini envelope has no writable task")


# ---------------------------------------------------------------------------
# read proxies
# ---------------------------------------------------------------------------


def test_dashboard_booted_on_initial_envelope(web_app, schedules_dir):
    """Precondition: the vendored app loaded the seeded FF envelope."""
    assert web_app.current_schedule_data is not None
    assert web_app.current_schedule_file.startswith("max_v1_")
    assert len(list(schedules_dir.glob("max_v1_*.json.gz"))) == 1


def test_bridge_proxies_points_shift(client, web_app):
    team = web_app.current_schedule_data["tasks"][0]["team"]
    resp = client.get(f"/api/ff/points/shift?team={team}&day=0&shift=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["team"] == team
    report = data["report"]
    assert {"goal", "earned", "attainment", "difficulty",
            "excused_points", "excusals"} <= set(report)


def test_bridge_proxies_leaderboard_never_raw_points(client):
    """GG-2 passes through the proxy intact: leaderboard rows carry
    attainment/efficiency/difficulty, NEVER raw goal/earned totals."""
    resp = client.get("/api/ff/points/leaderboard?day=0")
    assert resp.status_code == 200
    rows = resp.get_json()["leaderboard"]
    assert rows, "mini fleet must produce leaderboard rows"
    for row in rows:
        assert {"team", "attainment", "efficiency", "difficulty"} <= set(row)
        assert "goal" not in row and "earned" not in row  # GG-2
        assert "points" not in row


def test_bridge_proxies_scorecard_allowlisted(client):
    """Fortnight increment: /api/ff/scorecard is on the allow-list and FF
    error envelopes pass through untouched — the harness backend has no
    FF_SCORECARD history loaded, so the honest 404 {error, code} must
    arrive verbatim (never a bridge-level Flask 404 or a fake 200)."""
    resp = client.get("/api/ff/scorecard?slice=team&period=week")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["code"] == "no_scorecard"


def test_bridge_proxies_progression_and_recap(client, web_app):
    team = web_app.current_schedule_data["tasks"][0]["team"]
    resp = client.get(f"/api/ff/progression/team/{team}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["team"] == team
    assert {"streak", "badges", "level"} <= set(data)
    resp = client.get(f"/api/ff/recap?team={team}&day=0&shift=1")
    assert resp.status_code == 200
    recap = resp.get_json()["recap"]
    # LB-8: the recap IS the deterministic fallback content.
    assert recap["generated_by"] == "deterministic"
    assert recap["team"] == team


def test_bridge_proxies_explain_with_source_label(client, web_app):
    """LB-8 through the bridge: deterministic content is ALWAYS present;
    the source label only ever reads deterministic | llm-validated."""
    team = web_app.current_schedule_data["tasks"][0]["team"]
    resp = client.get(f"/api/ff/explain/candidates?team={team}&limit=5")
    assert resp.status_code == 200
    data = resp.get_json()
    expl = data["explanation"]
    assert expl["source"] in ("deterministic", "llm-validated")
    assert "summary" in expl
    if data["count"]:
        assert expl["items"], "deterministic items must accompany candidates"
        for cand in data["candidates"]:
            assert "components" in cand and "score" in cand  # GG-4 decompose
    if expl["source"] == "llm-validated":
        assert expl["narrative"]["confidence"] is not None


def test_bridge_scope_errors_pass_through(client):
    """FF's 404 for an unknown team passes through verbatim (no masking)."""
    resp = client.get("/api/ff/points/shift?team=NOPE&day=0&shift=1")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "unknown_team"


# ---------------------------------------------------------------------------
# the actuals write-back round trip (the closed loop)
# ---------------------------------------------------------------------------


def test_actuals_write_back_round_trip(client, web_app, schedules_dir):
    """Mark done via bridge -> FF replan -> NEW envelope exported -> local
    refresh -> the task is GONE from the dashboard's plan (OR-6: the only
    write was FF POST /api/v1/actuals, reached through the bridge)."""
    task = _pick_writable_task(schedules_dir)
    task_id, soi = task["taskId"], task["ff_task_id"]
    bems = task["mechanicIds"][0]
    before_files = set(schedules_dir.glob("max_v1_*.json.gz"))
    before_count = len(web_app.current_schedule_data["tasks"])
    old_schedule_file = web_app.current_schedule_file

    # 1. actuals through the bridge — envelope identity in, FF identity out.
    resp = client.post("/api/ff/actuals",
                       json={"taskId": task_id, "state": "done", "bems": bems})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["task_id"] == soi          # taskId -> FF task_id translated
    assert body["state"] == "done"
    assert body["stale"] is True           # FF says: replan to apply

    # 2. replan through the bridge: FF re-runs the engine, re-exports the
    #    envelope, and the bridge runs the local refresh.
    resp = client.post("/api/ff/replan")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["dashboard_refreshed"] is True
    after_files = set(schedules_dir.glob("max_v1_*.json.gz"))
    assert len(after_files) == len(before_files) + 1  # new envelope exported
    assert web_app.current_schedule_file != old_schedule_file
    assert body["current_schedule"] == web_app.current_schedule_file

    # 3. the done task left the plan (Stage-1 semantics: done -> excluded).
    tasks_now = web_app.current_schedule_data["tasks"]
    assert len(tasks_now) == before_count - 1
    assert all(t["taskId"] != task_id for t in tasks_now)

    # 4. the My Day surface agrees: the reported task is gone for its crew.
    resp = client.get(f"/api/myday?bems={bems}&day={task['day']}")
    assert resp.status_code == 200
    assert all(r["taskId"] != task_id for r in resp.get_json()["tasks"])

    # 5. POST /api/refresh (the client-side fallback step) stays honest+idempotent.
    resp = client.post("/api/refresh")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_done_reopen_409_passthrough_then_force(client, schedules_dir):
    """FF's done-reopen guard passes through the bridge: 409 without
    force, 200 with force=true (the client shows a confirm dialog)."""
    # The round-trip test above just marked this task done.
    task = _pick_writable_task(schedules_dir)
    resp = client.post("/api/ff/actuals",
                       json={"taskId": task["taskId"], "state": "in_progress"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "done_reopen_requires_force"
    resp = client.post("/api/ff/actuals",
                       json={"taskId": task["taskId"], "state": "in_progress",
                             "force": True})
    assert resp.status_code == 200
    assert resp.get_json()["forced"] is True
    # Restore done so later runs of the module stay consistent.
    resp = client.post("/api/ff/actuals",
                       json={"taskId": task["taskId"], "state": "done"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# excusal capture through the bridge
# ---------------------------------------------------------------------------


def test_excusal_notes_required_400_passthrough(client, web_app):
    """FF enforces notes for self-owned causes; the 400 passes through the
    bridge verbatim (client-side enforcement is a convenience, the FF
    backend stays the authority)."""
    task = next(t for t in web_app.current_schedule_data["tasks"]
                if t.get("ff_task_id"))
    resp = client.post("/api/ff/excusals",
                       json={"task_id": task["ff_task_id"],
                             "cause": "SAME_TEAM_PREDECESSOR", "notes": ""})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "notes_required"


def test_excusal_capture_with_notes_round_trip(client, web_app):
    task = next(t for t in web_app.current_schedule_data["tasks"]
                if t.get("ff_task_id"))
    resp = client.post(
        "/api/ff/excusals",
        json={"taskId": task["taskId"],  # envelope id: bridge translates
              "cause": "SAME_TEAM_PREDECESSOR",
              "notes": "pred slipped in our own sequence (bridge test)"})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    rec = body["excusal"]
    assert rec["task_id"] == task["ff_task_id"]
    assert rec["source"] == "manual"
    assert rec["excusable"] is False  # SAME_TEAM_PREDECESSOR is self-owned
    # Visible in the merged GET for that team.
    resp = client.get(f"/api/ff/excusals?team={task['team']}")
    assert resp.status_code == 200
    rows = resp.get_json()["excusals"]
    assert any(r["task_id"] == task["ff_task_id"] and r["source"] == "manual"
               for r in rows)


def test_unknown_cause_400_passthrough(client, web_app):
    task = next(t for t in web_app.current_schedule_data["tasks"]
                if t.get("ff_task_id"))
    resp = client.post("/api/ff/excusals",
                       json={"task_id": task["ff_task_id"],
                             "cause": "DOG_ATE_IT", "notes": "x"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "unknown_cause"


# ---------------------------------------------------------------------------
# honesty: FF backend down => 502, never a fake 200
# ---------------------------------------------------------------------------


def test_ff_backend_down_is_honest_502(client, monkeypatch):
    """Bridge honesty rule: an unreachable FF backend surfaces as HTTP 502
    with the {error, code} envelope — no cached stand-in, no fake 200."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    monkeypatch.setenv("FF_API_BASE", f"http://127.0.0.1:{dead_port}")
    resp = client.get("/api/ff/points/leaderboard?day=0")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["code"] == "ff_backend_down"
    assert "unreachable" in body["error"]
    # Writes fail just as honestly.
    resp = client.post("/api/ff/actuals", json={"taskId": "x_1", "state": "done"})
    assert resp.status_code == 502
    assert resp.get_json()["code"] == "ff_backend_down"
