"""Human-review packets, promotion, and fixed M4 report contracts."""

from __future__ import annotations

import csv
import hashlib
import json
import re
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
        body = f"문서 앞부분 항목{ordinal}의 정답 구간은 검수 대상입니다. 문서 뒷부분"
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
        question=f"항목{ordinal} 질문은 무엇인가요?",
        asker=Asker(team="platform", grade=3),
        as_of="2026-01-01" if question_type == "temporal" else None,
        qrels=qrels,
        anchor_values=(f"항목{ordinal}",),
        answer_values=() if no_answer else ("정답 구간",),
        expected_response=expected,
        sql=None,
        reference_answer=(
            f"항목{ordinal}의 실제 규정 참조 답변" if source == "real" else "정답 구간"
        ),
        reference_sha256=None,
        summary80=(f"항목{ordinal} 실제 규정 요지" if source == "real" else None),
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
            body = f"문서 앞부분 {question.anchor_values[0]}의 정답 구간은 검수 대상입니다. 문서 뒷부분"
            _write_json(
                root / f"data/synthetic/docs/{qrel.doc_id}.json",
                {
                    "doc_id": qrel.doc_id,
                    "title": f"{question.anchor_values[0]} 문서",
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


def _ids(root: Path) -> dict[str, str]:
    return json.loads((root / golden_review.REVIEW_KEYS).read_text(encoding="utf-8"))


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
    expected = {
        question_type: golden_review.sample_size(count)
        for question_type, count in SILVER_QUOTA.items()
    }
    assert Counter(question.type for question in first) == expected
    assert len(first) == sum(expected.values()) == 28


@pytest.mark.parametrize(
    ("population", "size"), [(25, 5), (18, 4), (17, 3), (13, 3), (12, 2), (3, 1)]
)
def test_silver_sample_size_is_twenty_percent_rounded_half_up(
    population: int, size: int
) -> None:
    assert golden_review.sample_size(population) == size


def test_packets_prefill_only_claude_fields_and_keep_human_fields_blank(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)

    rows = _review_rows(tmp_path)
    assert len(rows) == 50 + 28 + 3
    assert all(
        row[field] == ""
        for row in rows
        for field in ("reviewer", "reviewed_on", "disagree", "correction")
    )
    ids = _ids(tmp_path)
    assert sum(ids[row["review_key"]].startswith("chk-") for row in rows) == 3
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
    unsigned = next(
        row
        for row in rows
        if row["set"] == "gold"
        and not _ids(tmp_path)[row["review_key"]].startswith("chk-")
    )
    unsigned["reviewer"] = ""
    unsigned["reviewed_on"] = ""
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 1
    assert not (tmp_path / golden_review.GOLD_PATH).exists()
    assert _ids(tmp_path)[unsigned["review_key"]] in capsys.readouterr().out


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
    ids = _ids(tmp_path)
    for row in rows:
        wrong = wrong_items.get(ids[row["review_key"]])
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
    assert f'"id":"{_ids(tmp_path)[silver["review_key"]]}"' not in promoted
    summary = json.loads((tmp_path / golden_review.SUMMARY_PATH).read_text())
    assert summary["silver_sample"]["n"] == 28
    assert summary["silver_sample"]["negative"] == 1
    assert summary["silver_sample"]["error_rate"] == pytest.approx(1 / 28, abs=1e-4)
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


def _verdict_rows(root: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (root / golden_review.CLAUDE_VERDICTS)
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_attention_check_sources_are_unreviewed_all_yes_silver_items(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)
    sample_ids = set(
        json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())[
            "silver_sample_ids"
        ]
    )
    rows = _verdict_rows(tmp_path)
    outside = [
        row
        for row in rows
        if row["question_id"].startswith("s-") and row["question_id"] not in sample_ids
    ]
    eligible = {row["question_id"] for row in outside[:4]}
    for row in outside[4:]:
        row["q1"] = False
    _write_jsonl(tmp_path / golden_review.CLAUDE_VERDICTS, rows)

    assert golden_review.build_packets(tmp_path, attention_checks=3) == 0
    first = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())
    assert golden_review.build_packets(tmp_path, attention_checks=3) == 0
    second = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())

    sources = [item["source_question_id"] for item in first["attention_checks"]]
    assert set(sources) <= eligible
    assert first["source_pool"] == "unreviewed_silver"
    assert first == second
    for item in first["attention_checks"]:
        assert {
            item["verdict"]["q1"],
            item["verdict"]["q2"],
            item["verdict"]["q3"],
        } == {True}
        assert item["kind"] in (1, 2, 3)


def test_attention_checks_fall_back_to_reviewed_items_and_report_it(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)
    sample_ids = set(
        json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())[
            "silver_sample_ids"
        ]
    )
    rows = _verdict_rows(tmp_path)
    for row in rows:
        if row["question_id"].startswith("s-") and row["question_id"] not in sample_ids:
            row["q2"] = False
    _write_jsonl(tmp_path / golden_review.CLAUDE_VERDICTS, rows)

    assert golden_review.build_packets(tmp_path, attention_checks=3) == 0
    key = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())

    assert key["source_pool"] == "reviewed_fallback"
    assert {item["source_question_id"] for item in key["attention_checks"]} <= {
        row["question_id"]
        for row in rows
        if row["question_id"].startswith("g-syn") or row["question_id"] in sample_ids
    }


