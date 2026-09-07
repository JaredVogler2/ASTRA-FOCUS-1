# Excusal policy — owner rulings (2026-07-11) and their implementation

Companion to `docs/GATE_PRESSURE_DESIGN.md` (gate pressure needs fair
excusals or it becomes a blame machine). The three policy questions were
put to the owner; the rulings below are LAW in the codebase, each with
its enforcement point and tripwire tests (`tests/test_excusal_rulings.py`,
`tests/test_rework_origin.py`).

## Ruling 1 — rework ownership follows the parent SOI

> "Rework drops into the system with a parent_soi associated with the
> rework paperwork. These parent SOIs belong to a team, they must own it."

- `Task.rework_origin_team` = the parent task's team, ALWAYS — a
  paperwork-derived fact, never a sampled or lead-editable attribution.
- Excusability (`disruption.rework_excusable`): the injected work is
  excusable ONLY for a **different** team executing the fix (cross-trade
  repair of someone else's defect). The parent's own team fixing its own
  defect owns the work outright.
- A lead capturing `REWORK_INJECTION` manually cannot flip this — the
  merge re-derives from the origin (`merged_excusals`).
- Malformed data (no parent linkage): `REWORK_UNATTRIBUTED_EXCUSABLE`
  (default False — owned until the paperwork says otherwise).
- Sim realism: ~60% of injected rework is fixed by the parent's team
  (owned), ~40% is a cross-trade fix (skill "ANY", excusable for the
  fixer; the parent team's causation is named in the evidence).

## Ruling 2 — predecessor waits: cross-team excused, own-team owned,
## with ROOT-CAUSE pass-through

> "If the upstream predecessor task that delayed is caused by another
> team, then excused. If the upstream predecessor is owned by their own
> team then inexcusable."

- First hop unchanged: `CROSS_TEAM_PREDECESSOR` excusable,
  `SAME_TEAM_PREDECESSOR` owned.
- **Pass-through** (the ruling's own logic, applied through
  intermediaries): when the own-team blocking chain's ROOT cause is
  external — the root pred is parts-blocked, capacity/skill-starved by
  the engine's honest verdicts, or itself waiting on ANOTHER team — the
  successor's wait is excused, with the root named verbatim in the
  evidence (`_own_chain_root_excuse`, cycle-guarded, capped at
  `config.CHAIN_WALK_CAP` = **5 upstream hops** per owner update
  2026-07-11: a team with five own tasks stacked ahead of the wait has
  had every chance to work the sequence).

### Scope: excuses bind ONLY to the shift's planned slice (owner-confirmed)

Grading excusals attach at `points.shift_report`, which iterates ONLY
the tasks the scheduler assigned to exactly that (team, day, shift) —
a job scheduled far downstream never enters today's goal denominator,
so its excusability (either way) cannot move today's attainment. The
fleet-wide attribution census is DIAGNOSTIC ONLY (Excusals browse tab,
pareto context). Measured on the rulings-validation state (5-hop cap):

| Slice | same-team wait records | excused by pass-through |
|---|---|---|
| today's shift slices (grading-relevant) | 180 | 23 (**13%**) |
| commitment horizon (d+1..d+2) | 54 | 4 (7%) |
| far future — never in today's goal | 32,578 | 20,053 (62%) |

The previously-reported "64% pass-through" was the fleet-wide census —
dominated by far-future chains that cross teams somewhere upstream. The
number that touches grades is the 13%.
- A chain that bottoms out on own-team work that is simply not done
  stays OWNED — an own-team slip never launders itself by passing
  through more own-team tasks.
- Cascade visibility (already shipped with gate pressure): behind-gate
  successors tag `same_team` cascade so ONE slipped root never reads as
  N independent failures; leads chase the root.

### Expert feedback recorded with the ruling (owner asked)

The rule is the right backbone — it matches trade boundaries, it is
explainable on the floor in one sentence, and the pass-through closes
its one fairness hole (a parts-starved own-team pred is the vendor's
fault, not the crew's). Two watch-items on real data:

1. **Exported-delay blindness.** The downstream victim is excused, but
   the upstream BLOCKER currently feels only its own local miss — while
   the damage it exports (idle successor crews, traveled work) is
   usually larger. Recommended next: a **caused-delay ledger** per team
   ("days of downstream wait you exported this week", from the same
   cross-team wait records) — visibility first, never a grade hit until
   calibrated. Not yet implemented; awaiting owner nod.
2. **Boundary lobbying.** Once excusals ride on team boundaries, task
   ownership mapping becomes a pressure point. Ownership must come from
   SOI/trade master data, never lead-editable surfaces (true today in
   FF; keep it true in the real feed).

## Ruling 3 — overrun tolerance: excusable to 3x, owned beyond

> "Overruns are not excusable if the duration was over 3x the allotted
> standard."

- `OVERRUN_EXCUSE_FACTOR = 3.0` (config §5, env-tunable).
- `DURATION_OVERRUN` auto-fires on open work whose ACTUAL burn exceeds
  its standard (needs an actuals feed — `Task.actual_minutes`; the sim
  accumulates booked session minutes; real data brings clocked labor):
  excusable while `actual <= 3.0 x standard`, OWNED beyond.
- Manual overrun captures cannot flip a >3x blowout back to excused;
  without an actuals feed a manual capture stays owned (conservative:
  no data, no excuse).

### Expert feedback recorded with the ruling (owner asked)

Implementable — done. Two honest calibration notes:

1. **3x is generous.** Typical standards variance bands run 1.2–1.5x;
   at 3x, nearly all overruns become excusable and the signal mostly
   disappears from attainment. That may be exactly right for a floor
   with noisy standards — but the excused-overrun DISTRIBUTION should
   be watched (it is in the excusal records) and the dial tightened
   when WATTS-grade time-study data can separate "standard is wrong"
   from "execution was slow." The >3x blowouts that DO fire are rare,
   and each has a story — they are flagged `NOTES_REQUIRED` so the
   story gets captured.
2. **>3x is often not the crew either.** Undiscovered damage, mid-job
   parts waits, and wrong standards all masquerade as blowouts. The
   ruling charges the team by default (correct: it forces the
   conversation), but expect a real-data follow-up rule splitting
   overrun-vs-standard once WATTS integration lands.

## Interactions (unchanged laws)

GG-3 semantics intact (excusals leave the goal VISIBLY, never silently);
GG-2 intact (no excusal ever ADDS points); gate-pressure behind/cascade
metrics consume these same records; the needs-support leaderboard
framing (PSY-5) picks up the new excusable causes automatically.

All demo figures SYNTHETIC (OR-5); thresholds are placeholders pending
real cadence + WATTS standards data.
