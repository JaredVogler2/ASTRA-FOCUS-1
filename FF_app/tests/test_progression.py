"""GAMES progression tests — events, badges, streaks, recap, API scoping.

Tripwires for the §10 progression layer (mission spec, X2/X3 canon
adapted). Every test docstring quotes the rule it enforces:

- GG-1: events are DERIVED only (no write route exists; deterministic).
- GG-2: recap top plays + leaderboard rank by point VALUE, never raw
  points/task count.
- GG-3: excused-heavy shifts PAUSE a streak, never break it.
- GG-4: every badge names its real earning event(s).
- §G8/PSY-5: role scoping — team data only inside the caller's scope.
"""

from __future__ import annotations

import os

import pytest

import config
from tests.conftest import mk_aircraft, mk_fleet, mk_mech, mk_task, run_schedule

from ff.services import progression
from ff.services.points import leaderboard, score_task, shift_report
from ff.services.snapshot import build_snapshot

# ---------------------------------------------------------------------------
# badge/event fixture: one keystone gate + a blocked recovery task
# ---------------------------------------------------------------------------

G = "0001-T00001"                              # the keystone gate
S = [f"0001-T{n:05d}" for n in range(2, 7)]    # 5 successors, gated on G
R = "0001-T00007"                              # blocked-on-parts, then closed


def _badge_fleet():
    """Team T01, aircraft 1 (deadline 0 => every task CPM-critical):

    - G gates S1..S5 (completing G singly frees 5 -> keystone-cleared with
      count 5 >= KEYSTONE_MIN_UNLOCKED=3 and >= FIREBREAK_MIN_UNLOCKED=5);
    - R is blocked with parts ETA day 0 (done later => a task closed on an
      aircraft 'blocked entering the shift' -> the Recovery badge trigger).
    """
    mechanics = [
        mk_mech("T01-S1-M001", skills=["COMMON"]),
        mk_mech("T01-S1-M002", skills=["COMMON"]),
        mk_mech("T01-S1-M003", skills=["COMMON"]),
    ]
    tasks = [mk_task(G, dur=60, skill="COMMON", deadline=0)]
    tasks += [
        mk_task(tid, dur=60, skill="COMMON", deadline=0, preds=[G]) for tid in S
    ]
    tasks.append(
        mk_task(R, dur=60, skill="COMMON", deadline=0, state="blocked", parts_eta=0)
    )
    return mk_fleet(tasks, mechanics, aircraft=[mk_aircraft(1, deadline=0)])


@pytest.fixture()
def badge_world():
    """(fleet, cpm, schedule, log, summaries-by-stage, snaps-by-stage).

    Three consecutive derived states over ONE append-only log (GG-1):
    a) baseline (nothing done), b) G+R done, c) S1..S5 done too.
    """
    fleet = _badge_fleet()
    cpm, sched = run_schedule(fleet)
    for tid in [G] + S + [R]:
        assert (sched.assignments[tid].day, sched.assignments[tid].shift) == (0, 1)
    by_id = {t.task_id: t for t in fleet.tasks}
    log: list = []

    snap_a = build_snapshot(fleet, sched, cpm)
    _, summary_a, _ = progression.advance(None, log, snap_a)
    assert log == [], "baseline (no previous summary) must emit NO events"

    by_id[G].state = "done"
    by_id[R].state = "done"
    snap_b = build_snapshot(fleet, sched, cpm)
    progression.advance(summary_a, log, snap_b)
    summary_b = progression.snapshot_summary(snap_b)

    for tid in S:
        by_id[tid].state = "done"
    snap_c = build_snapshot(fleet, sched, cpm)
    progression.advance(summary_b, log, snap_c)

    return {
        "fleet": fleet,
        "log": log,
        "summary_a": summary_a,
        "summary_b": summary_b,
        "snap_a": snap_a,
        "snap_b": snap_b,
        "snap_c": snap_c,
    }


def _by_type(log, event_type):
    return [ev for ev in log if ev["event_type"] == event_type]


def _badges(log, badge):
    return [
        ev
        for ev in _by_type(log, "badge-earned")
        if ev["subject"]["badge"] == badge
    ]


# ---------------------------------------------------------------------------
# event stream (GG-1: derived only, deterministic, append-only)
# ---------------------------------------------------------------------------


