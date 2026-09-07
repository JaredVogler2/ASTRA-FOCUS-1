"""Factory Wall — big-monitor leaderboards, divisions, overdrive, feed.

Owner directive (2026-07-11): "team and team+shift leaderboards to drive
some level of competition. Display badges, a feed of progress made...
imagine leaderboards and progression bars displayed on large monitors
throughout the factory."

Everything here is a READ-MODEL over two existing sources — the graded
scorecard record aggregates (``ff.services.scorecard``) and the derived
game-event log (``ff.services.progression``). Nothing writes; nothing
invents; every number traces to a record or event (GG-1/GG-4).

Competition design (laws preserved, plus the owner's over-achievement
directive):

- **Attainment stays primary and capped at 100%** (GG-2: off-plan volume
  can never outrank plan-following — the correlation-gate law).
- **OVERDRIVE** is the new secondary lane: off-plan value completed
  (blockage break-throughs, pulled-ahead work, aged-backlog burn-down)
  as a percentage of the unit's goal. It separates units that both hit
  their plan — displayed as "+N% OD" — and NEVER lifts a unit above a
  higher-attainment one.
- **Divisions**: units compete within their control-station cohort
  (each team's division = the station band where most of its executed
  point value lives), because build maturity makes cross-station job
  mixes incomparable; a fleet-wide board still exists (attainment is
  self-normalizing, so it stays honest — divisions just make the rivalry
  like-for-like).
- **Rolling window**: boards rank over the last ``WALL_WINDOW_DAYS``
  work days, de-lumping small-slice units (a delivery-station team with
  six big jobs should not live or die on one shift).
- **PSY-5 preserved**: bottom entries carry needs-support framing via
  their excused share; raw point totals never appear in rankings.

Honesty (OR-5): the payload carries ``mock_data`` from the scorecard
meta; wall displays label synthetic data.
"""

from __future__ import annotations

import config
from ff.services import progression
from ff.services.scorecard import building_of_station

WALL_WINDOW_DAYS = 5  # rolling ranking window (work days) — env-tunable
FEED_LIMIT = 30
BADGE_LIMIT = 12


def _window_days(rows: list[dict], window: int) -> set:
    """The last ``window`` distinct workdays present in day-grain rows."""
    days = sorted({r["period_key"] for r in rows})
    return set(days[-window:]) if days else set()


def _fold_unit(rows: list[dict]) -> dict:
    """Fold one unit's day rows into window totals (pure sums)."""
    goal = sum(r["goal"] for r in rows)
    earned = sum(r["earned"] for r in rows)
    excused = sum(r["excused_points"] for r in rows)
    offplan = sum(r["offplan_points"] for r in rows)
    att = earned / goal if goal else 0.0
    return {
        "goal": goal,
        "earned": earned,
        "attainment": round(min(att, 1.0), 4),
        # Overdrive: above-schedule value relative to the window goal.
        "overdrive": round(offplan / goal, 4) if goal else 0.0,
        "excused_points": excused,
        "excused_share": round(excused / (goal + excused), 4) if goal + excused else 0.0,
        "days": len(rows),
    }


def _board(day_rows: list[dict], window: int) -> list[dict]:
    """Rank units from day-grain aggregate rows over the rolling window.

    Sort key (the competition law): attainment DESC (capped), then
    overdrive DESC, then unit id — overdrive is strictly a tie-breaking
    lane and can never lift a unit above higher attainment.
    """
    win = _window_days(day_rows, window)
    by_unit: dict[str, list[dict]] = {}
    for r in day_rows:
        if r["period_key"] in win and r["goal"] > 0:
            by_unit.setdefault(r["slice"], []).append(r)
    board = []
    for unit in sorted(by_unit):
        entry = _fold_unit(by_unit[unit])
        entry["unit"] = unit
        entry["needs_support"] = (
            entry["attainment"] < config.NEEDS_SUPPORT_ATTAINMENT
        )
        board.append(entry)
    board.sort(key=lambda e: (-e["attainment"], -e["overdrive"], e["unit"]))
    for rank, e in enumerate(board, 1):
        e["rank"] = rank
    return board


