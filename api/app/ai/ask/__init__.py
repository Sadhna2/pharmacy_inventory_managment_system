"""Natural-language questions answered as SQL over this database.

The model is never allowed to produce an answer. It produces a candidate query,
and deterministic code decides whether that query may run — the same division
of labour as `ai/intake`, where the model transcribes an invoice and
`intake/validate.py` decides whether the transcription is trustworthy.

`schema_context` is the other half of that bargain: a proposal can only be as
good as the briefing behind it, so the model is given the real schema, read out
of SQLAlchemy metadata, plus the domain facts no schema dump conveys.
"""

from app.ai.ask.schema_context import build_schema_context, invalidate_schema_context

__all__ = ["build_schema_context", "invalidate_schema_context"]
