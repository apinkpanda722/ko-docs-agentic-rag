"""Score parser article output deterministically against Law Open API text.

FP-043 denominator rules are explicit: every target in ``parse_targets.json``
is a document in the all-target aggregate, including missing, failed, and empty
results at zero.  A second aggregate includes only ``status=ok`` documents.
Text micro metrics average matched articles; text macro metrics average document
means.  Current and history roles are reported separately with the same rules.
No article text is written to the committed score manifest or standard output;
inspection text is confined to ignored ``data/interim/parse_diffs`` files.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TextIO

from kodocs.eval.text_normalize import (
    NORMALIZATION_RULES,
    normalize,
    raw_compare_form,
    strip_editorial,
)
from kodocs.ingest.manifest import (
    latest_records,
    load_records,
    now_iso,
    sha256_file,
)
from kodocs.parsing.articles import (
    ARTICLE_RE,
    Article,
    SplitResult,
    law_to_lines,
    split_document,
    split_text,
)
from kodocs.parsing.schema import ParsedDocument, from_json, output_path

DEFAULT_PARSERS: tuple[str, ...] = ("pypdf", "docling", "upstage")
LOW_SIMILARITY_THRESHOLD = 0.9


class ParsingScoreError(RuntimeError):
    """Parsing score inputs are absent or invalid."""


@dataclass(frozen=True)
class DocScore:
    """Numeric, key, and status-only score for one parser target."""

    parser: str
    target_key: str
    input_sha256: str
    status: str
    law_articles: int
    parsed_articles: int
    matched: int
    key_precision: float
    key_recall: float
    order_lcs_ratio: float
    structure_exact: bool
    addenda_law: int
    addenda_parsed: int
    text_sim_raw_mean: float
    text_sim_norm_mean: float
    text_exact_raw_rate: float
    text_exact_norm_rate: float
    editorial_law_only: int
    editorial_parsed_only: int
    editorial_both: int


@dataclass(frozen=True)
class _Comparison:
    score: DocScore
    unmatched_law_keys: tuple[tuple[int, int], ...]
    unmatched_parsed_keys: tuple[tuple[int, int], ...]
    low_similarity: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _Target:
    target_key: str
    law_rule_id: str
    law_serial: str
    role: str
    input_sha256: str


def score_document(
    parsed: ParsedDocument | None,
    law: Mapping[object, object],
    *,
    status: str,
    target_key: str,
    parser: str,
) -> DocScore:
    """Score one document, keeping every non-ok target as a zero-scored row."""
    return _compare_document(
        parsed,
        law,
        status=status,
        target_key=target_key,
        parser=parser,
    ).score


def splitter_ceiling(law: Mapping[object, object], *, target_key: str) -> DocScore:
    """Measure the splitter upper bound on lines built from structured law data."""
    return _compare_ceiling(law, target_key=target_key).score


def aggregate(scores: Sequence[DocScore]) -> dict[str, object]:
    """Aggregate both FP-043 document denominators and text micro/macro means."""
    all_scores = list(scores)
    ok_scores = [score for score in all_scores if score.status == "ok"]
    return {
        "status_counts": dict(
            sorted(Counter(score.status for score in all_scores).items())
        ),
        "all_targets": _aggregate_group(all_scores),
        "ok_only": _aggregate_group(ok_scores),
    }


def run(
    root: Path,
    *,
    parsers: Sequence[str] | None = None,
    out: TextIO = sys.stdout,
) -> dict[str, object]:
    """Score available parser runs, write safe summaries and local raw diffs."""
    target_path = root / "data/manifest/parse_targets.json"
    targets = _load_targets(root, target_path)
    selected_parsers = tuple(dict.fromkeys(parsers or DEFAULT_PARSERS))
    if not selected_parsers:
        raise ParsingScoreError("채점할 파서를 하나 이상 지정하세요.")

    current_records = list(
        latest_records(
            load_records(root / "data/manifest/parse_runs.jsonl"),
            lambda record: (record.get("parser"), record.get("input_sha256")),
        )
    )
    target_keys = {target.target_key for target in targets}
    parser_records = {
        parser: [
            record
            for record in current_records
            if record.get("parser") == parser
            and record.get("target_key") in target_keys
        ]
        for parser in selected_parsers
    }

    runnable = [parser for parser in selected_parsers if parser_records[parser]]
    for parser in selected_parsers:
        if not parser_records[parser]:
            print(f"{parser}: 실행 안 됨", file=out)
    if not runnable:
        raise ParsingScoreError(
            "선택한 파서의 실행 결과가 없습니다. 먼저 make parse-pypdf, "
            "make parse-docling 또는 make parse-upstage를 실행하세요."
        )

    law_by_target: dict[str, Mapping[object, object] | None] = {
        target.target_key: _load_law(root, target) for target in targets
    }
    parser_payloads: dict[str, object] = {}
    rows: list[tuple[str, dict[str, object]]] = []

    for parser in selected_parsers:
        records = parser_records[parser]
        if not records:
            parser_payloads[parser] = {"status": "not_run"}
            continue
        scores: list[DocScore] = []
        for target in targets:
            record = _record_for_target(records, target)
            law = law_by_target[target.target_key]
            comparison = _score_target(root, parser, target, record, law)
            scores.append(comparison.score)
            _write_diff(root, comparison)
        parser_aggregate = _aggregate_with_roles(scores, targets)
        parser_payloads[parser] = {
            "status": "scored",
            "aggregate": parser_aggregate,
            "documents": [asdict(score) for score in scores],
        }
        rows.append((parser, parser_aggregate["overall"]))

    ceiling_scores: list[DocScore] = []
    for target in targets:
        law = law_by_target[target.target_key]
        if law is None:
            score = _zero_score(
                parser="splitter-ceiling",
                target_key=target.target_key,
                input_sha256="",
                status="missing_law",
                law_articles=0,
                addenda_law=0,
            )
        else:
            comparison = _compare_ceiling(law, target_key=target.target_key)
            score = comparison.score
            if not score.structure_exact:
                print(
                    "splitter-ceiling 불일치 "
                    f"target_key={target.target_key} "
                    f"law_only={_json_keys(comparison.unmatched_law_keys)} "
                    f"parsed_only={_json_keys(comparison.unmatched_parsed_keys)}",
                    file=out,
                )
        ceiling_scores.append(score)
    ceiling_aggregate = _aggregate_with_roles(ceiling_scores, targets)
    parser_payloads["splitter-ceiling"] = {
        "status": "scored",
        "aggregate": ceiling_aggregate,
        "documents": [asdict(score) for score in ceiling_scores],
    }
    rows.append(("splitter-ceiling", ceiling_aggregate["overall"]))

    payload: dict[str, object] = {
        "generated_at": now_iso(),
        "git_commit": _git_commit(root),
        "parse_targets_sha256": sha256_file(target_path),
        "normalization_rules": dict(NORMALIZATION_RULES),
        "denominators": {
            "target_documents": len(targets),
            "all_targets": "parse_targets.json 전체 대상(실패·빈 결과·누락은 0점)",
            "ok_only": "status=ok인 대상",
            "text_micro": "짝지어진 조문",
            "text_macro": "해당 분모의 문서",
        },
        "parsers": parser_payloads,
    }
    _write_json_atomic(root / "data/manifest/parse_scores.json", payload)
    _print_summary(rows, out=out)
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for user-initiated, offline parsing score runs."""
    parser = argparse.ArgumentParser(
        description="파서별 조문을 법제처 조문과 결정론적으로 대조합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--parser",
        action="append",
        choices=DEFAULT_PARSERS,
        dest="parsers",
        help="채점할 파서(여러 번 지정 가능, 기본값: 전체)",
    )
    args = parser.parse_args(argv)
    try:
        run(args.root, parsers=args.parsers)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        ParsingScoreError,
    ) as exc:
        print(f"파싱 채점 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _compare_document(
    parsed: ParsedDocument | None,
    law: Mapping[object, object],
    *,
    status: str,
    target_key: str,
    parser: str,
) -> _Comparison:
    if status == "missing_law":
        return _zero_comparison(
            parser=parser,
            target_key=target_key,
            input_sha256=parsed.input_sha256 if parsed is not None else "",
            status=status,
            law_articles=0,
            addenda_law=0,
        )
    law_articles = _law_articles(law)
    addenda_law = len(_sequence_field(law, "addenda"))
    if status != "ok" or parsed is None:
        return _zero_comparison(
            parser=parser,
            target_key=target_key,
            input_sha256=parsed.input_sha256 if parsed is not None else "",
            status=status,
            law_articles=len(law_articles),
            addenda_law=addenda_law,
            unmatched_law_keys=tuple(_key(article) for article in law_articles),
        )
    result = split_document(parsed)
    return _compare_articles(
        law_articles,
        result,
        parser=parser,
        target_key=target_key,
        input_sha256=parsed.input_sha256,
        status=status,
        addenda_law=addenda_law,
    )


