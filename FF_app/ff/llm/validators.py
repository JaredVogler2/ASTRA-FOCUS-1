"""ff.llm.validators — the post-response validation pipeline + audit log.

Every LLM output passes these stages IN ORDER before anything renders
(L4-2, adapted to the two shipped templates):

1. ``schema``              — LB-4: structured I/O; strict shape validation.
2. ``candidate_whitelist`` — LB-3: any id outside the presented candidate
   set is a HARD reject (zero tolerance; one invented id fails the call).
3. ``numeric_grounding``   — LB-4: every number in a narrative field must
   match a metric supplied in the context within ``config.CLAIM_REL_TOL``
   (or its percent form). Conservative failure = reject: if the extractor
   cannot ground a number, the output is rejected — a false rejection
   costs a narrative (LB-8 fallback renders); a false pass costs trust.
4. ``uncertainty_presence``— LB-10: ``data_basis`` names a real basis
   (non-empty, not "N/A") and ``confidence`` is in [0, 1].

The pipeline NEVER raises to the caller: :func:`validate_output` returns
``{"ok": bool, "verdicts": [...]}`` and the caller renders its
deterministic fallback on ``ok=False`` (LB-8). Every call + verdict is
appended to the audit log (LB-7) via :func:`audit_append` — template
version, input hash, output hash, per-stage verdicts.

Pure stdlib; deterministic (no wall-clock inputs except the audit ``ts``
stamp, which is metadata, never an engine input).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import config

# Stage names are string constants (L4-2: tested against, never retyped).
STAGE_SCHEMA = "schema"
STAGE_WHITELIST = "candidate_whitelist"
STAGE_NUMERIC = "numeric_grounding"
STAGE_UNCERTAINTY = "uncertainty_presence"

# TaskKey grammar (ff.domain: f"{aircraft:04d}-T{n:05d}") — used to find
# id-shaped tokens in narrative text for the LB-3 whitelist scan.
TASK_ID_RE = re.compile(r"\b\d{4}-T\d{5}\b")

# Numeric-claim extraction (LB-4): digits with optional $-prefix, comma
# grouping, decimal part, magnitude suffix (K/M/B) or percent sign. The
# lookbehind refuses matches glued to a word character or hyphen so team
# codes ("T01") and task ids (already stripped) never yield claims.
_NUMBER_RE = re.compile(
    r"(?<![\w\-.])\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]\b)?"
)
_SUFFIX_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}

# LB-10 substance check: a data_basis that is one of these is a shape-only
# answer, not a basis (L4-2 stage 6: "schema guarantees shape; this stage
# guarantees substance").
_EMPTY_BASES = frozenset({"", "n/a", "na", "none", "unknown", "-"})


# ---------------------------------------------------------------------------
# hashing (LB-7 audit identity)
# ---------------------------------------------------------------------------


def canonical_hash(obj) -> str:
    """sha256 over the canonical JSON of ``obj`` (sorted keys, no spaces).

    LB-7: the audit trail keys every call by input hash + output hash;
    canonicalization makes the hash deterministic for equal content.
    """
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# stage 1 — schema (LB-4)
# ---------------------------------------------------------------------------


def check_schema(obj, schema: dict, path: str = "$") -> list[str]:
    """Validate ``obj`` against the template's response schema; return errors.

    LB-4 (cited law): all LLM I/O is structured — a response is either
    fully schema-valid or rejected; there is no best-effort parse and no
    partially valid object. Supported keywords (stdlib mini-validator):
    type (object/array/string/number/integer/boolean), required,
    properties, additionalProperties (STRICT: defaults to False for
    objects with declared properties), items, minLength, minimum, maximum.
    """
    errors: list[str] = []
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(obj, dict):
            return [f"{path}: expected object, got {type(obj).__name__}"]
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in obj:
                errors.append(f"{path}.{name}: required field missing")
        if props and not schema.get("additionalProperties", False):
            for name in sorted(obj):
                if name not in props:
                    errors.append(f"{path}.{name}: unexpected field")
        for name in sorted(props):
            if name in obj:
                errors.extend(check_schema(obj[name], props[name], f"{path}.{name}"))
        return errors
    if kind == "array":
        if not isinstance(obj, list):
            return [f"{path}: expected array, got {type(obj).__name__}"]
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(obj):
                errors.extend(check_schema(item, item_schema, f"{path}[{i}]"))
        return errors
    if kind == "string":
        if not isinstance(obj, str):
            return [f"{path}: expected string, got {type(obj).__name__}"]
        if len(obj) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: shorter than minLength {schema.get('minLength')}")
        return errors
    if kind == "integer":
        if not isinstance(obj, int) or isinstance(obj, bool):
            return [f"{path}: expected integer, got {type(obj).__name__}"]
    elif kind == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            return [f"{path}: expected number, got {type(obj).__name__}"]
    elif kind == "boolean":
        if not isinstance(obj, bool):
            return [f"{path}: expected boolean, got {type(obj).__name__}"]
    if kind in ("integer", "number") and not errors:
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    return errors


# ---------------------------------------------------------------------------
# field-path iteration (template-declared narrative/id fields)
# ---------------------------------------------------------------------------


def iter_field(output: dict, dotted: str):
    """Yield leaf values addressed by a template field path.

    Grammar: ``"summary"`` (top-level key) or ``"candidate_notes[].note"``
    (each element of a list field). Missing keys yield nothing — schema
    validation (stage 1) already enforced required presence.
    """
    head, _, rest = dotted.partition(".")
    if head.endswith("[]"):
        seq = output.get(head[:-2], [])
        if isinstance(seq, list):
            for item in seq:
                if rest and isinstance(item, dict):
                    yield from iter_field(item, rest)
                elif not rest:
                    yield item
    elif rest:
        child = output.get(head)
        if isinstance(child, dict):
            yield from iter_field(child, rest)
    elif head in output:
        yield output[head]


# ---------------------------------------------------------------------------
# stage 2 — candidate-id whitelist (LB-3)
# ---------------------------------------------------------------------------


def whitelist_ids(output: dict, template, allowed_ids: frozenset) -> list[str]:
    """LB-3 (cited law): the LLM ranks/explains only within the presented,
    ID-stable candidate set; ANY id outside it is a hard reject — one
    invented candidate = validation failure, zero tolerance.

    Checks (a) the template's declared id fields verbatim and (b) every
    task-id-shaped token inside the declared narrative fields.
    """
    reasons: list[str] = []
    for path in template.id_fields:
        for value in iter_field(output, path):
            if not isinstance(value, str) or value not in allowed_ids:
                reasons.append(f"invented_candidate:{value}")
    for path in template.narrative_fields:
        for text in iter_field(output, path):
            if not isinstance(text, str):
                continue
            for token in TASK_ID_RE.findall(text):
                if token not in allowed_ids:
                    reasons.append(f"invented_candidate:{token}")
    return reasons


# ---------------------------------------------------------------------------
# stage 3 — numeric-claim grounding (LB-4)
# ---------------------------------------------------------------------------


def collect_metrics(context) -> set:
    """Flatten every numeric leaf of the supplied context into a metric set.

    LB-4: these supplied metrics are the ONLY numbers a narrative may
    state. One allowed derived form: a fraction metric in [0, 1.5] may be
    narrated as its percent (x100) — e.g. attainment 0.75 as "75%".
    Numbers embedded inside context STRINGS (e.g. task names) are NOT
    metrics: quoting them is a reject (conservative rule).
    """
    metrics: set = set()

    def walk(node):
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            value = float(node)
            metrics.add(value)
            if 0.0 <= value <= 1.5:
                metrics.add(round(value * 100.0, 6))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(context)
    return metrics


def extract_claims(text: str) -> list[tuple[str, tuple]]:
    """Extract numeric claims from narrative text as (token, candidates).

    Task-id tokens are stripped first (their digits are ids, not claims —
    the whitelist stage owns them). Each claim yields the candidate values
    it could honestly mean: the literal value (with $/comma/K/M/B
    normalization) and, for ``%`` forms, also value/100.
    """
    cleaned = TASK_ID_RE.sub(" ", text)
    claims: list[tuple[str, tuple]] = []
    for match in _NUMBER_RE.finditer(cleaned):
        token = match.group(0)
        body = token.replace("$", "").replace(",", "").strip()
        percent = body.endswith("%")
        body = body.rstrip("%").strip()
        scale = 1.0
        if body and body[-1].lower() in _SUFFIX_SCALE:
            scale = _SUFFIX_SCALE[body[-1].lower()]
            body = body[:-1].strip()
        try:
            value = float(body) * scale
        except ValueError:
            claims.append((token, ()))  # undecidable -> conservative reject
            continue
        candidates = (value, value / 100.0) if percent else (value,)
        claims.append((token, candidates))
    return claims


def ground_numbers(output: dict, template, context, rel_tol: float | None = None) -> list[str]:
    """LB-4 numeric grounding: every number in every declared narrative
    field must match a supplied metric within ``rel_tol`` (default
    ``config.CLAIM_REL_TOL``) or be rejected as ``unsupported_claim``.

    Conservative failure = reject (L4-2 stage 5, verbatim rule): an
    unmatched or undecidable number rejects the whole output — the LB-8
    deterministic fallback renders instead.
    """
    tol = config.CLAIM_REL_TOL if rel_tol is None else float(rel_tol)
    metrics = collect_metrics(context)
    reasons: list[str] = []
    for path in template.narrative_fields:
        for text in iter_field(output, path):
            if not isinstance(text, str):
                continue
            for token, candidates in extract_claims(text):
                grounded = any(
                    abs(value - metric) <= max(tol * abs(metric), 1e-9)
                    for value in candidates
                    for metric in metrics
                )
                if not grounded:
                    reasons.append(f"unsupported_claim:{token.strip()}")
    return reasons


# ---------------------------------------------------------------------------
# stage 4 — uncertainty presence (LB-10)
# ---------------------------------------------------------------------------


def uncertainty_presence(output: dict) -> list[str]:
    """LB-10 (cited law): uncertainty is disclosed, never concealed —
    ``data_basis`` must name a real basis (non-empty, not "N/A") and
    ``confidence`` must be a number in [0, 1]. Schema guarantees the
    shape; this stage guarantees the substance.
    """
    reasons: list[str] = []
    basis = output.get("data_basis")
    if not isinstance(basis, str) or basis.strip().lower() in _EMPTY_BASES:
        reasons.append("data_basis_missing_or_empty")
    confidence = output.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
    ):
        reasons.append("confidence_missing_or_out_of_range")
    return reasons


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------


def validate_output(
    output: dict,
    template,
    context,
    allowed_ids: frozenset,
    rel_tol: float | None = None,
) -> dict:
    """Run all stages in order; return ``{"ok", "verdicts"}`` — never raise.

    LB-8: the caller renders deterministic content on ``ok=False``; this
    function is the wall between the model and the users, and it fails
    closed (any stage reject -> the whole output is rejected).
    """
    verdicts: list[dict] = []

    def stage(name: str, reasons: list[str]) -> bool:
        verdicts.append(
            {"stage": name, "passed": not reasons, "reasons": list(reasons)}
        )
        return not reasons

    ok = stage(STAGE_SCHEMA, check_schema(output, template.response_schema)
               if isinstance(output, dict)
               else [f"$: expected object, got {type(output).__name__}"])
    if ok:
        ok = stage(STAGE_WHITELIST, whitelist_ids(output, template, allowed_ids)) and ok
        ok = stage(STAGE_NUMERIC, ground_numbers(output, template, context, rel_tol)) and ok
        ok = stage(STAGE_UNCERTAINTY, uncertainty_presence(output)) and ok
    return {"ok": ok, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# audit log (LB-7)
# ---------------------------------------------------------------------------


def audit_append(path: str, record: dict) -> str:
    """Append one audit row to the JSONL log (LB-7 — every call recorded).

    Rows carry whatever the caller assembled (template id+version, input
    hash, output hash, verdicts / typed-error class) plus a UTC ``ts``
    stamp and the OR-5 ``mock_data`` label added here. Append is
    fsync'd so a crash never loses an acknowledged row; bodies are NEVER
    written (LLM_LOG_POLICY=metadata_only — hashes only).
    """
    row = dict(record)
    row.setdefault(
        "ts", datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    row.setdefault("mock_data", True)  # OR-5: all shipped data is synthetic
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def audit_rows(path: str) -> list[dict]:
    """Read the audit log back (tolerant: bad lines skipped, order kept)."""
    if not path or not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows
