"""INCREMENT 6 Part A — adapter-compat contract tests for ff.export.envelope.

THE contract (ARCHITECTURE.md INCREMENT 6 addendum): the exported
``max_v1_*.json.gz`` envelope must load THROUGH the vendored dashboard's
own ``web_flask/src/max_adapter.py`` (sys.path trick below) and come out
fully adapted — is_max_envelope detected, adaptation succeeds, task count
matches, priority ranks assigned, products/aircraft_status coherent,
metadata.stats blocks present. The vendored adapter is NEVER modified to
make these pass; envelope problems are exporter bugs (acceptance rule 5).
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone

import pytest

from tests.conftest import FF_ROOT
from ff.data.loader import save_json_gz
from ff.export.envelope import (
    REFERENCE_DATE,
    build_bems_map,
    build_envelope,
    export_envelope,
    update_commitments_for_cli,
)

WEB_FLASK_ROOT = os.path.join(FF_ROOT, "web_flask")

# Fixed wall-clock for deterministic filenames/stamps in every test here.
NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _adapter():
    """Import the VENDORED adapter via the sys.path trick (web_flask root
    first so ``src.*`` resolves inside the vendored tree)."""
    if WEB_FLASK_ROOT not in sys.path:
        sys.path.insert(0, WEB_FLASK_ROOT)
    import src.max_adapter as max_adapter  # noqa: PLC0415

    return max_adapter


@pytest.fixture(scope="module")
def envelope_path(fleet, schedule, cpm, tmp_path_factory):
    """One exported mini-fleet envelope shared by the read-only tests."""
    out_dir = tmp_path_factory.mktemp("envelope")
    return export_envelope(fleet, schedule, cpm, out_dir=out_dir, now=NOW)


@pytest.fixture(scope="module")
def adapted(envelope_path):
    """The envelope AFTER the vendored adapter's normalize_envelope."""
    return _adapter().load_envelope(str(envelope_path))


# ---------------------------------------------------------------------------
# file naming / discovery compatibility
# ---------------------------------------------------------------------------


def test_filename_matches_dashboard_discovery_glob(envelope_path):
    """find_available_schedules (vendored src/app.py) discovers max_v1_*
    .json.gz first-class; the exporter's filename must match that glob AND
    the {YYYYMMDD}_S{shift}_{HHMMSS}_UTC shape."""
    name = os.path.basename(str(envelope_path))
    assert name == "max_v1_20260711_S1_120000_UTC.json.gz"
    assert name.startswith("max_v1_") and name.endswith(".json.gz")


def test_export_is_deterministic_bytes(fleet, schedule, cpm, tmp_path):
    """Identical inputs + identical ``now`` => byte-identical envelopes
    (loader.save_json_gz: sorted keys, gzip mtime=0) — the determinism law
    extended to the export surface."""
    p1 = export_envelope(fleet, schedule, cpm, out_dir=tmp_path / "a", now=NOW)
    p2 = export_envelope(fleet, schedule, cpm, out_dir=tmp_path / "b", now=NOW)
    assert p1.read_bytes() == p2.read_bytes()


# ---------------------------------------------------------------------------
# adapter-compat contract (the addendum's named assertions)
# ---------------------------------------------------------------------------


def test_is_max_envelope_detected_on_raw_file(envelope_path):
    """Detection signal: mechanic_timelines is a DICT keyed by mechanic id
    (the vendored is_max_envelope rule) on the RAW on-disk envelope."""
    with gzip.open(str(envelope_path), "rt", encoding="utf-8") as f:
        raw = json.load(f)
    assert isinstance(raw["mechanic_timelines"], dict)
    assert _adapter().is_max_envelope(raw) is True


def test_adapt_succeeds_and_task_count_matches(adapted, schedule):
    """load_envelope (load + normalize + adapt) succeeds and the adapted
    task count equals scheduled + unscheduled — every open task exported,
    none invented."""
    assert adapted is not None
    expected = len(schedule.assignments) + len(schedule.unscheduled)
    assert len(adapted["tasks"]) == expected
    assert adapted["metadata"]["adapted_from"] == "max"


def test_priority_ranks_assigned(adapted):
    """The adapter assigns a global priority rank (1 = most critical) from
    cpm_priority/cpm_slack — every exported task must carry the inputs, so
    every adapted task gets a unique integer rank."""
    prios = [t.get("priority") for t in adapted["tasks"]]
    assert all(isinstance(p, int) and p >= 1 for p in prios)
    assert len(set(prios)) == len(prios)
    # priority_score synthesized from the exported cpm_priority.
    assert all(isinstance(t.get("priority_score"), float) for t in adapted["tasks"])


