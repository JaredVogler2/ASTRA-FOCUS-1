"""Committed delivery dates with hysteresis — MAX §11 doctrine, OR-4.

A committed date is a PROMISE: it moves on evidence, never on solver
jitter. ``update_commitments(state, projections, run_id, evidence)``
applies the four rules (config §9 constants, all tripwire-tested):

1. **First sighting commits immediately** — an aircraft with no committed
   date gets one now, reason ``"initial commitment"``.
2. **Jitter holds.** ``abs(delta) < COMMIT_HYSTERESIS_DAYS`` (delta =
   projected - committed) is noise: the commitment HOLDS and any pending
   streak is cleared.
3. **Bad news fast.** ``delta >= COMMIT_WORSEN_IMMEDIATE_DAYS`` recommits
   AT ONCE, with the evidence causes concatenated into the reason.
4. **Everything else needs persistence.** Small slips AND improvements
   must survive ``COMMIT_PERSISTENCE_RUNS`` consecutive runs at the same
   target (±1 day) before the commitment moves; the reason gains
   ``" (sustained N replans)"``. The pending streak RESETS whenever the
   target moves by more than 1 day.

Reasons: slips concatenate the evidence causes present for the aircraft,
drawn (in fixed order) from the keys ``rework_inserted``,
``execution_shortfall``, ``blocked_parts``, ``capacity`` — fallback
``"global replan shift"`` when no cause is recorded; improvements read
``"schedule improvement"``.

Persistence: ``data/commitments.json`` via the same atomic-write pattern
as actuals (tmp file + fsync + ``os.replace``); loaded at boot; updated
inside every /replan while the single-flight lock is held.

Deterministic throughout: projections iterate sorted by aircraft, evidence
keys in fixed order, no wall-clock inputs.
"""

from __future__ import annotations

import json
import os
import tempfile

import config

# Fixed evidence-cause order (drives reason strings deterministically).
EVIDENCE_KEYS: tuple[str, ...] = (
    "rework_inserted",
    "execution_shortfall",
    "blocked_parts",
    "capacity",
)
FALLBACK_SLIP_REASON = "global replan shift"
IMPROVEMENT_REASON = "schedule improvement"
INITIAL_REASON = "initial commitment"


# ---------------------------------------------------------------------------
# state shape helpers
# ---------------------------------------------------------------------------


def empty_state() -> dict:
    """A fresh commitments state: no aircraft committed, empty change log."""
    return {"aircraft": {}, "log": []}


def load_state(path: str) -> dict:
    """Load ``data/commitments.json`` into the state shape, or empty_state().

    Tolerant read (a missing or unreadable file yields a fresh state — the
    NEXT run then re-commits from first sighting, which is honest: we never
    invent a commitment history we cannot read back).
    """
    if not path or not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(doc, dict):
        return empty_state()
    aircraft = doc.get("aircraft")
    log = doc.get("log")
    state = empty_state()
    if isinstance(aircraft, dict):
        for key in sorted(aircraft):
            entry = aircraft[key]
            if isinstance(entry, dict) and "committed_day" in entry:
                state["aircraft"][str(key)] = {
                    "committed_day": int(entry["committed_day"]),
                    "pending": entry.get("pending") or None,
                }
    if isinstance(log, list):
        state["log"] = [row for row in log if isinstance(row, dict)]
    return state


