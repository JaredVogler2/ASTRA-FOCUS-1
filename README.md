# ASTRA FOCUS 1

A working manufacturing scheduling application built around the original **FF_app Python engine and FOCUS Flask dashboards**, with an integrated operational workspace for management, team leads, and mechanics.

This repository is the complete application source: the original **FF_app engine**, FOCUS Flask dashboards, ASTRA workspaces, browser libraries, tests, sample-data generator, and deployment configuration are all committed here. No submodules, other repositories, CDN assets, external database, or LLM service are required. Python framework packages are pinned in `requirements.txt` and installed during the build.

## Live hosting

[Deploy ASTRA FOCUS on Render](https://render.com/deploy?repo=https://github.com/JaredVogler2/ASTRA-FOCUS-1)

The configuration includes one 2 GB Python instance, a 5 GB persistent disk, generated session secret, readiness checks, and first-start administrator setup. Enter an administrator username and a password of at least 12 characters in Render. A paid host is required for persistent storage. [Deployment guide](docs/DEPLOYMENT.md).

## Start the complete application

With Docker Compose installed:

```bash
git clone https://github.com/JaredVogler2/ASTRA-FOCUS-1.git
cd ASTRA-FOCUS-1
docker compose up --build
```

Open **http://localhost:8080**. The default Compose configuration runs an explicitly labeled **synthetic 50-aircraft demonstration** with persistent state. Select a director, executive, superintendent, manager, lead, or mechanic persona. Initial scheduling and plan capture take approximately 35 seconds in the measured environment; allow up to several minutes on a smaller machine.

Python alternative, on Linux/macOS with Python 3.12:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ASTRA_DEMO=1 gunicorn -c gunicorn.conf.py wsgi:app
```

## Included application

| Workspace | Working behavior |
|---|---|
| Operations overview | Aircraft delivery outlook, actual task states, blocked/unplaced work, shift workload, and team capacity |
| Aircraft schedule | Paginated full-horizon task list; aircraft, team, status, skill, keyword and critical-path filters; task details; scoped CSV export |
| Team management | Named mechanics, qualifications, booked work, team utilization, persisted availability and shift configuration |
| My task list | Named assignments for the selected mechanic/day/shift; start, complete and blocked updates through the original actuals writer |
| Shift performance | Immutable pinned plan; original FF task values frozen at capture; recorded shift completion credit; original excusal policy; handover notes |
| Constraints & capacity | Original FF capacity pressure, blocked task queue, predecessor/release details, and disruption capture |
| What-if scenarios | Staffing changes evaluated on a copied fleet through the original scheduler and validator; live state remains unchanged |
| Plan history | Durable plan versions, parent lineage, user-attributed actuals, staffing, scenario and handover audit records |
| Original FOCUS comparison | Both original Flask shells at `/dashboard` and `/dashboard/classic`, using the current FF envelope; director/executive access, read-only |

The 22-tab original iOS shell and 23-tab classic shell are included in `FF_app/web_flask`. The comparison surface retains their presentation and read paths. Use the ASTRA workspace for operational writes; inherited experimental/dashboard-local writes are deliberately disabled in the comparison. The original FF role pages and read APIs remain available, but the ASTRA workspace owns authenticated write interactions.

## Core scheduling behavior

The application directly imports the bundled FF scheduler. It does not replace it with the experimental TypeScript planner from ASTRA-SEPT-6.

- Full qualified named crew or wait; no borrowing across teams.
- Precedence, releases, parts readiness, effective shift time, overtime and Sunday-night shift rules remain upstream.
- Near-term commitment defense uses the original incumbent slot/crew logic.
- The original independent V1–V9 validator checks schedules.
- Actual task state changes go through `POST /api/v1/actuals`; the wrapper adds security, attribution, persistence of the audit, revision checks, and idempotency.
- Replanning advances the as-of day when explicitly selected. It does not move the planning clock backward.
- Original point factors and excusal rules remain the calculation source. The pinned performance report preserves a historical denominator across replans and credits actuals to the entered execution day/shift.

**Staffing availability applies across the planning horizon.** This is the original engine's roster model, not a day-specific attendance calendar. Team transfers and qualification edits require a reviewed source-roster change. No optimality claim is made.

## Everyday workflow

1. A lead selects the team, planning day and shift, reviews the plan, and pins a shift baseline.
2. Mechanics work from their named task lists and record actual state changes.
3. Leads capture disruption evidence and handover notes.
4. A manager adjusts planning availability or shift assignments as needed and runs a replan.
5. Management reviews projected delivery, feasibility, capacity pressure and performance against the original pinned shift plan.
6. A what-if comparison can evaluate a staffing choice before applying it in Team management.

The original scheduler's raw on-time count can include aircraft with unplaced work. ASTRA's scenario comparison therefore shows **feasible and on-time aircraft**; overview rows explicitly flag unplaced work. Economic weights are labeled placeholder rates from the original configuration.

## Authentication and hosting

The demo is bound to localhost. For an authenticated non-demo deployment, follow [deployment instructions](docs/DEPLOYMENT.md). Production mode requires a private account file with password hashes and server-assigned role/scope, a secret key, and an explicit input fleet. It cannot use the self-selected demo personas.

Run **one process / one application instance** against persistent storage. Threaded requests are serialized around the live engine state. Gunicorn rejects multiple workers, and the WSGI entrypoint holds an exclusive lock on the state directory. SQL Server, MES/Teradata synchronization, enterprise SSO, distributed workers and a live cloud Python host are not provisioned by this repository.

The ready-to-use [Render Blueprint](render.yaml) creates a password-protected Python service with a persistent disk. [Start the deployment](https://render.com/deploy?repo=https://github.com/JaredVogler2/ASTRA-FOCUS-1), provide the administrator username/password, review the paid hosting configuration, and deploy. The initial fleet is generated sample data. See [the exact hosting steps](docs/DEPLOYMENT.md#live-hosting-on-render). A live URL is issued by the host after deployment; committing source to GitHub alone does not start a server.

## Source references

All three requested repositories were inspected. Exact revisions and paths are in [sources.lock.json](sources.lock.json):

- [MAX_FOCUS_1](https://github.com/JaredVogler2/MAX_FOCUS_1): original FOCUS workflows and MAX scheduling rules.
- [FABLE_FOCUS_REVIEW](https://github.com/JaredVogler2/FABLE_FOCUS_REVIEW): executable FF_app baseline, tests, services and preserved FOCUS Flask code.
- [ASTRA-SEPT-6](https://github.com/JaredVogler2/ASTRA-SEPT-6): prior website/deployment approach and review findings, especially scope, persistence and post-replan performance loss.

## Verification

```bash
python -m pip install -r requirements-dev.txt
python scripts/verify.py
```

See [measured validation](docs/VALIDATION.md), [architecture](docs/ARCHITECTURE.md), and [API usage](docs/API.md). All measured fleet results use synthetic data. The public source is an integration around the pinned baseline, not a claim that every proposed item in the reference research roadmap has been implemented.
