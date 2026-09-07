# FF_app — FOCU5 (from scratch)

A self-contained, Tanzu-hostable Flask application for 50-aircraft
mechanic/resource scheduling: precedence-DAG adherence (CPM), a
deterministic greedy named-placement scheduler, role-scoped views, a
points/GAMES layer (effort-weighted GG-2 scoring, streaks/badges/levels/
recaps), committed delivery dates with hysteresis + disruption/excusal
capture, and a constrained read-only LLM explanation surface (LB-1..10;
deterministic fallback always). The contract is
[`ARCHITECTURE.md`](ARCHITECTURE.md) — its code fences are authoritative.

> **Honesty note (OR-5):** all shipped data is **SYNTHETIC** and labeled
> `mock_data: true`. The economics rates in `config.py` §4 are
> **placeholders** (`"source": "config-defaults"`), not negotiated terms.
> No optimality claims are made anywhere.

Owner rules baked into the engine (never regress):

- **OR-1** full crew or wait — a task is never short-crewed.
- **OR-2** no borrowing mechanics across teams.
- **OR-3** the week's 3rd shift begins Sunday night (S3 on a non-working
  day `d` is plannable iff `d+1` is a working day).
- **OR-4** committed dates move on hysteresis: jitter held, bad news fast,
  sustained-streak recommits (config §9); the scheduler DEFENDS in-horizon
  commitments (config §12, INCREMENT 5) — committed-first dispatch +
  incumbent slot/crew defense inside 3 days (measured digital-week
  in-horizon slot stability 37.7% -> 92.0% mean, synthetic fleet50).
- **OR-5** mock-data and placeholder-economics labeling everywhere.
- **OR-6** `POST /api/v1/actuals` is the single write-path into task state.

## Quickstart

```bash
cd FF_app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Full quality gate: mini fixture -> schedule -> validate -> pytest -q
python run.py gates

# Generate the working dataset (50 aircraft, deterministic seed)
python run.py generate-data --aircraft 50 --out data/fleet50.json.gz

# Schedule it and validate the result (V1..V9)
python run.py run-schedule --data data/fleet50.json.gz --out data/schedule50.json.gz
python run.py validate data/schedule50.json.gz --data data/fleet50.json.gz

# Acceptance tooling (all deterministic, synthetic mock data)
python tools/points_gate.py --data data/fleet50.json.gz   # GG-2 flow-vs-chaser gate
python tools/bench.py                                     # 6-scenario benchmark suite
python tools/sim_week.py --data data/fleet50.json.gz --rounds 9

# Serve the dashboard (dev)
FF_ENV=dev python run.py serve --port 8080
# then visit http://localhost:8080/ — POST /login picks a persona/scope
```

Environment levers: every constant in `config.py` is overridable via
`FF_<NAME>` (e.g. `FF_OVERTIME=90`, `FF_GEN_SEED=1`). `FF_SECRET_KEY` is
required unless `FF_ENV=dev`. `FF_DATA` selects the fleet fixture
(default `data/fleet50.json.gz`); `FF_SCHEDULE` loads a precomputed
schedule instead of scheduling at boot.

## Docker

```bash
docker build -t ff-app .
docker run --rm -p 8080:8080 -e FF_ENV=dev ff-app
curl http://localhost:8080/healthz
```

## Tanzu / Cloud Foundry

```bash
cf push                       # uses manifest.yml (http health check on /healthz)
cf set-env ff-app FF_SECRET_KEY "$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
cf restage ff-app
```

The platform-injected `PORT` always wins (`PORT` > `FF_PORT` > `--port` >
`8080`). Config is env-only; JSON state on ephemeral disk is demo-only.

## Layout

| Path                        | What it is                                        |
|-----------------------------|---------------------------------------------------|
| `config.py`                 | §1–§11 constants, all env-overridable (`FF_<NAME>`) |
| `run.py`                    | CLI: generate-data / run-schedule / validate / serve / gates |
| `ff/domain.py`              | dataclasses + serde helpers + time/OR-3 helpers   |
| `ff/data/generator.py`      | deterministic mock fleet generator (`mock_data: true`) |
| `ff/data/loader.py`         | json.gz fixture load/save                         |
| `ff/engine/cpm.py`          | CPM forward/backward, slack, priority             |
| `ff/engine/scheduler.py`    | deterministic greedy named placement (the core)   |
| `ff/engine/validator.py`    | V1..V9 checks on a Schedule                       |
| `ff/engine/economics.py`    | lateness $, controllable/unavoidable split (placeholder rates) |
| `ff/services/snapshot.py`   | Snapshot assembly + `snapshot_id`                 |
| `ff/services/feasibility.py`| reason-coded readiness                            |
| `ff/services/candidates.py` | ranked candidates with decomposed scores          |
| `ff/services/points.py`     | effort-weighted point engine + shift targets + leaderboard (GG-2/GG-3) |
| `ff/services/capacity.py`   | $-weighted capacity pressure                      |
| `ff/services/commitments.py`| committed dates with hysteresis (OR-4, config §9) |
| `ff/services/disruption.py` | disruption causes + auto attribution + excusal merge |
| `ff/services/progression.py`| GAMES progression: events/streaks/badges/levels/recaps (GG-1..8) |
| `ff/llm/`                   | constrained LLM layer: providers/prompts/validators (LB-1..10) |
| `ff/bench/`, `benchmarks/`  | scenario mutation recipes + property-based expected bounds |
| `ff/sim/`                   | digital-week simulator (execution policies)       |
| `ff/web/app.py`             | Flask factory, role sessions, `/healthz` `/readyz` |
| `ff/web/api.py`             | `/api/v1` blueprint (incl. the OR-6 actuals write-path, excusals, progression, explain) |
| `ff/web/templates/`         | base + 6 role views (no CDN)                      |
| `tools/`                    | `bench.py` (6-scenario suite), `points_gate.py` (GG-2 gate), `sim_week.py` |
| `tests/`                    | pytest suite (runs against `data/mini`)           |
| `data/`                     | generated fixtures (gitignored except `mini/`)    |
| `data/mini/fleet.json.gz`   | committed tiny fixture (3 aircraft) for tests     |

## Development

- Pure Python 3.11: stdlib + `flask`/`gunicorn` only (`pytest` dev-only).
- Deterministic everywhere: seeded `random.Random`, sorted iteration,
  tie-breaks by id. Identical inputs + flags ⇒ identical outputs.
- Before committing: `python run.py gates` must pass.
- Log decisions in `BUILD_LOG.md`.
