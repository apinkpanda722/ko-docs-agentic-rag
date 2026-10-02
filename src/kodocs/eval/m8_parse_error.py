"""Offline M7 parse-error inventory; no answer text or causal guesses are emitted."""

import json
import math
import re
from collections import Counter
from pathlib import Path

from kodocs.eval.m7_rawhash import verify_fingerprint

RAW = Path("data/interim/m7/latency/runs.jsonl")
LATENCY = Path("data/manifest/m7_latency.json")
OUTPUT = Path("data/manifest/m8_parse_error.json")
LEDGER = Path("data/manifest/costs.jsonl")
LATENCY_TOLERANCE_S = 0.25
CAUSE_NOTE = "왜 길어졌는지는 원시 출력 미보존으로 미확정. 스트리밍 여부 자체는 원인이 아님(스트리밍에서도 strict 구조화 스키마를 전송함 — 설치된 langchain-openai 소스 확인)."
DEFINITION = "run_id·request_id로 묶는다. input/cached_input/cache_write/output 네 비용 줄의 usage는 동일한 요청의 반복 기록이므로 output_tokens·reasoning_tokens는 일치 검사 후 한 번만 센다(합산하지 않음). reasoning은 output의 부분량이다. amount_usd만 네 줄 합산. 요청 순서는 원장의 첫 등장 순서. trace의 질문·시스템·검색/임베딩 지연을 일치시킨 전체 실행 순서의 모델 단계 지연 벡터를 원장과 대조하며 유일한 연속 정렬만 연결한다. warmup 접두 요청은 벡터 정렬로 제외한다."
M7_REPORT = Path("docs/results/2026-10-02-m7-answer-eval.md")


def load_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def correlated(row, trace):
    # The latency raw rows have no trace_id. Require both measured segments to
    # match (seconds vs milliseconds), rather than joining by question alone.
    if any(
        row.get(k) != trace.get(k) for k in ("question_id", "system", "refuse_reason")
    ):
        return False
    timings = trace.get("timings", {})
    if any(
        row.get(k + "_s") is None or timings.get(k + "_ms") is None
        for k in ("embed", "search")
    ):
        # SQL-only turns have no retrieval timings. Require an independently
        # observed TTFT and total latency, never join absent segments alone.
        return (
            all(
                row.get(k + "_s") is None and timings.get(k + "_ms") is None
                for k in ("embed", "search")
            )
            and isinstance(row.get("ttft_s"), (int, float))
            and isinstance(timings.get("ttft_ms"), (int, float))
            and math.isclose(row["ttft_s"] * 1000, timings["ttft_ms"], abs_tol=1e-6)
            and isinstance(row.get("total_s"), (int, float))
            and isinstance(timings.get("total_ms"), (int, float))
            and abs(row["total_s"] * 1000 - timings["total_ms"])
            <= LATENCY_TOLERANCE_S * 1000
        )
    return all(
        isinstance(row.get(k + "_s"), (int, float))
        and isinstance(timings.get(k + "_ms"), (int, float))
        and math.isclose(row[k + "_s"] * 1000, timings[k + "_ms"], abs_tol=1e-6)
        for k in ("embed", "search")
    )


def ledger_requests(rows):
    grouped = {}
    for row in rows:
        request_id = row.get("request_id")
        if not request_id:
            continue
        key = (row["run_id"], request_id)
        usage = row.get("usage", {})
        record = {
            "run_id": row["run_id"],
            "request_id": request_id,
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "latency_ms": row.get("latency_ms"),
        }
        if key not in grouped:
            grouped[key] = {**record, "amount_usd": 0.0}
        elif any(grouped[key][k] != v for k, v in record.items()):
            raise ValueError("원장 요청 메타데이터 불일치")
        grouped[key]["amount_usd"] += float(row.get("amount_usd", 0))
    return list(grouped.values())


def order_links(rows, traces, requests):
    matched = [[t for t in traces if correlated(row, t)] for row in rows]
    if any(len(m) != 1 for m in matched):
        return {}
    selected = [m[0] for m in matched]
    positions = [next(i for i, t in enumerate(traces) if t is m) for m in selected]
    if positions != sorted(set(positions)):
        return {}
    steps = [
        (row, step)
        for row, trace in zip(rows, selected, strict=True)
        for step in trace.get("steps", [])
        if step.get("kind") == "model"
    ]
    if not steps or len(steps) > len(requests):
        return {}

    def agrees(step, request):
        latency = step.get("latency_ms")
        return (
            isinstance(latency, (int, float))
            and isinstance(request.get("latency_ms"), (int, float))
            and abs(latency - request["latency_ms"]) <= LATENCY_TOLERANCE_S * 1000
        )

    offsets = [
        start
        for start in range(len(requests) - len(steps) + 1)
        if all(agrees(step, requests[start + i]) for i, (_, step) in enumerate(steps))
    ]
    if len(offsets) != 1:
        return {}
    return {
        id(row): requests[offsets[0] + i]
        for i, (row, step) in enumerate(steps)
        if step.get("name") == "final_answer"
    }


