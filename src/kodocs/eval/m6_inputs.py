"""Build the commit-safe, preregistered M6 evaluation-input manifest."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from kodocs.eval.golden import Question, load_questions
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.tools.deps import M5_CONFIG_HASH

GOLD_PATH = Path("data/golden/queries/gold.jsonl")
SILVER_PATH = Path("data/golden/queries/silver.jsonl")
M5_CONFIG_PATH = Path("data/manifest/m5_config.json")
M6_INPUTS_PATH = Path("data/manifest/m6_eval_inputs.json")

EXPECTED_DEV_COUNT = 75
EXPECTED_H5_COUNT = 13
EXPECTED_ANSWER_DENOMINATOR = 56
EXPECTED_PERMISSION_DEV = 8
EXPECTED_UNANSWERABLE_DEV = 8
EXPECTED_ROUTER_TYPES = {
    "identifier": 13,
    "multi_doc": 12,
    "semantic": 20,
    "temporal": 10,
}


class M6InputError(RuntimeError):
    """A preregistered M6 input invariant changed."""


def build_input_manifest(
    gold: Sequence[Question],
    silver: Sequence[Question],
    *,
    gold_sha256: str,
    silver_sha256: str,
    m5_config_hash: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Return question IDs, counts, and hashes without storing question text."""
    if m5_config_hash != M5_CONFIG_HASH:
        raise M6InputError("m5_config_hash does not match the frozen M6 setting")
    _require_unique_ids((*gold, *silver))
    dev = [question for question in (*gold, *silver) if question.split == "dev"]
    h5 = [
        question
        for question in gold
        if question.split == "test"
        and question.type in {"identifier", "sql", "multi_doc"}
    ]
    denominator = [
        question
        for question in dev
        if question.expected_response == "answer" and question.answer_values
    ]
    excluded = []
    denominator_ids = {question.id for question in denominator}
    for question in dev:
        if question.id in denominator_ids:
            continue
        reasons = []
        if question.expected_response != "answer":
            reasons.append("expected_response_not_answer")
        if not question.answer_values:
            reasons.append("answer_values_missing")
        excluded.append({"id": question.id, "reasons": reasons})

    permission = [question for question in dev if question.type == "permission"]
    unanswerable = [question for question in dev if question.type == "unanswerable"]
    answer_type_counts = Counter(
        question.type for question in dev if question.expected_response == "answer"
    )
    ids_by_type: dict[str, list[str]] = defaultdict(list)
    for question in dev:
        if question.expected_response == "answer":
            ids_by_type[question.type].append(question.id)
    eligible = {
        name: {"count": count, "ids": sorted(ids_by_type[name])}
        for name, count in sorted(answer_type_counts.items())
        if count >= 8
    }
    below = {
        name: {"count": count, "ids": sorted(ids_by_type[name])}
        for name, count in sorted(answer_type_counts.items())
        if count < 8
    }

    _require_count("dev_set", dev, EXPECTED_DEV_COUNT)
    _require_count("h5_pool", h5, EXPECTED_H5_COUNT)
    _require_count("answer_match_denominator", denominator, EXPECTED_ANSWER_DENOMINATOR)
    _require_count("gate_permission_dev", permission, EXPECTED_PERMISSION_DEV)
    _require_count("unanswerable_dev", unanswerable, EXPECTED_UNANSWERABLE_DEV)
    actual_router = {name: value["count"] for name, value in eligible.items()}
    if actual_router != EXPECTED_ROUTER_TYPES:
        raise M6InputError(
            "router_types count mismatch: "
            f"expected {EXPECTED_ROUTER_TYPES}, got {actual_router}"
        )
    if set(below) != {"sql", "table"}:
        raise M6InputError(
            "router_types below_threshold mismatch: expected sql/table, "
            f"got {sorted(below)}"
        )

    by_set_source = {
        set_name: {
            source: sum(
                question.set == set_name and question.source == source
                for question in denominator
            )
            for source in ("real", "synthetic")
        }
        for set_name in ("gold", "silver")
    }
    if by_set_source != {
        "gold": {"real": 3, "synthetic": 10},
        "silver": {"real": 0, "synthetic": 43},
    }:
        raise M6InputError(
            f"answer_match_denominator source counts changed: {by_set_source}"
        )

    return {
        "schema_version": 1,
        "generated_at": generated_at or now_iso(),
        "status": "ok",
        "inputs": {
            "gold_sha256": gold_sha256,
            "silver_sha256": silver_sha256,
            "m5_config_hash": m5_config_hash,
        },
        "dev_set": _id_section(dev, by_set=True),
        "h5_pool": {
            **_id_section(h5, by_set=False),
            "by_type": dict(sorted(Counter(question.type for question in h5).items())),
        },
        "answer_match_denominator": {
            **_id_section(denominator, by_set=True),
            "by_source": dict(
                sorted(Counter(question.source for question in denominator).items())
            ),
            "by_set_source": by_set_source,
            "excluded": sorted(excluded, key=lambda item: item["id"]),
        },
        "gate_permission_dev": _id_section(permission, by_set=True),
        "unanswerable_dev": _id_section(unanswerable, by_set=True),
        "router_types": {
            "minimum_count": 8,
            "eligible": eligible,
            "below_threshold": below,
        },
    }


