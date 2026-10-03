"""Demo-fixes result document: numbers come from the impact manifest."""

import json
import shutil
from pathlib import Path

from kodocs.eval import m8_demo_fixes_report as m

ROOT = Path(__file__).resolve().parents[2]


def test_render_reports_the_measured_scope_and_the_silver_runs():
    text = m.render(ROOT)
    impact = json.loads((ROOT / m.IMPACT).read_text(encoding="utf-8"))
    kinds = impact["corpus"]["by_kind"]
    assert "| 장애 | 25 | 1 | 23 | 2 |" in text
    assert "| 티켓 | 22 | 0 | 21 | 1 |" in text
    assert "INC-2026-010(추가된 문서 syn-incident-020)" in text
    assert kinds["incident"]["identifiers"] == 25
    for question in ("s-syn-009", "s-syn-078", "s-syn-085"):
        assert question in text
    assert "호출 중 열람 가능한 문서가 빠진 호출은 0건" in text
    assert "선택적 재실행이 된다" in text
    assert "해당 없음" in text  # simple has no lookup_id
    assert (
        "evidence_format_version" in text and "수정 전 구성(근거 버전 1) 기준" in text
    )
    assert "결과표 탭을 제거했다" in text and "102줄" in text


def test_render_contains_no_question_or_answer_text():
    text = m.render(ROOT)
    assert "PRIVATE" not in text
    for forbidden in ("law-22", "제8조"):  # no regulation text or ids
        assert forbidden not in text


def test_write_preserves_the_manual_region(tmp_path):
    shutil.copytree(
        ROOT / "data/manifest",
        tmp_path / "data/manifest",
        ignore=shutil.ignore_patterns("m8_test_*"),
    )
    path = tmp_path / m.REPORT
    path.parent.mkdir(parents=True)
    path.write_text(f"intro\n{m.BEGIN}\nold\n{m.END}\nmanual notes")
    m.write_report(tmp_path)
    text = path.read_text()
    assert text.startswith("intro\n") and text.endswith("\nmanual notes")
    assert "old" not in text and "## 1. lookup_id" in text


def test_repository_document_is_current():
    document = (ROOT / m.REPORT).read_text(encoding="utf-8")
    generated = document.split(m.BEGIN)[1].split(m.END)[0]
    assert generated == f"\n\n{m.render(ROOT)}\n\n"
