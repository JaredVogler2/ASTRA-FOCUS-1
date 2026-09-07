"""Origin-aware rework excusability (owner directive: "rework caused by
the team" is NOT an excuse) + cascade/gate-health record semantics.
"""

from __future__ import annotations

import pytest

import config
from ff.services import disruption, scorecard as sc
from tests.conftest import mk_fleet, mk_mech, mk_task
from ff.engine.cpm import compute_cpm
from ff.engine.scheduler import build_schedule
from ff.services.snapshot import build_snapshot


def _snap_with_rework(origin: str | None):
    tasks = [
        mk_task("0001-T00001", dur=60),
        mk_task(
            "0001-T90001RWK",
            dur=60,
            preds=["0001-T00001"],
        ),
    ]
    rwk = tasks[1]
    rwk.is_rework = True
    rwk.rework_origin_team = origin
    fleet = mk_fleet(tasks, [mk_mech("T01-S1-M001"), mk_mech("T01-S1-M002")])
    cpm = compute_cpm(fleet.tasks)
    schedule = build_schedule(fleet, cpm)
    return build_snapshot(fleet, schedule, cpm)


def _rwk_record(snap):
    recs = [
        r
        for r in disruption.attribute(snap)
        if r["cause"] == disruption.REWORK_INJECTION
    ]
    assert len(recs) == 1
    return recs[0]


def test_cross_team_origin_rework_is_excusable():
    rec = _rwk_record(_snap_with_rework("T09"))
    assert rec["excusable"] is True
    assert "origin T09" in rec["evidence"]


def test_self_caused_rework_is_owned():
    """The team's own defect origin never excuses the injected work."""
    rec = _rwk_record(_snap_with_rework("T01"))  # mk_task team is T01
    assert rec["excusable"] is False
    assert "own team" in rec["evidence"]


def test_missing_origin_field_derives_from_parent_soi():
    """Rows written before the field existed still follow the paperwork
    rule: origin is DERIVED from the parent SOI's team. Here the parent
    is the task's own team -> owned; the evidence says it was derived."""
    rec = _rwk_record(_snap_with_rework(None))
    assert rec["excusable"] is False
    assert "derived from parent SOI" in rec["evidence"]
    assert config.REWORK_UNATTRIBUTED_EXCUSABLE is False  # own-it default
    # And with a CROSS-team parent, derivation excuses the fixer.
    from ff.services.disruption import rework_origin_of

    snap = _snap_with_rework(None)
    from ff.services.snapshot import tasks_by_id

    tasks = tasks_by_id(snap)
    tasks["0001-T00001"].team = "T09"  # parent SOI belongs to another team
    snap.pop("_excusals_auto", None)
    rec = _rwk_record(snap)
    assert rec["excusable"] is True
    assert rework_origin_of(tasks["0001-T90001RWK"], tasks) == "T09"


def test_manual_rework_capture_cannot_excuse_self_caused():
    """A lead typing REWORK_INJECTION into the capture form cannot excuse
    self-caused rework — merged_excusals re-derives from the origin.

    The rework task is IN_PROGRESS so the auto attribution does not fire
    (auto requires not_started) and the manual record survives the
    same-(task, cause) dedup instead of collapsing into auto.
    """
    snap = _snap_with_rework("T01")
    from ff.services.snapshot import tasks_by_id

    tasks_by_id(snap)["0001-T90001RWK"].state = "in_progress"
    snap.pop("_excusals_auto", None)  # drop memo built before the tweak
    manual = [
        {
            "task_id": "0001-T90001RWK",
            "cause": disruption.REWORK_INJECTION,
            "team": "T01",
            "day": 0,
            "shift": 2,
            "evidence": "lead says rework",
        }
    ]
    merged = disruption.merged_excusals(snap, manual)
    manual_recs = [
        r
        for group in merged.values()
        for r in group
        if r["source"] == "manual"
    ]
    assert manual_recs and all(r["excusable"] is False for r in manual_recs)


# ---------------------------------------------------------------------------
# gate-health record + aggregation semantics
# ---------------------------------------------------------------------------


def test_cascade_field_validation_and_aggregation():
    def R(**kw):
        base = dict(
            round_no=1, day=10, shift=1, team="T01", aircraft=1,
            station="P01", points=100, planned=True, done=False,
            excused=False,
        )
        base.update(kw)
        return sc.make_record(**base)

    with pytest.raises(ValueError):
        R(cascade="nonsense")

    recs = [
        R(behind_days=3, cascade="primary"),
        R(aircraft=2, behind_days=6, cascade="same_team", points=50),
        R(aircraft=3, behind_days=2, cascade="cross_team", points=40),
        R(aircraft=4, behind_days=4, done=True, points=70),  # burned down
        R(aircraft=5),  # on schedule
    ]
    row = sc.aggregate(recs, "fleet", "day")[0]
    assert row["behind_open_points"] == 190  # 100 + 50 + 40
    assert row["behind_done_points"] == 70
    assert row["behind_avg_age"] == round((3 + 6 + 2) / 3, 2)
    assert row["behind_primary_n"] == 1
    assert row["behind_same_team_n"] == 1
    assert row["behind_cross_team_n"] == 1