def load_m6_questions(
    root: Path = Path("."), *, split: str = "dev", set_name: str | None = None
) -> list[Question]:
    """Load M6 questions in stable gold-then-silver file order."""
    questions = [
        *load_questions(root / GOLD_PATH),
        *load_questions(root / SILVER_PATH),
    ]
    return [
        question
        for question in questions
        if question.split == split and (set_name is None or question.set == set_name)
    ]


def write_input_manifest(root: Path = Path(".")) -> dict[str, object]:
    """Validate local frozen inputs and atomically write the M6 manifest."""
    gold_path = root / GOLD_PATH
    silver_path = root / SILVER_PATH
    m5_path = root / M5_CONFIG_PATH
    m5 = _json_object(m5_path)
    payload = build_input_manifest(
        load_questions(gold_path),
        load_questions(silver_path),
        gold_sha256=sha256_file(gold_path),
        silver_sha256=sha256_file(silver_path),
        m5_config_hash=_required_string(m5.get("config_hash"), "m5 config_hash"),
    )
    destination = root / M6_INPUTS_PATH
    _write_json_atomic(destination, payload)
    return payload


def require_input_manifest(root: Path = Path(".")) -> Mapping[str, object]:
    """Validate M6 IDs/counts against the current frozen input files."""
    payload = _json_object(root / M6_INPUTS_PATH)
    if payload.get("status") != "ok":
        raise M6InputError("m6_eval_inputs status is not ok")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise M6InputError("m6_eval_inputs inputs object is missing")
    expected_hashes = {
        "gold_sha256": sha256_file(root / GOLD_PATH),
        "silver_sha256": sha256_file(root / SILVER_PATH),
        "m5_config_hash": M5_CONFIG_HASH,
    }
    for name, expected in expected_hashes.items():
        if inputs.get(name) != expected:
            raise M6InputError(f"m6_eval_inputs {name} mismatch")
    for section, expected in (
        ("dev_set", EXPECTED_DEV_COUNT),
        ("h5_pool", EXPECTED_H5_COUNT),
        ("answer_match_denominator", EXPECTED_ANSWER_DENOMINATOR),
        ("gate_permission_dev", EXPECTED_PERMISSION_DEV),
        ("unanswerable_dev", EXPECTED_UNANSWERABLE_DEV),
    ):
        value = payload.get(section)
        if not isinstance(value, Mapping) or value.get("count") != expected:
            raise M6InputError(f"m6_eval_inputs {section} count mismatch")
    return payload


def _id_section(questions: Sequence[Question], *, by_set: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "count": len(questions),
        "ids": sorted(question.id for question in questions),
    }
    if by_set:
        result["by_set"] = dict(
            sorted(Counter(question.set for question in questions).items())
        )
    return result


def _require_unique_ids(questions: Sequence[Question]) -> None:
    counts = Counter(question.id for question in questions)
    duplicates = sorted(qid for qid, count in counts.items() if count != 1)
    if duplicates:
        raise M6InputError(f"duplicate question IDs: {', '.join(duplicates)}")


def _require_count(label: str, values: Sequence[object], expected: int) -> None:
    if len(values) != expected:
        raise M6InputError(f"{label} count must be {expected}, got {len(values)}")


def _json_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON object required: {path}")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise M6InputError(f"{label} must be a non-empty string")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 평가 입력 manifest를 만듭니다.")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        payload = write_input_manifest(args.root)
    except Exception as exc:  # noqa: BLE001 - bounded CLI report
        print(f"M6 입력 집계 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        "M6 입력 집계 완료: "
        f"dev={payload['dev_set']['count']} "  # type: ignore[index]
        f"h5={payload['h5_pool']['count']} "  # type: ignore[index]
        f"denominator={payload['answer_match_denominator']['count']}"  # type: ignore[index]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
