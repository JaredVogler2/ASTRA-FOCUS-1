# FF_app — Build Log

Running log of build phases, decisions, and deviations from
`ARCHITECTURE.md`. Newest entry last. Every agent that ships a phase
appends an entry using the template below.

## Entry template

```
## YYYY-MM-DD — <phase name> (<agent/scope>)
- Files: <files written/changed>
- Contract sections implemented: <ARCHITECTURE.md sections>
- Decisions: <notable choices within the contract's latitude>
- Deviations: <NONE, or exact deviation + why>
- Self-check: <commands run + results>
- Open items for integrator: <anything the next phase must know>
```

---

## 2026-07-10 — skeleton (config / domain / CLI / packaging)
- Files: `config.py`, `ff/__init__.py`, `ff/domain.py`,
  `ff/{data,engine,services,web}/__init__.py`, `tests/__init__.py`,
  `run.py`, `requirements.txt`, `requirements-dev.txt`, `Dockerfile`,
  `.dockerignore`, `manifest.yml`, `README.md`, `data/.gitignore`,
  `BUILD_LOG.md`.
- Contract sections implemented: Layout, Time model, `config.py` §1–§8,
  `ff/domain.py` (exact dataclasses + serde + slot/calendar/OR-3 helpers),
  `run.py` subcommands, Tanzu packaging, requirements.
- Decisions:
  - Env override helper `config._env` parses by the type of the in-code
    default (bool/int/float/dict-as-JSON/str); dict keys/values coerced to
    the default's key/value types so `FF_SHIFT_EFFECTIVE` works.
  - `config.get_secret_key()` raises unless `FF_SECRET_KEY` set or
    `FF_ENV=dev` (dev fallback `'ff-dev-key'`); evaluated at
    `create_app()` time, not import time, so CLI/tests never need a key.
  - `config.resolve_port(cli_port)` encodes `PORT > FF_PORT > CLI > 8080`.
  - `ff.domain.shift_eligible` implements OR-3: S1/S2 need a working day;
    S3 is eligible on a working day, or on a non-working day `d` iff
    `d+1` is working (Sunday yes, Saturday no); flag-gated by
    `WEEK_STARTS_SUNDAY_NIGHT`.
  - `DAY_WORK_MINUTES = sum(SHIFT_EFFECTIVE.values())` (= 1290 at defaults).
  - Serde: generic `to_dict()` via `dataclasses.asdict`; per-class
    `from_dict` ignores unknown keys; `Fleet`/`Schedule` rebuild nested
    dataclasses, with sorted key iteration for determinism.
  - `run.py gates` runs all four steps as subprocesses (`sys.executable`,
    cwd = app root) and exits with the first non-zero code.
- Deviations: NONE.
- Self-check: `python3 -m py_compile config.py ff/domain.py run.py` OK;
  `python3 run.py --help` and per-subcommand `--help` OK; domain smoke
  test (round-trip equality, slot_index, OR-3 Sunday/Saturday cases) OK.
- Open items for integrator: `ff/data/loader.py` must expose
  `save_json_gz(obj_dict, path)` / `load_json_gz(path)`; `ff/web/app.py`
  must call `config.get_secret_key()` and `config.resolve_port()`;
  `run.py gates` expects `generate_fleet`, `compute_cpm`,
  `build_schedule`, `validate` with contract signatures.

## 2026-07-10 — integration (full-app gate pass)
- Files changed: `run.py` (gates mini-fixture step now passes
  `--teams 4 --mechanics 30`), `ff/engine/scheduler.py`
  (`build_schedule` now fills `stats["economics"]` and
  `stats["capacity_pressure"]` itself), `BUILD_LOG.md` (this entry).
  `data/mini/fleet.json.gz` regenerated back to the committed variant.
- Contract sections implemented/closed: `Schedule.stats` completeness
  (economics from `ff/engine/economics.py`, capacity_pressure from
  `ff/services/capacity.py` — previously `{}` placeholders on the
  `run-schedule` path; the web app had wired them only at boot/replan);
  `run.py gates` vs committed mini fixture agreement.
- Decisions:
  - Wired economics/capacity at the END of `build_schedule` (after the
    Schedule object exists) with a lazy `ff.services.capacity` import —
    no import cycle (capacity depends only on config+domain), engine→
    services edge confined to one call, determinism preserved (both are
    pure functions of fleet/schedule/start_day). `ff/web/app.py`'s own
    wiring at boot/replan still runs and overwrites with equivalent
    values — harmless.
  - Gates mini-fixture step pins `--teams 4 --mechanics 30` (the
    committed `data/mini/fleet.json.gz` spec, sha256 `be31ce54…`);
    without it the CLI defaults (20 teams / 600 mechanics) silently
    overwrote the fixture with a different variant. Verified the gates
    run now reproduces the committed fixture byte-identically.
- Deviations: NONE.
- Self-check / MEASURED ACCEPTANCE NUMBERS (Python 3.11.15, flask 3.1.3,
  pytest 9.1.1, gunicorn importable; no pip install needed):
  - `py_compile` on all 30 non-cache `.py` files: clean.
  - `generate-data --aircraft 3 --tasks-per-aircraft 40 --teams 4
    --mechanics 30` → 3 aircraft / 120 tasks / 30 mechanics, 0.09 s,
    sha256 `be31ce54f7a3e7ea…` (byte-identical to committed fixture).
  - `generate-data --aircraft 50` → 50 aircraft / 18,000 tasks /
    600 mechanics, seed 20260710, 0.98 s wall, 304 KB.
  - `run-schedule` on fleet50: **18,000/18,000 scheduled, 0
    unscheduled**, engine wall 1.1 s (total 2.3 s incl. I/O — target
    <60 s), fleet_lateness_days 1759, otd_count 11/50, makespan_day 98;
    economics total/unavoidable/controllable = $175.9M / $0 / $175.9M,
    late_count 39, source "config-defaults" (placeholder, OR-5);
    capacity top pool T06/S1 wait 1087 d ($108.7M-days), total wait
    2843 d. Two runs byte-identical except `wall_seconds`.
  - `validate data/schedule50.json.gz`: **0 violations** across V1–V9
    (V1..V9 all 0), 0.86 s. Validator untouched.
  - `pytest -q`: **67 passed** in 0.36 s.
  - `run.py gates`: ALL PASS (generate mini → schedule → validate 0
    violations → 67 passed), ~1 s total; mini fixture sha unchanged.
  - Serve smoke (FF_ENV=dev, :8091): `/healthz` ok; `/readyz` ready with
    snapshot_id; JSON `POST /login` (lead/T01) sets session;
    `GET /api/v1/state` scope-filtered (916 T01 tasks); `GET
    /api/v1/candidates?limit=5` returns ranked candidates with decomposed
    components + explanations; `GET /api/v1/points/leaderboard` ranks by
    attainment/efficiency (never raw points, GG-2); lead hitting
    `GET /api/v1/economics` → 403 (scope enforcement); `POST
    /api/v1/actuals {task_id, state:"done"}` → ok, stale:true, persisted
    to `data/actuals.json`, audit_seq 1 (OR-6 single write-path);
    `POST /api/v1/replan` → scheduled 17,999/18,000 (done task excluded),
    new snapshot_id, validator_total 0, wall 1.25 s; `GET
    /api/v1/tasks/<id>` after replan: state done, assignment None,
    feasibility DONE — the done task left the schedule. Smoke
    `data/actuals.json` removed afterwards for a clean tree.
- Open items for integrator: NONE — all gates green.

## 2026-07-10 — Maturity-mode fleet (owner feedback: ">40,000 tasks; 0-6,000+ per aircraft at takt")
- Generator: pulsed-line maturity ladder (20 LATE_TO_DELIVERY incl. ships with exactly 0 and 1
  open tasks, 20 POST_FAL 120-900, 10 IN_FACTORY up to 6,500). fleet50 = 50 aircraft /
  **55,529 open tasks** / 680 mechanics, generated 1.2s.
- Scheduler: ascending-slack dispatch (deadline-aware, REBUILD §6.2 seed rule); workload-scaled
  horizon (measured 4x fragmentation headroom — 3x left aircraft 49's 2,082-task tail through a
  thin 3-holder AVION pool past the horizon).
- Deadline calibration: class formulas set from the measured completion curve (frag factor 2.4);
  quota-aware crew clamp in the solvability repair.
- MEASURED (synthetic mock data): 55,529/55,529 scheduled, 0 unscheduled, wall 3.5s; validate
  V1-V9 = 0 violations; OTD 19/50 (MAX fleet50 reference: 23/50); fleet lateness 492 days;
  economics $49.2M controllable / $0 unavoidable (placeholder rates); makespan day 429;
  pytest 67 passed.

## 2026-07-10 — Benchmark suite (bench scope: ff/bench + benchmarks/ + tools/bench.py)
- Files: `ff/bench/__init__.py`, `ff/bench/mutations.py`,
  `benchmarks/{baseline_fleet50,mechanic_absence,skill_shortage,late_parts,rework_wave,pulse_slip}/`
  (each: manifest.json, objective.json, expected_validation.json, README.md),
  `tools/bench.py`, `tests/test_bench.py`,
  `outputs/bench_results.jsonl` + `outputs/bench_report.md` (measured).
- Contract sections implemented: B1 governing prompt (FOCU5/06_benchmarks/01)
  adapted to FF_app: scenario corpus with generator RECIPES (never dataset
  copies), PROPERTY-based expected_validation (never exact placements),
  runner that REUSES `ff.engine.validator` and Schedule.stats
  economics/capacity, JSONL results + trend vs previous same-id row,
  non-zero exit on any FAIL. Pure stdlib; deterministic (all seeds from
  manifests; runner itself rng-free).
- Decisions:
  - Mutations are PURE (deep-copy via Fleet serde) + provenance-stamped in
    `meta["bench_overrides"]`; runner cross-checks the stamp against the
    manifest (B1 gotcha: "variant passes without doing anything").
  - `rework_wave` follows the generator's own rework rule (parent-gated
    same-aircraft LEAF inheriting team/skill/crew) — DAG acyclicity and
    C12 hold by construction; solvability preserved (parent crew already
    pool-clamped).
  - `scheduled_rate` is over LIVE tasks (total - done). OTD on
    heavy-unscheduled scenarios is optimistic (completion ignores
    unplaceable work) — documented in scenario READMEs, surfaced via
    per-aircraft `unscheduled_tasks`.
  - Bounds policy: zero_violations + all_scheduled_or_reasoned absolute;
    otd_range = measured ±3 aircraft; fleet_lateness_range = measured ±25%;
    rate/count bounds with modest headroom. All centered on measured values.
