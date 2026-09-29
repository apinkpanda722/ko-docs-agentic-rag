"""Human-review packets, promotion, and fixed M4 report contracts."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from kodocs.eval import golden_review, m4_report
from kodocs.eval.golden import (
    GOLD_QUOTA,
    SILVER_QUOTA,
    Asker,
    Qrel,
    Question,
    question_to_dict,
    write_questions,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _question(
    question_id: str,
    question_set: str,
    source: str,
    question_type: str,
    *,
    ordinal: int,
) -> Question:
    no_answer = question_type == "unanswerable"
    sql = question_type == "sql"
    if source == "real":
        qrels = (
            ()
            if no_answer
            else (
                Qrel(
                    doc_id=f"law-{10_000 + ordinal}",
                    rel=2,
                    article_key="제1조",
                ),
            )
        )
    elif no_answer or sql:
        qrels = ()
    else:
        body = f"문서 앞부분 {question_id}의 정답 구간은 검수 대상입니다. 문서 뒷부분"
        answer = "정답 구간"
        start = body.index(answer)
        qrels = (
            Qrel(
                doc_id=f"doc-{question_id}",
                rel=2,
                fact_id=f"fact-{question_id}",
                start=start,
                end=start + len(answer),
                span_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            ),
        )
    expected = (
        "no_answer"
        if no_answer
        else (
            "indistinguishable_from_no_answer"
            if question_type == "permission"
            else "answer"
        )
    )
    tags = (
        ("temporal:revision",)
        if question_type == "temporal" and source == "real"
        else (("temporal:as_of_event",) if question_type == "temporal" else ())
    )
    return Question(
        id=question_id,
        set=question_set,
        source=source,
        type=question_type,
        tags=tags,
        question=f"{question_id} 질문은 무엇인가요?",
        asker=Asker(team="platform", grade=3),
        as_of="2026-01-01" if question_type == "temporal" else None,
        qrels=qrels,
        anchor_values=(question_id,),
        answer_values=() if no_answer else ("정답 구간",),
        expected_response=expected,
        sql=None,
        reference_answer=(
            f"{question_id}의 실제 규정 참조 답변" if source == "real" else "정답 구간"
        ),
        reference_sha256=None,
        summary80=(f"{question_id} 실제 규정 요지" if source == "real" else None),
        split=None,
        no_answer_checks=(
            {"seed": True, "docs": True, "real": True} if no_answer else None
        ),
    )


def _fixture_questions() -> tuple[list[Question], list[Question]]:
    synthetic: list[Question] = []
    real: list[Question] = []
    counters = {"g-syn": 0, "g-real": 0}
    ordinal = 0
    for (question_type, source), count in GOLD_QUOTA.items():
        for _ in range(count):
            ordinal += 1
            prefix = "g-real" if source == "real" else "g-syn"
            counters[prefix] += 1
            question = _question(
                f"{prefix}-{counters[prefix]:03d}",
                "gold",
                source,
                question_type,
                ordinal=ordinal,
            )
            (real if source == "real" else synthetic).append(question)
    for question_type, count in SILVER_QUOTA.items():
        for number in range(1, count + 1):
            ordinal += 1
            synthetic.append(
                _question(
                    f"s-{question_type}-{number:03d}",
                    "silver",
                    "synthetic",
                    question_type,
                    ordinal=ordinal,
                )
            )
    return synthetic, real


def _prepare_root(root: Path, *, attention_checks: int = 3) -> None:
    synthetic, real = _fixture_questions()
    write_questions(root / "data/synthetic/queries/validated.jsonl", synthetic)
    projected: list[Question] = []
    draft_rows: list[dict[str, object]] = []
    for question in real:
        answer = question.reference_answer or ""
        projected.append(
            replace(
                question,
                reference_answer=None,
                reference_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            )
        )
        draft_rows.append(question_to_dict(question))
    write_questions(root / "data/synthetic/queries/real_projection.jsonl", projected)
    _write_jsonl(root / "data/interim/golden/real_drafts.jsonl", draft_rows)

    for question in synthetic:
        for qrel in question.qrels:
            if qrel.rel != 2:
                continue
            body = (
                f"문서 앞부분 {question.id}의 정답 구간은 검수 대상입니다. 문서 뒷부분"
            )
            _write_json(
                root / f"data/synthetic/docs/{qrel.doc_id}.json",
                {
                    "doc_id": qrel.doc_id,
                    "title": f"{question.id} 문서",
                    "body": body,
                    "fact_spans": [
                        {
                            "fact_id": qrel.fact_id,
                            "start": qrel.start,
                            "end": qrel.end,
                        }
                    ],
                },
            )
    for question in real:
        for qrel in question.qrels:
            serial = qrel.doc_id.removeprefix("law-")
            _write_json(
                root / f"data/interim/law/rule-{serial}/{serial}.json",
                {
                    "name": f"가짜 규정 {serial}",
                    "name_without_agency": f"가짜 규정 {serial}",
                    "serial": serial,
                    "rule_id": f"rule-{serial}",
                    "issued_date": "2026-01-01",
                    "is_current": True,
                    "articles": [
                        "제1조(목적) 실제 규정의 정답 구간과 충분한 검수 문맥을 제공한다."
                    ],
                    "addenda": [],
                    "annexes": [],
                },
            )

    all_ids = [question.id for question in [*synthetic, *real]]
    _write_jsonl(
        root / golden_review.CLAUDE_VERDICTS,
        [
            {"question_id": question_id, "q1": True, "q2": True, "q3": True, "note": ""}
            for question_id in all_ids
        ],
    )
    _write_json(
        root / "data/manifest/golden_real.json",
        {
            "synthetic_no_answer_real_check": {
                question.id: True
                for question in synthetic
                if question.type == "unanswerable"
            },
            "no_answer_checks": {
                question.id: True
                for question in real
                if question.type == "unanswerable"
            },
        },
    )
    company = root / "data/seed/company.yaml"
    company.parent.mkdir(parents=True, exist_ok=True)
    company.write_text(yaml.safe_dump({"random_seed": 20250101}), encoding="utf-8")
    assert golden_review.build_packets(root, attention_checks=attention_checks) == 0


def _review_rows(root: Path) -> list[dict[str, str]]:
    with (root / golden_review.TEMPLATE_CSV).open(
        encoding="utf-8-sig", newline=""
    ) as source:
        return list(csv.DictReader(source))


def _write_review(
    root: Path,
    rows: list[dict[str, str]],
    *,
    name: str = "2026-09-29-review.csv",
) -> Path:
    path = root / "data/golden/m4_review" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=golden_review.CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _sign(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row["reviewer"] = "human-reviewer"
        row["reviewed_on"] = "2026-09-29"


def test_silver_sample_is_deterministic_stratified_twenty_percent() -> None:
    synthetic, _ = _fixture_questions()
    silver = [question for question in synthetic if question.set == "silver"]

    first = golden_review.silver_sample(silver, random_seed=77)
    second = golden_review.silver_sample(list(reversed(silver)), random_seed=77)

    assert [question.id for question in first] == [question.id for question in second]
    assert len(first) == 30
    assert Counter(question.type for question in first) == {
        question_type: count // 5 for question_type, count in SILVER_QUOTA.items()
    }


def test_packets_prefill_only_claude_fields_and_keep_human_fields_blank(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)

    rows = _review_rows(tmp_path)
    assert len(rows) == 83
    assert all(
        row[field] == ""
        for row in rows
        for field in ("reviewer", "reviewed_on", "disagree", "correction")
    )
    assert sum(row["question_id"].startswith("chk-") for row in rows) == 3
    assert not (tmp_path / "data/golden").exists()
    packet = (tmp_path / golden_review.PACKETS_DIR / "review_packet.md").read_text(
        encoding="utf-8"
    )
    assert "**정답 구간**" in packet
    assert "질문자" in packet
    assert "Claude 판정" in packet


def test_unsigned_gold_row_is_unreviewed_and_blocks_gold_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_root(tmp_path)
    rows = _review_rows(tmp_path)
    _sign(rows)
    unsigned = next(row for row in rows if row["set"] == "gold")
    unsigned["reviewer"] = ""
    unsigned["reviewed_on"] = ""
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 1
    assert not (tmp_path / golden_review.GOLD_PATH).exists()
    assert unsigned["question_id"] in capsys.readouterr().out


def test_attention_checks_are_detected_but_never_promoted_or_split(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)
    rows = _review_rows(tmp_path)
    _sign(rows)
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 0
    summary = json.loads((tmp_path / golden_review.SUMMARY_PATH).read_text())
    assert summary["attention_checks"] == {
        "n": 3,
        "detected": 0,
        # Committed manifests must not carry the private dummy ID prefix.
        "missed_ids": ["001", "002", "003"],
    }
    for relative in (golden_review.GOLD_PATH, golden_review.SILVER_PATH):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        assert "chk-" not in text
    assert "chk-" not in json.dumps(summary["split"], ensure_ascii=False)

    key = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())
    wrong_items = {
        item["question_id"]: str(item["wrong_item"]) for item in key["attention_checks"]
    }
    for row in rows:
        wrong = wrong_items.get(row["question_id"])
        if wrong is None:
            continue
        row["disagree"] = wrong
        row["correction"] = "remove:not-present" if wrong == "2" else "사람 판정"
    _write_review(tmp_path, rows, name="2026-09-30-review.csv")

    assert golden_review.promote(tmp_path) == 0
    detected = json.loads((tmp_path / golden_review.SUMMARY_PATH).read_text())
    assert detected["attention_checks"] == {
        "n": 3,
        "detected": 3,
        "missed_ids": [],
    }


def test_real_gold_is_projection_only_and_existing_gold_is_immutable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_root(tmp_path, attention_checks=0)
    rows = _review_rows(tmp_path)
    _sign(rows)
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 0
    gold_path = tmp_path / golden_review.GOLD_PATH
    real_rows = [
        json.loads(line)
        for line in gold_path.read_text(encoding="utf-8").splitlines()
        if '"source":"real"' in line
    ]
    assert len(real_rows) == 20
    assert all("reference_answer" not in row for row in real_rows)
    assert all(row["reference_sha256"] for row in real_rows)

    gold_path.write_text("different confirmed gold\n", encoding="utf-8")
    assert golden_review.promote(tmp_path) == 1
    assert gold_path.read_text(encoding="utf-8") == "different confirmed gold\n"
    assert "골드는 확정 후 수정하지 않는다" in capsys.readouterr().out


def test_negative_silver_sample_item_is_removed_and_wilson_interval_reported(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path, attention_checks=0)
    rows = _review_rows(tmp_path)
    _sign(rows)
    silver = next(row for row in rows if row["set"] == "silver")
    silver["disagree"] = "1"
    silver["correction"] = "답변 가능 판정에 동의하지 않음"
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 0
    promoted = (tmp_path / golden_review.SILVER_PATH).read_text(encoding="utf-8")
    assert silver["question_id"] not in promoted
    summary = json.loads((tmp_path / golden_review.SUMMARY_PATH).read_text())
    assert summary["silver_sample"]["n"] == 30
    assert summary["silver_sample"]["negative"] == 1
    assert summary["silver_sample"]["error_rate"] == pytest.approx(1 / 30, abs=1e-4)
    assert len(summary["silver_sample"]["wilson95"]) == 2


def test_committed_question_and_manifest_paths_have_no_attention_ids() -> None:
    paths = [
        *sorted((ROOT / "data/golden/queries").glob("**/*")),
        *sorted((ROOT / "data/synthetic/queries").glob("**/*")),
        *sorted((ROOT / "data/manifest").glob("golden_*.json")),
    ]
    for path in paths:
        if path.is_file():
            assert "chk-" not in path.read_text(encoding="utf-8")


def test_m4_report_uses_one_fixed_path_and_preserves_manual_regions(
    tmp_path: Path,
) -> None:
    destination = tmp_path / m4_report.REPORT_PATH
    destination.parent.mkdir(parents=True)
    destination.write_text(
        "# custom\n\n<!-- BEGIN generated -->\nold\n<!-- END generated -->\n\nmanual\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / golden_review.SUMMARY_PATH,
        {
            "gold": {
                "reviewed": 50,
                "confirmed": 50,
                "disagree_by_item": {
                    "q1_answerable": 0,
                    "q2_qrels_complete": 0,
                    "q3_labels_correct": 0,
                },
                "acceptance_rate": 1.0,
            },
            "silver_sample": {
                "n": 30,
                "negative": 0,
                "error_rate": 0.0,
                "wilson95": [0.0, 0.1135],
            },
            "attention_checks": {"n": 3, "detected": 3, "missed_ids": []},
            "split": {},
            "inputs": {},
        },
    )

    written = m4_report.write_report(tmp_path, run_date=date(2026, 9, 29))

    assert written == destination
    text = destination.read_text(encoding="utf-8")
    assert text.startswith("# custom")
    assert text.endswith("manual\n")
    assert "2026-09-29" in text
    assert "커밋" in text and "측정 환경" in text
    assert list((tmp_path / "docs/results").glob("*-m4-golden-set.md")) == [destination]
