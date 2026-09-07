# FOCUS Flask Dashboard (`web_app_flask/`) — the primary dashboard

The FOCUS dashboard — Flask + vanilla JS, port 5000 — ported from
FOCUS-2026-FGI's original and wired to the **MAX (Fable) schedule
optimization engine**. Per the owner's direction this is the primary
dashboard; the FastAPI/React app (`../web_app/`) is the comparison
surface.

## Running it

```bash
# 1. Install (from MAX/):
pip install -r requirements.txt        # includes flask / flask-cors / flask-compress

# 2. Produce a schedule for it to read (from MAX/):
python run.py run --data-dir data_files/mock/fleet50 \
    --roster-file data_files/mock/fleet50/staffing_roster.csv

# 3. Serve (from MAX/web_app_flask/):
python run.py                          # dev → http://localhost:5000

# Production (pip install gunicorn first — not in requirements.txt):
gunicorn 'src.app:create_app()' -b 0.0.0.0:5000 -w 1 --threads 8 --timeout 120
```

Then open **http://localhost:5000/dashboard**.

| URL | What |
|---|---|
| `/` | landing page (schedule picker) |
| `/dashboard` | main iOS-style shell — all 17 tabs |
| `/dashboard/classic` | classic shell — **identical tab set**, kept so old and new layouts can be compared |

After a new engine run, `POST /api/refresh` re-scans the schedules
directory — no server restart needed. Schedule source directory:
`MAX/outputs/schedules/` (override with the `MAX_SCHEDULES_DIR` env
var). The app loads the newest `max_v1_*.json.gz` envelopes by file
modification time.

## The 17 tabs

**Ported FOCUS views (pre-existing, retained in full):**

| Tab | What it does |
|---|---|
| Team Lead | jobs/task **priority list** ordered by the optimizer's global rank, with client-side **Smart Auto-Assign** driven by each task's crew binding |
| Management | executive dashboard + burndown |
| Mechanic | individual view |
| Industrial Engineering | review queue — flag / resolve tasks (`ie_review_queue.json`) |
| Staffing | peak demand by day |
| Supply Chain | late parts |
| Scenario | what-if runner + priority-impact estimator |
| Shift Performance | "Do What's Prioritized" grading with delay reasons (SQLite) |
| Project | aircraft tracking timeline |
| Schedule Budget | budget view |
| Worker Gantt | per-worker Gantt |
| Development | development-program board (check-off / override) |

**Insight tabs added July 2026 (each its own tab; existing views
untouched):**

| Tab | What it shows | API |
|---|---|---|
| **Commitments** | committed vs. projected delivery dates for the cohort (all post-CS-733 aircraft + next 8), lateness per ship, and the evidence-backed change log (hysteresis: <2-day jitter held, ≥7-day slips move immediately, improvements need 2 confirming replans) | `GET /api/projection` |
| **Economics** | fleet + per-aircraft lateness dollars split into **unavoidable floor** vs. **controllable**; rates are config defaults ($200M value, $100k/day) until real contract terms are supplied | `GET /api/projection/economics` |
| **Shift Book** | team-lead view of one (day, shift): every task with clock times and its named crew | `GET /api/shiftbook`, `/api/shiftbook/options` |
| **My Day** | one mechanic's day in sequence — times, aircraft, crewmates. **Read-only today**: no tap-to-complete yet (roadmap) | `GET /api/myday`, `/api/myday/options` |
| **Capacity** | ranked $-weighted bottleneck table (capacity-wait attribution per team/shift), measured +1-person what-if results ("recovers N late-days", from `tools/capacity_whatif.py`), and the **bottleneck-consistency** panel ("#1 in N of last M replans" from the pressure history) | `GET /api/capacity` |

## How it connects to the MAX engine

1. The engine exports `MAX/outputs/schedules/max_v1_*.json.gz`
   (Stage 1 intake → Stage 2 CPM-greedy **named placement** with the
   commitment layer → Stage 3 crew verification → Stage 4 continuity +
   delay repair; the CP-SAT path is off by default — see
   `MAX/README.md`).
2. `src/max_adapter.py` normalizes the MAX envelope into the FGI shape
   this dashboard was built against — flat `teamCapacities`
   ("TEAM S1 (ANY)"), `mechanic_timelines`, product delivery cards,
   per-pool `utilization`, per-task `priority` / `teamSkill` / integer
   `mechanic_id` derived from the named-crew BEMS ids (which ride
   along in `mechanicIds`). Legacy `fgi5_optimized_*.json.gz` files
   load unchanged.
3. The five insight tabs read the envelope's newer stats blocks
   directly (`stats.economics`, `stats.capacity_pressure`, the
   projection block) via the `projection` and `shiftbook` blueprints.

## Write paths — what this app does and does not persist

This app's write endpoints persist to **its own stores**:

- Shift-performance history → `web_app_flask/data/shift_performance.db` (SQLite, auto-created)
- IE review queue → `web_app_flask/ie_review_queue.json`
- Task assignment / auto-assign, scenario runs, development check-offs → app-local persistence

**None of these write the optimizer's live-state feed**
(`open_work_state.json`). Today the only dashboard write-path into the
optimizer intake is the FastAPI app's `POST /api/v1/d2/actuals`
endpoint (or the cadence simulator). Floor write-back from the My Day
tab is the top roadmap item.
