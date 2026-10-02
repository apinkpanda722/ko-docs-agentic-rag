"""M8 사전 등록 규칙 절을 원문 그대로 추출하고 freeze용 해시를 계산한다."""

import hashlib
import re
from pathlib import Path

REQUIRED_RULE_IDS: tuple[str, ...] = (
    "M8-R1",
    "M8-R2",
    "M8-R3",
    "M8-R4",
    "M8-H8",
    "M8-H8b",
    "M8-T1",
    "M8-T2",
    "M8-X1",
    "M8-P1",
    "M8-P2",
    "M8-N1",
    "M8-D1",
)


def extract_rules_section(path: Path = Path("docs/results/hypotheses.md")) -> str:
    """M8 헤딩부터 다음 level-2 헤딩 전까지 공백·줄바꿈을 보존한다."""
    text = path.read_bytes().decode("utf-8")
    match = re.search(
        r"^## M8 판정 규칙[^\n]*\n.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError(f"M8 판정 규칙 절이 없습니다: {path}")
    return match.group()


def rules_sha256(path: Path = Path("docs/results/hypotheses.md")) -> str:
    """추출한 규칙 원문의 UTF-8 SHA-256."""
    return hashlib.sha256(extract_rules_section(path).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    print(f"rules={len(REQUIRED_RULE_IDS)} sha256={rules_sha256()}")
