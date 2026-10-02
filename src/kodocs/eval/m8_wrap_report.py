"""M8 corrections from immutable M7 records, lockfile and source imports."""

import json
import re
import tomllib
from pathlib import Path

BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
REPORT = Path("docs/results/2026-10-03-m8-wrap-up.md")
M7_REPORT = Path("docs/results/2026-10-02-m7-answer-eval.md")
CONCLUSION = "M5~M7 수치는 이 유실이 있는 채로 측정되었다. 수정·재색인은 하지 않았다."
# Source: M7 result, M8 개선 후보(사용자 보존 영역), checked 2026-10-02.
# Used only as a contract for the document-parsed regulation list, not annex IDs.
REGULATION_IDS = tuple(
    "law-2200000" + n
    for n in (
        "148327",
        "148341",
        "148345",
        "148727",
        "148765",
        "148769",
        "149621",
        "150485",
        "150587",
        "151395",
    )
)
PACKAGE_MAPPING = (
    (
        "FlagEmbedding",
        "sentence-transformers",
        "sentence_transformers",
        "src/kodocs/retrieval/embedding.py",
    ),
    ("opensearch-py", "httpx", "httpx", "src/kodocs/retrieval/opensearch.py"),
)


def read_json(root, path):
    return json.loads((root / path).read_text())


def package_rows(root, latency):
    lock_text = (root / "uv.lock").read_text()
    versions = {
        p["name"]: p["version"]
        for p in tomllib.loads(lock_text)["package"]
        if "version" in p
    }
    rows = []
    for recorded, actual, module, path in PACKAGE_MAPPING:
        imports = [
            (i, line)
            for i, line in enumerate((root / path).read_text().splitlines(), 1)
            if re.match(r"\s*(?:from " + module + r"\b|import " + module + r"\b)", line)
        ]
        if not imports:
            raise ValueError("실제 사용 import 근거 없음")
        lock_line = next(
            i
            for i, line in enumerate(lock_text.splitlines(), 1)
            if re.fullmatch(r'\s*name\s*=\s*"' + re.escape(actual) + r'"\s*', line)
        )
        value = latency["environment"]["packages"][recorded]
        rows.append(
            (
                recorded,
                "null" if value is None else str(value),
                f"{actual} {versions[actual]}",
                f"{path}:{imports[0][0]}; uv.lock:{lock_line}",
            )
        )
    return rows


def regulations(document):
    section = document.split("## M8 개선 후보", 1)[1]
    match = re.search(r"(\d+)개 규정: (law-\d+(?:·\d+)+)", section)
    if not match:
        raise ValueError("M7 규정 목록 없음")
    pieces = match[2].split("·")
    prefix = pieces[0][: -len(pieces[1])]
    ids = (pieces[0], *(prefix + p for p in pieces[1:]))
    if len(set(ids)) != int(match[1]) or set(ids) != set(REGULATION_IDS):
        raise ValueError("M7 규정 수·목록 불일치")
    return ids


def annex_section(audit, questions, ids):
    # Original M7 manifest has no regulation count/list. If a fixture/future
    # source supplies that count, require agreement; never amend the M7 record.
    if audit.get("unit_label_lost_regulations", len(ids)) != len(ids) or len(
        ids
    ) != len(REGULATION_IDS):
        raise ValueError("별표 감사 규정 수 불일치")
    lines = [
        "## 별표 조립 한계",
        "",
        f"비교한 별표 {audit['sections_compared']}개 중 composite가 pypdf의 70% 미만인 별표 {audit['below_70_percent']}개, 단위 표기가 유실된 별표 {audit['unit_label_lost_sections']}개·{len(ids)}개 규정.",
        "",
        "출처: `m7_failures.annex_context_audit` (`measurement_limitations.annex_context_audit`) 및 M7 결과 문서 “M8 개선 후보”. 감사 manifest에는 규정 수·식별자 목록이 없으며 규정 수와 목록은 M7 문서에서 읽었다.",
        "",
        "### 규정 단위 상한 — 영향 문항 수",
        "",
        "같은 규정의 유실되지 않은 별표 문항도 포함하므로 별표 단위 값이 아니다. 중복 없는 문항 수이며 qrels의 rel 값으로 추가 제한하지 않는다.",
        "",
        "| 세트 | split | 규정 단위 상한(문항 수) |",
        "|---|---|---|",
    ]
    for group in ("gold", "silver"):
        for split in ("dev", "test"):
            count = len(
                {
                    q["id"]
                    for q in questions[group]
                    if q["split"] == split
                    and any(
                        r.get("annex_id") and r["doc_id"] in ids for r in q["qrels"]
                    )
                }
            )
            lines.append(f"| {group} | {split} | {count} |")
    lines += [
        "",
        "별표 단위 영향 문항 수는 미산출(M7 감사가 식별자 목록을 남기지 않음).",
        "",
        CONCLUSION,
    ]
    return "\n".join(lines)


