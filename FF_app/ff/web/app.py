"""ff.web.app — Flask application factory, demo role sessions, health, views.

Contract (ARCHITECTURE.md §ff/web — Flask, Tanzu-ready):

- ``create_app()`` factory; gunicorn entry ``ff.web.app:create_app()``.
- Boot: load fleet fixture (``FF_DATA`` env, default ``data/fleet50.json.gz``,
  fallback ``data/mini/fleet.json.gz``, last-resort in-memory mini generate so
  the app ALWAYS boots), apply ``data/actuals.json`` overrides if present,
  run CPM + the deterministic scheduler (or load ``FF_SCHEDULE`` if given),
  wire economics + capacity into ``Schedule.stats``, run the V1..V9
  validator, build the snapshot. Everything lands on
  ``app.extensions["ff"]``; a boot failure is recorded (never raised out of
  the factory) so ``/healthz`` stays alive and ``/readyz`` reports 503.
- Health: ``GET /healthz`` always 200 (liveness); ``GET /readyz`` 200 only
  when the snapshot is built, else 503 (readiness).
- Demo auth: ``POST /login`` stores ``{role, scope}`` in the session;
  role landing redirects; ``GET /logout``. SERVER-SIDE scope filtering (RS
  rules) is centralized in :func:`resolve_scope` / :func:`team_allowed` and
  applied by every view here and every API route in ``ff.web.api``.
- Views (server-rendered, no CDN, one base.html): /mechanic /lead /flm
  /super /director /vp. Every page carries the ALWAYS-visible mock-data
  banner: "SYNTHETIC DATA" + snapshot_id + freshness stamp (OR-5).

Owner rules touched here:

- OR-5: the banner + placeholder-economics labels are rendered on every
  page; ``mock_data`` comes from ``fleet.meta`` and is shown honestly.
- OR-6: this module performs NO fleet mutations; the single actuals
  write-path is ``POST /api/v1/actuals`` (ff.web.api), which calls back
  into :func:`persist_actuals` here for the atomic disk write. The
  OR-6-adjacent excusal capture (``POST /api/v1/excusals`` — FOCU5-local,
  never mutates task state) likewise persists only through
  :func:`persist_excusals`; commitments persist inside
  :func:`rebuild_state` via ``ff.services.commitments`` (§9, OR-4).

Determinism: all view assembly iterates in sorted order; the only
wall-clock values are freshness stamps (which are inherently wall-clock).
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

import config
from ff.data import loader
from ff.domain import Fleet, Schedule, TASK_STATES

# ---------------------------------------------------------------------------
# constants & paths
# ---------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parents[2]  # .../FF_app

DEFAULT_FLEET_REL = "data/fleet50.json.gz"
MINI_FLEET_REL = "data/mini/fleet.json.gz"
ACTUALS_REL = "data/actuals.json"
COMMITMENTS_REL = "data/commitments.json"
EXCUSALS_REL = "data/excusals.json"
GAME_EVENTS_REL = "data/game_events.jsonl"
LLM_AUDIT_REL = "data/llm_audit.jsonl"
LLM_CASSETTES_REL = "ff/llm/cassettes"

ROLES: tuple[str, ...] = ("mechanic", "lead", "flm", "super", "director", "vp")
ROLE_HOME: dict[str, str] = {r: "/" + r for r in ROLES}
ROLE_LABELS: dict[str, str] = {
    "mechanic": "My Day+",
    "lead": "Crew Board",
    "flm": "Shift Command",
    "super": "Position Control",
    "director": "Analytics",
    "vp": "Risk & Commitments",
}
TEAM_GROUP_SIZE = config.TEAM_GROUP_SIZE  # super scope rule — config §10
# (shared with ff.services.scorecard so grading and auth scopes never drift)
MECHANIC_MAX_ROWS = 40  # rows enriched with feasibility/points per render
LIST_CAP = 30  # blocked/unscheduled list caps on boards

_FEAS_CLASS = {
    "DONE": "muted",
    "IN_PROGRESS": "info",
    "READY": "ok",
    "WAITING_PREDECESSOR": "warn",
    "WAITING_PART": "warn",
    "WAITING_CREW": "block",
}


# ---------------------------------------------------------------------------
# small shared helpers (also used by ff.web.api)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC freshness stamp (wall-clock by nature; not an engine input)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_path(rel: str) -> str:
    """Resolve a config path: absolute as-is, else FF_app-root-relative.

    Enforces 12-factor 'no absolute paths in code': everything is anchored
    to the app root so cwd differences (gunicorn vs run.py) don't matter.
    A cwd-relative file that exists wins over a nonexistent root-relative
    one, so ``FF_DATA=./tmp/f.json.gz`` style overrides still work.
    """
    if os.path.isabs(rel):
        return rel
    rooted = str(APP_ROOT / rel)
    if os.path.exists(rooted):
        return rooted
    if os.path.exists(rel):
        return os.path.abspath(rel)
    return rooted


def _svc(name: str):
    """Lazily import ``ff.services.<name>``; return (module|None, error|None).

    Services are built by sibling modules against the same contract; lazy
    import keeps this app importable (and /healthz alive) during partial
    builds — a missing service surfaces as an HONEST per-section error or
    a 503, never a fake result.
    """
    try:
        return importlib.import_module(f"ff.services.{name}"), None
    except Exception as exc:  # ImportError or a module-level bug
        return None, f"{type(exc).__name__}: {exc}"


def _call(fn, *args, **kwargs):
    """Call a service function; return (value, None) or (None, error string).

    Honesty rule: view sections never fabricate data — a failing service
    yields an error string that the template renders verbatim.
    """
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _as_plain(obj):
    """Normalize service outputs (dataclass / to_dict / namespace) to plain
    JSON-safe python. Defensive: sibling modules own their concrete types;
    the web layer only assumes the CONTRACT-level field names."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _as_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        seq = sorted(obj, key=str) if isinstance(obj, (set, frozenset)) else obj
        return [_as_plain(v) for v in seq]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _as_plain(dataclasses.asdict(obj))
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _as_plain(to_dict())
    if hasattr(obj, "__dict__"):
        return _as_plain(vars(obj))
    return str(obj)


def snap_get(snap, *names, default=None):
    """Read the first present, non-None field from a Snapshot object or dict.

    The Snapshot concrete type belongs to ff.services.snapshot; the web
    layer reads only contract fields (snapshot_id, freshness) and tolerates
    either attribute or mapping access.
    """
    for name in names:
        if isinstance(snap, dict):
            value = snap.get(name)
        else:
            value = getattr(snap, name, None)
        if value is not None:
            return value
    return default


# ---------------------------------------------------------------------------
# state assembly (boot + replan pipeline)
# ---------------------------------------------------------------------------


def _load_fleet(state: dict) -> tuple[Fleet, str]:
    """Load the fleet fixture with the always-boot fallback chain.

    Order: ``FF_DATA`` env > ``data/fleet50.json.gz`` > ``data/mini/
    fleet.json.gz`` > generate a mini fleet in memory. The app must ALWAYS
    boot (contract §ff/web); every fallback taken is recorded as an honest
    warning surfaced on the VP data-trust panel.
    """
    candidates: list[tuple[str, str]] = []
    env_data = os.environ.get("FF_DATA", "")
    if env_data:
        candidates.append((env_data, "FF_DATA"))
    candidates.append((DEFAULT_FLEET_REL, "default"))
    candidates.append((MINI_FLEET_REL, "mini-fallback"))
    for rel, label in candidates:
        path = resolve_path(rel)
        if not os.path.exists(path):
            if label == "FF_DATA":
                state["warnings"].append(
                    f"FF_DATA={rel!r} not found; falling back to bundled fixtures"
                )
            continue
        try:
            fleet = Fleet.from_dict(loader.load_json_gz(path))
            return fleet, f"{rel} ({label})"
        except Exception as exc:
            state["warnings"].append(f"failed to load {rel!r}: {exc}")
    # Last resort so the app always boots: tiny in-memory synthetic fleet.
    from ff.data.generator import generate_fleet

    state["warnings"].append(
        "no fleet fixture found on disk; generated an in-memory mini fleet "
        f"(3 aircraft, seed {config.GEN_SEED})"
    )
    fleet = generate_fleet(3, 40, 3, 30, config.GEN_SEED)
    return fleet, "generated-in-memory (mini, 3 aircraft)"