def _compare_ceiling(law: Mapping[object, object], *, target_key: str) -> _Comparison:
    law_articles = _law_articles(law)
    result = split_text(law_to_lines(law))
    return _compare_articles(
        law_articles,
        result,
        parser="splitter-ceiling",
        target_key=target_key,
        input_sha256="",
        status="ok",
        addenda_law=len(_sequence_field(law, "addenda")),
    )


def _compare_articles(
    law_articles: Sequence[Article],
    parsed_result: SplitResult,
    *,
    parser: str,
    target_key: str,
    input_sha256: str,
    status: str,
    addenda_law: int,
) -> _Comparison:
    parsed_articles = parsed_result.articles
    pairs, unmatched_law, unmatched_parsed = _pair_first_unique(
        law_articles, parsed_articles
    )
    raw_similarities: list[float] = []
    norm_similarities: list[float] = []
    raw_exact = 0
    norm_exact = 0
    law_only = 0
    parsed_only = 0
    both = 0
    low_similarity: list[dict[str, object]] = []

    for law_article, parsed_article in pairs:
        law_raw = raw_compare_form(law_article.text)
        parsed_raw = raw_compare_form(parsed_article.text)
        law_norm = normalize(law_article.text)
        parsed_norm = normalize(parsed_article.text)
        raw_similarity = _similarity(law_raw, parsed_raw)
        norm_similarity = _similarity(law_norm, parsed_norm)
        raw_similarities.append(raw_similarity)
        norm_similarities.append(norm_similarity)
        raw_exact += law_raw == parsed_raw
        norm_exact += law_norm == parsed_norm
        _, law_annotations = strip_editorial(law_article.text)
        _, parsed_annotations = strip_editorial(parsed_article.text)
        if law_annotations and parsed_annotations:
            both += 1
        elif law_annotations:
            law_only += 1
        elif parsed_annotations:
            parsed_only += 1
        if norm_similarity < LOW_SIMILARITY_THRESHOLD:
            low_similarity.append(
                {
                    "key": list(_key(law_article)),
                    "text_sim_raw": raw_similarity,
                    "text_sim_norm": norm_similarity,
                    "law_text": law_article.text,
                    "parsed_text": parsed_article.text,
                }
            )

    law_keys = [_key(article) for article in law_articles]
    parsed_keys = [_key(article) for article in parsed_articles]
    matched = len(pairs)
    score = DocScore(
        parser=parser,
        target_key=target_key,
        input_sha256=input_sha256,
        status=status,
        law_articles=len(law_articles),
        parsed_articles=len(parsed_articles),
        matched=matched,
        key_precision=_ratio(matched, len(parsed_articles)),
        key_recall=_ratio(matched, len(law_articles)),
        order_lcs_ratio=_ratio(_lcs_length(law_keys, parsed_keys), len(law_keys)),
        structure_exact=law_keys == parsed_keys,
        addenda_law=addenda_law,
        addenda_parsed=len(parsed_result.addenda),
        text_sim_raw_mean=_mean(raw_similarities),
        text_sim_norm_mean=_mean(norm_similarities),
        text_exact_raw_rate=_ratio(raw_exact, matched),
        text_exact_norm_rate=_ratio(norm_exact, matched),
        editorial_law_only=law_only,
        editorial_parsed_only=parsed_only,
        editorial_both=both,
    )
    return _Comparison(
        score=score,
        unmatched_law_keys=tuple(_key(law_articles[index]) for index in unmatched_law),
        unmatched_parsed_keys=tuple(
            _key(parsed_articles[index]) for index in unmatched_parsed
        ),
        low_similarity=tuple(low_similarity),
    )


