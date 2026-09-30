"""Prepare immutable, authorization-aware inputs for M5 retrieval evaluation.

This module only reads already-produced local artifacts.  It neither calls an
external service nor changes the reviewed files under ``data/golden``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from kodocs.chunking.corpus import (
    CorpusDoc,
    CorpusLoadError,
    ParserCondition,
    load_corpus,
)
from kodocs.eval.golden import Qrel, Question, can_read, load_questions
from kodocs.eval.m5_metrics import Span, doc_relevance
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.parsing import annex_mapping

RETRIEVAL_TYPES = ("identifier", "semantic", "table", "multi_doc", "temporal")

_QUERY_SETS = ("gold", "silver")
_SPLITS = ("dev", "test")
_H7_TYPES = frozenset({"identifier", "semantic", "multi_doc"})
_MANIFEST_PATH = Path("data/manifest/m5_eval_inputs.json")


@dataclass(frozen=True)
class EvalQuestion:
    """One retrieval question with its asker-visible ground truth."""

    question: Question
    rel_by_doc: dict[str, int]
    n_relevant_total: int
    n_relevant_visible: int
    answer_ranges: tuple[Span, ...]
    repeated: bool


@dataclass(frozen=True)
class VisibilityFailure:
    """A failed prerequisite for authorization-aware retrieval evaluation."""

    question_id: str
    doc_id: str | None
    reason: str


@dataclass(frozen=True)
class ExcludedPair:
    """One inaccessible document intentionally excluded from the truth set."""

    question_id: str
    doc_id: str
    rel: int


@dataclass(frozen=True)
class VisibilityReport:
    """Complete visibility audit, including non-failing excluded pairs."""

    checked_question_count: int
    failures: tuple[VisibilityFailure, ...]
    excluded_pairs: tuple[ExcludedPair, ...]
    per_question: dict[str, dict[str, int]]

    @property
    def excluded_pair_count(self) -> int:
        return len(self.excluded_pairs)

    @property
    def excluded_question_count(self) -> int:
        return len({pair.question_id for pair in self.excluded_pairs})


@dataclass(frozen=True)
class AnswerRangeFailure:
    """One real or malformed synthetic qrel that could not be mapped."""

    question_id: str
    doc_id: str
    reference: str | None
    reason: str


@dataclass(frozen=True)
class _RepeatedDetails:
    repeated: bool
    evidence_sha256: tuple[str, ...]
    n_matching_docs: int
    evidence_count: int
    short_evidence_count: int


class EvalInputError(RuntimeError):
    """Local evaluation inputs violate an M5 prerequisite."""


def load_retrieval_targets(root: Path = Path(".")) -> dict[str, list[Question]]:
    """Load unique answer-bearing retrieval questions, partitioned by set/split."""
    result = {
        f"{set_name}_{split}": [] for set_name in _QUERY_SETS for split in _SPLITS
    }
    for set_name in _QUERY_SETS:
        questions = load_questions(root / f"data/golden/queries/{set_name}.jsonl")
        seen: set[str] = set()
        for question in questions:
            if question.id in seen:
                continue
            seen.add(question.id)
            if (
                question.type not in RETRIEVAL_TYPES
                or not any(qrel.rel >= 1 for qrel in question.qrels)
                or question.split not in _SPLITS
            ):
                continue
            result[f"{set_name}_{question.split}"].append(question)
    return result


def qrel_answer_ranges(
    q: Question,
    docs_by_id: Mapping[str, CorpusDoc],
    *,
    table_golden: Mapping,
) -> list[Span]:
    """Map asker-visible positive qrels to composite-corpus character ranges."""
    ranges, failures = _map_answer_ranges(q, docs_by_id, table_golden=table_golden)
    if failures:
        details = ", ".join(
            f"{failure.question_id}:{failure.doc_id}:{failure.reason}"
            for failure in failures
        )
        raise EvalInputError(f"answer range mapping failed: {details}")
    return ranges


def answer_repeated(q: Question, docs: Sequence[CorpusDoc]) -> tuple[bool, list[str]]:
    """Return the preregistered repetition tag and normalized-evidence hashes."""
    table_golden = _load_table_golden(Path("."), missing_ok=True)
    details = _RepeatedAnalyzer(docs, table_golden=table_golden).analyze(q)
    return details.repeated, list(details.evidence_sha256)


def check_asker_visibility(
    qs: Sequence[Question], docs_by_id: Mapping[str, CorpusDoc]
) -> list[VisibilityFailure]:
    """Return only missing-document and zero-visible-truth prerequisite failures."""
    return list(visibility_report(qs, docs_by_id).failures)


def visibility_report(
    qs: Sequence[Question], docs_by_id: Mapping[str, CorpusDoc]
) -> VisibilityReport:
    """Audit visible truth documents and record every inaccessible pair."""
    failures: list[VisibilityFailure] = []
    excluded: list[ExcludedPair] = []
    per_question: dict[str, dict[str, int]] = {}
    for question in qs:
        relevance = _positive_doc_relevance(question)
        visible = 0
        for doc_id, grade in relevance.items():
            document = docs_by_id.get(doc_id)
            if document is None:
                failures.append(VisibilityFailure(question.id, doc_id, "missing_doc"))
            elif _can_read_document(question, document):
                visible += 1
            else:
                excluded.append(ExcludedPair(question.id, doc_id, grade))
        if visible == 0:
            failures.append(VisibilityFailure(question.id, None, "no_visible_relevant"))
        per_question[question.id] = {
            "total": len(relevance),
            "visible": visible,
        }
    return VisibilityReport(
        checked_question_count=len(qs),
        failures=tuple(failures),
        excluded_pairs=tuple(excluded),
        per_question=per_question,
    )


def build_eval_questions(
    qs: Sequence[Question],
    docs_by_id: Mapping[str, CorpusDoc],
    *,
    table_golden: Mapping,
) -> list[EvalQuestion]:
    """Build metric-ready questions whose relevance mapping is visibility-safe."""
    audit = visibility_report(qs, docs_by_id)
    if audit.failures:
        raise EvalInputError(
            "visibility prerequisites failed: "
            + ", ".join(
                f"{failure.question_id}:{failure.doc_id}:{failure.reason}"
                for failure in audit.failures
            )
        )
    analyzer = _RepeatedAnalyzer(tuple(docs_by_id.values()), table_golden=table_golden)
    prepared: list[EvalQuestion] = []
    for question in qs:
        all_relevance = _positive_doc_relevance(question)
        visible_relevance = {
            doc_id: grade
            for doc_id, grade in all_relevance.items()
            if _can_read_document(question, docs_by_id[doc_id])
        }
        answer_ranges = qrel_answer_ranges(
            question, docs_by_id, table_golden=table_golden
        )
        repeated = analyzer.analyze(question).repeated
        prepared.append(
            EvalQuestion(
                question=question,
                rel_by_doc=visible_relevance,
                n_relevant_total=len(all_relevance),
                n_relevant_visible=len(visible_relevance),
                answer_ranges=tuple(answer_ranges),
                repeated=repeated,
            )
        )
    return prepared


def answer_ranges_by_doc(
    condition: ParserCondition, root: Path = Path(".")
) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """Return positive, visible qrel ranges for index truncation auditing."""
    documents, _report = load_corpus(condition, root=root)
    docs_by_id = {document.doc_id: document for document in documents}
    targets = load_retrieval_targets(root)
    questions = [question for group in targets.values() for question in group]
    audit = visibility_report(questions, docs_by_id)
    if audit.failures:
        raise EvalInputError(
            "visibility prerequisites failed while loading answer ranges"
        )
    table_golden = _load_table_golden(root)
    result: dict[str, dict[str, list[tuple[int, int]]]] = {
        "gold": {},
        "silver": {},
    }
    mapping_failures: list[AnswerRangeFailure] = []
    for set_name in _QUERY_SETS:
        collected: dict[str, set[tuple[int, int]]] = {}
        for split in _SPLITS:
            for question in targets[f"{set_name}_{split}"]:
                ranges, failures = _map_answer_ranges(
                    question, docs_by_id, table_golden=table_golden
                )
                mapping_failures.extend(failures)
                for span in ranges:
                    collected.setdefault(span.doc_id, set()).add((span.start, span.end))
        result[set_name] = {
            doc_id: sorted(ranges) for doc_id, ranges in sorted(collected.items())
        }
    if mapping_failures:
        raise EvalInputError(
            f"answer range mapping failed for {len(mapping_failures)} qrels"
        )
    return result


class _RepeatedAnalyzer:
    def __init__(self, docs: Sequence[CorpusDoc], *, table_golden: Mapping) -> None:
        self.docs = tuple(docs)
        self.docs_by_id = {document.doc_id: document for document in docs}
        self.table_golden = table_golden
        self.normalized_docs = tuple(
            (_logical_document_id(document), _normalize_evidence(document.text))
            for document in docs
        )

    def analyze(self, question: Question) -> _RepeatedDetails:
        evidence = _answer_evidence(
            question, self.docs_by_id, table_golden=self.table_golden
        )
        hashes: list[str] = []
        matching_counts: list[int] = []
        short_count = 0
        for text in evidence:
            normalized = _normalize_evidence(text)
            hashes.append(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
            if len(normalized) < 10:
                short_count += 1
            logical_matches = {
                logical_id
                for logical_id, normalized_body in self.normalized_docs
                if normalized and normalized in normalized_body
            }
            matching_counts.append(len(logical_matches))
        maximum = max(matching_counts, default=0)
        return _RepeatedDetails(
            repeated=maximum >= 2,
            evidence_sha256=tuple(hashes),
            n_matching_docs=maximum,
            evidence_count=len(evidence),
            short_evidence_count=short_count,
        )


def _positive_doc_relevance(question: Question) -> dict[str, int]:
    return {
        doc_id: grade
        for doc_id, grade in doc_relevance(
            [(qrel.doc_id, qrel.rel) for qrel in question.qrels]
        ).items()
        if grade >= 1
    }


def _can_read_document(question: Question, document: CorpusDoc) -> bool:
    return can_read(
        question.asker,
        {
            "level": document.access.level,
            "teams": document.access.teams,
            "min_grade": document.access.min_grade,
        },
    )


def _map_answer_ranges(
    question: Question,
    docs_by_id: Mapping[str, CorpusDoc],
    *,
    table_golden: Mapping,
) -> tuple[list[Span], list[AnswerRangeFailure]]:
    ranges: list[Span] = []
    failures: list[AnswerRangeFailure] = []
    for qrel in question.qrels:
        if qrel.rel < 1:
            continue
        document = docs_by_id.get(qrel.doc_id)
        if document is None or not _can_read_document(question, document):
            continue
        mapped, failure = _qrel_ranges(
            question, qrel, document, table_golden=table_golden
        )
        ranges.extend(mapped)
        if failure is not None:
            failures.append(failure)
    return _unique_spans(ranges), failures


def _qrel_ranges(
    question: Question,
    qrel: Qrel,
    document: CorpusDoc,
    *,
    table_golden: Mapping,
) -> tuple[list[Span], AnswerRangeFailure | None]:
    if question.source == "synthetic":
        if qrel.start is None and qrel.end is None:
            return [], None
        if (
            not _is_int(qrel.start)
            or not _is_int(qrel.end)
            or qrel.start < 0
            or qrel.end < qrel.start
            or qrel.end > len(document.text)
        ):
            return [], AnswerRangeFailure(
                question.id,
                document.doc_id,
                qrel.fact_id,
                "invalid_synthetic_span",
            )
        return [Span(document.doc_id, qrel.start, qrel.end)], None

    keys: list[tuple[str, str]] = []
    if qrel.article_key is not None:
        keys.append(("article", qrel.article_key))
    if qrel.annex_id is not None:
        annex_key = _annex_key(qrel.annex_id, table_golden)
        if annex_key is None:
            return [], AnswerRangeFailure(
                question.id,
                document.doc_id,
                qrel.annex_id,
                "annex_label_missing",
            )
        keys.append(("annex", annex_key))
    if not keys:
        return [], AnswerRangeFailure(
            question.id,
            document.doc_id,
            None,
            "real_qrel_reference_missing",
        )
    mapped = [
        Span(document.doc_id, section.start, section.end)
        for kind, key in keys
        for section in document.sections
        if section.kind == kind and section.key == key
    ]
    if mapped:
        return _unique_spans(mapped), None
    reference = ",".join(key for _kind, key in keys)
    return [], AnswerRangeFailure(
        question.id,
        document.doc_id,
        reference,
        "section_not_found",
    )


def _answer_evidence(
    question: Question,
    docs_by_id: Mapping[str, CorpusDoc],
    *,
    table_golden: Mapping,
) -> list[str]:
    evidence: list[str] = []
    for qrel in question.qrels:
        if qrel.rel != 2:
            continue
        document = docs_by_id.get(qrel.doc_id)
        if document is None:
            continue
        if question.source == "synthetic":
            if (
                _is_int(qrel.start)
                and _is_int(qrel.end)
                and 0 <= qrel.start <= qrel.end <= len(document.text)
            ):
                evidence.append(document.text[qrel.start : qrel.end])
            continue
        ranges, failure = _qrel_ranges(
            question, qrel, document, table_golden=table_golden
        )
        if failure is None:
            evidence.extend(document.text[span.start : span.end] for span in ranges)
    return evidence


def _annex_key(annex_id: str, table_golden: Mapping) -> str | None:
    raw_tables = table_golden.get("tables")
    candidates: Sequence[object]
    if isinstance(raw_tables, Sequence) and not isinstance(raw_tables, (str, bytes)):
        candidates = raw_tables
    else:
        direct = table_golden.get(annex_id)
        candidates = [direct] if direct is not None else []
    for candidate in candidates:
        if isinstance(candidate, str):
            table_id = annex_id
            label_text = candidate
        elif isinstance(candidate, Mapping):
            table_id = candidate.get("id", annex_id)
            label_text = candidate.get("annex_label")
        else:
            continue
        if table_id != annex_id or not isinstance(label_text, str):
            continue
        label = annex_mapping._block_label(label_text)
        return None if label is None else label.annex_id
    return None


def _normalize_evidence(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _logical_document_id(document: CorpusDoc) -> str:
    if document.source == "real" and document.rule_id:
        return f"real-rule:{document.rule_id}"
    return f"doc:{document.doc_id}"


def _unique_spans(spans: Sequence[Span]) -> list[Span]:
    return list(dict.fromkeys(spans))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_table_golden(root: Path, *, missing_ok: bool = False) -> Mapping[str, object]:
    path = root / "data/manifest/table_golden.json"
    if missing_ok and not path.is_file():
        return {"tables": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON object required: {path}")
    return value


def _target_counts(targets: Mapping[str, Sequence[Question]]) -> dict[str, int]:
    return {
        key: len({question.id for question in values})
        for key, values in targets.items()
    }


def _count_validation_errors(
    targets: Mapping[str, Sequence[Question]], review: Mapping[str, object]
) -> list[str]:
    counts = _target_counts(targets)
    split = review.get("split")
    if not isinstance(split, Mapping):
        return ["golden_review.split_missing"]
    errors: list[str] = []
    expected_counts: dict[str, int] = {}
    for set_name in _QUERY_SETS:
        set_report = split.get(set_name)
        if not isinstance(set_report, Mapping):
            errors.append(f"golden_review.split.{set_name}_missing")
            continue
        expected_test = set_report.get("retrieval_test_n")
        by_type = set_report.get("by_type")
        if not _is_int(expected_test) or not isinstance(by_type, Mapping):
            errors.append(f"golden_review.split.{set_name}_invalid")
            continue
        expected_counts[f"{set_name}_test"] = expected_test
        expected_counts[f"{set_name}_dev"] = sum(
            int(type_counts.get("dev", 0))
            for question_type, type_counts in by_type.items()
            if question_type in RETRIEVAL_TYPES and isinstance(type_counts, Mapping)
        )
    for key, expected in expected_counts.items():
        if counts.get(key) != expected:
            errors.append(f"target_count:{key}:{counts.get(key)}/{expected}")

    gold = [*targets.get("gold_dev", ()), *targets.get("gold_test", ())]
    real_by_split = Counter(
        question.split for question in gold if question.source == "real"
    )
    for split_name, expected in (("dev", 6), ("test", 13)):
        if real_by_split[split_name] != expected:
            errors.append(
                f"real_gold_count:{split_name}:{real_by_split[split_name]}/{expected}"
            )

    gold_review = split.get("gold")
    expected_h7 = gold_review.get("h7") if isinstance(gold_review, Mapping) else None
    h7 = Counter(
        question.source
        for question in targets.get("gold_test", ())
        if question.type in _H7_TYPES
    )
    if not isinstance(expected_h7, Mapping):
        errors.append("golden_review.split.gold.h7_missing")
    else:
        for source in ("real", "synthetic"):
            expected = expected_h7.get(f"{source}_n")
            if not _is_int(expected) or h7[source] != expected:
                errors.append(f"h7_count:{source}:{h7[source]}/{expected}")
    return errors


def _load_json_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON object required: {path}")
    return value


def _corpus_settings_summary(
    root: Path, documents: Sequence[CorpusDoc]
) -> dict[str, object]:
    identifiers = "\n".join(sorted(document.doc_id for document in documents))
    index_path = root / "data/manifest/m5_index.json"
    return {
        "condition": "composite",
        "document_count": len(documents),
        "document_ids_sha256": hashlib.sha256(identifiers.encode("utf-8")).hexdigest(),
        "m5_index_sha256": sha256_file(index_path) if index_path.is_file() else None,
    }


def _manifest_payload(root: Path) -> tuple[dict[str, object], bool]:
    targets = load_retrieval_targets(root)
    questions = [question for group in targets.values() for question in group]
    documents, _load_report = load_corpus("composite", root=root)
    docs_by_id = {document.doc_id: document for document in documents}
    table_golden = _load_table_golden(root)
    review = _load_json_object(root / "data/manifest/golden_review.json")
    count_errors = _count_validation_errors(targets, review)
    audit = visibility_report(questions, docs_by_id)

    mapping_failures: list[AnswerRangeFailure] = []
    ranges_by_question: dict[str, tuple[Span, ...]] = {}
    for question in questions:
        ranges, failures = _map_answer_ranges(
            question, docs_by_id, table_golden=table_golden
        )
        ranges_by_question[question.id] = tuple(ranges)
        mapping_failures.extend(failures)

    analyzer = _RepeatedAnalyzer(documents, table_golden=table_golden)
    repeated_details = {
        question.id: analyzer.analyze(question) for question in questions
    }
    question_tags = {
        question.id: {
            "repeated": repeated_details[question.id].repeated,
            "evidence_sha256": list(repeated_details[question.id].evidence_sha256),
            "n_matching_docs": repeated_details[question.id].n_matching_docs,
        }
        for question in questions
    }

    group_counts: dict[str, dict[str, dict[str, int]]] = {}
    for set_name in _QUERY_SETS:
        source_counts: dict[str, dict[str, int]] = {}
        for source in ("real", "synthetic"):
            counts = Counter()
            for question in targets[f"{set_name}_test"]:
                if question.source != source:
                    continue
                details = repeated_details[question.id]
                label = (
                    "tag_unavailable"
                    if details.evidence_count == 0
                    else "repeated"
                    if details.repeated
                    else "not_repeated"
                )
                counts[label] += 1
            source_counts[source] = {
                label: counts[label]
                for label in ("repeated", "not_repeated", "tag_unavailable")
            }
        group_counts[f"{set_name}_test"] = source_counts

    silver_counts = Counter()
    for question in targets["silver_test"]:
        details = repeated_details[question.id]
        label = (
            "tag_unavailable"
            if details.evidence_count == 0
            else "repeated"
            if details.repeated
            else "not_repeated"
        )
        silver_counts[label] += 1

    no_range_ids = sorted(
        question.id for question in questions if not ranges_by_question[question.id]
    )
    untaggable_ids = sorted(
        question.id
        for question in questions
        if repeated_details[question.id].evidence_count == 0
    )
    short_evidence_count = sum(
        details.short_evidence_count for details in repeated_details.values()
    )
    visibility = {
        "checked_question_count": audit.checked_question_count,
        "failure_count": len(audit.failures),
        "failures": [asdict(failure) for failure in audit.failures],
        "excluded_pair_count": audit.excluded_pair_count,
        "excluded_question_count": audit.excluded_question_count,
        "excluded_pairs": [asdict(pair) for pair in audit.excluded_pairs],
        "per_question": audit.per_question,
    }
    gold_path = root / "data/golden/queries/gold.jsonl"
    silver_path = root / "data/golden/queries/silver.jsonl"
    failed = bool(count_errors or audit.failures or mapping_failures)
    payload: dict[str, object] = {
        "generated_at": now_iso(),
        "status": "failed" if failed else "ok",
        "target_definition": {
            "unit": "unique_question_id",
            "types": list(RETRIEVAL_TYPES),
            "requires_positive_qrel": True,
        },
        "target_counts": _target_counts(targets),
        "count_validation_errors": count_errors,
        "questions": question_tags,
        "test_group_counts": group_counts,
        "silver_test_group_sizes": {
            label: silver_counts[label]
            for label in ("repeated", "not_repeated", "tag_unavailable")
        },
        "visibility": visibility,
        "answer_range_mapping": {
            "failure_count": len(mapping_failures),
            "failures": [asdict(failure) for failure in mapping_failures],
            "no_range_question_count": len(no_range_ids),
            "no_range_question_ids": no_range_ids,
        },
        "answer_repeated": {
            "tag_unavailable_count": len(untaggable_ids),
            "tag_unavailable_question_ids": untaggable_ids,
            "normalized_evidence_under_10_chars_count": short_evidence_count,
        },
        "input_hashes": {
            "gold_queries_sha256": sha256_file(gold_path),
            "silver_queries_sha256": sha256_file(silver_path),
            "corpus_settings": _corpus_settings_summary(root, documents),
        },
    }
    return payload, failed


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
            destination.write("\n")
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    """Write the preregistered M5 evaluation-input manifest."""
    parser = argparse.ArgumentParser(
        description="M5 검색 평가 입력과 권한·정답 구간 전제를 확정합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        payload, failed = _manifest_payload(args.root)
        destination = args.root / _MANIFEST_PATH
        _write_json_atomic(destination, payload)
        counts = payload["target_counts"]
        visibility = payload["visibility"]
        mapping = payload["answer_range_mapping"]
        print(
            f"targets={counts} "
            f"visibility_failures={visibility['failure_count']} "  # type: ignore[index]
            f"excluded_pairs={visibility['excluded_pair_count']} "  # type: ignore[index]
            f"range_failures={mapping['failure_count']} "  # type: ignore[index]
            f"manifest={destination}",
            flush=True,
        )
        if failed:
            print(
                "M5 평가 입력 전제 검사 실패; manifest의 실패 목록을 확인하세요.",
                file=sys.stderr,
            )
        return int(failed)
    except (
        CorpusLoadError,
        EvalInputError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        print(f"M5 평가 입력 생성 실패: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
