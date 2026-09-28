"""Contracts for deterministic query-to-evidence lexical overlap."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.eval import lexical, query_draft
from kodocs.eval.golden import Asker, Qrel, Question, load_questions, write_questions
from kodocs.eval.lexical import (
    bigram_containment,
    longest_common_substring,
    normalize,
    overlap,
    strip_values,
    summarize,
)


def _question(
    question_id: str,
    *,
    doc_id: str,
    target: str,
    question: str = "",
    question_type: str = "semantic",
    anchors: tuple[str, ...] = (),
) -> Question:
    return Question(
        id=question_id,
        set="silver",
        source="synthetic",
        type=question_type,
        tags=(),
        question=question,
        asker=Asker(team="platform", grade=3),
        as_of=None,
        qrels=(Qrel(doc_id=doc_id, rel=2, start=0, end=len(target)),),
        anchor_values=anchors,
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split="dev",
    )


def _write_fixture(
    root: Path,
    entries: list[tuple[Question, str, int]],
) -> None:
    write_questions(root / query_draft.SPECS_PATH, [entry[0] for entry in entries])
    docs_dir = root / lexical.DOCS_PATH
    drafts_dir = root / query_draft.DRAFTS_DIR
    docs_dir.mkdir(parents=True)
    drafts_dir.mkdir(parents=True)
    for spec, draft, attempt in entries:
        target = {
            "low": "가나다라",
            "mid": "가나다아아",
            "high": "가나다라마바사아자차카타파하",
            "permission": "비밀 문장",
        }[spec.id]
        docs_dir.joinpath(f"{spec.qrels[0].doc_id}.json").write_text(
            json.dumps({"doc_id": spec.qrels[0].doc_id, "body": target}),
            encoding="utf-8",
        )
        drafts_dir.joinpath(f"{spec.id}-a{attempt}.json").write_text(
            json.dumps(
                {
                    "id": spec.id,
                    "attempt": attempt,
                    "custom_id": f"{spec.id}-a{attempt}",
                    "result_type": "succeeded",
                    "text": draft,
                }
            ),
            encoding="utf-8",
        )


def _data_snapshot(root: Path) -> dict[str, bytes]:
    data = root / "data"
    return {
        str(path.relative_to(data)): path.read_bytes()
        for path in sorted(data.rglob("*"))
        if path.is_file()
    }


def test_normalize_is_nfc_and_removes_whitespace_and_punctuation() -> None:
    assert normalize(" 가,\n나-다! ") == "가나다"
    assert strip_values("INC-2025-001, 가 나 다", ["INC-2025-001", "나다"]) == "가"


@pytest.mark.parametrize(
    ("query", "target", "containment", "lcs"),
    [
        ("", "가나다", None, 0),
        ("가", "가", None, 1),
        ("가나다", "가나다", 1.0, 3),
        ("가나다", "라마바사", 0.0, 0),
    ],
)
def test_overlap_primitives_cover_empty_single_exact_and_disjoint_boundaries(
    query: str,
    target: str,
    containment: float | None,
    lcs: int,
) -> None:
    assert bigram_containment(query, target) == containment
    assert longest_common_substring(query, target) == lcs


ATOMIC_BODY = (
    "# 회의록\n"
    "- 참석: 결제서비스팀\n"
    "- 장애 대응 책임은 문다람 팀장이 맡아 복구 절차를 지휘하기로 했다. 다음 안건.\n"
)
ATOMIC_SPAN = "문다람 팀장"


def _atomic_spec(question: str) -> Question:
    start = ATOMIC_BODY.index(ATOMIC_SPAN)
    base = _question(
        "atomic",
        doc_id="doc-atomic",
        target=ATOMIC_SPAN,
        question=question,
    )
    return replace(
        base,
        qrels=(
            Qrel(
                doc_id="doc-atomic",
                rel=2,
                start=start,
                end=start + len(ATOMIC_SPAN),
            ),
        ),
        anchor_values=("billing-api",),
    )


def test_sentence_bounds_expand_span_to_its_sentence_without_list_marker() -> None:
    start = ATOMIC_BODY.index(ATOMIC_SPAN)

    s, e = lexical.sentence_bounds(ATOMIC_BODY, start, start + len(ATOMIC_SPAN))

    expected = "장애 대응 책임은 문다람 팀장이 맡아 복구 절차를 지휘하기로 했다."
    assert ATOMIC_BODY[s:e] == expected


def test_atomic_answer_question_copying_its_sentence_hits_overlap_cap() -> None:
    # Full run: atomic spans are a few characters ("문다람 팀장"), so a copied
    # question slipped under the cap when compared with the span alone.
    docs = {"doc-atomic": {"body": ATOMIC_BODY}}
    copied = _atomic_spec("장애 대응 책임을 맡아 복구 절차를 지휘하기로 한 사람은?")

    result = overlap(copied, copied.question, docs)

    assert result["containment"] > lexical.CONTAINMENT_CAP
    assert "overlap_cap" in query_draft.validate_draft(
        copied,
        copied.question,
        seed=SimpleNamespace(facts={}),  # type: ignore[arg-type]
        people=(),
        docs=docs,
    )


def test_atomic_answer_question_in_own_words_passes() -> None:
    docs = {"doc-atomic": {"body": ATOMIC_BODY}}
    reworded = _atomic_spec("billing-api 장애가 나면 누가 총괄하나요?")

    result = overlap(reworded, reworded.question, docs)

    assert result["containment"] <= lexical.CONTAINMENT_CAP
    assert result["lcs"] <= lexical.LCS_CAP


def test_narrative_span_that_is_already_a_sentence_gives_the_same_target() -> None:
    sentence = "지표 압축 작업의 메모리 상한이 낮게 설정되었다."
    body = f"## 원인\n{sentence}\n## 조치\n상한을 올렸다."
    start = body.index(sentence)
    end = start + len(sentence)
    base = _question(
        "narrative",
        doc_id="doc-n",
        target=sentence,
        question="원인은?",
    )
    spec = replace(base, qrels=(Qrel(doc_id="doc-n", rel=2, start=start, end=end),))
    question = "지표 압축 작업에서 무엇이 문제였나요?"

    expanded = overlap(spec, question, {"doc-n": {"body": body}})
    span_spec = replace(
        spec, qrels=(Qrel(doc_id="doc-n", rel=2, start=0, end=len(sentence)),)
    )
    span_only = overlap(span_spec, question, {"doc-n": {"body": sentence + "\n"}})

    assert lexical.sentence_bounds(body, start, end) == (start, end)
    assert expanded == span_only


def test_overlap_removes_subject_identifiers_and_dates_from_both_sides() -> None:
    target = "INC-2025-001 core-gateway 2025년 1월 15일"
    spec = _question(
        "subject-only",
        doc_id="doc-subject",
        target=target,
        anchors=("core-gateway",),
    )
    docs = {"doc-subject": {"body": target}}

    result = overlap(
        spec,
        "INC-2025-001, 2025-01-15 core-gateway?",
        docs,
    )

    assert result == {"containment": None, "lcs": 0, "target_chars": 0}


def test_overlap_without_comparable_rel_two_target_is_excluded() -> None:
    spec = replace(
        _question("permission", doc_id="restricted", target="비밀 문장"),
        type="permission",
        expected_response="indistinguishable_from_no_answer",
    )

    assert overlap(spec, "무엇인가요?", {"restricted": {"body": "비밀 문장"}}) == {
        "containment": None,
        "lcs": None,
        "target_chars": 0,
    }


def test_summarize_excludes_none_from_every_ratio_denominator() -> None:
    rows = [
        {"type": "semantic", "containment": None, "lcs": None},
        {"type": "identifier", "containment": 0.2, "lcs": 2},
        {"type": "semantic", "containment": 0.8, "lcs": 13},
    ]

    result = summarize(rows)

    assert result["all"]["containment"]["n"] == 2
    assert result["all"]["lcs"]["n"] == 2
    assert result["by_type"]["semantic"]["containment"]["n"] == 1
    assert result["over_cap"] == {"containment": 1, "lcs": 1, "either": 1}
    assert result["mismatch"] == 1


def test_validate_draft_rejects_only_values_above_the_initial_caps() -> None:
    seed = SimpleNamespace(facts={})
    high_target = "가나다라마바사아자차카타파하"
    high = _question("high", doc_id="doc-high", target=high_target)
    mid_target = "가나다아아"
    mid = _question("mid", doc_id="doc-mid", target=mid_target)

    assert query_draft.validate_draft(
        high,
        f"{high_target}?",
        seed=seed,  # type: ignore[arg-type]
        people=(),
        docs={"doc-high": {"body": high_target}},
    ) == ["overlap_cap"]
    assert (
        query_draft.validate_draft(
            mid,
            "가나다라마바사?",
            seed=seed,  # type: ignore[arg-type]
            people=(),
            docs={"doc-mid": {"body": mid_target}},
        )
        == []
    )


def test_validate_only_tags_mismatch_and_is_byte_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    low = _question("low", doc_id="doc-low", target="가나다라")
    mid = _question("mid", doc_id="doc-mid", target="가나다아아")
    permission = replace(
        _question("permission", doc_id="doc-permission", target="비밀 문장"),
        type="permission",
        expected_response="indistinguishable_from_no_answer",
    )
    entries = [
        (low, "마바사아?", 1),
        (mid, "가나다라마바사?", 1),
        (permission, "비밀 문장인가요?", 1),
    ]
    _write_fixture(tmp_path, entries)
    monkeypatch.setattr(
        query_draft,
        "load_seed",
        lambda _root: SimpleNamespace(facts={}, people=[]),
    )

    assert query_draft.main(["--root", str(tmp_path), "--validate-only"]) == 0
    first_validated = (tmp_path / query_draft.VALIDATION_PATH).read_bytes()
    first_summary = (tmp_path / query_draft.VALIDATION_SUMMARY_PATH).read_bytes()

    assert query_draft.main(["--root", str(tmp_path), "--validate-only"]) == 0
    assert (tmp_path / query_draft.VALIDATION_PATH).read_bytes() == first_validated
    assert (
        tmp_path / query_draft.VALIDATION_SUMMARY_PATH
    ).read_bytes() == first_summary

    validated = {
        item.id: item for item in load_questions(tmp_path / query_draft.VALIDATION_PATH)
    }
    assert validated["low"].tags == ("lexical_mismatch",)
    assert validated["mid"].tags == ()
    summary = json.loads(first_summary)
    assert summary["validated"] == 3
    assert summary["failed"] == 0
    assert summary["lexical_mismatch"] == {"n": 1, "share": 0.5}
    assert summary["caps"] == {
        "containment": lexical.CONTAINMENT_CAP,
        "lcs": lexical.LCS_CAP,
        "mismatch_max": lexical.MISMATCH_MAX,
    }


def test_validate_only_without_collected_drafts_fails_with_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert query_draft.main(["--root", str(tmp_path), "--validate-only"]) == 1
    assert "수거한 초안 없음, `make queries` 먼저" in capsys.readouterr().out


def test_lexical_report_prints_candidates_without_writing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    low = _question("low", doc_id="doc-low", target="가나다라")
    mid = _question("mid", doc_id="doc-mid", target="가나다아아")
    _write_fixture(tmp_path, [(low, "마바사아?", 1), (mid, "가나다라마바사?", 1)])
    write_questions(
        tmp_path / query_draft.VALIDATION_PATH,
        [replace(low, question="마바사아?", tags=("lexical_mismatch",))],
    )
    before = _data_snapshot(tmp_path)

    assert lexical.main(["report", "--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "유형별 어휘 겹침 분포" in output
    assert all(f"포함률 {cap}" in output for cap in ("0.5", "0.6", "0.7"))
    assert all(f"LCS {cap}" in output for cap in ("10", "12", "15"))
    assert "lexical_mismatch" in output
    assert _data_snapshot(tmp_path) == before


def test_lexical_report_without_collected_drafts_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lexical.main(["report", "--root", str(tmp_path)]) == 1
    assert "수거한 초안 없음, `make queries` 먼저" in capsys.readouterr().out