def test_products_and_aircraft_status_coherent(adapted, fleet, schedule):
    """Products cards + aircraft_status must cover exactly the aircraft
    that have exported tasks, with the day-level on-time verdict agreeing
    with the engine's stats (deadline source of truth = the fleet)."""
    lines_with_tasks = {int(t["line_number"]) for t in adapted["tasks"]}
    product_lines = {int(p["line_number"]) for p in adapted["products"]}
    status_lines = {int(a["line_number"]) for a in adapted["aircraft_status"]}
    assert product_lines == lines_with_tasks
    assert status_lines == lines_with_tasks
    deadlines = {a.aircraft: a.delivery_deadline_day for a in fleet.aircraft}
    for p in adapted["products"]:
        line = int(p["line_number"])
        assert p["totalTasks"] == sum(
            1 for t in adapted["tasks"] if int(t["line_number"]) == line
        )
        # A product whose completion (incl. honest fallback rows at horizon
        # end) is past its deadline must NOT read on-time.
        completion = max(
            int(t["day"]) for t in adapted["tasks"] if int(t["line_number"]) == line
        )
        if completion > deadlines[line]:
            assert p["onTime"] is False


def test_stats_blocks_present_for_insight_tabs(adapted):
    """metadata.stats must feed the vendored insight blueprints
    (projection.py: economics/projection/capacity_pressure + stability
    counters; the exact keys those endpoints read)."""
    stats = adapted["metadata"]["stats"]
    econ = stats["economics"]
    assert econ["source"] == "config-defaults"  # OR-5 placeholder label
    assert set(econ["fleet"]) >= {
        "aircraft_count", "late_count", "total_penalty_usd",
        "unavoidable_penalty_usd", "controllable_penalty_usd",
    }
    assert all("line_number" in r for r in econ["per_aircraft"])
    cap = stats["capacity_pressure"]
    assert "pools" in cap and "linesAnalyzed" in cap
    for pool in cap["pools"]:
        assert set(pool) >= {"team", "shift", "waitDays", "dollarDays",
                             "aircraftTouched", "topSkills"}
    proj = stats["projection"]
    assert proj["stationFeed"] is False
    assert set(proj) >= {"runId", "committed", "earlyFlow", "delivered",
                         "changes"}
    assert "commitment_kept" in stats and "commitment_in_horizon" in stats
    assert "final_lateness" in stats and "otd" in stats
    assert stats["mock_data"] is True  # OR-5: synthetic, labeled


def test_adapter_synthesized_surfaces(adapted, fleet):
    """Post-adaptation surfaces the 17 tabs read: shift-aware teamSkill,
    integer per-pool mechanic_id + bems_id, list mechanic_timelines,
    seat-count teamCapacities, staffing rows, utilization pools."""
    t = adapted["tasks"][0]
    team, shift, skill = t["team"], t["shift"], t["skill"]
    assert t["teamSkill"] == f"{team} S{shift} ({skill})"
    staffed = [t for t in adapted["tasks"] if not t.get("isFallback")]
    assert all(isinstance(t["mechanic_id"], int) for t in staffed)
    assert all(str(t["bems_id"]).isdigit() for t in staffed)
    assert isinstance(adapted["mechanic_timelines"], list)  # FGI pool shape
    # Roster seats, not seat-days: every (team, shift) roster pool must be
    # keyed "{team} S{shift} (ANY)" (FF teams are multi-skill => the
    # adapter's ANY tag) with the EXACT roster headcount. totalWorkforce is
    # roster + the adapter's deliberate 1-seat padding for scheduled
    # skill-pools without a roster record (upstream behavior, identical on
    # MAX production envelopes — never "fixed" in the vendored adapter).
    roster: dict[tuple, int] = {}
    for m in fleet.mechanics:
        roster[(m.team, m.shift)] = roster.get((m.team, m.shift), 0) + 1
    caps = adapted["teamCapacities"]
    for (team, shift), count in roster.items():
        assert caps[f"{team} S{shift} (ANY)"] == count
    assert adapted["totalWorkforce"] >= len(fleet.mechanics)
    padding = sum(1 for k, v in caps.items() if k.split("(")[1] != "ANY)" and v == 1)
    assert adapted["totalWorkforce"] == len(fleet.mechanics) + padding
    assert adapted["staffing_requirements"], "staffing rows must exist"
    assert adapted["utilization"], "utilization pools must exist"


def test_myday_bems_grammar_and_roundtrip(envelope_path, fleet):
    """The vendored My Day tab lists only ALL-DIGIT mechanic ids
    (_is_real_mech = str.isdigit). Every exported crew id must satisfy
    that grammar, be unique per roster member, and translate back to the
    FF mech id through ff_mechanic_map (the Part-C write-back path)."""
    with gzip.open(str(envelope_path), "rt", encoding="utf-8") as f:
        raw = json.load(f)
    bems_map = build_bems_map(fleet)
    assert len(set(bems_map.values())) == len(fleet.mechanics)
    assert all(b.isdigit() for b in bems_map.values())
    inverse = raw["ff_mechanic_map"]
    for t in raw["tasks"]:
        for bems, ff_id in zip(t["mechanicIds"], t["ff_mechanic_ids"]):
            assert bems.isdigit()
            assert inverse[bems] == ff_id
            assert bems_map[ff_id] == bems


