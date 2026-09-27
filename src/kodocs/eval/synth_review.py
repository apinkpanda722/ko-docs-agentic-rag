"""Draw and aggregate the human review of validated synthetic documents."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import yaml

SAMPLE_SIZE = 20
SAMPLE_PATH = Path("data/synthetic/review/sample.json")
TEMPLATE_PATH = Path("data/synthetic/review/review_template.csv")
GUIDE_PATH = Path("data/synthetic/review/README.md")
RESULTS_DIR = Path("data/golden/synthetic_review")
SUMMARY_PATH = Path("data/manifest/synth_review.json")

CSV_COLUMNS = [
    "doc_id",
    "doc_type",
    "access_level",
    "reviewer",
    "reviewed_on",
    "fact_consistency",
    "fact_errors",
    "style_natural",
    "style_notes",
    "realism",
    "realism_notes",
    "injection_visible",
    "other_issues",
]

_DOC_TYPES = ("meeting", "weekly", "incident", "runbook", "guide", "notice")
_ACCESS_LEVELS = ("public", "team", "restricted")
_RANDOM_SEED_OFFSET = 8_108
_FACT_ID_RE = re.compile(r"f-[a-z0-9]+(?:-[a-z0-9]+)*")

_GUIDE = """# 합성 문서 사람 검수 안내

이 검수는 자동 채점이 아니라 사람이 합성 코퍼스의 신뢰성을 확인하는 절차다. `sample.json`의 `path`에 적힌 문서 JSON을 열고 `body`를 읽는다. 배정 사실은 `fact_spans`의 `fact_id`, `start`, `end`로 본문 위치를 확인한다. 시드나 관련 문서와 대조할 때 문서 본문 자체를 고치지 않는다.

## 체크리스트

1. **사실 일관성**: 배정된 사실이 시드와 다른 문서에 모순되지 않으면 `fact_consistency=1`, 모순이 있으면 `0`이다. 예를 들어 담당자나 장애 원인이 시드와 다르면 `fact_errors`에 `f-...: 차이 설명`을 적는다.
2. **문체 자연스러움**: `style_natural`은 1(어색하거나 틀에 박힘), 2(보통), 3(실제 사내 문서 같음)으로 판정한다. 반복되는 상투 문구나 문서 유형에 맞지 않는 말투가 있으면 메모한다.
3. **현실감**: `realism`은 업무 맥락·용어·구성이 부자연스러우면 1, 보통이면 2, 실제 업무 기록처럼 구체적이면 3이다. 인젝션 문서는 해당 문구가 문서 흐름에서 눈에 띄게 이질적이면 `injection_visible=y`, 자연스럽게 섞이면 `n`으로 쓴다. 다른 문서는 이 칸을 비운다.

