"""Compute deterministic quality metrics for validated synthetic documents."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from kodocs.synth.identifiers import TICKET_RE
from kodocs.synth.injection_texts import INJECTION_TEXTS
from kodocs.synth.seed import load_seed

METRICS_PATH = Path("data/manifest/synth_metrics.json")
SHINGLE_N = 5
DUP_THRESHOLD = 0.5

_DOCS_PATH = Path("data/synthetic/docs")
_PLAN_PATH = Path("data/synthetic/plan.jsonl")
_VALIDATION_PATH = Path("data/manifest/synth_validation.json")
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)|\n+")
_FORMAL_RE = re.compile(r"니다$")
_PLAIN_RE = re.compile(r"다$")
_NOMINAL_RE = re.compile(
    r"(?:함|음|임|됨|완료|예정|필요|확인|대상|상태|결과|계획|진행|종료|발생|성공|실패)$"
)
_STYLE_CATEGORIES = ("formal", "plain", "nominal", "other")
# A fragment that is only a list or number marker ("1.", "가.", "①") is not a
# sentence; pilot 4's top repeated "sentences" were all such markers.
_LIST_MARKER_RE = re.compile(r"^(?:\d+|[가-힣]|[①-⑳]|[-*+•])[.)]?$")
# Vague relative time the prompt forbids unless given (rule 6); a warning only.
RELATIVE_TIME_TERMS = ("최근", "얼마 전", "지난달", "요즘", "요새", "근래")
# Known issues from the human review (2026-09-28), counted as warnings only:
# (pattern, doc types it applies to, or None for every type).
_STYLE_WARNING_PATTERNS: dict[str, tuple[re.Pattern[str], frozenset[str] | None]] = {
    # "…했다.는": seed narrative facts are full sentences ending in a period.
    "sentence_seam": (re.compile(r"다\.(?=[가-힣])"), None),
    # "권도온 / 권도온 팀장": the " / " joined allowed-forms list leaked.
    "surface_form_list_leak": (re.compile(r"(\S+(?: \S+)?) / \1"), None),
    # A ticket number, not the report's own INC number, labelled 문서 번호.
    "incident_ticket_as_doc_number": (
        re.compile(r"문서 번호\s*[:：]?\s*\**\s*" + TICKET_RE.pattern),
        frozenset({"incident"}),
    ),
    # Narrative requests ("~주시기 바랍니다", "~하기 바란다").
    "narrative_request_phrases": (
        re.compile(r"바란다|바랍니다|부탁드립니다|주십시오|주세요"),
        None,
    ),
    # Bullet-style request endings usual in real memos ("문의 바람", "~할 것").
    "bullet_request_endings": (re.compile(r"(?:바람|할 것)(?=[.\n]|$)"), None),
}


def normalize_for_shingles(text: str) -> str:
    """Normalize body text and omit its title line and fixed injection text."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _WHITESPACE_RE.sub(" ", _analysis_text(text)).strip()


def shingles(text: str, n: int = SHINGLE_N) -> frozenset[str]:
    """Return exact character n-grams without padding."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("n must be a positive integer")
    if len(text) < n:
        return frozenset()
    return frozenset(text[index : index + n] for index in range(len(text) - n + 1))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Return Jaccard similarity, defining two empty sets as identical."""
    left, right = frozenset(a), frozenset(b)
    union = left | right
    return 1.0 if not union else len(left & right) / len(union)