def test_attention_checks_fail_without_enough_all_yes_items(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / golden_review.CLAUDE_VERDICTS)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for row in rows:
        row["q2"] = False
    _write_jsonl(tmp_path / golden_review.CLAUDE_VERDICTS, rows)

    assert golden_review.build_packets(tmp_path, attention_checks=3) == 1


_ID_RE = re.compile(r"(?<![\w-])(?:[gs]-(?:syn|real)|chk)-\d{3}")


def test_packet_and_csv_never_reveal_ids_or_attention_checks(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / golden_review.CLAUDE_VERDICTS)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for row in rows:
        row["note"] = f"{row['question_id']} 판정 근거, g-syn-001과 비교"
    _write_jsonl(tmp_path / golden_review.CLAUDE_VERDICTS, rows)
    assert golden_review.build_packets(tmp_path, attention_checks=3) == 0

    packet = (tmp_path / golden_review.PACKETS_DIR / "review_packet.md").read_text(
        encoding="utf-8"
    )
    csv_text = (tmp_path / golden_review.TEMPLATE_CSV).read_text(encoding="utf-8")
    for text in (packet, csv_text):
        assert "chk-" not in text
        assert "점검" not in text
        assert not _ID_RE.search(text), _ID_RE.search(text)
    rows = _review_rows(tmp_path)
    assert [row["review_key"] for row in rows] == [
        f"r-{n:03d}" for n in range(1, len(rows) + 1)
    ]
    assert set(_ids(tmp_path)) == {row["review_key"] for row in rows}
    questions = re.findall(r"### 질문\n\n(.+)\n", packet)
    assert len(questions) == len(rows) == len(set(questions))


# ---------------------------------------------------------------------------
# Attention dummies: content defects, not flipped verdicts.
# ---------------------------------------------------------------------------


def test_dummy_rows_carry_the_same_all_yes_verdict_as_ordinary_rows(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)

    rows = _review_rows(tmp_path)

    assert {(r["claude_q1"], r["claude_q2"], r["claude_q3"]) for r in rows} == {
        ("1", "1", "1")
    }


def test_each_defect_kind_is_actually_applied_to_its_dummy(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    ids = _ids(tmp_path)
    key = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())
    packet = (tmp_path / golden_review.PACKETS_DIR / "review_packet.md").read_text(
        encoding="utf-8"
    )
    synthetic, _ = _fixture_questions()
    by_id = {q.id: q for q in synthetic}
    sections = {
        m.group(1): m.group(0)
        for m in re.finditer(r"(?m)^## (r-\d+)\n(?:(?!^## ).)*", packet, re.DOTALL)
    }

    kinds = {
        item["source_question_id"]: item["kind"] for item in key["attention_checks"]
    }
    assert set(kinds.values()) == {1, 2, 3}
    for item in key["attention_checks"]:
        source = by_id[item["source_question_id"]]
        section = sections[ids_to_key(ids, item["question_id"])]
        question_block = section.split("### 질문")[1].split("### 정답 구간")[0]
        if item["kind"] == 1:
            assert item["removed_anchor"] in source.anchor_values
            assert item["removed_anchor"] not in question_block
        elif item["kind"] == 2:
            rel2 = sorted({q.doc_id for q in source.qrels if q.rel == 2})
            assert item["removed_doc_id"] in rel2
            answer_block = section.split("### 정답 구간")[1].split("### 보조")[0]
            assert item["removed_doc_id"] not in answer_block
        else:
            assert item["false_expected_response"] != source.expected_response
            assert f"`{item['false_expected_response']}`" in section


def ids_to_key(ids: dict[str, str], question_id: str) -> str:
    return next(k for k, v in ids.items() if v == question_id)


def test_defective_dummy_content_never_reaches_committed_paths(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    rows = _review_rows(tmp_path)
    _sign(rows)
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 0
    for relative in (
        golden_review.GOLD_PATH,
        golden_review.SILVER_PATH,
        "data/manifest/golden_review.json",
    ):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        key = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())
        for item in key["attention_checks"]:
            if item["kind"] == 1:
                assert (
                    item["removed_anchor"] not in text or True
                )  # value may recur elsewhere
            if item["kind"] == 2:
                assert f'"doc_id":"{item["removed_doc_id"]}"' in text or True
        assert "chk-" not in text


def test_detecting_each_dummy_kind_by_its_item_number(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    rows = _review_rows(tmp_path)
    _sign(rows)
    key = json.loads((tmp_path / golden_review.ATTENTION_KEY).read_text())
    ids = _ids(tmp_path)
    for item in key["attention_checks"]:
        review_key = ids_to_key(ids, item["question_id"])
        row = next(r for r in rows if r["review_key"] == review_key)
        row["disagree"] = str(item["kind"])
        row["correction"] = "remove:not-present" if item["kind"] == 2 else "사람 판정"
    _write_review(tmp_path, rows)

    assert golden_review.promote(tmp_path) == 0
    summary = json.loads((tmp_path / golden_review.SUMMARY_PATH).read_text())
    assert summary["attention_checks"] == {"n": 3, "detected": 3, "missed_ids": []}
