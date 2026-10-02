from pathlib import Path

import pytest

from kodocs.eval import m8_adr_index as adr

ROOT = Path(__file__).resolve().parents[2]


def test_document_contract_and_freshness():
    entries = adr.parse((ROOT / "docs/ADR.md").read_text())
    assert [e["id"] for e in entries] == [f"ADR-{n:03}" for n in range(1, 32)]
    assert (ROOT / "docs/ADR-INDEX.md").read_text() == adr.render(entries)
    assert entries[19]["status"] == "폐기"
    assert entries[3]["status"] == "유효(일부 변경: ADR-030)"


@pytest.mark.parametrize(
    "text",
    [
        "### ADR-001: A\n**결정**: A",
        "### ADR-001: A\n**상태**: 이상",
        "### ADR-001: A\n**상태**: 유효\nADR-099",
        "### ADR-001: A\n**상태**: 대체됨(→ ADR-099)",
        "### ADR-001: A\n**상태**: 유효(일부 변경: ADR-099)",
    ],
)
def test_invalid_status_or_reference(text):
    with pytest.raises(ValueError):
        adr.parse(text)


def test_incoming_outgoing_and_anchor():
    rows = adr.parse(
        "### ADR-001: 첫 결정\n**상태**: 유효\nADR-002\n\n### ADR-002 (폐기): 다음\n**상태**: 폐기 — 설명\n"
    )
    doc = adr.render(rows)
    assert "ADR.md#adr-002-폐기-다음" in doc
    assert rows[0]["outgoing"] == ["ADR-002"]
    assert rows[1]["incoming"] == ["ADR-001"]


def test_korean_suffix_reference_is_not_omitted():
    rows = adr.parse(
        "### ADR-001: A\n**상태**: 유효\nADR-002의 변경\n### ADR-002: B\n**상태**: 유효\n"
    )
    assert rows[0]["outgoing"] == ["ADR-002"]
