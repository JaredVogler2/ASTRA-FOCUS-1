# Gate pressure — station-schedule urgency for non-critical work

**Status:** investigated + implemented behind config (`W_BEHIND`, default
ON at 35) + measured (results appended, §9). Owner directive, 2026-07-11:

> "Add pressure to complete jobs behind schedule (reason for why beyond
> CS Gate should be considered), so that jobs not critical still get
> done."

All figures in this document are SYNTHETIC (fleet50 mock data, OR-5).

---

## 1. The failure mode (why delivery-relative slack is not enough)

Every pressure signal in FF v1 derives from **one clock**: the
aircraft's delivery deadline.

- `urgency` = CPM slack vs the aircraft's delivery date
- `critical` = slack ≤ 0.5 days vs that same date
- `risk` / `recovery` = only fire when the aircraft is already LATE
  vs that date

A job at line position ~700 sits months from delivery. Its slack is
enormous, so its value multiplier is ~1× effort — the minimum. Three
systems then conspire to let it slide, **each behaving exactly as
designed**:

1. **Execution incentive.** Crews under point pressure finish the
   boosted jobs first; the cheap early-position fillers are the natural
   sacrifice when a shift runs short.
2. **Grading blindness.** Attainment is value-weighted (GG-2), so
   missing ten 12-point fillers dents the grade about as much as
   missing one keystone. Chronic early-position slippage is nearly
   invisible in the letter grade.
3. **Excusal asymmetry is no help.** The slipped filler has no
   excusable cause — the team "owns" it — but owning a 12-point miss
   costs nothing anyone notices.

The compounding cost is the classic **traveled-work spiral**: a job
done out of its build position costs a multiple of its in-position
cost (access closed up, stuffed areas, staging, re-certification), and
the backlog lands exactly when the aircraft enters the delivery window
— converting invisible early slippage into visible late fires. The
delivery-relative clock cannot see this because, by construction,
nothing early is ever urgent on it.

**The owner's framing is the correct one:** a job is behind schedule
when it is past its *station gate* (CS-gate / plan-of-record position),
not when it threatens delivery. Delivery is the last gate, not the only
gate.

## 2. The mechanism

### 2.1 Per-task gate (`Task.gate_day`, optional)

Every task may carry a **gate day** — the day the plan of record
expects it done by. Semantics: `today > gate_day` and not done ⇒ the
task is **behind schedule** (aging traveled work), regardless of
remaining delivery slack.

Gate sources, in order of fidelity:

