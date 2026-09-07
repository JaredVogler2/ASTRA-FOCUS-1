"""FF_app configuration — sectioned constants per ARCHITECTURE.md (§1-§8).

Every constant in this module is overridable via an environment variable
named ``FF_<NAME>`` (e.g. ``FF_OVERTIME=90``). Values are parsed according
to the type of the in-code default:

- bool:  "1/true/yes/on" -> True, "0/false/no/off" -> False
- int:   int(raw)
- float: float(raw)
- dict:  JSON object; keys/values coerced to the types of the default's
         keys/values (so ``FF_SHIFT_EFFECTIVE='{"1":400,"2":400,"3":300}'``
         yields ``{1:400, 2:400, 3:300}``)
- str:   used verbatim

Honesty rule (OR-5): the §4 economics figures are PLACEHOLDERS, not
negotiated rates; anything derived from them must carry the
``"source": "config-defaults"`` label.
"""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# env-override helper
# ---------------------------------------------------------------------------


def _parse_env_value(raw: str, default):
    """Parse a raw env string into the type of ``default``.

    Enforces the FF_<NAME> override contract: every constant is
    env-overridable with type-safe parsing (int/float/bool/dict/str).
    """
    if isinstance(default, bool):  # NOTE: bool before int (bool is an int)
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"cannot parse boolean from {raw!r}")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, dict):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"expected JSON object, got {raw!r}")
        if default:
            key_type = type(next(iter(default.keys())))
            val_type = type(next(iter(default.values())))
            return {key_type(k): val_type(v) for k, v in parsed.items()}
        return parsed
    return raw  # str passthrough


def _env(name: str, default):
    """Return ``FF_<name>`` from the environment (parsed) or ``default``.

    Empty-string env values are treated as unset so platform config that
    blanks a variable falls back to the in-code default deterministically.
    """
    raw = os.environ.get("FF_" + name)
    if raw is None or raw == "":
        return default
    return _parse_env_value(raw, default)


# ---------------------------------------------------------------------------
# §1 SHIFTS
# ---------------------------------------------------------------------------
# Effective plannable minutes per shift; MAX minutes = effective + overtime
# headroom. Shift 3 (night) is shorter.
SHIFT_EFFECTIVE: dict[int, int] = _env("SHIFT_EFFECTIVE", {1: 460, 2: 460, 3: 370})
SHIFT_MAX: dict[int, int] = _env("SHIFT_MAX", {1: 520, 2: 520, 3: 430})
OVERTIME: int = _env("OVERTIME", 60)
NO_START_BUFFER: int = _env("NO_START_BUFFER", 30)
UTILIZATION: float = _env("UTILIZATION", 0.85)
SHIFT_LABELS: dict[int, str] = _env(
    "SHIFT_LABELS", {1: "1st Shift", 2: "2nd Shift", 3: "3rd Shift (Night)"}
)

# ---------------------------------------------------------------------------
# §2 CALENDAR
# ---------------------------------------------------------------------------
# OR-3: the week's 3rd shift begins Sunday night — shift 3 is plannable on a
# non-working day d iff d+1 is a working day (see ff.domain.shift_eligible).
WEEK_STARTS_SUNDAY_NIGHT: bool = _env("WEEK_STARTS_SUNDAY_NIGHT", True)
HORIZON_MIN_DAYS: int = _env("HORIZON_MIN_DAYS", 60)

# ---------------------------------------------------------------------------
# §3 CPM
# ---------------------------------------------------------------------------
# isCritical <=> cpm_slack <= CRITICAL_SLACK_MIN (in days of work content).
CRITICAL_SLACK_MIN: float = _env("CRITICAL_SLACK_MIN", 0.5)
DEFAULT_GRACE_DAYS: int = _env("DEFAULT_GRACE_DAYS", 30)

# ---------------------------------------------------------------------------
# §4 ECONOMICS (PLACEHOLDERS — OR-5)
# ---------------------------------------------------------------------------
# These are NOT real rates. Every economics output derived from them must be
# labeled with ECONOMICS_SOURCE so no one mistakes them for negotiated terms.
AIRCRAFT_VALUE_USD: int = _env("AIRCRAFT_VALUE_USD", 200_000_000)
LATENESS_PENALTY_USD_PER_DAY: int = _env("LATENESS_PENALTY_USD_PER_DAY", 100_000)
ECONOMICS_SOURCE: str = _env("ECONOMICS_SOURCE", "config-defaults")

