"""Offline README generation using immutable manifest report functions."""

import argparse
import json
import re
import sys
from pathlib import Path

from kodocs.eval import m5_report, m7_report
from kodocs.eval.m8_contract import H8ResultError, load_h8_state
from kodocs.eval.m8_summary import improvement_bullet, summary_lines
from kodocs.ui.settings import UI_FINAL_MAX_OUTPUT_TOKENS

BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
M5 = "docs/results/2026-09-30-m5-retrieval.md"
M6 = "docs/results/2026-10-01-m6-agent-mcp.md"
M7 = "docs/results/2026-10-02-m7-answer-eval.md"
WRAP = "docs/results/2026-10-03-m8-wrap-up.md"
DEMO_FIXES = "docs/results/2026-10-03-m8-demo-fixes.md"
HYPOTHESES = "docs/results/hypotheses.md#변경-기록"
DEMO_VIDEO_URL = "https://youtu.be/F-HQYN8eit8"
# The order is the user-run reproduction sequence, not an executable workflow.
REPRODUCTION = (
    (
        "원문 수집",
        "collect-law collect-alio match-versions",
        "수집은 네트워크·사용자 실행; 매칭은 오프라인",
    ),
    (
        "파싱",
        "convert-hwp parse-targets parse-pypdf docling-models parse-docling parse-upstage-dry parse-upstage parse-score table-drafts table-golden table-score parse-report",
        "사용자 실행; 모델 다운로드는 네트워크, 업스테이지는 실제 API·과금(예상 비용 확인 후)",
    ),
    (
        "합성 데이터",
        "reg-index build-db plan-docs generate-dry generate synth-validate synth-metrics review-sample review-score synth-report",
        "generate는 실제 Anthropic API·과금·사용자 실행; 나머지는 오프라인",
    ),
    (
        "문항·사람 검수",
        "query-specs queries-dry queries query-validate golden-real-candidates golden-real-check golden-split-preview golden-packets golden-promote m4-report",
        "queries는 실제 Anthropic API·과금·사용자 실행; 실제 문항 작성·검수 후 promote도 사용자 실행",
    ),
    (
        "검색",
        "m5-annex-map m5-chunking m5-eval-inputs embed-models up m5-index m5-dev m5-freeze m5-test m5-report",
        "모델 다운로드·OpenSearch·로컬 임베딩은 사용자 실행; test는 고정 후 한 번, LLM 없음",
    ),
    (
        "답변",
        "m6-temporal m6-inputs m6-dry m6-dev m6-freeze m6-test m6-tokens m6-report",
        "temporal은 OpenSearch 필요; dev·test는 실제 OpenAI API·과금·사용자 실행; 토큰도 사용자 생성",
    ),
    (
        "채점",
        "m7-probe m7-cards m7-human-register m7-pilot m7-freeze m7-grade-dry m7-grade m7-analyze m7-evidence-check",
        "probe·pilot·grade는 실제 Gemini API·과금·사용자 실행; 카드 사람 채점 후 등록·freeze",
    ),
    (
        "보안",
        "m6-serve m7-security-set m7-security-dry m7-security m7-leak-review-register m7-security-rejudge",
        "OpenSearch·별도 MCP 서버 필요; security는 실제 OpenAI API·과금·사용자 실행, 사람 확인 후 등록",
    ),
    (
        "지연·보고",
        "m7-latency-dry m7-latency m7-failures m7-report",
        "latency는 실제 OpenAI API·과금·사용자 단독 실행; 나머지는 오프라인",
    ),
    (
        "공개 준비",
        "adr-index m8-parse-error m8-wrap-report m8-audit readme",
        "오프라인; parse_error는 로컬 원본이 필요",
    ),
)


def serving_improvement(root):
    """Reranker serving follow-up (README only, generated from the dev manifest)."""
    state = load_h8_state(root)
    if state.status != "result":
        return []
    bullet = improvement_bullet(root, state.result, state.result["selected"])
    return [bullet] if bullet else []