- Deviations: NONE from the task spec. NOTE: baseline fleet lateness
  measured 574 days vs 492 in the 2026-07-10 maturity-mode entry — the
  engine tree has moved since that entry (other agents mid-edit); the
  benchmark records today's measured truth and trends from here.
- Self-check / MEASURED (fleet50 maturity mode, seed 20260710, SYNTHETIC
  mock data, placeholder economics rates, no optimality claims):
  - `py_compile` clean on all new files; `pytest -q tests/test_bench.py`
    = 30 passed (full suite NOT run — other agents mid-edit).
  - Full suite run twice, ALL 6 PASS both times (~27 s per invocation,
    fleet gen shared per recipe): baseline_fleet50 sched 55529/55529,
    OTD 19/50, lateness 574 d, makespan 429, $57.4M, wall 2.3 s, hash
    a4894faa706a; late_parts (2776 blocked, ETA d10) OTD 19, lateness 664;
    mechanic_absence (T10 48→44) sched rate 0.8875 (6245 reason-coded),
    OTD 22, lateness 536; pulse_slip OTD 17, lateness 619; rework_wave
    (55729 tasks) OTD 19, lateness 570; skill_shortage (84/168 AVION
    holders stripped) sched rate 0.5119 (27102 reason-coded), OTD 31.
    Violations 0 (V1–V9) on every scenario. Second run: every
    reproducibility hash identical, all trend deltas 0 except wall_s.
- Open items for integrator: `outputs/bench_results.jsonl` is append-only
  history (trend source); rerun `python3 tools/bench.py` after engine
  changes and read the trend blocks before touching any expected bound.

## 2026-07-10 — INCREMENT 2: commitments + disruption/excusal (build agent, post-PR#3 addendum)
- Files: `config.py` (§9), `ff/services/commitments.py` (NEW),
  `ff/services/disruption.py` (NEW), `ff/services/points.py`
  (shift_report GG-3 wiring), `ff/web/app.py` (boot/replan commitments
  wiring, excusals load/persist, lead cause-chips + capture ctx, flm cause
  pareto, vp commitments board), `ff/web/api.py` (POST+GET /api/v1/excusals,
  replan response `commitments` block), `ff/web/templates/{vp,lead,flm}.html`,
  `ff/web/static/ff.js` (`.exc-apply` capture handler, plain fetch),
  `ff/web/static/ff.css` (`.exc-ctl`), `data/.gitignore`
  (+commitments.json/excusals.json as ephemeral runtime state),
  `tests/test_commitments.py` (NEW, 9), `tests/test_disruption.py` (NEW, 10),
  `tests/test_points.py` (+3 GG-3), `tests/test_api.py` (+6 excusal/commit,
  fixture now redirects FF_COMMITMENTS/FF_EXCUSALS), `BUILD_LOG.md`.
- Contract sections implemented: the ENTIRE INCREMENT 2 addendum —
  config §9 (`COMMIT_HYSTERESIS_DAYS=2, COMMIT_PERSISTENCE_RUNS=2,
  COMMIT_WORSEN_IMMEDIATE_DAYS=7`, env-overridable); `update_commitments`
  4 rules (initial / jitter-hold / bad-news-fast / sustained ±1-day streak
  with reset-on-move) + atomic `data/commitments.json` + snapshot
  `commitments` block, run inside boot AND every /replan (single-flight
  lock held); disruption CAUSES (8 verbatim) / EXCUSABLE (exact 5 `+`) /
  deterministic `attribute` (all 6 auto rules) / `merged_excusals`
  (extend-never-replace, task+cause dupes collapse to auto);
  POST+GET /api/v1/excusals (lead+ role gate, own-team scope 403, unknown
  task/cause 400, notes REQUIRED for SAME_TEAM_PREDECESSOR +
  DURATION_OVERRUN, atomic append `data/excusals.json` with
  source/entered_by/ts); points.shift_report GG-3 (excusable-cause planned
  tasks leave the goal denominator; `excused_points` + `excusals` fields —
  visible, never silent; non-excusable stays); the three view updates
  (house style, no CDN, plain fetch).