# ---------------------------------------------------------------------------
# §5 SCORING / POINTS
# ---------------------------------------------------------------------------
# GG-2 canon (GAMES): "Value is priority_score-weighted (mechanic-minute x
# criticality), never raw task count." Recalibrated 2026-07-10 (see
# BUILD_LOG): score = effort_term x value_multiplier, where the factor
# weights below are PERCENT-OF-EFFORT multipliers — a factor at full
# normalized strength adds W/100 of the task's effort term. Shared by
# ff.services.candidates and ff.services.points (one factor implementation,
# two weight profiles).
#
# Why percent-of-effort (measured evidence, fleet50 synthetic mock data,
# 2026-07-10): under the previous flat-points formula the correlation gate
# FAILED — the chaser policy (3,946 short completions, fleet lateness
# WORSENED by 86 days) out-pointed flow (1,855 completions, fleet lateness
# improved 30 days) 164,274 to 152,872 at equal effort budgets, because
# per-snapshot min-max normalizers flattened factor points at 55k-task
# scale and flat-ish per-task scoring paid 30-minute stubs almost the same
# as keystones. Effort-weighting makes points per crew-minute equal to the
# value multiplier, so volume alone can never beat value.
W_CRIT: int = _env("W_CRIT", 60)  # critical-path membership: +60% of effort
W_URGENCY: int = _env("W_URGENCY", 25)  # deadline proximity: up to +25%
W_DOWN: int = _env("W_DOWN", 45)  # downstream unlock count: up to +45%
W_RISK: int = _env("W_RISK", 30)  # $-risk exposure: up to +30%
W_SCARCE: int = _env("W_SCARCE", 20)  # scarce-skill utilization: up to +20%
W_RECOVER: int = _env("W_RECOVER", 30)  # recovery of late aircraft: +30%
# Gate pressure (docs/GATE_PRESSURE_DESIGN.md — owner directive: jobs past
# their STATION gate gain value so non-critical work still gets done).
# Bounded aging: raw = min(days_past_gate / BEHIND_CAP_DAYS, 1); weight
# sits between W_RISK and W_DOWN by design — an aged filler outranks a
# fresh filler, never a keystone. PLACEHOLDER calibration (OR-5): on real
# data the cap should approximate the measured traveled-work cost premium.
W_BEHIND: int = _env("W_BEHIND", 35)  # behind station gate: up to +35%
BEHIND_CAP_DAYS: int = _env("BEHIND_CAP_DAYS", 5)  # aging boost saturates
GATE_GRACE_DAYS: int = _env("GATE_GRACE_DAYS", 2)  # plan-of-record grace
# Rework excusability (owner ruling: rework drops with a parent_soi and
# the parent SOI's team OWNS it). REWORK_INJECTION is excusable only when
# the defect origin (Task.rework_origin_team = the parent task's team, by
# paperwork rule) is a DIFFERENT team than the one executing the fix.
# Unattributed rework follows this flag — default False: the receiving
# team owns it until the paperwork says otherwise.
REWORK_UNATTRIBUTED_EXCUSABLE: bool = _env("REWORK_UNATTRIBUTED_EXCUSABLE", False)
# Root-cause pass-through walk cap (owner ruling 2 + 2026-07-11 update:
# 5 hops, not 10). An own-team blocking chain is walked at most this many
# predecessors looking for an EXTERNAL root cause; deeper chains are
# owned outright — a team with five own tasks stacked ahead of the wait
# has had every chance to work the sequence.
CHAIN_WALK_CAP: int = _env("CHAIN_WALK_CAP", 5)
# Duration-overrun tolerance (owner ruling: overruns are NOT excusable
# once actual burn exceeds this multiple of the allotted standard; within
# it, overruns are excusable normal variation). PLACEHOLDER pending
# WATTS-grade standards data — 3x is generous; the excused-overrun
# distribution is surfaced so this dial can be tuned honestly.
OVERRUN_EXCUSE_FACTOR: float = _env("OVERRUN_EXCUSE_FACTOR", 3.0)
P_OOS: int = _env("P_OOS", 40)  # out-of-sequence penalty: -40% of effort
# Effort term scale: crew-minutes (duration_minutes x mechanics_required)
# per effort point. At 5, the generator's task range (30-min/1-crew stub ..
# 480-min/3-crew whale) maps to 6..288 effort points — the "workable
# 10-300 band" for typical tasks (measured fleet50 median task ≈ 250
# crew-minutes → 50 points).
EFFORT_CREW_MINUTES_PER_POINT: int = _env("EFFORT_CREW_MINUTES_PER_POINT", 5)
# Robust percentile normalizer bounds (replaces raw min-max, which a single
# outlier — e.g. one task with a 3,000-task downstream subtree — collapses
# to ~0 for every typical task at 55k-task scale). Values at/below the low
# percentile normalize to 0, at/above the high percentile to 1.
NORM_PCTL_LO: float = _env("NORM_PCTL_LO", 5.0)
NORM_PCTL_HI: float = _env("NORM_PCTL_HI", 95.0)

