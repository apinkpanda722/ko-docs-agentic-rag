"""M8 사전 등록 원문과 freeze용 규칙 해시 계약."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kodocs.eval.m5_test import _BOOTSTRAP_SEED
from kodocs.eval.m7_rules import rules_sha256 as m7_rules_sha256
from kodocs.eval.m8_rules import (
    REQUIRED_RULE_IDS,
    extract_rules_section,
    rules_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
HYPOTHESES = ROOT / "docs/results/hypotheses.md"


def test_required_rule_ids_occur_once_and_no_extra_ids():
    section = extract_rules_section(HYPOTHESES)
    ids = re.findall(r"^- \*\*(M8-[A-Z]+\d+b?)\*\*", section, re.MULTILINE)
    assert len(REQUIRED_RULE_IDS) == len(set(REQUIRED_RULE_IDS)) == 13
    assert sorted(ids) == sorted(REQUIRED_RULE_IDS)


def test_section_position_and_registered_values():
    text = HYPOTHESES.read_text(encoding="utf-8")
    assert text.index("## M7 판정 규칙") < text.index("## M8 판정 규칙")
    assert text.index("## M8 판정 규칙") < text.index("## 변경 기록")
    section = extract_rules_section(HYPOTHESES)
    for value in (
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "2,271,071,852",
        f"시드는 M5 test와 같은 {_BOOTSTRAP_SEED}다",
        "골드 test 검색 지표 대상 21문항",
        "상한 1024",
    ):
        assert value in section


def test_m7_section_matches_frozen_rules_hash():
    frozen = json.loads((ROOT / "data/manifest/m7_config.json").read_bytes())
    assert m7_rules_sha256(HYPOTHESES) == frozen["rules_sha256"]


def test_extract_preserves_exact_text_and_hashes_only_section(tmp_path):
    section = "## M8 판정 규칙 (고정)\r\n\r\n### 세부 규칙\r\n- 원문  \r\n\r\n"
    path = tmp_path / "hypotheses.md"
    path.write_bytes(("## M7 판정 규칙\n이전\n" + section + "## 변경 기록\n").encode())
    expected = hashlib.sha256(section.encode("utf-8")).hexdigest()
    assert extract_rules_section(path) == section
    assert rules_sha256(path) == rules_sha256(path) == expected
    path.write_bytes(("상단 변경\n" + section + "## 변경 기록\n다른 기록\n").encode())
    assert rules_sha256(path) == expected
    path.write_bytes(section.encode())
    assert extract_rules_section(path) == section


def test_extract_missing_heading_fails(tmp_path):
    path = tmp_path / "hypotheses.md"
    path.write_text("## M7 판정 규칙\n", encoding="utf-8")
    with pytest.raises(ValueError, match="M8 판정 규칙"):
        extract_rules_section(path)


def test_default_path_reads_registered_rules(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert extract_rules_section() == extract_rules_section(HYPOTHESES)
    assert rules_sha256() == rules_sha256(HYPOTHESES)


def test_module_prints_only_rule_count_and_hash():
    result = subprocess.run(
        [sys.executable, "-m", "kodocs.eval.m8_rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        result.stdout
        == f"rules={len(REQUIRED_RULE_IDS)} sha256={rules_sha256(HYPOTHESES)}\n"
    )
    assert result.stderr == ""
