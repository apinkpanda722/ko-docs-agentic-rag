"""Thread-safe, secret-safe JSONL tracing for answer executions."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from kodocs.agent.types import AnswerResult
from kodocs.tools import ToolResult, UserContext

TRACE_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()
_TOKEN_KEYS = {
    "accesstoken",
    "authorization",
    "bearertoken",
    "credential",
    "credentials",
    "apikey",
    "openaikey",
    "openaiapikey",
    "refreshtoken",
    "secret",
    "token",
}
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")
_OPENAI_KEY_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]+")
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_TOKEN_FIELDS = ("input", "cached_input", "cache_write", "output", "reasoning")


class TraceRecorder:
    """Create per-question sessions and append each result with one write."""

    def __init__(
        self,
        trace_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.trace_dir = trace_dir
        self._clock = clock
        self._now = now

    def start(
        self,
        *,
        question_id: str,
        system: Literal["simple", "agentic"],
        user: UserContext,
        model_record: Mapping[str, object],
        hashes: Mapping[str, object],
        trace_id: str | None = None,
    ) -> TraceSession:
        """Start collecting a trace without persisting the question text."""
        return TraceSession(
            recorder=self,
            question_id=question_id,
            system=system,
            user=user,
            model_record=model_record,
            hashes=hashes,
            trace_id=trace_id or uuid.uuid4().hex,
        )

    session = start

    def _append(self, payload: Mapping[str, object]) -> Path:
        line = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        path = self.trace_dir / f"{self._now():%Y-%m-%d}.jsonl"
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as target:
                target.write(line)
        return path


class TraceSession:
    """Collect steps for one question and finalize one JSONL record."""

    def __init__(
        self,
        *,
        recorder: TraceRecorder,
        question_id: str,
        system: Literal["simple", "agentic"],
        user: UserContext,
        model_record: Mapping[str, object],
        hashes: Mapping[str, object],
        trace_id: str,
    ) -> None:
        if system not in {"simple", "agentic"}:
            raise ValueError("trace system must be simple or agentic")
        self._recorder = recorder
        self._started = recorder._clock()
        self._base: dict[str, object] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": trace_id,
            "question_id": question_id,
            "system": system,
            "user": {"team": user.team, "grade": user.grade},
            "model": _model_trace(model_record),
            "hashes": _sanitize(hashes),
        }
        self._steps: list[dict[str, object]] = []
        self._timings: dict[str, float | None] = {
            "embed_ms": None,
            "search_ms": None,
            "ttft_ms": None,
            "total_ms": None,
        }
        self._steps_used = 0
        self._tokens_used = 0
        self._written_path: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        if self._written_path is not None:
            return False
        if exc is not None:
            self._write(
                result=None,
                guardrail=None,
                status="error",
                error=exc_type.__name__ if exc_type is not None else type(exc).__name__,
            )
        else:
            self._write(
                result=None,
                guardrail=None,
                status="error",
                error="TraceNotFinalized",
            )
        return False

    def add_model_step(
        self,
        name: str,
        *,
        latency_ms: float,
        tokens: Mapping[str, int] | None = None,
        cost_usd: float = 0.0,
        error: BaseException | str | None = None,
        ttft_ms: float | None = None,
    ) -> None:
        """Record model metadata without response content or request secrets."""
        token_record = _token_record(tokens)
        self._tokens_used += sum(token_record.values())
        self._steps.append(
            {
                "index": len(self._steps) + 1,
                "kind": "model",
                "name": name,
                "args": {},
                "latency_ms": float(latency_ms),
                "tokens": token_record,
                "cost_usd": float(cost_usd),
                "error": _error_name(error),
            }
        )
        if ttft_ms is not None and self._timings["ttft_ms"] is None:
            self._timings["ttft_ms"] = float(ttft_ms)

    def add_tool_step(
        self,
        name: str,
        args: Mapping[str, object],
        result: ToolResult,
        *,
        latency_ms: float,
        error: BaseException | str | None = None,
    ) -> None:
        """Record refs and serialized size, never the retrieval result body."""
        self._steps_used += 1
        tool_error = error
        if tool_error is None and result.is_error:
            tool_error = _tool_error_code(result.payload)
        self._steps.append(
            {
                "index": len(self._steps) + 1,
                "kind": "tool",
                "name": name,
                "args": _sanitize(args),
                "latency_ms": float(latency_ms),
                "tokens": _token_record(None),
                "cost_usd": 0.0,
                "error": _error_name(tool_error),
                "refs": _sanitize(result.refs),
                "result_chars": len(
                    json.dumps(
                        result.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            }
        )
        if result.timings:
            for key in ("embed_ms", "search_ms"):
                value = result.timings.get(key)
                if value is not None:
                    previous = self._timings[key]
                    self._timings[key] = float(value) + (previous or 0.0)

    def finish(
        self,
        result: AnswerResult,
        *,
        guardrail: Mapping[str, int] | None = None,
        status: str = "completed",
    ) -> Path:
        """Persist the completed trace once and return its dated path."""
        return self._write(
            result=result,
            guardrail=guardrail,
            status=status,
            error=None,
        )

    def _write(
        self,
        *,
        result: AnswerResult | None,
        guardrail: Mapping[str, int] | None,
        status: str,
        error: BaseException | str | None,
    ) -> Path:
        if self._written_path is not None:
            raise RuntimeError("trace session has already been recorded")
        timings = dict(self._timings)
        timings["total_ms"] = max(
            0.0,
            (self._recorder._clock() - self._started) * 1000,
        )
        citations = []
        refused = True
        refuse_reason = None
        if result is not None:
            citations = [
                citation.model_dump(mode="json") for citation in result.citations
            ]
            refused = result.refused
            refuse_reason = result.refuse_reason
        guardrail_record = (
            {
                "steps_used": self._steps_used,
                "tokens_used": self._tokens_used,
            }
            if guardrail is None
            else {
                "steps_used": int(guardrail["steps_used"]),
                "tokens_used": int(guardrail["tokens_used"]),
            }
        )
        payload = {
            **self._base,
            "steps": self._steps,
            "timings": timings,
            "citations": citations,
            "refused": refused,
            "refuse_reason": refuse_reason,
            "guardrail": guardrail_record,
            "status": status,
            "error": _error_name(error),
        }
        self._written_path = self._recorder._append(_sanitize(payload))
        return self._written_path


def _model_trace(record: Mapping[str, object]) -> dict[str, object]:
    safe = _sanitize(record)
    if not isinstance(safe, dict):
        raise TypeError("model record must be an object")
    identifier = safe.get("model", safe.get("id"))
    settings = safe.get("settings", safe)
    settings_hash = safe.get("settings_hash")
    if not isinstance(settings_hash, str):
        settings_hash = hashlib.sha256(
            json.dumps(
                settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return {"id": identifier, "settings": settings, "settings_hash": settings_hash}


def _token_record(tokens: Mapping[str, int] | None) -> dict[str, int]:
    source = {} if tokens is None else tokens
    result: dict[str, int] = {}
    for name in _TOKEN_FIELDS:
        value = source.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"trace token field {name} must be non-negative")
        result[name] = value
    return result


def _tool_error_code(payload: Mapping[str, object]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
        return error["code"]
    return "ToolError"


def _error_name(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return type(error).__name__
    return error if _ERROR_TYPE.fullmatch(error) else "Error"


def _sanitize(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = "".join(
                character for character in key.casefold() if character.isalnum()
            )
            if normalized in _TOKEN_KEYS or normalized.endswith("apikey"):
                result[key] = "[REDACTED]"
            else:
                result[key] = _sanitize(item)
        return result
    if isinstance(value, tuple | list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _OPENAI_KEY_VALUE.sub(
            "[REDACTED]",
            _BEARER_VALUE.sub("[REDACTED]", value),
        )
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)