def _index_fleet(state: dict) -> None:
    """Build deterministic lookup indexes: tasks, mechanics, teams, groups.

    Team groups (super scope) are contiguous chunks of TEAM_GROUP_SIZE over
    the SORTED team list — pure function of the fleet, identical every boot.
    """
    fleet: Fleet = state["fleet"]
    state["task_by_id"] = {t.task_id: t for t in fleet.tasks}
    state["mech_by_id"] = {m.mech_id: m for m in fleet.mechanics}
    teams = sorted({t.team for t in fleet.tasks} | {m.team for m in fleet.mechanics})
    state["teams"] = teams
    groups: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for i in range(0, len(teams), TEAM_GROUP_SIZE):
        chunk = teams[i : i + TEAM_GROUP_SIZE]
        gid = f"G{i // TEAM_GROUP_SIZE + 1}"
        groups[gid] = chunk
        labels[gid] = f"{gid} ({chunk[0]}–{chunk[-1]})"
    state["groups"] = groups
    state["group_labels"] = labels


def _read_actuals(state: dict) -> dict:
    """Read ``data/actuals.json`` (if present) into {task_id: override}.

    Accepts the wrapped document written by :func:`persist_actuals` or a
    bare map (hand-authored). Unreadable files are a warning, never a
    boot failure.
    """
    path = state["actuals_path"]
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        entries = doc.get("actuals", doc) if isinstance(doc, dict) else {}
        out = {}
        for tid in sorted(entries):
            if isinstance(entries[tid], dict):
                out[str(tid)] = dict(entries[tid])
        return out
    except Exception as exc:
        state["warnings"].append(f"failed to read actuals file {path!r}: {exc}")
        return {}


def _apply_actuals(state: dict) -> int:
    """Apply actuals overrides onto the in-memory fleet (sorted order).

    Same semantics as the live write-path (OR-6): only ``state`` and
    ``remaining_minutes`` are overridable; unknown task ids / states are
    warned and skipped, never invented.
    """
    applied = 0
    by_id = state["task_by_id"]
    for tid in sorted(state["actuals"]):
        entry = state["actuals"][tid]
        task = by_id.get(tid)
        if task is None:
            state["warnings"].append(f"actuals: unknown task_id {tid!r} skipped")
            continue
        new_state = entry.get("state")
        if new_state not in TASK_STATES:
            state["warnings"].append(
                f"actuals: task {tid} has invalid state {new_state!r}; skipped"
            )
            continue
        task.state = new_state
        rm = entry.get("remaining_minutes")
        task.remaining_minutes = int(rm) if rm is not None else None
        applied += 1
    return applied


