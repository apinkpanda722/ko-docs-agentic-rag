"""M7 사전 등록 규칙 절을 원문 그대로 추출하고 freeze용 해시를 계산한다."""

import hashlib
import re
from pathlib import Path

REQUIRED_RULE_IDS: tuple[str, ...] = (
    "M7-D1",
    "M7-X1",
    "M7-X2",
    "M7-G1",
    "M7-G2",
    "M7-G3",
    "M7-G4",
    "M7-G5",
    "M7-G6",
    "M7-K1",
    "M7-K2",
    "M7-K3",
    "M7-K4",
    "M7-H1",
    "M7-H2",
    "M7-SOL1",
    "M7-R1",
    "M7-L1",
    "M7-L2",
    "M7-L3",
    "M7-L4",
    "M7-L5",
    "M7-P1",
    "M7-P2",
    "M7-P3",
    "M7-P4",
    "M7-F1",
    "M7-F2",
    "M7-F3",
)


def extract_rules_section(path: Path = Path("docs/results/hypotheses.md")) -> str:
    """M7 헤딩부터 다음 level-2 헤딩 전까지 공백·줄바꿈을 보존한다."""
    text = path.read_bytes().decode("utf-8")
    match = re.search(
        r"^## M7 판정 규칙[^\n]*\n.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError(f"M7 판정 규칙 절이 없습니다: {path}")
    return match.group()


def rules_sha256(path: Path = Path("docs/results/hypotheses.md")) -> str:
    """추출한 규칙 원문의 UTF-8 SHA-256."""
    return hashlib.sha256(extract_rules_section(path).encode("utf-8")).hexdigest()
