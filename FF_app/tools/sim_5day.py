#!/usr/bin/env python3
"""Demo run — baseline + N executed shift task lists + dashboards feed.

``python3 tools/sim_5day.py --out demo/week1`` runs the owner-requested
proof loop on the fleet50 fixture:

- BASELINE plan anchored at the Sunday-night week boundary (start_day=6,
  a Sunday; the first plannable slot is (6, S3) — OR-3: "the week's 3rd
  shift begins Sunday night", so shift #1 of the demo IS Monday's 3rd
  shift, exactly the factory chronology: Mon-S3 (Sun night), Mon-S1,
  Mon-S2, Tue-S3 (Mon night), ... Fri-S2).
- 15 EXECUTED SHIFTS (a full Mon-Fri factory week) via
  ``ff.sim.digital_week.run_digital_week`` with the non-compliance levers
  ON: completions (``exec_rate``), slides, DEVIATIONS (crews skipping
  planned work to complete ready non-critical tasks instead —
  ``deviate_rate``), OUT-OF-SEQUENCE completions (``oos_per_round``,
  exercising P_OOS), and TRUE rework injection (``rework_per_shift`` new
  Task rows joining the DAG per shift).
- After every shift the REAL engine replans (incumbent-threaded, §12
  COMMITMENT / OR-4) and the run mirrors the web layer's pipeline
  verbatim: V1-V9 validation, commitments hysteresis update (§9),
  GAMES progression advance (GG-1 derived events), and a max_v1 envelope
  export for the vendored FOCUS dashboard (mtime-ordered).

Artifacts under ``--out`` (default ``demo/week1``):

    task_lists/r{NN}_d{DD}S{S}_{label}.md     15 floor task lists (+ .json.gz)
    envelopes/max_v1_*.json.gz                16 dashboards envelopes
    state/fleet_final.json.gz                 end-of-week floor state
    state/schedule_final.json.gz              final replan
    state/commitments.json                    commitments ledger (§9)
    state/game_events.jsonl                   GAMES event log (GG-1)
    state/excusals.json                       lead-captured excusals (GG-3)
    metrics/*.json                            per-round measured figures
    SUMMARY.md                                the measured week story

Honesty (OR-5): every artifact carries/announces ``mock_data``; all
figures below are synthetic fleet50 fixture data; no optimality claims.
Determinism: the sim rng is seeded; the side rng for lead-captured
excusals is an independent fixed stream; only wall-seconds and envelope
freshness stamps vary between runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

FF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FF_ROOT not in sys.path:
    sys.path.insert(0, FF_ROOT)

import config  # noqa: E402
from ff.data.loader import load_json_gz, save_json_gz  # noqa: E402
from ff.domain import Fleet  # noqa: E402
from ff.engine.cpm import compute_cpm, is_critical  # noqa: E402
from ff.engine.scheduler import build_schedule  # noqa: E402
from ff.engine.validator import validate  # noqa: E402
from ff.export.envelope import REFERENCE_DATE, export_envelope  # noqa: E402
from ff.services import commitments as commitments_svc  # noqa: E402
from ff.services import points as points_svc  # noqa: E402
from ff.services import progression as prog  # noqa: E402
from ff.services import scorecard as sc  # noqa: E402
from ff.services.snapshot import build_snapshot, tasks_by_id  # noqa: E402
from ff.sim.digital_week import advance_clock, run_digital_week  # noqa: E402

WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Demo defaults (synthetic realism, not doctrine constants): most planned
# work completes, a visible minority slides, crews walk off the plan ~7%
# of the time, one out-of-sequence jump per shift, a steady rework trickle.
EXEC_RATE = 0.88
DEVIATE_RATE = 0.07
OOS_PER_ROUND = 1
REWORK_PER_SHIFT = 12
LEAD_CAPTURE_RATE = 0.30  # share of slid tasks a lead excuses as LATE_PART

START_DAY = 6  # Sunday — first plannable slot is (6, S3): Monday's 3rd shift
START_SHIFT = 3
DEFAULT_DAYS = 5  # Mon-S3 .. Fri-S2: one full factory week (15 rounds)


def rounds_for_days(days: int) -> int:
    """Eligible shift slots across ``days`` WORK-day-calendar days from
    the Monday anchor (a slot belongs to the work day it flows into —
    OR-3: the overnight S3 counts toward day+1). The calendar decides the
    count (S3 runs Sunday night through Friday night; Saturday night is
    dark): 5 days -> 15 rounds, 14 days -> 32 rounds — never a naive
    days*3."""
    count = 0
    day, shift = START_DAY, START_SHIFT
    while workday_of_slot(day, shift) <= START_DAY + days:
        count += 1
        day, shift, _ = advance_clock(day, shift)
    return count


def slot_label(day: int, shift: int) -> str:
    """Factory label for a slot — S3 belongs to the NEXT day's work day."""
    if shift == 3:
        return (
            f"{WEEKDAY[(day + 1) % 7]} 3rd shift "
            f"(starts {WEEKDAY[day % 7]} night)"
        )
    return f"{WEEKDAY[day % 7]} {'1st' if shift == 1 else '2nd'} shift"


