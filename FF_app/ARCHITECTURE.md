# FF_app — Architecture Contract (AUTHORITATIVE)

FF_app is the FOCU5 application built **from scratch** per `../FOCU5/ORCHESTRATION.md`
(adapted: it lives here in `FABLE_FOCUS_REVIEW/FF_app/`, has **no import
dependency on MAX code** — MAX_FOCUS_1 is reference-only for conventions and
mock-data shapes), and must be Tanzu-hostable. Every module below implements
EXACTLY these interfaces; code fences are authoritative. Owner rules OR-1..6,
LB laws, and GG guardrails from `../FOCU5/00_foundation/00_mission_owner_rules_glossary.md`
bind everything. Honesty: all shipped data is SYNTHETIC and labeled
`mock_data: true`; economics rates are placeholders.

## Layout
```
FF_app/
  ARCHITECTURE.md  README.md  BUILD_LOG.md  requirements.txt
  Dockerfile  manifest.yml  .dockerignore  run.py  config.py
  ff/__init__.py
  ff/domain.py                # dataclasses + type aliases (below)
  ff/data/generator.py        # mock fleet generator
  ff/data/loader.py           # fixture load/save (json.gz)
  ff/engine/cpm.py            # CPM forward/backward, slack, priority
  ff/engine/scheduler.py      # deterministic greedy named placement
  ff/engine/validator.py      # V1..V9 checks on a Schedule
  ff/engine/economics.py      # lateness $, controllable split
  ff/services/snapshot.py     # Snapshot assembly + snapshot_id
  ff/services/feasibility.py  # reason-coded readiness
  ff/services/candidates.py   # ranked candidates, decomposed score
  ff/services/points.py       # V1 point engine + shift targets + leaderboard
  ff/services/capacity.py     # $-weighted capacity pressure
  ff/web/app.py               # Flask factory, role session, health
  ff/web/api.py               # /api/v1 blueprint
  ff/web/templates/*.html     # base + 6 role views (no CDN)
  ff/web/static/ff.css  ff/web/static/ff.js
  tests/test_*.py             # see Tests
  data/                       # generated fixtures (gitignored except mini/)
  data/mini/fleet.json.gz     # committed tiny fixture (3 aircraft) for tests
```

## Time model (mirrors MAX conventions)
- `SHIFT_EFFECTIVE={1:460,2:460,3:370}`, `SHIFT_MAX={1:520,2:520,3:430}`,
  `OVERTIME=60`, `NO_START_BUFFER=30`, `UTILIZATION=0.85`.
- Slot = `(day:int, shift:int in {1,2,3})`; `slot_index = day*3 + (shift-1)`.
- Working days: Mon–Fri = day%7 in 0..4 with day 0 = a Monday. **Sunday-night
  rule (OR-3):** shift 3 is plannable on non-working day `d` iff `d+1` is a
  working day.
- `isCritical` ⇔ `cpm_slack <= 0.5`.

## config.py — sectioned constants (exact names)
```python
# §1 SHIFTS: SHIFT_EFFECTIVE, SHIFT_MAX, OVERTIME=60, NO_START_BUFFER=30, UTILIZATION=0.85, SHIFT_LABELS
# §2 CALENDAR: WEEK_STARTS_SUNDAY_NIGHT=True, HORIZON_MIN_DAYS=60
# §3 CPM: CRITICAL_SLACK_MIN=0.5, DEFAULT_GRACE_DAYS=30
# §4 ECONOMICS (PLACEHOLDERS): AIRCRAFT_VALUE_USD=200_000_000, LATENESS_PENALTY_USD_PER_DAY=100_000
# §5 SCORING/POINTS (recalibrated 2026-07-10, GG-2 — see BUILD_LOG): W_CRIT=60, W_URGENCY=25, W_DOWN=45, W_RISK=30, W_SCARCE=20, W_RECOVER=30, P_OOS=40 (all percent-of-effort), EFFORT_CREW_MINUTES_PER_POINT=5, NORM_PCTL_LO=5.0, NORM_PCTL_HI=95.0 (BASE_CAP removed — superseded by the effort term)
# §6 GENERATOR: GEN_AIRCRAFT=50, GEN_TASKS_PER_AIRCRAFT=880 (uniform/test mode only), GEN_TASK_UNIVERSE=6500, GEN_TEAMS=20, GEN_MECHANICS=680, GEN_SEED=20260710
# §7 WEB: DEFAULT_PORT=8080 (Tanzu $PORT wins), SECRET_KEY from env FF_SECRET_KEY (dev fallback ONLY when FF_ENV=dev)
# §8 GAME: REWARDS_ENABLED=False
# All overridable via env FF_<NAME>.
```

## ff/domain.py (exact)
```python
TaskKey = str  # f"{aircraft:04d}-T{n:05d}"
@dataclass class Task: task_id:TaskKey; aircraft:int; name:str; team:str; skill:str;
    duration_minutes:int; mechanics_required:int; earliest_day:int; deadline_day:int;
    predecessors:list[TaskKey]; is_rework:bool=False; is_inspection:bool=False;
    state:str="not_started"  # not_started|in_progress|blocked|done
    parts_eta_day:int|None=None; remaining_minutes:int|None=None
@dataclass class Mechanic: mech_id:str; team:str; shift:int; skills:list[str]  # id f"{team}-S{shift}-M{i:03d}"
@dataclass class Aircraft: aircraft:int; name:str; delivery_deadline_day:int; station:str
@dataclass class Fleet: aircraft:list[Aircraft]; tasks:list[Task]; mechanics:list[Mechanic];
    meta:dict  # {"mock_data":True,"seed":int,"generated_at":str,"schema":1}
@dataclass class Assignment: task_id:TaskKey; day:int; shift:int; start_minute:int; end_minute:int;
    mechanic_ids:list[str]; team:str; skill:str; uses_overtime:bool
@dataclass class Schedule: assignments:dict[TaskKey,Assignment]; unscheduled:dict[TaskKey,str];  # reason code
    stats:dict; meta:dict  # stats keys below
```
`Schedule.stats` MUST contain: `total_tasks, scheduled, unscheduled, fleet_lateness_days,
otd_count, aircraft:[{aircraft,completion_day,deadline_day,lateness_days,on_time}],
makespan_day, wall_seconds, economics (from economics.py), capacity_pressure (from capacity.py)`.
Serialization: `loader.save_json_gz(obj_dict,path)` / `load_json_gz(path)`; dataclasses
have `to_dict()/from_dict()` via helpers in domain.py.