def test_unscheduled_exported_as_honest_fallback(fleet, schedule, cpm):
    """Unplaceable work is NEVER smeared onto day 0 (the adapter coerces a
    missing day to 0): it exports at horizon end with isFallback=True and
    the FULL crewShortfall — the C20-style honesty flag (OR-1: a short
    crew is never represented, so it can never be booked)."""
    if not schedule.unscheduled:
        pytest.skip("mini schedule placed everything — no fallback rows")
    env = build_envelope(fleet, schedule, cpm, now=NOW)
    by_id = {t.task_id: t for t in fleet.tasks}
    makespan = max(a.day for a in schedule.assignments.values())
    fallbacks = {t["soi"]: t for t in env["tasks"] if t["isFallback"]}
    assert set(fallbacks) == set(schedule.unscheduled)
    for tid, rec in fallbacks.items():
        assert rec["day"] == makespan + 1  # horizon end, never day 0
        assert rec["crewShortfall"] == by_id[tid].mechanics_required
        assert rec["mechanicIds"] == []
        assert rec["unscheduledReason"] == schedule.unscheduled[tid]
    # And the adapter flags them for manual review.
    adapted = _adapter().normalize_envelope(env)
    flagged = [t for t in adapted["tasks"] if t.get("isFallback")]
    assert all(t["requires_manual_review"] for t in flagged)
    assert all(t["scheduling_mode"] == "fallback" for t in flagged)


def test_commitments_block_feeds_projection(fleet, schedule, cpm, tmp_path):
    """The CLI export path advances the commitments state (first sighting
    commits immediately — §9 rule 1) and the projection block carries
    committed rows with committedDay/Date + station, the exact row shape
    the vendored /api/projection endpoint + commitments tab render."""
    state_path = tmp_path / "commitments.json"
    block = update_commitments_for_cli(fleet, schedule, "run-1", str(state_path))
    assert block["committed"], "first sighting must commit immediately"
    env = build_envelope(fleet, schedule, cpm, now=NOW, commitments_block=block)
    proj = env["metadata"]["stats"]["projection"]
    assert len(proj["committed"]) == len(block["committed"])
    stations = {a.aircraft: a.station for a in fleet.aircraft}
    for row in proj["committed"]:
        assert set(row) >= {"line", "station", "projectedDay", "projectedDate",
                            "deadlineDay", "latenessDays", "committedDay",
                            "committedDate"}
        assert row["station"] == stations[row["line"]]
        assert row["committedDate"].startswith("20")  # ISO date
    # Persisted state reloads (atomic write worked).
    assert json.loads(state_path.read_text())["aircraft"]


def test_iso_times_use_fgi_wall_clock_starts(fleet, schedule, cpm):
    """startTime ISO stamps anchor on the FGI wall-clock shift starts
    (addendum: S1 06:00, S2 14:30, S3 22:30) from the fixed Monday
    reference date."""
    env = build_envelope(fleet, schedule, cpm, now=NOW)
    assert env["metadata"]["reference_date"] == REFERENCE_DATE.isoformat()
    starts = {1: (6, 0), 2: (14, 30), 3: (22, 30)}
    checked = 0
    for t in env["tasks"]:
        if t["start_minute"] == 0 and not t["isFallback"]:
            dt = datetime.fromisoformat(t["startTime"])
            assert (dt.hour, dt.minute) == starts[t["shift"]]
            checked += 1
    assert checked > 0


def test_cli_export_envelope_subcommand(fleet, schedule, tmp_path):
    """run.py export-envelope --data F --schedule S --out-dir OUT writes a
    discoverable envelope (in-process main(), throwaway commitments state).

    FF_app's run.py is loaded by explicit path: the vendored dashboard also
    ships a run.py, and the sys.path trick used for the adapter must never
    let ``import run`` resolve to the wrong tree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ff_app_run", os.path.join(FF_ROOT, "run.py")
    )
    ff_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff_run)

    fleet_p = tmp_path / "fleet.json.gz"
    sched_p = tmp_path / "schedule.json.gz"
    save_json_gz(fleet.to_dict(), str(fleet_p))
    save_json_gz(schedule.to_dict(), str(sched_p))
    out_dir = tmp_path / "schedules"
    rc = ff_run.main([
        "export-envelope",
        "--data", str(fleet_p),
        "--schedule", str(sched_p),
        "--out-dir", str(out_dir),
        "--commitments-state", str(tmp_path / "commitments.json"),
    ])
    assert rc == 0
    files = list(out_dir.glob("max_v1_*.json.gz"))
    assert len(files) == 1
    adapted = _adapter().load_envelope(str(files[0]))
    assert adapted["metadata"]["adapted_from"] == "max"
    # CLI path committed the fleet (§9 rule 1) => projection rows present.
    assert adapted["metadata"]["stats"]["projection"]["committed"]