def slot_slug(day: int, shift: int) -> str:
    if shift == 3:
        return f"{WEEKDAY[(day + 1) % 7]}-3rd"
    return f"{WEEKDAY[day % 7]}-{'1st' if shift == 1 else '2nd'}"


def iso_date(day: int) -> str:
    return (REFERENCE_DATE + timedelta(days=day)).isoformat()


def workday_of_slot(day: int, shift: int) -> int:
    """The work day a slot belongs to (S3 flows into day+1 — OR-3)."""
    return day + 1 if shift == 3 else day


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


class WeekRunner:
    """Closure state for the on_executed / on_replanned hooks."""

    def __init__(self, out_dir: Path, seed: int):
        self.out = out_dir
        (self.out / "task_lists").mkdir(parents=True, exist_ok=True)
        (self.out / "envelopes").mkdir(parents=True, exist_ok=True)
        (self.out / "state").mkdir(parents=True, exist_ok=True)
        (self.out / "metrics").mkdir(parents=True, exist_ok=True)
        # Independent side stream: lead-capture coins must not perturb the
        # sim's own rng sequence.
        self.lead_rng = Random(seed * 7919 + 13)
        self.cstate = commitments_svc.empty_state()
        self.deadlines: dict[int, int] = {}
        self.manual_excusals: list[dict] = []
        self.game_log: list[dict] = []
        self.prev_summary = None
        self.shift_reports: dict[int, dict] = {}  # round -> {team: report}
        self.leaderboards: list[dict] = []
        self.validator_rows: list[dict] = []
        self.commit_changes: list[dict] = []
        self.envelope_paths: list[Path] = []
        self.now0 = datetime.now(timezone.utc)
        self.rounds_meta: dict[int, dict] = {}
        # Scorecard record stream (ff.services.scorecard shape) — one row
        # per planned task per executed shift + flagged offplan/oos rows.
        self.records: list[dict] = []
        self.station_of: dict[int, str] = {}
        # Points-greedy deviation channel: the (fleet, schedule, cpm) whose
        # slot is about to execute, with a lazily-built scoring snapshot
        # (crews chase value against the plan they are working).
        self.value_state: tuple | None = None
        self.value_snap: dict | None = None

    def set_value_state(self, fleet, schedule, cpm) -> None:
        self.value_state = (fleet, schedule, cpm)
        self.value_snap = None

    def value_of(self, task_id: str) -> int:
        """Current point value of a task (greedy-deviation ordering)."""
        if self.value_state is None:
            return 0
        if self.value_snap is None:
            self.value_snap = build_snapshot(*self.value_state)
        try:
            return int(points_svc.score_task(task_id, self.value_snap)["total"])
        except KeyError:
            return 0

    # -- baseline ---------------------------------------------------------

    def baseline(self, fleet: Fleet):
        """Plan + validate + commit + export the pre-week baseline."""
        cpm = compute_cpm(fleet.tasks)
        t0 = time.perf_counter()
        schedule = build_schedule(fleet, cpm, start_day=START_DAY)
        wall = time.perf_counter() - t0
        report = validate(schedule, fleet)
        self.validator_rows.append(
            {"round": 0, "label": "baseline", "summary": report["summary"]}
        )
        self.deadlines = {
            a.aircraft: a.delivery_deadline_day for a in fleet.aircraft
        }
        self.station_of = {a.aircraft: a.station for a in fleet.aircraft}
        # Gate pressure (docs/GATE_PRESSURE_DESIGN.md): stamp every task's
        # station gate from the PLAN OF RECORD before the week begins —
        # run_digital_week clones this fleet, so the gates travel with it.
        stamped = sc.stamp_gates_from_schedule(fleet, schedule)
        # Round 1 greedy ordering scores against this (identical-content)
        # snapshot; from round 2 on, on_replanned re-points value_state at
        # the live working fleet + its fresh plan.
        self.set_value_state(fleet, schedule, cpm)
        self._gates_stamped = stamped
        projections = commitments_svc.projections_from_stats(schedule.stats)
        evidence = commitments_svc.build_evidence(fleet, schedule)
        self.cstate, changes = commitments_svc.update_commitments(
            self.cstate, projections, "wk1-baseline", evidence
        )
        self.commit_changes.extend(changes)
        snap = build_snapshot(fleet, schedule, cpm)
        snap["commitments"] = commitments_svc.snapshot_block(
            self.cstate, projections, self.deadlines, "wk1-baseline"
        )
        snap["manual_excusals"] = self.manual_excusals
        snap["game_events"] = self.game_log
        _, self.prev_summary, _ = prog.advance(None, self.game_log, snap)
        path = export_envelope(
            fleet,
            schedule,
            cpm,
            snapshot=snap,
            out_dir=self.out / "envelopes",
            shift_number=START_SHIFT,
            now=self.now0,
        )
        self.envelope_paths.append(path)
        stats = schedule.stats
        return {
            "fleet_lateness": stats["fleet_lateness_days"],
            "otd": stats["otd_count"],
            "controllable_usd": stats["economics"]["controllable_penalty_usd"],
            "scheduled": stats["scheduled"],
            "unscheduled": stats["unscheduled"],
            "total_tasks": stats["total_tasks"],
            "makespan_day": stats.get("makespan_day"),
            "validator_total": report["summary"]["total"],
            "wall_s": round(wall, 2),
        }

    # -- per-shift hooks ----------------------------------------------------

    def on_executed(self, ctx: dict) -> None:
        fleet, schedule, cpm = ctx["fleet"], ctx["schedule"], ctx["cpm"]
        day, shift, rnd = ctx["day"], ctx["shift"], ctx["round"]
        by_id = {t.task_id: t for t in fleet.tasks}

        # GG-3 lead capture: a share of tasks that SLID get an excusable
        # cause captured by the lead (deterministic side stream). Deviated
        # (skipped) tasks are deliberately NOT excused — the team owns
        # walking off the plan, and the scoreboard must show it.
        for tid in ctx["slid"]:
            if self.lead_rng.random() < LEAD_CAPTURE_RATE:
                task = by_id[tid]
                self.manual_excusals.append(
                    {
                        "task_id": tid,
                        "cause": "LATE_PART",
                        "team": task.team,
                        "day": day,
                        "shift": shift,
                        "evidence": (
                            "lead capture: part shortage discovered "
                            f"mid-shift on {slot_label(day, shift)}"
                        ),
                        "entered_by": "demo-lead",
                        "ts": (
                            self.now0 + timedelta(minutes=90 * rnd)
                        ).isoformat(timespec="seconds"),
                    }
                )

        snap = build_snapshot(fleet, schedule, cpm)
        snap["manual_excusals"] = self.manual_excusals
        planned_teams = sorted(
            {by_id[tid].team for tid in ctx["planned"] if tid in by_id}
        )
        reports = {
            team: points_svc.shift_report(snap, team, day, shift)
            for team in planned_teams
        }
        self.shift_reports[rnd] = reports
        self.rounds_meta[rnd] = {
            "day": day,
            "shift": shift,
            "label": slot_label(day, shift),
            "date": iso_date(day),
            "workday": workday_of_slot(day, shift),
        }

        # Scorecard records: planned rows straight from the shift reports'
        # breakdowns (EXACT GG-3 excusal semantics), then flagged offplan /
        # out-of-sequence completions (planned=False — GG-2: they never
        # earn toward this slot's goal; compliance grades them instead).
        offplan_set, oos_set = set(ctx["offplan"]), set(ctx["oos"])

        def _behind_and_cascade(task) -> tuple[int, str]:
            """Gate-health fields (owner directive on out-of-control
            causes): days past the station gate, and — for OPEN behind
            work — WHY: 'cross_team' (another team's unfinished pred: the
            needs-support lane), 'same_team' (own team's unfinished pred:
            owned at the ROOT, counted apart so one slipped root never
            reads as N failures), 'primary' (preds done: owned outright).
            """
            if task.gate_day is None:
                return 0, ""
            behind = max(0, day - int(task.gate_day))
            if behind == 0:
                return 0, ""
            if task.state == "done":
                return behind, ""  # burned down — cascade moot
            kinds = set()
            for p in task.predecessors:
                pred = by_id.get(p)
                if pred is not None and p != task.task_id and pred.state != "done":
                    kinds.add(
                        "cross_team" if pred.team != task.team else "same_team"
                    )
            if "cross_team" in kinds:
                return behind, "cross_team"
            if "same_team" in kinds:
                return behind, "same_team"
            return behind, "primary"

        for team in planned_teams:
            for row in reports[team]["breakdown"]:
                task = by_id[row["task_id"]]
                behind, cascade = _behind_and_cascade(task)
                self.records.append(
                    sc.make_record(
                        round_no=rnd,
                        day=day,
                        shift=shift,
                        team=team,
                        aircraft=task.aircraft,
                        station=self.station_of.get(task.aircraft, ""),
                        points=row["points"],
                        planned=True,
                        done=row["done"],
                        excused=row["excused"],
                        behind_days=behind,
                        cascade=cascade,
                    )
                )
        for tid in sorted(offplan_set | oos_set):
            task = by_id[tid]
            behind, _cascade = _behind_and_cascade(task)
            self.records.append(
                sc.make_record(
                    round_no=rnd,
                    day=day,
                    shift=shift,
                    team=task.team,
                    aircraft=task.aircraft,
                    station=self.station_of.get(task.aircraft, ""),
                    points=points_svc.score_task(tid, snap)["total"],
                    planned=False,
                    done=True,
                    excused=False,
                    offplan=True,
                    oos=tid in oos_set,
                    behind_days=behind,
                )
            )

        # Canonical GG-2 leaderboard when a SLOT-day completes (its S3 was
        # the last slice to execute).
        if shift == 3:
            self.leaderboards.append(
                {
                    "slot_day": day,
                    "after_round": rnd,
                    "rows": points_svc.leaderboard(snap, day),
                }
            )

        self._write_task_list(ctx, snap, reports, by_id)

    def on_replanned(self, ctx: dict) -> None:
        fleet, schedule, cpm = ctx["fleet"], ctx["schedule"], ctx["cpm"]
        rnd = ctx["round"]
        run_id = f"wk1-r{rnd:02d}"

        report = validate(schedule, fleet)
        self.validator_rows.append(
            {"round": rnd, "label": run_id, "summary": report["summary"]}
        )

        projections = commitments_svc.projections_from_stats(schedule.stats)
        evidence = commitments_svc.build_evidence(fleet, schedule)
        self.cstate, changes = commitments_svc.update_commitments(
            self.cstate, projections, run_id, evidence
        )
        self.commit_changes.extend(changes)

        snap = build_snapshot(fleet, schedule, cpm)
        snap["commitments"] = commitments_svc.snapshot_block(
            self.cstate, projections, self.deadlines, run_id
        )
        snap["manual_excusals"] = self.manual_excusals
        snap["game_events"] = self.game_log
        _, self.prev_summary, _ = prog.advance(
            self.prev_summary, self.game_log, snap
        )

        path = export_envelope(
            fleet,
            schedule,
            cpm,
            snapshot=snap,
            out_dir=self.out / "envelopes",
            shift_number=ctx["exec_shift"],
            now=self.now0 + timedelta(minutes=90 * rnd),
        )
        self.envelope_paths.append(path)
        ctx["row"]["validator_total"] = report["summary"]["total"]
        # Greedy-deviation channel scores the NEXT slot against this plan.
        self.set_value_state(fleet, schedule, cpm)

    # -- artifacts ----------------------------------------------------------

    def _write_task_list(self, ctx, snap, reports, by_id) -> None:
        day, shift, rnd = ctx["day"], ctx["shift"], ctx["round"]
        label = slot_label(day, shift)
        slug = slot_slug(day, shift)
        status_of: dict[str, str] = {}
        for tid in ctx["executed"]:
            status_of[tid] = "COMPLETE"
        for tid in ctx["slid"]:
            status_of[tid] = "IN_PROGRESS (slid)"
        for tid in ctx["skipped"]:
            status_of[tid] = "NOT WORKED (crew deviated)"
        for tid in ctx["blocked"]:
            status_of[tid] = "NOT WORKED (blocked: predecessor incomplete)"
        excused_ids = {
            rec["task_id"]
            for rec in self.manual_excusals
            if rec["day"] == day and rec["shift"] == shift
        }
        asg = ctx["schedule"].assignments

        rows = []
        for tid in ctx["planned"]:
            task = by_id[tid]
            a = asg[tid]
            info = ctx["cpm"].get(tid)
            score = points_svc.score_task(tid, snap)
            rows.append(
                {
                    "task_id": tid,
                    "team": task.team,
                    "aircraft": task.aircraft,
                    "skill": task.skill,
                    "crew": list(a.mechanic_ids),
                    "duration_minutes": task.duration_minutes,
                    "critical": bool(info) and is_critical(info),
                    "points": score["total"],
                    "status": status_of.get(tid, "NOT WORKED"),
                    "excused": tid in excused_ids,
                    "explanation": score["explanation"],
                }
            )

        offplan_rows = []
        for tid in ctx["offplan"]:
            task = by_id[tid]
            score = points_svc.score_task(tid, snap)
            offplan_rows.append(
                {
                    "task_id": tid,
                    "team": task.team,
                    "aircraft": task.aircraft,
                    "duration_minutes": task.duration_minutes,
                    "points": score["total"],
                    "note": "off-plan completion (not in this shift's goal)",
                }
            )
        oos_rows = []
        for tid in ctx["oos"]:
            task = by_id[tid]
            score = points_svc.score_task(tid, snap)
            oos_rows.append(
                {
                    "task_id": tid,
                    "team": task.team,
                    "aircraft": task.aircraft,
                    "points": score["total"],
                    "oos_penalty": score["oos_penalty"],
                    "explanation": score["explanation"],
                }
            )
        rework_rows = []
        for tid in ctx["injected"]:
            task = by_id[tid]
            rework_rows.append(
                {
                    "task_id": tid,
                    "team": task.team,
                    "aircraft": task.aircraft,
                    "parent": task.predecessors[0] if task.predecessors else None,
                    "duration_minutes": task.duration_minutes,
                    "crew_size": task.mechanics_required,
                }
            )

        doc = {
            "round": rnd,
            "slot": {"day": day, "shift": shift},
            "label": label,
            "date": iso_date(day),
            "mock_data": True,
            "planned": rows,
            "offplan_completions": offplan_rows,
            "out_of_sequence_completions": oos_rows,
            "rework_injected": rework_rows,
            "team_reports": {
                team: {
                    k: rep[k]
                    for k in (
                        "goal",
                        "earned",
                        "attainment",
                        "difficulty",
                        "value_efficiency",
                        "excused_points",
                    )
                }
                for team, rep in reports.items()
            },
        }
        stem = f"r{rnd:02d}_d{day:02d}S{shift}_{slug}"
        save_json_gz(doc, str(self.out / "task_lists" / f"{stem}.json.gz"))

        lines = [
            f"# Shift task list — {label}",
            "",
            f"Round {rnd} · slot (day {day}, S{shift}) · {iso_date(day)}"
            " · SYNTHETIC fleet50 mock data",
            "",
            f"Planned {len(rows)} tasks · completed {len(ctx['executed'])}"
            f" · slid {len(ctx['slid'])} · deviated {len(ctx['skipped'])}"
            f" · blocked-by-pred {len(ctx['blocked'])}"
            f" · off-plan done {len(ctx['offplan'])}"
            f" · out-of-sequence {len(ctx['oos'])}"
            f" · rework injected {len(ctx['injected'])}",
            "",
            "## Team scoreboard (end of shift)",
            "",
            "| Team | Goal pts | Earned | Attainment | Difficulty |"
            " Value eff | Excused pts |",
            "|---|---|---|---|---|---|---|",
        ]
        for team in sorted(reports):
            rep = reports[team]
            lines.append(
                f"| {team} | {rep['goal']} | {rep['earned']} |"
                f" {_fmt_pct(rep['attainment'])} | {rep['difficulty']:.1f} |"
                f" {rep['value_efficiency']:.3f} | {rep['excused_points']} |"
            )
        lines += [
            "",
            "## Planned shop orders",
            "",
            "| Shop order | Team | A/C | Skill | Crew | Min | Crit | Pts |"
            " Status |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            status = r["status"] + (" · EXCUSED (lead)" if r["excused"] else "")
            lines.append(
                f"| {r['task_id']} | {r['team']} | {r['aircraft']} |"
                f" {r['skill']} | {' '.join(r['crew'])} |"
                f" {r['duration_minutes']} | {'Y' if r['critical'] else ''} |"
                f" {r['points']} | {status} |"
            )
        if offplan_rows:
            lines += [
                "",
                "## Off-plan completions (crew deviated to these)",
                "",
                "| Shop order | Team | A/C | Min | Pts | Note |",
                "|---|---|---|---|---|---|",
            ]
            for r in offplan_rows:
                lines.append(
                    f"| {r['task_id']} | {r['team']} | {r['aircraft']} |"
                    f" {r['duration_minutes']} | {r['points']} | {r['note']} |"
                )
        if oos_rows:
            lines += [
                "",
                "## Out-of-sequence completions (P_OOS penalized)",
                "",
                "| Shop order | Team | A/C | Pts | OOS dock | Why |",
                "|---|---|---|---|---|---|",
            ]
            for r in oos_rows:
                lines.append(
                    f"| {r['task_id']} | {r['team']} | {r['aircraft']} |"
                    f" {r['points']} | -{r['oos_penalty']} |"
                    f" {r['explanation']} |"
                )
        if rework_rows:
            lines += [
                "",
                "## Rework injected during this shift (joins next replan)",
                "",
                "| New shop order | Team | A/C | Parent | Min | Crew |",
                "|---|---|---|---|---|---|",
            ]
            for r in rework_rows:
                lines.append(
                    f"| {r['task_id']} | {r['team']} | {r['aircraft']} |"
                    f" {r['parent']} | {r['duration_minutes']} |"
                    f" {r['crew_size']} |"
                )
        (self.out / "task_lists" / f"{stem}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


def workday_boards(runner: WeekRunner) -> list[dict]:
    """GG-2 work-day leaderboards (Mon..Fri) from the stored slot reports.

    A work day = its three executed slots (S3-that-started-the-night-
    before + S1 + S2 — OR-3 chronology). Rank = (attainment, efficiency,
    difficulty), raw points never ranked — mirrors ``points.leaderboard``.
    """
    by_day: dict[int, dict[str, dict]] = {}
    for rnd, reports in runner.shift_reports.items():
        wd = runner.rounds_meta[rnd]["workday"]
        agg = by_day.setdefault(wd, {})
        for team, rep in reports.items():
            row = agg.setdefault(
                team, {"goal": 0, "earned": 0, "excused": 0, "counted": 0}
            )
            row["goal"] += rep["goal"]
            row["earned"] += rep["earned"]
            row["excused"] += rep["excused_points"]
            row["counted"] += sum(
                1 for b in rep["breakdown"] if not b["excused"]
            )
    boards = []
    for wd in sorted(by_day):
        rows = []
        for team, row in sorted(by_day[wd].items()):
            att = row["earned"] / row["goal"] if row["goal"] else 0.0
            rows.append(
                {
                    "team": team,
                    "attainment": round(att, 4),
                    "goal": row["goal"],
                    "earned": row["earned"],
                    "excused": row["excused"],
                    "difficulty": (
                        round(row["goal"] / row["counted"], 1)
                        if row["counted"]
                        else 0.0
                    ),
                }
            )
        rows.sort(key=lambda r: (-r["attainment"], -r["difficulty"], r["team"]))
        for rank, r in enumerate(rows, 1):
            r["rank"] = rank
        boards.append(
            {"workday": wd, "weekday": WEEKDAY[wd % 7], "rows": rows}
        )
    return boards


def render_scorecard_md(card: dict, days: int, rounds: int) -> str:
    """Human-readable graded scorecard (SYNTHETIC data, rubric shown)."""
    bands = ", ".join(f">={f:.2f}:{g}" for f, g in card["meta"]["grade_bands"])
    lines = [
        f"# Scorecard — {days} work days / {rounds} executed shifts",
        "",
        "All figures SYNTHETIC (fleet50 mock data). Grade rubric on "
        f"attainment: {bands}, else F (PLACEHOLDER bands — config §10).",
        "Compliance = in-plan earned / (in-plan + off-plan completed value).",
        "",
    ]

    def table(rows: list[dict], title: str, key_col: str = "slice") -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append(
            "| " + key_col + " | Period | Grade | Attainment | Goal | Earned |"
            " Excused | Off-plan | Compliance |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['slice']} | {r['period']} | **{r['grade']}** |"
                f" {_fmt_pct(r['attainment'])} | {r['goal']} | {r['earned']} |"
                f" {r['excused_points']} | {r['offplan_points']} |"
                f" {_fmt_pct(r['compliance'])} |"
            )
        lines.append("")

    tables, trends = card["tables"], card["trends"]

    def gate_health(rows: list[dict], title: str) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append(
            "| Slice | Period | Behind open pts | Avg age d | Burned-down"
            " pts | Primary | Same-team cascade | Cross-team cascade |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['slice']} | {r['period']} | {r['behind_open_points']} |"
                f" {r['behind_avg_age']} | {r['behind_done_points']} |"
                f" {r['behind_primary_n']} | {r['behind_same_team_n']} |"
                f" {r['behind_cross_team_n']} |"
            )
        lines.append("")

    table(tables["fleet"]["week"], "Fleet — by week")
    table(tables["fleet"]["day"], "Fleet — by day")
    gate_health(tables["fleet"]["day"], "Gate health — fleet backlog by day"
                " (behind station gate)")
    gate_health(tables["building"]["week"], "Gate health — buildings by week")
    table(tables["shift"]["week"], "Shift crews (S1/S2/S3) — by week")
    table(tables["group"]["week"], "Superintendent positions (G1–G4) — by week")
    table(tables["building"]["week"], "Buildings — by week")
    table(tables["team"]["week"], "Teams — by week")

    lines.append("## Trends (attainment, day grain)")
    lines.append("")
    lines.append("| Slice | Value | Direction | First | Last | Delta | Slope/day |")
    lines.append("|---|---|---|---|---|---|---|")
    for slice_by in ("fleet", "shift", "group", "building", "team"):
        for t in trends[slice_by]["day"]:
            arrow = {"improving": "UP", "declining": "DOWN", "flat": "FLAT"}[
                t["direction"]
            ]
            lines.append(
                f"| {slice_by} | {t['slice']} | {arrow} |"
                f" {_fmt_pct(t['first'])} | {_fmt_pct(t['last'])} |"
                f" {t['delta']:+.4f} | {t['slope']:+.5f} |"
            )
    lines.append("")
    lines.append("## Week-over-week attainment deltas")
    lines.append("")
    lines.append("| Slice | Value | From | To | Delta |")
    lines.append("|---|---|---|---|---|")
    for slice_by in ("fleet", "shift", "group", "building"):
        for d in card["week_over_week"][slice_by]:
            lines.append(
                f"| {slice_by} | {d['slice']} | W{d['from_week']} |"
                f" W{d['to_week']} | {d['delta']:+.4f} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=os.path.join(FF_ROOT, "data", "fleet50.json.gz"))
    ap.add_argument("--out", default=os.path.join(FF_ROOT, "demo", "week1"))
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--exec-rate", type=float, default=EXEC_RATE)
    ap.add_argument("--deviate-rate", type=float, default=DEVIATE_RATE)
    ap.add_argument("--oos-per-round", type=int, default=OOS_PER_ROUND)
    ap.add_argument("--rework-per-shift", type=int, default=REWORK_PER_SHIFT)
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="work-day-calendar days to execute (5 -> 15 shifts, 14 -> 32; "
        "the OR-3 calendar decides — Saturday night is dark)",
    )
    ap.add_argument(
        "--greedy-deviation",
        action="store_true",
        help="deviating crews pick the HIGHEST-point ready task instead of "
        "a random one (the GAMES thesis made operational — gate-pressure "
        "A/B behavior channel; docs/GATE_PRESSURE_DESIGN.md §9)",
    )
    args = ap.parse_args(argv)
    rounds = rounds_for_days(args.days)

    out = Path(args.out)
    fleet = Fleet.from_dict(load_json_gz(args.data))
    runner = WeekRunner(out, args.seed)

    print(
        f"=== FF {args.days}-day demo ({rounds} shifts; "
        "SYNTHETIC fleet50 mock data) ==="
    )
    print(
        f"anchor: start_day={START_DAY} (Sunday) -> first slot (6, S3) = "
        "Monday's 3rd shift, starts Sunday night (OR-3)"
    )
    base = runner.baseline(fleet)
    print(
        f"baseline: {base['scheduled']}/{base['total_tasks']} scheduled, "
        f"lateness {base['fleet_lateness']} d, otd {base['otd']}/50, "
        f"controllable ${base['controllable_usd']:,}, "
        f"V-violations {base['validator_total']}, wall {base['wall_s']}s"
    )

    result, final = run_digital_week(
        fleet,
        rounds=rounds,
        seed=args.seed,
        exec_rate=args.exec_rate,
        slide_remain=0.5,
        rework_per_round=args.rework_per_shift,
        start_day=START_DAY,
        start_shift=START_SHIFT,
        deviate_rate=args.deviate_rate,
        oos_per_round=args.oos_per_round,
        on_executed=runner.on_executed,
        on_replanned=runner.on_replanned,
        return_state=True,
        thread_incumbent=True,
        offplan_value_fn=runner.value_of if args.greedy_deviation else None,
    )

    # Force strictly ascending envelope mtimes (dashboard discovery order).
    t0 = time.time() - 3600
    for i, p in enumerate(runner.envelope_paths):
        os.utime(p, (t0 + i * 60, t0 + i * 60))

    # -- persist end-of-week state for the FF API server ---------------------
    work: Fleet = final["fleet"]
    schedule = final["schedule"]
    save_json_gz(work.to_dict(), str(out / "state" / "fleet_final.json.gz"))
    save_json_gz(
        schedule.to_dict(), str(out / "state" / "schedule_final.json.gz")
    )
    commitments_svc.persist_state(
        runner.cstate, str(out / "state" / "commitments.json")
    )
    prog.persist_events(
        runner.game_log, str(out / "state" / "game_events.jsonl")
    )
    (out / "state" / "excusals.json").write_text(
        json.dumps({"excusals": runner.manual_excusals}, indent=1),
        encoding="utf-8",
    )

    # -- metrics ------------------------------------------------------------
    boards = workday_boards(runner)
    (out / "metrics" / "rounds.json").write_text(
        json.dumps(result, indent=1, sort_keys=True), encoding="utf-8"
    )
    (out / "metrics" / "workday_leaderboards.json").write_text(
        json.dumps(boards, indent=1), encoding="utf-8"
    )
    (out / "metrics" / "slotday_leaderboards.json").write_text(
        json.dumps(runner.leaderboards, indent=1), encoding="utf-8"
    )
    (out / "metrics" / "commitment_changes.json").write_text(
        json.dumps(runner.commit_changes, indent=1), encoding="utf-8"
    )
    (out / "metrics" / "validator.json").write_text(
        json.dumps(runner.validator_rows, indent=1), encoding="utf-8"
    )
    shift_report_dump = {
        str(rnd): {
            "meta": runner.rounds_meta[rnd],
            "teams": {
                team: {
                    k: rep[k]
                    for k in (
                        "goal",
                        "earned",
                        "attainment",
                        "difficulty",
                        "value_efficiency",
                        "excused_points",
                    )
                }
                for team, rep in reports.items()
            },
        }
        for rnd, reports in runner.shift_reports.items()
    }
    (out / "metrics" / "shift_reports.json").write_text(
        json.dumps(shift_report_dump, indent=1), encoding="utf-8"
    )

    # -- scorecard: grade by shift / day / week, sliced 5 ways ----------------
    # Records are the single source (ff.services.scorecard shape); the
    # graded card is what the FF API serves (FF_SCORECARD env) and the
    # dashboards' Scorecard tab renders. SCORECARD.md is the human proof.
    save_json_gz(
        {"mock_data": True, "records": runner.records},
        str(out / "metrics" / "scorecard_records.json.gz"),
    )
    card = sc.build_scorecard(runner.records, mock_data=True)
    (out / "metrics" / "scorecard.json").write_text(
        json.dumps(card, indent=1, sort_keys=True), encoding="utf-8"
    )
    (out / "SCORECARD.md").write_text(
        render_scorecard_md(card, args.days, rounds), encoding="utf-8"
    )

    # -- summary -------------------------------------------------------------
    lines = [
        f"# FF {args.days}-day demo ({rounds} shifts) — measured summary",
        "",
        "All figures SYNTHETIC (fleet50 mock data, seed "
        f"{args.seed}); no optimality claims. Baseline anchored at the",
        "Sunday-night week boundary; 15 executed shifts = Mon-S3 (Sun night)"
        " through Fri-S2 (OR-3 chronology).",
        "",
        "## Baseline",
        "",
        f"- {base['scheduled']}/{base['total_tasks']} tasks scheduled,"
        f" 0 unscheduled" if base["unscheduled"] == 0 else
        f"- {base['scheduled']}/{base['total_tasks']} scheduled,"
        f" {base['unscheduled']} unscheduled",
        f"- fleet lateness {base['fleet_lateness']} days · OTD"
        f" {base['otd']}/50 · controllable ${base['controllable_usd']:,}",
        f"- V1-V9 violations: {base['validator_total']}",
        "",
        "## Executed shifts",
        "",
        "| # | Shift | Planned done | Slid | Deviated | Blocked | Off-plan |"
        " OOS | Rework in | Attain (fleet) | Lateness d | OTD | In-horizon"
        " slot-stable |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["rounds"]:
        rnd = row["round"]
        meta = runner.rounds_meta[rnd]
        reports = runner.shift_reports[rnd]
        goal = sum(r["goal"] for r in reports.values())
        earned = sum(r["earned"] for r in reports.values())
        att = _fmt_pct(earned / goal) if goal else "n/a"
        lines.append(
            f"| {rnd} | {meta['label']} | {row['executed']} | {row['slid']} |"
            f" {row['deviated']} | {row['blocked_pred']} |"
            f" {row['offplan_done']} | {row['oos_done']} |"
            f" {row['injected']} | {att} | {row['fleet_lateness']} |"
            f" {row['otd']} | {row['in_horizon_slot_stable_pct']}% |"
        )
    lines += ["", "## Work-day leaderboards (GG-2: attainment-ranked)", ""]
    for board in boards:
        lines.append(f"### {board['weekday']} (work day {board['workday']})")
        lines.append("")
        lines.append("| Rank | Team | Attainment | Goal | Earned | Excused |")
        lines.append("|---|---|---|---|---|---|")
        for r in board["rows"][:10]:
            lines.append(
                f"| {r['rank']} | {r['team']} | {_fmt_pct(r['attainment'])} |"
                f" {r['goal']} | {r['earned']} | {r['excused']} |"
            )
        lines.append("")
    n_recovery = sum(
        1 for e in runner.game_log if e.get("type") == "recovery-moment"
    )
    n_badges = sum(1 for e in runner.game_log if e.get("type") == "badge-earned")
    total_viol = sum(v["summary"]["total"] for v in runner.validator_rows)
    lines += [
        "## Delivery & integrity",
        "",
        f"- commitment log rows this week: {len(runner.commit_changes)}"
        f" (initial commits + hysteresis-gated changes)",
        f"- GAMES events derived: {len(runner.game_log)}"
        f" (recovery moments {n_recovery}, badges {n_badges})",
        f"- lead-captured excusals (GG-3): {len(runner.manual_excusals)}",
        f"- V1-V9 violations across baseline + 15 replans: {total_viol}",
        f"- envelopes exported for the FOCUS dashboard:"
        f" {len(runner.envelope_paths)}",
        f"- total wall: {result['wall_seconds_total']}s",
        "",
    ]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nartifacts written under {out}")
    print(
        f"V1-V9 total violations ({rounds + 1} plans): {total_viol} · "
        f"game events {len(runner.game_log)} · "
        f"commitment rows {len(runner.commit_changes)} · "
        f"excusals {len(runner.manual_excusals)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