def persist_state(state: dict, path: str) -> str:
    """Atomically persist the commitments state (same pattern as actuals).

    tmp file + fsync + ``os.replace`` in the destination directory, so a
    crash or concurrent boot never observes a truncated file; keys are
    sorted for deterministic bytes given identical content.
    """
    doc = {
        "schema": 1,
        "mock_data": True,  # OR-5: commitments over synthetic aircraft
        "aircraft": {k: state["aircraft"][k] for k in sorted(state["aircraft"])},
        "log": list(state["log"]),
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".ff-commitments-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# evidence -> reason strings
# ---------------------------------------------------------------------------


def _aircraft_evidence(evidence: dict, aircraft: int) -> dict:
    """Evidence causes for one aircraft.

    Accepts either the per-aircraft shape ``{str(ac): {cause: detail}}``
    (what :func:`build_evidence` produces) or a flat run-wide
    ``{cause: detail}`` dict applying to every aircraft.
    """
    if not isinstance(evidence, dict):
        return {}
    per_ac = evidence.get(str(aircraft), evidence.get(aircraft))
    if isinstance(per_ac, dict):
        return per_ac
    if any(k in evidence for k in EVIDENCE_KEYS):
        return evidence
    return {}


def _slip_reason(evidence: dict, aircraft: int) -> str:
    """Concatenate the evidence causes present for a slipping aircraft.

    Fixed EVIDENCE_KEYS order, ``key=N`` when the detail is a count;
    fallback ``"global replan shift"`` when no cause was recorded — a slip
    is NEVER logged without a reason string (OR-4: evidence, not vibes).
    """
    causes = _aircraft_evidence(evidence, aircraft)
    parts: list[str] = []
    for key in EVIDENCE_KEYS:
        value = causes.get(key)
        if not value:
            continue
        if isinstance(value, bool):
            parts.append(key)
        elif isinstance(value, int):
            parts.append(f"{key}={value}")
        else:
            parts.append(f"{key} ({value})")
    return " + ".join(parts) if parts else FALLBACK_SLIP_REASON


# ---------------------------------------------------------------------------
# THE update rule (OR-4 / MAX §11 — tripwire-tested)
# ---------------------------------------------------------------------------


def update_commitments(
    state: dict, projections: list[dict], run_id: str, evidence: dict
) -> tuple[dict, list[dict]]:
    """Apply one run's projections to the commitments state.

    Enforces the module-docstring rules exactly: (1) first sighting
    commits immediately; (2) jitter (< COMMIT_HYSTERESIS_DAYS) holds and
    clears pending; (3) >= COMMIT_WORSEN_IMMEDIATE_DAYS recommits at once
    with evidence ("bad news fast"); (4) all else needs a pending streak of
    COMMIT_PERSISTENCE_RUNS consecutive runs at the same target (±1 day),
    and the pending streak resets when the target moves.

    ``state`` is mutated in place and returned together with the list of
    log rows appended by THIS run
    (``{"run", "aircraft", "old", "new", "delta_days", "reason"}``).
    Aircraft absent from ``projections`` keep their commitment untouched.
    Deterministic: projections are processed sorted by aircraft number.
    """
    if not isinstance(state, dict) or "aircraft" not in state:
        state = empty_state()
    state.setdefault("aircraft", {})
    state.setdefault("log", [])
    changes: list[dict] = []

    def _log(aircraft: int, old, new: int, reason: str) -> None:
        row = {
            "run": run_id,
            "aircraft": aircraft,
            "old": old,
            "new": new,
            "delta_days": (new - old) if old is not None else 0,
            "reason": reason,
        }
        state["log"].append(row)
        changes.append(row)

    rows = sorted(
        (r for r in projections if isinstance(r, dict) and "aircraft" in r),
        key=lambda r: int(r["aircraft"]),
    )
    for row in rows:
        aircraft = int(row["aircraft"])
        projected = int(row["projected_day"])
        key = str(aircraft)
        entry = state["aircraft"].get(key)

        # Rule 1 — first sighting commits immediately.
        if entry is None:
            state["aircraft"][key] = {"committed_day": projected, "pending": None}
            _log(aircraft, None, projected, INITIAL_REASON)
            continue

        committed = int(entry["committed_day"])
        delta = projected - committed

        # Rule 2 — jitter: hold the commitment, clear any pending streak.
        if abs(delta) < config.COMMIT_HYSTERESIS_DAYS:
            entry["pending"] = None
            continue

        # Rule 3 — bad news fast: big worsening recommits at once.
        if delta >= config.COMMIT_WORSEN_IMMEDIATE_DAYS:
            entry["committed_day"] = projected
            entry["pending"] = None
            _log(aircraft, committed, projected, _slip_reason(evidence, aircraft))
            continue

        # Rule 4 — persistence: small slips AND improvements must be
        # sustained at the same target (±1 day) for COMMIT_PERSISTENCE_RUNS
        # consecutive runs; the pending streak resets when the target moves.
        pending = entry.get("pending")
        if pending is None or abs(projected - int(pending["target"])) > 1:
            pending = {"target": projected, "streak": 1}
        else:
            pending = {"target": int(pending["target"]), "streak": int(pending["streak"]) + 1}
        if pending["streak"] >= config.COMMIT_PERSISTENCE_RUNS:
            base = (
                _slip_reason(evidence, aircraft) if delta > 0 else IMPROVEMENT_REASON
            )
            reason = f"{base} (sustained {pending['streak']} replans)"
            entry["committed_day"] = projected
            entry["pending"] = None
            _log(aircraft, committed, projected, reason)
        else:
            entry["pending"] = pending

    return state, changes


# ---------------------------------------------------------------------------
# integration helpers (app boot / replan wiring)
# ---------------------------------------------------------------------------


def projections_from_stats(stats: dict) -> list[dict]:
    """Extract projection rows from ``Schedule.stats.aircraft`` completion days.

    Contract shape: ``[{"aircraft": int, "projected_day": int}]`` sorted by
    aircraft; rows without a completion_day are skipped (never invented).
    """
    rows: list[dict] = []
    for entry in stats.get("aircraft", []) or []:
        if not isinstance(entry, dict) or entry.get("completion_day") is None:
            continue
        rows.append(
            {
                "aircraft": int(entry["aircraft"]),
                "projected_day": int(entry["completion_day"]),
            }
        )
    rows.sort(key=lambda r: r["aircraft"])
    return rows


def build_evidence(fleet, schedule) -> dict:
    """Per-aircraft evidence counts for slip reasons (deterministic).

    Maps ``str(aircraft) -> {cause: count}`` over the EVIDENCE_KEYS, only
    non-zero causes recorded:

    - ``rework_inserted``:      not-started rework tasks (new punch work);
    - ``execution_shortfall``:  in-progress tasks (started, unfinished);
    - ``blocked_parts``:        blocked tasks (material waits);
    - ``capacity``:             tasks unscheduled ``no_crew_within_horizon``.
    """
    by_ac: dict[int, dict[str, int]] = {}

    def _bump(aircraft: int, key: str) -> None:
        causes = by_ac.setdefault(aircraft, {})
        causes[key] = causes.get(key, 0) + 1

    tasks = sorted(fleet.tasks, key=lambda t: t.task_id)
    for task in tasks:
        if task.is_rework and task.state == "not_started":
            _bump(task.aircraft, "rework_inserted")
        if task.state == "in_progress":
            _bump(task.aircraft, "execution_shortfall")
        if task.state == "blocked":
            _bump(task.aircraft, "blocked_parts")
    task_by_id = {t.task_id: t for t in tasks}
    for tid in sorted(schedule.unscheduled):
        if schedule.unscheduled[tid] != "no_crew_within_horizon":
            continue
        task = task_by_id.get(tid)
        if task is not None:
            _bump(task.aircraft, "capacity")

    return {
        str(ac): {k: by_ac[ac][k] for k in EVIDENCE_KEYS if k in by_ac[ac]}
        for ac in sorted(by_ac)
    }


def snapshot_block(
    state: dict, projections: list[dict], deadlines: dict, run_id: str
) -> dict:
    """The snapshot ``commitments`` block (contract addendum shape).

    ``{committed: [{aircraft, committed_day, projected_day, deadline_day,
    delta}], changes: [last 50 log rows], run_id}`` — committed rows sorted
    by aircraft, delta = projected - committed (positive = the plan now
    lands AFTER the promise).
    """
    committed_days = state.get("aircraft", {})
    rows: list[dict] = []
    for proj in sorted(projections, key=lambda r: int(r["aircraft"])):
        aircraft = int(proj["aircraft"])
        entry = committed_days.get(str(aircraft))
        if entry is None:
            continue
        committed = int(entry["committed_day"])
        projected = int(proj["projected_day"])
        rows.append(
            {
                "aircraft": aircraft,
                "committed_day": committed,
                "projected_day": projected,
                "deadline_day": deadlines.get(aircraft),
                "delta": projected - committed,
            }
        )
    return {
        "committed": rows,
        "changes": list(state.get("log", []))[-50:],
        "run_id": run_id,
    }
