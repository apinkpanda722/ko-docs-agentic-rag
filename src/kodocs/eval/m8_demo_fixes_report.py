"""Result document of the demo-rehearsal fixes (lookup_id, SQL evidence, GUI scope).

The measured parts come from ``data/manifest/m8_lookup_impact.json``; the serving
changes are described in text. No question or answer text is ever written.
"""

import json
from pathlib import Path

from kodocs.eval.m7_report import f3, table

REPORT = Path("docs/results/2026-10-03-m8-demo-fixes.md")
IMPACT = Path("data/manifest/m8_lookup_impact.json")
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"


def _score(row):
    mean = row["correctness_mean"]
    text = row["class"] or "채점 기록 없음"
    return text if mean is None else f"{text}, 정답성 {f3(mean)}"


def render(root=Path(".")):
    root = Path(root)
    impact = json.loads((root / IMPACT).read_text(encoding="utf-8"))
    kinds = impact["corpus"]["by_kind"]
    changed = impact["corpus"]["changed"]
    runs = impact["runs"]
    target, missed = impact["target"]["identifier"], impact["target"]["missed_document"]
    labels = {"incident": "장애", "ticket": "티켓"}
    agentic = [r for r in runs if r["system"] == "agentic"]
    affected = sum(r["calls_missing_report"] or 0 for r in agentic)
    unreadable = sum(not r["asker_can_read_missed_doc"] for r in runs)
    lines = [
        "## 1. lookup_id의 청크·문서 혼동 (사후 정정)",
        "",
        '**원인.** `lookup_id` 도구가 `authorized_lookup_id`에 `size = LOOKUP_DOCUMENT_LIMIT + 1`(6)을 넘겼는데, 그 함수의 `size`는 문서 수가 아니라 OpenSearch **청크** 수다. 결과가 날짜 내림차순이라 여러 청크를 가진 최신 문서 몇 개가 6칸을 채우면 청크가 하나뿐인 오래된 문서가 빠졌다. 부수 결함으로 `DocHit`에 `date` 필드가 없어 `getattr(hit, "date", None)`이 항상 `None`이었고 lookup_id 응답의 `date`가 항상 null이었다.',
        "",
        "**수정.** 청크를 넉넉히(`LOOKUP_CHUNK_REQUEST_LIMIT = 50`) 요청하고 문서로 묶은 뒤 `LOOKUP_DOCUMENT_LIMIT`(5)개로 자른다. 문서가 5개를 넘거나 청크 요청 상한까지 채워지면 `truncated=true`다. `DocHit.date`를 채워 응답의 `date`가 실제 값을 담는다. 출력 스키마(nullable `date`)는 그대로라 도구 버전(`TOOL_VERSIONS`)은 올리지 않았다.",
        "",
        "**영향 범위(코퍼스 전수, 소유 팀 6급 컨텍스트, 읽기 전용).** 수정 전(6청크)과 수정 후(50청크)의 도구 응답 문서 목록을 비교했다.",
        "",
        table(
            [
                "종류",
                "식별자 수",
                "수정으로 문서 목록이 바뀐 수",
                "상위 5문서와 같음",
                "직렬화 예산으로 5문서 중 앞부분만 반환",
            ],
            [
                [
                    labels[kind],
                    value["identifiers"],
                    value["changed_by_fix"],
                    value["equal_top5"],
                    value["budget_prefix"],
                ]
                for kind, value in kinds.items()
            ],
        ),
        "",
        "바뀐 식별자: "
        + (
            ", ".join(
                f"{c['value']}(추가된 문서 {', '.join(c['added'])})" for c in changed
            )
            or "없음"
        )
        + ". '직렬화 예산' 열은 이 수정과 무관한 기존 동작이다: 응답이 6,000자를 넘으면 마지막 문서를 버리고 `truncated=true`로 표시한다(수정 전에도 같다).",
        "",
        f"**실버 문항 영향 확인(trace·결과 집계, 문항·답변 원문 없음).** `{target}`를 언급하는 실버 3문항({', '.join(sorted({r['question_id'] for r in runs}))})의 M6·M7 로컬 실행 {len(runs)}건을 시스템·실행별로 센다. 단순 RAG에는 `lookup_id`가 없다.",
        "",
        table(
            [
                "문항",
                "실행",
                "시스템",
                "모델",
                f"질문자가 {missed} 열람 가능",
                f"`lookup_id({target})` 호출",
                f"호출 중 {missed} 누락(열람 가능한 경우만)",
                "기록된 점수",
            ],
            [
                [
                    r["question_id"],
                    r["run_set"],
                    r["system"],
                    r["model"],
                    "예" if r["asker_can_read_missed_doc"] else "아니오",
                    "해당 없음"
                    if r["lookup_id_calls_for_target"] is None
                    else r["lookup_id_calls_for_target"],
                    "해당 없음"
                    if r["calls_missing_report"] is None
                    else r["calls_missing_report"],
                    _score(r),
                ]
                for r in runs
            ],
        ),
        "",
        f"집계: 질문자가 `{missed}`를 열람할 수 없는 실행 {unreadable}/{len(runs)}건. Agentic의 `lookup_id({target})` 호출 중 열람 가능한 문서가 빠진 호출은 {affected}건이다. 열람 권한이 없는 문서가 응답에 없는 것은 결함이 아니라 권한 필터의 정상 동작이다. 따라서 이 세 문항의 M6·M7 기록은 이 결함의 영향을 받지 않았다.",
        "",
        f"**재실행하지 않은 이유.** 세 문항은 판정용이 아닌 방향 확인용 실버 문항이고, 위 측정에서 영향이 없었다. 결과를 본 뒤 일부 문항만 다시 돌리면 선택적 재실행이 된다. M6·M7 확정 결과 문서·manifest·수치는 바꾸지 않았다. 이 결함이 실제로 드러나는 경우는 `{target}`를 열람 가능한 팀(예: 인사팀 3급)이 질의할 때뿐이며 골드 문항에는 이 식별자가 없다(H5 판정 영향 없음).",
        "",
        "## 2. run_sql 근거에 실행 SQL 문 포함 (서빙 변경)",
        "",
        '**현상.** GUI 리허설에서 플랫폼팀 3급이 "플랫폼팀(owner_team이 platform) 티켓을 상태별로 세고 합계도 알려줘"를 묻자 `run_sql`은 정상 실행돼 94건을 반환했는데 최종 답변이 거절(`model_refused`)됐다. 같은 질문에 재무팀 3급은 56건으로 답변했다.',
        "",
        '**원인(추정).** 최종 답변 호출은 질문과 근거 텍스트만 본다. `run_sql` 근거가 `columns`·`rows`만 담고 실행 SQL 문이 없어, 숫자가 플랫폼팀 조건으로 뽑혔다는 것을 확인할 수 없고 "근거로 확인 못 하면 거절" 규칙에 따라 실행마다 거절과 답변이 갈릴 수 있다.',
        "",
        '**수정.** `run_sql` 근거 텍스트를 `{"sql": 실행한 SQL, "columns": ..., "rows": ...}`로 만든다(SQL은 모델이 쓴 것이라 숨길 정보가 없다). 문서 근거 형식은 그대로다. `EVIDENCE_FORMAT_VERSION`을 2로 두고 모든 trace의 `hashes.evidence_format_version`에 기록한다(`finalizer_hash`·프롬프트 해시는 이 형식을 포함하지 않아 값이 바뀌지 않는다).',
        "",
        "**평가 수치와의 관계.** 서빙 반영이며 평가 수치는 수정 전 구성(근거 버전 1) 기준이다. M6·M7의 SQL 문항 점수(H5 sql 유형 포함)는 수정 전 근거로 측정했고 다시 계산하지 않았다. 반복 실행으로 거절이 사라지는지의 확정 확인은 머지 후 사용자가 한다.",
        "",
        "## 3. GUI 범위 축소와 파일 감시",
        "",
        "- Streamlit 결과표 탭을 제거했다(사용자 결정). 결과는 README와 `docs/results/`에서 본다. PRD 6번 ②는 '제외(2026-10-03)'로 고쳤다.",
        '- `.streamlit/config.toml`에 `[server] fileWatcherType = "none"`을 추가했다. 파일 감시가 transformers의 지연 import 모듈 경로를 훑다가 torchvision 미설치로 "Examining the path of … raised" 로그를 대량으로 남기던 것을 끈다(앱 동작과 무관). 모듈을 로드하는 프로브 앱으로 이 설정이 없을 때 102줄, 있을 때 0줄임을 확인했다. torchvision은 설치하지 않았다.',
        "",
        "## 재확인(머지 후 사용자)",
        "",
        '1. Claude Code demo-hr-g3: "INC-2026-010 장애의 원인과 조치를 찾아서 세 줄로 요약해줘. 근거 문서 ID도 붙여줘." → `syn-incident-020`이 근거에 포함.',
        "2. Streamlit agentic, demo-platform-g3: 플랫폼팀 SQL 질문 3회 반복 → 매번 94건으로 답변(거절 없음).",
        "3. Streamlit에 질의 화면만 남고 결과표 탭이 없는지.",
    ]
    return "\n".join(lines)


def write_report(root=Path(".")):
    root = Path(root)
    path = root / REPORT
    original = (
        path.read_bytes()
        if path.exists()
        else f"# M8 데모 리허설 수정 결과\n\n{BEGIN}\n{END}\n\n## 사용자 보존 영역\n\n".encode()
    )
    begin, end = BEGIN.encode(), END.encode()
    if (
        original.count(begin) != 1
        or original.count(end) != 1
        or original.index(begin) >= original.index(end)
    ):
        raise ValueError("생성 영역 마커가 올바르지 않습니다")
    head, rest = original.split(begin, 1)
    _, tail = rest.split(end, 1)
    body = f"{BEGIN}\n\n{render(root)}\n\n{END}".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + body + tail)
    return path


def main():
    print(write_report())


if __name__ == "__main__":
    main()
