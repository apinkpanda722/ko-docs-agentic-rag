"""Load the immutable M5 corpus from local, already-produced artifacts.

The loader never calls a parser, model, or external service.  Synthetic bodies and
parser output fields are kept verbatim; normalization is limited to identifier keys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TextIO

import yaml

from kodocs.ingest.manifest import (
    latest_records,
    load_records,
    now_iso,
    sha256_file,
)
from kodocs.parsing.annex_mapping import (
    AnnexRegion,
    annex_regions,
    article_region_text,
    map_annex_tables,
)
from kodocs.parsing.articles import (
    ADDENDUM_RE,
    ARTICLE_RE,
    HEADING_RE,
    Article,
    SplitResult,
    article_branch,
    law_to_lines,
    split_document,
    split_text,
)
from kodocs.parsing.schema import ParsedDocument, from_json, output_path
from kodocs.synth.identifiers import (
    REGULATION_REF_RE,
    extract_identifiers,
    service_pattern,
)

ParserCondition = Literal["composite", "pypdf", "upstage"]

_PARSER_CONDITIONS: frozenset[str] = frozenset({"composite", "pypdf", "upstage"})
_IDENTIFIER_KINDS = ("ticket", "approval", "incident", "service", "regulation")
_MARKDOWN_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE
)
_MEETING_PARENT_RE = re.compile(r"^3\.\s*안건별\s+논의$")
_MEETING_AGENDA_RE = re.compile(r"^\d+\.\d+(?:\s|$)")
_INCIDENT_SECTION_RE = re.compile(r"^\d+\.(?:\s|$)")
_ARTICLE_KEY_RE = re.compile(r"^제\d+조(?:의\d+)?$")
_CHAPTER_RE = re.compile(r"^\s*제\s*\d+\s*장")
_PRIOR_MANIFEST_PATH = Path("data/manifest/m5_prior_versions.json")


class CorpusLoadError(RuntimeError):
    """The local corpus cannot be loaded at all."""


@dataclass(frozen=True)
class Access:
    level: str
    teams: tuple[str, ...]
    min_grade: int


@dataclass(frozen=True)
class Section:
    kind: str
    key: str | None
    path: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    source: str
    doc_type: str
    title: str
    text: str
    sections: tuple[Section, ...]
    access: Access
    team: str | None
    date: str | None
    rule_id: str | None
    is_current: bool
    superseded_by: str | None
    identifiers: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class PriorVersionRecord:
    rule_id: str
    current_serial: str
    current_issued_date: str
    prior_serial: str
    prior_issued_date: str


@dataclass(frozen=True)
class LoadReport:
    condition: ParserCondition
    synthetic_document_count: int
    expected_current_real_document_count: int
    loaded_current_real_document_count: int
    expected_prior_real_document_count: int
    loaded_prior_real_document_count: int
    composite_annex_fallback_count: int
    synthetic_zero_heading_section_document_count_by_type: dict[str, int]
    identifier_extracted_document_count_by_kind: dict[str, int]
    service_names_source: str
    skipped: tuple[tuple[str, str], ...]
    missing_prior_current_doc_ids: tuple[str, ...]
    prior_versions: tuple[PriorVersionRecord, ...]


@dataclass(frozen=True)
class QrelCheckReport:
    checked_qrel_count: int
    mismatch_count: int
    mismatches: tuple[str, ...]


def normalize_regulation_ref(value: str) -> str:
    """Return the single canonical form shared by corpus and query identifiers."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def load_corpus(
    condition: ParserCondition = "composite", *, root: Path = Path(".")
) -> tuple[list[CorpusDoc], LoadReport]:
    """Load synthetic documents, current rules, and temporal prior versions."""
    if condition not in _PARSER_CONDITIONS:
        raise ValueError(f"unsupported parser condition: {condition}")

    service_names, service_names_source = _load_service_names(root)
    service_re = service_pattern(service_names)
    regulation_names = _load_regulation_names(root)
    skipped: list[tuple[str, str]] = []

    synthetic, zero_sections = _load_synthetic_documents(
        root,
        service_re=service_re,
        regulation_names=regulation_names,
        skipped=skipped,
    )
    targets = _load_current_targets(root)
    parse_runs = _latest_parse_runs(root)
    current: list[CorpusDoc] = []
    fallback_count = 0
    for target_index, target in enumerate(targets):
        raw_serial = target.get("law_serial")
        doc_id = (
            f"law-{raw_serial}"
            if isinstance(raw_serial, str) and raw_serial
            else f"current-target-{target_index}"
        )
        try:
            document, document_fallbacks = _load_current_document(
                root,
                target,
                condition=condition,
                parse_runs=parse_runs,
                service_re=service_re,
                regulation_names=regulation_names,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            skipped.append((doc_id, _safe_failure_reason(exc)))
            continue
        current.append(document)
        fallback_count += document_fallbacks

    prior, prior_records, missing_prior = _load_prior_documents(
        root,
        targets=targets,
        service_re=service_re,
        regulation_names=regulation_names,
        skipped=skipped,
    )
    documents = [*synthetic, *current, *prior]
    if not documents:
        raise CorpusLoadError("로드된 문서가 0건입니다.")

    identifier_counts = {
        kind: sum(bool(document.identifiers[kind]) for document in documents)
        for kind in _IDENTIFIER_KINDS
    }
    temporal_count = len(_temporal_current_doc_ids(root))
    report = LoadReport(
        condition=condition,
        synthetic_document_count=len(synthetic),
        expected_current_real_document_count=len(targets),
        loaded_current_real_document_count=len(current),
        expected_prior_real_document_count=temporal_count,
        loaded_prior_real_document_count=len(prior),
        composite_annex_fallback_count=fallback_count,
        synthetic_zero_heading_section_document_count_by_type=dict(
            sorted(zero_sections.items())
        ),
        identifier_extracted_document_count_by_kind=dict(
            sorted(identifier_counts.items())
        ),
        service_names_source=service_names_source,
        skipped=tuple(skipped),
        missing_prior_current_doc_ids=tuple(missing_prior),
        prior_versions=tuple(prior_records),
    )
    return documents, report


def write_prior_versions_manifest(
    versions: Sequence[PriorVersionRecord], *, root: Path = Path(".")
) -> Path:
    """Write IDs and dates for temporal prior versions, never their source text."""
    payload: dict[str, object] = {
        "generated_at": now_iso(),
        "definition": (
            "temporal:revision 골드 문항의 현행 규정별 발령일 바로 전 법제처 버전"
        ),
        "version_count": len(versions),
        "versions": [asdict(version) for version in versions],
    }
    gold_path = root / "data/golden/queries/gold.jsonl"
    if gold_path.is_file():
        payload["gold_queries_sha256"] = sha256_file(gold_path)
    destination = root / _PRIOR_MANIFEST_PATH
    _write_json_atomic(destination, payload)
    return destination


def check_synthetic_qrels(
    documents: Sequence[CorpusDoc], *, root: Path = Path(".")
) -> QrelCheckReport:
    """Verify every synthetic gold/silver qrel against its immutable body span."""
    by_id = {document.doc_id: document for document in documents}
    checked = 0
    mismatches: list[str] = []
    for set_name in ("gold", "silver"):
        path = root / f"data/golden/queries/{set_name}.jsonl"
        for query in _load_json_lines(path, missing_ok=True):
            query_id = str(query.get("id", ""))
            qrels = query.get("qrels", ())
            if not isinstance(qrels, Sequence) or isinstance(qrels, (str, bytes)):
                mismatches.append(f"{set_name}:{query_id}:qrels_not_sequence")
                continue
            for qrel_index, qrel in enumerate(qrels):
                if not isinstance(qrel, Mapping):
                    continue
                doc_id = qrel.get("doc_id")
                if not isinstance(doc_id, str) or not doc_id.startswith("syn-"):
                    continue
                checked += 1
                label = f"{set_name}:{query_id}:{qrel_index}:{doc_id}"
                document = by_id.get(doc_id)
                start = qrel.get("start")
                end = qrel.get("end")
                expected = qrel.get("span_sha256")
                if document is None:
                    mismatches.append(f"{label}:document_missing")
                elif start is None and end is None and expected is None:
                    # Human-confirmed document-level qrel overrides intentionally
                    # have no fact span.  Their document existence is still checked.
                    continue
                elif (
                    not _is_int(start)
                    or not _is_int(end)
                    or not isinstance(expected, str)
                ):
                    mismatches.append(f"{label}:span_metadata_invalid")
                elif start < 0 or end < start or end > len(document.text):
                    mismatches.append(f"{label}:span_out_of_range")
                else:
                    actual = hashlib.sha256(
                        document.text[start:end].encode("utf-8")
                    ).hexdigest()
                    if actual != expected:
                        mismatches.append(f"{label}:sha256_mismatch")
    return QrelCheckReport(
        checked_qrel_count=checked,
        mismatch_count=len(mismatches),
        mismatches=tuple(mismatches),
    )


def _load_synthetic_documents(
    root: Path,
    *,
    service_re: re.Pattern[str],
    regulation_names: Sequence[str],
    skipped: list[tuple[str, str]],
) -> tuple[list[CorpusDoc], Counter[str]]:
    documents: list[CorpusDoc] = []
    zero_sections: Counter[str] = Counter()
    for path in sorted((root / "data/synthetic/docs").glob("syn-*.json")):
        try:
            payload = _load_json_object(path)
            doc_id = _required_text(payload, "doc_id")
            doc_type = _required_text(payload, "doc_type")
            title = _required_text(payload, "title")
            body = _required_text(payload, "body", allow_empty=True)
            raw_access = payload.get("access")
            if not isinstance(raw_access, Mapping):
                raise TypeError("access must be an object")
            access = _load_access(raw_access)
            sections = _synthetic_sections(body, title=title, doc_type=doc_type)
            if not sections:
                zero_sections[doc_type] += 1
                sections = (Section("preamble", None, (title,), 0, len(body)),)
            else:
                zero_sections[doc_type] += 0
            declared_refs = payload.get("regulation_refs", ())
            documents.append(
                CorpusDoc(
                    doc_id=doc_id,
                    source="synthetic",
                    doc_type=doc_type,
                    title=title,
                    text=body,
                    sections=sections,
                    access=access,
                    team=_optional_text(payload.get("team")),
                    date=_optional_text(payload.get("date")),
                    rule_id=None,
                    is_current=True,
                    superseded_by=None,
                    identifiers=_extract_document_identifiers(
                        body,
                        service_re=service_re,
                        regulation_names=regulation_names,
                        declared_regulation_refs=declared_refs,
                    ),
                )
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            skipped.append((path.stem, _safe_failure_reason(exc)))
    return documents, zero_sections


def _synthetic_sections(text: str, *, title: str, doc_type: str) -> tuple[Section, ...]:
    headings = [
        (
            match.start(),
            len(match.group("marks")),
            match.group("title"),
        )
        for match in _MARKDOWN_HEADING_RE.finditer(text)
    ]
    selected: list[tuple[int, int, str, tuple[str, ...], str]] = []
    meeting_parent: str | None = None
    for start, level, heading in headings:
        if level <= 2:
            meeting_parent = (
                heading
                if level == 2 and _MEETING_PARENT_RE.fullmatch(heading)
                else None
            )
        if doc_type == "meeting":
            if (
                level == 3
                and meeting_parent is not None
                and _MEETING_AGENDA_RE.match(heading)
            ):
                selected.append(
                    (start, level, heading, (title, meeting_parent, heading), "agenda")
                )
        elif doc_type == "incident":
            if level == 2 and _INCIDENT_SECTION_RE.match(heading):
                selected.append(
                    (start, level, heading, (title, heading), "heading_section")
                )
        elif level == 2:
            selected.append(
                (start, level, heading, (title, heading), "heading_section")
            )

    sections: list[Section] = []
    for start, level, _heading, path, kind in selected:
        end = len(text)
        for candidate_start, candidate_level, _ in headings:
            if candidate_start > start and candidate_level <= level:
                end = candidate_start
                break
        sections.append(Section(kind, None, path, start, end))
    return tuple(sections)


def _load_current_targets(root: Path) -> list[Mapping[str, object]]:
    path = root / "data/manifest/parse_targets.json"
    if not path.is_file():
        return []
    payload = _load_json_object(path)
    raw_targets = payload.get("targets", ())
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        raise CorpusLoadError("parse_targets.json targets가 배열이 아닙니다.")
    targets: list[Mapping[str, object]] = []
    for target in raw_targets:
        if not isinstance(target, Mapping):
            raise CorpusLoadError("parse_targets.json target이 객체가 아닙니다.")
        if target.get("role") == "current":
            targets.append(target)
    return targets


def _latest_parse_runs(
    root: Path,
) -> dict[tuple[object, object], Mapping[str, object]]:
    records = load_records(root / "data/manifest/parse_runs.jsonl")
    return {
        (record.get("parser"), record.get("target_key")): record
        for record in latest_records(
            records, key=lambda record: (record.get("parser"), record.get("target_key"))
        )
    }


def _load_current_document(
    root: Path,
    target: Mapping[str, object],
    *,
    condition: ParserCondition,
    parse_runs: Mapping[tuple[object, object], Mapping[str, object]],
    service_re: re.Pattern[str],
    regulation_names: Sequence[str],
) -> tuple[CorpusDoc, int]:
    rule_id = _required_text(target, "law_rule_id")
    serial = _required_text(target, "law_serial")
    source_sha256 = _required_text(target, "source_sha256")
    law = _load_json_object(root / f"data/interim/law/{rule_id}/{serial}.json")
    title = _law_title(law)
    target_key = f"{rule_id}:{serial}:{source_sha256}"
    required_parsers = (
        ("pypdf", "upstage") if condition == "composite" else (condition,)
    )
    parsed = {
        parser: _load_parsed_document(
            root,
            parser=parser,
            target_key=target_key,
            record=parse_runs.get((parser, target_key)),
        )
        for parser in required_parsers
    }
    if condition == "composite":
        text, sections, fallback_count = _composite_text_and_sections(
            parsed["pypdf"], parsed["upstage"], title=title
        )
    else:
        text, sections = _parser_text_and_sections(parsed[condition], title=title)
        fallback_count = 0
    identifiers = _extract_document_identifiers(
        text,
        service_re=service_re,
        regulation_names=regulation_names,
    )
    identifiers["regulation"] = _unique(
        (
            *identifiers["regulation"],
            *_self_regulation_identifiers(title, sections),
        )
    )
    return (
        CorpusDoc(
            doc_id=f"law-{serial}",
            source="real",
            doc_type="regulation",
            title=title,
            text=text,
            sections=sections,
            access=Access("public", (), 0),
            team=None,
            date=_optional_text(law.get("issued_date"))
            or _optional_text(target.get("issued_date")),
            rule_id=rule_id,
            is_current=True,
            superseded_by=None,
            identifiers=identifiers,
        ),
        fallback_count,
    )


def _load_parsed_document(
    root: Path,
    *,
    parser: str,
    target_key: str,
    record: Mapping[str, object] | None,
) -> ParsedDocument:
    if record is None:
        raise ValueError(f"{parser}:parse_run_missing")
    if record.get("status") not in {"ok", "skipped"}:
        raise ValueError(f"{parser}:parse_status_{record.get('status')}")
    input_sha256 = record.get("input_sha256")
    settings_hash = record.get("settings_hash")
    if not isinstance(input_sha256, str) or not input_sha256:
        raise ValueError(f"{parser}:input_sha256_missing")
    if not isinstance(settings_hash, str) or not settings_hash:
        raise ValueError(f"{parser}:settings_hash_missing")
    document = from_json(
        output_path(root, parser, input_sha256).read_text(encoding="utf-8")
    )
    if (
        document.parser != parser
        or document.input_sha256 != input_sha256
        or document.settings_hash != settings_hash
        or record.get("target_key") != target_key
    ):
        raise ValueError(f"{parser}:parse_output_identity_mismatch")
    return document


def _composite_text_and_sections(
    pypdf: ParsedDocument, upstage: ParsedDocument, *, title: str
) -> tuple[str, tuple[Section, ...], int]:
    base = article_region_text(pypdf)
    sections = list(
        _regulation_sections(
            base,
            title=title,
            annexes=(),
            split_result=split_document(pypdf),
        )
    )
    mapping = map_annex_tables(pypdf, upstage)
    regions = _unique_annex_regions(annex_regions(pypdf))
    text = base
    for region in regions:
        annex_text = (
            mapping.matched[region.annex_id].text
            if region.annex_id in mapping.matched
            else region.text
        )
        if text:
            text += "\n"
        start = len(text)
        text += annex_text
        sections.append(
            Section(
                "annex",
                region.annex_id,
                (title, region.label),
                start,
                len(text),
            )
        )
    return text, tuple(sections), len(mapping.fallback)


def _parser_text_and_sections(
    document: ParsedDocument, *, title: str
) -> tuple[str, tuple[Section, ...]]:
    text = "\n".join(block.text for block in document.blocks)
    regions = annex_regions(document)
    starts = _block_starts(document)
    annex_spans = tuple(
        (
            starts[region.block_start],
            starts[region.block_start] + len(region.text),
            region,
        )
        for region in regions
    )
    return text, _regulation_sections(
        text,
        title=title,
        annexes=annex_spans,
        split_result=split_document(document),
    )


def _regulation_sections(
    text: str,
    *,
    title: str,
    annexes: Sequence[tuple[int, int, AnnexRegion]],
    split_result: SplitResult,
) -> tuple[Section, ...]:
    line_rows = _line_rows(text)
    annex_ranges = tuple((start, end) for start, end, _ in annexes)

    def in_annex(position: int) -> bool:
        return any(start <= position < end for start, end in annex_ranges)

    headings: list[tuple[int, str]] = []
    articles: list[tuple[int, re.Match[str]]] = []
    addenda: list[tuple[int, str]] = []
    for start, line in line_rows:
        if in_annex(start):
            continue
        if HEADING_RE.match(line):
            headings.append((start, line))
        article_match = ARTICLE_RE.match(line)
        if article_match is not None:
            articles.append((start, article_match))
        if ADDENDUM_RE.match(line):
            addenda.append((start, line))

    first_addendum = addenda[0][0] if addenda else len(text) + 1
    main_articles = [
        (start, match) for start, match in articles if start < first_addendum
    ]
    structural_positions = sorted(
        {
            *(start for start, _ in headings),
            *(start for start, _ in main_articles),
            *(start for start, _ in addenda),
            *(start for start, _, _ in annexes),
            len(text),
        }
    )
    sections: list[Section] = []
    first_structure = min(structural_positions, default=len(text))
    if first_structure > 0 and text[:first_structure].strip():
        sections.append(Section("preamble", None, (title,), 0, first_structure))

    splitter_articles = split_result.articles
    for article_index, (start, match) in enumerate(main_articles):
        end = _next_position(structural_positions, start, len(text))
        splitter_article = _matching_splitter_article(
            splitter_articles, article_index, match
        )
        number = (
            splitter_article.number
            if splitter_article is not None
            else int(match.group("number"))
        )
        branch = (
            splitter_article.branch
            if splitter_article is not None
            else article_branch(match)
        )
        key = f"제{number}조" + (f"의{branch}" if branch else "")
        article_title = (
            splitter_article.title
            if splitter_article is not None
            else match.group("paren_title") or match.group("bracket_title")
        )
        article_label = key + (f"({article_title.strip()})" if article_title else "")
        chapter = _last_heading_before(
            [row for row in headings if _CHAPTER_RE.match(row[1])], start
        )
        path = (
            (title, article_label)
            if chapter is None
            else (title, chapter, article_label)
        )
        sections.append(Section("article", key, path, start, end))

    for index, (start, heading) in enumerate(addenda):
        candidates = [len(text)]
        if index + 1 < len(addenda):
            candidates.append(addenda[index + 1][0])
        candidates.extend(
            annex_start for annex_start, _, _ in annexes if annex_start > start
        )
        sections.append(
            Section("addendum", None, (title, heading), start, min(candidates))
        )
    sections.extend(
        Section("annex", region.annex_id, (title, region.label), start, end)
        for start, end, region in annexes
    )
    return tuple(sorted(sections, key=lambda section: (section.start, section.end)))


def _load_prior_documents(
    root: Path,
    *,
    targets: Sequence[Mapping[str, object]],
    service_re: re.Pattern[str],
    regulation_names: Sequence[str],
    skipped: list[tuple[str, str]],
) -> tuple[list[CorpusDoc], list[PriorVersionRecord], list[str]]:
    target_by_serial = {
        serial: target
        for target in targets
        if isinstance((serial := target.get("law_serial")), str) and serial
    }
    documents: list[CorpusDoc] = []
    records: list[PriorVersionRecord] = []
    missing: list[str] = []
    for current_doc_id in _temporal_current_doc_ids(root):
        current_serial = current_doc_id.removeprefix("law-")
        target = target_by_serial.get(current_serial)
        if target is None:
            missing.append(current_doc_id)
            skipped.append((current_doc_id, "prior_target_missing"))
            continue
        rule_id = _required_text(target, "law_rule_id")
        try:
            versions = [
                _load_json_object(path)
                for path in sorted(
                    (root / f"data/interim/law/{rule_id}").glob("*.json")
                )
            ]
            current_law = next(
                version
                for version in versions
                if str(version.get("serial")) == current_serial
            )
            current_date = _required_text(current_law, "issued_date")
            candidates = [
                version
                for version in versions
                if isinstance(version.get("issued_date"), str)
                and str(version["issued_date"]) < current_date
            ]
            if not candidates:
                raise LookupError("prior_version_missing")
            prior_law = max(
                candidates,
                key=lambda version: (
                    str(version["issued_date"]),
                    str(version.get("serial", "")),
                ),
            )
            prior_serial = _required_text(prior_law, "serial")
            prior_date = _required_text(prior_law, "issued_date")
            document = _law_api_document(
                prior_law,
                superseded_by=current_doc_id,
                service_re=service_re,
                regulation_names=regulation_names,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            StopIteration,
            LookupError,
            json.JSONDecodeError,
        ) as exc:
            missing.append(current_doc_id)
            skipped.append((current_doc_id, _safe_failure_reason(exc)))
            continue
        documents.append(document)
        records.append(
            PriorVersionRecord(
                rule_id=rule_id,
                current_serial=current_serial,
                current_issued_date=current_date,
                prior_serial=prior_serial,
                prior_issued_date=prior_date,
            )
        )
    return documents, records, missing


def _law_api_document(
    law: Mapping[str, object],
    *,
    superseded_by: str,
    service_re: re.Pattern[str],
    regulation_names: Sequence[str],
) -> CorpusDoc:
    title = _law_title(law)
    lines = law_to_lines(law)
    text = "\n".join(line for _, line in lines)
    sections = _regulation_sections(
        text,
        title=title,
        annexes=(),
        split_result=split_text(lines),
    )
    identifiers = _extract_document_identifiers(
        text, service_re=service_re, regulation_names=regulation_names
    )
    identifiers["regulation"] = _unique(
        (*identifiers["regulation"], *_self_regulation_identifiers(title, sections))
    )
    serial = _required_text(law, "serial")
    return CorpusDoc(
        doc_id=f"law-{serial}",
        source="real",
        doc_type="regulation",
        title=title,
        text=text,
        sections=sections,
        access=Access("public", (), 0),
        team=None,
        date=_required_text(law, "issued_date"),
        rule_id=_required_text(law, "rule_id"),
        is_current=False,
        superseded_by=superseded_by,
        identifiers=identifiers,
    )


def _temporal_current_doc_ids(root: Path) -> list[str]:
    path = root / "data/golden/queries/gold.jsonl"
    doc_ids: list[str] = []
    for query in _load_json_lines(path, missing_ok=True):
        tags = query.get("tags", ())
        if not isinstance(tags, Sequence) or "temporal:revision" not in tags:
            continue
        qrels = query.get("qrels", ())
        if not isinstance(qrels, Sequence) or isinstance(qrels, (str, bytes)):
            continue
        for qrel in qrels:
            if not isinstance(qrel, Mapping):
                continue
            doc_id = qrel.get("doc_id")
            if isinstance(doc_id, str) and doc_id.startswith("law-"):
                doc_ids.append(doc_id)
                break
    return list(dict.fromkeys(doc_ids))


def _extract_document_identifiers(
    text: str,
    *,
    service_re: re.Pattern[str],
    regulation_names: Sequence[str],
    declared_regulation_refs: object = (),
) -> dict[str, tuple[str, ...]]:
    extracted = extract_identifiers(text)
    result = {
        kind: _unique(item[0] for item in extracted[kind])
        for kind in ("ticket", "approval", "incident")
    }
    result["service"] = _unique(match.group() for match in service_re.finditer(text))
    regulation_refs: list[str] = []
    if isinstance(declared_regulation_refs, Sequence) and not isinstance(
        declared_regulation_refs, (str, bytes)
    ):
        regulation_refs.extend(
            normalize_regulation_ref(value)
            for value in declared_regulation_refs
            if isinstance(value, str)
        )
    known_pattern = _regulation_pattern(regulation_names)
    if known_pattern is not None:
        regulation_refs.extend(
            normalize_regulation_ref(match.group())
            for match in known_pattern.finditer(text)
        )
    else:
        regulation_refs.extend(
            _fallback_regulation_ref(match)
            for match in REGULATION_REF_RE.finditer(text)
        )
    result["regulation"] = _unique(regulation_refs)
    return {kind: result[kind] for kind in _IDENTIFIER_KINDS}


def _regulation_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    alternatives = []
    for name in sorted(set(names), key=lambda value: (-len(value), value)):
        alternatives.append(r"\s*".join(re.escape(part) for part in name.split()))
    return re.compile(
        rf"(?<![가-힣A-Za-z0-9·])(?:{'|'.join(alternatives)})"
        r"\s*제\s*\d+\s*조(?:\s*의\s*\d+)?"
    )


def _fallback_regulation_ref(match: re.Match[str]) -> str:
    """Render a generic regex match without a preceding lowercase identifier."""
    name = match.group("name").strip()
    name = re.sub(r"^(?:[a-z0-9·]+\s+)+", "", name)
    branch = match.group("branch")
    value = f"{name} 제{match.group('num')}조"
    if branch is not None:
        value += f"의{branch}"
    return normalize_regulation_ref(value)


def _self_regulation_identifiers(
    title: str, sections: Sequence[Section]
) -> tuple[str, ...]:
    return _unique(
        normalize_regulation_ref(f"{title} {section.key}")
        for section in sections
        if section.kind == "article"
        and section.key is not None
        and _ARTICLE_KEY_RE.fullmatch(section.key)
    )


def _load_service_names(root: Path) -> tuple[tuple[str, ...], str]:
    database = root / "data/synthetic/ledger.sqlite"
    if database.is_file():
        uri = f"file:{database.resolve()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                names = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT service FROM services ORDER BY service"
                    )
                )
            return names, "data/synthetic/ledger.sqlite:services"
        except sqlite3.Error:
            pass
    path = root / "data/seed/services.yaml"
    if not path.is_file():
        return (), "none"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_services = payload.get("services", ()) if isinstance(payload, Mapping) else ()
    names = tuple(
        str(service["name"])
        for service in raw_services
        if isinstance(service, Mapping) and isinstance(service.get("name"), str)
    )
    return tuple(sorted(set(names))), "data/seed/services.yaml"