- Decisions (inside the contract's latitude):
  - `run_id` = the run's snapshot_id; slip reasons concatenate evidence
    KEY NAMES verbatim (`rework_inserted=2 + blocked_parts=1`), fallback
    "global replan shift"; improvements = "schedule improvement"; sustained
    commits append " (sustained N replans)".
  - `build_evidence` derives per-aircraft counts deterministically:
    rework_inserted = not-started rework, execution_shortfall =
    in_progress, blocked_parts = blocked, capacity = unscheduled
    no_crew_within_horizon.
  - Unplaced tasks' excusal records use slot (today, shift 0 = unslotted)
    so they can never collide with a real planned slice; a DONE planned
    task is never excused (finished work needs no excuse) — keeps
    attainment ≤ 1 honest.
  - Manual excusals ride the snapshot by reference
    (`snap["manual_excusals"]` = the state list), so a lead capture
    affects GG-3 immediately, without waiting for a replan.
  - Stray `data/commitments.json` (mini-fleet runtime state that leaked
    into the mid-flight WIP snapshot before the test fixture redirected
    FF_COMMITMENTS) was untracked + deleted; data/.gitignore now covers
    commitments.json/excusals.json like actuals.json.
- Deviations: NONE.
- Self-check / MEASURED (Python 3.11, synthetic mock data):
  - `python3 -m py_compile` clean on all 10 touched .py files.
  - `python3 -m pytest -q`: **138 passed** in 0.82 s (was 97 pre-increment
    on this tree; +28 from this increment, +13 from the parallel
    bench/sim track, all green together).
  - `python3 run.py gates`: **ALL PASS** (mini fixture byte-stable,
    validate V1–V9 = 0 violations, 138 passed).
  - Serve smoke (FF_ENV=dev, :8095, fleet50 55,529 tasks): /readyz ready;
    JSON login lead/T01; POST /actuals → task 0006-T00054 blocked (OR-6,
    audit_seq 1); POST /api/v1/excusals {LATE_PART, notes} → ok,
    excusable:true, entered_by "lead:T01", persisted data/excusals.json;
    GET /api/v1/excusals?team=T01&day=0&shift=1 → 9 merged records incl.
    the manual capture (0.27 s); lead/T01 asking team=T02 → 403
    forbidden_scope; SAME_TEAM_PREDECESSOR w/o notes → 400 notes_required;
    POST /replan → ok in 4.9 s (engine wall 2.74 s), 55,529/55,529
    scheduled, response carries `commitments` {committed: 50 rows,
    changes: 50 "initial commitment" rows, run_id == snapshot_id};
    /lead 200 (LATE_PART ✓ chip + capture control), /vp 200 (committed
    board + change log w/ verbatim reasons), /flm 200 (cause pareto:
    LATE_PART / SAME_TEAM_PREDECESSOR / REWORK_INJECTION). Server killed;
    smoke-created data/{actuals,excusals,commitments}.json deleted.
- Open items for integrator: NONE for this increment. Note: the tree is
  shared with a parallel bench/sim track (tests/test_points_gate.py,
  tests/test_sim_week.py, outputs/ are theirs); full suite green with both.

## 2026-07-10 — Point-engine recalibration: effort-weighted scoring (correlation-gate fix)
- Files: `config.py` (§5), `ff/services/points.py`,
  `ff/services/candidates.py` (docstrings only — code already routes
  through the shared module), `ARCHITECTURE.md` (§5 line + services
  scoring description marked recalibrated), `tests/test_points.py`
  (3 tests' absolute expectations updated, invariants kept/strengthened),
  `outputs/points_validation.json` (regenerated by the gate),
  `BUILD_LOG.md`.
- Governing doctrine: GAMES canon GG-2 — "Value is priority_score-weighted
  (mechanic-minute x criticality), never raw task count"
  (`MAX_FOCUS_1/GAMES/01_scoring_engine_v1.md` + EXAMPLES). The probe
  policies and `tools/points_gate.py` were NOT touched (doctrine: fix the
  scoring, never the gate).
- BEFORE (measured FAIL, fleet50 55,529 tasks, synthetic mock data,
  seed 7, 9 probe shifts): chaser (3,946 short completions, fleet
  lateness WORSENED 86 days) out-pointed flow (1,855 completions, fleet
  lateness improved 30 days) 164,274 to 152,872. Diagnosis: per-snapshot
  raw min-max normalizers flattened factor points at 55k-task scale
  (single outliers — e.g. multi-thousand-task downstream subtrees — pushed
  typical tasks' normalized factors to ~0), so the flat BASE(<=15)
  dominated and 30-minute stubs paid almost the same as keystones.
- Recalibration (one implementation, BOTH profiles, in the SHARED
  `score_components`):
  - `score = effort_term x value_multiplier`; effort =
    `max(1, round(duration_minutes*mechanics_required /
    EFFORT_CREW_MINUTES_PER_POINT))` (new §5 constant, default 5 —
    generator range 30..1440 crew-min maps to 6..288 points, the workable
    10-300 band); each factor contribution =
    `round(effort * (W/100) * normalized_factor)` (§5 weights reinterpreted
    as percent-of-effort, defaults UNCHANGED: 60/25/45/30/20/30);
    P_OOS = -40% of effort (effort-scaled dock); BASE_CAP removed
    (superseded by the effort term). Max multiplier 3.1x.
  - Normalizers: robust-percentile scaling `clamp((raw-p5)/(p95-p5),0,1)`
    (new §5 NORM_PCTL_LO=5.0 / NORM_PCTL_HI=95.0) replaces raw min-max —
    outlier-immune, so the factor body spreads across [0,1] and whales
    clamp at 1. Frozen-per-snapshot rule unchanged.
  - GG-4 kept: ScoreBreakdown fully decomposed — effort is a NAMED
    component `{raw: crew-minutes, weight: divisor, points}`, factors keep
    `{raw, weight, points}`; explanation template gains
    `+N effort (Dm x M mech)`.
- AFTER (measured PASS, same fleet50 fixture/seed, synthetic mock data;
  `python3 tools/points_gate.py --data data/fleet50.json.gz`): flow
  STRICTLY out-earns chaser at ALL THREE probe depths —
  - 3 shifts: flow 83,239 (296 completions) vs chaser 73,269 (2,066);
  - 6 shifts: flow 160,268 (657) vs chaser 146,019 (3,097);
  - 9 shifts: flow 236,539 (1,084 completions; fleet lateness 574 -> 575,
    ~flat) vs chaser 218,524 (3,946; fleet lateness 574 -> 660, WORSENED
    86 days), effort budgets ~equal (762,393 vs 755,654 crew-min).
    Note: flow's fleet outcome changed vs the old scoring (+30 recovered
    -> -1) because effort-weighting shifts its picks toward big
    high-multiplier jobs; it still beats chaser's -86 by 85 days and the
    gate criterion (points ordering) is what GG-2 governs. Verdict PASS; ordering
    invariant 248 vs trivial 6 (>10x, margin 4.1x); correlation (a)
    pearson +0.0673 (n=180) — still positive, informational/non-gating as
    designed, reported honestly (threshold calibration needs real data).
  - Canonical artifact `outputs/points_validation.json` = the default run
    (rounds 9, probe 9); 3-/6-shift probe artifacts measured with
    --rounds 1 to scratch (probe results are independent of rounds).
- Tests: `test_points.py` updated where absolute point values changed
  (factor points now effort-weighted; `base` field -> `effort`; OOS dock
  effort-scaled). The ordering-invariant test now ALSO pins the
  effort-weighted contribution formula (strengthened);
  `test_leaderboard_not_raw_points` untouched (still passes). Gate tests
  untouched.
- Deviations: BASE_CAP removed from config §5 (ARCHITECTURE.md updated in
  place with a recalibration marker) — the effort term IS the base value
  now; a cap would reintroduce the flat-scoring failure.
- Self-check / MEASURED (Python 3.11, synthetic mock data): `py_compile`
  clean on all 4 touched .py files; `python3 -m pytest -q` = **138
  passed** (full suite, 0.77 s); `python3 run.py gates` = ALL PASS;
  canonical gate run wall 69 s.
- Open items for integrator: correlation check (a) remains positive at
  mock scale (+0.0673) — informational by design; revisit the threshold
  once real (non-mock) execution data exists. `outputs/sim_week_report.md`
  (parallel track) still describes the pre-recalibration diagnosis in its
  recommendation note; superseded by this entry.

## 2026-07-10 — INCREMENT 3: GAMES progression layer (events/streaks/badges/recap/levels)
- Files: `config.py` (§10 GAMES PROGRESSION), `ff/services/progression.py`
  (NEW — event stream, slot aggregate, streaks, badge engine + earn-rate
  monitor, team XP/levels, recap cards, jsonl persistence, `advance`
  pipeline), `ff/services/points.py` (leaderboard upgrade: difficulty
  column + tie-break, PSY-5 needs-support framing w/ excusal context —
  ranking inputs unchanged, GG-2 re-asserted), `ff/web/app.py`
  (boot/replan `advance` wiring inside the existing pipeline, event-log
  load/persist via `FF_GAME_EVENTS`, `_progression_ctx` [flm],
  `_my_contribution` [mechanic, §G8 self-scope], `_recap_cards` [lead]),
  `ff/web/api.py` (GET /progression/team/<team>, GET /recap,
  GET /progression/events — ALL read-only, GG-1), templates
  `flm.html`/`mechanic.html`/`lead.html` (house style, no CDN, existing
  sections untouched), `data/.gitignore` (+game_events.jsonl),
  `tests/test_progression.py` (NEW, 20), `tests/test_points.py`
  (leaderboard key-set assertion updated for the new row fields; raw-points
  prohibition strengthened), `tests/test_api.py` (fixture redirects
  FF_GAME_EVENTS), `ARCHITECTURE.md` (INCREMENT 3 addendum), `BUILD_LOG.md`.
- Governing canon: FOCU5 X2/X3 (`04_games/02_progression_levels_seasons.md`,
  `03_leaderboards_streaks_events.md`) under X0 PSY law; GG-1..8 enforced
  and cited in docstrings throughout.
- Decisions (inside the mission's latitude):
  - Event-type ids use the MISSION's hyphenated vocabulary
    (`completion-credited`, `keystone-cleared`, `recovery-moment`,
    `goal-crossed`, `streak-extended`, `badge-earned`, `unlock`) — X3's
    underscore spellings noted as the upstream equivalent.
  - `derive_events(prev_summary=None, snap)` is the BASELINE: first sight
    of a stream emits NO events (events are diffs, X3-1); boot therefore
    derives nothing and every event traces to a real state change.
  - Unlock attribution is LAST-GATE: a successor counts as freed by task T
    only when T was its sole predecessor unfinished in the previous
    summary — simultaneous multi-gate completions free nobody singly, so
    keystone counts can never be double-credited.
  - `recovery-moment` reads ONLY the snapshot `commitments.changes` rows of
    the CURRENT run with `delta_days < 0` (PSY-8; hysteresis already
    debounces improvements — sustained-2-replans upstream).
  - `slot_stats` is a one-pass memoized aggregate that MIRRORS
    `points.shift_report`'s GG-3 goal math; drift is pinned by
    `test_slot_stats_matches_shift_report` (equality per slot).
  - Badge dedupe is by `badge_key` (badge+scope), independent of
    snapshot-scoped event ids, so re-deriving on later snapshots never
    re-awards; a badge with NO nameable earning event is NOT awarded
    (GG-4 hard rule — e.g. Clean Sweep over pre-baseline completions).
  - Leaderboard difficulty adjustment = FINAL tie-breaker (attainment,
    efficiency, then difficulty): equal execution favors the harder plan;
    primary ordering — and the never-raw-points law — unchanged.
  - `data/game_events.jsonl` is rewritten whole via the house atomic
    pattern (tmp+fsync+replace) but the LOG is append-only: rows are never
    mutated/removed and `append_events` (idempotent on event_id) is the
    only writer into the list.
- Deviations: NONE.
- Self-check / MEASURED (Python 3.11, ALL figures synthetic fleet50 mock
  data — 55,529 open tasks, 50 aircraft, 680 mechanics, seed 20260710;
  placeholder §10 thresholds labeled config-defaults):
  - `python3 -m py_compile` clean on all 8 touched/new .py files.
  - `python3 -m pytest -q`: **158 passed** in 0.92 s (was 138; +20 from
    tests/test_progression.py).
  - `python3 run.py gates`: **ALL PASS** (mini fixture byte-stable,
    validate V1–V9 = 0 violations, 158 passed).
  - Fleet50 in-process measurement: boot (engine+validate+commitments+
    progression baseline) 6.48 s; replan after 30 root completions 6.49 s;
    fresh `slot_stats` pass 1.42 s over 9,665 (team,day,shift) slots;
    event derivation from the 30 completions: 30 completion-credited,
    12 unlock, 1 keystone-cleared, 15 badge-earned (badge storm at mock
    scale is EXPECTED at first replan — recovery-heavy maturity fleet; the
    §10 earn-rate monitor is the tripwire, budgets are placeholders);
    `points.leaderboard` 7.47 s (20 rows; all 20 needs_support at day 0 —
    honest: almost nothing done on day 0 of a mock fleet); `build_recap`
    0.095 s.
  - Serve smoke (test client, fleet50): login lead; 5 actuals→replan 200;
    GET /progression/team/<team> 200 (streak 0, level 2, xp 455 [5
    credited completions], 4 badges w/ earning_event_ids); GET /recap 200
    generated_by "deterministic" (LB-8); GET /progression/events
    read_only:true; POST /progression/events 405 (GG-1); other-team 403;
    /lead 200 (recap cards + LB-8 note), /flm 200 (progression strip,
    Difficulty column, needs-support framing), /mechanic 200 ("My
    contribution" collapsed panel + visible-only-to-you privacy note,
    §G8); `game_events.jsonl` persisted (12 lines, every row carries
    event_id/evidence/mock_data). Smoke state redirected to scratch —
    repo `data/` untouched (verified: only .gitignore + fixtures).
- Open items for integrator: badge earn-rate budgets (§10) are mock-scale
  placeholders — first-replan badge counts on fleet50 run hot by design;
  tune budgets against real cadence, never delete the alert. The events
  feed grows unbounded (append-only by law) — rotation policy is a future
  P2-style decision, recorded not blocking.

## 2026-07-10 — INCREMENT 4: constrained LLM layer (ff/llm/ — provider/prompts/validators/API)
- Files: `config.py` (§11 LLM), `ff/llm/__init__.py` (NEW),
  `ff/llm/provider.py` (NEW — LLMProvider protocol, typed exception
  taxonomy, Mock/Recorded/BCAI providers, shared L1-6 structured loop),
  `ff/llm/prompts.py` (NEW — versioned template assets, LB-6 sanitizer,
  deterministic context builders), `ff/llm/validators.py` (NEW — 4-stage
  post-response pipeline + LB-7 audit JSONL),
  `ff/llm/cassettes/explain_candidates@1.0.0.json` (NEW committed replay
  fixture), `ff/web/api.py` (`_scoped_candidates` shared helper +
  GET /api/v1/explain/candidates + `_attach_llm_narrative`),
  `ff/web/app.py` (`_build_llm_provider` factory wiring, llm_audit_path
  state, FF_LLM_PROVIDER/FF_LLM_AUDIT env), `data/.gitignore`
  (+llm_audit.jsonl), `tests/test_llm.py` (NEW, 51),
  `ARCHITECTURE.md` (INCREMENT 4 addendum), `BUILD_LOG.md`.
- Governing canon: FOCU5 Wave-5 `02_llm/01_bcai_provider.md` (L1),
  `02_prompt_template_assets.md` (L2), `04_grounding_validators_and_
  injection_defense.md` (L4), adapted to FF_app's stdlib+flask contract;
  LB-1..LB-10 enforced and cited in docstrings. WATTS `bcai_client.py`
  transport adopted with its GROUNDING §7 BANNED patterns removed
  (TLS-verification-off + urllib3 warning suppression, hardcoded
  endpoint/model/use_case_id/conversation_source/info_types, in-band
  "ERROR:" strings returned as content).
- Decisions (inside the mission's latitude):
  - Mission text said "config section 10" for the LLM knobs (the canon's
    `Background` §10 knob list); in THIS repo §10 is GAMES PROGRESSION,
    so the block landed as **config §11 LLM** — all knobs env-overridable
    (FF_LLM_*), the -test-host endpoint carries a loud comment, and the
    default use_case_id is "focu5-use-case.unprovisioned" (owner to
    provision; the WATTS id is never reused, per L1 open question).
  - Pure-stdlib transport: `urllib.request` +
    `ssl.create_default_context()` (optional LLM_CA_BUNDLE) replaces
    requests — TLS verification is structurally ON; the tripwire test
    greps ff/, config.py and run.py for the banned bypass tokens
    (verification-off, warning-suppression, unverified-context,
    CERT-off, hostname-check-off variants) so the pattern cannot return.
  - Credential law (L1-4/LB-9): BCAIChatGPTProvider takes a per-call
    `pat_supplier` callable; the app factory wires it to
    `session["bcai_token"]` (server session only). Empty PAT = typed
    LLMAuthError with ZERO transport attempts (measured in test). The
    PAT appears in the Authorization header only — the golden test
    asserts it is absent from the request body, and cassettes are
    header-free by construction.
  - MockLLMProvider is GROUNDED by construction: it parses the rendered
    DATA block back and narrates only supplied ids/numbers, so the dev
    default passes the real LB-3/LB-4 validators instead of bypassing
    them — the validation pipeline is exercised on every dev request.
  - Two retry budgets kept separate (L1 gotcha): transport retries
    (timeout/connection ONLY, exponential backoff, never auth) vs the
    schema parse-validate-reprompt loop (LLM_SCHEMA_RETRIES=2 ->
    LLMSchemaError). One shared `_structured_completion` implementation
    serves mock and live paths so semantics cannot drift.
  - Numeric grounding (LB-4): metric set = every numeric leaf of the
    supplied context; ONE allowed derived form — a [0,1.5] fraction may
    be narrated as its percent (attainment 0.75 <-> "75%");
    $-K/M/B/comma normalization; task-id tokens stripped before number
    extraction; undecidable claims REJECT (conservative rule, L4-2
    stage 5 verbatim). Sanitizer neutralizes by VISIBLE [data]…[/data]
    bracketing and breaks DATA-delimiter tokens with a middle dot so a
    crafted task name can never close the block early.
  - Cassette `recorded_at` is the literal string "recorded" (no wall
    clock) so committed cassettes stay byte-stable across re-records.
  - `GET /candidates` refactored onto the same `_scoped_candidates`
    helper the explain route uses — the presented candidate set (the
    LB-3 whitelist) is one implementation, and existing candidates-route
    behavior is pinned by the untouched test_api tests.
- Deviations: NONE beyond the §10->§11 section-number note above.
- Self-check / MEASURED (Python 3.11, ALL figures synthetic mock data —
  fleet50 = 55,529 open tasks / 50 aircraft / 680 mechanics, seed
  20260710; placeholder §11 endpoint/ids labeled config-defaults):
  - `python3 -m py_compile` clean on all 8 touched/new .py files
    (config.py, ff/llm/{__init__,provider,prompts,validators}.py,
    ff/web/{api,app}.py, tests/test_llm.py).
  - `python3 -m pytest -q`: **209 passed** in 0.98 s (was 158; +51 from
    tests/test_llm.py — red-team corpus RT-001..RT-010, validator hard
    rejects, both retry budgets, BCAI envelope golden, cassette replay,
    audit rows, API happy/degrade/scope/405). No network call anywhere
    (transports injected).
  - `python3 run.py gates`: **ALL PASS** (mini fixture byte-stable,
    validate V1–V9 = 0 violations, 209 passed).
  - TLS tripwire: grep over ff/ + config.py + run.py = 0 hits for all
    six banned bypass tokens (test_no_tls_verification_bypass_anywhere).
  - Fleet50 in-process smoke (state redirected to scratch, repo data/
    untouched): boot 6.66 s; login lead/T01;
    GET /api/v1/explain/candidates?team=T01&limit=10 -> 200 in
    **0.024 s**, count 10, `source: "llm-validated"`, mock_data true,
    grounded narrative ("10 ready candidates … top-ranked 0021-T00049
    carries score 189 at rank 1", confidence 0.9); provider forced to
    always_malformed -> 200 in 0.017 s, `source: "deterministic"`, no
    narrative key, deterministic items intact (LB-8); other-team ask ->
    403 forbidden_scope; audit log = 2 rows (ok=true with 4-stage
    verdicts; ok=false error=LLMSchemaError) — LB-7.
  - Committed cassette replays byte-identically against the FIXED
    fixture context and equals the mock output (determinism test).
- Open items for integrator: production BCAI host + a provisioned FOCU5
  use_case_id are owner actions (config swap, no code change); no auth
  route stores `session["bcai_token"]` yet — `validate_token` is ready
  for it when a PAT-entry surface is approved (until then "bcai" mode
  degrades to deterministic via typed LLMAuthError, by design). The
  sanitizer control-phrase list is code-versioned (L4-D suggests
  config-versioned) — widen only with review, never silently.

## 2026-07-10 — TRACK 3 FINAL INTEGRATION (acceptance record — all figures SYNTHETIC fleet50 mock data, 55,529 open tasks / 50 aircraft / 680 mechanics, seed 20260710; economics rates and §10/§11 ids are config-default placeholders; no optimality claims)
- Files changed: `README.md` (feature list + layout table caught up to
  increments 2–4 and the tooling: config §1–§11, OR-4 line,
  commitments/disruption/progression/llm/bench/sim rows, acceptance-tool
  quickstart lines), `BUILD_LOG.md` (this entry). NO code changes — the
  tree was green at handoff; nothing needed fixing. ARCHITECTURE.md
  untouched (no command/interface drift: the increment addenda already
  match the shipped code).
- Step 1 — py_compile: clean on ALL **53** non-cache `.py` files
  (`find`-driven, single invocation, exit 0).
- Step 2 — full suite: `python3 -m pytest -q` = **209 passed** in 1.03 s
  (0 failed/skipped/xfail; no exclusions).
- Step 3 — `python3 run.py gates`: **ALL PASS** — mini fixture regenerated
  byte-stable (git-clean, sha256 25b0348c3f631889…), mini schedule
  97/120 scheduled (23 reason-coded — expected on the infeasible-tight
  uniform fixture), validate V1–V9 = **0 violations**, 209 passed.
- Step 4 — full chain at scale:
  - `generate-data --aircraft 50` → 50 aircraft / **55,529 tasks** /
    680 mechanics, mock_data=true, sha256 ea402f2936b8a92c….
  - `run-schedule` → **55,529/55,529 scheduled, 0 unscheduled**,
    OTD **19/50**, fleet lateness **574 days**, makespan day 429,
    engine wall **2.316 s**; economics total/unavoidable/controllable =
    **$57.4M / $0 / $57.4M**, late_count 31, source "config-defaults".
  - `validate data/schedule50.json.gz` → **0 violations**
    (V1..V9 all zero).
- Step 5 — points gate (`tools/points_gate.py --data data/fleet50.json.gz`):
  **VERDICT PASS**, no recalibration needed — flow STRICTLY beats chaser
  at all three probe depths (seed 7):
  - 3 shifts: flow **83,239** pts (296 completions) vs chaser **73,269**
    (2,066);
  - 6 shifts: flow **160,268** (657) vs chaser **146,019** (3,097);
  - 9 shifts (canonical, rounds 9): flow **236,539** (1,084 completions,
    fleet lateness 574→575) vs chaser **218,524** (3,946 completions,
    lateness 574→660 = WORSENED 86 d); effort budgets ~equal
    (762,393 vs 755,654 crew-min).
  Ordering invariant: critical 248 vs trivial 6 (>10×). Correlation (a)
  pearson **+0.0673** (n=180) — informational/non-gating by design,
  reported honestly; threshold calibration still needs real data.
  Canonical artifact `outputs/points_validation.json` regenerated by the
  default run; 3-/6-shift probes measured with `--rounds 1` to scratch.
- Step 6 — `tools/bench.py`: **ALL 6 scenarios PASS**, exit 0; every
  reproducibility hash UNCHANGED vs the stored history and every trend
  delta 0 except wall_s — no bound re-centering needed (zero-violations
  bounds untouched, policy upheld):
  - baseline_fleet50: rate 1.0, 0 violations, OTD 19/50, lateness 574,
    wall 2.503 s, hash a4894faa706a;
  - late_parts: rate 1.0, OTD 19, lateness 664, hash 63b4185d72bc;
  - mechanic_absence: rate 0.8875 (reason-coded remainder), OTD 22,
    lateness 536, hash 0bf0e665d80d;
  - pulse_slip: rate 1.0, OTD 17, lateness 619, hash fdd3080a7379;
  - rework_wave: rate 1.0, OTD 19, lateness 570, hash a2dcc7f1c749;
  - skill_shortage: rate 0.5119 (reason-coded), OTD 31, lateness 107,
    hash 8654e2f8154b. Violations **0 (V1–V9) on every scenario**.
- Step 7 — `tools/sim_week.py --data data/fleet50.json.gz --rounds 9`
  (seed 7): lateness trend **574 baseline → 564 final (net −10 days)**
  despite 40 injected tasks/round (360 total, universe 55,529→55,889);
  round-by-round lateness 566, 567, 608, 583, 584, 602, 574, 578, 564;
  OTD stable **19** (one dip to 17 at round 3, one 20 at round 8);
  **0 unscheduled in every round**; executed per shift 543/298/139/519/
  289/114/447/263/133 (S3 nights thinner by design); stability:
  slot 12.3–21.7%, mechanic 51.4–55.0%; wall ≈2.8–3.0 s/round,
  total 31.9 s.
- Step 8 — serve smoke (FF_ENV=dev, :8097, fleet50; ALL state files
  redirected to scratch via FF_ACTUALS/FF_COMMITMENTS/FF_EXCUSALS/
  FF_GAME_EVENTS/FF_LLM_AUDIT — repo `data/` verified untouched):
  - `/healthz` → `{"status":"ok"}`; `/readyz` → ready,
    snapshot_id c9de0684ec143901.
  - login lead/T01 → `/lead` 200 (44,903 bytes): recap card renders with
    the LB-8 `deterministic` note + excusal capture control present;
    `POST /api/v1/excusals` {0021-T00049, LATE_PART, notes} → ok,
    excusable:true, entered_by "lead:T01", source "manual", persisted
    atomically (mock_data:true in the store).
  - login flm/T01 → `/flm` 200: streak strip, badge chips, cause pareto,
    leaderboard Difficulty column and needs-support framing all render.
  - login vp → `/vp` 200: committed-vs-projected board + change log with
    **50 "initial commitment" rows** verbatim; mock-data banner present.
  - `GET /api/v1/explain/candidates?team=T01&limit=5` (lead session,
    mock provider) → 200 in **0.039 s**; `explanation.source` =
    **"llm-validated"**; deterministic content ALWAYS present (5 ranked
    items each with the templated factor explanation); validated
    narrative grounded — "5 ready candidates supplied for team T01.
    Top-ranked candidate 0021-T00049 carries score 189 at rank 1.",
    confidence 0.9, data_basis "supplied ranked candidate metrics only
    (synthetic mock data)"; audit log 1 row ok=true with all 4 stage
    verdicts (schema/candidate_whitelist/numeric_grounding/
    uncertainty_presence) — LB-7.
  - Server killed (port 8097 confirmed closed); smoke state files
    (actuals/excusals/commitments/game_events/llm_audit) deleted.
- Deviations: NONE. Prior-phase notes' open items stand unchanged
  (correlation threshold, badge budgets, event-log rotation, BCAI
  host/use-case provisioning + PAT-entry surface — all owner/real-data
  actions, none blocking).
- Track 3 verdict: **ACCEPTED** — every gate green on one tree, one
  commit-ready state.

## 2026-07-10 — INCREMENT 5: scheduler-side commitment defense (OR-4, config §12)
- Files: `config.py` (§12 COMMITMENT), `ff/domain.py` (Assignment gains
  optional `kept_incumbent: bool = False`, serialized, from_dict tolerant of
  absence), `ff/engine/scheduler.py` (incumbent param +
  `incumbent_from_schedule` helper + committed-first heap key + slot defense
  + mechanic preference w/ the two-attempt fallback + commitment stats),
  `ff/web/app.py` (boot keeps schedule = incumbent None; /replan threads
  `incumbent_from_schedule(previous)` inside `rebuild_state`),
  `ff/sim/digital_week.py` (per-round incumbent threading, default ON, +
  `in_horizon_survivors`/`in_horizon_slot_stable_pct` + per-round commitment
  counters), `run.py` (`run-schedule --incumbent <schedule.json.gz>`),
  `tools/sim_week.py` (`--no-incumbent` A/B control arm + ih columns),
  `tests/test_commitment_defense.py` (NEW, 10), `BUILD_LOG.md`.
- Contract sections implemented: the ENTIRE INCREMENT 5 addendum. MAX
  reference semantics (GROUNDING §1, config §10 commitment layer):
  committed-first dispatch (heap key gains a leading 0/1 in-horizon
  component; the ascending-slack key is untouched for the non-committed
  component), incumbent slot tried FIRST at/after the precedence/parts
  floor ("precedence is physics; stickiness is not"), incumbent-crew
  preference with the two-attempt free-choice fallback ("crew continuity
  must never cost slot continuity"), free repack beyond
  COMMITMENT_HORIZON_DAYS=3. Stats gain `commitment_in_horizon` /
  `commitment_kept` / `commitment_mech_kept`.
- Decisions (inside the contract's latitude):
  - Mechanic preference (with its two-attempt fallback) applies at EVERY
    slot an in-horizon task probes (defense slot AND later scan slots), so
    a task that loses its slot to physics still tries to keep its crew.
  - Pinned in_progress work is offered the same defense; its floor is
    start_day, so a stale (past) incumbent slot is skipped — floor wins.
  - `incumbent_from_schedule` maps EVERY assignment; the engine filters to
    the in-horizon band at init (COMMITMENT_ENABLED honored there).
  - `kept_incumbent=True` is set ONLY by the defense-success path; the
    `test_validator_clean...` test pins kept flags == commitment_kept.
- Deviations: NONE.
- Self-check / MEASURED (Python 3.11, ALL figures synthetic fleet50 mock
  data — 55,529 open tasks / 50 aircraft / 680 mechanics, seed 20260710;
  placeholder economics rates; no optimality claims):
  - `py_compile`: clean on ALL 54 non-cache `.py` files.
  - `python3 -m pytest -q`: **219 passed** (was 209; +10 from
    tests/test_commitment_defense.py — every addendum tripwire, docstrings
    quote OR-4 and the MAX rules verbatim).
  - `python3 run.py gates`: **ALL PASS** (mini fixture byte-stable,
    validate V1–V9 = 0 violations, 219 passed).
  - **Byte-identical incumbent=None proof at scale**: fleet50 run-schedule
    (no incumbent) reproducibility hash `a4894faa706a` — EXACTLY equal to
    the committed pre-change `data/schedule50.json.gz` and the stored bench
    history baseline; `tools/bench.py` = ALL 6 PASS, every scenario
    hash_changed=False, every trend delta 0 except wall_s. Old fixture
    (no `kept_incumbent` key) loads with the field defaulting False.
  - Fleet50 self-incumbent (schedule twice, second threading the first):
    commitment_in_horizon **2,866**, commitment_kept **2,866** (100%),
    commitment_mech_kept **2,866**; scheduled 55,529/55,529, 0 unscheduled;
    validate V1–V9 = **0 violations**. Lateness 574 -> 595 (+21 d) on the
    self-replan (defended slots trade some left-pull), OTD 19 both.
  - Performance (same-machine 3-run A/B, engine wall): no-incumbent min
    **2.695 s** vs self-incumbent min **2.761 s** = **+2.4%** (budget ~10%;
    historical baselines 2.316–3.5 s bracket both). No pre-change checkout
    exists (not a git repo), so bench-history walls (2.33–2.61 s today,
    +0.05–0.23 s vs stored) are the honest cross-change reference.
  - **Digital-week A/B (seed 7, 9 rounds, fleet50)** — the acceptance gate:
    - in-horizon slot stability: OFF **20.5–50.8%** (mean 37.7) -> ON
      **74.3–99.8%** (mean **92.0**) — exceeds the MAX reference band
      (64–73%); day-rolled S3 rounds sit at 74.3–78.4 (the horizon window
      shifts), intra-day rounds at 99.1–99.8. kept/offered over 9 rounds:
      18,927/20,607 (91.8%).
    - fleet-wide: slot% 12.3–21.7 (mean 15.4) -> 12.5–23.7 (mean 16.1);
      mech% 51.4–55.0 (mean 53.3) -> 50.8–57.6 (mean 54.0).
    - HONEST TRADEOFF (not hidden): per-round fleet lateness mean 581 ->
      599 (+18 d ≈ +3.1%), final round 564 -> 619 (+55 d ≈ +$5.5M
      controllable at placeholder rates); OTD 17–20 -> 18–20 (comparable).
      Stability is bought with some left-pull — tune
      COMMITMENT_HORIZON_DAYS if the balance shifts on real data.
    - 0 unscheduled every round, both arms; wall ≈3.1–3.6 s/round both arms.
  - `tools/points_gate.py`: **VERDICT PASS** — flow 236,539 vs chaser
    218,524 (probe numbers unchanged); informational correlation (a) moved
    +0.0673 -> **-0.0713** (n=180, toward the gate's stated negative
    target) because the gate's internal digital week now threads incumbents;
    non-gating by design, reported honestly.
  - Serve smoke (mini fleet, state redirected to scratch): boot commitment
    stats 0/0/0 (no previous plan — byte-identical path), POST /replan 200
    threads the incumbent (54/54/54 on mini), validator 0 before and after;
    kept_incumbent flags == commitment_kept.
- Open items for integrator: the digital-week default is now
  incumbent-threading ON (`--no-incumbent` is the control arm);
  `outputs/points_validation.json` and `outputs/bench_results.jsonl` were
  regenerated/appended by the gate runs. The lateness-vs-stability tradeoff
  above is the number to re-measure when real (non-mock) execution data
  exists.

## 2026-07-10 — INCREMENT 5 MEASUREMENT + INTEGRATION (acceptance record — all figures SYNTHETIC fleet50 mock data, 55,529 open tasks / 50 aircraft / 680 mechanics, seed 20260710; placeholder economics rates; no optimality claims)
- Files changed: `ff/web/api.py` (integration fix: POST /replan response
  `stats` now carries `commitment_in_horizon` / `commitment_kept` /
  `commitment_mech_kept` — the §12 defense was invisible at the API;
  additive only, docstring cites OR-4), `README.md` (OR-4 line gains the
  §12 scheduler-side defense + the measured A/B headline), `BUILD_LOG.md`
  (this entry). Everything else verified as shipped by the builder.
- Step 1 — hygiene: `py_compile` clean on ALL **54** non-cache `.py`
  files; `python3 -m pytest -q` = **219 passed** (1.06 s; re-run green
  after the api.py fix); `python3 run.py gates` = **ALL PASS** (mini
  fixture byte-stable, validate V1–V9 = **0 violations**, 219 passed).
- Step 2 — byte-identical incumbent=None proof at scale (independently
  re-measured): fresh `run-schedule` (no incumbent) vs the pre-change
  `data/schedule50.json.gz` artifact — **0 semantic assignment diffs over
  55,529 rows**, unscheduled maps equal, stats equal after removing
  wall_seconds + the three new counters (all 0), `kept_incumbent=True`
  count 0, reproducibility hash **a4894faa706a** on BOTH (the stored bench
  baseline). Old fixture rows (no `kept_incumbent` key) load fine.
- Step 3 — **digital-week A/B (the acceptance gate)**: two runs of
  `tools/sim_week.py --data data/fleet50.json.gz --rounds 9 --seed 7`,
  arm A `--no-incumbent` (control) vs arm B default (threading ON).
  Baseline both arms: lateness 574 d, OTD 19, 55,529/55,529, $57.4M.

  | rnd | slot | A slot% | B slot% | A mech% | B mech% | A ih% | B ih% | A late | B late | A otd | B otd |
  |----|------|--------|--------|--------|--------|-------|-------|-------|-------|------|------|
  | 1 | d0S1 | 12.3 | 14.0 | 51.4 | 51.1 | 32.1 | **99.8** | 566 | 576 | 19 | 20 |
  | 2 | d0S2 | 14.7 | 18.3 | 52.6 | 56.2 | 41.2 | **99.7** | 567 | 616 | 19 | 18 |
  | 3 | d0S3 | 15.5 | 12.5 | 54.5 | 50.8 | 20.5 | **74.3** | 608 | 613 | 17 | 19 |
  | 4 | d1S1 | 14.0 | 14.5 | 53.0 | 53.5 | 36.7 | **99.3** | 583 | 595 | 19 | 18 |
  | 5 | d1S2 | 18.5 | 19.4 | 54.6 | 56.1 | 48.0 | **99.8** | 584 | 579 | 19 | 20 |
  | 6 | d1S3 | 13.9 | 13.4 | 53.9 | 52.2 | 34.1 | **78.4** | 602 | 611 | 19 | 19 |
  | 7 | d2S1 | 12.7 | 14.8 | 52.2 | 54.6 | 42.3 | **99.1** | 574 | 600 | 19 | 19 |
  | 8 | d2S2 | 21.7 | 23.7 | 55.0 | 57.6 | 50.8 | **99.2** | 578 | 581 | 20 | 19 |
  | 9 | d2S3 | 15.1 | 14.2 | 52.5 | 53.7 | 33.5 | **78.0** | 564 | 619 | 19 | 18 |

  - **in-horizon slot stability: 20.5–50.8% (mean 37.7) -> 74.3–99.8%
    (mean 92.0)** — clears the MAX reference band (64–73%). Day-rolled S3
    rounds (window shifts) sit at 74.3–78.4; intra-day rounds 99.1–99.8.
    kept/offered over 9 rounds: **18,927/20,607 = 91.8%** slot-kept,
    19,154 crew-kept (counters summed from the run JSON).
  - fleet-wide: slot% 12.3–21.7 (mean 15.4) -> 12.5–23.7 (mean 16.1);
    mech% 51.4–55.0 (mean 53.3) -> 50.8–57.6 (mean 54.0).
  - **HONEST TRADEOFF (per the gate: never hidden)**: per-round fleet
    lateness mean 580.7 -> 598.9 (**+18.2 d ≈ +3.1%**); final round
    564 -> 619 (**+55 d ≈ +$5.5M controllable at placeholder rates**).
    OTD comparable: 17–20 -> 18–20 (final 19 -> 18). Defended slots trade
    away some left-pull — tune COMMITMENT_HORIZON_DAYS on real data.
  - **0 unscheduled every round, both arms**; wall/round 3.1–3.5 s (A,
    total 35.7 s) vs 3.0–3.3 s (B, total 36.9 s).
- Step 4 — gates re-run on the final tree:
  - `tools/points_gate.py --data data/fleet50.json.gz`: **VERDICT PASS** —
    flow 236,539 vs chaser 218,524; ordering invariant 248 vs 6;
    informational correlation (a) pearson **-0.0713** (n=180, toward the
    stated negative target now that the gate's internal week threads
    incumbents; non-gating by design).
  - `tools/bench.py`: **ALL 6 PASS**, exit 0, every `hash_changed=False`
    (baseline a4894faa706a / late_parts 63b4185d72bc / mechanic_absence
    0bf0e665d80d / pulse_slip fdd3080a7379 / rework_wave a2dcc7f1c749 /
    skill_shortage 8654e2f8154b), every trend delta 0 except wall_s —
    **no bound re-centering needed**; zero-violations bounds untouched.
- Step 5 — serve smoke (FF_ENV=dev, :8098, fleet50; ALL state redirected
  to scratch via FF_ACTUALS/FF_COMMITMENTS/FF_EXCUSALS/FF_GAME_EVENTS/
  FF_LLM_AUDIT — repo `data/` verified untouched afterwards):
  - boot ready (snapshot c9de0684ec143901); JSON login lead/T01;
    `POST /actuals` x3 (0021-T00049, 0041-T00193, 0041-T00004 -> done,
    audit_seq 1–3, persisted to scratch).
  - `POST /replan` #1 -> 200: 55,526/55,529 scheduled (3 done left the
    graph), 0 unscheduled, validator_total **0**, stats
    commitment_in_horizon/kept/mech_kept = **2,866/2,866/2,866**.
  - `POST /replan` #2 -> 200: **commitment_in_horizon 2,867 > 0,
    commitment_kept 2,867 > 0** (visible in the response stats via the
    api.py fix), validator_total **0**, snapshot_id identical
    (deterministic fixpoint). Kept-task check: 25 sampled assignments
    **all 25 byte-equal across the two replans**; the 3 in-horizon ones
    carry `kept_incumbent: true` (e.g. 0021-T00027 held d2/S1 min 0–348
    with crew T01-S1-M019+M021 in both plans).
  - Server killed (port 8098 confirmed closed); scratch smoke state
    deleted; `git status data/` shows only the expected gates-regenerated
    `data/mini/schedule.json.gz` (assignments now serialize
    `kept_incumbent`).
- Deviations: NONE. The builder's increment-5 entry above was
  independently re-measured — every claimed number reproduced exactly.
- Increment 5 verdict: **ACCEPTED** — OR-1..6 tripwires green, validator
  never weakened, incumbent=None byte-identical, A/B evidence recorded
  with the lateness tradeoff stated.

## 2026-07-11 — INCREMENT 6 Parts A+B: max_v1 envelope exporter + vendored FOCUS Flask dashboard (all figures SYNTHETIC fleet50 mock data, 55,529 open tasks / 50 aircraft / 680 mechanics, seed 20260710; placeholder economics rates)
- Files ADDED: `ff/export/__init__.py`, `ff/export/envelope.py` (the Part-A
  exporter: build_envelope/export_envelope/update_commitments_for_cli/
  build_bems_map), `tests/test_envelope_export.py` (13 adapter-compat
  contract tests), `web_flask/` (Part B: MAX/web_app_flask vendored
  VERBATIM — 55 files, diff -r exit 0 pre-shim — plus
  `web_flask/src/ff_constants.py` shim and `web_flask/VENDOR_CHANGES.md`),
  `outputs/schedule50_inc6.json.gz`,
  `outputs/schedules/max_v1_20260711_S1_003258_UTC.json.gz`.
- Files MODIFIED: `run.py` (export-envelope subcommand; run-schedule
  auto-export w/ --no-envelope/--envelope-dir/--commitments-state; gates
  gains the envelope step against a throwaway temp dir + existence check),
  `ff/web/app.py` (rebuild_state exports the envelope on REPLANS only —
  boot never exports; FF_ENVELOPE_DIR/FF_ENVELOPE_EXPORT env; failure =
  honest warning, never a failed replan), `tests/test_api.py` +
  `tests/test_progression.py` (app fixtures redirect FF_ENVELOPE_DIR into
  the temp base — same never-pollute-the-repo rule as the state files).
- Vendored-tree diffs (each with file:line in `web_flask/VENDOR_CHANGES.md`):
  ff_constants shim (460/460/370, 480/480/390, 520/520/430, 1290 + verbatim
  shift detection) replacing `max.core.config` imports in
  `src/max_adapter.py:38` and `src/blueprints/development.py:31` and the
  `max.core.shift_detection` lazy import at
  `src/blueprints/shift_performance.py:1081`; `src/app.py:24-40`
  flask_cors/flask_compress made optional (packages absent here; no-op
  shims, byte-identical behavior when installed); `src/paths.py` comments
  only (MAX_ROOT name kept, now = FF_app root, so the default
  SCHEDULES_DIR lands on FF_app/outputs/schedules with MAX_SCHEDULES_DIR
  still winning). Residual `max.*` import scan over web_flask: ZERO live
  imports (one comment mention). NOTHING under /home/user/MAX_FOCUS_1
  touched. The vendored adapter was NEVER modified beyond the documented
  shim lines (acceptance rule 5 — no adapter fixes were needed; the two
  wrong contract-test expectations were fixed in the TEST, documented
  there: teamCapacities 1-seat padding for roster-less skill pools is
  deliberate upstream behavior, identical on MAX production envelopes).
- Envelope contract decisions (verified against the adapter SOURCE):
  `soi = task_id` ("0021-T00049"), `line_number = aircraft`,
  `taskId = f"{soi}_{line}"`; BEMS = digit projection of FF mech ids
  ("T01-S1-M001" -> "011001") because the vendored My Day lists only
  all-digit ids (`_is_real_mech` = str.isdigit); originals ride along in
  `ff_mechanic_ids` + top-level `ff_mechanic_map` for the Part-C
  write-back. Unscheduled work exports at horizon end (makespan+1) with
  isFallback=true + full crewShortfall (C20-style honesty; the adapter
  coerces day=None to 0, which would smear unplaceable work onto day 0).
  mechanic_timelines = dict keyed by BEMS (the is_max_envelope detection
  signal), one entry per crew MEMBER (OR-1: every seat is real).
  metadata.stats carries economics (per_aircraft+fleet, source
  config-defaults), capacity_pressure (camelCase pools + linesAnalyzed +
  topSkills), projection (committed/earlyFlow/delivered/changes from the
  ff commitments block, stationFeed:false), final_lateness, otd,
  commitment_* counters, mock_data:true. Reference date = 2026-01-26 (a
  Monday: FF day 0 law); ISO wall starts S1 06:00 / S2 14:30 / S3 22:30.
  CLI + replan exports advance the commitments state (MAX doctrine);
  gates route it to a throwaway dir so mini runs never touch the fleet50
  `data/commitments.json` (verified byte-identical after the full chain).
- MEASURED acceptance record:
  1. `python3 -m pytest -q` = **232 passed** (219 baseline + 13 new) in
     1.47 s; re-run after all smokes: 232 passed.
  2. `python3 run.py gates` = **ALL PASS** (mini 97/120 scheduled + 23
     honest fallbacks; validate V1-V9 = 0 violations; envelope step wrote
     max_v1_*.json.gz to the gate temp dir; 232 passed).
  3. Full chain: `run-schedule --data data/fleet50.json.gz --out
     outputs/schedule50_inc6.json.gz` -> 55,529/55,529 scheduled, 0
     unscheduled, otd 19, makespan 429, engine wall 3.227 s (command total
     15.0 s incl. envelope build+gzip) -> auto-exported
     `outputs/schedules/max_v1_20260711_S1_003258_UTC.json.gz`
     (**6,089,639 B** gz, 55,529 tasks, 680 timeline pools).
  4. Vendored boot (cd web_flask; MAX_SCHEDULES_DIR=<FF_app>/outputs/
     schedules python3 run.py; port 5000) loaded the FF envelope
     ("55,529 tasks") — curl transcript (status, size, counts):
     `/` 200 11,151 B; `/dashboard` 200 111,492 B; `/dashboard/classic`
     200 120,957 B; `/api/available-schedules` lists the 1 FF envelope;
     `/api/scenario/3stage?limit=5` 200, pagination total **55,529**,
     unique integer priority ranks (sample 46660/22043/22808/22778/53963),
     teamSkill "T04 S2 (POWER)", integer mechanic_id + digit bems_id;
     `/api/scenario/3stage/summary` 200 436 B (55,529/55,529, success
     100.0, 886 critical); `/api/projection` 200 20,115 B — committed
     **50**, earlyFlow 0, delivered 1, changes 50, finalLateness **574**
     (the known fleet50 figure), stationFeed false;
     `/api/projection/economics` 200 12,571 B — **$57.4M** total /
     $57.4M controllable / 31 late / 50 rows / source config-defaults;
     `/api/capacity` 200 11,012 B — 46 pools, linesAnalyzed 31, top pool
     T06/S2 797 waitDays $79.7M-days, topSkills populated;
     `/api/shiftbook/options` 200 278 B — 20 teams, todayDay 0, shift 1;
     `/api/myday/options` 200 9,582 B — **680 BEMS = the full FF roster**
     (011001..203008); data probes: `/api/shiftbook?team=T01&day=0&
     shift=1` -> 39 tasks / 17 mechanics; `/api/myday?bems=011001&day=0`
     -> 2 tasks / 463 min + next-day preview (day 1, 4 tasks). Server
     killed; port 5000 verified closed; smoke runtime artifacts
     (web_flask/data/shift_performance.db) removed.
  5. Web replan path smoke (mini, all state in scratch): boot exports 0
     envelopes (boot never exports), POST /replan 200 -> exactly 1
     envelope in FF_ENVELOPE_DIR, adapts clean (adapted_from=max, 120
     tasks, projection committed 3), zero warnings.
- Determinism: byte-identical exports for identical inputs + fixed `now`
  (tripwire test); envelope bytes via loader.save_json_gz (sorted keys,
  gzip mtime=0, atomic tmp+replace); discovery stays MTIME-newest (owner
  rule quoted in export_envelope docstring — never re-sorted by metadata).
- Honesty notes: totalWorkforce on the dashboard reads 1,118 = 680 roster
  seats + 438 deliberate upstream 1-seat pads for scheduled skill-pools
  without roster records (adapter behavior, identical on MAX envelopes;
  documented in the contract test); economics rates remain placeholders;
  every figure above is fleet50 SYNTHETIC mock data.
- Part C NOT started (per orchestrator instruction).

## 2026-07-11 — INCREMENT 6 Part C: FF bridge + 4 insight tabs + My Day write-back (all figures SYNTHETIC fleet50 mock data unless marked mini; placeholder economics rates)
- Files ADDED: `web_flask/src/blueprints/ff_bridge.py` (the /api/ff proxy:
  service session via POST /login role=director scope=all + re-login-on-401,
  urllib+cookiejar stdlib only; GET points/shift, points/leaderboard,
  progression/team/<t>, recap, excusals, explain/candidates; POST excusals,
  actuals, replan — the ONLY writes, forwarded verbatim to the FF single
  write-paths after the documented identity translation taskId->ff_task_id /
  BEMS->ff mech id; replan additionally runs the LOCAL reload_schedules()
  and reports dashboard_refreshed/current_schedule; FF down = honest 502
  {error, code:"ff_backend_down"}; FF error statuses pass through verbatim),
  `web_flask/templates/partials/_arena_view.html` / `_progression_view.html`
  / `_excusals_view.html` / `_explain_view.html` (house pattern: one
  self-contained partial each, included in BOTH shells, own fetch/render;
  Arena ring + goal/earned/attainment + excusals listed visibly + GG-2
  leaderboard with needs-support framing; Progression streak/GG-3 pause
  count/badges w/ earning events/level-XP/recap card; Excusals merged
  auto+manual list + capture form w/ client-side notes-required mirror;
  Explain decomposed components + deterministic-always + source badge
  deterministic|llm-validated per LB-8), `tests/test_ff_bridge.py` (12
  tests: BOTH apps booted — FF backend over real HTTP on an ephemeral
  werkzeug server w/ mini fleet + all state in pytest tmp, web_flask via
  test client w/ MAX_SCHEDULES_DIR+FF_API_BASE pointed at the fixtures).
- Files MODIFIED (each with file:line in `web_flask/VENDOR_CHANGES.md`
  §7-§11): `web_flask/src/app.py:104-108,260` (bridge import+register),
  `web_flask/templates/dashboard-ios.html:112-127,1274-1278` +
  `dashboard2.html:63-67,92-96` (4 nav buttons + 4 includes per shell,
  matching data-view ids arena/progression/excusals/explain),
  `web_flask/templates/partials/_myday_view.html` (THE one permitted touch
  to an existing view — additive only: Start/Done/Blocked(parts-ETA prompt)
  buttons on today's cards, #mdWriteStatus line, write-back loop actuals ->
  replan -> /api/refresh-fallback -> re-fetch, confirm dialog on the 409
  done-reopen + force re-post; preview cards render byte-identically).
  Existing 17 views otherwise untouched; nothing under /home/user/MAX_FOCUS_1
  touched; OR-6 held (the bridge forwards to FF POST /api/v1/actuals — no
  second write-path); GG-1 held (Arena/Progression are GET-only surfaces).
- MEASURED acceptance record:
  1. `python3 -m pytest -q` = **244 passed** (232 baseline + 12 new) in
     3.40 s. `python3 run.py gates` = **ALL PASS** (mini validate V1-V9 =
     0 violations; envelope step OK; 244 passed). The gates run's only
     repo side effect (`data/mini/schedule.json.gz`, wall_seconds
     0.005->0.006, assignments/unscheduled byte-equal) was restored to
     HEAD after verification.
  2. Live smoke (FF backend `run.py serve --port 8080`, FF_ENV=dev,
     fleet50, ALL FF state redirected to scratch via FF_ACTUALS/
     FF_COMMITMENTS/FF_EXCUSALS/FF_GAME_EVENTS/FF_LLM_AUDIT,
     FF_ENVELOPE_DIR=outputs/schedules; web_flask on :5000 with
     MAX_SCHEDULES_DIR=<FF_app>/outputs/schedules + FF_API_BASE):
     - FF ready in ~2 s (snapshot **c9de0684ec143901** — the known
       deterministic fleet50 id); dashboard loaded the 55,529-task envelope.
     - Bridge tab APIs (through :5000): points/shift T01 d0 S1 -> goal
       2,311 / earned 0 / attainment 0.0 / **excused 185 pts across 3
       excusals listed** / 39 breakdown rows; leaderboard -> 20 rows,
       needs_support framing present, **zero raw-point keys** (GG-2);
       progression/team/T01 -> streak 0 (305 counted, **53 paused** GG-3),
       level 1 / 0 XP; recap -> generated_by **deterministic**, mock_data
       true; excusals GET T01 -> **2,480 merged records** (CROSS_TEAM_
       PREDECESSOR / REWORK_INJECTION / SAME_TEAM_PREDECESSOR); explain ->
       5 candidates, source **llm-validated** (mock provider, confidence
       0.9, template explain_candidates@1.0.0) WITH the deterministic
       items always present (LB-8); excusal POST w/o notes -> **400
       notes_required passthrough**, with notes -> 200 manual_count 1.
     - MY DAY WRITE-BACK LOOP (bems 011001, day 0, before: 0013-T00039_13
       397 min + 0006-T00055_6 66 min = 463 min): POST /api/ff/actuals
       {taskId:"0013-T00039_13", state:"done"} -> 200, translated
       task_id **0013-T00039**, audit_seq 1; POST /api/ff/replan ->
       **19.75 s**, ok, 55,528/55,529 scheduled, 0 unscheduled,
       validator_total 0, commitment 2,865/2,865, dashboard_refreshed
       true; envelope **max_v1_20260711_S1_003258_UTC.json.gz ->
       max_v1_20260711_S1_005642_UTC.json.gz** (6,097,692 B); POST
       /api/refresh idempotent-success; My Day re-fetch -> **0013-T00039_13
       GONE** (day now 0041-T00141_41 + 0006-T00055_6 = 371 min); reopen
       without force -> **409 done_reopen_requires_force** passthrough.
     - Both served shells grep-verified: data-view arena/progression/
       excusals/explain present **1x nav + 1x view container each** in
       /dashboard AND /dashboard/classic.
     - Teardown: both servers killed (ports 5000/8080 closed); replan
       envelope deleted -> outputs/schedules again holds EXACTLY the one
       pre-smoke fleet50 envelope (md5 02e71941563b2b78c6d8fcea12e3ec1e
       unchanged); data/commitments.json md5 bbca75b6a20a7ead11de10d27c620d2a
       unchanged; scratch smoke state deleted; web_flask/data/
       shift_performance.db runtime artifact removed; repo `data/` clean.
- Honesty notes: the FF v1 actuals endpoint ignores the forwarded
  parts_eta_day/mechanic_id ride-alongs (documented in VENDOR_CHANGES §11);
  the bridge runs an all-scope SERVICE session — per-user auth pass-through
  is the documented future hardening item (VENDOR_CHANGES §7); Arena/
  Progression/Explain figures above are SYNTHETIC fleet50 mock data and the
  LLM narrative came from the deterministic mock provider.

## 2026-07-11 — 5-day demo week: 15 executed shift task lists + dashboards proof (PR #10)

Owner request: give the baseline schedule "accompanying afterward
schedules" — 15 task lists (3 shifts/day x 5 days, OR-3 chronology:
Mon-S3 starts Sunday night, then Mon-S1, Mon-S2, Tue-S3, ...), each with
evolving shop-order statuses (complete / new injected rework / crews NOT
following the plan), each state re-run through the scheduler, dashboards
proving performance/team/delivery metrics react.

- `ff/sim/digital_week.py` gained two off-by-default non-compliance
  levers (`deviate_rate`, `oos_per_round`) + `on_replanned` hook +
  STRICT execution physics (auto-on with levers; legacy rng streams
  byte-identical — tripwire-tested).
- FIRST RUN measured 27 V2 violations across 15 replans — root cause:
  independent completion coins let work start on top of skipped preds,
  and in_progress pinning + committed-first dispatch let the skipped
  pred re-book after its started successor. Fixed with the VOID-EDGE
  rule (scheduler + validator V2 exemption, keyed strictly on task
  state: started work voids its incoming edges; P_OOS already charges
  the floor violation) + strict slot chronology in the sim. This is the
  production-honest semantic for real MES actuals, not a demo patch.
  `test_commitment_defense` two-attempt geometry re-sculpted
  (per-mechanic pinned chains); 252 tests pass.
- SECOND RUN (tools/sim_5day.py, seed 11, exec 0.88 / deviate 0.07 /
  oos 1 / rework 12 per shift, all SYNTHETIC fleet50 mock data):
  - baseline (anchored start_day=6, first slot (6,S3)): 55,529/55,529
    scheduled, lateness 720 d, OTD 16/50, controllable $65.5M
    (placeholder rates), V1-V9 = 0, wall 3.2 s.
  - 15 executed shifts: per-shift completions 107-487, slid 14-80,
    deviated 3-40, blocked-by-pred 2-54, off-plan 3-40, OOS 1/shift,
    rework +12/shift (+180 total); fleet attainment 73.6-86.8%;
    lateness 798 -> 854 d; OTD 12-16; in-horizon slot stability
    96.7-99.1% on day shifts, 67.8-84.6% on day-rolled S3 rounds.
  - V1-V9 across baseline + 15 replans: **0 violations**.
  - commitments ledger: 152 rows (50 initial, hysteresis-gated changes
    with measured causes e.g. "rework_inserted=7 + execution_shortfall=2
    (sustained 2 replans)"), 27 sustained improvements -> recovery
    moments (best: aircraft 4, day 17 -> 11).
  - GAMES: 9,094 derived events (4,447 completion-credited, 1,649
    unlock, 129 keystone-cleared, 27 recovery-moment, 2,842
    badge-earned); 170 lead-captured excusals (GG-3).
  - artifacts: demo/week1/ (15 task-list MDs + json.gz, 16 envelopes
    [gitignored, regenerable], metrics JSON, SUMMARY.md, end-of-week
    state for the FF API, 11 dashboard screenshots).
- Dashboards proof: FF API booted from the end-of-week state files +
  vendored FOCUS dashboard over the 16 mtime-ordered envelopes; all 11
  tabs screenshotted (Shift Book browsing the week, Commitments showing
  98.3% near-term slots defended + cause-logged date changes,
  Progression showing paused-not-broken streaks / L7 / recovery
  badges, Arena showing the LIVE upcoming shift with excused points —
  executed-shift scoreboards live in the task lists and metrics, by
  design: the live snapshot cannot re-derive executed plans).

## 2026-07-11 — Fortnight: 14 days / 32 shifts + graded scorecard (PR #11)

Owner request: 14 days x 3 shifts of schedules; verify the application
can GRADE performance by shift, by day, by week, and TREND progress,
with views by team / shift / position (superintendent) / building.

- Calendar honesty: 14 calendar days = **32 eligible shifts**, not 42
  (OR-3: S3 runs Sunday night through Friday night; Saturday night is
  dark). `tools/sim_5day.py --days N` computes rounds from the calendar.
- `ff/services/scorecard.py` (+12 unit tests, hand-computed arithmetic):
  execution records -> graded tables at 3 period grains x 5 slices
  (fleet/team/group=superintendent position/shift crew/building), GG-3
  excused-leaves-goal, GG-2 off-plan-never-earns -> compliance metric,
  GRADE_BANDS letter grades (placeholder rubric, config §10),
  least-squares trend classification + week-over-week deltas. Building
  derives from the aircraft line station (P01-05 FAL-A, P06-10 FAL-B,
  POST-FAL FLIGHTLINE, LATE-DELIVERY DELIVERY); TEAM_GROUP_SIZE moved
  to config §10 (web super scopes + grading share one rule).
  Tests caught a real bug: aggregate() sorted period keys as STRINGS,
  reversing trend series past day 9 — fixed with numeric ordering.
- `GET /api/v1/scorecard` (FF_SCORECARD history feed, honest 404 bare),
  `/api/ff/scorecard` bridge route, Scorecard (NEW) tab on BOTH shells.
- MEASURED fortnight (seed 11, same non-compliance levers; SYNTHETIC):
  - baseline 55,529/55,529, lateness 720 d, OTD 16/50; **V1-V9 = 0
    across all 33 plans**; 10,093 graded records; 15,937 game events;
    264 commitment rows; 304 lead-captured excusals; +384 rework rows.
  - Fleet: W1 attainment 81.7% (C) -> W2 81.3% (C), WoW -0.4 pts;
    compliance 86.6% -> 88.1%.
  - Shift crews: S1 84.4/84.2 (best), S2 79.6/79.5, S3 78.7/77.0 —
    night shift measurably weakest, exactly the operational signal the
    view exists to surface.
  - Positions: G1-G4 all C-band; trend engine flags G2 DECLINING at day
    grain (-0.9%/day slope) while others hold flat.
  - Buildings: DELIVERY compliance 98.6/98.3% vs FAL-B 61.0/70.6% —
    deviating crews freelance where ready non-critical work exists
    (the factory), not at delivery; physically coherent.
  - Saturdays (night-only work days) grade C/D on thin goals — honest,
    labeled by the day view.
- Dashboards: FF API booted with FF_SCORECARD; Scorecard tab
  screenshotted in 5 slice/period combos + Shift Book (33 envelopes),
  Commitments, Progression at end of fortnight
  (demo/fortnight/screenshots/).

## 2026-07-11 — Gate pressure MEASURED: adversary probes + 3-arm fortnight A/B (PR #12)

Full investigation + verdicts in docs/GATE_PRESSURE_DESIGN.md §9. All
SYNTHETIC (fleet50 mock data, seed 11); V1-V9 = 0 across all 99 A/B
plans + gate reruns.

- Slow-roller adversary: mini fleet roller LOSES -11% (11,153 vs
  12,541); fleet50 9/18-shift probes tie EXACTLY (pre-saturation
  horizons: farming cannot construct an advantage). Fleet50 gate PASS
  (flow 236,539 > chaser 218,524; roller check permanent).
- 3-arm fortnight: boost cuts end aged-open backlog 24% under identical
  chasing behavior (B 1,901 -> C 1,451 pts); both greedy arms keep
  backlog fresher than status quo (avg age 2.05-2.31 d vs 3.47); cost:
  -1 OTD, -1.5 attainment pts W2, +29 lateness d vs B (still beats
  status quo 1,013 vs 1,074). Pressure, not distortion.
- Findings + limitations (single seed; ~7% behavior channel => floors
  not ceilings) + shipping defaults recorded in the doc.

## 2026-07-11 — Excusal rulings VALIDATED live (PR #13)

5-day validation run (seed 11, greedy deviation; SYNTHETIC): V1-V9 = 0
across all 16 plans. Live cause mix under the three owner rulings
(docs/EXCUSAL_POLICY.md):

- Ruling 1 (parent-SOI ownership): sim-injected fix mix measured 108
  self / 72 cross-trade (the 60/40 design); origin now DERIVES from the
  parent SOI for rows written before the field existed (generator punch
  work: same-team parents -> owned, evidence says "[derived from parent
  SOI]"). End-state records: 4,155 owned / 54 excusable (cross-trade).
- Ruling 2 (root-cause pass-through): SAME_TEAM_PREDECESSOR split
  21,168 excused-by-external-root vs 11,644 owned — 64% pass-through in
  THIS mock (20 teams interleave densely on every aircraft DAG, so most
  own-team chains hit a cross-team task within a few hops). Flagged for
  owner review as a calibration lever (chain-walk cap / root-distance
  bound); on real zone-based ownership the rate will be lower.
- Ruling 3 (3x overrun tolerance): 13 DURATION_OVERRUN records fired,
  all at 1.5x actual/standard -> honestly excused; no >3x blowout
  occurred in a 5-day window (the sim's slide model reaches 3x only
  after ~5 consecutive slides — rare by construction, boundary
  unit-tested at 2.5x/3.5x).

## 2026-07-11 — Factory Wall: division leagues, overdrive lane, kiosk page (PR #14)

Owner directive: team and team+shift leaderboards for competition,
badges + progress feed + progression bars on large factory monitors.
Owner questions answered in the design:

- Attainment IS earned-vs-scheduled and stays CAPPED at 100% (GG-2: the
  correlation-gate law — off-plan volume never outranks plan-hitting).
  Above-schedule work (blockage break-throughs, pulled-ahead jobs, aged
  backlog burn-down) now shows as the OVERDRIVE lane (+N% OD = off-plan
  value / goal): strictly a tie-breaker, never lifts a unit above
  higher attainment (tripwire-tested with a 5000-pt freeloader).
- Station-maturity fairness (700/711/722/733 job-mix differences):
  DIVISIONS — each team competes in its control-station cohort (band
  holding the majority of its executed value) plus the fleet board;
  ROLLING WINDOW (WALL_WINDOW_DAYS=5) de-lumps small-slice units.

Shipped: scorecard slice 'team_shift' (crew units, e.g. T01-S1);
ff/services/wall.py (boards, divisions, progression bars, badge/
recovery feed — read-model only, aggregates only per §G8);
GET /api/v1/wall + /wall kiosk page (dark, 1920x1080, 4 auto-rotating
panels, 60 s refresh; WALL_PUBLIC=False default — kiosk mode is an
explicit env opt-in for trusted internal networks). 293 tests pass
(8 new: competition law, divisions, crew units, feed shapes, rolling
window, API auth/kiosk/404).

Measured on the fortnight history (SYNTHETIC): divisions DELIVERY 4 /
FLIGHTLINE 15 / FAL-B 1; fleet top: T16 88.7% (+8.6 OD), T17 86.4%
(+14.5 OD); crew board top: T17-S1 97.5%; screenshots in
demo/fortnight/screenshots/ (wall panels 01-04).
