"""Deterministic character-overlap checks for drafted evaluation questions."""

from __future__ import annotations

import argparse
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from kodocs.eval.golden import Question, load_questions
from kodocs.synth.identifiers import extract_identifiers

CONTAINMENT_CAP = 0.6
LCS_CAP = 12
MISMATCH_MAX = 0.07

DOCS_PATH = Path("data/synthetic/docs")
SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")
DRAFTS_DIR = Path("data/synthetic/queries/drafts")
VALIDATION_PATH = Path("data/synthetic/queries/validated.jsonl")

_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}"
    r"|\d{4}\s*년(?:도)?\s*\d{1,2}\s*월\s*\d{1,2}\s*일"
    r"|\d{1,2}\s*월\s*\d{1,2}\s*일"
    r")(?!\d)"
)
_NON_COMPARABLE_TYPES = frozenset({"permission", "sql", "unanswerable"})
_LIST_MARKER_RE = re.compile(r"[ \t]*(?:[-*+•]|\d+[.)])[ \t]+")
_PERCENTILES = (("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p90", 0.9))


def normalize(text: str) -> str:
    """Apply NFC and remove every whitespace and Unicode punctuation character."""
    return "".join(
        character
        for character in unicodedata.normalize("NFC", text)
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def strip_values(text: str, values: Sequence[str]) -> str:
    """Normalize text and remove normalized subject/identifier/date values."""
    stripped = normalize(text)
    normalized_values = sorted(
        {normalize(value) for value in values if normalize(value)},
        key=lambda value: (-len(value), value),
    )
    for value in normalized_values:
        stripped = stripped.replace(value, "")
    return stripped


def bigram_containment(query: str, target: str) -> float | None:
    """Return the share of query character bigrams occurring in the target."""
    normalized_query = normalize(query)
    normalized_target = normalize(target)
    query_bigrams = [
        normalized_query[index : index + 2]
        for index in range(len(normalized_query) - 1)
    ]
    if not query_bigrams:
        return None
    target_bigrams = {
        normalized_target[index : index + 2]
        for index in range(len(normalized_target) - 1)
    }
    return sum(bigram in target_bigrams for bigram in query_bigrams) / len(
        query_bigrams
    )


def longest_common_substring(query: str, target: str) -> int:
    """Return the longest contiguous common substring length after normalization."""
    left = normalize(query)
    right = normalize(target)
    if not left or not right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    previous = [0] * (len(left) + 1)
    longest = 0
    for right_character in right:
        current = [0]
        for index, left_character in enumerate(left, start=1):
            length = previous[index - 1] + 1 if left_character == right_character else 0
            current.append(length)
            longest = max(longest, length)
        previous = current
    return longest


def sentence_bounds(body: str, start: int, end: int) -> tuple[int, int]:
    """Expand ``[start, end)`` to the sentence holding it.

    Boundaries are a newline, or ``.``/``?``/``!`` followed by whitespace or the
    end of the text (so ``99.99%`` and ``2025.3.1`` do not split). A leading
    list marker (``-``, ``*``, ``+``, ``•``, ``1.``, ``1)``) is excluded. A span
    that already ends a sentence keeps its own end.
    """
    left = start
    while left > 0 and not _is_boundary(body, left - 1):
        left -= 1
    marker = _LIST_MARKER_RE.match(body, left, start)
    if marker:
        left = marker.end()
    while left < start and body[left].isspace():
        left += 1

    stripped_end = end
    while stripped_end > start and body[stripped_end - 1].isspace():
        stripped_end -= 1
    if stripped_end > start and _is_boundary(body, stripped_end - 1):
        return left, stripped_end
    right = end
    while right < len(body) and not _is_boundary(body, right):
        right += 1
    if right < len(body) and body[right] != "\n":
        right += 1  # keep the terminating punctuation
    return left, right


def _is_boundary(body: str, index: int) -> bool:
    character = body[index]
    if character == "\n":
        return True
    return character in ".?!" and (index + 1 == len(body) or body[index + 1].isspace())


def overlap(
    spec: Question,
    question: str,
    docs: Mapping[str, Mapping[str, object]],
) -> dict[str, float | int | None]:
    """Compare a draft with its joined relevance-2 evidence after value stripping."""
    if spec.type in _NON_COMPARABLE_TYPES:
        return {"containment": None, "lcs": None, "target_chars": 0}

    target_parts: list[str] = []
    relevant = [qrel for qrel in spec.qrels if qrel.rel == 2]
    if not relevant:
        return {"containment": None, "lcs": None, "target_chars": 0}
    for qrel in relevant:
        document = docs.get(qrel.doc_id)
        body = document.get("body") if document is not None else None
        if (
            not isinstance(body, str)
            or not isinstance(qrel.start, int)
            or isinstance(qrel.start, bool)
            or not isinstance(qrel.end, int)
            or isinstance(qrel.end, bool)
            or not 0 <= qrel.start < qrel.end <= len(body)
        ):
            return {"containment": None, "lcs": None, "target_chars": 0}
        # An atomic span is a few characters ("문다람 팀장"); compare with the
        # sentence that holds it so a copied question still hits the cap.
        start, end = sentence_bounds(body, qrel.start, qrel.end)
        target_parts.append(body[start:end])

    target = "\n".join(target_parts)
    clean_question = _strip_overlap_values(question, spec.anchor_values)
    clean_target = _strip_overlap_values(target, spec.anchor_values)
    return {
        "containment": bigram_containment(clean_question, clean_target),
        "lcs": longest_common_substring(clean_question, clean_target),
        "target_chars": len(clean_target),
    }


def summarize(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize comparable rows with one shared eligibility denominator."""
    comparable = [row for row in rows if _comparable(row)]
    question_types = sorted({str(row.get("type", "unknown")) for row in rows})

    def distributions(selected: Sequence[Mapping[str, object]]) -> dict[str, object]:
        return {
            "containment": _distribution(
                [float(row["containment"]) for row in selected]
            ),
            "lcs": _distribution([float(row["lcs"]) for row in selected]),
        }

    by_type = {
        question_type: distributions(
            [
                row
                for row in comparable
                if str(row.get("type", "unknown")) == question_type
            ]
        )
        for question_type in question_types
    }
    containment_exceeded = sum(
        float(row["containment"]) > CONTAINMENT_CAP for row in comparable
    )
    lcs_exceeded = sum(int(row["lcs"]) > LCS_CAP for row in comparable)
    either_exceeded = sum(
        float(row["containment"]) > CONTAINMENT_CAP or int(row["lcs"]) > LCS_CAP
        for row in comparable
    )
    return {
        "all": distributions(comparable),
        "by_type": by_type,
        "over_cap": {
            "containment": containment_exceeded,
            "lcs": lcs_exceeded,
            "either": either_exceeded,
        },
        "mismatch": sum(
            float(row["containment"]) <= MISMATCH_MAX for row in comparable
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Print overlap observations without modifying any project file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("report",))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return _report(args.root.resolve())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"어휘 겹침 보고 실패: {_first_line(str(exc))}")
        return 1


def load_documents(path: Path) -> dict[str, Mapping[str, object]]:
    """Load deterministic synthetic document mappings from one directory."""
    documents: dict[str, Mapping[str, object]] = {}
    if not path.exists():
        return documents
    for document_path in sorted(path.glob("*.json")):
        payload = json.loads(document_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError(f"합성 문서는 객체여야 합니다: {document_path}")
        doc_id = payload.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError(f"합성 문서에 doc_id가 없습니다: {document_path}")
        documents[doc_id] = payload
    return documents


def _strip_overlap_values(text: str, anchors: Sequence[str]) -> str:
    identifiers = [
        value
        for matches in extract_identifiers(text).values()
        for value, _start, _end in matches
    ]
    dates = [match.group() for match in _DATE_RE.finditer(text)]
    return strip_values(text, (*anchors, *identifiers, *dates))


def _comparable(row: Mapping[str, object]) -> bool:
    containment = row.get("containment")
    lcs = row.get("lcs")
    return (
        isinstance(containment, (int, float))
        and not isinstance(containment, bool)
        and isinstance(lcs, int)
        and not isinstance(lcs, bool)
    )


def _distribution(values: Sequence[float]) -> dict[str, int | float | None]:
    if not values:
        return {
            "n": 0,
            "min": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p90": None,
            "max": None,
        }
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": _clean_number(ordered[0]),
        **{
            name: _clean_number(_percentile(ordered, quantile))
            for name, quantile in _PERCENTILES
        },
        "max": _clean_number(ordered[-1]),
    }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _clean_number(value: float) -> int | float:
    rounded = round(value, 10)
    return int(rounded) if rounded.is_integer() else rounded


def _report(root: Path) -> int:
    draft_paths = sorted((root / DRAFTS_DIR).glob("*.json"))
    if not draft_paths:
        print("수거한 초안 없음, `make queries` 먼저")
        return 1

    specs = load_questions(root / SPECS_PATH)
    by_id = {spec.id: spec for spec in specs}
    docs = load_documents(root / DOCS_PATH)
    rows: list[dict[str, object]] = []
    observed: set[tuple[str, str]] = set()
    for path in draft_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError(f"초안 캐시는 객체여야 합니다: {path}")
        spec_id = payload.get("id")
        text = payload.get("text")
        if (
            payload.get("result_type") != "succeeded"
            or not isinstance(spec_id, str)
            or not isinstance(text, str)
            or spec_id not in by_id
        ):
            continue
        question = text.strip()
        observed.add((spec_id, question))
        rows.append(_overlap_row(by_id[spec_id], question, docs))

    validated_path = root / VALIDATION_PATH
    if validated_path.exists():
        for spec in load_questions(validated_path):
            key = (spec.id, spec.question)
            if spec.question and key not in observed:
                rows.append(_overlap_row(spec, spec.question, docs))

    summary = summarize(rows)
    _print_distributions(summary)
    comparable = [row for row in rows if _comparable(row)]
    print("\n현재 상한 초과")
    print(
        f"포함률 {CONTAINMENT_CAP}: {summary['over_cap']['containment']} / "
        f"LCS {LCS_CAP}: {summary['over_cap']['lcs']} / "
        f"하나 이상: {summary['over_cap']['either']}"
    )
    print("\n상한 후보별 초과 수")
    print("| 후보 | 초과 수 |")
    print("|---|---:|")
    for cap in (0.5, 0.6, 0.7):
        count = sum(float(row["containment"]) > cap for row in comparable)
        print(f"| 포함률 {cap:.1f} | {count} |")
    for cap in (10, 12, 15):
        count = sum(int(row["lcs"]) > cap for row in comparable)
        print(f"| LCS {cap} | {count} |")
    mismatch = int(summary["mismatch"])
    share = mismatch / len(comparable) if comparable else 0.0
    print(
        f"\nlexical_mismatch: {mismatch}/{len(comparable)} "
        f"({share:.1%}, 포함률 <= {MISMATCH_MAX})"
    )
    return 0


def _overlap_row(
    spec: Question,
    question: str,
    docs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    return {"id": spec.id, "type": spec.type, **overlap(spec, question, docs)}


def _print_distributions(summary: Mapping[str, object]) -> None:
    print("유형별 어휘 겹침 분포")
    print(
        "| 유형 | n | 포함률 min | p25 | p50 | p75 | p90 | max | "
        "LCS min | p25 | p50 | p75 | p90 | max |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    all_distribution = summary["all"]
    assert isinstance(all_distribution, Mapping)
    _print_distribution_row("전체", all_distribution)
    by_type = summary["by_type"]
    assert isinstance(by_type, Mapping)
    for question_type, distribution in by_type.items():
        assert isinstance(distribution, Mapping)
        _print_distribution_row(str(question_type), distribution)


def _print_distribution_row(label: str, distribution: Mapping[str, object]) -> None:
    containment = distribution["containment"]
    lcs = distribution["lcs"]
    assert isinstance(containment, Mapping) and isinstance(lcs, Mapping)
    fields = ("min", "p25", "p50", "p75", "p90", "max")
    values = [label, str(containment["n"])]
    values.extend(_format_metric(containment[field]) for field in fields)
    values.extend(_format_metric(lcs[field]) for field in fields)
    print("| " + " | ".join(values) + " |")


def _format_metric(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _first_line(value: str) -> str:
    return value.splitlines()[0][:300] if value else "unknown error"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
