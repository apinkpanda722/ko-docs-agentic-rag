"""Threat-model documentation contracts; no external services are needed."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/THREAT_MODEL.md"
RESULT_PATTERN = re.compile(
    r"누설\s*\d+\s*/\s*(?:\d+|n\b)"
    r"|(?:누설률|오거절률|노출률|시도율)\s*[:=은는]?\s*\d+(?:\.\d+)?\s*%"
    r"|P95\s*(?:실측\s*)?[:=은는≤<>]?\s*\d+(?:\.\d+)?\s*(?:초|ms|s\b)",
    re.IGNORECASE,
)


def _document() -> str:
    assert DOCUMENT.is_file()
    return DOCUMENT.read_text(encoding="utf-8")


def test_threat_model_exists_and_adr_references_resolve():
    document = _document()
    references = set(re.findall(r"ADR-\d{3}", document))
    adr = (ROOT / "docs/ADR.md").read_text(encoding="utf-8")
    registered = set(re.findall(r"^### (ADR-\d{3})\b", adr, re.MULTILINE))
    assert references
    assert references <= registered


def test_threat_table_contains_required_topics():
    document = _document()
    section = document.split("## 3. 위협 표", 1)[1].split("\n## ", 1)[0]
    rows = "\n".join(line for line in section.splitlines() if line.startswith("|"))
    for keyword in (
        "직접",
        "우회",
        "식별자",
        "존재",
        "간접 인젝션",
        "SQL",
        "부분 답변",
        "토큰",
        "메타데이터",
    ):
        assert keyword in rows
    for category in ("LLM01", "LLM02", "LLM06"):
        assert category in rows


def test_threat_model_contains_no_measured_results():
    assert RESULT_PATTERN.search(_document()) is None


@pytest.mark.parametrize(
    "text",
    ["누설 0/218", "누설 0/n", "누설률: 0%", "P95 3.2초", "P95 = 4200ms"],
)
def test_measured_result_pattern_detects_forbidden_examples(text):
    assert RESULT_PATTERN.search(text)


def test_threat_model_documents_h6_limit_and_real_test_references():
    document = _document()
    for phrase in ("관측값", "95%", "3/n", "증명", "Clopper-Pearson"):
        assert phrase in document
    references = re.findall(r"`(tests/[\w/]+\.py)(?:::(test_\w+))?`", document)
    assert references
    for relative_path, name in references:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        if name:
            assert re.search(
                rf"^def {re.escape(name)}\(", path.read_text(), re.MULTILINE
            )
