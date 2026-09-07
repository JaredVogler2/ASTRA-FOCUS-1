# web_flask VENDOR_CHANGES — FF_app vendored copy of MAX/web_app_flask

Source: `/home/user/MAX_FOCUS_1/MAX/web_app_flask` copied VERBATIM on
2026-07-11 (INCREMENT 6 Part B). Verified byte-identical with `diff -r`
immediately after the copy (exit 0), before the changes below.
Exclusions applied at copy time: `__pycache__/`, `*.db`,
`ie_review_queue.json`, `outputs/` (none of the last three existed in the
source tree; listed for the record).

RULES HONORED: no restyling, no tab renames, no view-behavior changes.
Every diff below is a path or import-shim change only, per the INCREMENT 6
Part B addendum in `FF_app/ARCHITECTURE.md`. Never edit the 17 views'
behavior here; envelope problems are fixed in the EXPORTER
(`FF_app/ff/export/envelope.py`), never in the vendored adapter.

## 1. NEW FILE — `src/ff_constants.py`

Local constants shim replacing the MAX engine package imports. Carries the
IDENTICAL values verified against `MAX/max/core/config.py` (GROUNDING.md §1)
on 2026-07-11:
`SHIFT_EFFECTIVE={1:460,2:460,3:370}`, `SHIFT_PAID={1:480,2:480,3:390}`,
`SHIFT_MAX={1:520,2:520,3:430}`, `DAY_WORK_MINUTES=1290`, plus a verbatim
copy of `max.core.shift_detection.detect_shift_and_work_day` (ET boundaries
05:30/14:00/22:30; 3rd shift belongs to the work day it flows INTO).

## 2. `src/max_adapter.py:33-40` — engine-constants import redirected

- Upstream: `from max.core.config import (DAY_WORK_MINUTES, SHIFT_EFFECTIVE,
  SHIFT_MAX, SHIFT_PAID,)`.
- Now (`src/max_adapter.py:38`): `from src.ff_constants import (...)` —
  identical names, identical values. The `import src.paths` line is kept
  (it anchors ROOT/SCHEDULES_DIR); its comment updated because it no longer
  exists to enable `max.*` imports. No adapter LOGIC was touched.

## 3. `src/blueprints/development.py:29-31` — same shim redirect

- Upstream line 29: `from max.core.config import SHIFT_EFFECTIVE, SHIFT_PAID`.
- Now (`development.py:31`): `from src.ff_constants import SHIFT_EFFECTIVE,
  SHIFT_PAID` (+2-line comment). This was a MODULE-TOP import: without the
  shim the whole app failed at boot (app.py registers this blueprint
  unconditionally).

## 4. `src/blueprints/shift_performance.py:1078-1081` — shift-detection shim

- Upstream line 1079 (lazy, inside try/except):
  `from max.core.shift_detection import detect_shift_and_work_day`.
- Now (`shift_performance.py:1081`): `from src.ff_constants import
  detect_shift_and_work_day` — the shim carries the verbatim boundary
  logic, so the snapshot-less plan-only path keeps WORKING instead of
  silently falling into the except branch.

## 5. `src/app.py:24-40` — flask_cors / flask_compress made OPTIONAL

- Upstream lines 25-26 imported `flask_cors.CORS` and
  `flask_compress.Compress` unconditionally. FF_app's runtime rule is
  stdlib+flask only and this environment does not have those packages, so
  the app could not boot — classified as a vendored path/dependency bug,
  fixed HERE per acceptance rule 5.
- Now: both imports are wrapped in try/except ImportError with no-op
  fallbacks (`CORS(app)` returns app; `Compress(app)` does nothing).
  With the packages installed, behavior is byte-identical to upstream;
  without them the dashboard serves uncompressed same-origin responses.

## 6. `src/paths.py` — docstring/comments updated; SCHEDULES_DIR default

