"""Deterministically preview leakage-aware dev/test question splits."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from kodocs.eval.golden import (
    ATTENTION_CHECK_PREFIX,
    GOLD_SPLIT_QUOTA,
    QUESTION_TYPES,
    Question,
    load_questions,
)

SPLIT_SEED_OFFSET = 404

SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")
REAL_PROJECTION_PATH = Path("data/synthetic/queries/real_projection.jsonl")
PREVIEW_PATH = Path("data/manifest/golden_split_preview.json")
COMPANY_PATH = Path("data/seed/company.yaml")
REGULATION_REFS_PATH = Path("data/manifest/regulation_refs.json")
LAW_DIR = Path("data/interim/law")

_RETRIEVAL_TYPES = frozenset(
    {"identifier", "semantic", "table", "multi_doc", "temporal"}
)
_H5_TYPES = frozenset({"identifier", "sql", "multi_doc"})
_H7_TYPES = frozenset({"identifier", "semantic", "multi_doc"})
_TEMPORAL_TAGS = ("temporal:revision", "temporal:as_of_event")
_OVERSIZED_GROUP_SHARE = 0.3


class _UnionFind:
    def __init__(self, values: Sequence[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            self.parent[right_root] = left_root
        else:
            self.parent[left_root] = right_root


def question_groups(questions: Sequence[Question]) -> list[list[str]]:
    """Group questions by shared rel=2 synthetic docs or real-law rule IDs."""
    return _question_groups(questions, Path.cwd())


def assign_split(
    questions: Sequence[Question],
    quota: Mapping[str, Mapping[str, int]] | None,
    *,
    random_seed: int,
    test_share: float = 0.6,
) -> tuple[dict[str, str], dict]:
    """Assign whole question groups to dev/test as close to quotas as possible."""
    return _assign_split(
        questions,
        quota,
        random_seed=random_seed,
        test_share=test_share,
        root=Path.cwd(),
    )


def split_report(questions: Sequence[Question], assignment: Mapping[str, str]) -> dict:
    """Summarize allocation, leakage, and preregistered hypothesis counts."""
    return _split_report(questions, assignment, root=Path.cwd())


def preview(root: Path) -> int:
    """Write a deterministic split preview without creating golden questions."""
    root = Path(root).resolve()
    try:
        synthetic = load_questions(root / SPECS_PATH)
        real_path = root / REAL_PROJECTION_PATH
        real = load_questions(real_path) if real_path.is_file() else []
        seed = _company_random_seed(root) + SPLIT_SEED_OFFSET

        gold = [question for question in synthetic if question.set == "gold"] + real
        silver = [question for question in synthetic if question.set == "silver"]
        _, gold_report = _assign_split(
            gold,
            GOLD_SPLIT_QUOTA,
            random_seed=seed,
            test_share=0.6,
            root=root,
        )
        _, silver_report = _assign_split(
            silver,
            None,
            random_seed=seed,
            test_share=0.6,
            root=root,
        )
        if not real:
            gold_report["warnings"].append(
                "real_projection_missing:합성 골드 부분만 미리보기"
            )

        cross_doc_ids = sorted(
            set(gold_report["cross_split_docs"]["doc_ids"])
            | set(silver_report["cross_split_docs"]["doc_ids"])
        )
        payload = {
            "random_seed": seed,
            "split_seed_offset": SPLIT_SEED_OFFSET,
            "test_share": 0.6,
            "real_items": {
                "status": "included" if real else "missing",
                "n": len(real),
                "note": (
                    "실제 골드 투영을 포함함"
                    if real
                    else "실제 골드 20문항이 없어 합성 골드 부분만 보고함"
                ),
            },
            "gold": gold_report,
            "silver": silver_report,
            "cross_split_docs": {
                "n": (
                    gold_report["cross_split_docs"]["n"]
                    + silver_report["cross_split_docs"]["n"]
                ),
                "doc_ids": cross_doc_ids[:20],
            },
        }
        _write_json_atomic(root / PREVIEW_PATH, payload)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"golden split 미리보기 실패: {str(exc).splitlines()[0]}")
        return 1

    print(
        "golden split 미리보기 완료: "
        f"gold={len(gold)}, silver={len(silver)}, {PREVIEW_PATH.as_posix()}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview",))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return preview(args.root)


def _question_groups(questions: Sequence[Question], root: Path) -> list[list[str]]:
    _validate_question_ids(questions)
    if not questions:
        return []

    union_find = _UnionFind([question.id for question in questions])
    serial_to_rule = _serial_to_rule_id(root) if _has_real_qrels(questions) else {}
    owners: dict[str, str] = {}
    for question in sorted(questions, key=lambda item: item.id):
        tokens: set[str] = set()
        for qrel in question.qrels:
            if qrel.rel != 2:
                continue
            if question.source == "real":
                serial = _law_serial(qrel.doc_id)
                rule_id = serial_to_rule.get(serial)
                tokens.add(f"rule:{rule_id}" if rule_id else f"law:{serial}")
            else:
                tokens.add(f"doc:{qrel.doc_id}")
        for token in sorted(tokens):
            owner = owners.setdefault(token, question.id)
            union_find.union(owner, question.id)

    grouped: dict[str, list[str]] = defaultdict(list)
    for question in sorted(questions, key=lambda item: item.id):
        grouped[union_find.find(question.id)].append(question.id)
    return sorted(
        (sorted(group) for group in grouped.values()), key=lambda group: group[0]
    )


def _assign_split(
    questions: Sequence[Question],
    quota: Mapping[str, Mapping[str, int]] | None,
    *,
    random_seed: int,
    test_share: float,
    root: Path,
) -> tuple[dict[str, str], dict]:
    _validate_question_ids(questions)
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise TypeError("random_seed는 정수여야 합니다.")
    if not 0 <= test_share <= 1:
        raise ValueError("test_share는 0 이상 1 이하여야 합니다.")

    questions_by_id = {question.id: question for question in questions}
    type_order = _type_order(questions, quota)
    totals = Counter(question.type for question in questions)
    targets = _split_targets(totals, type_order, quota, test_share)
    groups = [
        [questions_by_id[question_id] for question_id in group]
        for group in _question_groups(questions, root)
    ]
    rng = random.Random(random_seed)
    test_group_indexes, _, distance = _optimize_groups(
        groups, type_order, totals, targets, rng
    )
    warnings: list[str] = []

    while distance:
        oversized = [
            (index, group)
            for index, group in enumerate(groups)
            if len(questions) > 1
            and len(group) / len(questions) > _OVERSIZED_GROUP_SHARE
            and len(group) > 1
        ]
        if not oversized:
            break
        group_index, group = max(
            oversized,
            key=lambda item: (len(item[1]), tuple(question.id for question in item[1])),
        )
        left, right = _best_large_group_boundary(
            groups,
            group_index,
            type_order,
            totals,
            targets,
            rng,
        )
        groups = [
            value for index, value in enumerate(groups) if index != group_index
        ] + [left, right]
        warnings.append(
            "oversized_group_split:"
            f"size={len(group)}:left={len(left)}:right={len(right)}"
        )
        test_group_indexes, _, distance = _optimize_groups(
            groups, type_order, totals, targets, rng
        )

    assignment: dict[str, str] = {}
    for index, group in enumerate(groups):
        split = "test" if index in test_group_indexes else "dev"
        for question in group:
            assignment[question.id] = split
    assignment = dict(sorted(assignment.items()))

    report = _split_report(questions, assignment, root=root)
    report["quota_diff"] = _quota_diff(questions, assignment, type_order, targets)
    if any(
        difference
        for by_type in report["quota_diff"].values()
        for difference in by_type.values()
    ):
        warnings.append("quota_not_exact:" + _format_quota_diff(report["quota_diff"]))
    report["warnings"] = warnings
    return assignment, report


def _split_report(
    questions: Sequence[Question], assignment: Mapping[str, str], *, root: Path
) -> dict:
    _validate_question_ids(questions)
    expected_ids = {question.id for question in questions}
    if set(assignment) != expected_ids:
        missing = sorted(expected_ids - set(assignment))
        extra = sorted(set(assignment) - expected_ids)
        raise ValueError(
            f"split assignment ids mismatch: missing={missing}, extra={extra}"
        )
    invalid = sorted(
        question_id
        for question_id, split in assignment.items()
        if split not in {"dev", "test"}
    )
    if invalid:
        raise ValueError(f"invalid split values: {invalid}")

    type_order = _type_order(questions, None)
    by_type = {
        question_type: {
            "test": sum(
                assignment[question.id] == "test" and question.type == question_type
                for question in questions
            ),
            "dev": sum(
                assignment[question.id] == "dev" and question.type == question_type
                for question in questions
            ),
        }
        for question_type in type_order
    }
    inferred_quota = (
        GOLD_SPLIT_QUOTA
        if questions and {question.set for question in questions} == {"gold"}
        else None
    )
    totals = Counter(question.type for question in questions)
    targets = _split_targets(totals, type_order, inferred_quota, 0.6)
    groups = _question_groups(questions, root)
    sizes = Counter(len(group) for group in groups)
    cross_docs = _cross_split_doc_ids(questions, assignment)

    retrieval_test_n = sum(
        assignment[question.id] == "test"
        and question.type in _RETRIEVAL_TYPES
        and bool(question.qrels)
        for question in questions
    )
    h5_bundle_test_n = sum(
        assignment[question.id] == "test" and question.type in _H5_TYPES
        for question in questions
    )
    h7 = {
        "synthetic_n": sum(
            assignment[question.id] == "test"
            and question.type in _H7_TYPES
            and question.source == "synthetic"
            for question in questions
        ),
        "real_n": sum(
            assignment[question.id] == "test"
            and question.type in _H7_TYPES
            and question.source == "real"
            for question in questions
        ),
    }
    temporal_tags = {
        tag: {
            "test": sum(
                assignment[question.id] == "test" and tag in question.tags
                for question in questions
            ),
            "dev": sum(
                assignment[question.id] == "dev" and tag in question.tags
                for question in questions
            ),
        }
        for tag in _TEMPORAL_TAGS
    }
    return {
        "by_type": by_type,
        "quota_diff": _quota_diff(questions, assignment, type_order, targets),
        "groups": {
            "n": len(groups),
            "max_size": max((len(group) for group in groups), default=0),
            "size_hist": {str(size): sizes[size] for size in sorted(sizes)},
        },
        "cross_split_docs": {"n": len(cross_docs), "doc_ids": cross_docs[:20]},
        "retrieval_test_n": retrieval_test_n,
        "h5_bundle_test_n": h5_bundle_test_n,
        "h7": h7,
        "temporal_tags": temporal_tags,
        "warnings": [],
    }


def _optimize_groups(
    groups: Sequence[Sequence[Question]],
    type_order: Sequence[str],
    totals: Mapping[str, int],
    targets: Mapping[str, Mapping[str, int]],
    rng: random.Random,
) -> tuple[set[int], dict[str, int], int]:
    """Return the exact minimum-L1 group allocation using sparse subset DP."""
    vectors = [Counter(question.type for question in group) for group in groups]
    pure_by_type: dict[str, list[int]] = defaultdict(list)
    mixed: list[int] = []
    for index, vector in enumerate(vectors):
        nonzero = [question_type for question_type, count in vector.items() if count]
        if len(nonzero) == 1:
            pure_by_type[nonzero[0]].append(index)
        else:
            mixed.append(index)

    pure_options: dict[str, dict[int, tuple[int, ...]]] = {}
    for question_type in type_order:
        indexes = list(pure_by_type.get(question_type, ()))
        rng.shuffle(indexes)
        options: dict[int, tuple[int, ...]] = {0: ()}
        for index in indexes:
            size = vectors[index][question_type]
            for count, selected in list(options.items()):
                options.setdefault(count + size, (*selected, index))
        pure_options[question_type] = options

    rng.shuffle(mixed)
    zero = (0,) * len(type_order)
    mixed_states: dict[tuple[int, ...], tuple[int, ...]] = {zero: ()}
    for index in mixed:
        vector = vectors[index]
        for counts, selected in list(mixed_states.items()):
            next_counts = tuple(
                counts[position] + vector[question_type]
                for position, question_type in enumerate(type_order)
            )
            mixed_states.setdefault(next_counts, (*selected, index))

    best_key: tuple[int, int, int] | None = None
    best_selected: set[int] = set()
    best_counts: dict[str, int] = {question_type: 0 for question_type in type_order}
    target_test_total = sum(targets["test"].values())
    for mixed_counts, mixed_selected in mixed_states.items():
        selected = set(mixed_selected)
        final_counts: dict[str, int] = {}
        for position, question_type in enumerate(type_order):
            base = mixed_counts[position]
            choices = pure_options[question_type]
            pure_count, pure_selected = min(
                choices.items(),
                key=lambda item: _type_distance(
                    base + item[0],
                    totals.get(question_type, 0),
                    targets["test"][question_type],
                    targets["dev"][question_type],
                ),
            )
            final_counts[question_type] = base + pure_count
            selected.update(pure_selected)

        differences = [
            final_counts[question_type] - targets["test"][question_type]
            for question_type in type_order
        ] + [
            totals.get(question_type, 0)
            - final_counts[question_type]
            - targets["dev"][question_type]
            for question_type in type_order
        ]
        key = (
            sum(abs(value) for value in differences),
            max((abs(value) for value in differences), default=0),
            abs(sum(final_counts.values()) - target_test_total),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_selected = selected
            best_counts = final_counts

    return best_selected, best_counts, 0 if best_key is None else best_key[0]


def _best_large_group_boundary(
    groups: Sequence[Sequence[Question]],
    group_index: int,
    type_order: Sequence[str],
    totals: Mapping[str, int],
    targets: Mapping[str, Mapping[str, int]],
    rng: random.Random,
) -> tuple[list[Question], list[Question]]:
    ordered = sorted(groups[group_index], key=lambda question: question.id)
    rng.shuffle(ordered)
    best_key: tuple[int, int, int, int] | None = None
    best_parts: tuple[list[Question], list[Question]] | None = None
    for boundary in range(1, len(ordered)):
        left = ordered[:boundary]
        right = ordered[boundary:]
        crossing = _rel2_doc_ids(left) & _rel2_doc_ids(right)
        candidate_groups = [
            value for index, value in enumerate(groups) if index != group_index
        ] + [left, right]
        _, _, distance = _optimize_groups(
            candidate_groups, type_order, totals, targets, rng
        )
        key = (len(crossing), distance, abs(len(left) - len(right)), boundary)
        if best_key is None or key < best_key:
            best_key = key
            best_parts = (left, right)
    if best_parts is None:
        raise ValueError("oversized group boundary를 찾을 수 없습니다.")
    return best_parts


def _split_targets(
    totals: Mapping[str, int],
    type_order: Sequence[str],
    quota: Mapping[str, Mapping[str, int]] | None,
    test_share: float,
) -> dict[str, dict[str, int]]:
    if quota is None:
        test = {
            question_type: round(totals.get(question_type, 0) * test_share)
            for question_type in type_order
        }
        dev = {
            question_type: totals.get(question_type, 0) - test[question_type]
            for question_type in type_order
        }
        return {"test": test, "dev": dev}
    if set(quota) != {"test", "dev"}:
        raise ValueError("quota는 test와 dev 매핑을 가져야 합니다.")
    return {
        split: {
            question_type: _quota_value(quota[split], question_type)
            for question_type in type_order
        }
        for split in ("test", "dev")
    }


def _quota_diff(
    questions: Sequence[Question],
    assignment: Mapping[str, str],
    type_order: Sequence[str],
    targets: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    return {
        split: {
            question_type: sum(
                assignment[question.id] == split and question.type == question_type
                for question in questions
            )
            - targets[split][question_type]
            for question_type in type_order
        }
        for split in ("test", "dev")
    }


def _type_order(
    questions: Sequence[Question],
    quota: Mapping[str, Mapping[str, int]] | None,
) -> list[str]:
    values = {question.type for question in questions}
    if quota is not None:
        for split_values in quota.values():
            values.update(split_values)
    return [
        question_type for question_type in QUESTION_TYPES if question_type in values
    ] + sorted(values - set(QUESTION_TYPES))


def _type_distance(actual_test: int, total: int, test: int, dev: int) -> int:
    return abs(actual_test - test) + abs(total - actual_test - dev)


def _quota_value(values: Mapping[str, int], question_type: str) -> int:
    value = values.get(question_type, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"quota[{question_type}]는 0 이상의 정수여야 합니다.")
    return value


def _validate_question_ids(questions: Sequence[Question]) -> None:
    ids = [question.id for question in questions]
    checks = sorted(
        question_id
        for question_id in ids
        if question_id.startswith(ATTENTION_CHECK_PREFIX)
    )
    if checks:
        raise ValueError(
            f"{ATTENTION_CHECK_PREFIX} 점검용 문항은 split할 수 없습니다: {checks}"
        )
    duplicates = sorted(
        question_id for question_id, count in Counter(ids).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"중복 문항 ID: {duplicates}")


def _has_real_qrels(questions: Sequence[Question]) -> bool:
    return any(question.source == "real" and question.qrels for question in questions)


def _law_serial(doc_id: str) -> str:
    if not doc_id.startswith("law-") or not doc_id.removeprefix("law-"):
        raise ValueError(
            f"실제 문항 qrel doc_id가 law-serial 형식이 아닙니다: {doc_id}"
        )
    return doc_id.removeprefix("law-")


def _serial_to_rule_id(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    refs_path = root / REGULATION_REFS_PATH
    if refs_path.is_file():
        payload = json.loads(refs_path.read_text(encoding="utf-8"))
        rules = payload.get("rules") if isinstance(payload, Mapping) else None
        if isinstance(rules, Mapping):
            for value in rules.values():
                if not isinstance(value, Mapping):
                    continue
                serial = value.get("serial")
                rule_id = value.get("rule_id")
                if isinstance(serial, str) and isinstance(rule_id, str):
                    result[serial] = rule_id
    law_root = root / LAW_DIR
    if law_root.is_dir():
        for path in sorted(law_root.glob("*/*.json")):
            result[path.stem] = path.parent.name
    return result


def _rel2_doc_ids(questions: Sequence[Question]) -> set[str]:
    return {
        qrel.doc_id
        for question in questions
        for qrel in question.qrels
        if qrel.rel == 2
    }


def _cross_split_doc_ids(
    questions: Sequence[Question], assignment: Mapping[str, str]
) -> list[str]:
    splits_by_doc: dict[str, set[str]] = defaultdict(set)
    for question in questions:
        for doc_id in _rel2_doc_ids((question,)):
            splits_by_doc[doc_id].add(assignment[question.id])
    return sorted(doc_id for doc_id, splits in splits_by_doc.items() if len(splits) > 1)


def _format_quota_diff(diff: Mapping[str, Mapping[str, int]]) -> str:
    return ",".join(
        f"{split}.{question_type}={value:+d}"
        for split in ("test", "dev")
        for question_type, value in diff[split].items()
        if value
    )


def _company_random_seed(root: Path) -> int:
    payload = yaml.safe_load((root / COMPANY_PATH).read_text(encoding="utf-8"))
    seed = payload.get("random_seed") if isinstance(payload, Mapping) else None
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("company.random_seed가 정수가 아닙니다.")
    return seed


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