def comparison_stats(requests, maximum, excluded=()):
    """Ledger output sizes per run.

    Only the streaming M7 latency run bills a parse failure (its request is
    recorded before the failure is raised). The frozen non-streaming M6/M7 path
    raises before ``_record_usage``, so its ledger rows are SUCCESSFUL responses
    only: their maximum is not evidence about truncation.
    """
    result = {}
    for run in ("m7-latency", "m6-dev", "m6-test-01", "m7-security"):
        group = [r for r in requests if r["run_id"] == run]
        outputs = [
            r["output_tokens"] for r in group if isinstance(r["output_tokens"], int)
        ]
        if run == "m7-latency":
            remaining = [
                r["output_tokens"]
                for r in group
                if r["request_id"] not in excluded
                and isinstance(r["output_tokens"], int)
            ]
            result[run] = {
                "count": len(group),
                "records_parse_failures": True,
                "max_output_tokens": max(outputs, default=None),
                "max_output_tokens_reached_count": sum(v >= maximum for v in outputs),
                "remaining_max_output_tokens": max(remaining, default=None),
            }
        else:
            result[run] = {
                "count": len(group),
                "records_parse_failures": False,
                "max_successful_output_tokens": max(outputs, default=None),
            }
    return result


NON_STREAM_THRESHOLD_S = 15
NON_STREAM_TOKENS_NOTE = "토큰 미기록(원장 누락 경로)"
NON_STREAM_DEFINITION = (
    "trace의 final_answer 단계가 ChatParseError이고 그 시도 기록이 error_type=ValidationError·status=parse_error인 것"
    "(비스트리밍 SDK 파싱이 예외로 끝난 경로; 스트리밍 파싱 실패는 시도 status가 success이고 error_type이 없다). "
    "재현 실행 경로 A의 실패는 재현 manifest와 질문·시스템·총 지연(±0.25초)이 같은 trace를 제외한다. "
    "분류는 final_answer 지연 15초 이상이면 '폭주 의심(지연 근거)', 아니면 '폭주 아님(지연 근거)'이며 토큰·원시 출력으로 확인한 값이 아니다."
)


def non_streaming_parse_failures(traces, repro_runs=()):
    """Non-streaming parse failures that never reached the cost ledger."""
    items = []
    for trace in traces:
        for step in trace.get("steps", []):
            if (
                step.get("name") != "final_answer"
                or step.get("error") != "ChatParseError"
            ):
                continue
            if not any(
                a.get("error_type") == "ValidationError"
                and a.get("status") == "parse_error"
                for a in step.get("attempts") or []
            ):
                continue
            total_ms = (trace.get("timings") or {}).get("total_ms")
            if isinstance(total_ms, (int, float)) and any(
                r["question_id"] == trace.get("question_id")
                and r["system"] == trace.get("system")
                and abs(r["latency_s"] - total_ms / 1000) <= LATENCY_TOLERANCE_S
                for r in repro_runs
            ):
                continue
            latency_s = step["latency_ms"] / 1000
            items.append(
                {
                    "question_id": trace.get("question_id"),
                    "system": trace.get("system"),
                    "date": trace.get("_date"),
                    "final_answer_latency_s": latency_s,
                    "tokens": NON_STREAM_TOKENS_NOTE,
                    "classification": "폭주 의심(지연 근거)"
                    if latency_s >= NON_STREAM_THRESHOLD_S
                    else "폭주 아님(지연 근거)",
                }
            )
    return sorted(items, key=lambda i: (i["date"] or "", i["question_id"] or ""))