def _load_regulation_names(root: Path) -> tuple[str, ...]:
    path = root / "data/manifest/regulation_refs.json"
    if not path.is_file():
        return ()
    payload = _load_json_object(path)
    rules = payload.get("rules", {})
    if not isinstance(rules, Mapping):
        return ()
    return tuple(str(name) for name in rules if isinstance(name, str))


def _load_access(value: Mapping[object, object]) -> Access:
    level = value.get("level")
    teams = value.get("teams")
    min_grade = value.get("min_grade")
    if level not in {"public", "team", "restricted"}:
        raise ValueError("invalid access level")
    if not isinstance(teams, Sequence) or isinstance(teams, (str, bytes)):
        raise TypeError("access teams must be a sequence")
    if not _is_int(min_grade):
        raise TypeError("access min_grade must be an integer")
    return Access(str(level), tuple(str(team) for team in teams), min_grade)


def _law_title(law: Mapping[str, object]) -> str:
    title = law.get("name_without_agency") or law.get("name")
    if not isinstance(title, str) or not title:
        raise ValueError("law name is missing")
    return title


def _unique_annex_regions(regions: Sequence[AnnexRegion]) -> list[AnnexRegion]:
    seen: set[str] = set()
    unique: list[AnnexRegion] = []
    for region in regions:
        if region.annex_id not in seen:
            unique.append(region)
            seen.add(region.annex_id)
    return unique