def test_event_stream_derivation(badge_world):
    """GG-1: 'derived only, no user-generated events; deterministic given
    inputs' — completions, unlocks and the keystone all come from the
    summary-vs-snapshot diff; the gate that singly freed 5 successors emits
    unlock AND keystone-cleared (single gate freeing >= 3)."""
    log = badge_world["log"]
    completions = _by_type(log, "completion-credited")
    assert {ev["subject"]["task_id"] for ev in completions} == set([G, R] + S)
    for ev in completions:
        assert ev["points_delta"] > 0, "GG-2: completions credit point VALUE"
        assert ev["evidence"], "GG-4: evidence never empty"
        assert ev["mock_data"] is True, "OR-5: synthetic label rides on events"

    unlocks = _by_type(log, "unlock")
    assert len(unlocks) == 1 and unlocks[0]["subject"]["task_id"] == G
    assert unlocks[0]["subject"]["count"] == 5
    assert sorted(unlocks[0]["subject"]["unlocked"]) == S

    keystones = _by_type(log, "keystone-cleared")
    assert len(keystones) == 1 and keystones[0]["subject"]["count"] == 5
    assert config.KEYSTONE_MIN_UNLOCKED <= 5


def test_event_determinism_and_idempotence(badge_world):
    """GG-6/X3-1: 'identical inputs => identical events; re-deriving over
    the same snapshot pair yields identical event ids (idempotent
    re-publish)' — a second derivation appends NOTHING."""
    first = progression.derive_events(badge_world["summary_a"], badge_world["snap_b"])
    second = progression.derive_events(badge_world["summary_a"], badge_world["snap_b"])
    assert first == second, "same inputs must yield byte-identical events"
    assert [ev["event_id"] for ev in first] == [ev["event_id"] for ev in second]

    log = badge_world["log"]
    before = len(log)
    appended = progression.append_events(log, first)
    assert appended == [], "append is idempotent on event_id (append-only log)"
    assert len(log) == before


def test_goal_crossed_and_streak_extended(badge_world):
    """PSY-2 milestones + streak events: attainment crossing each §10
    milestone fires goal-crossed exactly once per (team, slot, milestone);
    reaching full attainment fires streak-extended."""
    log = badge_world["log"]
    milestones = sorted(
        ev["subject"]["milestone"]
        for ev in _by_type(log, "goal-crossed")
        if ev["team"] == "T01" and ev["day"] == 0 and ev["shift"] == 1
    )
    assert milestones == sorted(config.GOAL_MILESTONES), (
        "each milestone fires exactly once across the two derivations"
    )
    extended = _by_type(log, "streak-extended")
    assert len(extended) == 1
    assert extended[0]["subject"]["streak"] == 1


def test_recovery_moment_from_commitments_change_log():
    """PSY-8 (quoted law): 'emotional peak = economic peak — recovery-moment
    fires ONLY from the commitments change log (an improved committed_day),
    never from a locally recomputed projection.' An improving row of the
    CURRENT run emits the event; a worsening row emits nothing."""
    fleet = _badge_fleet()
    cpm, sched = run_schedule(fleet)
    snap_a = build_snapshot(fleet, sched, cpm)
    summary_a = progression.snapshot_summary(snap_a)

    snap_b = build_snapshot(fleet, sched, cpm)
    snap_b["commitments"] = {
        "run_id": "run2",
        "changes": [
            {"run": "run2", "aircraft": 1, "old": 20, "new": 16, "delta_days": -4,
             "reason": "schedule improvement (sustained 2 replans)"},
            {"run": "run2", "aircraft": 2, "old": 10, "new": 18, "delta_days": 8,
             "reason": "blocked_parts=1"},
            {"run": "run1", "aircraft": 3, "old": 9, "new": 5, "delta_days": -4,
             "reason": "schedule improvement"},  # older run: already published
        ],
        "committed": [],
    }
    events = progression.derive_events(summary_a, snap_b)
    moments = [ev for ev in events if ev["event_type"] == "recovery-moment"]
    assert len(moments) == 1, "only the CURRENT run's improving row fires"
    assert moments[0]["aircraft"] == 1
    assert moments[0]["subject"]["delta_days"] == -4
    assert "Recovery secured" in moments[0]["evidence"]


# ---------------------------------------------------------------------------
# badges (GG-4: every badge names its earning event)
# ---------------------------------------------------------------------------