def demo_fix_corrections(root):
    """Post-hoc corrections found in the demo rehearsal (numbers from the manifest)."""
    path = root / "data/manifest/m8_lookup_impact.json"
    if not path.exists():
        return []
    kinds = json.loads(path.read_text(encoding="utf-8"))["corpus"]["by_kind"]
    inc, tk = kinds["incident"], kinds["ticket"]
    link = f"[M8 데모 리허설 수정]({DEMO_FIXES})"
    return [
        f"- `lookup_id` 청크·문서 혼동: 도구가 문서 한도를 OpenSearch 청크 수로 넘겨 오래된 문서가 빠졌다. 코퍼스 전수 측정에서 영향은 장애 {inc['identifiers']}건 중 {inc['changed_by_fix']}건, 티켓 {tk['identifiers']}건 중 {tk['changed_by_fix']}건이며 실버 3문항 기록은 영향이 없었다(질문자의 열람 권한). 실버는 재실행하지 않았고 M6·M7 수치는 수정 전 구성 기준이다({link}).",
        f"- `run_sql` 근거에 실행 SQL이 없어 같은 질문이 거절과 답변으로 갈리던 문제를 근거 형식 변경(SQL 포함)으로 고쳤다. 서빙 반영이며 평가 수치는 수정 전 구성 기준이다({link}).",
    ]


def runaway_limits(root):
    repro = repro_summary(root)
    ledger = (
        "M6·M7 비스트리밍 평가 실행과 재현 경로 A에서 파싱 실패한 요청은 원장에 기록되지 않았다(고정 경로가 비용 기록 전에 예외를 던진다). "
        "10월 원장과 콘솔 차이 $0.01과 규모가 맞는다(정황). 고정 경로 코드는 고치지 않았다"
        f"([M8 원장 누락 정리]({WRAP}{NON_STREAM_ANCHOR}))."
    )
    if repro is None:
        return [ledger, ""]
    paths = ", ".join(f"{p} {n}" for p, n in repro["by_path"].items())
    return [
        f"출력 폭주(최종 답변이 출력 상한까지 이어져 JSON이 잘림): 재현 실행 {repro['stage']}단계 {repro['attempts']}회에서 경로별 {paths}건(경로 A 비스트리밍·B/C 스트리밍). 스트리밍·스키마와 무관하게 발생했고 건수가 작아 경로 간 차이는 판단하지 않는다. 왜 길어졌는지는 원시 출력 미보존(M7)으로 미확정이다([M8 재현]({WRAP}#스트리밍-폭주-재현)).",
        "",
        ledger,
        "",
    ]


def nonstream_correction(root, errors):
    items = errors["non_streaming_parse_failures"]["items"]
    repro = repro_summary(root)
    reproduced = (
        f" 재현 경로 A(비스트리밍)에서 폭주 {repro['by_path'].get('A', 0)}/{repro['attempts_by_path'].get('A', 0)}회가 나왔고,"
        if repro
        else ""
    )
    return [
        "- 비스트리밍 폭주 0건 근거 정정: 원장 비교(비스트리밍 최대 출력)를 근거로 삼았으나 고정 비스트리밍 경로는 파싱 실패 요청을 원장에 남기지 않아 근거가 될 수 없었다."
        + reproduced
        + f" trace에서 비스트리밍 parse 실패 {len(items)}건을 찾았다([M8]({WRAP}{NON_STREAM_ANCHOR})). 첫 재현 실행은 손으로 만든 스키마의 HTTP 400으로 중단됐고 라이브러리 스키마로 수정해 재실행했다.",
    ]


NON_STREAM_ANCHOR = "#비스트리밍-parse-실패m6m7-평가-실행-원장-누락-경로"


def repro_summary(root):
    """Counts of the last completed reproduction stage, from the committed manifest."""
    path = root / "data/manifest/m8_stream_repro.json"
    if not path.exists():
        return None
    done = [
        e
        for e in json.loads(path.read_text(encoding="utf-8"))["history"]
        if e["status"] == "completed"
    ]
    if not done:
        return None
    runs = done[-1]["runs"]
    by_path = {
        p: sum(r["runaway"] for r in runs if r.get("path") == p)
        for p in sorted({r.get("path") for r in runs if r.get("path")})
    }
    return {
        "stage": done[-1]["stage"],
        "attempts": len(runs),
        "by_path": by_path,
        "attempts_by_path": {p: sum(r.get("path") == p for r in runs) for p in by_path},
    }


def load(root, name):
    return json.loads((root / "data/manifest" / name).read_text(encoding="utf-8"))


def latest(document, *, kind=None):
    records = (
        [h for h in document.get("history", []) if h.get("kind") == kind]
        if kind
        else [h for h in document.get("history", []) if h.get("status") == "completed"]
    )
    return document | records[-1]["result"] if records else document