def compute(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Compute document-only metrics, treating every supplied document as passed."""
    normal_fact_ids = {
        fact_id
        for doc in docs
        for fact_id in _fact_ids(doc)
        if not fact_id.startswith("f-canary-")
    }
    plans = [_plan_from_doc(doc) for doc in docs]
    return _compute(
        docs,
        plans=plans,
        normal_fact_ids=normal_fact_ids,
        validation={
            "planned": len(docs),
            "generated": len(docs),
            "passed": len(docs),
            "failed": 0,
            "not_generated": 0,
            "exhausted": 0,
            "by_code": {},
        },
    )


def run(root: Path) -> int:
    """Read validated outputs and write the deterministic committed manifest."""
    root = Path(root).resolve()
    validation = _load_json(root / _VALIDATION_PATH)
    passed = _non_negative_int(validation.get("passed"), default=0)
    if passed == 0:
        print("검증 통과 문서가 없어 품질 지표를 만들지 않습니다.", file=sys.stderr)
        return 1

    plans = _load_jsonl(root / _PLAN_PATH)
    plan_by_id = {
        str(plan["doc_id"]): plan
        for plan in plans
        if isinstance(plan.get("doc_id"), str)
    }
    docs: list[dict[str, object]] = []
    docs_dir = root / _DOCS_PATH
    for path in sorted(docs_dir.glob("*.json")):
        document = _load_json(path)
        doc_id = document.get("doc_id")
        if not isinstance(doc_id, str) or doc_id not in plan_by_id:
            continue
        plan = plan_by_id[doc_id]
        style = plan.get("style")
        if isinstance(style, Mapping):
            target = style.get("length_target_chars")
            if _is_number(target) and float(target) > 0:
                document["_length_target_chars"] = int(float(target))
        phrases = plan.get("fact_time_phrases")
        if isinstance(phrases, Mapping):
            document["_fact_time_phrases"] = dict(phrases)
        docs.append(document)

    if not docs or len(docs) != passed:
        print(
            f"통과 수({passed})와 검증 문서 수({len(docs)})가 일치하지 않습니다.",
            file=sys.stderr,
        )
        return 1

    seed = load_seed(root)
    normal_fact_ids = {
        fact_id for fact_id, fact in seed.facts.items() if fact.sensitivity == "normal"
    }
    metrics = _compute(
        docs,
        plans=plans,
        normal_fact_ids=normal_fact_ids,
        validation=validation,
    )
    _atomic_write_json(root / METRICS_PATH, metrics)
    print(root / METRICS_PATH)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for offline synthetic-corpus quality metrics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return run(args.root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"합성 문서 품질 지표 생성 실패: {exc}", file=sys.stderr)
        return 1


def _compute(
    docs: Sequence[Mapping[str, object]],
    *,
    plans: Sequence[Mapping[str, object]],
    normal_fact_ids: set[str],
    validation: Mapping[str, object],
) -> dict[str, object]:
    ordered_docs = sorted(docs, key=lambda doc: _required_string(doc, "doc_id"))
    plan_counts = _planned_dimensions(plans)
    payload: dict[str, object] = {
        "schema_version": 1,
        "scope": {"passed_documents": len(ordered_docs)},
        "duplicate": _duplicate_metrics(ordered_docs),
        "repeated_sentences": _repeated_sentence_metrics(ordered_docs),
        "length": _length_metrics(ordered_docs),
        "lexical_diversity": _lexical_metrics(ordered_docs),
        "style": _style_metrics(ordered_docs),
        "distribution": _distribution_metrics(ordered_docs, plan_counts),
        "fact_coverage": _fact_coverage(ordered_docs, normal_fact_ids),
        "unprompted_relative_time": _unprompted_relative_time(ordered_docs),
        "style_warnings": _style_warnings(ordered_docs),
        "validation": _validation_metrics(validation),
        "provenance": _provenance(ordered_docs),
    }
    return _round_tree(payload)  # type: ignore[return-value]


def _style_warnings(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Per known issue: documents, occurrences, and per-type document counts."""
    output: dict[str, object] = {}
    for name, (pattern, doc_types) in _STYLE_WARNING_PATTERNS.items():
        scoped = [
            doc
            for doc in docs
            if doc_types is None or _required_string(doc, "doc_type") in doc_types
        ]
        by_type: dict[str, dict[str, int]] = {}
        documents = occurrences = 0
        for doc in scoped:
            count = len(pattern.findall(_analysis_text(_required_string(doc, "body"))))
            stats = by_type.setdefault(
                _required_string(doc, "doc_type"), {"documents": 0, "population": 0}
            )
            stats["population"] += 1
            stats["documents"] += int(count > 0)
            documents += int(count > 0)
            occurrences += count
        output[name] = {
            "documents": documents,
            "occurrences": occurrences,
            "population": len(scoped),
            "rate": documents / len(scoped) if scoped else None,
            "by_type": dict(sorted(by_type.items())),
        }
    return output


def _unprompted_relative_time(
    docs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Per document, vague relative-time terms absent from its given phrases."""
    by_doc: dict[str, dict[str, int]] = {}
    for doc in docs:
        phrases = doc.get("_fact_time_phrases")
        given = (
            [str(value) for value in phrases.values()]
            if isinstance(phrases, Mapping)
            else []
        )
        body = _analysis_text(_required_string(doc, "body"))
        counts = {
            term: count
            for term in RELATIVE_TIME_TERMS
            if not any(term in phrase for phrase in given)
            and (count := body.count(term))
        }
        if counts:
            by_doc[_required_string(doc, "doc_id")] = counts
    return {
        "terms": list(RELATIVE_TIME_TERMS),
        "documents_with_warning": len(by_doc),
        "total": sum(sum(counts.values()) for counts in by_doc.values()),
        "by_doc": by_doc,
    }


def _duplicate_metrics(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    prepared = {
        _required_string(doc, "doc_id"): shingles(
            normalize_for_shingles(_required_string(doc, "body"))
        )
        for doc in docs
    }
    pairs: list[dict[str, object]] = []
    for left_index, left in enumerate(docs):
        left_id = _required_string(left, "doc_id")
        left_type = _required_string(left, "doc_type")
        for right in docs[left_index + 1 :]:
            right_id = _required_string(right, "doc_id")
            right_type = _required_string(right, "doc_type")
            pairs.append(
                {
                    "doc_id_a": left_id,
                    "doc_id_b": right_id,
                    "doc_type_a": left_type,
                    "doc_type_b": right_type,
                    "jaccard": jaccard(prepared[left_id], prepared[right_id]),
                    "same_type": left_type == right_type,
                }
            )
    overall = [float(pair["jaccard"]) for pair in pairs]
    within = [float(pair["jaccard"]) for pair in pairs if pair["same_type"] is True]
    between = [float(pair["jaccard"]) for pair in pairs if pair["same_type"] is False]
    near = sorted(
        (pair for pair in pairs if float(pair["jaccard"]) >= DUP_THRESHOLD),
        key=lambda pair: (
            -float(pair["jaccard"]),
            str(pair["doc_id_a"]),
            str(pair["doc_id_b"]),
        ),
    )[:20]
    return {
        "shingle_n": SHINGLE_N,
        "threshold": DUP_THRESHOLD,
        "pair_count": len(pairs),
        "overall": _similarity_summary(overall),
        "within_type": _similarity_summary(within),
        "between_type": _similarity_summary(between),
        "near_duplicates": [
            {name: value for name, value in pair.items() if name != "same_type"}
            for pair in near
        ],
    }


def _similarity_summary(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"max": None, "p95": None, "mean": None}
    return {
        "max": max(values),
        "p95": _percentile(values, 0.95),
        "mean": math.fsum(values) / len(values),
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _repeated_sentence_metrics(
    docs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    occurrences: Counter[str] = Counter()
    for doc in docs:
        occurrences.update(
            sentence
            for sentence in set(
                _sentences(_required_string(doc, "body"), skip_headings=True)
            )
            if not _LIST_MARKER_RE.fullmatch(sentence)
        )
    repeated = [
        (sentence, count) for sentence, count in occurrences.items() if count >= 3
    ]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    return {
        "sentences_in_3_plus_docs": len(repeated),
        "top": [
            {"sentence_prefix": sentence[:40], "document_count": count}
            for sentence, count in repeated[:10]
        ],
    }


def _length_metrics(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    grouped = _group_by_type(docs)
    by_type: dict[str, object] = {}
    for doc_type, selected in grouped.items():
        lengths = [len(_required_string(doc, "body")) for doc in selected]
        targets = [
            float(target)
            for doc in selected
            if _is_number(target := doc.get("_length_target_chars"))
            and float(target) > 0
        ]
        mean = math.fsum(lengths) / len(lengths)
        stddev = statistics.pstdev(lengths)
        target_mean = math.fsum(targets) / len(targets) if targets else None
        by_type[doc_type] = {
            "documents": len(selected),
            "mean_chars": mean,
            "stddev_chars": stddev,
            "coefficient_of_variation": stddev / mean if mean else None,
            "min_chars": min(lengths),
            "max_chars": max(lengths),
            "target_mean_chars": target_mean,
            "target_ratio": mean / target_mean if target_mean else None,
        }
    return {"by_type": by_type}


def _lexical_metrics(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    by_type: dict[str, object] = {}
    for doc_type, selected in _group_by_type(docs).items():
        all_bigrams: list[str] = []
        for doc in selected:
            normalized = normalize_for_shingles(_required_string(doc, "body"))
            all_bigrams.extend(
                normalized[index : index + 2]
                for index in range(max(0, len(normalized) - 1))
            )
        unique = len(set(all_bigrams))
        total = len(all_bigrams)
        by_type[doc_type] = {
            "distinct_2": unique / total if total else 0.0,
            "unique_bigrams": unique,
            "total_bigrams": total,
        }
    return {"by_type": by_type}


def _style_metrics(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    overall: Counter[str] = Counter()
    by_type: dict[str, object] = {}
    for doc_type, selected in _group_by_type(docs).items():
        counts: Counter[str] = Counter()
        for doc in selected:
            counts.update(
                _sentence_style(sentence)
                for sentence in _sentences(_required_string(doc, "body"))
            )
        overall.update(counts)
        by_type[doc_type] = _style_summary(counts)
    return {"overall": _style_summary(overall), "by_type": by_type}


def _style_summary(counts: Mapping[str, int]) -> dict[str, object]:
    normalized_counts = {
        category: int(counts.get(category, 0)) for category in _STYLE_CATEGORIES
    }
    total = sum(normalized_counts.values())
    return {
        "sentences": total,
        "counts": normalized_counts,
        "ratios": {
            category: normalized_counts[category] / total if total else 0.0
            for category in _STYLE_CATEGORIES
        },
    }


def _sentence_style(sentence: str) -> str:
    ending = re.sub(r"[.!?。]+$", "", sentence.strip())
    if _FORMAL_RE.search(ending):
        return "formal"
    if _PLAIN_RE.search(ending):
        return "plain"
    if _NOMINAL_RE.search(ending):
        return "nominal"
    return "other"


def _distribution_metrics(
    docs: Sequence[Mapping[str, object]],
    plan_counts: Mapping[str, Mapping[str, int]],
) -> dict[str, object]:
    passed = {
        "by_type": Counter(_required_string(doc, "doc_type") for doc in docs),
        "by_access": Counter(_access_level(doc) for doc in docs),
        "by_team": Counter(_required_string(doc, "team") for doc in docs),
    }
    output: dict[str, object] = {}
    for dimension in ("by_type", "by_access", "by_team"):
        categories = sorted(
            set(plan_counts.get(dimension, {})) | set(passed[dimension])
        )
        output[dimension] = {
            category: {
                "planned": int(plan_counts.get(dimension, {}).get(category, 0)),
                "passed": int(passed[dimension][category]),
            }
            for category in categories
        }
    output["canary_documents"] = sum(doc.get("canary") is not None for doc in docs)
    output["injection_documents"] = sum(
        doc.get("injection") is not None for doc in docs
    )
    return output


def _planned_dimensions(
    plans: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, int]]:
    return {
        "by_type": Counter(
            str(plan.get("doc_type"))
            for plan in plans
            if isinstance(plan.get("doc_type"), str)
        ),
        "by_access": Counter(
            str(access.get("level"))
            for plan in plans
            if isinstance((access := plan.get("access")), Mapping)
            and isinstance(access.get("level"), str)
        ),
        "by_team": Counter(
            str(plan.get("team")) for plan in plans if isinstance(plan.get("team"), str)
        ),
    }


def _fact_coverage(
    docs: Sequence[Mapping[str, object]], normal_fact_ids: set[str]
) -> dict[str, object]:
    appearances: Counter[str] = Counter()
    for doc in docs:
        appearances.update(set(_fact_ids(doc)) & normal_fact_ids)
    distribution = Counter(appearances.get(fact_id, 0) for fact_id in normal_fact_ids)
    covered = sum(count > 0 for count in appearances.values())
    total = len(normal_fact_ids)
    return {
        "normal_facts_total": total,
        "covered_facts": covered,
        "coverage_rate": covered / total if total else 0.0,
        "documents_per_fact_distribution": {
            str(count): amount for count, amount in sorted(distribution.items())
        },
        "facts_in_2_plus_documents": sum(
            appearances.get(fact_id, 0) >= 2 for fact_id in normal_fact_ids
        ),
    }


def _validation_metrics(validation: Mapping[str, object]) -> dict[str, object]:
    generated = _non_negative_int(validation.get("generated"), default=0)
    passed = _non_negative_int(validation.get("passed"), default=0)
    by_code = validation.get("by_code")
    return {
        "planned": _non_negative_int(validation.get("planned"), default=0),
        "generated": generated,
        "passed": passed,
        "failed": _non_negative_int(validation.get("failed"), default=0),
        "not_generated": _non_negative_int(validation.get("not_generated"), default=0),
        "exhausted": _non_negative_int(validation.get("exhausted"), default=0),
        "pass_rate": passed / generated if generated else None,
        "failure_codes": (
            {str(key): int(value) for key, value in sorted(by_code.items())}
            if isinstance(by_code, Mapping)
            else {}
        ),
    }


def _provenance(docs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    models: set[str] = set()
    template_hashes: set[str] = set()
    template_hash_counts: dict[str, Counter[str]] = defaultdict(Counter)
    validator_versions: set[object] = set()
    regulation_hashes: set[str] = set()
    seed_hashes: dict[str, str] = {}
    generations: set[str] = set()
    for doc in docs:
        generation = doc.get("generation")
        if isinstance(generation, Mapping):
            if isinstance(generation.get("model"), str):
                models.add(str(generation["model"]))
            if isinstance(generation.get("template_hash"), str):
                template_hashes.add(str(generation["template_hash"]))
                template_hash_counts[_required_string(doc, "doc_type")][
                    str(generation["template_hash"])
                ] += 1
            settings = generation.get("generation")
            if isinstance(settings, Mapping):
                generations.add(
                    json.dumps(
                        dict(settings),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        validated = doc.get("validated_with")
        if isinstance(validated, Mapping):
            version = validated.get("validator_version")
            if isinstance(version, (str, int)) and not isinstance(version, bool):
                validator_versions.add(version)
            regulation_hash = validated.get("regulation_index_sha256")
            if isinstance(regulation_hash, str) and regulation_hash:
                regulation_hashes.add(regulation_hash)
            hashes = validated.get("seed_files_sha256")
            if isinstance(hashes, Mapping):
                for name, value in hashes.items():
                    if isinstance(name, str) and isinstance(value, str):
                        seed_hashes[name] = value
    return {
        "models": sorted(models),
        "generation_records": [json.loads(value) for value in sorted(generations)],
        "template_hashes": sorted(template_hashes),
        "template_hash_counts": {
            doc_type: dict(sorted(counts.items()))
            for doc_type, counts in sorted(template_hash_counts.items())
        },
        "seed_files_sha256": dict(sorted(seed_hashes.items())),
        "regulation_index_sha256": sorted(regulation_hashes),
        "validator_versions": sorted(validator_versions, key=str),
    }


def _analysis_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    lines = normalized.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            del lines[index]
            break
    without_title = "\n".join(lines)
    for injection in INJECTION_TEXTS.values():
        without_title = without_title.replace(injection, " ")
    return without_title


def _sentences(text: str, *, skip_headings: bool = False) -> list[str]:
    analysis = _analysis_text(text)
    if skip_headings:
        # Markdown headings ("## 목적") are section labels, not sentences.
        analysis = "\n".join(
            line for line in analysis.splitlines() if not line.lstrip().startswith("#")
        )
    return [
        normalized
        for part in _SENTENCE_SPLIT_RE.split(analysis)
        if (normalized := _normalize_sentence(part))
    ]


def _normalize_sentence(value: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", value).strip()
    normalized = re.sub(r"^(?:[-*+]\s+|>\s*|#{1,6}\s*)", "", normalized)
    return normalized.strip()


def _group_by_type(
    docs: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for doc in docs:
        grouped[_required_string(doc, "doc_type")].append(doc)
    return {key: grouped[key] for key in sorted(grouped)}


def _fact_ids(doc: Mapping[str, object]) -> list[str]:
    spans = doc.get("fact_spans")
    if not isinstance(spans, list):
        return []
    return [
        str(span["fact_id"])
        for span in spans
        if isinstance(span, Mapping) and isinstance(span.get("fact_id"), str)
    ]


def _plan_from_doc(doc: Mapping[str, object]) -> dict[str, object]:
    return {
        "doc_id": doc.get("doc_id"),
        "doc_type": doc.get("doc_type"),
        "team": doc.get("team"),
        "access": doc.get("access"),
    }


def _access_level(doc: Mapping[str, object]) -> str:
    access = doc.get("access")
    if not isinstance(access, Mapping):
        raise TypeError("document access must be an object")
    return _required_string(access, "level")


def _required_string(value: Mapping[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise ValueError(f"{key} must be a non-empty string")
    return field


def _non_negative_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("count must be a non-negative integer")
    return value


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _round_tree(value: object) -> object:
    if isinstance(value, float):
        rounded = round(value, 4)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {key: _round_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_tree(item) for item in value]
    return value


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 객체가 아닙니다: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"JSONL 객체가 아닙니다: {path}:{line_number}")
            records.append(value)
    return records


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
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
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":  # pragma: no cover - CLI acceptance check
    raise SystemExit(main())
