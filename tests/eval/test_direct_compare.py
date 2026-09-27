"""Tests for paired Upstage PDF-vs-original comparison."""

import io
import json
from pathlib import Path

import pytest

from kodocs.eval.direct_compare import compare
from kodocs.eval.parsing_report import render_tables
from kodocs.eval.parsing_score import run as run_parsing_score
from kodocs.eval.table_score import run as run_table_score
from kodocs.ingest.manifest import sha256_file
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
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def _doc(
    parser: str,
    target: str,
    sha: str,
    *,
    structure: bool,
    precision: float,
    recall: float,
    raw: float,
    normalized: float,
) -> dict[str, object]:
    return {
        "parser": parser,
        "target_key": target,
        "input_sha256": sha,
        "status": "ok",
        "structure_exact": structure,
        "key_precision": precision,
        "key_recall": recall,
        "text_sim_raw_mean": raw,
        "text_sim_norm_mean": normalized,
    }


def test_compare_uses_only_targets_with_both_results_and_ledger_refs(
    tmp_path: Path,
) -> None:
    paired = "law:serial:source"
    pdf_only = "pdf:only:source"
    direct_only = "direct:only:source"
    pdf_sha = "p" * 64
    source_sha = "s" * 64
    _write_json(
        tmp_path / "data/manifest/parse_scores.json",
        {
            "parsers": {
                "upstage": {
                    "status": "scored",
                    "documents": [
                        _doc(
                            "upstage",
                            paired,
                            pdf_sha,
                            structure=True,
                            precision=1.0,
                            recall=0.5,
                            raw=0.8,
                            normalized=0.9,
                        ),
                        _doc(
                            "upstage",
                            pdf_only,
                            "x" * 64,
                            structure=True,
                            precision=1.0,
                            recall=1.0,
                            raw=1.0,
                            normalized=1.0,
                        ),
                    ],
                },
                "upstage-direct": {
                    "status": "scored",
                    "documents": [
                        _doc(
                            "upstage-direct",
                            paired,
                            source_sha,
                            structure=False,
                            precision=0.5,
                            recall=0.5,
                            raw=0.7,
                            normalized=0.85,
                        ),
                        _doc(
                            "upstage-direct",
                            direct_only,
                            "y" * 64,
                            structure=True,
                            precision=1.0,
                            recall=1.0,
                            raw=1.0,
                            normalized=1.0,
                        ),
                    ],
                },
            }
        },
    )
    _write_json(
        tmp_path / "data/manifest/table_scores.json",
        {
            "scores": [
                {
                    "golden_id": "paired-table",
                    "parser": "upstage",
                    "status": "ok",
                    "cell_exact_rate": 0.75,
                },
                {
                    "golden_id": "paired-table",
                    "parser": "upstage-direct",
                    "status": "ok",
                    "cell_exact_rate": 1.0,
                },
                {
                    "golden_id": "one-sided",
                    "parser": "upstage",
                    "status": "ok",
                    "cell_exact_rate": 1.0,
                },
            ]
        },
    )
    _write_jsonl(
        tmp_path / "data/manifest/parse_runs.jsonl",
        [
            {
                "parser": "upstage",
                "target_key": paired,
                "input_sha256": pdf_sha,
                "status": "ok",
                "elapsed_s": 4.0,
                "page_count": 2,
            },
            {
                "parser": "upstage-direct",
                "target_key": paired,
                "input_sha256": source_sha,
                "status": "ok",
                "elapsed_s": 9.0,
                "page_count": 3,
            },
            {
                "parser": "upstage",
                "target_key": pdf_only,
                "input_sha256": "x" * 64,
                "status": "ok",
                "elapsed_s": 100.0,
                "page_count": 10,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "data/manifest/costs.jsonl",
        [
            {"ref": pdf_sha, "quantity": 2, "amount_usd": 0.022},
            {"ref": source_sha, "quantity": 3, "amount_usd": 0.033},
            {"ref": "x" * 64, "quantity": 10, "amount_usd": 0.11},
        ],
    )

    result = compare(tmp_path)

    assert result["paired_targets"] == 1
    assert result["paired_tables"] == 1
    pdf = result["upstage"]
    direct = result["upstage-direct"]
    assert pdf["structure_exact_rate"] == 1.0
    assert pdf["key_f1_mean"] == pytest.approx(2 / 3)
    assert pdf["text_sim_raw_mean"] == 0.8
    assert pdf["text_sim_norm_mean"] == 0.9
    assert pdf["table_cell_exact_rate"] == 0.75
    assert pdf["billed_pages"] == 2
    assert pdf["seconds_per_page"] == 2.0
    assert direct["structure_exact_rate"] == 0.0
    assert direct["key_f1_mean"] == 0.5
    assert direct["table_cell_exact_rate"] == 1.0
    assert direct["billed_pages"] == 3
    assert direct["seconds_per_page"] == 3.0
    rendered = render_tables(tmp_path)
    assert "변환 경유 vs 직접 입력" in rendered
    assert "양쪽 결과가 모두 있는 대상 1개" in rendered
    assert "HWP 과금 쪽수 = 응답 usage.pages" in rendered


def test_compare_returns_none_without_a_complete_pair(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "data/manifest/parse_scores.json",
        {
            "parsers": {
                "upstage": {
                    "status": "scored",
                    "documents": [
                        _doc(
                            "upstage",
                            "pdf:only:source",
                            "p" * 64,
                            structure=True,
                            precision=1.0,
                            recall=1.0,
                            raw=1.0,
                            normalized=1.0,
                        )
                    ],
                }
            }
        },
    )

    assert compare(tmp_path) is None


def test_article_and_table_scorers_join_direct_results_by_source_sha(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "data/raw/pdf/input.pdf"
    source_path = tmp_path / "data/raw/alio/input.hwpx"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"converted-pdf")
    source_path.write_bytes(b"original-hwpx")
    pdf_sha = sha256_file(pdf_path)
    source_sha = sha256_file(source_path)
    target_key = f"law:serial:{source_sha}"
    _write_json(
        tmp_path / "data/manifest/parse_targets.json",
        {
            "targets": [
                {
                    "law_rule_id": "law",
                    "law_serial": "serial",
                    "rule_no": "1",
                    "source_sha256": source_sha,
                    "source_path": source_path.relative_to(tmp_path).as_posix(),
                    "source_kind": "hwpx",
                    "pdf_path": pdf_path.relative_to(tmp_path).as_posix(),
                    "page_count": 1,
                    "role": "current",
                }
            ]
        },
    )
    _write_json(
        tmp_path / "data/interim/law/law/serial.json",
        {"articles": ["제1조(목적) 본문"], "addenda": [], "annexes": []},
    )
    parsed = ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser="upstage-direct",
        parser_version="test",
        settings={},
        settings_hash="settings",
        input_path=source_path.relative_to(tmp_path).as_posix(),
        input_sha256=source_sha,
        input_kind="hwpx",
        page_count=1,
        blocks=(
            Block(page=1, kind="text", text="제1조(목적) 본문"),
            Block(page=1, kind="table", text="|정답|"),
        ),
        tables=(
            ParsedTable(
                page=1,
                block_position=1,
                cells=(TableCell(0, 0, 1, 1, "정답"),),
            ),
        ),
        elapsed_s=0.1,
    )
    parsed_path = output_path(tmp_path, parsed.parser, source_sha)
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_path.write_text(to_json(parsed), encoding="utf-8")
    _write_jsonl(
        tmp_path / "data/manifest/parse_runs.jsonl",
        [
            {
                "parser": "upstage-direct",
                "target_key": target_key,
                "input_sha256": source_sha,
                "status": "ok",
            }
        ],
    )

    parsing_payload = run_parsing_score(
        tmp_path, parsers=["upstage-direct"], out=io.StringIO()
    )

    direct_doc = parsing_payload["parsers"]["upstage-direct"]["documents"][0]
    assert direct_doc["status"] == "ok"
    assert direct_doc["input_sha256"] == source_sha
    assert direct_doc["structure_exact"] is True

    golden_path = tmp_path / "data/golden/tables/golden.md"
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(
        "---\n"
        'rule_no: "1"\n'
        f'source_sha256: "{source_sha}"\n'
        f'pdf_sha256: "{pdf_sha}"\n'
        'section: "Contents/section0.xml"\n'
        "table_ordinal: 0\n"
        'annex_label: "[별표 1]"\n'
        'annex_kind: "별표"\n'
        "shape: [1, 1]\n"
        "nested: false\n"
        'reviewed_by: "reviewer"\n'
        'reviewed_on: "2026-09-27"\n'
        "---\n"
        "|정답|\n|---|\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "data/manifest/table_golden.json",
        {
            "tables": [
                {
                    "id": "golden",
                    "source_sha256": source_sha,
                    "pdf_sha256": pdf_sha,
                    "golden_file_sha256": sha256_file(golden_path),
                }
            ]
        },
    )

    table_payload = run_table_score(tmp_path, out=io.StringIO())

    direct_table = next(
        score
        for score in table_payload["scores"]
        if score["parser"] == "upstage-direct"
    )
    assert direct_table["status"] == "ok"
    assert direct_table["cell_exact_rate"] == 1.0