def _failed_note(state):
    """Never hide a failed attempt behind a plain "not run"."""
    if not state.failed_attempts:
        return ""
    kinds = ", ".join(
        sorted({str(kind or status) for _, status, kind in state.failed_attempts})
    )
    return f"(실행 시도 {len(state.failed_attempts)}회 실패: {kinds}, 결과 없음)"


def _h8(state):
    if state.status == "not_run":
        failed = _failed_note(state)
        return [
            "H8",
            "미실행",
            "해당 없음",
            "해당 없음",
            "리랭커 실험(H8)은 미실행" + failed,
        ], "리랭커 실험(H8)은 미실행" + failed + "."
    result, first = state.result, state.first_result
    h8 = result["h8"]
    row = [
        "H8",
        h8["verdict"],
        m7_report.f3(h8["diff"], True)
        + " "
        + m7_report.ci3(h8["ci_low"], h8["ci_high"]),
        h8["n"],
        "재정렬 − M5 최종(NDCG@10)",
    ]
    lines = [
        "### H8 리랭커 결과",
        "",
        m7_report.table(
            [
                "세트",
                "대조군 NDCG@10",
                "재정렬 NDCG@10",
                "대조군 Recall@10",
                "재정렬 Recall@10",
                "순위 악화 문항",
            ],
            [
                [
                    scope,
                    m7_report.f3(result[scope]["control"]["ndcg10"]),
                    m7_report.f3(result[scope]["reranked"]["ndcg10"]),
                    m7_report.f3(result[scope]["control"]["recall10"]),
                    m7_report.f3(result[scope]["reranked"]["recall10"]),
                    result["harm"][scope],
                ]
                for scope in ("gold", "silver")
            ],
        ),
        "",
        f"선택 depth {result['selected']['depth']}·fusion {result['selected']['fusion']}, max_length {result['max_length']}, test 잘림 비율 {m7_report.f3(result['truncated_ratio'])}, 장치 {result['device']}.",
        "",
        "채택 문구만 단독으로 읽지 않는다:",
        "",
        *summary_lines(result, state.latency),
        "",
        "첫 실행: "
        + first["h8"]["verdict"]
        + ", "
        + m7_report.f3(first["h8"]["diff"], True)
        + " "
        + m7_report.ci3(first["h8"]["ci_low"], first["h8"]["ci_high"]),
        "",
        # Stable text: the README must be identical on a clone without the
        # non-committed raw files (data/interim), so it never states whether
        # the local raw comparison happened.
        "실행 기록·결과의 해시는 커밋된 manifest로 검증했다. 비커밋 원본이 로컬에 있으면 원본 해시도 대조하고 없으면 대조하지 않는다.",
    ]
    if state.latency:
        latency = state.latency
        lines += [
            "",
            "### 리랭커 지연(초)",
            "",
            m7_report.table(
                ["P50", "P95", "최대", "M7 단순 P95", "보수적 합", "목표 대비 여유"],
                [
                    [latency["rerank_s"][key] for key in ("p50_s", "p95_s", "max_s")]
                    + [
                        latency[key]
                        for key in (
                            "simple_p95_m7",
                            "conservative_sum_s",
                            "margin_to_5s",
                        )
                    ]
                ],
            ),
            "",
            "두 P95의 합은 분포 합성이 아닌 보수적 근사이며 여유는 판정이 아니다. M7은 임베딩 캐시 적중 상태 측정이다.",
        ]
    lines += [
        "",
        "출처: [M8 리랭커](docs/results/2026-10-03-m8-rerank.md) · [검증된 manifest]("
        + state.result_path.as_posix()
        + ").",
    ]
    return row, "\n".join(lines)