def _law_articles(law: Mapping[object, object]) -> tuple[Article, ...]:
    articles: list[Article] = []
    for value in _sequence_field(law, "articles"):
        text = str(value)
        match = ARTICLE_RE.match(text)
        if match is None:
            continue
        title = match.group("paren_title") or match.group("bracket_title")
        articles.append(
            Article(
                number=int(match.group("number")),
                branch=int(match.group("branch") or 0),
                title=title.strip() if title is not None else None,
                deleted=match.group("deleted") is not None,
                text=text,
                first_page=None,
            )
        )
    return tuple(articles)


def _pair_first_unique(
    law_articles: Sequence[Article], parsed_articles: Sequence[Article]
) -> tuple[list[tuple[Article, Article]], list[int], list[int]]:
    first_parsed: dict[tuple[int, int], int] = {}
    for index, article in enumerate(parsed_articles):
        first_parsed.setdefault(_key(article), index)

    used_keys: set[tuple[int, int]] = set()
    matched_law_indexes: set[int] = set()
    matched_parsed_indexes: set[int] = set()
    pairs: list[tuple[Article, Article]] = []
    for law_index, law_article in enumerate(law_articles):
        key = _key(law_article)
        parsed_index = first_parsed.get(key)
        if key in used_keys or parsed_index is None:
            continue
        used_keys.add(key)
        matched_law_indexes.add(law_index)
        matched_parsed_indexes.add(parsed_index)
        pairs.append((law_article, parsed_articles[parsed_index]))
    return (
        pairs,
        [
            index
            for index in range(len(law_articles))
            if index not in matched_law_indexes
        ],
        [
            index
            for index in range(len(parsed_articles))
            if index not in matched_parsed_indexes
        ],
    )


