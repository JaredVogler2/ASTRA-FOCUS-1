"""ff.llm.provider — LLMProvider protocol + Mock / Recorded / BCAI providers.

Wave-5 canon L1 adapted to FF_app (pure stdlib + flask; no requests):

- ``complete_structured(request, response_schema) -> dict`` is the ONLY
  entry point (LB-4: all LLM I/O is structured — there is no
  ``complete_text``). It returns a schema-valid dict or raises a typed
  exception; never a raw string, never a half-parsed object.
- Structured-output enforcement (L1-6, schema_in_prompt mode): parse ->
  validate -> re-prompt with the validation error, at most
  ``config.LLM_SCHEMA_RETRIES`` (default 2) times -> then raise
  :class:`LLMSchemaError` (typed failure; LB-8 callers render their
  deterministic content). Never best-effort, never partial.
- TWO retry budgets (L1 gotcha): the schema loop above is separate from
  the BCAI transport retry budget (``LLM_RETRIES``, timeout/connection
  errors ONLY — never auth, never parsed-but-invalid).
- Credential law (L1-4 / LB-9): the PAT lives ONLY in the server-side
  session; :class:`BCAIChatGPTProvider` receives it via a ``pat_supplier``
  callable evaluated PER CALL (the web layer passes a session reader) —
  never at construction, never from disk or committed env, never logged,
  never in cassettes.
- TLS is ALWAYS verified (LB-9): the default transport uses
  ``ssl.create_default_context()`` (optionally ``config.LLM_CA_BUNDLE``).
  The WATTS TLS-verification-off / warning-suppression pattern is BANNED
  repo-wide (tripwire: tests/test_llm.py grep test; GROUNDING §7).
- Logging policy (L1-5): metadata only — template id+version, outcome,
  hashes. Prompt/response bodies are NEVER logged by this module.

ALL tests and CI run on Mock/Recorded — no network call exists anywhere
in the test suite (the BCAI transport is injectable).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import config
from ff.llm.validators import canonical_hash, check_schema

logger = logging.getLogger("ff.llm")


# ---------------------------------------------------------------------------
# typed exception taxonomy (LB-8: typed failure is what makes degrade possible)
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class: every provider failure is typed so callers can degrade
    to deterministic content (LB-8) instead of string-sniffing replies."""


class LLMAuthError(LLMError):
    """401/403, missing/invalid PAT. NEVER retried (L1-2)."""


class LLMTimeoutError(LLMError):
    """Transport timeout (HTTP or in-band 'ERROR: Request timed out')."""


class LLMProtocolError(LLMError):
    """Unparseable envelope (bad NDJSON last line / missing choices path).
    Carries at most the first 200 bytes — never the full body (L1-5)."""


class LLMSchemaError(LLMError):
    """Model output failed schema validation after the bounded re-prompt
    loop (L1-6). The caller's LB-8 fallback renders."""


class LLMProviderError(LLMError):
    """Everything else: connection failures after retries, HTTP >= 400
    (non-auth), in-band ERROR strings with no more specific class."""