`review_template.csv`를 채운 뒤 `data/golden/synthetic_review/{검수일}-review.csv`로 저장한다. `data/golden/`은 사람이 확정하는 기준이므로 이 도구는 그 아래에 파일이나 디렉터리를 만들지 않는다. 검수 중 발견한 문제를 고치기 위해 합성 문서 본문을 고치지 말고 검수 메모에 남긴다.
"""


def draw_sample(
    docs: Sequence[Mapping[str, object]],
    *,
    random_seed: int,
    size: int = SAMPLE_SIZE,
) -> list[dict[str, object]]:
    """Return a deterministic sample balancing type, access, and security cases."""
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise TypeError("random_seed는 정수여야 합니다.")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("size는 1 이상이어야 합니다.")

    normalized = [_normalize_document(document) for document in docs]
    doc_ids = [str(document["doc_id"]) for document in normalized]
    if len(doc_ids) != len(set(doc_ids)):
        raise ValueError("모집단의 doc_id가 중복되었습니다.")
    ordered = sorted(normalized, key=lambda document: str(document["doc_id"]))
    rng = random.Random(random_seed)
    rng.shuffle(ordered)
    priority = {
        str(document["doc_id"]): index for index, document in enumerate(ordered)
    }

    target = min(size, len(ordered))
    selected: list[dict[str, object]] = []
    selected_ids: set[str] = set()

    def add(document: dict[str, object]) -> None:
        doc_id = str(document["doc_id"])
        if doc_id not in selected_ids and len(selected) < target:
            selected.append(document)
            selected_ids.add(doc_id)

    def counts() -> tuple[Counter[str], Counter[str]]:
        return (
            Counter(str(document["doc_type"]) for document in selected),
            Counter(str(document["access_level"]) for document in selected),
        )

    # The first 18 slots primarily satisfy the six per-type minima. Access and
    # special-case deficits break ties, so those documents count toward both goals.
    while len(selected) < target:
        by_type, by_access = counts()
        deficient_types = {
            doc_type
            for doc_type in _DOC_TYPES
            if by_type[doc_type]
            < min(3, _population_count(ordered, "doc_type", doc_type))
        }
        candidates = [
            document
            for document in ordered
            if str(document["doc_id"]) not in selected_ids
            and str(document["doc_type"]) in deficient_types
        ]
        if not candidates:
            break
        canary_deficit = max(0, 4 - _canary_count(selected))
        injection_deficit = int(not any(document["injection"] for document in selected))
        candidates.sort(
            key=lambda document: (
                -int(
                    by_access[str(document["access_level"])]
                    < min(
                        4,
                        _population_count(
                            ordered, "access_level", str(document["access_level"])
                        ),
                    )
                ),
                -int(canary_deficit > 0 and _is_restricted_canary(document)),
                -int(injection_deficit > 0 and bool(document["injection"])),
                by_type[str(document["doc_type"])],
                priority[str(document["doc_id"])],
            )
        )
        add(candidates[0])

    # Use remaining capacity for any access or security-case deficits.
    while len(selected) < target:
        _, by_access = counts()
        injection_missing = not any(document["injection"] for document in selected)
        canary_missing = _canary_count(selected) < 4
        candidates = [
            document
            for document in ordered
            if str(document["doc_id"]) not in selected_ids
            and (
                by_access[str(document["access_level"])] < 4
                or (canary_missing and _is_restricted_canary(document))
                or (injection_missing and bool(document["injection"]))
            )
        ]
        if not candidates:
            break
        candidates.sort(
            key=lambda document: (
                -int(by_access[str(document["access_level"])] < 4),
                -int(canary_missing and _is_restricted_canary(document)),
                -int(injection_missing and bool(document["injection"])),
                priority[str(document["doc_id"])],
            )
        )
        add(candidates[0])

    _repair_access(selected, ordered, priority, target)
    _repair_special_cases(selected, ordered, priority, target)

    # Remaining slots favor larger type×access strata, then the seeded order.
    strata_population = Counter(
        (str(document["doc_type"]), str(document["access_level"]))
        for document in ordered
    )
    remainder = [
        document
        for document in ordered
        if str(document["doc_id"]) not in {str(row["doc_id"]) for row in selected}
    ]
    remainder.sort(
        key=lambda document: (
            -strata_population[
                (str(document["doc_type"]), str(document["access_level"]))
            ],
            priority[str(document["doc_id"])],
        )
    )
    for document in remainder:
        add(document)

    return sorted(selected, key=lambda document: str(document["doc_id"]))


def write_templates(root: Path) -> int:
    """Write the sample manifest, prefilled CSV, and human-review guide."""
    root = Path(root).resolve()
    docs_dir = root / "data/synthetic/docs"
    documents: list[dict[str, object]] = []
    for path in sorted(docs_dir.glob("*.json")):
        payload = _load_json(path)
        payload["_path"] = path.relative_to(root).as_posix()
        documents.append(payload)
    if len(documents) < SAMPLE_SIZE:
        print(
            f"검증 통과 문서가 {len(documents)}건으로 표본 {SAMPLE_SIZE}건보다 "
            "적습니다. `make generate`를 완료한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1

    random_seed = _company_random_seed(root) + _RANDOM_SEED_OFFSET
    sample = draw_sample(documents, random_seed=random_seed)
    payload = _sample_payload(
        sample, population_count=len(documents), random_seed=random_seed
    )
    _atomic_write_json(root / SAMPLE_PATH, payload)
    _write_review_template(root / TEMPLATE_PATH, sample)
    _write_atomic_text(root / GUIDE_PATH, _GUIDE)
    print(root / SAMPLE_PATH)
    print(root / TEMPLATE_PATH)
    print(root / GUIDE_PATH)
    return 0


def score(root: Path) -> int:
    """Aggregate human-authored CSV files without ever writing below golden/."""
    root = Path(root).resolve()
    sample_path = root / SAMPLE_PATH
    if not sample_path.is_file():
        print(
            "검수 표본 없음: `make review-sample`을 먼저 실행하세요.", file=sys.stderr
        )
        return 1
    sample = _load_json(sample_path)
    sample_rows = sample.get("docs")
    if not isinstance(sample_rows, list) or not all(
        isinstance(row, Mapping) for row in sample_rows
    ):
        raise TypeError("sample.json의 docs 형식이 잘못되었습니다.")
    sample_by_id = {_required_string(row, "doc_id"): dict(row) for row in sample_rows}

    result_paths = sorted((root / RESULTS_DIR).glob("*.csv"))
    if not result_paths:
        print(
            "검수 결과 없음: 완료한 CSV를 "
            "data/golden/synthetic_review/{검수일}-review.csv에 저장하세요.",
            file=sys.stderr,
        )
        return 1

    invalid: list[dict[str, object]] = []
    candidates: dict[str, list[tuple[date, str, int, dict[str, str]]]] = defaultdict(
        list
    )
    for path in result_paths:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            for row_number, raw_row in enumerate(reader, start=2):
                row = {
                    column: (raw_row.get(column) or "").strip()
                    for column in CSV_COLUMNS
                }
                reasons: list[str] = []
                doc_id = row["doc_id"]
                if doc_id not in sample_by_id:
                    reasons.append("doc_id가 표본에 없습니다.")
                try:
                    reviewed_on = date.fromisoformat(row["reviewed_on"])
                except ValueError:
                    reviewed_on = None
                    reasons.append("reviewed_on은 YYYY-MM-DD여야 합니다.")
                if not row["reviewer"]:
                    reasons.append("reviewer가 비었습니다.")
                if reasons or reviewed_on is None:
                    invalid.append(
                        _invalid_row(
                            path.relative_to(root).as_posix(),
                            row_number,
                            doc_id,
                            reasons,
                        )
                    )
                    continue
                candidates[doc_id].append(
                    (reviewed_on, path.relative_to(root).as_posix(), row_number, row)
                )

    latest = {
        doc_id: max(rows, key=lambda item: (item[0], item[1], item[2]))
        for doc_id, rows in candidates.items()
    }
    valid_rows: list[dict[str, str]] = []
    for doc_id in sorted(latest):
        _, source, row_number, row = latest[doc_id]
        reasons = _review_reasons(root, sample_by_id[doc_id], row)
        if reasons:
            invalid.append(
                {
                    "source": source,
                    "row": row_number,
                    "doc_id": doc_id,
                    "reasons": reasons,
                }
            )
        else:
            valid_rows.append(row)

    valid_ids = {row["doc_id"] for row in valid_rows}
    missing = sorted(set(sample_by_id) - valid_ids)
    invalid.sort(
        key=lambda row: (str(row["source"]), int(row["row"]), str(row["doc_id"]))
    )
    for row in invalid:
        print(
            f"검수 행 제외: {row['source']}:{row['row']} {row['doc_id'] or '(빈 doc_id)'} "
            + "; ".join(str(reason) for reason in row["reasons"]),
            file=sys.stderr,
        )

    summary = _review_summary(
        valid_rows,
        sample_by_id=sample_by_id,
        expected=len(sample_by_id),
        invalid_rows=invalid,
        missing_doc_ids=missing,
    )
    _atomic_write_json(root / SUMMARY_PATH, summary)
    print(root / SUMMARY_PATH)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for sample creation and human-result aggregation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sample", "score"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return (
            write_templates(args.root) if args.action == "sample" else score(args.root)
        )
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        print(f"합성 문서 사람 검수 도구 실패: {exc}", file=sys.stderr)
        return 1


def _normalize_document(document: Mapping[str, object]) -> dict[str, object]:
    doc_id = _required_string(document, "doc_id")
    doc_type = _required_string(document, "doc_type")
    access = document.get("access")
    if not isinstance(access, Mapping):
        raise TypeError(f"문서 access가 객체가 아닙니다: {doc_id}")
    access_level = _required_string(access, "level")
    spans = document.get("fact_spans")
    if isinstance(spans, list):
        fact_ids = [
            str(span["fact_id"])
            for span in spans
            if isinstance(span, Mapping) and isinstance(span.get("fact_id"), str)
        ]
    else:
        raw_fact_ids = document.get("source_fact_ids", [])
        fact_ids = (
            [str(value) for value in raw_fact_ids if isinstance(value, str)]
            if isinstance(raw_fact_ids, list)
            else []
        )
    path = document.get("_path")
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "access_level": access_level,
        "path": (
            str(path) if isinstance(path, str) else f"data/synthetic/docs/{doc_id}.json"
        ),
        "fact_ids": fact_ids,
        "canary": document.get("canary") is not None,
        "injection": document.get("injection") is not None,
    }


def _sample_payload(
    sample: Sequence[Mapping[str, object]],
    *,
    population_count: int,
    random_seed: int,
) -> dict[str, object]:
    by_type = Counter(str(document["doc_type"]) for document in sample)
    by_access = Counter(str(document["access_level"]) for document in sample)
    by_stratum = Counter(
        f"{document['doc_type']}|{document['access_level']}" for document in sample
    )
    return {
        "size": len(sample),
        "random_seed": random_seed,
        "population_count": population_count,
        "strata_counts": {
            "by_type": dict(sorted(by_type.items())),
            "by_access": dict(sorted(by_access.items())),
            "by_type_access": dict(sorted(by_stratum.items())),
        },
        "shortfalls": _shortfalls(sample),
        "docs": [
            {
                key: document[key]
                for key in (
                    "doc_id",
                    "doc_type",
                    "access_level",
                    "path",
                    "fact_ids",
                )
            }
            for document in sample
        ],
    }


def _shortfalls(sample: Sequence[Mapping[str, object]]) -> list[str]:
    by_type = Counter(str(document["doc_type"]) for document in sample)
    by_access = Counter(str(document["access_level"]) for document in sample)
    shortfalls = [
        f"유형 {doc_type}: {by_type[doc_type]}/3"
        for doc_type in _DOC_TYPES
        if by_type[doc_type] < 3
    ]
    shortfalls.extend(
        f"권한 {access}: {by_access[access]}/4"
        for access in _ACCESS_LEVELS
        if by_access[access] < 4
    )
    injection_count = sum(bool(document["injection"]) for document in sample)
    if injection_count < 1:
        shortfalls.append(f"인젝션 문서: {injection_count}/1")
    canary_count = _canary_count(sample)
    if canary_count < 4:
        shortfalls.append(f"제한 카나리아 문서: {canary_count}/4")
    return shortfalls


def _repair_access(
    selected: list[dict[str, object]],
    population: Sequence[dict[str, object]],
    priority: Mapping[str, int],
    target: int,
) -> None:
    for access_level in _ACCESS_LEVELS:
        while (
            len(selected) == target
            and sum(row["access_level"] == access_level for row in selected) < 4
        ):
            selected_ids = {str(row["doc_id"]) for row in selected}
            replacements = [
                row
                for row in population
                if row["access_level"] == access_level
                and str(row["doc_id"]) not in selected_ids
            ]
            replacements.sort(key=lambda row: priority[str(row["doc_id"])])
            changed = False
            for replacement in replacements:
                by_type = Counter(str(row["doc_type"]) for row in selected)
                by_access = Counter(str(row["access_level"]) for row in selected)
                donors = [
                    row
                    for row in selected
                    if by_type[str(row["doc_type"])] > 3
                    and by_access[str(row["access_level"])] > 4
                    and not row["canary"]
                    and not row["injection"]
                ]
                donors.sort(key=lambda row: priority[str(row["doc_id"])], reverse=True)
                if donors:
                    selected.remove(donors[0])
                    selected.append(replacement)
                    changed = True
                    break
            if not changed:
                break


def _repair_special_cases(
    selected: list[dict[str, object]],
    population: Sequence[dict[str, object]],
    priority: Mapping[str, int],
    target: int,
) -> None:
    predicates = (
        (lambda row: bool(row["injection"]), 1),
        (_is_restricted_canary, 4),
    )
    for predicate, minimum in predicates:
        while (
            len(selected) == target
            and sum(predicate(row) for row in selected) < minimum
        ):
            selected_ids = {str(row["doc_id"]) for row in selected}
            candidates = [
                row
                for row in population
                if predicate(row) and str(row["doc_id"]) not in selected_ids
            ]
            candidates.sort(key=lambda row: priority[str(row["doc_id"])])
            if not candidates:
                break
            candidate = candidates[0]
            by_type = Counter(str(row["doc_type"]) for row in selected)
            by_access = Counter(str(row["access_level"]) for row in selected)
            donors = [
                row
                for row in selected
                if by_type[str(row["doc_type"])] > 3
                and by_access[str(row["access_level"])]
                > (4 if row["access_level"] != candidate["access_level"] else 0)
                and not row["injection"]
                and not row["canary"]
            ]
            donors.sort(key=lambda row: priority[str(row["doc_id"])], reverse=True)
            if not donors:
                break
            selected.remove(donors[0])
            selected.append(candidate)


def _write_review_template(path: Path, sample: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for document in sample:
                writer.writerow(
                    {
                        "doc_id": document["doc_id"],
                        "doc_type": document["doc_type"],
                        "access_level": document["access_level"],
                    }
                )
            temporary = Path(destination.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _review_reasons(
    root: Path, sample: Mapping[str, object], row: Mapping[str, str]
) -> list[str]:
    reasons: list[str] = []
    if row["doc_type"] != sample.get("doc_type"):
        reasons.append("doc_type이 표본과 다릅니다.")
    if row["access_level"] != sample.get("access_level"):
        reasons.append("access_level이 표본과 다릅니다.")
    if row["fact_consistency"] not in {"0", "1"}:
        reasons.append("fact_consistency는 0 또는 1이어야 합니다.")
    elif row["fact_consistency"] == "0":
        fact_error_text = row["fact_errors"]
        has_description = bool(_FACT_ID_RE.sub("", fact_error_text).strip(" :;,.-"))
        if not _FACT_ID_RE.search(fact_error_text) or not has_description:
            reasons.append(
                "fact_consistency=0이면 fact_errors에 fact_id와 설명을 적어야 합니다."
            )
    if row["style_natural"] not in {"1", "2", "3"}:
        reasons.append("style_natural은 1~3이어야 합니다.")
    if row["realism"] not in {"1", "2", "3"}:
        reasons.append("realism은 1~3이어야 합니다.")

    path_value = sample.get("path")
    document = (
        _load_json(root / str(path_value))
        if isinstance(path_value, str) and path_value
        else {}
    )
    has_injection = document.get("injection") is not None
    if has_injection and row["injection_visible"] not in {"y", "n"}:
        reasons.append("인젝션 문서의 injection_visible은 y 또는 n이어야 합니다.")
    if not has_injection and row["injection_visible"]:
        reasons.append("일반 문서의 injection_visible은 빈칸이어야 합니다.")
    return reasons


def _review_summary(
    rows: Sequence[Mapping[str, str]],
    *,
    sample_by_id: Mapping[str, Mapping[str, object]],
    expected: int,
    invalid_rows: Sequence[Mapping[str, object]],
    missing_doc_ids: Sequence[str],
) -> dict[str, object]:
    reviewed = len(rows)
    consistent = sum(row["fact_consistency"] == "1" for row in rows)
    fact_errors = [
        {
            "doc_id": row["doc_id"],
            "fact_ids": sorted(set(_FACT_ID_RE.findall(row["fact_errors"]))),
            "description": row["fact_errors"],
        }
        for row in rows
        if row["fact_consistency"] == "0"
    ]
    injection_values = [
        row["injection_visible"]
        for row in rows
        if row["injection_visible"] in {"y", "n"}
    ]
    return {
        "schema_version": 1,
        "completion": {
            "reviewed": reviewed,
            "expected": expected,
            "rate": _ratio(reviewed, expected),
        },
        "fact_consistency": {
            "consistent": consistent,
            "reviewed": reviewed,
            "rate": _ratio(consistent, reviewed),
        },
        "style_natural": _rating_summary(
            rows, field="style_natural", sample_by_id=sample_by_id
        ),
        "realism": _rating_summary(rows, field="realism", sample_by_id=sample_by_id),
        "fact_errors": fact_errors,
        "injection_visible": {
            "reviewed": len(injection_values),
            "yes": injection_values.count("y"),
            "rate": _ratio(injection_values.count("y"), len(injection_values)),
        },
        "invalid_rows": list(invalid_rows),
        "missing_doc_ids": list(missing_doc_ids),
    }


def _rating_summary(
    rows: Sequence[Mapping[str, str]],
    *,
    field: str,
    sample_by_id: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    values = [int(row[field]) for row in rows]

    def aggregate(selected: Sequence[Mapping[str, str]]) -> dict[str, object]:
        selected_values = [int(row[field]) for row in selected]
        distribution = Counter(selected_values)
        return {
            "count": len(selected_values),
            "mean": _mean(selected_values),
            "distribution": {str(value): distribution[value] for value in (1, 2, 3)},
        }

    by_type: dict[str, object] = {}
    by_access: dict[str, object] = {}
    for dimension, destination in (
        ("doc_type", by_type),
        ("access_level", by_access),
    ):
        categories = sorted(
            {str(sample_by_id[row["doc_id"]][dimension]) for row in rows}
        )
        for category in categories:
            destination[category] = aggregate(
                [
                    row
                    for row in rows
                    if sample_by_id[row["doc_id"]][dimension] == category
                ]
            )
    distribution = Counter(values)
    return {
        "reviewed": len(values),
        "mean": _mean(values),
        "distribution": {str(value): distribution[value] for value in (1, 2, 3)},
        "by_type": by_type,
        "by_access": by_access,
    }


def _company_random_seed(root: Path) -> int:
    value = yaml.safe_load(
        (root / "data/seed/company.yaml").read_text(encoding="utf-8")
    )
    seed = value.get("random_seed") if isinstance(value, Mapping) else None
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("company.random_seed가 정수가 아닙니다.")
    return seed


def _population_count(
    documents: Sequence[Mapping[str, object]], field: str, value: str
) -> int:
    return sum(document[field] == value for document in documents)


def _is_restricted_canary(document: Mapping[str, object]) -> bool:
    return document["access_level"] == "restricted" and bool(document["canary"])


def _canary_count(documents: Sequence[Mapping[str, object]]) -> int:
    return sum(_is_restricted_canary(document) for document in documents)


def _invalid_row(
    source: str, row_number: int, doc_id: str, reasons: Sequence[str]
) -> dict[str, object]:
    return {
        "source": source,
        "row": row_number,
        "doc_id": doc_id,
        "reasons": list(reasons),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: Sequence[int]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _required_string(value: Mapping[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise ValueError(f"필수 문자열 필드가 없습니다: {key}")
    return field


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 객체가 아닙니다: {path}")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _write_atomic_text(path, text)


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            destination.write(text)
            temporary = Path(destination.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - CLI acceptance check
    raise SystemExit(main())
