"""Factory Wall — competition law, divisions, overdrive, feed (owner
directive 2026-07-11: leaderboards + progression on factory monitors).
"""

from __future__ import annotations

import config
from ff.services import scorecard as sc
from ff.services import wall


def R(**kw):
    base = dict(
        round_no=1, day=7, shift=1, team="T01", aircraft=1, station="P01",
        points=100, planned=True, done=True, excused=False,
    )
    base.update(kw)
    return sc.make_record(**base)


def _world():
    """Two teams, two days: T01 hits its plan with off-plan extra (P01 ->
    FAL-A); T02 misses on plan but never freelances (P07 -> FAL-B)."""
    recs = []
    for day in (7, 8):
        recs += [
            R(day=day, team="T01", aircraft=1, points=100),
            R(day=day, team="T01", aircraft=2, points=100, shift=2),
            # off-plan extra: T01 broke through a blockage (overdrive)
            R(day=day, team="T01", aircraft=3, points=50, planned=False,
              offplan=True),
            R(day=day, team="T02", aircraft=4, station="P07", points=100),
            R(day=day, team="T02", aircraft=5, station="P07", points=100,
              done=False),
        ]
    card = sc.build_scorecard(recs, mock_data=True)
    return card, recs


def _events():
    return [
        {"event_type": "completion-credited", "team": "T01", "day": 7,
         "shift": 1, "points_delta": 300.0, "evidence": "closed X",
         "event_id": "e1", "subject": {}},
        {"event_type": "badge-earned", "team": "T01", "day": 7, "shift": 1,
         "evidence": "Recovery: closed 0001-T00001", "event_id": "e2",
         "subject": {"badge": "Recovery", "badge_key": "recovery",
                     "earning_event_ids": ["e1"]}},
        {"event_type": "recovery-moment", "team": "T02", "day": 8,
         "shift": 1, "evidence": "committed day 17 -> 15", "event_id": "e3",
         "subject": {}},
    ]


def test_competition_law_attainment_primary_overdrive_tiebreak():
    card, recs = _world()
    w = wall.build_wall(card, recs, _events())
    board = {e["unit"]: e for e in w["team_board"]}
    t1, t2 = board["T01"], board["T02"]
    # T01 hit its plan (100%) with overdrive; T02 sits at 50%.
    assert t1["attainment"] == 1.0 and t1["rank"] == 1
    assert t1["overdrive"] == round(100 / 400, 4)  # 50x2 offplan / 400 goal
    assert t2["attainment"] == 0.5 and t2["rank"] == 2
    # LAW: overdrive can never lift a unit above higher attainment — give
    # T02 huge off-plan volume and it must still rank below T01.
    recs2 = recs + [
        R(day=8, team="T02", aircraft=9, station="P07", points=5000,
          planned=False, offplan=True)
    ]
    card2 = sc.build_scorecard(recs2, mock_data=True)
    w2 = wall.build_wall(card2, recs2, _events())
    b2 = {e["unit"]: e for e in w2["team_board"]}
    assert b2["T02"]["overdrive"] > b2["T01"]["overdrive"]
    assert b2["T01"]["rank"] == 1, "off-plan volume must never outrank plan-hitting"
    # attainment stays capped at 100% no matter what
    assert all(e["attainment"] <= 1.0 for e in w2["team_board"])


def test_divisions_from_executed_value():
    card, recs = _world()
    w = wall.build_wall(card, recs, _events())
    assert w["divisions"]["FAL-A"] == ["T01"]
    assert w["divisions"]["FAL-B"] == ["T02"]
    div_board = w["team_boards_by_division"]["FAL-B"]
    assert div_board[0]["unit"] == "T02" and div_board[0]["division_rank"] == 1


def test_team_shift_units_and_slice():
    card, recs = _world()
    w = wall.build_wall(card, recs, _events())
    units = {e["unit"] for e in w["team_shift_board"]}
    assert {"T01-S1", "T01-S2", "T02-S1"} <= units
    ts = {e["unit"]: e for e in w["team_shift_board"]}
    assert ts["T01-S2"]["attainment"] == 1.0
    # scorecard slice itself validates too
    rows = sc.aggregate(recs, "team_shift", "day")
    assert any(r["slice"] == "T01-S2" for r in rows)


def test_progression_and_feed_shapes():
    card, recs = _world()
    w = wall.build_wall(card, recs, _events())
    prog = {p["team"]: p for p in w["progression"]}
    assert prog["T01"]["xp"] == 300 and prog["T01"]["level"] >= 2
    assert 0.0 <= prog["T01"]["pct_to_next"] <= 1.0
    assert prog["T01"]["recent_badges"] == ["Recovery"]
    types = [f["type"] for f in w["feed"]]
    assert "badge-earned" in types and "recovery-moment" in types
    assert "completion-credited" not in types  # too chatty for a wall
    assert all(f["evidence"] for f in w["feed"])  # GG-4: receipts shown
    assert w["meta"]["mock_data"] is True


def test_rolling_window_limits_days():
    recs = []
    # 8 days of history; window 5 must exclude the first 3 (bad) days
    for day in range(7, 15):
        recs.append(R(day=day, aircraft=day, points=100,
                      done=(day >= 10)))
    card = sc.build_scorecard(recs, mock_data=True)
    w = wall.build_wall(card, recs, _events(), window_days=5)
    t1 = w["team_board"][0]
    assert t1["days"] == 5
    assert t1["attainment"] == 1.0  # early misses aged out of the window
    assert config.WALL_WINDOW_DAYS == 5