| Source | When | Notes |
|---|---|---|
| CS-gate / station calendar feed | production | per-task gate from the real station progression plan (MAX's `station_progression.csv` analog); the mechanism reads `gate_day` and does not care who wrote it |
| **Plan-of-record stamping** (implemented for mock) | demo/sim | `gate_day = baseline-schedule slot day + GATE_GRACE_DAYS` — the published takt plan IS the gate; slipping more than the grace past where the plan of record put you = behind |
| unset (`None`) | legacy fixtures | factor inert; zero behavior change (tripwire-tested) |

The plan-of-record form was chosen for the mock because it needs no
invented data: the baseline schedule already exists, is committed, and
is exactly what a CS-gate ladder is — the published expectation of
where each job sits in the flow.

### 2.2 Bounded aging boost (`behind` factor, config §5)

New scoring factor in the ONE shared factor module (`points.py`):

```
behind_raw  = min(days_past_gate / BEHIND_CAP_DAYS, 1)   # 0..1, bounded
contribution = effort × (W_BEHIND / 100) × behind_raw
```

- `W_BEHIND = 35` (percent-of-effort, between W_RISK 30 and W_DOWN 45:
  aged position work outranks generic fillers but never outranks
  clearing a keystone)
- `BEHIND_CAP_DAYS = 5`: the boost saturates — a job 30 days behind is
  not worth 6× a job 5 days behind (that would make backlog *farming*
  lucrative, §4)
- Pass-through scaling, NOT percentile normalization: behind_days is
  zero for most of the fleet most of the time, so a p5/p95 normalizer
  degenerates (`hi ≤ lo` ⇒ factor never pays). The raw is already
  bounded 0..1 by design.

This is the classic OR **aging / anti-starvation escalation** applied
to the incentive layer: value grows with time-in-queue so low-priority
work cannot starve, with a cap so aging never dominates true
criticality.

### 2.3 What deliberately did NOT change (v1)

- **The scheduler.** The engine already left-pulls every task to its
  earliest feasible slot; the plan is not where the starvation happens
  — execution adherence is. Gate-aware *dispatch* (e.g.
  `effective_slack = min(cpm_slack, gate_slack)`) was considered and
  parked: in capacity-tight windows it can displace genuinely critical
  work with merely-aged work, which is the wrong trade. If the measured
  incentive channel proves insufficient on real data, dispatch aging
  belongs behind its own measured A/B.
- **Attainment/grade semantics.** The grade stays plan-relative
  (earned/goal with the boost riding in both) — being handed a backlog
  does not change your letter grade; *burning it down* pays points and
  shows in gate-health (§3).

## 3. Visibility: gate health in the scorecard

Points pressure without visibility invites quiet failure, so the
scorecard grows a **gate-health** lane alongside (never inside) the
grade, per slice × period:

- `behind_open_points` — aged value still open (the backlog)
- `behind_done_points` — aged value completed (burn-down credit)
- `behind_avg_age_days` — how stale the open backlog is
- backlog trend over days/weeks (same trend engine as attainment)

Framing follows PSY-5: a slice with absorbed excusable causes carrying
a big backlog *needs support*; only unexcused aging is "owned."

## 4. Perverse-incentive analysis (what this idea must survive)

| Exploit | Channel | Why it fails / mitigation |
|---|---|---|
| **Slow-rolling** — deliberately let jobs age past gate, harvest the boosted points later | raw points / XP | (1) attainment: skipping costs earned-at-today's-value now; the later boost inflates goal AND earned — cancels, the miss never cancels. (2) leaderboard NEVER ranks raw points (GG-2 hard rule). (3) boost caps at +35% after 5 days — far below the value already forfeited. (4) **adversarially tested**: a slow-roller policy was added to the correlation gate; the gate FAILS the build if slow-rolling ever out-scores flow (§9). |
| **Backlog farming** — leads steer the plan to create aged work | planning | gates come from the plan of record / CS feed, not from anything a team edits; re-planning does not move `gate_day`. |
| **Gaming excusals** — excuse the aging, keep the boost | GG-3 | excusal removes a task from the GOAL, it never adds points; excused aged work is reported in the needs-support lane, not the owned lane. |
| **Chasing aged fillers over keystones** | dispatch of attention | weight ordering: W_CRIT 60 > W_DOWN 45 > W_BEHIND 35 — an aged filler beats a fresh filler, never a keystone; the correlation gate (flow-beats-chaser) re-ran green with the factor live (§9). |

## 5. Interaction review (every doctrine surface)

- **GG-2 (flow, not volume):** intact — boost is effort-scaled, rides
  in goal and earned symmetrically, leaderboard unchanged.
- **GG-3 (excusal fairness):** intact — aging attribution reuses the
  same excusable-cause machinery; behind + excusable ⇒ support lane.
- **GG-4 (explainable):** the chip gains a fixed phrase:
  `+N behind station gate (D days)` — a mechanic sees exactly why the
  old job pays more today than yesterday.
- **OR-1/OR-2/OR-3:** untouched (no crew, borrowing, or calendar
  changes).
- **Commitments (§9/§12):** untouched mechanically; gate health is a
  *leading indicator* for future committed-date slips and is surfaced
  beside them.
- **P_OOS:** unchanged and still dominant — an aged job jumped out of
  sequence still loses 40% of effort; aging never licenses DAG jumps.
- **Engine/validator:** untouched (V1–V9 semantics identical).

## 6. Calibration notes (placeholders, OR-5)

`W_BEHIND=35`, `BEHIND_CAP_DAYS=5`, `GATE_GRACE_DAYS=2` are synthetic-
data placeholders. On real data, calibrate so that (a) the slow-roller
gate margin stays comfortably negative, (b) measured traveled-work cost
multiples (real, from IE time studies — WATTS is the natural source)
set the cap: the boost should approximate the *avoided* out-of-position
cost premium, never exceed it.

## 7. Real-data mapping

- `gate_day` per task from the CS-gate / station-progression calendar
  (task → position/zone → gate date). MAX already consumes
  `station_progression.csv` for delivery projection; the same feed
  extended to task zones is the production source.
- Until task-level zone mapping exists, the plan-of-record stamping
  (baseline + grace) is an honest interim on real data too — "you are
  behind where the published plan put you" is precisely how leads
  reason about traveled work today.

## 8. Alternatives considered and rejected

- **Grade penalty for aged work** (attainment × gate-health): punishes
  teams for inherited backlog and double-counts with the boost;
  violates the plan-relative fairness that makes grades trustworthy.
- **Unbounded aging** (linear forever): makes ancient backlog dominate
  keystones and licenses farming; rejected for the capped form.
- **Scheduler-side aging in v1**: see §2.3 — risk of displacing
  critical work under capacity pressure; parked pending real-data need.
- **Separate "backlog leaderboard"**: rankings multiply, attention
  fragments; gate health as a lane on the existing scorecard keeps one
  ranking (attainment) and one rubric.

## 9. Measured results (fortnight A/B, seed 11, SYNTHETIC)

*Appended after the experiment runs — three arms over the 32-shift
fortnight:*

- **Arm A (status quo):** W_BEHIND=0, crews slide/deviate at random —
  the world where nothing pushes the fillers.
- **Arm B (chasing, no gate pressure):** crews prioritize by points
  (value-biased slides, points-greedy deviation), W_BEHIND=0 — proves
  point-chasing alone does NOT burn the backlog (it chases hot
  aircraft).
- **Arm C (chasing + gate pressure):** same behavior model,
  W_BEHIND=35 — the hypothesis: the boost redirects the same chasing
  energy into the aging backlog without hurting OTD/lateness.

Metrics: behind-schedule open value + average age (end of fortnight and
trend), fleet OTD, fleet lateness, attainment distribution, correlation
gate (flow vs chaser vs slow-roller) verdict.

### 9.1 Adversary probes (slow-roller) — the exploit loses or cannot exist

| Probe | flow | slow-roller | verdict |
|---|---|---|---|
| mini fleet, 9 shifts (aging mix exists) | **12,541** | 11,153 | roller loses **−11%** (5 fewer completions): deferring work into the boost forfeits more than the 35%-capped boost pays back |
| fleet50, 9 shifts | 236,539 | 236,539 | exact tie — every ready task is pre-saturation, withholding degenerates to flow's own ordering: **farming cannot construct an advantage** |
| fleet50, 18 shifts | 466,821 | 466,821 | same tie at 2x window (gate + grace + cap ≈ 7 days exceeds any realistic execution horizon) |

Full gate on fleet50: flow 236,539 > chaser 218,524, ordering invariant
holds — **VERDICT PASS** with the roller check now a permanent hard-fail
criterion (`outputs/points_validation.json`).

### 9.2 Three-arm fortnight (32 shifts each, seed 11, V1–V9 = 0 on all 99 plans)

| Metric | A status quo | B greedy, no boost | C greedy + boost |
|---|---|---|---|
| Aged open backlog, end (pts) | 1,750 | 1,901 | **1,451** |
| Aged open backlog, avg age end (days) | 3.47 | **2.05** | 2.31 |
| Aged value burned down (pts, total) | 145,758 | 131,804 | 132,947 |
| Off-plan value completed (pts) | 80,872 | 189,552 | 195,139 |
| Fleet attainment W1 → W2 | 80.3 → 81.1% | 80.7 → **81.4%** | 80.7 → 79.9% |
| OTD, end | 12 | 12 | 11 |
| Fleet lateness, end (days) | 1,074 | **984** | 1,013 |

**Findings (honest, single-seed, synthetic):**

1. **The boost works as intended, at a modest and appropriate size.**
   B → C isolates the boost under identical chasing behavior: end-of-
   fortnight aged open backlog drops **24%** (1,901 → 1,451). The same
   greedy energy, redirected into aging work.
2. **Point-chasing itself is half the medicine.** Both greedy arms keep
   the open backlog far FRESHER than status quo (avg age ~2.1–2.3 days
   vs 3.47) and complete ~2.4× the off-plan value — value ordering
   naturally catches missed work; the boost focuses it further.
3. **The cost is visible and small.** C gives up one OTD aircraft vs
   A/B, ~1.5 attainment points in week 2 (crews divert to aged catch-up
   work), and 29 lateness days vs B — while still beating status quo on
   lateness (1,013 vs 1,074). Pressure, not distortion: exactly the
   W_CRIT > W_DOWN > W_BEHIND ordering intent.
4. **Limitations.** One seed; the deviation channel moves only ~7% of
   crew decisions per shift, so aggregate effects are floors, not
   ceilings — on a real floor where points inform ALL prioritization
   the lever is stronger. Day-grain backlog series are noisy
   (single-seed spikes both directions); endpoint and age metrics are
   the stable readouts. Calibrate W_BEHIND / BEHIND_CAP_DAYS on real
   traveled-work cost data (§6) before treating the trade as tuned.

**Recommendation:** keep `W_BEHIND=35 / BEHIND_CAP_DAYS=5 /
GATE_GRACE_DAYS=2` as the shipping defaults; revisit against the §6
calibration sources when real actuals land.
