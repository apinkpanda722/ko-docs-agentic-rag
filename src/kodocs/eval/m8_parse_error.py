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
    return all(
        isinstance(row.get(k + "_s"), (int, float))
        and isinstance(timings.get(k + "_ms"), (int, float))
        and math.isclose(row[k + "_s"] * 1000, timings[k + "_ms"], abs_tol=1e-6)
        for k in ("embed", "search")
    )


def analyze(rows, *, expected, model, traces=(), m6_rows=()):
    items = []
    for row in rows:
        if row.get("refuse_reason") != "parse_error":
            continue
        matched = [t for t in traces if correlated(row, t)]
        generic = any(
            s.get("error") == "ChatParseError"
            for t in matched
            for s in t.get("steps", [])
        )
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
                "classification": "indeterminate",
                "reason": reason,
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
        "m6_note": "비스트리밍 M6는 참고 정보이며 분류 입력이 아니다.",
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
        row
        for path in sorted((root / "logs/traces").glob("*.jsonl"))
        for row in load_rows(path)
    ]
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
