"""User-run paid Gemini settings probe; --dry-run never calls the API."""

import argparse
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from kodocs.config import load_settings
from kodocs.llm.gemini_chat import (
    BillingUncertainError,
    FatalLLMError,
    GeminiGrader,
    GradeError,
    GradeParseError,
    GradeRequest,
    GradeStopError,
    create_client,
    sdk_evidence,
)
from kodocs.llm.grade_settings import load_grade_config


class ProbeScore(BaseModel):
    accuracy: int = Field(ge=0, le=3, strict=True)
    faithfulness: int = Field(ge=0, le=3, strict=True)
    rationale: str = Field(max_length=200)


CONFIRMED_BASIS = (
    "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash",
    "https://ai.google.dev/gemini-api/docs/gemini-3",
)
CHECKED_ON = "2026-10-01"


def _call(records, name):
    return next((r for r in records if r["name"] == name), None)


def _minimal_summary(records):
    entry = _call(records, "minimal_check")
    if entry is None:
        return {"ran": False}
    return {
        "ran": True,
        "supported": bool(entry["accepted"]),
        "error_type": entry["error_type"],
        "status_code": entry["status_code"],
        "sdk_error_type": entry.get("sdk_error_type"),
    }


def _repeat_summary(records):
    first, second = _call(records, "baseline"), _call(records, "repeat")
    if first is None or second is None or not (first["parsed"] and second["parsed"]):
        return {"compared": False}
    a, b = first["scores"], second["scores"]
    return {
        "compared": True,
        "scores_equal": all(a[key] == b[key] for key in ("accuracy", "faithfulness")),
        "rationale_equal": a["rationale"] == b["rationale"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    # Confirmed settings (docs, 2026-10-01): thinking low, temperature/seed never sent.
    config = replace(load_grade_config(), temperature=None, seed=None, max_attempts=1)
    plans = (
        ("baseline", config),
        ("minimal_check", replace(config, thinking_level="minimal")),
        ("repeat", config),
    )
    request = GradeRequest(
        "m7-probe",
        "Return JSON integer accuracy and faithfulness scores (0 to 3).",
        "Reference: 2+2=4. Answer: 2+2=4. Evidence: 2+2=4.",
        ProbeScore,
    )
    settings = load_settings(env={}) if args.dry_run else load_settings()
    grader = GeminiGrader(config, settings)
    estimate = grader.estimate(request)
    # All three calls must fit before the first user-triggered send.
    grader.check_budget(
        replace(estimate, maximum_cost_usd=estimate.maximum_cost_usd * 3)
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "calls": [
                        {"name": name, "settings": candidate.record()}
                        for name, candidate in plans
                    ],
                    "maximum_cost_usd": estimate.maximum_cost_usd * 3,
                    "estimated_input_tokens_per_call": estimate.estimated_input_tokens,
                    "sdk": sdk_evidence(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    grader = GeminiGrader(
        config,
        settings,
        client=create_client(config, settings.require_key("GEMINI_API_KEY")),
    )
    records = []
    stopped = False
    for name, candidate in plans:
        grader.config = candidate
        entry = {
            "name": name,
            "settings": candidate.record(),
            "settings_hash": candidate.settings_hash(),
            "accepted": False,
            "error_type": None,
            "status_code": None,
            "parsed": False,
            "usage_fields": [],
            "thinking_tokens_used": None,
            "model_version": None,
            "response_id": None,
            "scores": None,
        }
        try:
            result = grader.grade(request)
        except GradeError as exc:
            entry["error_type"] = type(exc).__name__
            entry["sdk_error_type"] = (
                exc.attempts[-1].error_type if exc.attempts else None
            )
            entry["status_code"] = exc.status_code
            entry["accepted"] = isinstance(exc, GradeParseError)
            if isinstance(exc, GradeParseError) and hasattr(exc, "usage"):
                entry.update(
                    {
                        "usage_fields": [
                            "prompt_token_count",
                            "candidates_token_count",
                            "thoughts_token_count",
                            "total_token_count",
                        ],
                        "usage": asdict(exc.usage),
                        "thinking_tokens_used": exc.usage.thoughts_token_count > 0,
                        "model_version": exc.model_version,
                    }
                )
            stopped = isinstance(exc, (GradeStopError, BillingUncertainError)) or (
                isinstance(exc, FatalLLMError) and exc.status_code != 400
            )
        else:
            entry.update(
                {
                    "accepted": True,
                    "parsed": True,
                    "usage_fields": [
                        "prompt_token_count",
                        "candidates_token_count",
                        "thoughts_token_count",
                        "total_token_count",
                    ],
                    "usage": asdict(result.usage),
                    "thinking_tokens_used": result.usage.thoughts_token_count > 0,
                    "model_version": result.model_record["model_version"],
                    "response_id": result.request_id,
                    "scores": result.parsed.model_dump(),
                }
            )
        records.append(entry)
        if stopped:
            break
    document = {
        "ts": datetime.now(UTC).isoformat(),
        "sdk": sdk_evidence(),
        "calls": records,
        "stopped": stopped,
        "confirmed_settings": {
            "thinking_level": "low",
            "temperature": None,
            "seed": None,
            "basis": list(CONFIRMED_BASIS),
            "checked_on": CHECKED_ON,
            "note": "확정값은 minimal_check 결과와 무관하게 low(문서상 최저 수준)이다.",
        },
        "minimal_check": _minimal_summary(records),
        "repeat_comparison": _repeat_summary(records),
    }
    target = Path("data/manifest/m7_probe.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Probe metadata written: {target}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