def render(root=Path(".")):
    root = Path(root)
    state = load_h8_state(
        root
    )  # Fail before writing anything, never mask a broken run.
    m5 = load(root, "m5_test_results/run-01.json")
    analysis = load(root, "m7_analysis.json")
    latency = latest(load(root, "m7_latency.json"))
    security = latest(load(root, "m7_security_results.json"), kind="judge_correction")
    h5, h6 = analysis["h5"], analysis["h6"]
    summary = m7_report.summary_section(
        {
            "analysis": analysis,
            "h5": h5,
            "h6": h6,
            "latency": latency,
            "security": security,
            "criteria": [],
            "m5": m7_report.load_m5_hypotheses(root),
            "verdict": m7_report.fill_verdict(h5["verdict_text"], h5["primary"]),
            "bound": m7_report.noncache_bound(latency),
            "dev_cost": m7_report.dev_cost_ratio(load(root, "m6_dev.json")),
        }
    ).split("### H1~H7 판정", 1)[1]
    h8_row, h8_section = _h8(state)
    summary = "### H1~H8 판정" + summary
    # Append the H8 row to the existing report table without duplicating H1~H7 logic.
    row = m7_report.table(
        ["가설", "판정", "차이 [95% CI]", "n", "비교·보조"], [h8_row]
    ).splitlines()[-1]
    summary = summary.replace("\n\nH1~H4", "\n" + row + "\n\nH1~H4", 1)
    summary = re.sub(r"\]\((2026-[^)]*\.md[^)]*)\)", r"](docs/results/\1)", summary)
    for heading in ("### H5 유형별", "### H6 경로", "### 지연("):
        summary = summary.replace(
            "\n\n" + heading,
            f"\n\n출처: [M7 핵심 결과]({M7}#핵심-결과-요약).\n\n" + heading,
            1,
        )
    summary = summary.replace(
        "\n\n비캐시 보정",
        f"\n\n출처: [M7 지연]({M7}#핵심-결과-요약).\n\n비캐시 보정",
        1,
    )
    kappa = analysis["kappa"]
    simple = latency["systems"]["simple"]
    lines = ["## 핵심 결과", "", "### 검색 품질 사다리", ""]
    counts = m5["question_counts"]
    for title, n, ladder in (
        ("골드 test — 주 표", counts["gold_test"], m5["analysis"]["ladder"]),
        (
            "실버 test — 방향 확인",
            counts["silver_test"],
            m5["analysis"]["silver_direction"]["all"]["ladder"],
        ),
    ):
        lines += [
            m5_report._ladder_section(f"{title}(n={n})", ladder),
            "",
            f"출처: [M5 검색 결과]({M5}#검색-결과).",
            "",
        ]
    fixed = m5["fixed_configuration"]
    lines += [
        "고정 조건: "
        + ", ".join(f"{key}={value}" for key, value in fixed.items())
        + ". dev에서 선택 후 test를 고정했다. 구조 청킹의 NDCG 하락도 그대로 보고한다.",
        "",
        f"출처: [M5 검색 결과]({M5}#검색-결과).",
        "",
        summary,
        "",
        f"채점 검증 κ = {m7_report.f3(kappa['quadratic_kappa'])} {m7_report.ci3(kappa['ci_low'], kappa['ci_high'])}, 표본 n={kappa['n']}. "
        + (
            "검증 통과." if kappa["passed"] else "검증 미달; 사람 기준 판정을 확인한다."
        ),
        "",
        f"단순 RAG {simple['verdict']}, 목표 {simple['target_s']}초. 지연은 스트리밍·동시성 단독 측정이며 GUI의 답변(최종 답변 출력 상한과 자동 재생성이 평가 설정과 다름)과 구분한다.",
        "",
        f"출처: [M7 판정·H5·H6·지연]({M7}#핵심-결과-요약).",
        "",
        h8_section,
        "",
        "### 비용(USD, 원장 정가 + 부가세)",
        "",
    ]
    ledger = [
        json.loads(line)
        for line in (root / "data/manifest/costs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    lines += [
        m7_report.table(
            ["구분", "실행", "USD"],
            [
                [category, run, f"{amount:.6f}"]
                for category, runs in m7_report.aggregate_costs(ledger).items()
                for run, amount in runs.items()
            ],
        ),
        "",
        "운영은 답변·보안 실행, 평가는 Gemini 채점, 별도 측정은 지연 실행 비용이다. 준비·dev 비용을 운영비에 합산하지 않는다.",
        "",
        f"출처: [M7 비용]({M7}#비용--원장-정가--부가세-gemini-평가-비용-분리) · [M6 dev·과금 내역]({M6}) · [비용 원장](data/manifest/costs.jsonl).",
        "",
        "## 재현 방법",
        "",
        "Python 3.12, uv, Docker/OpenSearch 3.x를 사용한다. `make verify`는 외부 API를 mock하여 API 키 없이 통과한다. 의존성 그룹과 로컬 변환 도구 준비는 [명령 안내](AGENTS.md#명령어)를 따른다.",
        "",
        "법제처·ALIO 원문은 저장소에 없으며 사용자가 수집한다. 원문 재배포를 금지하고 출처·해시만 기록한다([ADR-002](docs/ADR.md#adr-002-가상-it-서비스-회사-실제-규정은-nia-내부규정-원문을-무가공-로컬-사용)).",
        "",
        "아래 순서는 사용자가 직접 실행한다. 과금 명령은 예상 비용과 월 상한을 확인한다. 확정 test 재실행은 `RERUN_REASON`과 이력 보존이 필요하다. 골드 확정은 사람 검수 후에만 진행한다.",
        "",
        m7_report.table(
            ["단계", "명령 순서", "조건"],
            [
                [
                    label,
                    " → ".join(f"`make {target}`" for target in targets.split()),
                    note,
                ]
                for label, targets, note in REPRODUCTION
            ],
        ),
        "",
        "## 한계",
        "",
        "합성 문항은 단일 시드와 가상 회사에 기반하고 작은 표본의 결과를 다른 조직으로 일반화할 수 없다. H2·H3·H4는 차이를 입증하지 못해 판정 보류이며 동등성을 뜻하지 않는다.",
        "",
        f"n-gram 분석기는 M5에서 제외했다. 사전 등록 가설에 걸려 있지 않아 판정에 필요 없었다([ADR-028](docs/ADR.md#adr-028-m5-검색-실험-설계), [M5 범위와 제외 사유]({M5}#범위와-제외-사유)).",
        "",
    ]
    # Step 2's regulation upper-bound table is already generated and is the
    # published source; do not rescan qrels or infer annex-level affected counts.
    wrap = (root / WRAP).read_text()
    upper = re.search(r"\| 세트 \| split \| 규정 단위 상한.*?(?=\n\n)", wrap, re.DOTALL)
    annex = load(root, "m7_failures.json")["measurement_limitations"][
        "annex_context_audit"
    ]
    if upper is None:
        raise ValueError("M8 정정 보고의 규정 단위 상한 표 없음")
    lines += [
        f"별표 조립에서 단위 표기가 유실된 별표 {annex['unit_label_lost_sections']}개. 아래 영향 문항 수는 별표 단위가 아닌 규정 단위 상한이다.",
        "",
        upper.group(),
        "",
        "별표 단위 영향 문항 수는 미산출이다. M5~M7은 유실이 있는 상태로 측정했고 수정·재색인하지 않았다.",
        "",
        f"출처: [M8 별표 조립 한계]({WRAP}#별표-조립-한계).",
        "",
        f"사람 채점은 단독 평가자의 {load(root, 'm7_human_grades.json')['rows']}건이며 평가자 간 일치도는 측정하지 않았다. 보안 누설 없음은 관측값이고 안전성 증명이 아니다. MCP는 도구 수준 평가이며 정적 bearer 토큰을 사용한다. OAuth·토큰 자동 만료·전역 속도 제한은 미구현이다. 임베딩 캐시 적중과 외부 API 변동도 지연 해석을 제한한다.",
        "",
        f"출처: [M7 한계]({M7}#한계사용자-보존-영역) · [위협 모델](docs/THREAT_MODEL.md).",
        "",
        *runaway_limits(root),
        "## 사후 정정 공개",
        "",
        f"- M5 정답 집합: 검색 전 인가로 반환 불가능한 문서를 분모에서 제외했다. test 결과를 보기 전에 변경했다([가설 변경 기록]({HYPOTHESES}), [M5]({M5})).",
        f"- M6 dev 첫 실행: 합성 문항 section_key와 인용 검증기의 불일치로 무효 처리했다. 수정 후 dev를 재실행했으며 무효 실행은 모델 선택에서 제외했다([변경 기록]({HYPOTHESES}), [M6 실행 이력]({M6})).",
        f"- M7 채점 캐시: 답변 식별자와 run_set을 포함하지 않은 키 충돌을 수정했다. 잘못 재사용한 채점은 무효화하고 이력을 보존했다([변경 기록]({HYPOTHESES}), [M7]({M7})).",
        "- c2 식별자 고유성: "
        + (m7_report.correction_note(security) or "정정 이력 없음")
        + f" ([변경 기록]({HYPOTHESES}), [M7]({M7})).",
        f"- 지연 환경 패키지명: 수집 목록 오류를 정정했다. 실제 사용은 sentence-transformers와 httpx이며 설치 실패가 아니다. 측정 당시 manifest는 보존했다([M8 환경 기록 정정]({WRAP}#m7-환경-기록-정정)).",
    ]
    errors = load(root, "m8_parse_error.json")
    lines += [
        *nonstream_correction(root, errors),
        f"- 스트리밍 parse_error: {errors['count']}건, "
        + ", ".join(f"{key} {n}건" for key, n in errors["class_counts"].items())
        + ". "
        + errors.get("cause_note", "원인 판정 기록 부족; 원시 출력 미보존으로 미확정.")
        + f" API를 재호출하지 않았다([M8 오프라인 분류]({WRAP}#스트리밍-parse_error-오프라인-분류)).",
        *demo_fix_corrections(root),
        "",
        "## 다음 개선",
        "",
        *serving_improvement(root),
        "- 인용을 자유 문자열 대신 근거 목록 중 선택(근거별 enum)으로 바꿔 인용 생성 루프를 원천 차단한다. 답변 계약 변경이라 재평가가 필요하며 이번에는 구현하지 않았다.",
        "",
        "## 데모",
        "",
        "주 데모는 Claude Code를 MCP 클라이언트로 연결해 서버가 결정한 사용자 권한으로 도구를 호출하는 장면이다. 연결 절차와 장면별 질문·기대값은 [데모 대본](docs/demo/scenario.md)을 따른다.",
        "",
        f"시연 영상: {DEMO_VIDEO_URL} (2분 41초, 유튜브 일부 공개). 0:00 MCP 서버 기동·토큰 없는 요청 401·사용자별 토큰 주입·Claude Code 도구 연결 확인, 0:56 같은 질문을 플랫폼팀 3급과 재무팀 3급이 던졌을 때의 범위 차이·없는 번호 조회·권한 없는 문서 직접 열기·SQL 집계, 1:53 Streamlit GUI의 직급 차이(인사팀 4급·2급)와 Agentic RAG·단순 RAG 비교.",
        "",
    ]
    for label, name, chapter in (
        ("Claude Code + MCP", "demo-claude-code-mcp.gif", "0:00~1:53"),
        ("Streamlit GUI", "demo-streamlit.png", "1:53~"),
    ):
        path = Path("docs/images") / name
        lines += [
            f"![{label}]({path.as_posix()})"
            if (root / path).is_file()
            else f"{label}: [시연 영상]({DEMO_VIDEO_URL}) {chapter} 구간.",
            "",
        ]
    eval_cap = json.loads((root / "data/manifest/m6_config.json").read_text())[
        "answer"
    ]["settings"]["max_output_tokens"]
    ui_cap = UI_FINAL_MAX_OUTPUT_TOKENS
    lines += [
        f"보조 GUI는 `make ui`로 실행한다(OpenSearch·API 키 필요, 실제 답변은 과금·사용자 실행). 같은 질문을 사용자 전환으로 비교하고 답변·인용·거절·도구 트레이스·비용을 표시한다. 화면은 질의 화면 하나이며(결과는 README와 `docs/results/`에서 본다), 질의 화면은 `answer()`의 실제 모델 스트리밍(최종 답변), 도구 호출 실시간 표시, 폭주 시 진단 기록·재시도를 제공한다. 최종 답변 출력 상한은 GUI에서 {ui_cap}토큰(평가 설정은 {eval_cap}토큰)이며 상한에서 잘리면 1회 자동 재생성한다."
    ]
    return "\n".join(lines)


def write_readme(root=Path(".")):
    root = Path(root)
    path = root / "README.md"
    original = path.read_bytes()
    begin, end = BEGIN.encode(), END.encode()
    if (
        original.count(begin) != 1
        or original.count(end) != 1
        or original.index(begin) >= original.index(end)
    ):
        raise ValueError("README generated 표지의 짝·순서 오류")
    before, rest = original.split(begin)
    _, after = rest.split(end)
    updated = before + begin + b"\n\n" + render(root).encode() + b"\n\n" + end + after
    if len(updated.splitlines()) > 400:
        raise ValueError("README 400줄 초과")
    path.write_bytes(updated)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        print(write_readme(args.root))
    except (H8ResultError, OSError, ValueError, KeyError) as exc:
        print(f"README 생성 실패: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