def test_badge_firebreak(badge_world):
    """Firebreak: 'keystone unlocking >= 5 downstream' (X2 catalog) — the
    gate that freed 5 successors earns it ONCE, naming the real
    keystone-cleared event (GG-4)."""
    log = badge_world["log"]
    rows = _badges(log, "firebreak")
    assert len(rows) == 1, "awarded exactly once (dedupe across derivations)"
    keystone_ids = {ev["event_id"] for ev in _by_type(log, "keystone-cleared")}
    assert set(rows[0]["subject"]["earning_event_ids"]) <= keystone_ids
    assert rows[0]["subject"]["earning_event_ids"], "GG-4: names its earning event"
    assert rows[0]["team"] == "T01"


def test_badge_recovery(badge_world):
    """Recovery: 'closed a task on an aircraft late/blocked entering the
    shift' — R was blocked in the previous summary and done now; its
    completion event (recovery flag) is the named earning event (GG-4)."""
    log = badge_world["log"]
    rows = _badges(log, "recovery")
    assert len(rows) == 1
    earning = rows[0]["subject"]["earning_event_ids"]
    completions = {
        ev["event_id"]: ev for ev in _by_type(log, "completion-credited")
    }
    assert len(earning) == 1 and earning[0] in completions
    assert completions[earning[0]]["subject"]["task_id"] == R
    assert completions[earning[0]]["subject"]["recovery"] is True


def test_badge_clean_sweep(badge_world):
    """Clean Sweep: '100% of planned critical tasks done in a shift' — the
    badge fires only once EVERY planned critical task of the slot is done
    (>= CLEAN_SWEEP_MIN_CRITICAL blocks the vacuous-100% gotcha), naming
    the completion events that earned it (GG-4)."""
    log = badge_world["log"]
    rows = _badges(log, "clean_sweep")
    assert len(rows) == 1
    row = rows[0]
    assert (row["team"], row["day"], row["shift"]) == ("T01", 0, 1)
    assert sorted(row["subject"]["critical_done"]) == sorted([G, R] + S)
    completion_ids = {ev["event_id"] for ev in _by_type(log, "completion-credited")}
    assert set(row["subject"]["earning_event_ids"]) <= completion_ids
    assert len(row["subject"]["earning_event_ids"]) == 7

    # Not awarded before the last critical task was done: the badge appears
    # only after the third advance (snap_c) — its earning ids include S-task
    # completions, which only exist in the c-derivation.
    s_completions = {
        ev["event_id"]
        for ev in _by_type(log, "completion-credited")
        if ev["subject"]["task_id"] in S
    }
    assert s_completions & set(row["subject"]["earning_event_ids"])


def test_badge_flow_keeper_and_self_caused_forfeit(badge_world):
    """Flow Keeper: 'a week with zero self-caused SAME_TEAM_PREDECESSOR /
    DURATION_OVERRUN excusals' — earned on the clean fixture week; NOT
    earned while a same-team predecessor wait (self-caused) exists in the
    week. Absorbed (excusable) causes never count (GG-3 carry-over)."""
    # Positive: the badge_world week ends with zero self-caused records.
    rows = _badges(badge_world["log"], "flow_keeper")
    assert len(rows) == 1 and rows[0]["team"] == "T01"
    assert rows[0]["subject"]["earning_event_ids"], "GG-4"

    # Negative: an OPEN same-team-gated chain (C2 waits on own team's C1)
    # is a self-caused record in the week -> no Flow Keeper, even though a
    # completion (D) was credited.
    c1, c2, d = "0002-T00001", "0002-T00002", "0002-T00003"
    mechanics = [mk_mech("T01-S1-M001", skills=["COMMON"]),
                 mk_mech("T01-S1-M002", skills=["COMMON"])]
    tasks = [
        mk_task(c1, aircraft=2, dur=60, skill="COMMON"),
        mk_task(c2, aircraft=2, dur=60, skill="COMMON", preds=[c1]),
        mk_task(d, aircraft=2, dur=60, skill="COMMON"),
    ]
    fleet = mk_fleet(tasks, mechanics, aircraft=[mk_aircraft(2, deadline=30)])
    cpm, sched = run_schedule(fleet)
    snap_a = build_snapshot(fleet, sched, cpm)
    summary_a = progression.snapshot_summary(snap_a)
    by_id = {t.task_id: t for t in fleet.tasks}
    by_id[d].state = "done"
    # C1 stays in_progress: C2 now WAITS on its own team's unfinished work.
    by_id[c1].state = "in_progress"
    snap_b = build_snapshot(fleet, sched, cpm)
    log: list = []
    progression.append_events(log, progression.derive_events(summary_a, snap_b))
    assert _by_type(log, "completion-credited"), "a completion exists in the week"
    badge_events, _rates = progression.evaluate_badges(log, snap_b)
    assert not [
        ev for ev in badge_events if ev["subject"]["badge"] == "flow_keeper"
    ], "a self-caused (SAME_TEAM_PREDECESSOR) excusal forfeits Flow Keeper"


