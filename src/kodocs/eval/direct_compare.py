"""Build the paired Upstage PDF-vs-original-input comparison from manifests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from kodocs.parsing.upstage_adapter import SERVICE

_PARSERS = ("upstage", "upstage-direct")
_TABLE_RESULT_STATUSES = frozenset({"ok", "no_tables", "all_invalid"})


def compare(root: Path) -> dict[str, object] | None:
    """Return paired metrics, or ``None`` until both experiment arms exist."""
    scores = _load_json(root / "data/manifest/parse_scores.json")
    parsers = _mapping(scores.get("parsers")) if scores is not None else None
    documents = {
        parser: _ok_documents(_mapping(parsers.get(parser)) if parsers else None)
        for parser in _PARSERS
    }
    paired_keys = sorted(set(documents["upstage"]) & set(documents["upstage-direct"]))
    if not paired_keys:
        return None

    table_scores = _load_json(root / "data/manifest/table_scores.json")
    paired_tables = _paired_table_scores(table_scores)
    runs = _load_jsonl(root / "data/manifest/parse_runs.jsonl")
    costs = _load_jsonl(root / "data/manifest/costs.jsonl")

    result: dict[str, object] = {
        "paired_targets": len(paired_keys),
        "paired_tables": len(paired_tables),
    }
    for parser in _PARSERS:
        selected = [documents[parser][key] for key in paired_keys]
        refs = {
            value
            for document in selected
            if isinstance((value := document.get("input_sha256")), str) and value
        }
        billed_pages = _billed_pages(costs, refs)
        elapsed_s = _elapsed_seconds(runs, parser=parser, refs=refs)
        result[parser] = {
            "structure_exact_rate": _mean(
                [
                    1.0 if document.get("structure_exact") is True else 0.0
                    for document in selected
                ]
            ),
            "key_f1_mean": _mean([_document_f1(document) for document in selected]),
            "text_sim_raw_mean": _mean(
                [_number(document.get("text_sim_raw_mean")) for document in selected]
            ),
            "text_sim_norm_mean": _mean(
                [_number(document.get("text_sim_norm_mean")) for document in selected]
            ),
            "table_cell_exact_rate": _mean([pair[parser] for pair in paired_tables]),
            "billed_pages": billed_pages,
            "seconds_per_page": (
                elapsed_s / billed_pages
                if billed_pages is not None and billed_pages
                else None
            ),
        }
    return result


def _ok_documents(
    payload: Mapping[str, object] | None,
) -> dict[str, Mapping[str, object]]:
    values = payload.get("documents") if payload else None
    documents: dict[str, Mapping[str, object]] = {}
    if not isinstance(values, list):
        return documents
    for value in values:
        document = _mapping(value)
        if document is None or document.get("status") != "ok":
            continue
        target_key = document.get("target_key")
        if isinstance(target_key, str) and target_key:
            documents[target_key] = document
    return documents


def _paired_table_scores(
    payload: Mapping[str, object] | None,
) -> list[dict[str, float]]:
    values = payload.get("scores") if payload else None
    by_golden: dict[str, dict[str, float]] = {}
    if not isinstance(values, list):
        return []
    for value in values:
        score = _mapping(value)
        if score is None:
            continue
        parser = score.get("parser")
        golden_id = score.get("golden_id")
        exact = score.get("cell_exact_rate")
        if (
            parser in _PARSERS
            and isinstance(golden_id, str)
            and score.get("status") in _TABLE_RESULT_STATUSES
            and _is_number(exact)
        ):
            by_golden.setdefault(golden_id, {})[str(parser)] = float(exact)
    return [
        values_by_parser
        for golden_id, values_by_parser in sorted(by_golden.items())
        if all(parser in values_by_parser for parser in _PARSERS)
    ]


def _billed_pages(
    costs: Sequence[Mapping[str, object]], refs: set[str]
) -> float | None:
    if not refs:
        return None
    quantities = [
        float(quantity)
        for entry in costs
        if entry.get("service") in {None, SERVICE}
        and entry.get("ref") in refs
        and _is_number(quantity := entry.get("quantity"))
    ]
    return math.fsum(quantities) if quantities else None


def _elapsed_seconds(
    runs: Sequence[Mapping[str, object]], *, parser: str, refs: set[str]
) -> float:
    latest: dict[str, float] = {}
    for record in runs:
        ref = record.get("input_sha256")
        elapsed = record.get("elapsed_s")
        if (
            record.get("parser") == parser
            and record.get("status") in {"ok", "empty"}
            and isinstance(ref, str)
            and ref in refs
            and _is_number(elapsed)
        ):
            latest[ref] = float(elapsed)
    return math.fsum(latest.values())


def _document_f1(document: Mapping[str, object]) -> float:
    precision = _number(document.get("key_precision"))
    recall = _number(document.get("key_recall"))
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _number(value: object) -> float:
    return float(value) if _is_number(value) else 0.0


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _load_json(path: Path) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return _mapping(value)


def _load_jsonl(path: Path) -> list[Mapping[str, object]]:
    if not path.exists():
        return []
    values: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and (value := _mapping(json.loads(line))) is not None:
            values.append(value)
    return values


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None
