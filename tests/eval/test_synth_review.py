"""Human-review sampling, templates, and deterministic aggregation."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from kodocs.eval.synth_report import render_tables
from kodocs.eval.synth_review import (
    CSV_COLUMNS,
    SAMPLE_SIZE,
    draw_sample,
    score,
    write_templates,
)

DOC_TYPES = ("meeting", "weekly", "incident", "runbook", "guide", "notice")
ACCESS_LEVELS = ("public", "team", "restricted")


def _documents() -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    number = 0
    for type_index, doc_type in enumerate(DOC_TYPES):
        for access_index, access_level in enumerate(ACCESS_LEVELS):
            count = 2 + (type_index + access_index) % 4
            for offset in range(count):
                number += 1
                documents.append(
                    {
                        "doc_id": f"doc-{number:03d}",
                        "doc_type": doc_type,
                        "access": {"level": access_level},
                        "source_fact_ids": [f"f-{number:03d}"],
                        "canary": (
                            {"canary_id": f"cn-{number:03d}"}
                            if access_level == "restricted"
                            else None
                        ),
                        "injection": (
                            {"injection_id": "inj-01"} if number == 1 else None
                        ),
                    }
                )
    return documents


def _write_population(root: Path, documents: list[dict[str, object]]) -> None:
    company = root / "data/seed/company.yaml"
    company.parent.mkdir(parents=True, exist_ok=True)
    company.write_text("random_seed: 20250101\n", encoding="utf-8")
    docs_dir = root / "data/synthetic/docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for document in documents:
        path = docs_dir / f"{document['doc_id']}.json"
        path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _write_review(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _completed_rows(root: Path) -> list[dict[str, str]]:
    rows = _read_csv(root / "data/synthetic/review/review_template.csv")
    sample = json.loads(
        (root / "data/synthetic/review/sample.json").read_text(encoding="utf-8")
    )
    injection_ids = {
        document["doc_id"]
        for document in sample["docs"]
        if json.loads((root / document["path"]).read_text(encoding="utf-8"))[
            "injection"
        ]
        is not None
    }
    for index, row in enumerate(rows):
        row.update(
            {
                "reviewer": "검수자",
                "reviewed_on": "2026-09-27",
                "fact_consistency": "0" if index == 0 else "1",
                "fact_errors": "f-001: 시드와 담당자 불일치" if index == 0 else "",
                "style_natural": str(index % 3 + 1),
                "style_notes": "",
                "realism": str((index + 1) % 3 + 1),
                "realism_notes": "",
                "injection_visible": "y" if row["doc_id"] in injection_ids else "",
                "other_issues": "",
            }
        )
    return rows


def test_draw_sample_is_deterministic_and_meets_all_strata_constraints() -> None:
    documents = _documents()

    first = draw_sample(documents, random_seed=20258109)
    second = draw_sample(list(reversed(documents)), random_seed=20258109)

    assert first == second
    assert len(first) == SAMPLE_SIZE
    assert {row["doc_type"] for row in first} == set(DOC_TYPES)
    assert {row["access_level"] for row in first} == set(ACCESS_LEVELS)
    assert min(Counter(row["doc_type"] for row in first).values()) >= 3
    assert min(Counter(row["access_level"] for row in first).values()) >= 4
    assert sum(bool(row["injection"]) for row in first) >= 1
    assert (
        sum(
            bool(row["canary"]) and row["access_level"] == "restricted" for row in first
        )
        >= 4
    )


def test_write_templates_records_constraint_shortfalls(tmp_path: Path) -> None:
    documents = _documents()[:20]
    for document in documents:
        document["doc_type"] = "meeting"
        document["access"] = {"level": "public"}
        document["canary"] = None
        document["injection"] = None
    _write_population(tmp_path, documents)

    assert write_templates(tmp_path) == 0

    sample = json.loads(
        (tmp_path / "data/synthetic/review/sample.json").read_text(encoding="utf-8")
    )
    assert sample["size"] == 20
    assert sample["shortfalls"]
    assert any("유형" in reason for reason in sample["shortfalls"])
    assert any("인젝션" in reason for reason in sample["shortfalls"])
    assert any("카나리아" in reason for reason in sample["shortfalls"])


def test_write_templates_rejects_population_below_twenty(tmp_path: Path) -> None:
    _write_population(tmp_path, _documents()[:19])

    assert write_templates(tmp_path) == 1
    assert not (tmp_path / "data/synthetic/review/sample.json").exists()


def test_review_template_has_exact_columns_and_prefilled_values(
    tmp_path: Path,
) -> None:
    _write_population(tmp_path, _documents())

    assert write_templates(tmp_path) == 0

    template = tmp_path / "data/synthetic/review/review_template.csv"
    assert not template.read_bytes().startswith(b"\xef\xbb\xbf")
    with template.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        assert reader.fieldnames == CSV_COLUMNS
    assert len(rows) == SAMPLE_SIZE
    assert all(
        row["doc_id"] and row["doc_type"] and row["access_level"] for row in rows
    )
    assert all(not row[column] for row in rows for column in CSV_COLUMNS[3:])
    guide = (tmp_path / "data/synthetic/review/README.md").read_text(encoding="utf-8")
    assert "body" in guide and "fact_spans" in guide
    assert "data/golden/synthetic_review/{검수일}-review.csv" in guide
    assert "문서 본문을 고치지" in guide


def test_score_uses_latest_rows_and_computes_exact_aggregates(
    tmp_path: Path,
) -> None:
    _write_population(tmp_path, _documents())
    assert write_templates(tmp_path) == 0
    rows = _completed_rows(tmp_path)
    older = dict(rows[1])
    older["reviewed_on"] = "2026-09-26"
    older["style_natural"] = "1"
    _write_review(
        tmp_path / "data/golden/synthetic_review/2026-09-27-review.csv",
        [older, *rows],
    )
    golden = tmp_path / "data/golden"
    golden_before = {
        path.relative_to(golden): path.read_bytes()
        for path in golden.rglob("*")
        if path.is_file()
    }

    assert score(tmp_path) == 0
    assert {
        path.relative_to(golden): path.read_bytes()
        for path in golden.rglob("*")
        if path.is_file()
    } == golden_before

    summary = json.loads(
        (tmp_path / "data/manifest/synth_review.json").read_text(encoding="utf-8")
    )
    assert summary["completion"] == {
        "expected": 20,
        "rate": 1.0,
        "reviewed": 20,
    }
    assert summary["fact_consistency"] == {
        "consistent": 19,
        "rate": 0.95,
        "reviewed": 20,
    }
    assert summary["style_natural"]["mean"] == 1.95
    assert summary["style_natural"]["distribution"] == {"1": 7, "2": 7, "3": 6}
    assert summary["realism"]["mean"] == 2.05
    assert summary["fact_errors"] == [
        {
            "description": "f-001: 시드와 담당자 불일치",
            "doc_id": rows[0]["doc_id"],
            "fact_ids": ["f-001"],
        }
    ]
    assert summary["invalid_rows"] == []
    assert summary["missing_doc_ids"] == []
    assert set(summary["style_natural"]["by_type"]) == set(DOC_TYPES)
    assert set(summary["realism"]["by_access"]) == set(ACCESS_LEVELS)
    assert summary["injection_visible"]["rate"] == 1.0

    rendered = render_tables(tmp_path)
    assert "## 7. 사람 검수(20건)" in rendered
    assert "20/20 (100.0%)" in rendered
    assert "95.0%" in rendered
    assert "1.9500" in rendered


def test_score_excludes_only_invalid_row_and_reports_reason(
    tmp_path: Path, capsys: object
) -> None:
    _write_population(tmp_path, _documents())
    assert write_templates(tmp_path) == 0
    rows = _completed_rows(tmp_path)
    invalid_doc_id = rows[5]["doc_id"]
    rows[5]["style_natural"] = "4"
    _write_review(tmp_path / "data/golden/synthetic_review/2026-09-27-review.csv", rows)

    assert score(tmp_path) == 0

    summary = json.loads(
        (tmp_path / "data/manifest/synth_review.json").read_text(encoding="utf-8")
    )
    assert summary["completion"]["reviewed"] == 19
    assert summary["missing_doc_ids"] == [invalid_doc_id]
    assert len(summary["invalid_rows"]) == 1
    assert "style_natural" in summary["invalid_rows"][0]["reasons"][0]
    assert invalid_doc_id in capsys.readouterr().err  # type: ignore[attr-defined]


def test_score_without_results_returns_one_and_never_writes_golden(
    tmp_path: Path,
) -> None:
    _write_population(tmp_path, _documents())
    assert write_templates(tmp_path) == 0
    golden = tmp_path / "data/golden"
    golden.mkdir(parents=True)
    sentinel = golden / "existing.csv"
    sentinel.write_text("사람이 확정한 내용\n", encoding="utf-8")
    before = {
        path.relative_to(golden): path.read_bytes()
        for path in golden.rglob("*")
        if path.is_file()
    }

    assert score(tmp_path) == 1

    after = {
        path.relative_to(golden): path.read_bytes()
        for path in golden.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "data/manifest/synth_review.json").exists()