def test_badge_earn_rate_monitor(badge_world):
    """X2 obligation: 'earn-rate budgets are monitored ... badge inflation is
    a design failure, surfaced, never silently accepted' — the report
    counts every earned badge against its §10 budget and carries the
    placeholder label (OR-5)."""
    _new, rates = progression.evaluate_badges(
        badge_world["log"], badge_world["snap_c"]
    )
    assert rates["source"] == "config-defaults"
    assert set(rates["badges"]) == set(progression.BADGE_IDS)
    for badge in progression.BADGE_IDS:
        entry = rates["badges"][badge]
        assert set(entry) == {"count", "budget_per_team_week", "expected", "alert"}
    assert rates["badges"]["firebreak"]["count"] == 1
    # Inflation tripwire: a synthetic pile of duplicate-key-free badge
    # events over the same tiny fixture MUST raise the alert.
    inflated = list(badge_world["log"])
    for i in range(20):
        fake = dict(inflated[-1])
        fake = {
            **fake,
            "event_id": f"x{i}",
            "event_type": "badge-earned",
            "subject": {"badge": "recovery", "badge_key": f"recovery:T01:x{i}",
                        "earning_event_ids": ["e"]},
        }
        inflated.append(fake)
    assert progression.earn_rate_report(inflated, badge_world["snap_c"])["badges"][
        "recovery"
    ]["alert"] is True


# ---------------------------------------------------------------------------
# streaks (GG-3 tripwire)
# ---------------------------------------------------------------------------

X0, X1, XB, X3 = (f"0004-T{n:05d}" for n in range(1, 5))


def _streak_fleet():
    """Four single-task day-slices for T01: day 0, day 1, day 2 (blocked on
    parts — the excused-heavy shift), day 3."""
    mechanics = [mk_mech("T01-S1-M001", skills=["COMMON"]),
                 mk_mech("T01-S1-M002", skills=["COMMON"])]
    tasks = [
        mk_task(X0, aircraft=4, dur=60, skill="COMMON", earliest=0),
        mk_task(X1, aircraft=4, dur=60, skill="COMMON", earliest=1),
        mk_task(XB, aircraft=4, dur=60, skill="COMMON", earliest=2,
                state="blocked", parts_eta=2),
        mk_task(X3, aircraft=4, dur=60, skill="COMMON", earliest=3),
    ]
    fleet = mk_fleet(tasks, mechanics, aircraft=[mk_aircraft(4, deadline=30)])
    cpm, sched = run_schedule(fleet)
    for tid, day in ((X0, 0), (X1, 1), (XB, 2), (X3, 3)):
        assert (sched.assignments[tid].day, sched.assignments[tid].shift) == (day, 1)
    return fleet, cpm, sched


def test_streak_pauses_on_excused_shift_never_breaks():
    """GG-3 (quoted law): 'excused-heavy shifts (goal 0 after excusals)
    PAUSE never break' — canon fixture 100%, 100%, excused-miss, 100%
    yields streak 3 and alive (GAMES catalog mechanic 2: 'Excused shifts
    don't break a streak'). Day 2's only planned task is blocked-on-parts
    (auto LATE_PART, excusable) so its goal is 0 after excusals."""
    fleet, cpm, sched = _streak_fleet()
    by_id = {t.task_id: t for t in fleet.tasks}
    for tid in (X0, X1, X3):
        by_id[tid].state = "done"
    snap = build_snapshot(fleet, sched, cpm)

    day2 = shift_report(snap, "T01", 2, 1)
    assert day2["goal"] == 0 and day2["excused_points"] > 0, (
        "precondition: day 2 is excused-heavy (goal 0 after excusals)"
    )

    streak = progression.compute_streak(snap, "T01")
    assert streak["streak"] == 3, "100%, 100%, excused-miss, 100% => streak 3"
    assert streak["alive"] is True
    assert streak["paused_shifts"] == 1
    assert streak["shifts_counted"] == 3