- The CODE is functionally unchanged: `MAX_ROOT = WEBAPP_ROOT.parent` now
  resolves to `FF_app/`, so the default `SCHEDULES_DIR` automatically
  becomes `FF_app/outputs/schedules` (the addendum's required default) with
  the `MAX_SCHEDULES_DIR` env override still winning, exactly as upstream.
- The variable name `MAX_ROOT` is KEPT (projection.py imports it for the
  capacity what-if artifact at `<root>/outputs/capacity_whatif.json`,
  which now points at `FF_app/outputs/capacity_whatif.json` — absent =>
  the capacity tab honestly reports "No what-if sweep on file").
- Docstring + comments rewritten to describe the FF layout honestly (the
  sys.path append now exposes `ff.*` instead of `max.*`).

## INCREMENT 6 Part C additions (2026-07-11) — FF bridge + 4 insight tabs
## + My Day write-back. ADDITIVE ONLY; the 17 vendored views' behavior is
## untouched except the documented My Day additive diff (§11, the one
## permitted touch per the Part C addendum).

## 7. NEW FILE — `src/blueprints/ff_bridge.py`

Thin server-side proxy (url_prefix `/api/ff`) to the FF_app backend at
`FF_API_BASE` (env, default `http://127.0.0.1:8080`), stdlib+flask only
(urllib.request + http.cookiejar; no third-party HTTP client). Fixed
allow-list: GET points/shift, points/leaderboard, progression/team/<t>,
recap, excusals, explain/candidates; POST excusals, actuals, replan —
the ONLY writes, forwarded verbatim to the FF single write-paths (OR-6:
POST /api/v1/actuals stays the one actuals path; this proxy adds no
second one). POST /api/ff/replan additionally runs the LOCAL dashboard
refresh (`current_app.reload_schedules()` — the exact body of
`POST /api/refresh`) after FF re-exports the envelope, and reports
`dashboard_refreshed` / `current_schedule` honestly. Identity
translation for writes (Part-A ride-alongs): envelope
`taskId = f"{soi}_{line}"` -> FF `task_id` (= soi) via per-task
`ff_task_id` (fallback: strip the `_<line>` suffix); digit BEMS -> FF
mech id via top-level `ff_mechanic_map`. Unreachable FF backend => an
honest HTTP 502 `{error, code: "ff_backend_down"}` — never a fake 200;
FF error statuses (400 notes_required, 409 done_reopen_requires_force,
403/404, …) pass through untouched.

AUTH — FUTURE HARDENING ITEM: the bridge holds a SERVICE session
(`POST /login` role=director scope=all on first use, re-login once on
401). The vendored dashboard has no per-user auth, so per-user
credential pass-through (each dashboard user logging into FF with their
own persona, so FF's RS scope rules apply per user instead of an
all-scope service account) is deferred and documented here as a known
gap. Do not treat the service session as an authorization model.

## 8. `src/app.py:104-108` + `src/app.py:260` — bridge registration

- `src/app.py:108`: `from src.blueprints.ff_bridge import ff_bridge_bp`
  (with a 3-line VENDOR CHANGE comment at 105-107).
- `src/app.py:260`: `app.register_blueprint(ff_bridge_bp)`.
Both additive, alongside the existing blueprint registrations.

## 9. NEW PARTIALS — 4 insight tabs (house pattern, both shells)

`templates/partials/_arena_view.html` (`data-view="arena"`),
`_progression_view.html` (`progression`), `_excusals_view.html`
(`excusals`), `_explain_view.html` (`explain`) — each a self-contained
partial with its own fetch/render script (same recipe as
`_capacity_view.html`), reading ONLY `/api/ff/*` (plus the existing
`/api/shiftbook/options` for team/day/shift dropdown defaults).
Arena + Progression are read-only (GG-1); the leaderboard renders
attainment/efficiency/difficulty + needs-support framing, never raw
points (GG-2, enforced upstream by the FF API). Excusals posts lead
captures through the bridge with client-side notes-required enforcement
for SAME_TEAM_PREDECESSOR / DURATION_OVERRUN (server 400 passthrough
remains the authority). Explain renders the deterministic explanation
always and the validated narrative only when present, with the
`deterministic` | `llm-validated` source badge (LB-8).

## 10. Shell nav buttons + includes (both shells; file:line)

- `templates/dashboard-ios.html:112-127`: four `ios-nav-tab` buttons
  (`data-view` arena/progression/excusals/explain) after the Capacity
  tab; `templates/dashboard-ios.html:1274-1278`: the four
  `{% include %}` lines after `_capacity_view.html`.
- `templates/dashboard2.html:63-67`: four `view-tab` buttons after the
  Capacity tab; `templates/dashboard2.html:92-96`: the four
  `{% include %}` lines after `_capacity_view.html`.
All marked `VENDOR ADD (INCREMENT 6 Part C)` inline. No existing button
or include was moved, renamed, or restyled.

## 11. `templates/partials/_myday_view.html` — the ONE permitted touch
## to an existing view (additive buttons only, per the Part C addendum)

Additive diff (every block marked `VENDOR ADD (INCREMENT 6 Part C)`):

- lines 17-18: `#mdWriteStatus` status line div next to `#mdSummary`.
- `card(r)` -> `card(r, actions)` (line 41; button row lines 58-70):
  today's cards (actions=true) gain a 3-button row — `▶ Start` /
  `✓ Done` / `⛔ Blocked` with `data-md-action`/`data-md-task`
  attributes; preview cards (actions=false) render EXACTLY as before.
  The two call sites (`render()`, lines 77 and 84) pass the flag; card
  markup above the button row is byte-identical to upstream.
- lines 95-176: the write-back functions (`mdStatus` line 113, `mdPost`
  line 119, `mdReport` line 130) implementing the addendum loop: POST
  `/api/ff/actuals` (409 done-reopen => confirm dialog, re-post with
  `force: true`; Blocked prompts for a parts-ETA day) -> POST
  `/api/ff/replan` (FF replan + envelope re-export + bridge-side local
  refresh) -> POST `/api/refresh` ONLY as fallback when the bridge
  reports `dashboard_refreshed: false` -> `load()` re-fetches the day.
- lines 184-190: click delegation on `#mdToday` (today column only).

HONESTY NOTE: FF v1 `POST /api/v1/actuals` consumes
`task_id/state/remaining_minutes/force`; the `parts_eta_day` and
`bems`-derived `mechanic_id` ride-alongs are forwarded for the audit
trail and ignored by the v1 endpoint (blocked tasks keep their
generator-assigned parts ETA until FF grows a parts-ETA write field).

## NOT changed (for the record)

- Port stays 5000 (`run.py`, `src/app.py.__main__`) per the addendum.
- All 17 original tabs, both shells: behavior untouched (Part C added
  4 NEW tabs and the documented additive My Day buttons; no existing
  view logic, style, or tab name was altered).
- `src/blueprints/*` pre-existing logic, `src/database/`,
  `src/scheduler/`, `src/server_utils.py`: untouched except the two
  import lines in §2-§4 and the registration lines in §8.
- Landing page, discovery globs (`max_v1_*` first-class), mtime-newest
  discovery rule (owner rule; never re-order by metadata): untouched.

## Fortnight increment (Scorecard tab) — additive only

- `src/blueprints/ff_bridge.py`: one new allow-list route
  `GET /api/ff/scorecard -> GET /api/v1/scorecard` (read-only aggregate
  data; query string passes through; FF error envelopes pass through
  untouched — tripwire-tested in tests/test_ff_bridge.py).
- `templates/partials/_scorecard_view.html`: NEW partial — graded
  execution history (grade chips per slice x period, day-grain trend
  arrows, week-over-week deltas; slice selector: team / superintendent
  position G1-G4 / building / shift crew / fleet; period selector:
  week / day / shift). Fetches /api/ff/scorecard only.
- `templates/dashboard-ios.html` + `templates/dashboard2.html`: one nav
  button (`data-view="scorecard"`, "Scorecard (NEW)") + one include
  each, appended after Explain. No existing tab, view, style, or
  discovery logic touched (owner rule: old vs new stay comparable).