def _divisions_from_records(records: list[dict]) -> dict[str, str]:
    """team -> division (control-station cohort) from the raw record
    stream: the BUILDING band (FAL-A / FAL-B / FLIGHTLINE / DELIVERY,
    derived from stations) holding the majority of the team's EXECUTED
    point value. The scorecard card has no team-x-building cross table,
    so divisions come from records — the same single source the card was
    built from. Deterministic tie-break by band name; teams with no done
    records map to "UNASSIGNED"."""
    weight: dict[str, dict[str, int]] = {}
    for rec in records:
        if not rec.get("done"):
            continue
        band = rec.get("building") or building_of_station(rec.get("station", ""))
        w = weight.setdefault(rec["team"], {})
        w[band] = w.get(band, 0) + int(rec.get("points", 0))
    out = {}
    for team in sorted(weight):
        bands = weight[team]
        out[team] = sorted(bands.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return out


def _feed(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(recent feed, recent badges) from the derived game-event log.

    Feed = the most recent noteworthy events (keystones, recoveries,
    goal-crossings, streaks, badges) newest-first; plain completion
    events are excluded (too chatty for a wall). Every entry keeps its
    evidence string — the wall shows receipts, not hype (GG-4).
    """
    noteworthy = (
        "keystone-cleared", "recovery-moment", "goal-crossed",
        "streak-extended", "badge-earned",
    )
    feed = [
        {
            "type": e["event_type"],
            "team": e.get("team"),
            "day": e.get("day"),
            "shift": e.get("shift"),
            "evidence": e.get("evidence", ""),
        }
        for e in events
        if e.get("event_type") in noteworthy
    ]
    badges = [f for f in feed if f["type"] == "badge-earned"][-BADGE_LIMIT:]
    return feed[-FEED_LIMIT:][::-1], badges[::-1]


def build_wall(
    card: dict,
    records: list[dict],
    events: list[dict],
    window_days: int | None = None,
) -> dict:
    """Assemble the full wall payload (JSON-safe, aggregates only — §G8).

    Inputs: the graded scorecard (``build_scorecard`` output), the raw
    record stream it was built from (for team->division mapping), and
    the derived game-event log. Returns::

        {"meta": {mock_data, window_days, generated_from},
         "divisions": {division: [team...]},
         "team_board": [...],          # fleet-wide, rolling window
         "team_boards_by_division": {division: [...]},
         "team_shift_board": [...],    # team+shift units, rolling window
         "progression": [{team, level, xp, next_at, pct, badges}...],
         "feed": [...], "badges": [...]}
    """
    window = int(window_days or WALL_WINDOW_DAYS)
    team_rows = card["tables"]["team"]["day"]
    ts_rows = card["tables"]["team_shift"]["day"]

    divisions_of = _divisions_from_records(records)
    divisions: dict[str, list[str]] = {}
    for team in sorted(divisions_of):
        divisions.setdefault(divisions_of[team], []).append(team)

    team_board = _board(team_rows, window)
    for e in team_board:
        e["division"] = divisions_of.get(e["unit"], "UNASSIGNED")
    by_division: dict[str, list[dict]] = {}
    for e in team_board:
        by_division.setdefault(e["division"], []).append(e)
    for div_board in by_division.values():
        for rank, e in enumerate(div_board, 1):
            e["division_rank"] = rank

    team_shift_board = _board(ts_rows, window)
    for e in team_shift_board:
        e["division"] = divisions_of.get(e["unit"].rsplit("-S", 1)[0], "UNASSIGNED")

    progression_rows = []
    teams = sorted({r["slice"] for r in team_rows})
    for team in teams:
        level = progression.team_level(events, team)
        badges = progression.badges_for_team(events, team)
        nxt = level.get("next_level_xp")
        into = level.get("xp_into_level", 0)
        span = None
        if nxt is not None:
            floor = level.get("xp", 0) - into
            span = nxt - floor
        progression_rows.append(
            {
                "team": team,
                "division": divisions_of.get(team, "UNASSIGNED"),
                "level": level.get("level"),
                "xp": level.get("xp", 0),
                "next_level_xp": nxt,
                "pct_to_next": (
                    round(min(into / span, 1.0), 4) if span and span > 0 else 1.0
                ),
                "badge_count": len(badges),
                "recent_badges": [b.get("badge") for b in badges[-3:]],
            }
        )

    feed, badges = _feed(events)
    return {
        "meta": {
            "mock_data": bool(card.get("meta", {}).get("mock_data", True)),
            "window_days": window,
            "generated_from": {
                "records": len(records),
                "events": len(events),
            },
        },
        "divisions": divisions,
        "team_board": team_board,
        "team_boards_by_division": by_division,
        "team_shift_board": team_shift_board,
        "progression": progression_rows,
        "feed": feed,
        "badges": badges,
    }
