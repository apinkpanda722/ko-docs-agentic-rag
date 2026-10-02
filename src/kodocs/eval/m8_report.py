"""Offline M8 report generated from authenticated manifests, preserving manual bytes."""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kodocs.eval.m5_dev import _git_commit
from kodocs.eval.m7_latency import _verify_history
from kodocs.eval.m7_rawhash import RawHashMismatch, fingerprint
from kodocs.eval.m7_report import details, f3, stat3, table
from kodocs.eval.m8_contract import RUNS_PATH, _checked, load_h8_state
from kodocs.eval.m8_latency import NOTE, RAW_PATH
from kodocs.eval.m8_test import M5_RESULT_PATH, read_json, read_rows

REPORT_PATH = Path("docs/results/2026-10-03-m8-rerank.md")
TEMPLATE_PATH = Path(__file__).parent / "templates/m8_rerank.md"
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"


def optional(root, path):
    return read_json(root / path) if (root / path).exists() else {}


def render(root=Path(".")):
    root = Path(root)
    state = load_h8_state(root)
    # Reports show every attempt, so authenticate middle runs and failed archives too.
    for record in read_rows(root / RUNS_PATH):
        if record["status"] == "completed":
            _checked(root, record)
        elif record.get("raw_path"):
            raw_path = root / record["raw_path"]
            if not raw_path.resolve().is_relative_to(
                (root / "data/interim/m8/test").resolve()
            ):
                raise RawHashMismatch("M8 원본 경로 위반")
            if raw_path.exists() and fingerprint(raw_path) != record["raw"]:
                raise RawHashMismatch("M8 실패 원본 SHA-256/행 수 불일치")
    latency = state.latency
    if latency:
        _verify_history(latency)
    if (
        latency
        and (root / RAW_PATH).exists()
        and fingerprint(root / RAW_PATH) != latency.get("raw")
    ):
        raise RawHashMismatch("M8 latency 원본 SHA-256/행 수 불일치")
    if state.status == "not_run":
        failed = state.failed_attempts
        return "리랭커 실험(H8)은 미실행이다." + (
            f" 실행 시도 {len(failed)}회가 실패했고 결과 파일은 없다." if failed else ""
        )
    result = state.result
    h8 = result["h8"]
    lines = [
        "## 핵심 요약",
        "",
        f"{h8['verdict']}: 차이 {f3(h8['diff'], signed=True)}, 95% CI [{f3(h8['ci_low'])}, {f3(h8['ci_high'])}], n={h8['n']}.",
        "",
    ]
    if h8["verdict"] == "H8 판정 보류(차이 입증 못 함)":
        lines += [
            "n=21이라 판정 보류가 나올 수 있고 그 경우에도 결과를 그대로 보고한다.",
            "",
        ]
    m5 = read_json(root / M5_RESULT_PATH)
    lines += [
        "## M5 사다리 +리랭커",
        "",
        "고정 청킹보다 구조 청킹 대조군의 NDCG가 낮았던 M5 결과를 그대로 이어 붙인다.",
        "",
    ]
    for scope in ("gold", "silver"):
        ladder = (
            m5["analysis"]["ladder"]
            if scope == "gold"
            else m5["analysis"]["silver_direction"]["all"]["ladder"]
        )
        rows = [
            [
                r["id"],
                f3(r["mean_ndcg10"]),
                f3(r["mean_recall10"]),
                stat3((r.get("delta_from_previous") or {}).get("ndcg10")),
                stat3((r.get("delta_from_previous") or {}).get("recall10")),
            ]
            for r in ladder["stages"]
        ]
        comparison = result.get("comparisons", {}).get(scope, {})
        fallback = (
            {"mean_diff": h8["diff"], "ci_low": h8["ci_low"], "ci_high": h8["ci_high"]}
            if scope == "gold"
            else {}
        )
        rows.append(
            [
                "+리랭커",
                f3(result[scope]["reranked"]["ndcg10"]),
                f3(result[scope]["reranked"]["recall10"]),
                stat3(comparison.get("ndcg10", fallback)),
                stat3(comparison.get("recall10")),
            ]
        )
        lines += [
            f"### {scope} (n={ladder['n']})",
            "",
            table(
                [
                    "단계",
                    "NDCG@10",
                    "Recall@10",
                    "Δ NDCG [95% CI]",
                    "Δ Recall [95% CI]",
                ],
                rows,
            ),
            "",
        ]
    lines += [
        "## 보조 지표(M8-H8b, 판정 아님)",
        "",
        table(
            ["세트", "순위 악화 문항 수"],
            [[s, result["harm"][s]] for s in ("gold", "silver")],
        ),
        "",
        "악화는 문항별 NDCG 차이 < 0이며 0은 세지 않는다. Recall@50은 후보의 문서 단위 상한이다.",
        "",
        details(result.get("auxiliary")),
        "",
        "구간 겹침 적중률·실제/합성·유형별 기술 통계·실버 방향 확인은 위 보조 지표에만 기록한다.",
        "",
        "### d1 문항",
        "",
        details(result.get("auxiliary", {}).get("d1")),
        "",
    ]
    dev = optional(root, Path("data/manifest/m8_dev.json"))
    lengths = optional(root, Path("data/manifest/m8_max_length.json"))
    lines += [
        "## dev 격자·고정 선택·길이",
        "",
        details(dev.get("grid")),
        "",
        "선택: " + str(result["selected"]),
        "",
        details(dev.get("baseline")),
        "",
        dev.get("baseline_note", "미기록"),
        "",
        details(lengths),
        "",
        f"test 잘린 쌍 비율: {result['truncated_ratio']}; max_length={result['max_length']} (보고만, 규칙 변경 없음).",
        "",
        "## 지연(M8-P2, 기술 통계)",
        "",
        details(latency),
        "",
        NOTE,
        "",
        "장치·batch_size·max_length·MPS 메모리·임베딩 캐시 여부·패키지는 측정 환경 기록값이다.",
        "",
        details((latency or {}).get("environment")),
        "",
    ]
    versions = {}
    if (root / "uv.lock").exists():
        packages = tomllib.loads((root / "uv.lock").read_text())["package"]
        versions = {
            p["name"]: p["version"]
            for p in packages
            if p["name"] in ("sentence-transformers", "torch", "transformers")
        }
    lines += [
        "## 실행 정보",
        "",
        details(
            {
                "고정 실행일(KST)": "2026-10-03",
                "생성일(KST)": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                "생성 코드 커밋": _git_commit(root),
                "측정 코드 커밋": result.get("code_commit"),
                **{
                    k: result.get(k)
                    for k in (
                        "ran_at",
                        "config_hash",
                        "config_file_sha256",
                        "m5_config_hash",
                        "rules_sha256",
                        "model_id",
                        "model_revision",
                        "license",
                        "model_size_bytes",
                        "device",
                        "batch_size",
                        "init_s",
                        "mps_memory",
                        "reranker",
                    )
                },
                "uv.lock 패키지 버전": versions,
            }
        ),
        "",
        "### 첫 실행과 재실행 이력",
        "",
        details(read_rows(root / RUNS_PATH)),
        "",
    ]
    attempts = []
    for record in read_rows(root / RUNS_PATH):
        if record["status"] == "completed":
            value = read_json(root / record["result_path"])
            statistic = value["h8"]
            attempts.append(
                [
                    record["seq"],
                    record.get("rerun_reason"),
                    statistic["verdict"],
                    f3(statistic["diff"], signed=True),
                    f"[{f3(statistic['ci_low'])}, {f3(statistic['ci_high'])}]",
                    value["gold"]["reranked"]["ndcg10"],
                    value["silver"]["reranked"]["ndcg10"],
                    value["harm"],
                ]
            )
    lines += [
        table(
            [
                "실행",
                "재실행 사유",
                "H8",
                "차이",
                "95% CI",
                "골드 NDCG",
                "실버 NDCG",
                "악화",
            ],
            attempts,
        ),
        "",
    ]
    failed = [r for r in read_rows(root / RUNS_PATH) if r["status"] != "completed"]
    if failed:
        lines += [
            "### 실패·중단 시도(원본 보존, 결과 파일 없음)",
            "",
            table(
                ["시도", "상태", "오류 유형", "원본 행 수", "보존 경로"],
                [
                    [
                        r["attempt"],
                        r["status"],
                        r.get("error_type"),
                        r["raw"]["rows"],
                        r["raw_path"],
                    ]
                    for r in failed
                ],
            ),
            "",
            "실패 시도는 결과 번호를 쓰지 않고 원본을 `failed-attempt-NN/`에 보존한다. 실행 기록은 지우지 않으며 재실행은 `RERUN_REASON`과 함께 했다. 실패 시도만 있을 때 H8을 오류 대신 '미실행(시도 n회 실패)'으로 표시한 보완은 결과를 보기 전에 [변경 기록](hypotheses.md#변경-기록)에 공개했다(M8-D1 표시 규칙의 예외 1건, 판정 규칙은 변경 없음).",
            "",
        ]
        if (
            failed[0].get("attempt") == 1
            and failed[0].get("error_type") == "AttributeError"
        ):
            lines += [
                (
                    "첫 시도 실패 공개(사후 정정): 첫 시도(attempt 1)는 점수 계산을 마친 뒤 분석 마지막 단계에서 AttributeError로 실패했다. "
                    "d1 문항 입력을 `m7_failures.json` 전체에서 키 `d1`을 일반 순회로 찾아 읽었는데 `class_descriptions.d1`이 문자열이었기 때문이다. "
                    "d1 입력을 `by_system_set[…].classes.d1.ids`의 명시 경로로 읽도록 고쳤고, 보조 입력 구조 검사와 분석 리허설을 리랭커 로딩 전으로 옮겼다. "
                    "판정 규칙·설정·후보는 바꾸지 않았다."
                ),
                "",
            ]
    for label, value in [("첫 실행", state.first_result), ("최신 실행", result)]:
        lines += [
            label,
            "",
            details({k: value[k] for k in ("h8", "gold", "silver", "harm")}),
            "",
        ]
    lines += [
        "## 범위 밖(M8-N1)",
        "",
        "- 답변 재평가: OpenAI·Gemini 재실행은 이 검색 실험 범위 밖이다.",
        "- small-to-big: M7 d2=0이라 근거가 없다.",
        "- 코퍼스 v2: 별표 영향은 골드 test g-real-007 한 문항으로 효과를 측정하기 어렵다.",
        "- tools 연동 미적용: M6 고정 설정과 도구 계약을 보호한다.",
        "- 한국어 미세조정 리랭커 비교는 변수 하나 실험 범위 밖이다.",
        "",
        "H8이 채택이어도 답변 수준 효과는 측정하지 않았다.",
    ]
    return "\n".join(lines)


def write_report(root=Path(".")):
    root = Path(root)
    generated = render(root).encode("utf-8")  # Authenticate before touching any output.
    path = root / REPORT_PATH
    original = path.read_bytes() if path.exists() else TEMPLATE_PATH.read_bytes()
    begin, end = BEGIN.encode(), END.encode()
    if (
        original.count(begin) != 1
        or original.count(end) != 1
        or original.index(begin) >= original.index(end)
    ):
        raise ValueError("M8 보고서 생성 영역 오류")
    start, finish = original.index(begin) + len(begin), original.index(end)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        original[:start] + b"\n\n" + generated + b"\n\n" + original[finish:]
    )
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        write_report(args.root)
        return 0
    except Exception as exc:  # noqa: BLE001 - raw/private text must not be printed
        print(f"M8 report 거부: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
