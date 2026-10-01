"""Final answer normalization shared by simple and agentic systems."""

from __future__ import annotations

import hashlib
import inspect

from kodocs.agent.citations import validate_citations
from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.types import AnswerResult, ModelAnswer, RefuseReason

NO_ANSWER_TEXT = "제공된 자료에서 답을 찾을 수 없습니다."
_REFUSE_REASONS: frozenset[str] = frozenset(
    {
        "model_refused",
        "no_valid_citation",
        "parse_error",
        "guardrail_max_steps",
        "guardrail_tokens",
        "guardrail_timeout",
    }
)


def finalize_answer(
    model_answer: ModelAnswer | None,
    log: EvidenceLog,
    *,
    refuse_reason: RefuseReason | str | None = None,
) -> AnswerResult:
    """Validate citations and make every refusal externally indistinguishable."""
    if refuse_reason is not None and refuse_reason not in _REFUSE_REASONS:
        raise ValueError("unsupported refusal reason")

    proposed = () if model_answer is None else tuple(model_answer.citations)
    valid, dropped = validate_citations(proposed, log)
    trace: dict[str, object] = {
        "dropped_citations": [citation.model_dump(mode="json") for citation in dropped]
    }

    reason = refuse_reason
    if reason is None and model_answer is None:
        reason = "parse_error"
    elif reason is None and model_answer.refused:
        reason = "model_refused"
    elif reason is None and not valid:
        reason = "no_valid_citation"

    if reason is not None:
        return AnswerResult(
            answer=NO_ANSWER_TEXT,
            citations=(),
            refused=True,
            refuse_reason=reason,
            trace=trace,
        )
    return AnswerResult(
        answer=model_answer.answer,
        citations=valid,
        refused=False,
        refuse_reason=None,
        trace=trace,
    )


def finalizer_hash() -> str:
    """Fingerprint the answer-finalization logic that decides refusals and citations.

    Cached run rows are only comparable when this logic is unchanged, so the hash
    is part of every run cache key (a changed validator invalidates older rows).
    """
    source = "\n".join(
        (
            inspect.getsource(validate_citations),
            inspect.getsource(finalize_answer),
            NO_ANSWER_TEXT,
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
