"""Prepare and validate locally drafted real-regulation golden questions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

from kodocs.eval.golden import (
    Question,
    load_questions,
    question_from_dict,
    question_to_dict,
    validate_question,
)
from kodocs.eval.lexical import longest_common_substring
from kodocs.ingest.manifest import sha256_file
from kodocs.parsing.articles import law_to_lines, split_text

INTERIM_DIR = Path("data/interim/golden")
CANDIDATES_PATH = INTERIM_DIR / "real_candidates.md"
DRAFTS_PATH = INTERIM_DIR / "real_drafts.jsonl"
PROJECTION_PATH = Path("data/synthetic/queries/real_projection.jsonl")
SUMMARY_PATH = Path("data/manifest/golden_real.json")
REAL_QUOTA = {
    "identifier": 2,
    "semantic": 4,
    "table": 6,
    "multi_doc": 2,
    "temporal": 5,
    "unanswerable": 1,
}

LAW_DIR = Path("data/interim/law")
REGULATION_REFS_PATH = Path("data/manifest/regulation_refs.json")
VERSION_MATCHES_PATH = Path("data/manifest/version_matches.jsonl")
TABLE_GOLDEN_PATH = Path("data/manifest/table_golden.json")
SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")

_REAL_ID_RE = re.compile(r"^g-real-\d{3}$")
_LAW_DOC_ID_RE = re.compile(r"^law-(\d+)$")
_ELIGIBLE_CONFIDENCES = frozenset({"exact", "approximate"})
_SYNTHETIC_ATTRIBUTE_TERMS = (
    "비용",
    "소요 금액",
    "영향 사용자 수",
    "사용자 수",
)


def law_versions(root: Path) -> list[dict[str, object]]:
    """Return deterministic local-law metadata without article or annex bodies."""
    versions: list[dict[str, object]] = []
    for path in _law_files(root):
        law = _load_mapping(path)
        versions.append(
            {
                "name": _required_text(law, "name", path),
                "serial": _required_text(law, "serial", path),
                "rule_id": _required_text(law, "rule_id", path),
                "issued_date": _required_text(law, "issued_date", path),
                "is_current": _required_bool(law, "is_current", path),
            }
        )
    return sorted(
        versions,
        key=lambda item: (
            str(item["rule_id"]),
            str(item["issued_date"]),
            str(item["serial"]),
        ),
    )


def article_texts(law: Mapping[object, object]) -> dict[str, str]:
    """Split one cleaned Law API object into in-memory article-key text."""
    result = split_text(law_to_lines(law))
    articles: dict[str, str] = {}
    for article in result.articles:
        key = f"제{article.number}조"
        if article.branch:
            key += f"의{article.branch}"
        articles[key] = article.text
    return articles


def revision_pairs(root: Path) -> list[dict[str, object]]:
    """Return changed adjacent versions whose two matches are usable by EVAL."""
    eligible = _eligible_version_matches(root / VERSION_MATCHES_PATH)
    grouped: dict[str, list[tuple[dict[str, object], dict[str, str]]]] = defaultdict(
        list
    )
    for path in _law_files(root):
        law = _load_mapping(path)
        metadata = {
            "name": _required_text(law, "name", path),
            "serial": _required_text(law, "serial", path),
            "rule_id": _required_text(law, "rule_id", path),
            "issued_date": _required_text(law, "issued_date", path),
        }
        grouped[str(metadata["rule_id"])].append((metadata, article_texts(law)))

    pairs: list[dict[str, object]] = []
    for rule_id in sorted(grouped):
        versions = sorted(
            grouped[rule_id],
            key=lambda item: (
                str(item[0]["issued_date"]),
                str(item[0]["serial"]),
            ),
        )
        for (previous, previous_articles), (next_, next_articles) in pairwise(versions):
            previous_key = (rule_id, str(previous["serial"]))
            next_key = (rule_id, str(next_["serial"]))
            if previous_key not in eligible or next_key not in eligible:
                continue
            changed = sorted(
                key
                for key in previous_articles.keys() & next_articles.keys()
                if previous_articles[key] != next_articles[key]
            )
            if not changed:
                continue
            pairs.append(
                {
                    "rule_id": rule_id,
                    "name": str(next_["name"]),
                    "previous_serial": str(previous["serial"]),
                    "previous_issued_date": str(previous["issued_date"]),
                    "previous_confidence": eligible[previous_key],
                    "next_serial": str(next_["serial"]),
                    "next_issued_date": str(next_["issued_date"]),
                    "next_confidence": eligible[next_key],
                    "changed_article_keys": changed,
                }
            )
    return pairs


def search_law_texts(root: Path, terms: Sequence[str]) -> list[tuple[str, str]]:
    """Find articles containing every non-empty term in the same article."""
    return _search_articles(_laws_by_serial(root), terms)


def write_candidates(root: Path) -> int:
    """Write the local-only safe-structure candidate worksheet."""
    root = Path(root).resolve()
    if not _law_files(root):
        print("법제처 정제 파일 없음, `make collect-law` 먼저(사용자 실행)")
        return 1
    try:
        regulation_index = _load_mapping(root / REGULATION_REFS_PATH)
        table_manifest = _load_mapping(root / TABLE_GOLDEN_PATH)
        pairs = revision_pairs(root)
        text = _candidate_markdown(regulation_index, table_manifest, pairs)
        _write_text_atomic(root / CANDIDATES_PATH, text)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"실제 규정 후보 생성 실패: {_first_line(exc)}")
        return 1
    print(f"실제 규정 후보 작성 완료: {CANDIDATES_PATH.as_posix()}")
    return 0


def check_drafts(root: Path) -> int:
    """Validate private drafts and write body-free deterministic outputs."""
    root = Path(root).resolve()
    if not _law_files(root):
        print("법제처 정제 파일 없음, `make collect-law` 먼저(사용자 실행)")
        return 1
    drafts_path = root / DRAFTS_PATH
    if not drafts_path.is_file():
        print("실제 규정 초안 없음, Claude 초안 작성·사람 확인 먼저")
        return 1
    try:
        return _check_drafts(root)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"실제 규정 초안 검증 실패: {_first_line(exc)}")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for candidate generation and private-draft checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("candidates", "check"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.command == "candidates":
        return write_candidates(args.root)
    return check_drafts(args.root)


def _check_drafts(root: Path) -> int:
    raw_rows = _load_jsonl(root / DRAFTS_PATH)
    regulation_index = _load_mapping(root / REGULATION_REFS_PATH)
    table_manifest = _load_mapping(root / TABLE_GOLDEN_PATH)
    table_ids = _table_ids(table_manifest)
    pairs = revision_pairs(root)
    revision_versions = {
        (str(pair[serial_key]), str(pair[date_key]))
        for pair in pairs
        for serial_key, date_key in (
            ("previous_serial", "previous_issued_date"),
            ("next_serial", "next_issued_date"),
        )
    }
    laws_by_serial = _laws_by_serial(root)

    errors: list[str] = []
    projected_questions: list[Question] = []
    projection_rows: list[dict[str, object]] = []
    real_no_answer_checks: dict[str, bool] = {}
    seen_ids: set[str] = set()

    for row in raw_rows:
        question_id = row.get("id")
        display_id = question_id if isinstance(question_id, str) else "<unknown>"
        if not isinstance(question_id, str) or not _REAL_ID_RE.fullmatch(question_id):
            errors.append(f"{display_id}:invalid_real_id")
        elif question_id in seen_ids:
            errors.append(f"{display_id}:duplicate_id")
        else:
            seen_ids.add(question_id)
        if row.get("set") != "gold" or row.get("source") != "real":
            errors.append(f"{display_id}:real_identity_fields")

        reference_answer = row.get("reference_answer")
        if not isinstance(reference_answer, str) or not reference_answer:
            errors.append(f"{display_id}:missing_reference_answer")
            continue
        question = question_from_dict(row)
        reference_sha256 = hashlib.sha256(reference_answer.encode("utf-8")).hexdigest()

        no_answer_checks = question.no_answer_checks
        if question.type == "unanswerable":
            terms = _string_terms(row.get("no_answer_terms"))
            if len(terms) < 2:
                errors.append(f"{display_id}:missing_no_answer_terms")
                real_check = False
            else:
                real_check = not _search_articles(laws_by_serial, terms)
                if not real_check:
                    errors.append(f"{display_id}:unanswerable_real_match")
            real_no_answer_checks[display_id] = real_check
            no_answer_checks = {**(no_answer_checks or {}), "real": real_check}

        projected = replace(
            question,
            reference_answer=None,
            reference_sha256=reference_sha256,
            no_answer_checks=no_answer_checks,
        )
        validation_errors = validate_question(
            projected,
            docs={},
            regulation_index=regulation_index,
            table_ids=table_ids,
        )
        if validation_errors:
            errors.append(f"{display_id}:{','.join(validation_errors)}")
        if projected.type == "temporal" and not _temporal_matches(
            projected, revision_versions
        ):
            errors.append(f"{display_id}:temporal_revision_mismatch")
        if _has_real_text_leak(projected, laws_by_serial):
            errors.append(f"{display_id}:real_text_leak")

        projection = question_to_dict(projected)
        projection.pop("reference_answer", None)
        projected_questions.append(projected)
        projection_rows.append(projection)

    counts = {
        question_type: sum(
            question.type == question_type for question in projected_questions
        )
        for question_type in REAL_QUOTA
    }
    quota_diff = {
        question_type: counts[question_type] - expected
        for question_type, expected in REAL_QUOTA.items()
    }
    unexpected_types = sorted(
        set(Counter(question.type for question in projected_questions))
        - set(REAL_QUOTA)
    )
    if any(quota_diff.values()) or unexpected_types:
        errors.append("quota_mismatch")
    _print_quota(counts, quota_diff)

    synthetic_checks = _synthetic_no_answer_checks(root, laws_by_serial)
    failed_synthetic = sorted(
        question_id for question_id, passed in synthetic_checks.items() if not passed
    )
    if failed_synthetic:
        errors.append("synthetic_unanswerable_real_match:" + ",".join(failed_synthetic))

    if errors:
        for error in dict.fromkeys(errors):
            print(f"검증 오류: {error}")
        return 1

    projection_rows.sort(key=lambda row: str(row["id"]))
    _write_jsonl_atomic(root / PROJECTION_PATH, projection_rows)
    summary = {
        "counts": counts,
        "quota_diff": quota_diff,
        "no_answer_checks": dict(sorted(real_no_answer_checks.items())),
        "synthetic_no_answer_real_check": synthetic_checks,
        "revision_pairs_available": len(pairs),
        "inputs": {
            "regulation_refs_sha256": sha256_file(root / REGULATION_REFS_PATH),
            "version_matches_sha256": sha256_file(root / VERSION_MATCHES_PATH),
            "table_golden_sha256": sha256_file(root / TABLE_GOLDEN_PATH),
            "law_files_sha256": _law_files_sha256(root),
            "drafts_sha256": sha256_file(root / DRAFTS_PATH),
        },
    }
    _write_json_atomic(root / SUMMARY_PATH, summary)
    print(f"실제 규정 투영 검증 완료: {len(projection_rows)}문항")
    return 0


def _candidate_markdown(
    regulation_index: Mapping[str, object],
    table_manifest: Mapping[str, object],
    pairs: Sequence[Mapping[str, object]],
) -> str:
    lines = ["# 실제 규정 골드 문항 후보", "", "## 현행 규정 조항", ""]
    rules = regulation_index.get("rules")
    if not isinstance(rules, Mapping):
        raise TypeError("regulation_refs rules must be an object")
    for name, value in sorted(rules.items(), key=lambda item: str(item[0])):
        if not isinstance(value, Mapping):
            raise TypeError("regulation_refs rule must be an object")
        lines.append(f"### {name} (law-{value.get('serial', '')})")
        lines.append("")
        articles = value.get("articles")
        if not isinstance(articles, Mapping):
            raise TypeError("regulation_refs articles must be an object")
        for article_key, title in sorted(
            articles.items(), key=lambda item: str(item[0])
        ):
            lines.append(f"- {article_key} — {title}")
        lines.append("")

    lines.extend(["## 확정 표", ""])
    tables = table_manifest.get("tables")
    if not isinstance(tables, Sequence) or isinstance(tables, (str, bytes)):
        raise TypeError("table_golden tables must be an array")
    for table in sorted(tables, key=lambda item: str(_as_mapping(item).get("id", ""))):
        value = _as_mapping(table)
        lines.append(
            f"- {value.get('id', '')} | 규정 {value.get('rule_no', '')} | "
            f"{value.get('annex_label', '')}"
        )

    lines.extend(["", "## 최신성 개정 쌍", ""])
    for pair in pairs:
        changed = ", ".join(str(key) for key in pair["changed_article_keys"])
        lines.append(
            f"- {pair['name']} ({pair['rule_id']}): "
            f"{pair['previous_serial']} → {pair['next_serial']} | "
            f"{pair['previous_issued_date']} → {pair['next_issued_date']} | {changed}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _eligible_version_matches(path: Path) -> dict[tuple[str, str], str]:
    matches: dict[tuple[str, str], str] = {}
    rank = {"approximate": 0, "exact": 1}
    for row in _load_jsonl(path):
        confidence = row.get("confidence")
        rule_id = row.get("law_rule_id")
        serial = row.get("law_serial")
        if (
            confidence not in _ELIGIBLE_CONFIDENCES
            or not isinstance(rule_id, (str, int))
            or isinstance(rule_id, bool)
            or not isinstance(serial, (str, int))
            or isinstance(serial, bool)
        ):
            continue
        key = (str(rule_id), str(serial))
        previous = matches.get(key)
        if previous is None or rank[str(confidence)] > rank[previous]:
            matches[key] = str(confidence)
    return matches


def _laws_by_serial(root: Path) -> dict[str, dict[str, str]]:
    laws: dict[str, dict[str, str]] = {}
    for path in _law_files(root):
        law = _load_mapping(path)
        serial = _required_text(law, "serial", path)
        if serial in laws:
            raise ValueError(f"duplicate law serial: {serial}")
        laws[serial] = article_texts(law)
    return laws


def _search_articles(
    laws_by_serial: Mapping[str, Mapping[str, str]], terms: Sequence[str]
) -> list[tuple[str, str]]:
    normalized_terms = tuple(
        dict.fromkeys(term.strip() for term in terms if term.strip())
    )
    if not normalized_terms:
        return []
    return sorted(
        (f"law-{serial}", article_key)
        for serial, articles in laws_by_serial.items()
        for article_key, text in articles.items()
        if all(term in text for term in normalized_terms)
    )


def _temporal_matches(
    question: Question, revision_versions: set[tuple[str, str]]
) -> bool:
    if question.tags.count("temporal:revision") != 1 or not question.as_of:
        return False
    serials: list[str] = []
    for qrel in question.qrels:
        match = _LAW_DOC_ID_RE.fullmatch(qrel.doc_id)
        if match is None:
            return False
        serials.append(match.group(1))
    return bool(serials) and all(
        (serial, question.as_of) in revision_versions for serial in serials
    )


def _has_real_text_leak(
    question: Question, laws_by_serial: Mapping[str, Mapping[str, str]]
) -> bool:
    texts: list[str] = []
    for qrel in question.qrels:
        match = _LAW_DOC_ID_RE.fullmatch(qrel.doc_id)
        if match is None or qrel.article_key is None:
            continue
        text = laws_by_serial.get(match.group(1), {}).get(qrel.article_key)
        if text is not None:
            texts.append(text)
    values = (question.question, question.summary80 or "")
    return any(
        longest_common_substring(value, text) > 30 for value in values for text in texts
    )


def _synthetic_no_answer_checks(
    root: Path, laws_by_serial: Mapping[str, Mapping[str, str]]
) -> dict[str, bool]:
    specs = load_questions(root / SPECS_PATH)
    checks: dict[str, bool] = {}
    for spec in sorted(specs, key=lambda item: item.id):
        if spec.source != "synthetic" or spec.type != "unanswerable":
            continue
        anchors = tuple(value for value in spec.anchor_values if value)
        passed = bool(anchors) and not any(
            _search_articles(laws_by_serial, (*anchors, attribute_term))
            for attribute_term in _SYNTHETIC_ATTRIBUTE_TERMS
        )
        checks[spec.id] = passed
    return checks


def _table_ids(manifest: Mapping[str, object]) -> set[str]:
    tables = manifest.get("tables")
    if not isinstance(tables, Sequence) or isinstance(tables, (str, bytes)):
        raise TypeError("table_golden tables must be an array")
    result: set[str] = set()
    for item in tables:
        table_id = _as_mapping(item).get("id")
        if not isinstance(table_id, str) or not table_id:
            raise ValueError("table_golden table has no id")
        result.add(table_id)
    return result


def _law_files(root: Path) -> list[Path]:
    return sorted((root / LAW_DIR).glob("*/*.json"))


def _law_files_sha256(root: Path) -> str:
    hashes = [sha256_file(path) for path in _law_files(root)]
    serialized = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _load_mapping(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, Mapping):
        raise TypeError(f"expected JSON object: {path}")
    return dict(value)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise TypeError(f"expected JSON object: {path}:{line_number}")
            rows.append(dict(value))
    return rows


def _required_text(value: Mapping[str, object], key: str, path: Path) -> str:
    field = value.get(key)
    if not isinstance(field, (str, int)) or isinstance(field, bool) or not str(field):
        raise ValueError(f"missing {key}: {path}")
    return str(field)


def _required_bool(value: Mapping[str, object], key: str, path: Path) -> bool:
    field = value.get(key)
    if not isinstance(field, bool):
        raise TypeError(f"missing {key}: {path}")
    return field


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected JSON object")
    return value


def _string_terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    terms: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return ()
        terms.append(item.strip())
    return tuple(dict.fromkeys(terms))


def _print_quota(counts: Mapping[str, int], diff: Mapping[str, int]) -> None:
    for question_type, expected in REAL_QUOTA.items():
        print(
            f"{question_type}: actual={counts[question_type]} "
            f"expected={expected} diff={diff[question_type]:+d}"
        )


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    serialized = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    )
    _write_text_atomic(path, serialized)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, text)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0]


if __name__ == "__main__":
    raise SystemExit(main())
