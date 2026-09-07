"""GET /api/v1/scorecard — graded-history endpoint (fortnight increment).

Boots the app twice: once WITH FF_SCORECARD pointing at a small
build_scorecard file (200 path: rows/trends/week-over-week served, rubric
visible), once WITHOUT (honest 404 — no execution history is never a
fake empty 200). Bad slice/period -> 400 envelopes.
"""

from __future__ import annotations

import json
import os

import pytest

from ff.data.generator import generate_fleet
from ff.data.loader import save_json_gz
from ff.services import scorecard as sc
from tests.conftest import MINI_ARGS


def _card_file(base) -> str:
    """Write a tiny but real build_scorecard card to disk."""
    records = [
        sc.make_record(
            round_no=1, day=7, shift=1, team="T01", aircraft=1,
            station="P01", points=100, planned=True, done=True, excused=False,
        ),
        sc.make_record(
            round_no=1, day=7, shift=1, team="T01", aircraft=2,
            station="P01", points=50, planned=True, done=False, excused=False,
        ),
        sc.make_record(
            round_no=2, day=14, shift=1, team="T01", aircraft=3,
            station="POST-FAL", points=80, planned=True, done=True,
            excused=False,
        ),
    ]
    path = str(base / "scorecard.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sc.build_scorecard(records, mock_data=True), f)
    return path


@pytest.fixture()
def app_factory(tmp_path):
    """Build an app bound to tmp state; optionally with FF_SCORECARD."""
    fleet = generate_fleet(*MINI_ARGS)
    fixture_path = tmp_path / "fleet-mini.json.gz"
    save_json_gz(fleet.to_dict(), str(fixture_path))
    (tmp_path / "data").mkdir(exist_ok=True)

    keys = (
        "FF_DATA", "FF_ENV", "FF_SCHEDULE", "FF_ACTUALS", "FF_COMMITMENTS",
        "FF_EXCUSALS", "FF_GAME_EVENTS", "FF_ENVELOPE_DIR", "FF_SCORECARD",
    )
    old_cwd, old_env = os.getcwd(), {k: os.environ.get(k) for k in keys}

    def make(scorecard_path: str | None):
        os.chdir(tmp_path)
        os.environ["FF_DATA"] = str(fixture_path)
        os.environ["FF_ACTUALS"] = str(tmp_path / "data" / "actuals.json")
        os.environ["FF_COMMITMENTS"] = str(tmp_path / "data" / "c.json")
        os.environ["FF_EXCUSALS"] = str(tmp_path / "data" / "e.json")
        os.environ["FF_GAME_EVENTS"] = str(tmp_path / "data" / "g.jsonl")
        os.environ["FF_ENVELOPE_DIR"] = str(tmp_path / "schedules")
        os.environ["FF_ENV"] = "dev"
        os.environ.pop("FF_SCHEDULE", None)
        if scorecard_path:
            os.environ["FF_SCORECARD"] = scorecard_path
        else:
            os.environ.pop("FF_SCORECARD", None)
        from ff.web.app import create_app

        application = create_app()
        application.config["TESTING"] = True
        return application

    yield make

    os.chdir(old_cwd)
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _client(app):
    client = app.test_client()
    resp = client.post(
        "/login", json={"role": "director", "persona": "director", "scope": "all"}
    )
    assert resp.status_code in (200, 302)
    return client


def test_scorecard_served_with_history(app_factory, tmp_path):
    app = app_factory(_card_file(tmp_path))
    client = _client(app)

    resp = client.get("/api/v1/scorecard?slice=team&period=week")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["mock_data"] is True  # OR-5 label rides on the response
    assert d["slice"] == "team" and d["period"] == "week"
    by_week = {r["period_key"]: r for r in d["rows"]}
    assert by_week[1]["attainment"] == round(100 / 150, 4)
    assert by_week[2]["attainment"] == 1.0 and by_week[2]["grade"] == "A"
    assert d["meta"]["grade_bands"], "rubric must be visible"
    assert d["week_over_week"], "W1->W2 delta expected"

    # building slice works off the same card
    resp = client.get("/api/v1/scorecard?slice=building&period=week")
    slices = {r["slice"] for r in resp.get_json()["rows"]}
    assert slices == {"FAL-A", "FLIGHTLINE"}

    # defaults: slice=team period=week
    assert client.get("/api/v1/scorecard").status_code == 200

    # bad args -> 400 envelope
    for q in ("slice=bogus", "period=century"):
        r = client.get("/api/v1/scorecard?" + q)
        assert r.status_code == 400 and "code" in r.get_json()


def test_scorecard_404_without_history(app_factory):
    app = app_factory(None)
    client = _client(app)
    resp = client.get("/api/v1/scorecard")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "no_scorecard"


# ---------------------------------------------------------------------------
# GET /api/v1/wall + /wall kiosk page (factory-wall increment)
# ---------------------------------------------------------------------------


def _wall_history(base) -> tuple[str, str]:
    """(card_path, records_path) built from ONE record set — the same
    single-source rule production follows (the card is derived from the
    records the runner wrote)."""
    records = [
        sc.make_record(
            round_no=1, day=7, shift=1, team="T01", aircraft=1,
            station="P01", points=100, planned=True, done=True, excused=False,
        ),
        sc.make_record(
            round_no=1, day=7, shift=1, team="T01", aircraft=2,
            station="P01", points=60, planned=False, done=True,
            excused=False, offplan=True,
        ),
    ]
    rec_path = str(base / "records.json.gz")
    save_json_gz({"mock_data": True, "records": records}, rec_path)
    card_path = str(base / "wall_card.json")
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(sc.build_scorecard(records, mock_data=True), f)
    return card_path, rec_path


def test_wall_served_with_history(app_factory, tmp_path, monkeypatch):
    card_path, rec_path = _wall_history(tmp_path)
    monkeypatch.setenv("FF_SCORECARD_RECORDS", rec_path)
    app = app_factory(card_path)

    # RS rule: login required by default
    anon = app.test_client()
    assert anon.get("/api/v1/wall").status_code == 401
    assert anon.get("/wall").status_code == 302  # redirect to /login

    client = _client(app)
    resp = client.get("/api/v1/wall")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["meta"]["mock_data"] is True
    assert d["team_board"] and d["team_board"][0]["unit"] == "T01"
    assert d["team_board"][0]["overdrive"] > 0  # off-plan extra visible
    assert "FAL-A" in d["divisions"]
    assert client.get("/wall").status_code == 200


def test_wall_public_kiosk_mode(app_factory, tmp_path, monkeypatch):
    import config

    card_path, rec_path = _wall_history(tmp_path)
    monkeypatch.setenv("FF_SCORECARD_RECORDS", rec_path)
    app = app_factory(card_path)
    monkeypatch.setattr(config, "WALL_PUBLIC", True)
    anon = app.test_client()
    assert anon.get("/api/v1/wall").status_code == 200  # kiosk monitors
    assert anon.get("/wall").status_code == 200
    # kiosk payload stays aggregates-only (§G8): no mechanic ids anywhere
    body = anon.get("/api/v1/wall").get_data(as_text=True)
    assert "-M0" not in body


def test_wall_404_without_history(app_factory, tmp_path):
    app = app_factory(_card_file(tmp_path))  # card but no records feed
    client = _client(app)
    resp = client.get("/api/v1/wall")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "no_wall_history"
