"""ff.llm — the constrained LLM layer (Wave-5 canon L1/L2/L4, adapted).

The LLM in FF_app is a NARRATOR, never an author (WATTS
``recommendation_engine`` precedent: Python computes, the LLM narrates):

- LB-1/LB-5: no schedule authorship, no tools, no writes — the ONLY LLM
  surface in the whole application is ``GET /api/v1/explain/candidates``,
  which is read-only and degrades to deterministic content.
- LB-3/LB-4: every output passes ``ff.llm.validators`` — candidate-id
  whitelist and numeric-claim grounding are hard rejects.
- LB-6: operational free text enters prompts only through
  ``prompts.sanitize_untrusted_text`` inside delimited DATA blocks.
- LB-7: every call + verdict is appended to ``data/llm_audit.jsonl``.
- LB-8: provider/validation failure is a typed, bounded event; every
  surface renders its deterministic content regardless.
- LB-9: BCAI transport is TLS-verified (verification-off is BANNED —
  tripwire grep test), PAT comes from the server session only,
  config-driven.
- LB-10: ``data_basis`` + ``confidence`` are REQUIRED in every schema.

Modules: ``provider`` (LLMProvider protocol + Mock/Recorded/BCAI),
``prompts`` (versioned template assets + sanitizer + context builders),
``validators`` (post-response pipeline + audit log). Committed replay
fixtures live in ``ff/llm/cassettes/``.
"""

from ff.llm.provider import (  # noqa: F401  (package facade)
    BCAIChatGPTProvider,
    LLMAuthError,
    LLMError,
    LLMProtocolError,
    LLMProvider,
    LLMProviderError,
    LLMRequest,
    LLMSchemaError,
    LLMTimeoutError,
    MockLLMProvider,
    RecordedResponseProvider,
)
