# Operational API

Sign in through `/sign-in`. Browser requests use the signed `astra_session` cookie. Read `/api/astra/bootstrap` for role/scope, IDs, planning day, current revision and CSRF token.

For JSON mutations send `Content-Type: application/json`, `X-CSRF-Token`, and `X-Astra-Revision` from the latest read. Stale supplied revisions return 409. The original API's compatibility calls may omit the revision; the new workspace always supplies it. Requests during another state operation return `409 state_busy`; refresh and retry. Requests never widen scope through query parameters.

| Route | Method | Purpose |
|---|---|---|
| `/api/astra/bootstrap` | GET | Current identity and authorized selection values |
| `/api/astra/overview?day=0&shift=1&team=T01` | GET | Scoped operational summary |
| `/api/astra/tasks` | GET | Paginated tasks; q/team/aircraft/state/skill/critical/day/shift/mechanic filters |
| `/api/astra/tasks/<id>` | GET | Task detail, crew, predecessor statuses and original factor breakdown |
| `/api/astra/export` | GET | Same scoped filters, CSV with formula-safe text cells |
| `/api/astra/roster` | GET, POST | Read roster; persist planning availability/shift configuration |
| `/api/astra/scenarios` | POST | Evaluate a roster change on a copied fleet; never apply it |
| `/api/astra/baselines` | GET, POST | Inspect or immutably pin team/day/shift plan |
| `/api/astra/shift-notes` | GET, POST | Read or append handover notes |
| `/api/astra/history` | GET | Plan lineage and scoped audit history |
| `/api/v1/actuals` | POST | Original single task-state mutation endpoint, with ASTRA wrapper |
| `/api/v1/excusals` | GET, POST | Original policy-controlled disruption evidence |
| `/api/v1/replan` | POST | Original engine replan, validated plan capture and envelope refresh |

Example actuals body:

```json
{"task_id":"0001-T00001","state":"done","execution_day":0,"execution_shift":1}
```

Task states: `not_started`, `in_progress`, `blocked`, `done`. `remaining_minutes` is supported for in-progress work. Leads/managers must explicitly supply `force: true` to reopen completed work. The mechanic workspace cannot force a reopen. An `Idempotency-Key` header identifies retries of one actuals request; reusing a key for a different payload/identity returns 409.

Example roster configuration or scenario body:

```json
{"changes":[{"mech_id":"T01-S1-M001","available":false,"shift":1}]}
```

IDs must come from the actual roster. Availability and shift apply across the planning horizon. Team ownership and skills cannot be edited through this endpoint.

Example replan body:

```json
{"start_day":1}
```

The planning day cannot move backward. Omit it to retain the current as-of day. Replanning is synchronous and returns the new plan ID, snapshot ID and revision. The UI shows its running state until completion.

Baseline body:

```json
{"team":"T01","day":0,"shift":1}
```

The first POST pins the current plan; repeat attempts return 409. A stale schedule must be replanned first. The GET response includes frozen original values and recorded execution credit. A baseline is a plan commitment, not an editable target.

The inherited FF read endpoints remain available for economics, capacity, candidates, progression, recap, wall and scorecard. Missing external scorecard history is reported as missing, not replaced with invented observations. The preserved full-fleet comparison routes are read-only and director/executive scoped.
