"""Economics — lateness penalty $ with a controllable-vs-unavoidable split.

PLACEHOLDER RATES (OR-5 honesty rule): every figure here derives from the
config §4 placeholder constants, NOT negotiated terms; the output therefore
always carries ``"source": "config-defaults"`` so nobody mistakes it for a
real dollar commitment.

Math (exact, per ARCHITECTURE.md §ff/engine/economics.py), per aircraft:

    penalty_usd  = lateness_days * LATENESS_PENALTY_USD_PER_DAY
    floor_days   = min(max(0, today_day - deadline_day), lateness_days)
                   ("unavoidable": lateness already locked in by the calendar
                    — days past the deadline that no replan can recover)
    controllable = lateness_days - floor_days
                   (the remainder: lateness a better schedule could still
                    claw back)

Fleet rollup: total / unavoidable / controllable penalty USD + late_count.
"""

from __future__ import annotations

import config


def fleet_economics(stats_aircraft: list, today_day: int) -> dict:
    """Compute placeholder lateness economics for the fleet.

    ``stats_aircraft`` is ``Schedule.stats["aircraft"]``: a list of dicts
    each carrying at least ``aircraft``, ``deadline_day``, ``lateness_days``.
    ``today_day`` anchors the unavoidable floor: lateness days already in
    the past (today beyond the deadline) cannot be recovered by any replan,
    so they are split out as unavoidable; the rest is controllable.

    Deterministic (sorted by aircraft number) and honesty-labeled: the
    result carries ``"source": "config-defaults"`` because the per-day rate
    is the config §4 PLACEHOLDER, not a negotiated rate (OR-5).
    """
    rate = config.LATENESS_PENALTY_USD_PER_DAY
    per_aircraft: list[dict] = []
    total_usd = 0
    unavoidable_usd = 0
    controllable_usd = 0
    late_count = 0

    for entry in sorted(stats_aircraft, key=lambda e: e["aircraft"]):
        lateness_days = max(0, int(entry["lateness_days"]))
        deadline_day = int(entry["deadline_day"])
        penalty_usd = lateness_days * rate
        floor_days = min(max(0, int(today_day) - deadline_day), lateness_days)
        controllable_days = lateness_days - floor_days
        floor_usd = floor_days * rate
        ctrl_usd = controllable_days * rate

        if lateness_days > 0:
            late_count += 1
        total_usd += penalty_usd
        unavoidable_usd += floor_usd
        controllable_usd += ctrl_usd

        per_aircraft.append(
            {
                "aircraft": entry["aircraft"],
                "deadline_day": deadline_day,
                "lateness_days": lateness_days,
                "penalty_usd": penalty_usd,
                "floor_days": floor_days,
                "unavoidable_penalty_usd": floor_usd,
                "controllable_days": controllable_days,
                "controllable_penalty_usd": ctrl_usd,
            }
        )

    return {
        "aircraft": per_aircraft,
        "total_penalty_usd": total_usd,
        "unavoidable_penalty_usd": unavoidable_usd,
        "controllable_penalty_usd": controllable_usd,
        "late_count": late_count,
        "today_day": int(today_day),
        "penalty_usd_per_day": rate,
        # OR-5: placeholder-rate label — NEVER remove.
        "source": config.ECONOMICS_SOURCE,
    }