def test_streak_broken_by_unexcused_miss():
    """GG-3 boundary: 'only an UNEXCUSED sub-100% shift breaks a streak' —
    with day 3's task NOT done (and not excused), the walk ends at 0."""
    fleet, cpm, sched = _streak_fleet()
    by_id = {t.task_id: t for t in fleet.tasks}
    for tid in (X0, X1):
        by_id[tid].state = "done"
    # X3 stays not_started: an unexcused miss on day 3.
    snap = build_snapshot(fleet, sched, cpm)
    streak = progression.compute_streak(snap, "T01")
    assert streak["streak"] == 0 and streak["alive"] is False
    # Bounded walk: up to day 1 the streak was 2 and alive.
    upto = progression.compute_streak(snap, "T01", upto=(1, 1))
    assert upto["streak"] == 2 and upto["alive"] is True


def test_slot_stats_matches_shift_report():
    """Drift tripwire: the one-pass slot aggregate MIRRORS
    points.shift_report's GG-3 goal math — equality pinned per slot."""
    fleet, cpm, sched = _streak_fleet()
    by_id = {t.task_id: t for t in fleet.tasks}
    by_id[X0].state = "done"
    snap = build_snapshot(fleet, sched, cpm)
    slots = progression.slot_stats(snap)
    for day in (0, 1, 2, 3):
        report = shift_report(snap, "T01", day, 1)
        slot = slots[("T01", day, 1)]
        assert slot["goal"] == report["goal"], day
        assert slot["earned"] == report["earned"], day
        assert slot["excused_points"] == report["excused_points"], day
        assert slot["attainment"] == report["attainment"], day


# ---------------------------------------------------------------------------
# recap cards (LB-8)
# ---------------------------------------------------------------------------


def test_recap_card_content(badge_world):
    """LB-8 (quoted law): 'every surface renders its deterministic content
    when the LLM is unavailable — the recap card is the canonical example'.
    build_recap is deterministic, LLM-free by construction, top plays are
    ranked BY POINT VALUE (GG-2), and the card carries badges, streak,
    points banked and the mock label (OR-5)."""
    snap = badge_world["snap_c"]
    log = badge_world["log"]
    recap = progression.build_recap("T01", 0, 1, snap, log)

    assert recap["generated_by"] == "deterministic", "LB-8: no LLM anywhere"
    assert recap["mock_data"] is True, "OR-5 label"
    assert recap["attainment"] == 1.0
    assert recap["goal"] > 0 and recap["earned"] == recap["goal"]

    plays = recap["top_plays"]
    assert 1 <= len(plays) <= 5
    points_order = [p["points"] for p in plays]
    assert points_order == sorted(points_order, reverse=True), (
        "GG-2: top plays ranked by point value, never task count/order"
    )
    for play in plays:
        assert play["explanation"], "GG-4: every play stays explainable"

    assert {b["badge"] for b in recap["badges"]} >= {"clean_sweep", "firebreak"}
    assert recap["streak"]["streak"] == 1
    completions = _by_type(log, "completion-credited")
    assert recap["points_banked"] == sum(
        int(round(ev["points_delta"])) for ev in completions
    )
    # Determinism: same inputs => same card.
    assert progression.build_recap("T01", 0, 1, snap, log) == recap