# ---------------------------------------------------------------------------
# §6 GENERATOR
# ---------------------------------------------------------------------------
# Defaults for the synthetic (mock_data: true) fleet generator.
GEN_AIRCRAFT: int = _env("GEN_AIRCRAFT", 50)
GEN_TASKS_PER_AIRCRAFT: int = _env("GEN_TASKS_PER_AIRCRAFT", 880)
GEN_TEAMS: int = _env("GEN_TEAMS", 20)
GEN_MECHANICS: int = _env("GEN_MECHANICS", 680)
GEN_TASK_UNIVERSE: int = _env("GEN_TASK_UNIVERSE", 6500)
GEN_SEED: int = _env("GEN_SEED", 20260710)

# ---------------------------------------------------------------------------
# §7 WEB
# ---------------------------------------------------------------------------
DEFAULT_PORT: int = _env("DEFAULT_PORT", 8080)
# FF_ENV: "dev" enables the dev secret-key fallback; anything else (including
# unset) requires FF_SECRET_KEY. Tanzu manifest sets FF_ENV=prod.
FF_ENV: str = os.environ.get("FF_ENV", "")
DEV_SECRET_KEY: str = "ff-dev-key"


def get_secret_key() -> str:
    """Return the Flask secret key, enforcing the §7 SECRET_KEY rule.

    FF_SECRET_KEY is REQUIRED unless FF_ENV=dev, in which case the fixed
    dev fallback 'ff-dev-key' is used. Raises RuntimeError otherwise so a
    misconfigured prod deploy fails fast instead of running with a
    guessable key.
    """
    key = os.environ.get("FF_SECRET_KEY", "")
    if key:
        return key
    if os.environ.get("FF_ENV", "") == "dev":
        return DEV_SECRET_KEY
    raise RuntimeError(
        "FF_SECRET_KEY is required (set FF_ENV=dev to use the dev fallback key)"
    )


def resolve_port(cli_port: int | None = None) -> int:
    """Resolve the listen port with Tanzu precedence: PORT > FF_PORT > CLI > DEFAULT_PORT.

    Tanzu/CF injects PORT; it always wins so the platform health check can
    reach the app.
    """
    for raw in (os.environ.get("PORT"), os.environ.get("FF_PORT")):
        if raw:
            return int(raw)
    if cli_port is not None:
        return int(cli_port)
    return DEFAULT_PORT


# ---------------------------------------------------------------------------
# FACTORY WALL (owner directive 2026-07-11: leaderboards + progression on
# large factory monitors). WALL_WINDOW_DAYS = rolling ranking window in
# work days (de-lumps small-slice units — a delivery team with six big
# jobs should not live or die on one shift). WALL_PUBLIC=True allows the
# read-only /wall page + GET /api/v1/wall WITHOUT login — for kiosk
# monitors on a trusted internal network ONLY (aggregates only, §G8;
# default False: normal role login required).
WALL_WINDOW_DAYS: int = _env("WALL_WINDOW_DAYS", 5)
WALL_PUBLIC: bool = _env("WALL_PUBLIC", False)

# §8 GAME
# ---------------------------------------------------------------------------
# GG guardrail: tangible rewards are OFF by default; the points layer is
# informational (attainment/efficiency), never a raw-points contest.
REWARDS_ENABLED: bool = _env("REWARDS_ENABLED", False)

# ---------------------------------------------------------------------------
# §9 COMMITMENTS (MAX §11 doctrine, OR-4 — commitments move on evidence)
# ---------------------------------------------------------------------------
# |delta| below the hysteresis band is jitter: the committed date HOLDS.
COMMIT_HYSTERESIS_DAYS: int = _env("COMMIT_HYSTERESIS_DAYS", 2)
# Slips/improvements inside the band need this many consecutive replans at
# the same target (±1 day) before the commitment moves ("sustained").
COMMIT_PERSISTENCE_RUNS: int = _env("COMMIT_PERSISTENCE_RUNS", 2)
# A worsening of at least this many days recommits AT ONCE ("bad news fast").
COMMIT_WORSEN_IMMEDIATE_DAYS: int = _env("COMMIT_WORSEN_IMMEDIATE_DAYS", 7)

