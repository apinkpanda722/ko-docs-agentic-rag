"""M7 사전 등록 문서와 freeze용 규칙 해시 계약."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kodocs.eval.m7_rules import (
    REQUIRED_RULE_IDS,
    extract_rules_section,
    rules_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
HYPOTHESES = ROOT / "docs/results/hypotheses.md"
BASELINE_SHA256 = {
    "M5": "8b18adbb9ef5afaf06eeddfd370ffbd8d459904417df3645ebea41e4b7dd5ea3",
    "M6": "781e9e606a697bf5d0f6ea947d94b4647d9ab310d96480e1f83552751dbd5143",
}


def _section(data: bytes, phase: str) -> bytes:
    pattern = (rf"^## {phase} 판정 규칙[^\n]*\n.*?(?=^## |\Z)").encode()
    match = re.search(pattern, data, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group()


def test_required_rule_ids_occur_once_and_no_extra_ids():
    section = extract_rules_section(HYPOTHESES)
    ids = re.findall(r"^- \*\*(M7-[A-Z]+\d+)\*\*", section, re.MULTILINE)
    assert len(REQUIRED_RULE_IDS) == 29
    assert len(set(REQUIRED_RULE_IDS)) == 29
    assert sorted(ids) == sorted(REQUIRED_RULE_IDS)
    assert set(re.findall(r"\bM7-[A-Z]+\d+\b", section)) == set(REQUIRED_RULE_IDS)


@pytest.mark.parametrize("phase", ["M5", "M6"])
def test_pre_registered_sections_are_byte_identical(phase):
    current = _section(HYPOTHESES.read_bytes(), phase)
    if shutil.which("git"):
        result = subprocess.run(
            ["git", "show", "HEAD:docs/results/hypotheses.md"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            assert current == _section(result.stdout, phase)
            return
    assert hashlib.sha256(current).hexdigest() == BASELINE_SHA256[phase]


def test_adr_records_paid_tier_change_without_rewriting_adr004():
    adr = (ROOT / "docs/ADR.md").read_text(encoding="utf-8")
    assert "### ADR-030: M7 답변 채점·보안 테스트셋·지연 측정 설계" in adr
    adr004 = adr.split("### ADR-004:", 1)[1].split("### ADR-005:", 1)[0]
    assert "Gemini API(무료 등급 Flash)" in adr004
    assert (
        "> 변경: 채점 모델 등급은 "
        "[ADR-030](#adr-030-m7-답변-채점보안-테스트셋지연-측정-설계)에서 "
        "무료에서 유료로 변경했다."
    ) in adr004


def test_failure_patterns_and_fixed_settings_are_documented():
    patterns = (ROOT / ".claude/skills/failure-patterns/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "### FP-051 " in patterns
    assert "### FP-052 " in patterns
    section = extract_rules_section(HYPOTHESES)
    for value in ("20261001", "20260930", "2026-12-31", "gemini-3.8-flash"):
        assert value in section


def test_extract_preserves_exact_text_and_hashes_only_section(tmp_path):
    section = "## M7 판정 규칙 (고정)\n\n### 세부 규칙\n- **M7-D1** 원문\n\n"
    path = tmp_path / "hypotheses.md"
    path.write_text("## M6 판정 규칙\n이전\n" + section + "## 변경 기록\n기록\n")
    assert extract_rules_section(path) == section
    assert rules_sha256(path) == hashlib.sha256(section.encode("utf-8")).hexdigest()
    path.write_text("상단 변경\n" + section + "## 변경 기록\n다른 기록\n")
    assert rules_sha256(path) == hashlib.sha256(section.encode("utf-8")).hexdigest()
    path.write_text(section)
    assert extract_rules_section(path) == section


def test_extract_missing_heading_fails(tmp_path):
    path = tmp_path / "hypotheses.md"
    path.write_text("## M6 판정 규칙\n")
    with pytest.raises(ValueError, match="M7 판정 규칙"):
        extract_rules_section(path)


def test_default_path_reads_registered_rules(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert extract_rules_section() == extract_rules_section(HYPOTHESES)
    assert rules_sha256() == rules_sha256(HYPOTHESES)