# ---------------------------------------------------------------------------
# request + protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """One structured LLM call (L1-1). ``template_id``/``template_version``
    make every call auditable (LB-7); messages are the rendered prompt."""

    messages: tuple  # of {"role": ..., "content": ...} dicts
    template_id: str
    template_version: str
    conversation_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class LLMProvider(Protocol):
    """The provider protocol (L1-1): structured completion or typed failure.

    LB-4 (cited law): there is deliberately NO complete_text method —
    unstructured returns are how WATTS ended up regex-scraping replies.
    """

    def complete_structured(self, request: LLMRequest, response_schema: dict) -> dict:
        """Return a dict validated against ``response_schema`` or raise LLMError."""
        ...  # pragma: no cover — Protocol body


# ---------------------------------------------------------------------------
# shared structured-output loop (L1-6 — one implementation, every provider)
# ---------------------------------------------------------------------------


def _parse_json_object(raw: str):
    """Parse a model reply into a JSON object; tolerate code fences only.

    Returns (obj|None, errors). Anything that is not exactly one JSON
    object is an error (LB-4: never best-effort parse).
    """
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0 and text.endswith("```"):
            text = text[first_nl + 1 : -3].strip()
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return None, [f"not valid JSON: {exc}"]
    if not isinstance(obj, dict):
        return None, [f"expected a JSON object, got {type(obj).__name__}"]
    return obj, []


def _structured_completion(generate, request: LLMRequest, response_schema: dict,
                           schema_retries: int) -> dict:
    """Parse -> validate -> bounded re-prompt loop -> typed failure (L1-6).

    ``generate(messages) -> str`` is the provider-specific raw completion.
    On validation failure the error list is fed back as a re-prompt at
    most ``schema_retries`` times (default config 2, the LB-8 bounded
    budget), after which :class:`LLMSchemaError` raises. Deterministic
    given a deterministic ``generate``.
    """
    messages = list(request.messages)
    last_errors: list[str] = ["no attempt made"]
    for _attempt in range(max(0, int(schema_retries)) + 1):
        raw = generate(tuple(messages))
        obj, errors = _parse_json_object(raw)
        if obj is not None:
            errors = check_schema(obj, response_schema)
            if not errors:
                logger.info(
                    "llm ok template=%s@%s output_sha=%s",
                    request.template_id, request.template_version,
                    canonical_hash(obj)[:16],
                )
                return obj
        last_errors = errors
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous reply failed validation: "
                    + "; ".join(last_errors)
                    + ". Reply again with ONLY one JSON object matching the schema."
                ),
            }
        )
    logger.info(
        "llm schema-failure template=%s@%s", request.template_id,
        request.template_version,
    )
    raise LLMSchemaError(
        f"output failed schema validation after {schema_retries} retries: "
        + "; ".join(last_errors)
    )


# ---------------------------------------------------------------------------
# MockLLMProvider — deterministic, offline, credential-free (L1-7)
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """Deterministic mock: schema-valid canned outputs, zero network.

    GROUNDED by construction: it reads the presented context back out of
    the rendered DATA block and narrates ONLY supplied ids/numbers, so its
    output passes the full LB-3/LB-4 validator pipeline the same way a
    compliant live model would. Modes (L1-7) exercise the failure paths:

    - ``"ok"`` (default): valid output first try;
    - ``"malformed_then_valid"``: first reply is not JSON, then valid —
      exercises the L1-6 bounded retry loop;
    - ``"always_malformed"``: every reply invalid — exercises the typed
      LLMSchemaError failure (LB-8);
    - ``"timeout"`` / ``"auth"`` / ``"error_inband"``: raise the mapped
      typed exceptions immediately.

    Deterministic: no RNG, no wall clock; identical request => identical
    output (``generate_calls`` counts attempts for the retry tests).
    """

    def __init__(self, mode: str = "ok", canned: dict | None = None):
        self.mode = mode
        self.canned = dict(canned or {})
        self.generate_calls = 0

    # -- grounded canned narratives (numbers/ids come from the context) ----
    @staticmethod
    def _explain_candidates(ctx: dict) -> dict:
        candidates = ctx.get("candidates") or []
        team = ctx.get("team")
        count = ctx.get("count", len(candidates))
        if candidates:
            top = candidates[0]
            summary = (
                f"{count} ready candidates supplied for team {team}. "
                f"Top-ranked candidate {top.get('candidate_id')} carries "
                f"score {top.get('score')} at rank {top.get('rank')}."
            )
            notes = [
                {
                    "candidate_id": c.get("candidate_id"),
                    "note": f"rank {c.get('rank')} with score {c.get('score')}",
                }
                for c in candidates[:3]
            ]
        else:
            summary = f"{count} ready candidates supplied for team {team}."
            notes = []
        return {
            "summary": summary,
            "candidate_notes": notes,
            "data_basis": "supplied ranked candidate metrics only (synthetic mock data)",
            "confidence": 0.9,
        }

    @staticmethod
    def _shift_recap(ctx: dict) -> dict:
        narrative = (
            f"Shift {ctx.get('shift')} recap for team {ctx.get('team')}, day "
            f"{ctx.get('day')}: attainment {ctx.get('attainment')} against a "
            f"goal of {ctx.get('goal')} points, with {ctx.get('earned')} "
            f"points earned. Badges earned this shift: "
            f"{ctx.get('badges_earned')}. Steady work; the board holds."
        )
        return {
            "narrative": narrative,
            "data_basis": "supplied deterministic recap fields only (synthetic mock data)",
            "confidence": 0.9,
        }

    def _valid_output(self, request: LLMRequest) -> dict:
        if request.template_id in self.canned:
            return dict(self.canned[request.template_id])
        from ff.llm import prompts  # lazy: prompts imports LLMRequest from here

        ctx = prompts.extract_data_block(request.messages) or {}
        if request.template_id == "explain_candidates":
            return self._explain_candidates(ctx)
        if request.template_id == "shift_recap_narrative":
            return self._shift_recap(ctx)
        # Unknown template: minimal LB-10-compliant object (schema-agnostic
        # canned outputs must be supplied via ``canned`` for other shapes).
        return {
            "data_basis": "supplied context (synthetic mock data)",
            "confidence": 0.5,
        }

    def complete_structured(self, request: LLMRequest, response_schema: dict) -> dict:
        """Structured completion via the SAME bounded loop as the live path
        (L1-6) so retry/typed-failure semantics are exercised identically."""
        if self.mode == "timeout":
            raise LLMTimeoutError("mock timeout")
        if self.mode == "auth":
            raise LLMAuthError("mock auth failure (401)")
        if self.mode == "error_inband":
            raise LLMProviderError("ERROR: mock in-band provider error")

        def generate(messages) -> str:
            self.generate_calls += 1
            if self.mode == "always_malformed":
                return "this is not json at all"
            if self.mode == "malformed_then_valid" and self.generate_calls == 1:
                return "{broken json"
            return json.dumps(self._valid_output(request), sort_keys=True)

        return _structured_completion(
            generate, request, response_schema, config.LLM_SCHEMA_RETRIES
        )


# ---------------------------------------------------------------------------
# RecordedResponseProvider — committed cassette replay (L1-7)
# ---------------------------------------------------------------------------


def request_input_hash(request: LLMRequest) -> str:
    """Cassette/audit key: hash of (template id, version, messages) — the
    full model-visible input, canonicalized (LB-7)."""
    return canonical_hash(
        [request.template_id, request.template_version, list(request.messages)]
    )


class RecordedResponseProvider:
    """Replay committed cassettes from ``ff/llm/cassettes/*.json``.

    Cassettes are keyed by (template_id, template_version, input_hash),
    committed, and carry ``recorded_at``/``model``/``mock_data`` stamps.
    They contain ONLY the model-visible request hash and the response —
    never headers, never a PAT (L1-4 scrub law: credentials are not even
    reachable from here). ``record=True`` + a ``delegate`` provider adds
    write-through recording (owner/dev sessions only). Replay is
    deterministic; a missing cassette is a typed failure (LB-8), never a
    fabricated response.
    """

    def __init__(self, cassette_dir: str, delegate=None, record: bool = False):
        self.cassette_dir = Path(cassette_dir)
        self.delegate = delegate
        self.record = bool(record)
        self._store: dict[tuple, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.cassette_dir.is_dir():
            return
        for path in sorted(self.cassette_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for row in doc.get("cassettes", []):
                key = (
                    row.get("template_id"),
                    row.get("template_version"),
                    row.get("input_hash"),
                )
                if all(key) and isinstance(row.get("response"), dict):
                    self._store[key] = row["response"]

    def _cassette_path(self, request: LLMRequest) -> Path:
        return self.cassette_dir / (
            f"{request.template_id}@{request.template_version}.json"
        )

    def _record(self, request: LLMRequest, key: tuple, response: dict) -> None:
        """Write-through recording (header-free by design; mock_data-stamped)."""
        path = self._cassette_path(request)
        doc = {"schema": 1, "mock_data": True, "cassettes": []}
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        doc.setdefault("cassettes", []).append(
            {
                "template_id": key[0],
                "template_version": key[1],
                "input_hash": key[2],
                "model": "recorded",
                "recorded_at": "recorded",  # no wall clock: cassettes stay byte-stable
                "response": response,
            }
        )
        self.cassette_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8"
        )

    def complete_structured(self, request: LLMRequest, response_schema: dict) -> dict:
        """Replay by input hash; validate on the way out (a drifted cassette
        raises LLMSchemaError rather than shipping stale invalid shapes)."""
        key = (request.template_id, request.template_version,
               request_input_hash(request))
        if key in self._store:
            response = json.loads(json.dumps(self._store[key]))  # defensive copy
            errors = check_schema(response, response_schema)
            if errors:
                raise LLMSchemaError(f"cassette response invalid: {'; '.join(errors)}")
            return response
        if self.record and self.delegate is not None:
            response = self.delegate.complete_structured(request, response_schema)
            self._store[key] = response
            self._record(request, key, response)
            return response
        raise LLMProviderError(
            f"no cassette for template={request.template_id}@"
            f"{request.template_version} input_hash={key[2][:16]}…"
        )


# ---------------------------------------------------------------------------
# BCAIChatGPTProvider — the WATTS transport, flaws removed (L1-2)
# ---------------------------------------------------------------------------


def _default_transport(url: str, body: bytes, headers: dict, timeout: float):
    """POST via urllib with TLS VERIFICATION ON (LB-9 — the WATTS
    verification-off + warning-suppression pattern is BANNED; GROUNDING §7).

    Returns (status_code, response_bytes); HTTP error statuses are
    returned (with their body) for the provider to map; timeout and
    connection failures propagate as OSError/TimeoutError for the
    transport retry loop.
    """
    import ssl
    import urllib.error
    import urllib.request

    context = ssl.create_default_context(cafile=config.LLM_CA_BUNDLE or None)
    req = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # non-2xx WITH a response
        return exc.code, exc.read()


class BCAIChatGPTProvider:
    """The WATTS ``bcai_client`` transport, verbatim-adapted (L1-2):

    KEPT (proven contract): envelope fields ``messages``,
    ``conversation_mode: ["default"]``, ``model``, ``conversation_guid``,
    ``stream: "false"`` (a STRING, per the proven envelope — a boolean may
    change server behavior), ``conversation_source``, ``use_case_id``,
    ``info_types``; auth header ``Authorization: basic <raw PAT>``
    (lowercase 'basic', raw token, NOT Base64); NDJSON last-line parse ->
    ``choices[0].message.content``; the validate_token ping.

    REMOVED (banned WATTS flaws, GROUNDING §7): the TLS-verification-off
    call + urllib3 warning suppression (TLS is verified, LB-9); hardcoded
    endpoint/model/use_case_id/conversation_source/info_types (ALL from
    config §11, env-overridable); in-band ``"ERROR:"`` strings returned
    as content (mapped to typed exceptions here — one missed
    startswith-check in a caller can never render an error to a user).

    Credential law (L1-4/LB-9): ``pat_supplier`` is a zero-arg callable
    (the web layer passes a server-session reader) evaluated per call.
    The PAT never appears in config, on disk, in logs, or in the request
    BODY (header only).
    """

    def __init__(
        self,
        *,
        pat_supplier,
        endpoint: str | None = None,
        model: str | None = None,
        use_case_id: str | None = None,
        conversation_source: str | None = None,
        info_types=None,
        timeout_s: float | None = None,
        retries: int | None = None,
        backoff_s: float | None = None,
        schema_retries: int | None = None,
        transport=None,
        sleep=time.sleep,
    ):
        self._pat_supplier = pat_supplier
        self.endpoint = endpoint if endpoint is not None else config.LLM_ENDPOINT
        self.model = model if model is not None else config.LLM_MODEL
        self.use_case_id = (
            use_case_id if use_case_id is not None else config.LLM_USE_CASE_ID
        )
        self.conversation_source = (
            conversation_source
            if conversation_source is not None
            else config.LLM_CONVERSATION_SOURCE
        )
        self.info_types = list(
            info_types if info_types is not None else config.LLM_INFO_TYPES
        )
        self.timeout_s = float(timeout_s if timeout_s is not None else config.LLM_TIMEOUT_S)
        self.retries = int(retries if retries is not None else config.LLM_RETRIES)
        self.backoff_s = float(
            backoff_s if backoff_s is not None else config.LLM_RETRY_BACKOFF_S
        )
        self.schema_retries = int(
            schema_retries if schema_retries is not None else config.LLM_SCHEMA_RETRIES
        )
        self._transport = transport or _default_transport
        self._sleep = sleep

    # -- envelope (every field from config; nothing hardcoded, L1-2) --------
    def _body(self, messages, conversation_guid: str) -> dict:
        return {
            "messages": list(messages),
            "conversation_mode": ["default"],
            "model": self.model,
            "conversation_guid": conversation_guid,
            "stream": "false",  # STRING literal, per the proven WATTS contract
            "conversation_source": self.conversation_source,
            "use_case_id": self.use_case_id,
            "info_types": list(self.info_types),
        }

    @staticmethod
    def _headers(pat_token: str) -> dict:
        # Raw PAT, lowercase 'basic' — NOT Base64 Basic auth (L1 gotcha).
        return {
            "accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"basic {pat_token}",
        }

    def _post(self, messages, conversation_guid: str, pat_token: str,
              timeout: float) -> str:
        """One transport exchange with the L1-2 retry budget: timeout/
        connection errors ONLY are retried (exponential backoff); auth is
        never retried; parse failures are never retried here (that is the
        schema loop's separate budget)."""
        body = json.dumps(self._body(messages, conversation_guid)).encode("utf-8")
        headers = self._headers(pat_token)
        last_exc: LLMError | None = None
        for attempt in range(self.retries + 1):
            try:
                status, raw = self._transport(self.endpoint, body, headers, timeout)
            except TimeoutError as exc:  # socket.timeout is an alias (3.10+)
                last_exc = LLMTimeoutError(f"transport timeout: {exc}")
            except OSError as exc:  # URLError/ConnectionError family
                last_exc = LLMProviderError(f"connection error: {exc}")
            else:
                return self._parse(status, raw)
            if attempt < self.retries:
                self._sleep(self.backoff_s * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def _parse(self, status: int, raw: bytes) -> str:
        """Status + NDJSON handling; in-band ERROR strings become typed
        exceptions, NEVER content (the WATTS flaw this class removes)."""
        if status in (401, 403):
            raise LLMAuthError(f"BCAI auth failure (HTTP {status})")
        if status >= 400:
            raise LLMProviderError(
                f"BCAI HTTP {status}: {raw[:200]!r}"
            )
        text = raw.decode("utf-8", errors="replace")
        lines = text.strip().split("\n")  # NDJSON: parse the LAST line
        try:
            doc = json.loads(lines[-1])
            content = doc["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProtocolError(
                f"unparseable BCAI response ({exc}): {text[:200]!r}"
            )
        if isinstance(content, str) and content.startswith("ERROR:"):
            low = content.lower()
            if "timed out" in low or "timeout" in low:
                raise LLMTimeoutError(content[:200])
            if ("401" in low or "403" in low or "unauthorized" in low
                    or "forbidden" in low or "token" in low):
                raise LLMAuthError(content[:200])
            raise LLMProviderError(content[:200])
        if not isinstance(content, str):
            raise LLMProtocolError(f"non-string content: {type(content).__name__}")
        return content

    def complete_structured(self, request: LLMRequest, response_schema: dict) -> dict:
        """Structured completion: session PAT -> transport -> shared L1-6
        parse-validate-retry loop -> typed failure. LB-9: no PAT => typed
        LLMAuthError with ZERO network attempts."""
        pat = str(self._pat_supplier() or "").strip()
        if not pat:
            raise LLMAuthError("no PAT available in server session (LB-9)")
        guid = request.conversation_id or (
            f"focu5-{request.template_id}-{request_input_hash(request)[:12]}"
        )

        def generate(messages) -> str:
            return self._post(messages, guid, pat, self.timeout_s)

        return _structured_completion(
            generate, request, response_schema, self.schema_retries
        )

    def validate_token(self, pat_token: str) -> bool:
        """The WATTS ping pattern verbatim (minimal 'Reply with OK.' /
        'ping' conversation, short config timeout): False on auth failure
        or ANY error, True on a 2xx exchange. Used by an auth route to
        vet a PAT before storing it in the server session (LB-9)."""
        messages = (
            {"role": "system", "content": "Reply with OK."},
            {"role": "user", "content": "ping"},
        )
        try:
            self._post(
                messages,
                "focu5-auth-ping",
                str(pat_token or "").strip(),
                float(config.LLM_PING_TIMEOUT_S),
            )
            return True
        except LLMError:
            return False