def render(root):
    latency = read_json(root, "data/manifest/m7_latency.json")
    packages = package_rows(root, latency)
    m5 = read_json(root, "data/manifest/m5_config.json")
    errors = read_json(root, "data/manifest/m8_parse_error.json")
    from kodocs.eval.m7_rawhash import verify_fingerprint
    from kodocs.eval.m8_parse_error import LATENCY, RAW

    current = verify_fingerprint(root / LATENCY, "latency", root / RAW)
    if current != errors["raw_fingerprint"]:
        raise ValueError("M8 parse_error 분석 원본 불일치")
    lines = [
        "## M7 환경 기록 정정",
        "",
        "수집 목록 오류이며 M7 manifest·확정 지표는 보존했다. 실제 사용 버전은 현재 `uv.lock`의 잠금 버전이며 측정 당시 설치 버전의 재수집 값이 아니다.",
        "",
        "| 기록 패키지 | 기록값 | 실제 사용(잠금 버전) | 근거 파일·줄 |",
        "|---|---|---|---|",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in packages]
    lines += [
        "",
        f"임베딩 revision: `{m5['embedder']['revision']}` (`data/manifest/m5_config.json`, embedder.revision).",
        "",
        "## 스트리밍 parse_error 오프라인 분류",
        "",
        f"분석 {errors['count']}건 / M7 결과 문서 보고 {errors['reported_count']}건 / 건수 일치: {errors['count_matches']}. 원본 SHA-256·행 수 대조 통과. 재호출 없음.",
        "",
        "| 문항 | 시스템 | 모델 | 분류 | 사유 | M6 비스트리밍 parse_error(참고) |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {i['question_id']} | {i['system']} | {i['model']} | {i['classification']} | {i['reason']} | {i['m6_parse_error']} |"
        for i in errors["items"]
    ]
    if errors.get("cause_note"):
        lines += [
            "",
            errors["cause_note"],
            "",
            errors["definition"],
            f"지연 허용 오차: {errors['latency_tolerance_s']}초.",
            "",
            "| 문항 | 연결 | 출력 토큰 | 상한 | reasoning | 모델 지연(초) |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {i['question_id']} | {i['link_method']} | {i['output_tokens']} | {i['max_output_tokens']} | {i['reasoning_tokens']} | {i['latency_s']} |"
            for i in errors["items"]
        ]
        lines += [
            "",
            "| run_id | 요청 수 | 최대 출력 | 상한 도달 | 오류 외 최대 출력 |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {run} | {v['count']} | {v['max_output_tokens']} | {v['max_output_tokens_reached_count']} | {v.get('remaining_max_output_tokens', '—')} |"
            for run, v in errors["comparisons"].items()
        ]
    lines += [
        "",
        "분류별 건수: "
        + ", ".join(f"{k} {v}" for k, v in errors["class_counts"].items()),
        "",
        "토큰 계수 0·ChatParseError만으로 빈 출력이나 max_output_tokens 도달을 확정하지 않는다. 답변 본문·원시 출력은 분석 manifest에 기록하지 않는다.",
        "",
    ]
    questions = {
        group: [
            json.loads(line)
            for line in (root / f"data/golden/queries/{group}.jsonl")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        for group in ("gold", "silver")
    }
    audit = read_json(root, "data/manifest/m7_failures.json")[
        "measurement_limitations"
    ]["annex_context_audit"]
    lines.append(
        annex_section(audit, questions, regulations((root / M7_REPORT).read_text()))
    )
    from kodocs.eval.m8_stream_repro import report

    lines += [
        "",
        "## 스트리밍 폭주 재현",
        "",
        (
            "보고 규칙(실행 전 고정): 경로별·질문별 폭주 건수와 비율, 폭주 건의 형태 통계를 기술 통계로만 보고한다(가설 검정 없음). "
            "경로 A=비스트리밍+sdk 스키마(M6 동일), B=스트리밍+langchain 스키마(M7 동일), C=스트리밍+sdk 스키마(UI 경로). "
            "해석은 “B 대 C = 스트리밍 아래 스키마 차이, A 대 C = 같은 스키마에서 스트리밍 여부”로만 한다. "
            "폭주 0건이면 “재현 안 됨(1단계 300회[, 2단계 452회])”. 원시 출력은 인용하지 않는다. "
            "결과 manifest가 없으면 “재현 실행 미실행”, 결과·원본 해시 검증 실패는 오류로 중단한다."
        ),
        "",
        (
            "첫 실행 실패 공개(사후 정정): 재현 실행의 첫 실행(history seq 1)은 HTTP 400 “Invalid schema for response_format”"
            "(FatalLLMError)로 중단됐다(사용자 보고: 과금 0, 원장에 repro 기록 없음, 원시 파일 0바이트). 원인은 스트리밍 콜백 경로가 "
            "`ModelAnswer.model_json_schema()` 원본을 손으로 감싸 strict json_schema로 보낸 것이다(Citation의 required가 일부 필드뿐이라 "
            "strict 조건 위반). 가짜 채팅 테스트만 있어 잡지 못했다. 스키마를 설치된 라이브러리 함수로만 만들도록 수정하고 요청 payload 테스트를 "
            "추가했으며, 첫 실행 기록은 지우지 않고 재실행 사유와 함께 이력에 남긴다."
        ),
        "",
        report(root),
        "",
        "실행 순서: 이 step 완료 → `make up` → `make m8-stream-repro-dry` → `make m8-stream-repro` → 필요하면 `STAGE=2 make m8-stream-repro` → `make m8-wrap-report` → `make readme` → 커밋 → 리랭커(step 6~8).",
    ]
    return "\n".join(lines)


def write_report(root=Path(".")):
    root = Path(root)
    path = root / REPORT
    original = (
        path.read_bytes()
        if path.exists()
        else f"# M8 마무리·정정\n\n{BEGIN}\n{END}\n\n## 사용자 보존 영역\n\n".encode()
    )
    begin, end = BEGIN.encode(), END.encode()
    if (
        original.count(begin) != 1
        or original.count(end) != 1
        or original.index(begin) >= original.index(end)
    ):
        raise ValueError("generated 표지의 짝·순서 오류")
    before, rest = original.split(begin)
    _, after = rest.split(end)
    body = render(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(before + begin + b"\n\n" + body.encode() + b"\n\n" + end + after)
    return path


if __name__ == "__main__":
    print(write_report())