def _aggregate_group(scores: Sequence[DocScore]) -> dict[str, int | float]:
    document_count = len(scores)
    matched_articles = sum(score.matched for score in scores)
    return {
        "documents": document_count,
        "law_articles": sum(score.law_articles for score in scores),
        "parsed_articles": sum(score.parsed_articles for score in scores),
        "matched_articles": matched_articles,
        "structure_exact_rate": _ratio(
            sum(score.structure_exact for score in scores), document_count
        ),
        "key_precision_mean": _mean([score.key_precision for score in scores]),
        "key_recall_mean": _mean([score.key_recall for score in scores]),
        "key_f1_mean": _mean(
            [_f1(score.key_precision, score.key_recall) for score in scores]
        ),
        "order_lcs_ratio_mean": _mean([score.order_lcs_ratio for score in scores]),
        "text_sim_raw_mean_micro": _weighted_text_mean(scores, "text_sim_raw_mean"),
        "text_sim_norm_mean_micro": _weighted_text_mean(scores, "text_sim_norm_mean"),
        "text_exact_raw_rate_micro": _weighted_text_mean(scores, "text_exact_raw_rate"),
        "text_exact_norm_rate_micro": _weighted_text_mean(
            scores, "text_exact_norm_rate"
        ),
        "text_sim_raw_mean_macro": _mean([score.text_sim_raw_mean for score in scores]),
        "text_sim_norm_mean_macro": _mean(
            [score.text_sim_norm_mean for score in scores]
        ),
        "text_exact_raw_rate_macro": _mean(
            [score.text_exact_raw_rate for score in scores]
        ),
        "text_exact_norm_rate_macro": _mean(
            [score.text_exact_norm_rate for score in scores]
        ),
        "editorial_law_only": sum(score.editorial_law_only for score in scores),
        "editorial_parsed_only": sum(score.editorial_parsed_only for score in scores),
        "editorial_both": sum(score.editorial_both for score in scores),
    }


def _aggregate_with_roles(
    scores: Sequence[DocScore], targets: Sequence[_Target]
) -> dict[str, object]:
    role_by_key = {target.target_key: target.role for target in targets}
    roles = sorted({target.role for target in targets})
    return {
        "overall": aggregate(scores),
        "roles": {
            role: aggregate(
                [score for score in scores if role_by_key[score.target_key] == role]
            )
            for role in roles
        },
    }


