"""Comparison-only normalization for Korean rule article text.

The reference text and parsed text remain unchanged at rest.  Normalization is
applied only while scoring, in this fixed order:

| Rule | Transformation |
| --- | --- |
| N1 | Unicode NFC (never NFKC, which would change markers such as ``①``) |
| N2 | Remove balanced legal editorial annotations matched below |
| N3 | Unify curly quotes and middle-dot variants |
| N4 | Remove every Unicode whitespace character |
| N5 | Leave every other character unchanged |
"""

import re
import unicodedata

_EDITORIAL_METADATA = r"[0-9\s.,·ㆍ~～\-年月日時제조의항호부터시행개정신설삭제종전]*"

EDITORIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"<\s*(?:개정|신설|삭제|종전){_EDITORIAL_METADATA}>"),
    re.compile(
        rf"\[\s*(?:본조신설|전문개정|종전|제목개정|시행일)"
        rf"{_EDITORIAL_METADATA}\]"
    ),
)

NORMALIZATION_RULES: dict[str, str] = {
    "N1": "유니코드 NFC 적용(NFKC 미사용)",
    "N2": "균형 잡힌 법령 편집 주석 제거",
    "N3": "따옴표와 가운뎃점 변형 통일",
    "N4": "모든 유니코드 공백 제거",
    "N5": "그 외 문자는 변경하지 않음",
}

_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "ㆍ": "·",
        "・": "·",
        "･": "·",
        "‧": "·",
    }
)


def raw_compare_form(s: str) -> str:
    """Return the raw comparison form: NFC plus outer trimming only."""
    return unicodedata.normalize("NFC", s).strip()


def normalize(s: str) -> str:
    """Apply N1 through N5 for normalized comparison, without changing input."""
    normalized = unicodedata.normalize("NFC", s)
    normalized, _ = strip_editorial(normalized)
    normalized = normalized.translate(_PUNCTUATION_TRANSLATION)
    return re.sub(r"\s+", "", normalized)


def strip_editorial(s: str) -> tuple[str, int]:
    """Return text with balanced editorial annotations removed and their count."""
    stripped = s
    count = 0
    for pattern in EDITORIAL_PATTERNS:
        stripped, removed = pattern.subn("", stripped)
        count += removed
    return stripped, count