def test_team_level_pure_fold(badge_world):
    """PSY-4 (quoted law): 'XP reflects cumulative validated contribution —
    never purchasable, never decaying' — team XP is a pure fold over
    completion-credited events; more credited events never lower it."""
    log = badge_world["log"]
    level = progression.team_level(log, "T01")
    assert level["xp"] > 0 and level["level"] >= 1
    assert level["curve_source"] == "config-defaults"  # OR-5 placeholder label
    # Folding over a PREFIX of the log never yields more XP than the whole:
    prefix = progression.team_level(log[: len(log) // 2], "T01")
    assert prefix["xp"] <= level["xp"], "XP only accumulates (no decay path)"


# ---------------------------------------------------------------------------
# persistence (append-only jsonl, atomic)
# ---------------------------------------------------------------------------


def test_events_persist_roundtrip(badge_world, tmp_path):
    """Append-only persistence: persist -> load reproduces the log verbatim
    (order kept, nothing mutated); re-appending loaded events is a no-op."""
    log = badge_world["log"]
    path = str(tmp_path / "game_events.jsonl")
    progression.persist_events(log, path)
    loaded = progression.load_events(path)
    assert loaded == log
    assert progression.append_events(list(loaded), loaded) == []


# ---------------------------------------------------------------------------
# leaderboard re-assertion (GG-2) + needs-support framing (PSY-5)
# ---------------------------------------------------------------------------


def test_leaderboard_never_raw_points_and_needs_support():
    """GG-2 re-assertion + PSY-5: rows rank by (attainment, efficiency,
    difficulty tie-break) and NEVER carry raw goal/earned point totals;
    the bottom team (attainment < NEEDS_SUPPORT_ATTAINMENT with a nonzero
    goal) is framed 'needs support' WITH its excusal context — never
    shamed, never eliminated."""
    mechanics = [
        mk_mech("T01-S1-M001", skills=["COMMON"]),
        mk_mech("T01-S1-M002", skills=["COMMON"]),
        mk_mech("T02-S1-M001", team="T02", skills=["COMMON"]),
        mk_mech("T02-S1-M002", team="T02", skills=["COMMON"]),
    ]
    tasks = [
        mk_task("0005-T00001", aircraft=5, dur=60, skill="COMMON"),
        mk_task("0005-T00002", aircraft=5, team="T02", dur=60, skill="COMMON"),
        mk_task("0005-T00003", aircraft=5, team="T02", dur=60, skill="COMMON",
                state="blocked", parts_eta=0),
    ]
    fleet = mk_fleet(tasks, mechanics, aircraft=[mk_aircraft(5, deadline=30)])
    cpm, sched = run_schedule(fleet)
    by_id = {t.task_id: t for t in fleet.tasks}
    by_id["0005-T00001"].state = "done"  # T01 finishes; T02 earns nothing
    snap = build_snapshot(fleet, sched, cpm)

    rows = leaderboard(snap, 0)
    assert [r["team"] for r in rows] == ["T01", "T02"]
    for row in rows:
        assert not ({"goal", "earned", "points", "raw_points"} & set(row)), (
            "GG-2: raw point totals never leak into leaderboard rows"
        )
        assert {"attainment", "efficiency", "difficulty", "rank"} <= set(row)
    t02 = rows[1]
    assert t02["attainment"] < config.NEEDS_SUPPORT_ATTAINMENT
    assert t02["needs_support"] is True
    assert t02["support"]["framing"] == "needs support"
    causes = {c["cause"] for c in t02["support"]["top_causes"]}
    assert "LATE_PART" in causes, "PSY-5: excusal context attached, not blame"
    assert rows[0]["needs_support"] is False and rows[0]["support"] is None


# ---------------------------------------------------------------------------
# API scoping (GG-1 read-only + RS rules) — app booted on the mini fleet
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(fleet, tmp_path_factory):
    """Flask app on the generated mini fleet, ALL persistence redirected to
    a temp dir (incl. FF_GAME_EVENTS) so the repo tree stays clean."""
    from ff.data.loader import save_json_gz

    base = tmp_path_factory.mktemp("ff_progression_api")
    fixture_path = base / "fleet-mini.json.gz"
    save_json_gz(fleet.to_dict(), str(fixture_path))
    (base / "data").mkdir(exist_ok=True)

    old_cwd = os.getcwd()
    keys = ("FF_DATA", "FF_ENV", "FF_SCHEDULE", "FF_ACTUALS", "FF_COMMITMENTS",
            "FF_EXCUSALS", "FF_GAME_EVENTS", "FF_ENVELOPE_DIR")
    old_env = {k: os.environ.get(k) for k in keys}
    os.chdir(base)
    os.environ["FF_DATA"] = str(fixture_path)
    os.environ["FF_ACTUALS"] = str(base / "data" / "actuals.json")
    os.environ["FF_COMMITMENTS"] = str(base / "data" / "commitments.json")
    os.environ["FF_EXCUSALS"] = str(base / "data" / "excusals.json")
    os.environ["FF_GAME_EVENTS"] = str(base / "data" / "game_events.jsonl")
    # INCREMENT 6: replans auto-export max_v1 envelopes — temp base too.
    os.environ["FF_ENVELOPE_DIR"] = str(base / "schedules")
    os.environ["FF_ENV"] = "dev"
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


def _login(client, role, scope):
    resp = client.post("/login", json={"role": role, "scope": scope},
                       follow_redirects=True)
    assert resp.status_code < 400, f"{role}/{scope} must log in"


def test_progression_api_scope_enforced(client, fleet):
    """§G8/RS rules: a mechanic asking for ANOTHER team's progression or
    recap gets 403 (the same tamper check as every scoped route); their own
    team answers 200 with streak/badges/level."""
    mech = sorted(fleet.mechanics, key=lambda m: m.mech_id)[0]
    other = sorted({t.team for t in fleet.tasks} - {mech.team})[0]
    _login(client, "mechanic", mech.mech_id)

    denied = client.get(f"/api/v1/progression/team/{other}")
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "forbidden_scope"
    denied_recap = client.get(f"/api/v1/recap?team={other}")
    assert denied_recap.status_code == 403

    ok = client.get(f"/api/v1/progression/team/{mech.team}")
    assert ok.status_code == 200
    body = ok.get_json()
    assert set(body) >= {"team", "streak", "badges", "level", "mock_data"}
    assert body["team"] == mech.team
    assert body["mock_data"] is True  # OR-5 label on the payload

    unknown = client.get("/api/v1/progression/team/T99")
    assert unknown.status_code == 404


def test_recap_api_and_events_feed_scoped(client, fleet):
    """LB-8 + GG-1: the recap API returns the deterministic card; the events
    feed is read-only and server-side filtered — a scoped role sees only
    its own team's events (plus fleet-level '' rows)."""
    team = sorted({t.team for t in fleet.tasks})[0]
    _login(client, "lead", team)

    recap = client.get(f"/api/v1/recap?team={team}&day=0&shift=1")
    assert recap.status_code == 200
    card = recap.get_json()["recap"]
    assert card["generated_by"] == "deterministic"  # LB-8: no LLM in the path
    assert set(card) >= {"attainment", "goal", "earned", "top_plays", "badges",
                         "streak", "recovery_moments", "points_banked"}

    feed = client.get("/api/v1/progression/events?limit=100")
    assert feed.status_code == 200
    body = feed.get_json()
    assert body["read_only"] is True
    for ev in body["events"]:
        assert ev["team"] in ("", team), "scoped feed leaks another team's events"


def test_progression_api_is_get_only(client, app, fleet):
    """GG-1 route audit (quoted law): 'all read-only ... no user-generated
    events' — every /progression and /recap rule accepts GET only; POSTing
    an event is 405, never stored."""
    team = sorted({t.team for t in fleet.tasks})[0]
    _login(client, "lead", team)
    for path in ("/api/v1/progression/events",
                 f"/api/v1/progression/team/{team}",
                 "/api/v1/recap"):
        resp = client.post(path, json={"event_type": "completion-credited"})
        assert resp.status_code == 405, f"{path} must refuse writes (GG-1)"
    for rule in app.url_map.iter_rules():
        if "/progression" in rule.rule or rule.rule.endswith("/recap"):
            methods = set(rule.methods or ()) - {"HEAD", "OPTIONS"}
            assert methods == {"GET"}, f"{rule.rule} exposes {methods} (GG-1)"


def test_events_derive_after_actuals_replan(client, fleet):
    """End-to-end GG-1 loop: reporting a real completion (OR-6 write-path)
    then replanning derives a completion-credited event into the feed —
    the game state moved because REAL work moved, with no game write."""
    roots = sorted(t.task_id for t in fleet.tasks
                   if not t.predecessors and t.state == "not_started")
    tid = roots[-1]
    team = next(t.team for t in fleet.tasks if t.task_id == tid)
    _login(client, "lead", team)
    done = client.post("/api/v1/actuals", json={"task_id": tid, "state": "done"})
    assert done.status_code < 300
    replan = client.post("/api/v1/replan")
    assert replan.status_code < 300
    feed = client.get("/api/v1/progression/events?limit=500")
    assert feed.status_code == 200
    credited = [
        ev for ev in feed.get_json()["events"]
        if ev["event_type"] == "completion-credited"
        and ev["subject"].get("task_id") == tid
    ]
    assert len(credited) == 1, "the real completion was credited exactly once"