# ---------------------------------------------------------------------------
# §10 GAMES PROGRESSION (X2/X3 canon adapted — PLACEHOLDER thresholds, OR-5)
# ---------------------------------------------------------------------------
# Event thresholds. A completion is a keystone-cleared when it singly frees
# at least KEYSTONE_MIN_UNLOCKED incomplete successors (mission: "single
# gate freeing >=3"); the Firebreak badge requires FIREBREAK_MIN_UNLOCKED
# ("keystone unlocking >= 5 downstream").
KEYSTONE_MIN_UNLOCKED: int = _env("KEYSTONE_MIN_UNLOCKED", 3)
FIREBREAK_MIN_UNLOCKED: int = _env("FIREBREAK_MIN_UNLOCKED", 5)
# Clean Sweep requires at least this many PLANNED critical tasks in the
# shift (X2 gotcha: a shift with zero planned critical jobs is a vacuous
# 100% and never earns the badge).
CLEAN_SWEEP_MIN_CRITICAL: int = _env("CLEAN_SWEEP_MIN_CRITICAL", 1)
# goal-crossed milestones, percent of shift goal (PSY-2 micro-milestones).
# Stored as a comma-separated string so FF_GOAL_MILESTONES stays a simple
# env override; parsed once here into an ascending int tuple.
GOAL_MILESTONES: tuple = tuple(
    int(x) for x in str(_env("GOAL_MILESTONES", "25,50,75,100")).split(",") if x.strip()
)
# Team level curve: monotone XP thresholds (PSY-4 mastery arc — levels are
# NEVER purchasable and NEVER decay; XP is a pure fold over credited
# completion events). PLACEHOLDER values, mock-scale calibrated only.
LEVEL_CURVE: tuple = tuple(
    int(x)
    for x in str(
        _env("LEVEL_CURVE", "0,250,750,1500,3000,6000,12000,24000")
    ).split(",")
    if x.strip()
)
# PSY-5 needs-support framing: teams below this attainment (with a nonzero
# goal) are framed as "needs support" WITH their excusal context attached —
# never shamed, never eliminated.
NEEDS_SUPPORT_ATTAINMENT: float = _env("NEEDS_SUPPORT_ATTAINMENT", 0.5)
# SCORECARD (fortnight increment): shared org constants + grade bands.
# TEAM_GROUP_SIZE is the superintendent scope rule (contiguous chunks of
# the sorted team list) — the web layer and the scorecard service BOTH
# read it here so the two can never drift. GRADE_BANDS map attainment
# floors to letter grades (PLACEHOLDER thresholds, OR-5 — calibrate on
# real cadence data); descending floors, anything below the last is "F".
TEAM_GROUP_SIZE: int = _env("TEAM_GROUP_SIZE", 5)
GRADE_BANDS: tuple = tuple(
    (float(pair.split(":")[0]), pair.split(":")[1])
    for pair in _env("GRADE_BANDS", "0.95:A,0.85:B,0.75:C,0.60:D").split(",")
)
# Trend classification: |per-period slope| below this is "flat".
TREND_FLAT_SLOPE: float = _env("TREND_FLAT_SLOPE", 0.005)
# Badge earn-rate budgets: expected earns per team-week (X2 §G3 — badge
# inflation is a design failure, surfaced by the monitor, never silently
# accepted). PLACEHOLDERS pending real cadence data.
BADGE_EARN_BUDGET_PER_TEAM_WEEK: dict[str, float] = _env(
    "BADGE_EARN_BUDGET_PER_TEAM_WEEK",
    {"firebreak": 1.0, "recovery": 2.0, "clean_sweep": 1.0, "flow_keeper": 1.0},
)
# The monitor alerts when a badge's observed earn count exceeds
# budget * teams * weeks * this factor.
BADGE_INFLATION_ALERT_FACTOR: float = _env("BADGE_INFLATION_ALERT_FACTOR", 3.0)