def persist_actuals(state: dict) -> str:
    """Atomically persist the actuals map to ``data/actuals.json`` (OR-6).

    Single write-path rule: ONLY ``POST /api/v1/actuals`` calls this.
    Atomic write (tmp file + fsync + ``os.replace`` in the same directory)
    so a crash or concurrent boot never sees a truncated file. Keys are
    sorted for deterministic bytes given identical content.
    """
    path = state["actuals_path"]
    doc = {
        "schema": 1,
        "mock_data": True,  # OR-5: this file describes synthetic tasks
        "updated_at": _now_iso(),
        "actuals": {tid: state["actuals"][tid] for tid in sorted(state["actuals"])},
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ff-actuals-", suffix=".json")
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


def _read_excusals(state: dict) -> list:
    """Read ``data/excusals.json`` (if present) into the manual-capture list.

    Accepts the wrapped document written by :func:`persist_excusals` or a
    bare list (hand-authored). Unreadable files are a warning, never a
    boot failure. Order is preserved — the file is an append log.
    """
    path = state["excusals_path"]
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        entries = doc.get("excusals", doc) if isinstance(doc, dict) else doc
        if not isinstance(entries, list):
            return []
        return [dict(e) for e in entries if isinstance(e, dict)]
    except Exception as exc:
        state["warnings"].append(f"failed to read excusals file {path!r}: {exc}")
        return []


def persist_excusals(state: dict) -> str:
    """Atomically persist the manual excusal log to ``data/excusals.json``.

    Append semantics with atomic bytes: the in-memory list (already
    appended to by the ONLY writer, ``POST /api/v1/excusals``) is written
    whole via tmp file + fsync + ``os.replace`` — a crash or concurrent
    boot never sees a truncated log. List order (capture order) is kept.
    """
    path = state["excusals_path"]
    doc = {
        "schema": 1,
        "mock_data": True,  # OR-5: excusals describe synthetic tasks
        "updated_at": _now_iso(),
        "excusals": list(state["excusals"]),
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ff-excusals-", suffix=".json")
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


def _snapshot_id(snap, fleet: Fleet, schedule: Schedule) -> str:
    """Snapshot identity: the service's ``snapshot_id`` field, with a
    deterministic sha256 fallback (seed + assignments) if absent — the
    banner must never show a fabricated random id."""
    sid = snap_get(snap, "snapshot_id", "id")
    if sid:
        return str(sid)
    payload = json.dumps(
        {
            "seed": fleet.meta.get("seed"),
            "assignments": {
                tid: schedule.assignments[tid].to_dict()
                for tid in sorted(schedule.assignments)
            },
            "unscheduled": dict(sorted(schedule.unscheduled.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def rebuild_state(state: dict) -> None:
    """The one build pipeline: CPM -> schedule -> economics/capacity wiring
    -> V1..V9 validation -> snapshot. Used at boot and by POST /replan.

    Rules enforced:
    - Deterministic engine inputs only (in-memory fleet + start_day); the
      scheduler itself guarantees identical output for identical input.
    - Integrator duty: ``Schedule.stats['economics']`` and
      ``['capacity_pressure']`` are shipped as {} by the engine and wired
      here from ff.engine.economics / ff.services.capacity.
    - ``FF_SCHEDULE`` (a prebuilt schedule fixture) is honored on the FIRST
      build only; every replan recomputes from live state.
    - OR-5: ``mock_data`` is propagated from fleet.meta, never assumed.
    - Commitments (§9, OR-4): every build — boot AND replan (the replan
      single-flight lock is already held by the caller) — feeds the run's
      projections + evidence through ``commitments.update_commitments``,
      persists ``data/commitments.json`` atomically, and attaches the
      snapshot ``commitments`` block. Manual excusals ride on the snapshot
      (``manual_excusals``) so points/GG-3 sees lead captures immediately.
    - Commitment defense (§12, OR-4: "committed work dispatches before new
      work"): boot keeps its schedule (no previous plan exists in memory =>
      ``incumbent=None``, byte-identical engine behavior); every replan
      threads ``incumbent_from_schedule(previous schedule)`` so surviving
      in-horizon work defends its incumbent slot + crew.
    """
    from ff.engine.cpm import compute_cpm
    from ff.engine.economics import fleet_economics
    from ff.engine.scheduler import build_schedule, incumbent_from_schedule
    from ff.engine.validator import validate
    from ff.services import capacity
    from ff.services import commitments as commitments_svc
    from ff.services.snapshot import build_snapshot

    fleet: Fleet = state["fleet"]
    start_day: int = state.get("start_day", 0)
    # §12 COMMITMENT: the previous in-memory schedule (boot has none) is the
    # incumbent the next build defends inside the commitment horizon.
    previous: Schedule | None = state.get("schedule")

    cpm = compute_cpm(fleet.tasks)
    schedule: Schedule | None = None
    if state.get("build_count", 0) == 0:
        sched_env = os.environ.get("FF_SCHEDULE", "")
        if sched_env:
            try:
                schedule = Schedule.from_dict(
                    loader.load_json_gz(resolve_path(sched_env))
                )
                state["warnings"].append(f"loaded prebuilt schedule FF_SCHEDULE={sched_env!r}")
            except Exception as exc:
                state["warnings"].append(
                    f"FF_SCHEDULE={sched_env!r} unusable ({exc}); computing instead"
                )
                schedule = None
    if schedule is None:
        incumbent = (
            incumbent_from_schedule(previous) if previous is not None else None
        )
        schedule = build_schedule(
            fleet, cpm, start_day=start_day, incumbent=incumbent
        )

    # Integrator wiring (see ff/engine/scheduler._build_stats docstring).
    schedule.stats["economics"] = fleet_economics(
        schedule.stats.get("aircraft", []), today_day=start_day
    )

    class _SnapView:
        """Minimal fleet+schedule view for capacity BEFORE the snapshot exists."""

        def __init__(self, fleet, schedule, cpm):
            self.fleet, self.schedule, self.cpm = fleet, schedule, cpm

    schedule.stats["capacity_pressure"] = capacity.pressure(
        _SnapView(fleet, schedule, cpm)
    )

    report = validate(schedule, fleet)
    snap = build_snapshot(fleet, schedule, cpm)

    state["cpm"] = cpm
    state["schedule"] = schedule
    state["snapshot"] = snap
    state["snapshot_id"] = _snapshot_id(snap, fleet, schedule)

    # -- commitments (§9, OR-4) + excusal wiring ------------------------------
    run_id = state["snapshot_id"]
    cstate = state.get("commitments")
    if not isinstance(cstate, dict):
        cstate = commitments_svc.load_state(state["commitments_path"])
    projections = commitments_svc.projections_from_stats(schedule.stats)
    evidence = commitments_svc.build_evidence(fleet, schedule)
    cstate, _changes = commitments_svc.update_commitments(
        cstate, projections, run_id, evidence
    )
    state["commitments"] = cstate
    try:
        commitments_svc.persist_state(cstate, state["commitments_path"])
    except OSError as exc:  # disk trouble must not take readiness down
        state["warnings"].append(f"failed to persist commitments: {exc}")
    deadlines = {a.aircraft: a.delivery_deadline_day for a in fleet.aircraft}
    if isinstance(snap, dict):
        snap["commitments"] = commitments_svc.snapshot_block(
            cstate, projections, deadlines, run_id
        )
        # Same list OBJECT as state["excusals"]: POST /api/v1/excusals
        # appends there, so GG-3 sees new captures without a replan.
        snap["manual_excusals"] = state.setdefault("excusals", [])

    # -- GAMES progression (§10, GG-1): derive events from the summary diff --
    # Events are DERIVED ONLY (GG-1): the diff between the previous run's
    # summary and this snapshot, plus the commitments change log. The very
    # first build is the baseline (no prev summary => no events). A failing
    # progression service degrades to an HONEST warning, never fake events.
    prog_mod, prog_err = _svc("progression")
    state.setdefault("game_events", [])
    if prog_mod is not None:
        try:
            appended, summary, earn_rates = prog_mod.advance(
                state.get("game_summary"), state["game_events"], snap
            )
            state["game_summary"] = summary
            state["earn_rates"] = earn_rates
            if appended:
                try:
                    prog_mod.persist_events(
                        state["game_events"], state["game_events_path"]
                    )
                except OSError as exc:  # disk trouble must not break readiness
                    state["warnings"].append(f"failed to persist game events: {exc}")
        except Exception as exc:
            state["warnings"].append(
                f"progression derivation failed: {type(exc).__name__}: {exc}"
            )
    else:
        state["warnings"].append(f"progression unavailable: {prog_err}")
    if isinstance(snap, dict):
        # Ride-along reference (like manual_excusals): recap/build views and
        # the progression API read the one append-only log.
        snap["game_events"] = state["game_events"]
    # Snapshot contract: 'created_at' is the UTC freshness stamp (the
    # 'freshness' key is the {mock_data, seed} honesty block, NOT a time).
    stamp = snap_get(snap, "created_at", "built_at", "generated_at")
    state["built_at"] = stamp if isinstance(stamp, str) else _now_iso()
    state["validator_summary"] = dict(report.get("summary", {}))
    state["validator_total"] = int(
        report.get("summary", {}).get("total", len(report.get("violations", [])))
    )
    state["mock_data"] = bool(fleet.meta.get("mock_data", False))
    state["build_count"] = state.get("build_count", 0) + 1
    state["boot_error"] = None

    # -- INCREMENT 6 Part A: max_v1 envelope export on the REPLAN path -------
    # Every replan (a previous in-memory schedule exists; boot does not
    # export) writes a fresh envelope for the vendored FOCUS dashboard
    # (web_flask discovers by file mtime, newest first). The commitments
    # block just attached to the snapshot rides into metadata.stats.
    # FF_ENVELOPE_DIR overrides the default outputs/schedules;
    # FF_ENVELOPE_EXPORT=0 disables. Failure is an HONEST warning, never a
    # failed replan (the in-memory state above is already consistent).
    if previous is not None and os.environ.get("FF_ENVELOPE_EXPORT", "1") != "0":
        try:
            from ff.export.envelope import export_envelope

            out_dir = resolve_path(
                os.environ.get("FF_ENVELOPE_DIR", "") or "outputs/schedules"
            )
            env_path = export_envelope(
                fleet, schedule, cpm, snapshot=snap, out_dir=out_dir
            )
            state["last_envelope"] = str(env_path)
        except Exception as exc:
            state["warnings"].append(
                f"envelope export failed: {type(exc).__name__}: {exc}"
            )


# ---------------------------------------------------------------------------
# scope model (RS rules — server-side, used by views here AND ff.web.api)
# ---------------------------------------------------------------------------


def _build_llm_provider(state: dict):
    """Construct the configured LLM provider, or None (LB-8: absent = the
    deterministic surfaces simply render without garnish).

    Selection: env ``FF_LLM_PROVIDER`` at factory time (test-friendly),
    else ``config.LLM_PROVIDER`` (§11; default "mock" — deterministic,
    offline, credential-free, so ALL dev and CI runs need zero
    credentials, L1 mission). "bcai" wires the PAT supplier to the
    SERVER-SIDE SESSION ONLY (LB-9 credential law: never disk, never
    committed env, never construction-time). A broken provider is an
    honest warning + None, never a boot failure.
    """
    kind = (os.environ.get("FF_LLM_PROVIDER", "") or config.LLM_PROVIDER).strip().lower()
    try:
        if kind in ("", "none", "off"):
            return None
        from ff.llm.provider import (
            BCAIChatGPTProvider,
            MockLLMProvider,
            RecordedResponseProvider,
        )

        if kind == "mock":
            return MockLLMProvider()
        if kind == "recorded":
            return RecordedResponseProvider(resolve_path(LLM_CASSETTES_REL))
        if kind == "bcai":
            # LB-9 (cited law): the PAT lives ONLY in the server session;
            # the supplier is evaluated per call inside a request context.
            return BCAIChatGPTProvider(
                pat_supplier=lambda: session.get("bcai_token", "")
            )
        state["warnings"].append(f"unknown LLM provider {kind!r}; LLM surface off")
        return None
    except Exception as exc:
        state["warnings"].append(
            f"LLM provider {kind!r} unavailable: {type(exc).__name__}: {exc}"
        )
        return None


def resolve_scope(state: dict) -> dict | None:
    """Resolve the session into a server-side scope, or None if not logged in.

    RS rule enforced (contract §ff/web): mechanic sees own tasks; lead/flm
    their team; super their team GROUP; director/vp all. Returns
    ``{"role", "scope", "mech_id", "teams"}`` where ``teams`` is a set of
    allowed teams or None meaning ALL. The client never supplies scope on a
    request — only at login — so tampering with query params cannot widen it.
    """
    role = session.get("role")
    scope = session.get("scope")
    if role not in ROLES or not isinstance(scope, str):
        return None
    mech_id = None
    teams: set[str] | None
    if role == "mechanic":
        mech_id = scope
        mech = state.get("mech_by_id", {}).get(scope)
        if mech is None:
            return None  # stale session against a different fixture
        teams = {mech.team}
    elif role in ("lead", "flm"):
        if scope not in state.get("teams", []):
            return None
        teams = {scope}
    elif role == "super":
        group = state.get("groups", {}).get(scope)
        if not group:
            return None
        teams = set(group)
    else:  # director / vp
        teams = None
    return {"role": role, "scope": scope, "mech_id": mech_id, "teams": teams}


def team_allowed(sc: dict, team: str) -> bool:
    """True iff ``team`` is inside the resolved scope (None teams = all).

    This is THE tamper check: a request naming an unauthorized team gets
    403 from every caller.
    """
    return sc["teams"] is None or team in sc["teams"]


def scope_label(state: dict, sc: dict) -> str:
    """Human label for the banner: mech id / team / group range / 'all'."""
    if sc["role"] == "super":
        return state.get("group_labels", {}).get(sc["scope"], sc["scope"])
    if sc["role"] in ("director", "vp"):
        return "all aircraft / all teams"
    return sc["scope"]


# ---------------------------------------------------------------------------
# view context builders
# ---------------------------------------------------------------------------


def _common_ctx(state: dict, sc: dict | None, engine_required: bool = True) -> dict:
    """Context shared by every page: OR-5 banner fields + nav + readiness."""
    ready = state.get("snapshot") is not None
    ctx = {
        "ready": ready,
        "engine_required": engine_required,
        "boot_error": state.get("boot_error"),
        "snapshot_id": state.get("snapshot_id"),
        "built_at": state.get("built_at"),
        "mock_data": bool(state.get("mock_data", False)),
        "data_source": state.get("data_source"),
        "validator_total": state.get("validator_total"),
        "shift_labels": config.SHIFT_LABELS,
        "role": None,
        "scope": None,
        "scope_label": None,
        "home": "/login",
        "role_label": None,
    }
    if sc is not None:
        ctx.update(
            role=sc["role"],
            scope=sc["scope"],
            scope_label=scope_label(state, sc),
            home=ROLE_HOME[sc["role"]],
            role_label=ROLE_LABELS[sc["role"]],
        )
    return ctx


def _feas_row(feas_mod, feas_err, tid, snap) -> dict:
    """Feasibility fragment for one task row (status chip + reason codes)."""
    if feas_mod is None:
        return {"feas": None, "feas_class": "err", "feas_error": feas_err}
    value, err = _call(feas_mod.evaluate, tid, snap)
    if err:
        return {"feas": None, "feas_class": "err", "feas_error": err}
    plain = _as_plain(value) or {}
    status = str(plain.get("status", "?"))
    return {
        "feas": plain,
        "feas_class": _FEAS_CLASS.get(status, "info"),
        "feas_error": None,
    }


def _score_chips(points_mod, pts_err, tid, snap) -> dict:
    """Why-points chips for one task: top score components by points."""
    if points_mod is None:
        return {"chips": [], "score_total": None, "points_error": pts_err}
    value, err = _call(points_mod.score_task, tid, snap)
    if err:
        return {"chips": [], "score_total": None, "points_error": err}
    plain = _as_plain(value) or {}
    components = plain.get("components") or {}
    chips = []
    if isinstance(components, dict):
        for name in sorted(components):
            comp = components[name]
            pts = comp.get("points", comp) if isinstance(comp, dict) else comp
            try:
                pts = round(float(pts), 1)
            except (TypeError, ValueError):
                pts = 0.0
            chips.append({"name": name, "points": pts})
        chips.sort(key=lambda c: (-c["points"], c["name"]))
    total = plain.get("total", plain.get("score", plain.get("points")))
    try:
        total = round(float(total), 1) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return {"chips": chips[:4], "score_total": total, "points_error": None}


def _mechanic_ctx(state: dict, sc: dict) -> dict:
    """My Day+ data: MY ordered tasks with readiness + why-points chips.

    RS rule: only assignments whose named crew contains this mechanic are
    shown — a mechanic's page is built purely from their own tasks.
    """
    mech = state["mech_by_id"][sc["mech_id"]]
    schedule: Schedule = state["schedule"]
    snap = state["snapshot"]
    mine = [
        schedule.assignments[tid]
        for tid in sorted(schedule.assignments)
        if sc["mech_id"] in schedule.assignments[tid].mechanic_ids
    ]
    mine.sort(key=lambda a: (a.day, a.shift, a.start_minute, a.task_id))
    feas_mod, feas_err = _svc("feasibility")
    points_mod, pts_err = _svc("points")
    rows = []
    for asg in mine[:MECHANIC_MAX_ROWS]:
        task = state["task_by_id"].get(asg.task_id)
        row = {
            "task_id": asg.task_id,
            "name": task.name if task else asg.task_id,
            "day": asg.day,
            "shift": asg.shift,
            "start": asg.start_minute,
            "end": asg.end_minute,
            "uses_overtime": asg.uses_overtime,
            "crew": list(asg.mechanic_ids),
            "state": task.state if task else "?",
            "remaining": task.remaining_minutes if task else None,
            "skill": asg.skill,
        }
        row.update(_feas_row(feas_mod, feas_err, asg.task_id, snap))
        row.update(_score_chips(points_mod, pts_err, asg.task_id, snap))
        rows.append(row)
    team_unscheduled = sum(
        1
        for tid in schedule.unscheduled
        if state["task_by_id"].get(tid) is not None
        and state["task_by_id"][tid].team == mech.team
    )
    return {
        "mech": {"mech_id": mech.mech_id, "team": mech.team, "shift": mech.shift,
                 "skills": list(mech.skills)},
        "rows": rows,
        "total_rows": len(mine),
        "shown_rows": len(rows),
        "states": list(TASK_STATES),
        "team_unscheduled": team_unscheduled,
        "contribution": _my_contribution(state, mine, points_mod, pts_err),
    }


def _my_contribution(state: dict, mine: list, points_mod, pts_err) -> dict:
    """'My contribution' panel data — SELF-SCOPE ONLY (§G8 privacy default).

    §G8 / PSY-5 (cited law): individual visibility is team-first and
    opt-in; a mechanic's personal stats are theirs alone. This panel is
    assembled strictly from the LOGGED-IN mechanic's own crewed
    assignments (the same self-scope the whole /mechanic page already
    enforces) and is rendered opt-in (collapsed <details>, the user opens
    it). No other persona's surface aggregates per-mechanic points
    anywhere in the app. GG-2: contribution = point value of completed
    work, never raw task count alone.
    """
    out = {"done_count": 0, "points": 0, "top": [], "error": pts_err}
    if points_mod is None:
        return out
    snap = state["snapshot"]
    scored: list[dict] = []
    for asg in mine:
        task = state["task_by_id"].get(asg.task_id)
        if task is None or task.state != "done":
            continue
        value, err = _call(points_mod.score_task, asg.task_id, snap)
        if err:
            out["error"] = err
            continue
        plain = _as_plain(value) or {}
        scored.append(
            {
                "task_id": asg.task_id,
                "points": plain.get("total", 0),
                "explanation": plain.get("explanation", ""),
            }
        )
    scored.sort(key=lambda r: (-r["points"], r["task_id"]))
    out["done_count"] = len(scored)
    out["points"] = sum(r["points"] for r in scored)
    out["top"] = scored[:3]
    return out


def _team_candidates(state: dict, sc: dict, team: str, limit: int) -> dict:
    """Ranked candidates for one team via ff.services.candidates.rank.

    Defense in depth: even though ``rank`` receives the scope, results are
    re-filtered server-side so no candidate outside the caller's scope can
    render (RS rule).
    """
    cand_mod, cerr = _svc("candidates")
    if cand_mod is None:
        return {"rows": [], "error": cerr}
    scope_dict = {
        "role": sc["role"],
        "scope": sc["scope"],
        "team": team,
        "teams": [team],
        "shift": None,
        "mech_id": sc["mech_id"],
    }
    value, err = _call(cand_mod.rank, state["snapshot"], scope_dict, limit)
    if err:
        return {"rows": [], "error": err}
    rows = []
    for cand in value or []:
        plain = _as_plain(cand) or {}
        task = state["task_by_id"].get(plain.get("task_id", ""))
        if task is None or task.team != team or not team_allowed(sc, task.team):
            continue  # server-side scope filter, always
        comps = plain.get("components") or {}
        top = []
        if isinstance(comps, dict):
            for name in sorted(comps):
                c = comps[name]
                pts = c.get("points", c) if isinstance(c, dict) else c
                try:
                    pts = round(float(pts), 1)
                except (TypeError, ValueError):
                    pts = 0.0
                top.append({"name": name, "points": pts})
            top.sort(key=lambda c: (-c["points"], c["name"]))
        rows.append(
            {
                "rank": plain.get("rank"),
                "task_id": plain.get("task_id"),
                "name": task.name,
                "score": plain.get("score"),
                "components": top[:3],
                "reason_codes": plain.get("reason_codes") or [],
                "explanation": plain.get("explanation", ""),
            }
        )
    return {"rows": rows[:limit], "error": None}


def _team_cause_map(state: dict, team: str) -> tuple[dict, str | None]:
    """(task_id -> [excusal records], error) for one team's disruption causes.

    Merged view (auto attribution EXTENDED by lead captures — never
    replaced) filtered server-side to ``team`` (RS rule). A failing
    disruption service yields an honest error string, never fake chips.
    """
    disr_mod, derr = _svc("disruption")
    if disr_mod is None:
        return {}, derr
    merged, err = _call(
        disr_mod.merged_excusals, state["snapshot"], state.get("excusals", [])
    )
    if err:
        return {}, err
    by_task: dict = {}
    for (rec_team, _day, _shift), records in sorted(merged.items()):
        if rec_team != team:
            continue
        for rec in records:
            by_task.setdefault(rec["task_id"], []).append(
                {
                    "cause": rec["cause"],
                    "excusable": rec["excusable"],
                    "evidence": rec["evidence"],
                    "source": rec["source"],
                }
            )
    return by_task, None


def _capture_ctx() -> dict:
    """Cause dropdown context for the lead capture control (contract:
    cause must be in CAUSES; notes REQUIRED for SAME_TEAM_PREDECESSOR and
    DURATION_OVERRUN — the list is surfaced so the UI can say so)."""
    disr_mod, derr = _svc("disruption")
    if disr_mod is None:
        return {"causes": [], "notes_required": [], "error": derr}
    return {
        "causes": list(disr_mod.CAUSES),
        "notes_required": sorted(disr_mod.NOTES_REQUIRED),
        "error": None,
    }


def _lead_ctx(state: dict, sc: dict, day: int) -> dict:
    """Crew Board data: team assignments by mechanic, blocked list with
    reasons + disruption cause chips + the excusal capture control,
    top-10 candidates. RS rule: everything is filtered to the lead's own
    team — no cross-team rows can appear."""
    team = sc["scope"]
    fleet: Fleet = state["fleet"]
    schedule: Schedule = state["schedule"]
    roster = sorted(
        (m for m in fleet.mechanics if m.team == team), key=lambda m: m.mech_id
    )
    team_days = sorted(
        {schedule.assignments[tid].day for tid in schedule.assignments
         if schedule.assignments[tid].team == team}
    )
    if day < 0:
        day = team_days[0] if team_days else state.get("start_day", 0)
    board = {m.mech_id: {1: [], 2: [], 3: []} for m in roster}
    for tid in sorted(schedule.assignments):
        asg = schedule.assignments[tid]
        if asg.team != team or asg.day != day:
            continue
        task = state["task_by_id"].get(tid)
        chip = {
            "task_id": tid,
            "name": task.name if task else tid,
            "start": asg.start_minute,
            "end": asg.end_minute,
            "uses_overtime": asg.uses_overtime,
        }
        for mech_id in asg.mechanic_ids:
            if mech_id in board:
                board[mech_id][asg.shift].append(chip)
    for cells in board.values():
        for s in (1, 2, 3):
            cells[s].sort(key=lambda c: (c["start"], c["task_id"]))

    causes_by_task, causes_error = _team_cause_map(state, team)
    blocked = [
        {
            "task_id": t.task_id,
            "name": t.name,
            "parts_eta_day": t.parts_eta_day,
            "reason": (
                f"blocked; parts ETA day {t.parts_eta_day}"
                if t.parts_eta_day is not None
                else "blocked (no parts ETA recorded)"
            ),
            "causes": causes_by_task.get(t.task_id, []),
        }
        for t in sorted(fleet.tasks, key=lambda t: t.task_id)
        if t.team == team and t.state == "blocked"
    ]
    unscheduled = []
    for tid in sorted(schedule.unscheduled):
        task = state["task_by_id"].get(tid)
        if task is not None and task.team == team:
            unscheduled.append(
                {
                    "task_id": tid,
                    "name": task.name,
                    "reason": schedule.unscheduled[tid],
                    "causes": causes_by_task.get(tid, []),
                }
            )
    candidates = _team_candidates(state, sc, team, 10)
    return {
        "team": team,
        "day": day,
        "team_days": team_days[:14],
        "roster": [m.mech_id for m in roster],
        "board": board,
        "blocked": blocked[:LIST_CAP],
        "blocked_total": len(blocked),
        "unscheduled": unscheduled[:LIST_CAP],
        "unscheduled_total": len(unscheduled),
        "candidates": candidates,
        "capture": _capture_ctx(),
        "causes_error": causes_error,
        "recaps": _recap_cards(state, team, day),
    }


def _recap_cards(state: dict, team: str, day: int) -> dict:
    """Shift recap cards (S1–S3) for the lead view — deterministic (LB-8).

    LB-8 (cited law): the recap IS the deterministic fallback content —
    no LLM is involved anywhere; ``progression.build_recap`` assembles it
    purely from the snapshot + the derived event log. RS rule: the lead's
    own team only (enforced by the caller's scope). A failing service
    yields an honest error, never a fabricated recap.
    """
    prog_mod, perr = _svc("progression")
    out = {"cards": [], "error": perr}
    if prog_mod is None:
        return out
    snap = state["snapshot"]
    events = state.get("game_events", [])
    for shift in (1, 2, 3):
        value, err = _call(prog_mod.build_recap, team, day, shift, snap, events)
        if err:
            out["error"] = err
            continue
        out["cards"].append(_as_plain(value))
    return out


def _pct(value) -> float:
    """Attainment -> bar width %: fractions (<=1.5) scale x100; clamp 0..100."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= x <= 1.5:
        x *= 100.0
    return max(0.0, min(100.0, round(x, 1)))


def _flm_ctx(state: dict, sc: dict, day: int) -> dict:
    """Shift Command data: S1-S3 attainment, leaderboard, unscheduled causes.

    GG-2 enforced downstream: the leaderboard shown here is
    ``points.leaderboard`` output (attainment + efficiency), never a
    raw-points ranking."""
    team = sc["scope"]
    schedule: Schedule = state["schedule"]
    snap = state["snapshot"]
    points_mod, perr = _svc("points")
    if day < 0:
        day = state.get("start_day", 0)
    shifts = []
    for s in (1, 2, 3):
        entry = {"shift": s, "label": config.SHIFT_LABELS.get(s, f"S{s}"),
                 "report": None, "error": perr, "bar": 0.0}
        if points_mod is not None:
            value, err = _call(points_mod.shift_report, snap, team, day, s)
            plain = _as_plain(value) if value is not None else None
            entry["report"] = plain
            entry["error"] = err
            if isinstance(plain, dict):
                entry["bar"] = _pct(plain.get("attainment"))
        shifts.append(entry)
    leaderboard = {"rows": [], "error": perr}
    if points_mod is not None:
        value, err = _call(points_mod.leaderboard, snap, day)
        leaderboard["error"] = err
        if not err:
            rows = []
            for i, row in enumerate(_as_plain(value) or []):
                if isinstance(row, dict):
                    rows.append(
                        {
                            "rank": i + 1,
                            "team": row.get("team"),
                            "attainment": row.get("attainment"),
                            "efficiency": row.get("efficiency"),
                            "difficulty": row.get("difficulty"),
                            # PSY-5: needs-support framing + excusal
                            # context — never shaming copy.
                            "needs_support": bool(row.get("needs_support")),
                            "support": row.get("support"),
                            "mine": row.get("team") == team,
                        }
                    )
            leaderboard["rows"] = rows
    causes: dict[str, int] = {}
    for tid in sorted(schedule.unscheduled):
        task = state["task_by_id"].get(tid)
        if task is not None and task.team == team:
            reason = schedule.unscheduled[tid]
            causes[reason] = causes.get(reason, 0) + 1
    return {
        "team": team,
        "day": day,
        "shifts": shifts,
        "leaderboard": leaderboard,
        "causes": sorted(causes.items(), key=lambda kv: (-kv[1], kv[0])),
        "pareto": _cause_pareto(state, team),
        "progression": _progression_ctx(state, team),
    }


def _progression_ctx(state: dict, team: str) -> dict:
    """Streak + badges + level block for one team (GAMES §10).

    GG-1: read-only over the derived event log; GG-3: the streak shown
    pauses (never breaks) on excused-heavy shifts; GG-4: every badge chip
    carries its evidence + earning events; PSY-4: level/XP are the pure
    fold over credited completions. A failing service yields an honest
    error string, never fabricated progression.
    """
    prog_mod, perr = _svc("progression")
    out = {"streak": None, "badges": [], "level": None, "error": perr}
    if prog_mod is None:
        return out
    snap = state["snapshot"]
    events = state.get("game_events", [])
    streak, err = _call(prog_mod.compute_streak, snap, team)
    if err:
        out["error"] = err
        return out
    out["streak"] = _as_plain(streak)
    badges, err = _call(prog_mod.badges_for_team, events, team)
    if err:
        out["error"] = err
    else:
        out["badges"] = _as_plain(badges) or []
    level, err = _call(prog_mod.team_level, events, team)
    if err:
        out["error"] = err
    else:
        out["level"] = _as_plain(level)
    return out


def _cause_pareto(state: dict, team: str) -> dict:
    """Disruption cause pareto for one team (merged excusals by cause).

    Counts every merged excusal record (auto + manual, manual EXTENDS)
    grouped by cause, sorted by count descending then cause id; the
    excusable split is shown explicitly (GG-3: only excusable causes can
    leave a goal denominator — the rest the team owns)."""
    by_task, err = _team_cause_map(state, team)
    if err:
        return {"rows": [], "excused": 0, "owned": 0, "error": err}
    counts: dict[str, dict] = {}
    for records in by_task.values():
        for rec in records:
            row = counts.setdefault(
                rec["cause"], {"cause": rec["cause"], "excusable": rec["excusable"],
                               "count": 0, "manual": 0}
            )
            row["count"] += 1
            if rec["source"] == "manual":
                row["manual"] += 1
    rows = sorted(counts.values(), key=lambda r: (-r["count"], r["cause"]))
    return {
        "rows": rows,
        "excused": sum(r["count"] for r in rows if r["excusable"]),
        "owned": sum(r["count"] for r in rows if not r["excusable"]),
        "error": None,
    }


def _super_ctx(state: dict, sc: dict) -> dict:
    """Position Control data: per-team capacity pressure (group-filtered),
    escalations from unscheduled reasons, fleet station-progress strip.

    RS rule: pressure pools and escalations are filtered to the super's
    team group; the station strip is aircraft-level (labeled fleet-wide)."""
    fleet: Fleet = state["fleet"]
    schedule: Schedule = state["schedule"]
    teams = sorted(sc["teams"]) if sc["teams"] is not None else state["teams"]
    pressure = schedule.stats.get("capacity_pressure", {}) or {}
    pools = [
        p
        for p in pressure.get("pools", [])
        if p.get("team") in set(teams)
    ]
    escalations = []
    for team in teams:
        reasons: dict[str, int] = {}
        samples: list[str] = []
        for tid in sorted(schedule.unscheduled):
            task = state["task_by_id"].get(tid)
            if task is None or task.team != team:
                continue
            reason = schedule.unscheduled[tid]
            reasons[reason] = reasons.get(reason, 0) + 1
            if len(samples) < 3:
                samples.append(tid)
        blocked = sum(
            1 for t in fleet.tasks if t.team == team and t.state == "blocked"
        )
        if reasons or blocked:
            escalations.append(
                {
                    "team": team,
                    "reasons": sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])),
                    "total": sum(reasons.values()),
                    "blocked": blocked,
                    "samples": samples,
                }
            )
    escalations.sort(key=lambda e: (-e["total"], e["team"]))

    stats_ac = {a["aircraft"]: a for a in schedule.stats.get("aircraft", [])}
    per_ac_done: dict[int, int] = {}
    per_ac_total: dict[int, int] = {}
    for task in fleet.tasks:
        per_ac_total[task.aircraft] = per_ac_total.get(task.aircraft, 0) + 1
        if task.state == "done":
            per_ac_done[task.aircraft] = per_ac_done.get(task.aircraft, 0) + 1
    stations: dict[str, dict] = {}
    for ac in sorted(fleet.aircraft, key=lambda a: (a.station, a.aircraft)):
        row = stations.setdefault(
            ac.station, {"station": ac.station, "aircraft": 0, "done": 0,
                         "total": 0, "late": 0}
        )
        row["aircraft"] += 1
        row["done"] += per_ac_done.get(ac.aircraft, 0)
        row["total"] += per_ac_total.get(ac.aircraft, 0)
        if stats_ac.get(ac.aircraft, {}).get("lateness_days", 0) > 0:
            row["late"] += 1
    station_rows = []
    for name in sorted(stations):
        row = stations[name]
        row["pct_done"] = round(100.0 * row["done"] / row["total"], 1) if row["total"] else 0.0
        station_rows.append(row)
    return {
        "teams": teams,
        "pools": pools,
        "pressure_meta": {
            "source": pressure.get("source"),
            "method": pressure.get("method"),
            "penalty_usd_per_day": pressure.get("penalty_usd_per_day"),
        },
        "escalations": escalations,
        "stations": station_rows,
    }


def _director_ctx(state: dict, sc: dict) -> dict:
    """Analytics data: economics table (controllable vs unavoidable,
    placeholder-labeled per OR-5) + OTD rollup + honest what-if note."""
    schedule: Schedule = state["schedule"]
    econ = schedule.stats.get("economics", {}) or {}
    rows = sorted(
        econ.get("aircraft", []),
        key=lambda r: (-r.get("controllable_penalty_usd", 0),
                       -r.get("penalty_usd", 0), r.get("aircraft", 0)),
    )
    stats = schedule.stats
    n_aircraft = len(stats.get("aircraft", []))
    return {
        "econ": econ,
        "econ_rows": rows[:30],
        "econ_rows_total": len(rows),
        "otd": {
            "otd_count": stats.get("otd_count"),
            "n_aircraft": n_aircraft,
            "otd_pct": round(100.0 * stats.get("otd_count", 0) / n_aircraft, 1)
            if n_aircraft
            else 0.0,
            "fleet_lateness_days": stats.get("fleet_lateness_days"),
            "makespan_day": stats.get("makespan_day"),
        },
    }


def _vp_ctx(state: dict, sc: dict) -> dict:
    """Risk & Commitments data: committed-vs-projected board + change log
    (OR-4: commitments move on evidence, reasons rendered VERBATIM),
    late-aircraft board, $ exposure (placeholder banner, OR-5), and the
    data-trust panel (mock flag, snapshot age, validator status) — the
    honesty surface of the whole app."""
    fleet: Fleet = state["fleet"]
    schedule: Schedule = state["schedule"]
    snap = state["snapshot"]
    commitments = snap.get("commitments") if isinstance(snap, dict) else None
    commitments = commitments or {"committed": [], "changes": [], "run_id": None}
    econ = schedule.stats.get("economics", {}) or {}
    late = sorted(
        (r for r in econ.get("aircraft", []) if r.get("lateness_days", 0) > 0),
        key=lambda r: (-r.get("lateness_days", 0), r.get("aircraft", 0)),
    )
    stats_ac = {a["aircraft"]: a for a in schedule.stats.get("aircraft", [])}
    ac_names = {a.aircraft: a.name for a in fleet.aircraft}
    late_rows = []
    for r in late[:25]:
        extra = stats_ac.get(r["aircraft"], {})
        late_rows.append(
            {
                **r,
                "name": ac_names.get(r["aircraft"], f"AC-{r['aircraft']:04d}"),
                "completion_day": extra.get("completion_day"),
                "unscheduled_tasks": extra.get("unscheduled_tasks", 0),
            }
        )
    trust = {
        "mock_data": bool(fleet.meta.get("mock_data", False)),
        "seed": fleet.meta.get("seed"),
        "generated_at": fleet.meta.get("generated_at"),
        "data_source": state.get("data_source"),
        "snapshot_id": state.get("snapshot_id"),
        "built_at": state.get("built_at"),
        "build_count": state.get("build_count"),
        "validator_total": state.get("validator_total"),
        "validator_summary": state.get("validator_summary", {}),
        "scheduled": schedule.stats.get("scheduled"),
        "unscheduled": schedule.stats.get("unscheduled"),
        "wall_seconds": schedule.stats.get("wall_seconds"),
        "warnings": list(state.get("warnings", [])),
        "audit_entries": len(state.get("audit", [])),
        "actuals_overrides": len(state.get("actuals", {})),
        "manual_excusals": len(state.get("excusals", [])),
    }
    return {
        "econ": econ,
        "late_rows": late_rows,
        "late_total": len(late),
        "trust": trust,
        "commitments": commitments,
    }


# ---------------------------------------------------------------------------
# the factory
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    """Build the FF Flask app (contract §ff/web; gunicorn-callable).

    Boot NEVER raises out of the factory for data/engine problems: failures
    are recorded on the state and reported by /readyz (503) and the pages'
    not-ready panel — /healthz must stay alive for the platform (Tanzu)
    health check. Only a misconfigured SECRET_KEY (config §7 rule) raises,
    by design: fail fast rather than run with a guessable session key.
    """
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.get_secret_key()

    state: dict = {
        "warnings": [],
        "audit": [],
        "actuals": {},
        "replan_lock": threading.Lock(),
        "start_day": 0,
        "build_count": 0,
        "snapshot": None,
        "boot_error": None,
        "teams": [],
        "groups": {},
        "group_labels": {},
        "task_by_id": {},
        "mech_by_id": {},
        "actuals_path": resolve_path(os.environ.get("FF_ACTUALS", "") or ACTUALS_REL),
        "commitments": None,
        "excusals": [],
        "commitments_path": resolve_path(
            os.environ.get("FF_COMMITMENTS", "") or COMMITMENTS_REL
        ),
        "excusals_path": resolve_path(os.environ.get("FF_EXCUSALS", "") or EXCUSALS_REL),
        # GAMES progression (§10): the append-only derived event log (GG-1)
        # plus the previous-run summary the next derivation diffs against.
        "game_events": [],
        "game_summary": None,
        "earn_rates": None,
        "game_events_path": resolve_path(
            os.environ.get("FF_GAME_EVENTS", "") or GAME_EVENTS_REL
        ),
        # LLM layer (§11): the LB-7 audit log path + the configured
        # provider. LB-1/LB-5: the ONLY consumer is the read-only
        # GET /api/v1/explain/candidates route.
        "llm_audit_path": resolve_path(
            os.environ.get("FF_LLM_AUDIT", "") or LLM_AUDIT_REL
        ),
        "llm_provider": None,
        # Scorecard (fortnight increment): graded execution HISTORY is a
        # data feed (the digital-fortnight runner writes it today; a real
        # actuals pipeline writes the identical shape tomorrow). Loaded
        # once at boot from FF_SCORECARD; None -> the API 404s honestly.
        "scorecard": None,
        "scorecard_path": resolve_path(os.environ.get("FF_SCORECARD", "")),
    }
    state["llm_provider"] = _build_llm_provider(state)
    if os.environ.get("FF_SCORECARD", ""):
        try:
            with open(state["scorecard_path"], "r", encoding="utf-8") as f:
                card = json.load(f)
            if isinstance(card, dict) and "tables" in card:
                state["scorecard"] = card
            else:
                state["warnings"] = state.get("warnings", []) + [
                    f"FF_SCORECARD={state['scorecard_path']!r} is not a "
                    "scorecard (missing 'tables'); ignored"
                ]
        except (OSError, ValueError) as exc:
            state["warnings"] = state.get("warnings", []) + [
                f"failed to load FF_SCORECARD: {exc}"
            ]
    # Factory wall (owner directive): the raw record stream behind the
    # scorecard — needed for the team->division mapping. Same data-feed
    # posture as FF_SCORECARD: loaded once at boot, honest absence.
    state["scorecard_records"] = None
    if os.environ.get("FF_SCORECARD_RECORDS", ""):
        try:
            from ff.data.loader import load_json_gz as _load_gz

            blob = _load_gz(
                resolve_path(os.environ["FF_SCORECARD_RECORDS"])
            )
            recs = blob.get("records") if isinstance(blob, dict) else None
            if isinstance(recs, list):
                state["scorecard_records"] = recs
            else:
                state["warnings"] = state.get("warnings", []) + [
                    "FF_SCORECARD_RECORDS is not a {records: [...]} file; ignored"
                ]
        except (OSError, ValueError) as exc:
            state["warnings"] = state.get("warnings", []) + [
                f"failed to load FF_SCORECARD_RECORDS: {exc}"
            ]
    app.extensions["ff"] = state

    try:
        fleet, source = _load_fleet(state)
        state["fleet"] = fleet
        state["data_source"] = source
        state["mock_data"] = bool(fleet.meta.get("mock_data", False))
        _index_fleet(state)
        state["actuals"] = _read_actuals(state)
        applied = _apply_actuals(state)
        if applied:
            state["warnings"].append(
                f"applied {applied} actuals override(s) from {state['actuals_path']}"
            )
        # Manual excusal log + commitments state load at boot (contract
        # addendum); rebuild_state then runs the commitments update.
        state["excusals"] = _read_excusals(state)
        if state["excusals"]:
            state["warnings"].append(
                f"loaded {len(state['excusals'])} manual excusal(s) from "
                f"{state['excusals_path']}"
            )
        # GAMES progression: reload the append-only event log (GG-1 — the
        # log is derived state; a missing service is an honest warning).
        prog_mod, prog_err = _svc("progression")
        if prog_mod is not None:
            state["game_events"] = prog_mod.load_events(state["game_events_path"])
            if state["game_events"]:
                state["warnings"].append(
                    f"loaded {len(state['game_events'])} game event(s) from "
                    f"{state['game_events_path']}"
                )
        rebuild_state(state)
    except Exception as exc:
        state["boot_error"] = f"{type(exc).__name__}: {exc}"

    from ff.web.api import bp as api_bp

    app.register_blueprint(api_bp)

    # -- health -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        """Liveness: ALWAYS 200 while the process serves requests."""
        return jsonify({"status": "ok"}), 200

    @app.get("/readyz")
    def readyz():
        """Readiness: 200 only when the snapshot is built, else 503."""
        if state.get("snapshot") is not None:
            return (
                jsonify(
                    {
                        "status": "ready",
                        "snapshot_id": state.get("snapshot_id"),
                        "built_at": state.get("built_at"),
                    }
                ),
                200,
            )
        return (
            jsonify({"status": "not_ready", "error": state.get("boot_error")}),
            503,
        )

    # -- auth ---------------------------------------------------------------

    @app.get("/login")
    def login_page():
        """Login page; dropdowns are populated from the LOADED fleet so every
        offered persona/scope is real (no fictional scopes)."""
        sc = resolve_scope(state)
        if sc is not None:
            return redirect(ROLE_HOME[sc["role"]])
        ctx = _common_ctx(state, None, engine_required=False)
        ctx.update(
            roles=list(ROLES),
            role_labels=ROLE_LABELS,
            mechanics=sorted(state.get("mech_by_id", {})),
            teams=state.get("teams", []),
            groups=[
                {"id": gid, "label": state["group_labels"][gid]}
                for gid in sorted(state.get("groups", {}))
            ],
            login_error=None,
        )
        return render_template("login.html", **ctx)

    @app.post("/login")
    def login_submit():
        """Demo login: role + scope into the session (RS scope source of truth).

        Scope validation is server-side against the loaded fleet: a mechanic
        id must be on the roster, a team must exist, a group must exist;
        director/vp are forced to 'all'. Accepts form posts (redirect) and
        JSON (``{"ok": true, "redirect": ...}``).
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = request.form
        role = str(payload.get("role", "") or "").strip()
        scope = str(payload.get("scope", "") or "").strip()
        error = None
        if role not in ROLES:
            error = f"unknown role {role!r}"
        elif role == "mechanic" and scope not in state.get("mech_by_id", {}):
            error = "mechanic scope must be a roster mech_id"
        elif role in ("lead", "flm") and scope not in state.get("teams", []):
            error = "lead/flm scope must be an existing team"
        elif role == "super" and scope not in state.get("groups", {}):
            error = "super scope must be a team group id"
        if role in ("director", "vp"):
            scope = "all"
        if error:
            if request.is_json:
                return jsonify({"error": error, "code": "invalid_login"}), 400
            ctx = _common_ctx(state, None, engine_required=False)
            ctx.update(
                roles=list(ROLES),
                role_labels=ROLE_LABELS,
                mechanics=sorted(state.get("mech_by_id", {})),
                teams=state.get("teams", []),
                groups=[
                    {"id": gid, "label": state["group_labels"][gid]}
                    for gid in sorted(state.get("groups", {}))
                ],
                login_error=error,
            )
            return render_template("login.html", **ctx), 400
        session.clear()
        session["role"] = role
        session["scope"] = scope
        if request.is_json:
            return jsonify({"ok": True, "role": role, "scope": scope,
                            "redirect": ROLE_HOME[role]})
        return redirect(ROLE_HOME[role])

    @app.get("/logout")
    def logout():
        """Clear the demo session and return to the login page."""
        session.clear()
        return redirect("/login")

    @app.get("/")
    def index():
        """Role landing redirect: logged-in role's view, else login."""
        sc = resolve_scope(state)
        return redirect(ROLE_HOME[sc["role"]] if sc else "/login")

    # -- role views ----------------------------------------------------------

    def _gate(role_name: str):
        """View gate: require login; require the view's own role (RS rule —
        each persona lands only on its own surface); require nothing else."""
        sc = resolve_scope(state)
        if sc is None:
            return None, redirect("/login")
        if sc["role"] != role_name:
            return None, redirect(ROLE_HOME[sc["role"]])
        return sc, None

    def _day_arg() -> int:
        raw = request.args.get("day", "")
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return -1  # sentinel: view picks its deterministic default

    @app.get("/mechanic")
    def view_mechanic():
        sc, resp = _gate("mechanic")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_mechanic_ctx(state, sc))
        return render_template("mechanic.html", **ctx)

    @app.get("/lead")
    def view_lead():
        sc, resp = _gate("lead")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_lead_ctx(state, sc, _day_arg()))
        return render_template("lead.html", **ctx)

    @app.get("/flm")
    def view_flm():
        sc, resp = _gate("flm")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_flm_ctx(state, sc, _day_arg()))
        return render_template("flm.html", **ctx)

    @app.get("/super")
    def view_super():
        sc, resp = _gate("super")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_super_ctx(state, sc))
        return render_template("super.html", **ctx)

    @app.get("/director")
    def view_director():
        sc, resp = _gate("director")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_director_ctx(state, sc))
        return render_template("director.html", **ctx)

    @app.get("/vp")
    def view_vp():
        sc, resp = _gate("vp")
        if resp is not None:
            return resp
        ctx = _common_ctx(state, sc)
        if ctx["ready"]:
            ctx.update(_vp_ctx(state, sc))
        return render_template("vp.html", **ctx)

    @app.get("/wall")
    def view_wall():
        """Factory-wall kiosk page (owner directive: large-monitor
        leaderboards/progression/feed). Same access rule as the API:
        config.WALL_PUBLIC allows unauthenticated kiosks on a trusted
        internal network (aggregates only, §G8); otherwise any logged-in
        role. The page is self-refreshing and fetches /api/v1/wall."""
        if not config.WALL_PUBLIC and resolve_scope(state) is None:
            return redirect("/login")
        return render_template(
            "wall.html",
            mock_data=bool(state.get("mock_data", True)),
            wall_public=bool(config.WALL_PUBLIC),
        )

    # -- JSON errors for API paths -------------------------------------------

    @app.errorhandler(404)
    def not_found(err):
        """/api/* paths get the JSON error envelope; pages get plain text."""
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found", "code": "not_found"}), 404
        return "Not found", 404

    @app.errorhandler(405)
    def method_not_allowed(err):
        if request.path.startswith("/api/"):
            return (
                jsonify({"error": "method not allowed", "code": "method_not_allowed"}),
                405,
            )
        return "Method not allowed", 405

    return app
