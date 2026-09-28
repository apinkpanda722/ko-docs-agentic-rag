"""Real-regulation golden candidate and draft validation contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval import golden_real
from kodocs.eval.golden import (
    Asker,
    Qrel,
    Question,
    question_to_dict,
    write_questions,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
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


def _law(
    *,
    serial: str,
    issued_date: str,
    current: bool,
    first_body: str,
) -> dict[str, object]:
    return {
        "name": "가짜 복무규정",
        "serial": serial,
        "rule_id": "fake-rule-1",
        "issued_date": issued_date,
        "is_current": current,
        "articles": [
            f"제1조(목적) {first_body}",
            "제2조(신청) 가짜 신청은 정해진 양식으로 제출한다.",
        ],
        "addenda": [],
        "annexes": [],
    }


def _question(
    number: int,
    question_type: str,
    *,
    qrels: tuple[Qrel, ...] | None = None,
    tags: tuple[str, ...] = (),
    as_of: str | None = None,
) -> Question:
    if qrels is None:
        qrels = (Qrel(doc_id="law-101", rel=2, article_key="제1조"),)
    no_answer = question_type == "unanswerable"
    return Question(
        id=f"g-real-{number:03d}",
        set="gold",
        source="real",
        type=question_type,
        tags=tags,
        question=f"가짜 규정 질문 {number}은 무엇인가?",
        asker=Asker(team="platform", grade=3),
        as_of=as_of,
        qrels=() if no_answer else qrels,
        anchor_values=("가짜 규정",),
        answer_values=() if no_answer else (f"가짜 답 {number}",),
        expected_response="no_answer" if no_answer else "answer",
        sql=None,
        reference_answer="확인할 수 없다." if no_answer else f"가짜 참조 답변 {number}",
        reference_sha256=None,
        summary80=f"가짜 규정 문항 {number}의 짧은 요지",
        split=None,
        no_answer_checks=(
            {"seed": True, "docs": True, "real": None} if no_answer else None
        ),
    )


def _draft_rows() -> list[dict[str, object]]:
    questions: list[Question] = []
    number = 1
    for question_type, count in golden_real.REAL_QUOTA.items():
        for table_index in range(count):
            kwargs: dict[str, object] = {}
            if question_type == "table":
                kwargs["qrels"] = (
                    Qrel(
                        doc_id="law-101",
                        rel=2,
                        annex_id=f"fake-table-{table_index + 1}",
                    ),
                )
            elif question_type == "temporal":
                kwargs.update(
                    tags=("temporal:revision",),
                    as_of="2026-01-02",
                )
            questions.append(_question(number, question_type, **kwargs))
            number += 1
    rows = [question_to_dict(question) for question in questions]
    for row in rows:
        if row["type"] == "unanswerable":
            row["no_answer_terms"] = ["존재하지않는주어", "존재하지않는속성"]
    return rows


@pytest.fixture
def real_root(tmp_path: Path) -> Path:
    old_law = _law(
        serial="100",
        issued_date="2025-01-02",
        current=False,
        first_body="가짜 이전 기준을 적용한다.",
    )
    current_law = _law(
        serial="101",
        issued_date="2026-01-02",
        current=True,
        first_body="가짜 현재 기준을 적용하며 이 문장은 누출 검사를 위해 충분히 길게 작성한다.",
    )
    _write_json(tmp_path / "data/interim/law/fake-rule-1/100.json", old_law)
    _write_json(tmp_path / "data/interim/law/fake-rule-1/101.json", current_law)
    _write_jsonl(
        tmp_path / golden_real.VERSION_MATCHES_PATH,
        [
            {
                "confidence": "exact",
                "law_serial": "100",
                "law_issued_date": "2025-01-02",
                "law_rule_id": "fake-rule-1",
            },
            {
                "confidence": "approximate",
                "law_serial": "101",
                "law_issued_date": "2026-01-02",
                "law_rule_id": "fake-rule-1",
            },
        ],
    )
    _write_json(
        tmp_path / golden_real.REGULATION_REFS_PATH,
        {
            "rules": {
                "가짜 복무규정": {
                    "rule_id": "fake-rule-1",
                    "serial": "101",
                    "articles": {"제1조": "목적", "제2조": "신청"},
                }
            }
        },
    )
    _write_json(
        tmp_path / golden_real.TABLE_GOLDEN_PATH,
        {
            "tables": [
                {
                    "id": f"fake-table-{number}",
                    "rule_no": "fake-rule-no",
                    "annex_label": f"[별표 {number}] 가짜 표",
                }
                for number in range(1, 7)
            ]
        },
    )
    _write_jsonl(tmp_path / golden_real.DRAFTS_PATH, _draft_rows())
    synthetic = replace(
        _question(999, "unanswerable"),
        id="g-syn-test",
        source="synthetic",
        question="",
        summary80=None,
        anchor_values=("가상서비스",),
    )
    write_questions(tmp_path / golden_real.SPECS_PATH, [synthetic])
    return tmp_path


def _rewrite_drafts(root: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    path = root / golden_real.DRAFTS_PATH
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutate(rows)
    _write_jsonl(path, rows)


def test_law_versions_excludes_bodies_and_article_texts_uses_shared_splitter(
    real_root: Path,
) -> None:
    versions = golden_real.law_versions(real_root)

    assert [version["serial"] for version in versions] == ["100", "101"]
    assert all(
        not {"articles", "annexes", "addenda"}.intersection(version)
        for version in versions
    )
    law = json.loads(
        (real_root / "data/interim/law/fake-rule-1/101.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(golden_real.article_texts(law)) == {"제1조", "제2조"}
    assert golden_real.article_texts(law)["제1조"].startswith("제1조(목적)")


def test_revision_pairs_require_two_eligible_adjacent_versions(real_root: Path) -> None:
    pairs = golden_real.revision_pairs(real_root)

    assert pairs == [
        {
            "rule_id": "fake-rule-1",
            "name": "가짜 복무규정",
            "previous_serial": "100",
            "previous_issued_date": "2025-01-02",
            "previous_confidence": "exact",
            "next_serial": "101",
            "next_issued_date": "2026-01-02",
            "next_confidence": "approximate",
            "changed_article_keys": ["제1조"],
        }
    ]

    matches_path = real_root / golden_real.VERSION_MATCHES_PATH
    rows = [json.loads(line) for line in matches_path.read_text().splitlines()]
    rows[1]["confidence"] = "failed"
    _write_jsonl(matches_path, rows)
    assert golden_real.revision_pairs(real_root) == []


def test_search_law_texts_requires_every_term_in_the_same_article(
    real_root: Path,
) -> None:
    assert golden_real.search_law_texts(real_root, ["현재 기준", "충분히 길게"]) == [
        ("law-101", "제1조")
    ]
    assert golden_real.search_law_texts(real_root, ["현재 기준", "정해진 양식"]) == []


def test_candidates_fail_loudly_without_local_law_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert golden_real.write_candidates(tmp_path) == 1
    assert "법제처 정제 파일 없음, `make collect-law` 먼저(사용자 실행)" in (
        capsys.readouterr().out
    )
    assert not (tmp_path / golden_real.CANDIDATES_PATH).exists()


def test_candidates_include_safe_current_tables_and_revision_metadata(
    real_root: Path,
) -> None:
    assert golden_real.write_candidates(real_root) == 0
    text = (real_root / golden_real.CANDIDATES_PATH).read_text(encoding="utf-8")

    assert "가짜 복무규정" in text
    assert "제1조 — 목적" in text
    assert "fake-table-1" in text
    assert "100 → 101" in text
    assert "가짜 현재 기준을 적용" not in text


def test_check_writes_deterministic_body_free_projection_and_summary(
    real_root: Path,
) -> None:
    assert golden_real.check_drafts(real_root) == 0
    projection_path = real_root / golden_real.PROJECTION_PATH
    summary_path = real_root / golden_real.SUMMARY_PATH
    first_projection = projection_path.read_bytes()
    first_summary = summary_path.read_bytes()

    assert golden_real.check_drafts(real_root) == 0
    assert projection_path.read_bytes() == first_projection
    assert summary_path.read_bytes() == first_summary

    drafts = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in (real_root / golden_real.DRAFTS_PATH)
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    projection = [json.loads(line) for line in first_projection.decode().splitlines()]
    assert len(projection) == 20
    assert all("reference_answer" not in row for row in projection)
    assert all(
        row["reference_sha256"]
        == hashlib.sha256(
            drafts[row["id"]]["reference_answer"].encode("utf-8")
        ).hexdigest()
        for row in projection
    )
    unanswerable = next(row for row in projection if row["type"] == "unanswerable")
    assert unanswerable["no_answer_checks"]["real"] is True

    summary = json.loads(first_summary)
    assert summary["counts"] == golden_real.REAL_QUOTA
    assert summary["quota_diff"] == {key: 0 for key in golden_real.REAL_QUOTA}
    assert summary["no_answer_checks"] == {"g-real-020": True}
    assert summary["synthetic_no_answer_real_check"] == {"g-syn-test": True}
    assert summary["revision_pairs_available"] == 1
    assert set(summary["inputs"]) == {
        "regulation_refs_sha256",
        "version_matches_sha256",
        "table_golden_sha256",
        "law_files_sha256",
        "drafts_sha256",
    }


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda rows: rows[0]["qrels"][0].update(article_key="제99조"),
            "unknown_article",
        ),
        (lambda rows: rows.pop(), "quota_mismatch"),
        (
            lambda rows: next(row for row in rows if row["type"] == "temporal").update(
                as_of="2024-01-01"
            ),
            "temporal_revision_mismatch",
        ),
    ],
)
def test_check_rejects_schema_quota_and_temporal_errors(
    real_root: Path,
    capsys: pytest.CaptureFixture[str],
    mutate,
    error: str,
) -> None:  # type: ignore[no-untyped-def]
    _rewrite_drafts(real_root, mutate)

    assert golden_real.check_drafts(real_root) == 1
    assert error in capsys.readouterr().out
    assert not (real_root / golden_real.PROJECTION_PATH).exists()


def test_check_rejects_real_unanswerable_when_terms_exist_together(
    real_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def mutate(rows: list[dict[str, object]]) -> None:
        row = next(item for item in rows if item["type"] == "unanswerable")
        row["no_answer_terms"] = ["현재 기준", "충분히 길게"]

    _rewrite_drafts(real_root, mutate)

    assert golden_real.check_drafts(real_root) == 1
    assert "unanswerable_real_match" in capsys.readouterr().out


def test_check_records_failed_synthetic_real_text_check(
    real_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    law_path = real_root / "data/interim/law/fake-rule-1/101.json"
    law = json.loads(law_path.read_text(encoding="utf-8"))
    law["articles"][0] += " 가상서비스 비용"
    _write_json(law_path, law)

    assert golden_real.check_drafts(real_root) == 1
    assert "synthetic_unanswerable_real_match" in capsys.readouterr().out


def test_check_rejects_long_real_text_fragment_in_projection(
    real_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    law = json.loads(
        (real_root / "data/interim/law/fake-rule-1/101.json").read_text(
            encoding="utf-8"
        )
    )
    leaked = law["articles"][0]
    _rewrite_drafts(real_root, lambda rows: rows[0].update(question=leaked))

    assert golden_real.check_drafts(real_root) == 1
    assert "real_text_leak" in capsys.readouterr().out


def test_committed_projection_never_contains_reference_answers() -> None:
    path = ROOT / golden_real.PROJECTION_PATH
    if not path.is_file():
        return
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all("reference_answer" not in row for row in rows)
    assert all(
        isinstance(row.get("reference_sha256"), str)
        and len(row["reference_sha256"]) == 64
        and all(
            character in "0123456789abcdef" for character in row["reference_sha256"]
        )
        for row in rows
    )
