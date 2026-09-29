"""Tests for the deterministic M5 corpus loader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kodocs.chunking.corpus import (
    Access,
    check_synthetic_qrels,
    load_corpus,
    normalize_regulation_ref,
    write_prior_versions_manifest,
)
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    ParsedTable,
    TableCell,
    output_path,
    to_json,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, values: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(value, ensure_ascii=False)}\n" for value in values),
        encoding="utf-8",
    )


def _minimal_root(root: Path) -> None:
    _write_json(root / "data/manifest/parse_targets.json", {"targets": []})
    _write_jsonl(root / "data/golden/queries/gold.jsonl", [])
    _write_jsonl(root / "data/golden/queries/silver.jsonl", [])
    services = root / "data/seed/services.yaml"
    services.parent.mkdir(parents=True, exist_ok=True)
    services.write_text(
        "services:\n  - name: core-gateway\n    owner_team: platform\n",
        encoding="utf-8",
    )


def _synthetic_payload(
    *,
    doc_id: str,
    doc_type: str,
    body: str,
    title: str = "테스트 문서",
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "body": body,
        "title": title,
        "access": {"level": "team", "teams": ["platform"], "min_grade": 2},
        "team": "platform",
        "date": "2026-01-02",
    }


def _parsed_document(
    parser: str,
    input_sha256: str,
    blocks: tuple[Block, ...],
    *,
    tables: tuple[ParsedTable, ...] = (),
) -> ParsedDocument:
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser,
        parser_version="test",
        settings={},
        settings_hash=f"{parser}-settings",
        input_path="data/raw/pdf/test.pdf",
        input_sha256=input_sha256,
        input_kind="pdf",
        page_count=2,
        blocks=blocks,
        tables=tables,
        elapsed_s=0.0,
    )


def _write_current_fixture(root: Path) -> tuple[ParsedDocument, ParsedDocument]:
    _minimal_root(root)
    rule_id = "rule-1"
    serial = "current-1"
    source_sha256 = "a" * 64
    target_key = f"{rule_id}:{serial}:{source_sha256}"
    _write_json(
        root / "data/manifest/parse_targets.json",
        {
            "targets": [
                {
                    "role": "current",
                    "law_rule_id": rule_id,
                    "law_serial": serial,
                    "source_sha256": source_sha256,
                    "issued_date": "2026-01-01",
                }
            ]
        },
    )
    _write_json(
        root / f"data/interim/law/{rule_id}/{serial}.json",
        {
            "articles": ["제1조(목적) 법제처 본문"],
            "addenda": [],
            "annexes": [],
            "issued_date": "2026-01-01",
            "rule_id": rule_id,
            "serial": serial,
            "name": "(가상기관) 테스트규정",
            "name_without_agency": "테스트규정",
        },
    )

    pypdf_sha = "b" * 64
    pypdf = _parsed_document(
        "pypdf",
        pypdf_sha,
        (
            Block(1, "heading", "테스트규정"),
            Block(1, "heading", "제1장 총칙"),
            Block(1, "heading", "제1절 일반"),
            Block(1, "text", "제1조(목적) pypdf  원문"),
            Block(2, "heading", "[별표 1] 성공 표"),
            Block(2, "text", "pypdf 성공 표 원문"),
            Block(2, "heading", "[별표 2] 대체 표"),
            Block(2, "text", "  pypdf fallback  원문  "),
        ),
    )
    upstage_sha = "c" * 64
    table = ParsedTable(
        page=2,
        block_position=4,
        cells=(
            TableCell(0, 0, 1, 1, "항목"),
            TableCell(0, 1, 1, 1, "값"),
            TableCell(1, 0, 1, 1, "가"),
            TableCell(1, 1, 1, 1, "업스테이지 원문"),
        ),
    )
    upstage = _parsed_document(
        "upstage",
        upstage_sha,
        (
            Block(1, "heading", "테스트규정"),
            Block(1, "heading", "제1장 총칙"),
            Block(1, "heading", "제1절 일반"),
            Block(1, "text", "제1조(목적) upstage 원문"),
            Block(2, "heading", "[별표 1] 성공 표"),
            Block(2, "table", "parser가 보존한 표 텍스트"),
        ),
        tables=(
            ParsedTable(
                page=table.page,
                block_position=table.block_position + 1,
                cells=table.cells,
            ),
        ),
    )
    for document in (pypdf, upstage):
        destination = output_path(root, document.parser, document.input_sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(to_json(document), encoding="utf-8")
    _write_jsonl(
        root / "data/manifest/parse_runs.jsonl",
        [
            {
                "parser": "pypdf",
                "target_key": target_key,
                "input_sha256": "0" * 64,
                "settings_hash": "stale-settings",
                "status": "error",
            },
            *(
                {
                    "parser": document.parser,
                    "target_key": target_key,
                    "input_sha256": document.input_sha256,
                    "settings_hash": document.settings_hash,
                    "status": "ok",
                }
                for document in (pypdf, upstage)
            ),
        ],
    )
    return pypdf, upstage


def test_synthetic_body_is_unchanged_and_meeting_agendas_are_sections(
    tmp_path: Path,
) -> None:
    _minimal_root(tmp_path)
    body = (
        "# 회의록\n\n## 3. 안건별 논의\n\n"
        "### 3.1 첫 안건\n\n원문  공백\n\n"
        "### 3.2 둘째 안건\n\n둘째 본문\n\n## 4. 결정 사항\n\n결정"
    )
    payload = _synthetic_payload(
        doc_id="syn-meeting-001", doc_type="meeting", body=body
    )
    _write_json(tmp_path / "data/synthetic/docs/syn-meeting-001.json", payload)

    documents, report = load_corpus(root=tmp_path)

    assert len(documents) == 1
    document = documents[0]
    assert document.text == body
    assert document.access == Access("team", ("platform",), 2)
    assert [section.kind for section in document.sections] == ["agenda", "agenda"]
    assert document.text[
        document.sections[0].start : document.sections[0].end
    ].startswith("### 3.1 첫 안건")
    assert (
        "## 4. 결정 사항"
        not in document.text[document.sections[-1].start : document.sections[-1].end]
    )
    assert report.synthetic_document_count == 1
    assert report.synthetic_zero_heading_section_document_count_by_type == {
        "meeting": 0
    }


@pytest.mark.parametrize(
    ("doc_type", "body", "expected_kind", "expected_count"),
    [
        ("incident", "# 장애\n## 1. 요약\nA\n## 2. 원인\nB", "heading_section", 2),
        ("weekly", "# 주간\n## 진행 사항\nA\n## 다음 주\nB", "heading_section", 2),
        ("runbook", "제목만 있고 구간 없음", "preamble", 1),
    ],
)
def test_synthetic_heading_rules_and_whole_document_fallback(
    tmp_path: Path,
    doc_type: str,
    body: str,
    expected_kind: str,
    expected_count: int,
) -> None:
    _minimal_root(tmp_path)
    doc_id = f"syn-{doc_type}-001"
    _write_json(
        tmp_path / f"data/synthetic/docs/{doc_id}.json",
        _synthetic_payload(doc_id=doc_id, doc_type=doc_type, body=body),
    )

    documents, report = load_corpus(root=tmp_path)

    assert len(documents[0].sections) == expected_count
    assert documents[0].sections[0].kind == expected_kind
    assert report.synthetic_zero_heading_section_document_count_by_type == {
        doc_type: int(expected_kind == "preamble")
    }


def test_identifier_extraction_uses_services_and_normalizes_regulation_refs(
    tmp_path: Path,
) -> None:
    _minimal_root(tmp_path)
    body = "PAY-1234 EXP-2026-0001 INC-2026-001 core-gateway 정보보안 기본지침 제37조"
    _write_json(
        tmp_path / "data/synthetic/docs/syn-guide-001.json",
        _synthetic_payload(doc_id="syn-guide-001", doc_type="guide", body=body),
    )

    documents, report = load_corpus(root=tmp_path)

    assert documents[0].identifiers == {
        "ticket": ("PAY-1234",),
        "approval": ("EXP-2026-0001",),
        "incident": ("INC-2026-001",),
        "service": ("core-gateway",),
        "regulation": ("정보보안기본지침제37조",),
    }
    assert report.service_names_source == "data/seed/services.yaml"
    assert report.identifier_extracted_document_count_by_kind == {
        "approval": 1,
        "incident": 1,
        "regulation": 1,
        "service": 1,
        "ticket": 1,
    }
    assert normalize_regulation_ref("규정  제5조의2") == "규정제5조의2"


def test_composite_uses_pypdf_articles_upstage_tables_and_fallback(
    tmp_path: Path,
) -> None:
    _write_current_fixture(tmp_path)

    documents, report = load_corpus("composite", root=tmp_path)

    assert len(documents) == 1
    document = documents[0]
    assert document.doc_id == "law-current-1"
    assert "제1조(목적) pypdf  원문" in document.text
    assert "제1조(목적) upstage 원문" not in document.text
    assert "업스테이지 원문" in document.text
    assert "  pypdf fallback  원문  " in document.text
    assert [(section.kind, section.key) for section in document.sections] == [
        ("preamble", None),
        ("article", "제1조"),
        ("annex", "별표1"),
        ("annex", "별표2"),
    ]
    article = next(
        section for section in document.sections if section.kind == "article"
    )
    assert article.path == ("테스트규정", "제1장 총칙", "제1조(목적)")
    assert document.identifiers["regulation"] == ("테스트규정제1조",)
    assert report.expected_current_real_document_count == 1
    assert report.loaded_current_real_document_count == 1
    assert report.composite_annex_fallback_count == 1


@pytest.mark.parametrize("condition", ["pypdf", "upstage"])
def test_single_parser_conditions_preserve_full_parser_text(
    tmp_path: Path, condition: str
) -> None:
    pypdf, upstage = _write_current_fixture(tmp_path)
    source = pypdf if condition == "pypdf" else upstage

    documents, report = load_corpus(condition, root=tmp_path)

    assert documents[0].text == "\n".join(block.text for block in source.blocks)
    assert report.composite_annex_fallback_count == 0


def test_missing_current_input_is_reported_without_stopping_other_documents(
    tmp_path: Path,
) -> None:
    _write_current_fixture(tmp_path)
    targets_path = tmp_path / "data/manifest/parse_targets.json"
    payload = json.loads(targets_path.read_text(encoding="utf-8"))
    payload["targets"].append(
        {
            "role": "current",
            "law_rule_id": "missing-rule",
            "law_serial": "missing-serial",
            "source_sha256": "f" * 64,
            "issued_date": "2026-01-02",
        }
    )
    _write_json(targets_path, payload)

    documents, report = load_corpus(root=tmp_path)

    assert [document.doc_id for document in documents] == ["law-current-1"]
    assert report.expected_current_real_document_count == 2
    assert report.loaded_current_real_document_count == 1
    assert report.skipped[0][0] == "law-missing-serial"
    assert report.skipped[0][1].startswith("FileNotFoundError:")


def test_prior_version_is_immediately_before_current_and_manifest_has_no_text(
    tmp_path: Path,
) -> None:
    _minimal_root(tmp_path)
    rule_id = "rule-temporal"
    current_serial = "300"
    _write_json(
        tmp_path / "data/manifest/parse_targets.json",
        {
            "targets": [
                {
                    "role": "current",
                    "law_rule_id": rule_id,
                    "law_serial": current_serial,
                    "source_sha256": "d" * 64,
                    "issued_date": "2026-03-01",
                }
            ]
        },
    )
    for serial, issued_date, article in (
        ("100", "2024-01-01", "제1조(구버전) 가장 오래된 본문"),
        ("200", "2025-02-01", "제1조(직전버전) 직전 본문"),
        (current_serial, "2026-03-01", "제1조(현행) 현행 본문"),
    ):
        _write_json(
            tmp_path / f"data/interim/law/{rule_id}/{serial}.json",
            {
                "articles": [article],
                "addenda": [],
                "annexes": [],
                "issued_date": issued_date,
                "rule_id": rule_id,
                "serial": serial,
                "name": "시간규정",
                "name_without_agency": "시간규정",
            },
        )
    _write_jsonl(
        tmp_path / "data/golden/queries/gold.jsonl",
        [
            {
                "id": "temporal-1",
                "tags": ["temporal:revision"],
                "qrels": [{"doc_id": f"law-{current_serial}"}],
            }
        ],
    )

    documents, report = load_corpus(root=tmp_path)

    prior = next(document for document in documents if not document.is_current)
    assert prior.doc_id == "law-200"
    assert prior.date == "2025-02-01"
    assert prior.superseded_by == "law-300"
    assert prior.text == "제1조(직전버전) 직전 본문"
    assert report.loaded_prior_real_document_count == 1
    destination = write_prior_versions_manifest(report.prior_versions, root=tmp_path)
    manifest_text = destination.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["versions"][0] == {
        "current_serial": "300",
        "current_issued_date": "2026-03-01",
        "prior_serial": "200",
        "prior_issued_date": "2025-02-01",
        "rule_id": rule_id,
    }
    assert "직전 본문" not in manifest_text


def test_check_synthetic_qrels_checks_gold_and_silver_without_local_dependencies(
    tmp_path: Path,
) -> None:
    _minimal_root(tmp_path)
    body = "앞부분 정답 구간 뒷부분"
    payload = _synthetic_payload(doc_id="syn-guide-001", doc_type="guide", body=body)
    _write_json(tmp_path / "data/synthetic/docs/syn-guide-001.json", payload)
    start = body.index("정답")
    end = start + len("정답 구간")
    digest = hashlib.sha256(body[start:end].encode()).hexdigest()
    query = {
        "id": "query-1",
        "source": "synthetic",
        "qrels": [
            {
                "doc_id": "syn-guide-001",
                "start": start,
                "end": end,
                "span_sha256": digest,
            }
        ],
    }
    _write_jsonl(tmp_path / "data/golden/queries/gold.jsonl", [query])
    _write_jsonl(tmp_path / "data/golden/queries/silver.jsonl", [query])
    documents, _ = load_corpus(root=tmp_path)

    result = check_synthetic_qrels(documents, root=tmp_path)

    assert result.checked_qrel_count == 2
    assert result.mismatch_count == 0
    assert result.mismatches == ()