def _score_target(
    root: Path,
    parser: str,
    target: _Target,
    record: Mapping[str, object] | None,
    law: Mapping[object, object] | None,
) -> _Comparison:
    record_sha = _string(record.get("input_sha256")) if record is not None else ""
    input_sha = record_sha or target.input_sha256
    if law is None:
        return _zero_comparison(
            parser=parser,
            target_key=target.target_key,
            input_sha256=input_sha,
            status="missing_law",
            law_articles=0,
            addenda_law=0,
        )
    if record is None:
        comparison = _compare_document(
            None,
            law,
            status="missing",
            target_key=target.target_key,
            parser=parser,
        )
        return replace(
            comparison,
            score=replace(comparison.score, input_sha256=input_sha),
        )
    status = _record_status(record)
    parsed: ParsedDocument | None = None
    if status == "ok" and record_sha:
        try:
            parsed = from_json(
                output_path(root, parser, record_sha).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            status = "missing"
    comparison = _compare_document(
        parsed,
        law,
        status=status,
        target_key=target.target_key,
        parser=parser,
    )
    return replace(
        comparison,
        score=replace(comparison.score, input_sha256=input_sha),
    )


def _record_status(record: Mapping[str, object]) -> str:
    value = record.get("status")
    if value in {"ok", "skipped"}:
        return "ok"
    if value in {"empty", "failed"}:
        return str(value)
    return "missing"


def _record_for_target(
    records: Sequence[Mapping[str, object]], target: _Target
) -> Mapping[str, object] | None:
    candidates = [
        record for record in records if record.get("target_key") == target.target_key
    ]
    if not candidates:
        return None
    if target.input_sha256:
        matching = [
            record
            for record in candidates
            if record.get("input_sha256") == target.input_sha256
        ]
        if matching:
            return matching[-1]
    return candidates[-1]


def _load_targets(root: Path, path: Path) -> list[_Target]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ParsingScoreError(
            "data/manifest/parse_targets.json이 없습니다. "
            "먼저 make parse-targets를 실행하세요."
        ) from exc
    values = payload.get("targets") if isinstance(payload, Mapping) else None
    if not isinstance(values, list) or not values:
        raise ParsingScoreError(
            "파싱 대상이 0건입니다. 먼저 make parse-targets를 실행하세요."
        )
    targets: list[_Target] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ParsingScoreError(f"targets[{index}]가 객체가 아닙니다.")
        rule = _string(value.get("law_rule_id"))
        serial = _string(value.get("law_serial"))
        source_sha = _string(value.get("source_sha256"))
        role = _string(value.get("role"))
        pdf_value = value.get("pdf_path")
        input_sha = ""
        if isinstance(pdf_value, str) and pdf_value:
            pdf_path = root / pdf_value
            if pdf_path.is_file():
                input_sha = sha256_file(pdf_path)
        targets.append(
            _Target(
                target_key=f"{rule}:{serial}:{source_sha}",
                law_rule_id=rule,
                law_serial=serial,
                role=role,
                input_sha256=input_sha,
            )
        )
    return targets


def _load_law(root: Path, target: _Target) -> Mapping[object, object] | None:
    path = root / "data/interim/law" / target.law_rule_id / f"{target.law_serial}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, Mapping):
        raise ParsingScoreError(f"법제처 파일이 객체가 아닙니다: {path}")
    return payload


def _write_diff(root: Path, comparison: _Comparison) -> None:
    score = comparison.score
    if not score.input_sha256:
        return
    payload = {
        "parser": score.parser,
        "target_key": score.target_key,
        "input_sha256": score.input_sha256,
        "status": score.status,
        "unmatched_law_keys": _json_keys(comparison.unmatched_law_keys),
        "unmatched_parsed_keys": _json_keys(comparison.unmatched_parsed_keys),
        "low_similarity_articles": list(comparison.low_similarity),
    }
    path = (
        root / "data/interim/parse_diffs" / score.parser / f"{score.input_sha256}.json"
    )
    _write_json_atomic(path, payload)


def _print_summary(
    rows: Sequence[tuple[str, Mapping[str, object]]], *, out: TextIO
) -> None:
    print(
        "| 파서 | 문서 수(ok/전체) | 구조 완전 일치율 | 키 F1 평균 | "
        "순서 LCS 평균 | 정규화 유사도(micro) | "
        "정규화 완전 일치율(micro) | 원문 유사도(micro) |",
        file=out,
    )
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |", file=out)
    for parser, overall in rows:
        status_counts = overall["status_counts"]
        all_targets = overall["all_targets"]
        ok_documents = status_counts.get("ok", 0)  # type: ignore[union-attr]
        documents = all_targets["documents"]  # type: ignore[index]
        print(
            f"| {parser} | {ok_documents}/{documents} | "
            f"{_percent(all_targets['structure_exact_rate'])} | "  # type: ignore[index]
            f"{_number(all_targets['key_f1_mean'])} | "  # type: ignore[index]
            f"{_number(all_targets['order_lcs_ratio_mean'])} | "  # type: ignore[index]
            f"{_number(all_targets['text_sim_norm_mean_micro'])} | "  # type: ignore[index]
            f"{_percent(all_targets['text_exact_norm_rate_micro'])} | "  # type: ignore[index]
            f"{_number(all_targets['text_sim_raw_mean_micro'])} |",  # type: ignore[index]
            file=out,
        )


def _zero_comparison(
    *,
    parser: str,
    target_key: str,
    input_sha256: str,
    status: str,
    law_articles: int,
    addenda_law: int,
    unmatched_law_keys: tuple[tuple[int, int], ...] = (),
) -> _Comparison:
    return _Comparison(
        score=_zero_score(
            parser=parser,
            target_key=target_key,
            input_sha256=input_sha256,
            status=status,
            law_articles=law_articles,
            addenda_law=addenda_law,
        ),
        unmatched_law_keys=unmatched_law_keys,
        unmatched_parsed_keys=(),
        low_similarity=(),
    )


def _zero_score(
    *,
    parser: str,
    target_key: str,
    input_sha256: str,
    status: str,
    law_articles: int,
    addenda_law: int,
) -> DocScore:
    return DocScore(
        parser=parser,
        target_key=target_key,
        input_sha256=input_sha256,
        status=status,
        law_articles=law_articles,
        parsed_articles=0,
        matched=0,
        key_precision=0.0,
        key_recall=0.0,
        order_lcs_ratio=0.0,
        structure_exact=False,
        addenda_law=addenda_law,
        addenda_parsed=0,
        text_sim_raw_mean=0.0,
        text_sim_norm_mean=0.0,
        text_exact_raw_rate=0.0,
        text_exact_norm_rate=0.0,
        editorial_law_only=0,
        editorial_parsed_only=0,
        editorial_both=0,
    )


def _sequence_field(mapping: Mapping[object, object], field: str) -> Sequence[object]:
    value = mapping.get(field, ())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise TypeError(f"{field} must be a sequence")


def _lcs_length(
    first: Sequence[tuple[int, int]], second: Sequence[tuple[int, int]]
) -> int:
    previous = [0] * (len(second) + 1)
    for left in first:
        current = [0]
        for index, right in enumerate(second, start=1):
            if left == right:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def _weighted_text_mean(scores: Sequence[DocScore], field: str) -> float:
    denominator = sum(score.matched for score in scores)
    if not denominator:
        return 0.0
    return sum(getattr(score, field) * score.matched for score in scores) / denominator


def _similarity(first: str, second: str) -> float:
    return SequenceMatcher(None, first, second, autojunk=False).ratio()


def _ratio(numerator: float, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _key(article: Article) -> tuple[int, int]:
    return article.number, article.branch


def _json_keys(keys: Sequence[tuple[int, int]]) -> list[list[int]]:
    return [list(key) for key in keys]


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _write_json_atomic(path: Path, payload: object) -> None:
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
            json.dump(
                payload, destination, ensure_ascii=False, indent=2, sort_keys=True
            )
            destination.write("\n")
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _number(value: object) -> str:
    return f"{float(value):.4f}"


def _percent(value: object) -> str:
    return f"{float(value) * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
