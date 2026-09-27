"""Contract tests for synthetic-corpus identifier extraction."""

import ast
from pathlib import Path

from kodocs.synth.identifiers import (
    APPROVAL_RE,
    INCIDENT_RE,
    REGULATION_REF_RE,
    TICKET_RE,
    extract_identifiers,
    service_pattern,
)


def test_fixed_identifier_patterns_match_whole_ids_without_cross_matching() -> None:
    text = "PAY-1234 EXP-2026-0412 INC-2026-031 PLT-9999"

    assert [match.group() for match in TICKET_RE.finditer(text)] == [
        "PAY-1234",
        "PLT-9999",
    ]
    assert APPROVAL_RE.fullmatch("EXP-2026-0412")
    assert INCIDENT_RE.fullmatch("INC-2026-031")
    assert not TICKET_RE.search("EXP-2026-0412")
    assert not TICKET_RE.search("INC-2026-031")
    assert not TICKET_RE.search("XPAY-1234")
    assert not TICKET_RE.search("PAY-12345")


def test_regulation_references_recognize_names_and_branch_articles() -> None:
    expected = [
        ("출장규칙 제8조", "출장규칙", "8", None),
        ("인사규정 제12조의2", "인사규정", "12", "2"),
        ("정보보안 기본지침 제3조", "정보보안 기본지침", "3", None),
    ]

    for text, name, number, branch in expected:
        match = REGULATION_REF_RE.fullmatch(text)
        assert match is not None
        assert (match.group("name"), match.group("num"), match.group("branch")) == (
            name,
            number,
            branch,
        )


def test_service_pattern_is_seed_driven_longest_first_and_boundary_safe() -> None:
    pattern = service_pattern(["billing", "billing-api", "search-v2"])

    assert [
        match.group()
        for match in pattern.finditer(
            "billing-api와 billing, search-v2 / xbilling-api billing-api-v2"
        )
    ] == ["billing-api", "billing", "search-v2"]


def test_extract_identifiers_reports_values_and_source_spans() -> None:
    text = "PAY-1234 이후 EXP-2026-0412와 INC-2026-031, 출장규칙 제8조를 확인했다."

    found = extract_identifiers(text)

    for kind, expected in {
        "ticket": "PAY-1234",
        "approval": "EXP-2026-0412",
        "incident": "INC-2026-031",
        "regulation": "출장규칙 제8조",
    }.items():
        assert len(found[kind]) == 1
        value, start, end = found[kind][0]
        assert value == expected
        assert text[start:end] == expected


def test_identifier_module_has_no_yaml_or_seed_dependency() -> None:
    path = Path("src/kodocs/synth/identifiers.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert "yaml" not in imports
    assert "kodocs.synth.seed" not in imports