def analyze(
    rows,
    *,
    expected,
    model,
    traces=(),
    m6_rows=(),
    ledger=(),
    max_output_tokens=4000,
    repro_runs=(),
):
    requests = ledger_requests(ledger)
    latency_requests = [r for r in requests if r["run_id"] == "m7-latency"]
    order = order_links(rows, traces, latency_requests)
    items = []
    excluded = set()
    for row in rows:
        if row.get("refuse_reason") != "parse_error":
            continue
        matched = [t for t in traces if correlated(row, t)]
        generic = any(
            s.get("error") == "ChatParseError"
            for t in matched
            for s in t.get("steps", [])
        )
        request_ids = {
            s.get("request_id")
            for t in matched
            for s in t.get("steps", [])
            if s.get("name") == "final_answer" and s.get("request_id")
        }
        linked = [r for r in latency_requests if r["request_id"] in request_ids]
        request = linked[0] if len(request_ids) == len(linked) == 1 else None
        link_method = "request_id" if request else "unlinked"
        if not request_ids and request is None and id(row) in order:
            request = order[id(row)]
            link_method = "order_and_latency"
        reached = (
            request is not None
            and isinstance(request["output_tokens"], int)
            and request["output_tokens"] >= max_output_tokens
        )
        if request:
            excluded.add(request["request_id"])
        # ChatParseError and zero token counters do not establish empty output,
        # truncation, schema violation or a stream interruption. No raw output or
        # diagnostic message/length was retained in the observed record schema.
        reason = (
            "ChatParseError 유형만 기록; 오류 메시지·원시 출력·출력 길이 없음"
            if generic
            else "원인 판정 기록 부족; 오류 메시지·원시 출력·출력 길이 없음"
        )
        reference = [
            r
            for r in m6_rows
            if all(r.get(k) == row.get(k) for k in ("question_id", "system"))
        ]
        items.append(
            {
                "question_id": row["question_id"],
                "system": row["system"],
                "model": model,
                "classification": "max_output_tokens_reached"
                if reached
                else "indeterminate",
                "reason": "max_output_tokens 도달(JSON 잘림)" if reached else reason,
                "link_method": link_method,
                "output_tokens": request["output_tokens"] if request else None,
                "max_output_tokens": max_output_tokens,
                "reasoning_tokens": request["reasoning_tokens"] if request else None,
                "latency_s": request["latency_ms"] / 1000
                if request and request["latency_ms"] is not None
                else None,
                "answer_latency_s": row.get("total_s"),
                "matched_traces": len(matched),
                "m6_parse_error": any(
                    r.get("refuse_reason") == "parse_error" for r in reference
                )
                if reference
                else None,
            }
        )
    return {
        "schema_version": 1,
        "source": RAW.as_posix(),
        "reported_count": expected,
        "count": len(items),
        "count_matches": len(items) == expected,
        "class_counts": dict(Counter(i["classification"] for i in items)),
        "items": items,
        "definition": DEFINITION,
        "strict_schema_source": "langchain_openai/chat_models/base.py:4685-4720 (_construct_responses_api_payload; pydantic stream uses strict=True)",
        "latency_tolerance_s": LATENCY_TOLERANCE_S,
        "cause_note": CAUSE_NOTE,
        "comparisons": comparison_stats(requests, max_output_tokens, excluded),
        "m6_note": "비스트리밍 M6는 참고 정보이며 분류 입력이 아니다.",
        "non_streaming_parse_failures": {
            "definition": NON_STREAM_DEFINITION,
            "threshold_s": NON_STREAM_THRESHOLD_S,
            "ledger_note": "M6·M7 비스트리밍 평가 실행과 재현 경로 A에서 파싱 실패한 요청은 원장에 기록되지 않는다(고정 경로가 비용 기록 전에 예외를 던짐). 비스트리밍 행의 원장 최대 출력은 성공한 응답의 값일 뿐이다.",
            "items": non_streaming_parse_failures(traces, repro_runs),
        },
    }


def build(root=Path(".")):
    root = Path(root)
    raw_hash = verify_fingerprint(root / LATENCY, "latency", root / RAW)
    report = (root / M7_REPORT).read_text()
    match = re.search(r"지연 한계 2[^\n]*?parse_error (\d+)건", report)
    if not match:
        raise ValueError("M7 지연 parse_error 보고 건수 없음")
    latency = json.loads((root / LATENCY).read_text())
    traces = [
        {**row, "_date": path.stem}
        for path in sorted((root / "logs/traces").glob("*.jsonl"))
        for row in load_rows(path)
    ]
    repro_path = root / "data/manifest/m8_stream_repro.json"
    repro_runs = (
        [
            r
            for entry in json.loads(repro_path.read_text())["history"]
            for r in entry["runs"]
            if r.get("path") == "A"
            and r["status"] == "parse_error"
            and isinstance(r.get("latency_s"), (int, float))
        ]
        if repro_path.exists()
        else []
    )
    m6 = [
        row
        for path in sorted((root / "data/interim/m6/test/run-01").rglob("runs.jsonl"))
        for row in load_rows(path)
    ]
    # The fixed answer model is available independently of optional traces.
    config = json.loads((root / "data/manifest/m6_config.json").read_text())["answer"]
    if config["settings_hash"] != latency["model_settings_hash"]:
        raise ValueError("M7 모델 설정 해시와 M6 고정 모델 불일치")
    traces = [
        t
        for t in traces
        if isinstance(t.get("model"), dict)
        and t["model"].get("settings_hash") == latency["model_settings_hash"]
    ]
    result = analyze(
        load_rows(root / RAW),
        expected=int(match[1]),
        model=config["model"],
        traces=traces,
        m6_rows=m6,
        ledger=load_rows(root / LEDGER) if (root / LEDGER).exists() else (),
        max_output_tokens=config.get("settings", {}).get("max_output_tokens", 4000),
        repro_runs=repro_runs,
    )
    result["raw_fingerprint"] = raw_hash
    return result


def main():
    result = build()
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"parse_error {result['count']}건, M7 보고 {result['reported_count']}건")


if __name__ == "__main__":
    main()