## ff/data/generator.py
`generate_fleet(n_aircraft:int, tasks_per_aircraft:int, n_teams:int, n_mechanics:int, seed:int) -> Fleet`
- Deterministic (`random.Random(seed)`). **Default = pulsed-line maturity mode**
  (owner directive 2026-07-10): aircraft enter at takt and sit at different
  maturity — ~40% LATE_TO_DELIVERY punch lists (ships 1-2 carry exactly 0 and 1
  open tasks; rest 3-120), ~40% POST_FAL residual (120-900), ~20% IN_FACTORY
  ladder up to `task_universe` (default 6,500 → 55k+ open tasks fleet-wide).
  Deadlines per class are calibrated against the MEASURED completion curve
  (fragmentation factor 2.4, calibration run 2026-07-10) so OTD/economics are
  non-trivial: late-delivery late-by-design, post-FAL ~65% on time, in-factory
  takt-staggered ~50%. Uniform mode (explicit `tasks_per_aircraft`) retained
  for tests/mini fixture with ~30% infeasible-tight deadlines.
- Per-aircraft DAG (OR/C12: NO cross-aircraft edges): layered random DAG,
  predecessors only from earlier layers, 0–3 preds/task, acyclic **by construction**;
  ~8% rework tasks (a rework task's predecessor is its parent), ~10% inspection tasks
  (duration 45, gate 1–3 successors).
- Durations 30–480 min (fit in one shift ≤ SHIFT_EFFECTIVE minus buffer; no
  segmentation in v1 — enforce duration ≤ 430), crew 1–3, skills from a pool of 8
  codes; team assignment clusters tasks by aircraft-zone-ish grouping.
- Mechanics spread across teams×shifts with 1–3 skills; every (team,skill) demanded
  by tasks has ≥1 holder on ≥1 shift (solvability guarantee), but scarcity is real
  (some pools thin → delays).
- `run.py generate-data --aircraft 50 --out data/fleet50.json.gz` and
  `--aircraft 3 --tasks-per-aircraft 40 --out data/mini/fleet.json.gz`.

## ff/engine/cpm.py
`compute_cpm(tasks:list[Task]) -> dict[TaskKey, CpmInfo]` where
`CpmInfo = {es:int, lf:int, slack_minutes:float, cpm_priority:float, downstream_count:int}`
— per-aircraft forward/backward pass on the DAG in MINUTES of work content
(day capacity = sum(SHIFT_EFFECTIVE)=1290); slack vs deadline_day*1290;
`cpm_priority` = remaining critical-chain work through the task (higher = more
hangs behind it); `downstream_count` = transitive successor count (memoized
reverse topo). Pure, deterministic.

## ff/engine/scheduler.py — THE CORE
`build_schedule(fleet:Fleet, cpm:dict, start_day:int=0, horizon_days:int=None) -> Schedule`
Deterministic greedy named placement (MAX-style, from scratch):
1. Ready heap keyed `(-cpm_priority, task_id)`; task ready when all preds placed.
2. Slot scan from `max(earliest_day, preds' end)` forward over eligible slots
   (working-day rule + Sunday-night S3; blocked tasks floor at `parts_eta_day`).
3. Pool = mechanics of (team, shift) with the skill (skill "ANY" matches all).
   **Full crew or wait (OR-1):** place ONLY when `mechanics_required` distinct
   qualified mechanics are simultaneously free for the whole duration; NEVER
   short-crew, NEVER borrow across teams (OR-2). Activation quota: at most
   `floor(pool_size*UTILIZATION)` distinct mechanics used per (team,shift,day).
4. Per-mechanic ledger: next-free-minute per (mech,day,shift); per-mechanic load
   cap `SHIFT_EFFECTIVE+OVERTIME`; start ≤ `SHIFT_EFFECTIVE-NO_START_BUFFER`;
   end ≤ `SHIFT_MAX` (uses_overtime if end > SHIFT_EFFECTIVE).
5. Precedence: pred.end (slot,minute) ≤ succ.start; same-slot requires
   `succ.start_minute >= pred.end_minute`.
6. `state=="done"` tasks are excluded (preds satisfied); `in_progress` pinned to
   `start_day` with `remaining_minutes`; unschedulable within horizon →
   `unscheduled[task_id] = "no_crew_within_horizon" | "crew_exceeds_pool" | "no_skill_holder"`.
   NO fictional placements ever.
7. Deterministic: identical fleet+args ⇒ identical schedule (tie-breaks by id).
Target: 50 aircraft / 55,000+ open tasks (pulsed-line maturity mode) in < 60s pure Python (measured: ~3.5s). Horizon defaults workload-scaled (measured 4x fragmentation headroom).

## ff/engine/validator.py
`validate(schedule, fleet) -> {"violations":[{id,task_id,msg}], "summary":{...}, "checks":[...]}`
V1 duration identity (end-start == duration, or remaining for in_progress);
V2 precedence order (incl. same-slot minute rule); V3 working-day/Sunday rule;
V4 shift fit + no-start buffer; V5 full-crew (len(mechanic_ids)==mechanics_required,
all real roster ids, team match, skill match); V6 mechanic non-overlap;
V7 per-mechanic load cap ≤ SHIFT_EFFECTIVE+OVERTIME; V8 activation quota;
V9 parts-ETA floor. `run.py validate <schedule.json.gz> --data <fleet>` exits
non-zero on any violation.

## ff/engine/economics.py
`fleet_economics(stats_aircraft:list, today_day:int) -> dict` — per aircraft:
`penalty_usd = lateness_days*LATENESS_PENALTY_USD_PER_DAY`,
`floor_days = min(max(0, today_day-deadline_day), lateness_days)` (unavoidable),
controllable = rest; fleet rollup `{total_penalty_usd, unavoidable_penalty_usd,
controllable_penalty_usd, late_count}` + `"source":"config-defaults"` (placeholder label).

## ff/services/*
- `snapshot.build_snapshot(fleet, schedule, cpm) -> Snapshot` with
  `snapshot_id = sha256(fleet.meta.seed + schedule hash)[:16]`, freshness stamp.
- `feasibility.evaluate(task_id, snap) -> {"status": "DONE|IN_PROGRESS|READY|WAITING_PREDECESSOR|WAITING_PART|WAITING_CREW",
  "reason_codes":[...], "blocking_task":..., "blocking_team":...}` (deterministic, mirrors scheduler rules).
- `candidates.rank(snap, scope:dict, limit=25) -> [Candidate]`; Candidate carries
  `task_id, rank, score, components:{name:{raw,weight,points}}, reason_codes, explanation`
  (factors: critical/urgency/downstream/risk$/scarcity/recovery — §5 weights; frozen
  per-snapshot robust-percentile normalizers [recalibrated 2026-07-10: raw min-max
  measured to flatten at 55k-task scale]; deterministic explanation template).
- `points.score_task(task_id, snap) -> ScoreBreakdown` (SAME factor module as
  candidates — one implementation, two weight profiles; GG-2 recalibration:
  score = effort_term x value_multiplier, effort = crew-minutes /
  EFFORT_CREW_MINUTES_PER_POINT shown as a named component, each factor
  contribution = effort x weight% x normalized factor);
  `points.shift_report(snap, team, day, shift) -> {goal, earned, attainment, difficulty,
  value_efficiency, breakdown:[...]}` (goal = Σ points of planned slice; earned from
  `state=="done"`); `points.leaderboard(snap, day) -> [{team, attainment, efficiency}]`
  ranked by attainment+efficiency, never raw points (GG-2).
- `capacity.pressure(snap) -> {"pools":[{team,shift,wait_days,dollar_days,aircraft_touched}],...}`
  — walk each late aircraft's completion chain, attribute capacity-wait to (team,shift),
  $-weight by marginal penalty (MAX doctrine, simplified).

## ff/web — Flask, Tanzu-ready
- `create_app()` factory; `PORT` env (Tanzu) > `FF_PORT` > 8080; `FF_SECRET_KEY`
  required unless `FF_ENV=dev`; gunicorn entry `ff.web.app:create_app()`.
- Health: `GET /healthz` (liveness) and `GET /readyz` (fleet+schedule loaded).
- Demo auth: `POST /login` picks persona+scope (mechanic id / team / all) stored in
  session; server-side scope filtering on every API (RS rules); `GET /logout`.
- Views (server-rendered, no CDN, one base.html): `/mechanic` My Day+ (my tasks,
  readiness chips, why-points chips), `/lead` Crew Board (team-shift board, blocked
  reasons, next-best candidates), `/flm` Shift Command (attainment rings S1–S3, cause
  list, leaderboard), `/super` Position Control (station progress, capacity pressure,
  escalations), `/director` Analytics (economics trend table, OTD, what-if note),
  `/vp` Risk & Commitments ($ exposure w/ placeholder banner, late board, data-trust
  panel). Every page: mock-data banner + snapshot freshness stamp (OR-5).
- `/api/v1`: `GET /state`, `GET /tasks/<id>` (+feasibility), `GET /candidates?team=&shift=&limit=`,
  `GET /economics`, `GET /capacity`, `GET /points/shift?team=&day=&shift=`,
  `GET /points/leaderboard`, `POST /actuals` `{task_id,state,remaining_minutes?}`
  (single write-path OR-6: updates in-memory fleet state + persists
  `data/actuals.json`; role-gated), `POST /replan` (re-runs scheduler on current
  state; single-flight lock). JSON error envelope `{error,code}`.
- App state: loads fleet fixture (`FF_DATA` env, default `data/fleet50.json.gz`),
  applies `data/actuals.json` if present, runs cpm+scheduler at boot (or loads
  `FF_SCHEDULE` if given), builds snapshot; `POST /replan` rebuilds.

## Tanzu packaging
- `Dockerfile`: python:3.11-slim, non-root user, `pip install -r requirements.txt`,
  `EXPOSE 8080`, CMD `gunicorn -b 0.0.0.0:${PORT:-8080} 'ff.web.app:create_app()'`.
- `manifest.yml` (TAS/cf push): name ff-app, python buildpack path fallback,
  health-check-type http endpoint /healthz, env FF_ENV=prod.
- 12-factor: all config via env; SQLite/JSON state on ephemeral disk documented as
  demo-only; no absolute paths; logs to stdout.

## requirements.txt
`flask`, `gunicorn` (runtime); `pytest` (dev, requirements-dev.txt). Nothing else.

## Tests (pytest; run against data/mini fixture, generated on the fly if absent)
- test_generator.py: determinism (same seed ⇒ identical), DAG acyclic, no cross-
  aircraft edges, solvability (every demanded (team,skill) has a holder).
- test_cpm.py: slack/priority on a hand-built 5-task DAG; critical flag.
- test_scheduler.py: precedence order honored; **full-crew-or-wait tripwire** (2-crew
  task waits for both, never 1); no-borrow tripwire; Sunday-night S3 plannable
  tripwire (quote OR-3); activation quota; determinism (two runs identical).
- test_validator.py: clean schedule ⇒ 0 violations; injected violations detected
  (one per V-check).
- test_points.py: ordering invariant — recovery-boosted critical task outscores
  10× trivial tasks; excused/goal math; leaderboard never ranks by raw points.
- test_api.py: app boots; healthz/readyz; scope enforcement (mechanic role cannot
  read another team's candidates); actuals write→replan drops done task.
- test_economics.py: floor split math.
Gate: `python run.py gates` = generate mini → schedule → validate → pytest -q.

## run.py subcommands
`generate-data`, `run-schedule --data X --out Y`, `validate SCHED --data X`,
`serve [--port]`, `gates`. All deterministic flags; `--seed`.
```

## INCREMENT 2 (post-PR#3): commitments + disruption/excusal (AUTHORITATIVE ADDENDUM)

### config.py §9 COMMITMENTS
`COMMIT_HYSTERESIS_DAYS=2, COMMIT_PERSISTENCE_RUNS=2, COMMIT_WORSEN_IMMEDIATE_DAYS=7`
(env-overridable like all constants).

### ff/services/commitments.py
`update_commitments(state:dict, projections:list[dict], run_id:str, evidence:dict) -> tuple[dict, list[dict]]`
- `projections` rows: `{aircraft:int, projected_day:int}` (from Schedule.stats.aircraft
  completion_day). `state` shape: `{"aircraft": {str(ac): {"committed_day": int,
  "pending": {"target": int, "streak": int} | None}}, "log": [...]}`.
- Rules (MAX §11, OR-4 — tripwire-tested): (1) first sighting commits immediately,
  reason "initial commitment"; (2) `abs(delta) < COMMIT_HYSTERESIS_DAYS` = jitter →
  hold, clear pending; (3) `delta >= COMMIT_WORSEN_IMMEDIATE_DAYS` → recommit at once
  with evidence ("bad news fast"); (4) all else (small slips AND improvements) needs a
  pending streak of `COMMIT_PERSISTENCE_RUNS` consecutive runs at the same target
  (±1 day) — reason gains " (sustained N replans)"; pending resets when target moves.
- Log rows: `{"run", "aircraft", "old", "new", "delta_days", "reason"}`. Slip reasons
  concatenate evidence causes from keys `rework_inserted`, `execution_shortfall`,
  `blocked_parts`, `capacity` (fallback "global replan shift"); improvements =
  "schedule improvement".
- Persistence: `data/commitments.json` atomic write (same pattern as actuals);
  loaded at boot; updated inside every /replan (single-flight lock already held).
- Snapshot gains `commitments` block: `{committed:[{aircraft, committed_day,
  projected_day, deadline_day, delta}], changes:[last 50 log rows], run_id}`.

### ff/services/disruption.py
- `CAUSES` (8, verbatim ids): `LATE_PART`+, `CROSS_TEAM_PREDECESSOR`+,
  `SAME_TEAM_PREDECESSOR`-, `CAPACITY_SHORTAGE`+, `SKILL_SHORTAGE`+,
  `DURATION_OVERRUN`-, `REWORK_INJECTION`+, `PLAN_CHURN`-; `EXCUSABLE` = the 5 `+`.
- `attribute(snap) -> list[ExcusalRecord]` where ExcusalRecord =
  `{task_id, team, day, shift, cause, excusable, evidence:str, source:"auto"}`.
  Automatic attribution (deterministic): blocked task w/ parts_eta → LATE_PART
  ("parts ETA day N"); feasibility WAITING_PREDECESSOR with blocking_team != team →
  CROSS_TEAM_PREDECESSOR (evidence names blocking task+team), same team →
  SAME_TEAM_PREDECESSOR; unscheduled 'no_crew_within_horizon' → CAPACITY_SHORTAGE;
  'no_skill_holder'/'crew_exceeds_pool' → SKILL_SHORTAGE; is_rework not_started →
  REWORK_INJECTION for the RECEIVING team.
- `merged_excusals(snap, manual:list) -> dict[(team,day,shift), list]` — manual
  captures EXTEND auto attribution (never replace); duplicates (same task+cause)
  collapse to auto.

### Lead capture (OR-6-adjacent write, FOCU5-local)
`POST /api/v1/excusals` `{task_id, cause, notes?}` — role lead+ scoped to own team;
cause must be in CAUSES; notes REQUIRED for `SAME_TEAM_PREDECESSOR` and
`DURATION_OVERRUN`; persists `data/excusals.json` (atomic, append) with
`{..., source:"manual", entered_by:<session id>, ts}`; 400 unknown task/cause,
403 out-of-scope team. `GET /api/v1/excusals?team=&day=&shift=` role-scoped.

### points.shift_report — GG-3 wiring
Excused planned tasks (excusable causes only) leave the GOAL DENOMINATOR; report
gains `excused_points`, `excusals:[{task_id, cause, evidence, source}]` — visible,
never silent. Non-excusable causes stay in the denominator.

### Views
- vp.html: committed-vs-projected board (committed_day vs projected_day vs deadline,
  delta badge) + change log with reasons verbatim.
- lead.html: blocked/waiting rows gain cause chip + "capture excusal" control
  (cause dropdown + notes; posts to /api/v1/excusals).
- flm.html: cause pareto (merged excusals by cause, excusable split shown).

### Tests (tripwires, docstrings quote the rule)
test_commitments.py: initial commit; 1-day jitter held; 9-day slip immediate w/
evidence in reason; 3-day slip needs 2 runs ("sustained 2 replans"); improvement
also needs persistence; pending resets on target move; log accumulates.
test_disruption.py: each auto-attribution rule; merged extend-not-replace;
excusable set exact. test_points (extend): excused task leaves denominator;
non-excusable stays. test_api (extend): excusal capture authz (mechanic 403,
lead wrong-team 403), notes-required 400, replan updates commitments block.

## INCREMENT 3: GAMES progression layer (AUTHORITATIVE ADDENDUM, 2026-07-10)

X2/X3 canon (`../FOCU5/04_games/02`+`03`) adapted; GG-1..8 + PSY law bind.

### config.py §10 GAMES PROGRESSION (placeholders labeled, env-overridable)
`KEYSTONE_MIN_UNLOCKED=3, FIREBREAK_MIN_UNLOCKED=5, CLEAN_SWEEP_MIN_CRITICAL=1,
GOAL_MILESTONES=(25,50,75,100), LEVEL_CURVE=(0,250,750,1500,3000,6000,12000,24000),
NEEDS_SUPPORT_ATTAINMENT=0.5, BADGE_EARN_BUDGET_PER_TEAM_WEEK={4 badges},
BADGE_INFLATION_ALERT_FACTOR=3.0` (tuple constants parsed from CSV strings).

### ff/services/progression.py
- `derive_events(prev_snapshot_summary, snap) -> [GameEvent]` — APPEND-ONLY
  derived events (GG-1: no user-generated events, ever; `prev=None` =
  baseline, no events). Types (verbatim): `completion-credited` (+recovery
  flag, points_delta from score_task), `unlock` (last-gate attribution over
  the per-aircraft DAG), `keystone-cleared` (single gate freeing >=
  KEYSTONE_MIN_UNLOCKED), `recovery-moment` (ONLY from the commitments
  change log, current run, delta_days<0 — PSY-8: emotional peak = economic
  peak), `goal-crossed` (§10 milestones, fires once per crossing),
  `streak-extended`, `badge-earned`. Event id =
  `f"{snapshot_id}:{type}:{subject_key}"` (idempotent re-derivation);
  evidence never empty (GG-4); mock_data label on every event (OR-5).
- `snapshot_summary(snap)`; `slot_stats(snap)` one-pass per-slot aggregate
  MIRRORING points.shift_report GG-3 math (equality tripwire-tested).
- `compute_streak(snap, team, upto=None)` — consecutive shifts attainment
  >= 1.0; excused-heavy shifts (goal 0 after excusals) PAUSE never break
  (GG-3, named test); unexcused sub-100% breaks.
- `evaluate_badges(events, snap) -> (new_badges, earn_rate_report)` — the 4
  canon badges: Firebreak (keystone >= FIREBREAK_MIN_UNLOCKED), Recovery
  (completion on aircraft late/blocked entering shift), Clean Sweep (100%
  planned critical done, >=1 required — no vacuous 100%), Flow Keeper
  (week with zero self-caused SAME_TEAM_PREDECESSOR/DURATION_OVERRUN;
  absorbed causes never count). Every badge stores `earning_event_ids`
  (GG-4); dedupe by badge_key; earn-rate counters vs §10 budgets = the
  badge-inflation monitor (alert surfaced, never silent).
- `team_level(events, team)` — XP = pure fold over completion-credited
  events (PSY-4: no decay path, no purchase path); §10 LEVEL_CURVE.
- `build_recap(team, day, shift, snap, events=None)` — deterministic recap
  card (attainment, top plays BY POINT VALUE, badges, streak, recovery
  moments, points banked, excused summary); NO LLM — LB-8 fallback content
  IS this (`generated_by: "deterministic"`).
- Persistence: `data/game_events.jsonl` (env `FF_GAME_EVENTS`), append-only
  log, atomic tmp+fsync+replace; `load_events`/`append_events` (idempotent
  by event_id)/`persist_events`; `advance(prev_summary, log, snap)` is the
  web layer's single entry (boot + every replan, inside the replan lock).

### points.leaderboard (upgrade — GG-2 re-asserted)
Rows gain `difficulty` (the existing per-team difficulty index; FINAL
tie-breaker after attainment+efficiency so equal execution favors the
harder plan), `needs_support` (goal>0 and attainment <
NEEDS_SUPPORT_ATTAINMENT) and `support` = `{framing: "needs support",
top_causes: [{cause, count}]}` (PSY-5: excusal context attached, never
shaming). Raw goal/earned point totals still NEVER appear in rows.

### API (ff/web/api.py — ALL read-only, GG-1; role-scoped like every route)
`GET /api/v1/progression/team/<team>` (streak, badges, level, earn_rates;
404 unknown/403 out-of-scope), `GET /api/v1/recap?team=&day=&shift=`,
`GET /api/v1/progression/events?limit=` (newest first; scoped roles see own
teams + fleet-level `team==""` rows). No POST/PUT/DELETE exists (route
audit tested).

### Views (house style, no CDN; existing sections untouched)
flm.html: progression strip (streak w/ GG-3 pause count, level/XP, badge
chips w/ evidence + earning events) + leaderboard difficulty column +
needs-support framing. mechanic.html: "My contribution" `<details>` panel —
SELF-SCOPE ONLY, collapsed by default (§G8 team-first privacy; individual
stats only for the logged-in mechanic). lead.html: S1–S3 deterministic
shift recap cards (LB-8 note rendered).

### Tests (tests/test_progression.py — docstrings quote the rules)
Each badge rule (+ flow-keeper self-caused forfeit + earn-rate alert);
`test_streak_pauses_on_excused_shift_never_breaks` (canon fixture 100%,
100%, excused-miss, 100% => streak 3 alive); event determinism/idempotence;
recovery-moment only from the change log's current improving rows;
goal-crossed once per milestone; slot_stats==shift_report drift tripwire;
recap content + determinism; leaderboard never-raw-points re-assert +
needs-support; API scoping (403/404/405 + GET-only route audit + end-to-end
actuals->replan->event). test_points/test_api updated for the new row
fields and the FF_GAME_EVENTS redirect.

## INCREMENT 4: constrained LLM layer (AUTHORITATIVE ADDENDUM, 2026-07-10)

Wave-5 canon (`../FOCU5/02_llm/01`+`02`+`04`) adapted; LB-1..LB-10 bind.
WATTS `bcai_client.py` is the transport reference; its GROUNDING §7
BANNED patterns (TLS-verification-off + warning suppression, hardcoded
endpoint/model/ids, in-band ERROR strings returned as content) are
removed, with a repo-wide grep tripwire test.

### config.py §11 LLM (env-overridable FF_LLM_*)
`LLM_PROVIDER="mock"` (| recorded | bcai | none), `LLM_ENDPOINT` (-TEST
host, loud comment), `LLM_MODEL="gpt-5.4-mini"`,
`LLM_USE_CASE_ID="focu5-use-case.unprovisioned"` (NEVER the WATTS id),
`LLM_CONVERSATION_SOURCE`, `LLM_INFO_TYPES` (CSV), `LLM_TIMEOUT_S=120`,
`LLM_PING_TIMEOUT_S=30`, `LLM_RETRIES=2` + `LLM_RETRY_BACKOFF_S=1.0`
(transport budget), `LLM_SCHEMA_RETRIES=2` (separate parse-validate
budget), `LLM_TEMPERATURE=0.0`, `LLM_MAX_TOKENS`, `LLM_CA_BUNDLE`
(verification ALWAYS on), `LLM_LOG_POLICY="metadata_only"`,
`LLM_AUDIT_PATH="data/llm_audit.jsonl"`, `CLAIM_REL_TOL=0.005`,
`UNTRUSTED_TEXT_MAX_LEN=500`.

### ff/llm/provider.py
`LLMRequest` (messages + template id/version — LB-7 identity);
`LLMProvider` protocol: `complete_structured(request, response_schema)
-> dict` is the ONLY entry (no complete_text, LB-4); typed taxonomy
`LLMError/LLMAuthError/LLMTimeoutError/LLMProtocolError/LLMSchemaError/
LLMProviderError` (LB-8). Shared L1-6 loop: parse -> validate ->
bounded re-prompt (`LLM_SCHEMA_RETRIES`) -> `LLMSchemaError`.
`MockLLMProvider` — deterministic, offline, GROUNDED (reads the DATA
block back; narrates only supplied ids/numbers); failure modes
malformed_then_valid / always_malformed / timeout / auth / error_inband.
`RecordedResponseProvider` — committed cassettes `ff/llm/cassettes/*.json`
keyed (template_id, version, input_hash); header/PAT-free by design;
record mode = delegate write-through. `BCAIChatGPTProvider` — WATTS
envelope verbatim (`conversation_mode:["default"]`, `stream:"false"`
STRING, `Authorization: basic <raw PAT>` lowercase-not-Base64, NDJSON
LAST-line parse -> `choices[0].message.content`), ALL fields from
config §11; TLS verified via `ssl.create_default_context` (stdlib
urllib — no requests dep); in-band `ERROR:` -> typed exceptions, never
content; PAT via per-call `pat_supplier` (server session only, LB-9);
transport retries on timeout/connection ONLY; `validate_token` ping.

### ff/llm/prompts.py
Versioned `PromptTemplate` assets (id, semver, response schema,
narrative_fields/id_fields validator declarations): `explain_candidates`
@1.0.0 (narrates a SUPPLIED ranked candidate list) and
`shift_recap_narrative`@1.0.0 (garnishes the deterministic recap dict;
GG-6 mission-control tone, no exclamation marks). Template laws:
structured data ONLY as compact JSON inside one `<<<DATA context:json …
DATA>>>` block; the never-follow-instructions preamble + prohibited-
actions paragraph verbatim on every render; schema embedded from the
LIVE dict (schema-in-prompt); `data_basis`+`confidence` REQUIRED in
every schema (LB-10, import-time assertion). `sanitize_untrusted_text`
is the LB-6 choke point: length cap (marked truncation),
delimiter-collision escape (middle dot breaks `<<<DATA`/`DATA>>>`),
control-phrase/protocol-marker neutralization by VISIBLE
`[data]…[/data]` bracketing (never deletion). Deterministic context
builders `build_explain_candidates_context` / `build_shift_recap_context`
reshape engine output; they compute nothing (L2-4).

### ff/llm/validators.py
Post-response pipeline (fail-closed, never raises): `schema` (strict —
additionalProperties rejected) -> `candidate_whitelist` (LB-3: one id
outside the presented set = hard reject, incl. id-shaped tokens in
narrative text) -> `numeric_grounding` (LB-4: every narrative number
must match a supplied context metric within `CLAIM_REL_TOL`; percent
form of a [0,1.5] fraction is the one allowed derived form;
undecidable = reject) -> `uncertainty_presence` (LB-10 substance:
basis not "N/A", confidence in [0,1]). `audit_append` -> fsync'd JSONL
rows (template id+version, input hash, output hash, per-stage verdicts,
ts, mock_data — LB-7; bodies never logged, hashes only).

### API wiring (ff/web/api.py + app.py)
`GET /api/v1/explain/candidates?team=&shift=&limit=` — THE one LLM
surface (LB-1/LB-5: read-only, no tools, no other route). Candidate set
via `_scoped_candidates` (ONE implementation shared with
`GET /candidates` so the LB-3 whitelist can never drift). Deterministic
explanation ALWAYS built (the LB-8 fallback = the existing templated
candidate explanations); if `state["llm_provider"]` is configured
(factory `_build_llm_provider`: env FF_LLM_PROVIDER > config §11;
default mock; bcai wires `pat_supplier` to `session["bcai_token"]`) the
validated narrative is attached and `source` flips
`'deterministic'`->`'llm-validated'`; ANY provider/validation failure
degrades SILENTLY (200 + deterministic), audited either way to
`state["llm_audit_path"]` (env FF_LLM_AUDIT > data/llm_audit.jsonl,
gitignored).

### Tests (tests/test_llm.py — docstrings quote the laws)
Repo-wide TLS-bypass grep tripwire; RED-TEAM corpus RT-001..RT-010
(instruction-shaped task names, TOOL_CALL/tool_call fences, DATA
delimiter escapes, script/img/md-link smuggling, role-tag look-alikes)
— neutralize visibly, never delete; validator hard rejects (invented
ids in fields AND narrative, ungrounded `$4.2M`, 0.5%-tolerance near
miss, percent derived form allowed); LB-10 substance; mock determinism
+ both bounded retry budgets + typed failures; BCAI envelope golden
(synthetic config, PAT header-only), NDJSON last-line, in-band ERROR
taxonomy, auth-never-retried, backoff sequence, zero-network-on-no-PAT;
cassette record/replay determinism + the COMMITTED fixture cassette +
missing-cassette typed failure; audit row shape; API happy/degrade/
no-provider/401/403/405 + single-LLM-route audit; recap template round
trip (GG-6 no-exclamation check).

## INCREMENT 5: scheduler-side commitment defense (OR-4) — AUTHORITATIVE ADDENDUM

Motivation (measured): digital-week slot stability 12.3-21.7% vs MAX's 41-59%
fleet-wide / 64-73% in the defended band. MAX's mechanism (config §10,
GROUNDING §1): committed-first dispatch + incumbent slot/mechanic defense
inside a 3-day horizon. Replicate it, then MEASURE the A/B on the digital week.

### config.py §12 COMMITMENT
`COMMITMENT_ENABLED=True, COMMITMENT_HORIZON_DAYS=3` (env-overridable).

### ff/engine/scheduler.py
`build_schedule(fleet, cpm, start_day=0, horizon_days=None, incumbent=None)`
- `incumbent`: dict task_id -> {"day","shift","start_minute","mechanic_ids"}
  built from a prior Schedule's assignments (helper
  `incumbent_from_schedule(schedule) -> dict` exported by scheduler.py).
- In-horizon set: incumbent entries with `start_day <= day < start_day +
  COMMITMENT_HORIZON_DAYS` when COMMITMENT_ENABLED.
- **Committed-first dispatch:** heap key becomes
  `(0 if in-horizon-incumbent else 1, slack, -priority, task_id)` — a ready
  committed task always pops before new work (MAX rule; tripwire).
- **Slot defense:** a surviving in-horizon task tries its incumbent
  (day, shift) FIRST, at or after its precedence/parts floor — the floor
  always wins ("precedence is physics; stickiness is not"); on success the
  assignment records `kept_incumbent: True` (Assignment gains this optional
  field, default False, serialized).
- **Mechanic preference:** crew selection prefers incumbent `mechanic_ids`
  when qualified+free (sort key: incumbent members first, then earliest-free,
  then id). Two-attempt rule: if preferring the incumbent crew pushes the
  start beyond the slot's feasible window, retry free-choice — crew
  continuity must never cost slot continuity (MAX rule; test).
- Beyond horizon: free repack (left-pull preserved). `incumbent=None`
  must be BYTE-IDENTICAL to today's behavior (tripwire test).
- Stats: `commitment_in_horizon` (count offered), `commitment_kept`
  (slot kept), `commitment_mech_kept` (>=1 incumbent crew member kept).

### Wiring
- `ff/web/app.py` /replan + boot: pass `incumbent_from_schedule(previous
  schedule)` when one exists (in-memory previous; else none).
- `ff/sim/digital_week.py`: thread the incumbent from each round's prior
  schedule into the replan; record `slot_stable_pct`/`mech_stable_pct` as
  today, plus new `in_horizon_slot_stable_pct` (survivors whose incumbent
  slot was in-horizon and kept).
- `run.py run-schedule` gains `--incumbent <schedule.json.gz>`.

### Tests (tripwires; quote OR-4)
in-horizon task keeps incumbent slot even when an earlier slot is free;
beyond-horizon repacks left; infeasible incumbent slides with predecessor
(floor wins); incumbent mechanic preferred over equally-free peer;
crew-continuity-never-costs-slot (two-attempt); committed-first beats a
higher-priority NEW task for the last seat; incumbent=None byte-identical;
validator V1-V9 still clean with commitments active.

### Acceptance gate (measured, recorded in BUILD_LOG)
Digital-week A/B (same seed, 9 rounds): incumbent-threading OFF vs ON —
report fleet slot%/mech% and in-horizon slot% both arms; expect a large
in-horizon improvement (MAX reference: 64-73%); report lateness/OTD deltas
honestly (stability must not silently cost lateness — if it does, report
the tradeoff, don't hide it). Full pytest + run.py gates + points_gate +
bench all green.

## INCREMENT 6: the real FOCUS Flask UI on FF_app intelligence — AUTHORITATIVE ADDENDUM

Owner feedback 2026-07-11: FF_app's minimal role views are not usable for
mechanics. Adopt the July-2026 FOCUS Flask dashboard (MAX/web_app_flask —
the 17-tab dual-shell UI mechanics already know) as FF_app's primary UI,
fed by FF_app's engine through a max_v1-compatible envelope.

### Part A — envelope exporter (ff/export/envelope.py)
`export_envelope(fleet, schedule, cpm, snapshot, out_dir) -> path` writing
`max_v1_{YYYYMMDD}_S{shift}_{HHMMSS}_UTC.json.gz` (atomic tmp+replace),
schema-compatible with MAX/web_app_flask/src/max_adapter.py (verify against
the adapter SOURCE, field by field — GROUNDING section 2 is the map):
- per-task: taskId, task_key, soi (task name-derived job id), product
  ("AC-%04d"), line_number (=aircraft), day/shift/start_minute/end_minute,
  duration, duration_minutes, startTime/endTime ISO (S1 06:00 S2 14:30
  S3 22:30 wall starts), team, teams, teamSkill, skill, resource_team,
  mechanics, mechanic_id, mechanicIds, state, type
  (Rework|Inspection|Production), workGroup, is_inspection, isReworkTask,
  isCritical (cpm_slack<=0.5), cpm_slack, cpm_priority, deadlineDay,
  deadlineSource, csDeadlineDay, ecdDay/partsEta (blocked), isLatePartTask,
  isFallback=False, crewShortfall=0, placedBy="greedy_named",
  uses_overtime, dependencies (predecessor taskIds), superintendent
  (group id from R0 org mapping "G1".."G4"), cs (station), lane=0,
  is_unlimited_capacity=False, is_duration_segment=False.
- unscheduled tasks exported with day=None handling per adapter tolerance
  (verify what the adapter does; if intolerant, export at horizon-end with
  isFallback=True + crewShortfall — the HONEST C20-style flag).
- top-level: scenario_id, name ("FF_app Fable Schedule"), metadata
  {schedule_label, reference_date, schema_version:2, mock_data:true, stats},
  tasks, mechanic_timelines (dict keyed by mech id — the MAX-detection
  signal), teamCapacities {total_mechanics, mechanics_by_shift, skills},
  staffing_requirements/utilization ("{team}|S{s}|D{d}"), products,
  aircraft_status, summary, predecessors_map, successors_map, otd,
  workingDays, todaySlot, shift_number, work_day, shift_label,
  aircraft_line_numbers.
- metadata.stats: economics (per_aircraft + fleet, source config-defaults),
  capacity_pressure (pools+linesAnalyzed), projection (committed[]/
  earlyFlow[]/delivered[]/changes[] built from ff commitments block +
  aircraft stats; stationFeed:false), final_lateness, otd, commitment_kept,
  commitment_in_horizon.
- CLI: run.py export-envelope --data F --schedule S --out-dir OUT; also
  called automatically at the end of run-schedule and /replan.

### Part B — vendor the Flask app (FF_app/web_flask/)
Copy MAX/web_app_flask -> FF_app/web_flask VERBATIM (templates, static,
src/, run.py), then the MINIMAL diffs (each documented in a
VENDOR_CHANGES.md): (1) SCHEDULES_DIR default -> FF_app/outputs/schedules
(env MAX_SCHEDULES_DIR still wins); (2) strip/neutralize anything importing
the MAX engine package if present (check_imports-style scan; the app is
envelope-driven so expected diffs are path-only); (3) port default 5000 kept.
Do NOT restyle, do NOT rename tabs, do NOT touch the 17 views' behavior.
Acceptance: app boots on the FF envelope and ALL 17 tabs render with data
(Team Lead priority list populated, My Day per-BEMS day, Shift Book,
Management delivery cards, Commitments/Economics/Capacity insight tabs fed
by the FF stats blocks).

### Part C — bolster (new insight tabs, BOTH shells, house pattern)
Following the established recipe (blueprint + one partial included in both
shells + nav buttons): (1) "Arena" tab — FF points shift report +
leaderboard + aircraft health (reads FF_app API); (2) "Progression" tab —
streaks/badges/recap cards; (3) "Excusals" — lead capture (writes FF_app
/api/v1/excusals); (4) "Explain" — LLM-validated candidate explanation
(source-labeled, deterministic fallback). (5) MY DAY WRITE-BACK: add
tap-to-report (in_progress/done/blocked) posting to FF_app /api/v1/actuals
then triggering /replan + envelope re-export + POST /api/refresh — the
closed loop. FF_app web (ff/web) keeps serving as the API backend on 8080;
web_flask talks to it via FF_API_BASE env (default http://127.0.0.1:8080).
Existing 17 views stay untouched (owner directive).

### Tests/gates
tests/test_envelope_export.py: adapter-compat contract — load the exported
envelope THROUGH the vendored src/max_adapter.py (sys.path trick) and
assert: is_max_envelope detected, adapt succeeds, task count matches,
priority ranks assigned, products/aircraft_status coherent, stats blocks
present. Existing suite stays green; run.py gates extended with the
export step. Serve smoke: boot BOTH apps, curl the Flask /api/scenario/
3stage and one insight endpoint, screenshot-ready.