# ---------------------------------------------------------------------------
# §11 LLM (Wave-5 canon L1/L2/L4 — LB-1..LB-10; see ff/llm/)
# ---------------------------------------------------------------------------
# Provider selection: "mock" (default — deterministic, offline, credential-
# free), "recorded" (cassette replay from ff/llm/cassettes/), "bcai" (the
# live WATTS-derived transport), "none" (no LLM surface at all). ALL FOCU5
# development and CI run on mock/recorded; the live path is a config swap,
# never a code change (L1 mission).
LLM_PROVIDER: str = _env("LLM_PROVIDER", "mock")
# LOUD WARNING (L1 gotcha): this is the -TEST host. The production BCAI
# host is an OPEN QUESTION (owner to confirm) — do not assume this value
# is production-ready.
LLM_ENDPOINT: str = _env(
    "LLM_ENDPOINT",
    "https://boeingai-test.web.boeing.com/bcai-public-api/conversation",
)
LLM_MODEL: str = _env("LLM_MODEL", "gpt-5.4-mini")  # GPT-5.5-class = config swap
# FOCU5 gets its OWN use_case_id (owner to provision). NEVER reuse the
# WATTS id "bcai-use-case.design-practices" (L1 open question, verbatim).
LLM_USE_CASE_ID: str = _env("LLM_USE_CASE_ID", "focu5-use-case.unprovisioned")
LLM_CONVERSATION_SOURCE: str = _env(
    "LLM_CONVERSATION_SOURCE", "bcai-api-system-identifier"
)
# CSV-parsed like GOAL_MILESTONES so FF_LLM_INFO_TYPES stays a simple env var.
LLM_INFO_TYPES: tuple = tuple(
    x.strip() for x in str(_env("LLM_INFO_TYPES", "earn")).split(",") if x.strip()
)
LLM_TIMEOUT_S: int = _env("LLM_TIMEOUT_S", 120)
LLM_PING_TIMEOUT_S: int = _env("LLM_PING_TIMEOUT_S", 30)
# TWO retry budgets (L1 gotcha — never conflate): transport retries cover
# timeout/connection errors only; schema retries cover parse-validate-
# reprompt (LB-8 bounded loop, then typed failure).
LLM_RETRIES: int = _env("LLM_RETRIES", 2)
LLM_RETRY_BACKOFF_S: float = _env("LLM_RETRY_BACKOFF_S", 1.0)
LLM_SCHEMA_RETRIES: int = _env("LLM_SCHEMA_RETRIES", 2)
LLM_TEMPERATURE: float = _env("LLM_TEMPERATURE", 0.0)
LLM_MAX_TOKENS: int = _env("LLM_MAX_TOKENS", 1024)
# Optional CA bundle path for TLS verification. Verification is ALWAYS on
# (LB-9); an empty value means the system trust store. The WATTS
# verification-off pattern is BANNED repo-wide (tripwire: tests/test_llm.py
# grep test).
LLM_CA_BUNDLE: str = _env("LLM_CA_BUNDLE", "")
# "metadata_only" (default, LB-9/L1-5): prompt/response BODIES are never
# logged. FF_app does not implement a debug-bodies mode at all.
LLM_LOG_POLICY: str = _env("LLM_LOG_POLICY", "metadata_only")
# LB-7 audit trail: every LLM call + validation verdict appends here.
LLM_AUDIT_PATH: str = _env("LLM_AUDIT_PATH", "data/llm_audit.jsonl")
# LB-4 numeric-claim grounding: relative tolerance for narrative numbers vs
# supplied metrics (L4-2 stage 5; default 0.5%). Conservative failure =
# reject (a false rejection costs a narrative; a false pass costs trust).
CLAIM_REL_TOL: float = _env("CLAIM_REL_TOL", 0.005)
# LB-6 sanitizer: length cap for any single untrusted operational text
# value entering a DATA block (truncation is marked, never silent).
UNTRUSTED_TEXT_MAX_LEN: int = _env("UNTRUSTED_TEXT_MAX_LEN", 500)

# ---------------------------------------------------------------------------
# §12 COMMITMENT (MAX config §10 mechanism, OR-4 — scheduler-side defense)
# ---------------------------------------------------------------------------
# OR-4: "Committed work dispatches before new work. Inside the 3-day
# commitment horizon, incumbent slots and mechanics are defended." When
# enabled and an ``incumbent`` map (a prior schedule's placements) is
# threaded into ``ff.engine.scheduler.build_schedule``, tasks whose
# incumbent slot lies within COMMITMENT_HORIZON_DAYS of start_day dispatch
# FIRST and defend their incumbent (day, shift) + crew. With no incumbent
# (or COMMITMENT_ENABLED=False) the scheduler behaves exactly as before —
# tripwire-tested byte-identical.
COMMITMENT_ENABLED: bool = _env("COMMITMENT_ENABLED", True)
COMMITMENT_HORIZON_DAYS: int = _env("COMMITMENT_HORIZON_DAYS", 3)