def _block_starts(document: ParsedDocument) -> tuple[int, ...]:
    starts: list[int] = []
    cursor = 0
    for index, block in enumerate(document.blocks):
        starts.append(cursor)
        cursor += len(block.text)
        if index + 1 < len(document.blocks):
            cursor += 1
    return tuple(starts)


def _line_rows(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        rows.append((cursor, line))
        cursor += len(raw_line)
    if not rows and text == "":
        return []
    if cursor < len(text):
        rows.append((cursor, text[cursor:]))
    return rows


def _last_heading_before(
    headings: Sequence[tuple[int, str]], position: int
) -> str | None:
    found = [heading for start, heading in headings if start < position]
    return found[-1] if found else None


def _matching_splitter_article(
    articles: Sequence[Article], index: int, match: re.Match[str]
) -> Article | None:
    if index >= len(articles):
        return None
    article = articles[index]
    if article.number == int(
        match.group("number")
    ) and article.branch == article_branch(match):
        return article
    return None


def _next_position(positions: Sequence[int], start: int, default: int) -> int:
    return next((position for position in positions if position > start), default)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _load_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _load_json_lines(
    path: Path, *, missing_ok: bool = False
) -> list[Mapping[str, object]]:
    if missing_ok and not path.is_file():
        return []
    values: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise TypeError(f"JSONL object required: {path}")
        values.append(value)
    return values


def _required_text(
    value: Mapping[object, object], key: str, *, allow_empty: bool = False
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or (not allow_empty and not item):
        raise ValueError(f"{key} must be text")
    return item


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_failure_reason(exc: Exception) -> str:
    message = str(exc).splitlines()[0][:120]
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__


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


def _print_report(report: LoadReport, *, out: TextIO) -> None:
    print(
        f"condition={report.condition} synthetic={report.synthetic_document_count} "
        f"current_real={report.loaded_current_real_document_count}/"
        f"{report.expected_current_real_document_count} "
        f"prior_real={report.loaded_prior_real_document_count}/"
        f"{report.expected_prior_real_document_count}",
        file=out,
    )
    print(
        f"annex_fallback={report.composite_annex_fallback_count} "
        f"zero_heading_sections="
        f"{report.synthetic_zero_heading_section_document_count_by_type} "
        f"service_names_source={report.service_names_source}",
        file=out,
    )
    print(
        f"identifier_documents={report.identifier_extracted_document_count_by_kind} "
        f"skipped={len(report.skipped)}",
        file=out,
    )


def main(argv: list[str] | None = None) -> int:
    """Load the corpus and optionally run all local M5 integrity checks."""
    parser = argparse.ArgumentParser(
        description="M5 로컬 코퍼스를 결정론적으로 로드합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--condition", choices=sorted(_PARSER_CONDITIONS), default="composite"
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        documents, report = load_corpus(args.condition, root=args.root)
        destination = write_prior_versions_manifest(
            report.prior_versions, root=args.root
        )
        _print_report(report, out=sys.stdout)
        print(f"prior_manifest={destination}", file=sys.stdout)
        failed = False
        if (
            report.loaded_current_real_document_count
            != report.expected_current_real_document_count
        ):
            print(
                "ERROR: 현행 실제 문서 로드 수가 대상 수와 다릅니다: "
                f"{report.loaded_current_real_document_count}/"
                f"{report.expected_current_real_document_count}",
                file=sys.stderr,
            )
            failed = True
        if report.expected_prior_real_document_count != 5:
            print(
                "ERROR: temporal:revision 현행 정답 문서가 5건이 아닙니다: "
                f"{report.expected_prior_real_document_count}건",
                file=sys.stderr,
            )
            failed = True
        if report.loaded_prior_real_document_count != 5:
            print(
                "ERROR: 개정 전 직전 버전이 5건 모두 로드되지 않았습니다: "
                f"{report.loaded_prior_real_document_count}/5",
                file=sys.stderr,
            )
            failed = True
        if args.check:
            qrel_check = check_synthetic_qrels(documents, root=args.root)
            print(
                f"synthetic_qrels_checked={qrel_check.checked_qrel_count} "
                f"mismatches={qrel_check.mismatch_count}",
                file=sys.stdout,
            )
            for mismatch in qrel_check.mismatches:
                print(f"ERROR: {mismatch}", file=sys.stderr)
            failed = failed or bool(qrel_check.mismatch_count)
        return int(failed)
    except (
        CorpusLoadError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        print(f"코퍼스 로드 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
