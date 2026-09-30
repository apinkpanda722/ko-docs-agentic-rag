"""Map pypdf annex regions to Upstage tables without changing source text.

The mapping rule was fixed after an FP-016 pass over all M2 parse targets.  A
label starts an annex until the next annex label, addendum, or article head.
Upstage's full-width angle labels (``〈별지 …〉``) are accepted in addition to
``ANNEX_RE``.  Editorial references using ``〔…〕`` and bracketed labels followed
by the particles ``의`` or ``에`` are not boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.parsing.articles import ADDENDUM_RE, ANNEX_RE, ARTICLE_RE
from kodocs.parsing.schema import ParsedDocument, ParsedTable, from_json, output_path
from kodocs.parsing.tables import cells_to_grid, grid_to_markdown

RULE_VERSION = "m5-annex-v1"
DEFAULT_MANIFEST = Path("data/manifest/m5_annex_mapping.json")
_NORMAL_NO_TABLE_KINDS = frozenset({"별지", "서식"})
_CLOSERS = {"[": "]", "【": "】", "<": ">", "〔": "〕"}
_REFERENCE_SUFFIX_RE = re.compile(r"^(?:의|에)(?:\s|$)")
_NUMBER_RE = re.compile(
    r"(?:별\s*표|별\s*지|서\s*식|부\s*록)\s*"
    r"(?:제\s*)?(?P<number>\d+)\s*"
    r"(?:(?P<branch_mark>의|-)\s*(?P<branch_before>\d+)\s*)?"
    r"(?:호\s*(?:의\s*(?P<branch_after>\d+)\s*)?)?"
)


class AnnexMappingError(RuntimeError):
    """The local parse corpus cannot produce a valid annex mapping manifest."""


@dataclass(frozen=True)
class AnnexRegion:
    """One raw pypdf annex region."""

    annex_id: str
    kind: str
    label: str
    block_start: int
    block_end: int
    text: str


@dataclass(frozen=True)
class AnnexTable:
    """Ordered Upstage tables associated with one normalized annex label."""

    annex_id: str
    tables: tuple[ParsedTable, ...]
    text: str


@dataclass(frozen=True)
class AnnexMapping:
    """Successful mappings plus isolated fallbacks and unassigned tables."""

    matched: dict[str, AnnexTable]
    fallback: list[str]
    unmapped_tables: list[int]


@dataclass(frozen=True)
class _Label:
    annex_id: str
    kind: str
    label: str
    bracket_style: str
    numbering_style: str


@dataclass(frozen=True)
class _LabelScan:
    label: _Label | None
    excluded_reason: str | None = None
    bracket_style: str | None = None
    numbering_style: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class _MappingDetails:
    mapping: AnnexMapping
    fallback_reasons: Mapping[str, str]
    normal_no_table: tuple[str, ...]
    normal_no_table_reasons: Mapping[str, str]
    unmapped_reasons: Mapping[int, str]
    upstage_label_ids: frozenset[str]
    matched_table_indexes: Mapping[str, tuple[int, ...]]


def annex_regions(pypdf_doc: ParsedDocument) -> list[AnnexRegion]:
    """Return raw pypdf annex regions delimited by structural boundaries."""
    regions: list[AnnexRegion] = []
    active: tuple[int, _Label] | None = None

    def finish(end: int) -> None:
        nonlocal active
        if active is None:
            return
        start, label = active
        regions.append(
            AnnexRegion(
                annex_id=label.annex_id,
                kind=label.kind,
                label=label.label,
                block_start=start,
                block_end=end,
                text="\n".join(block.text for block in pypdf_doc.blocks[start:end]),
            )
        )
        active = None

    for block_index, block in enumerate(pypdf_doc.blocks):
        label = _block_label(block.text)
        if label is not None:
            finish(block_index)
            active = (block_index, label)
        elif active is not None and _is_structural_boundary(block.text):
            finish(block_index)
    finish(len(pypdf_doc.blocks))
    return regions


def article_region_text(pypdf_doc: ParsedDocument) -> str:
    """Join original pypdf blocks after excluding every detected annex region."""
    excluded: set[int] = set()
    for region in annex_regions(pypdf_doc):
        excluded.update(range(region.block_start, region.block_end))
    return "\n".join(
        block.text
        for index, block in enumerate(pypdf_doc.blocks)
        if index not in excluded
    )


def map_annex_tables(
    pypdf_doc: ParsedDocument,
    upstage_doc: ParsedDocument,
) -> AnnexMapping:
    """Map valid Upstage table blocks to pypdf annex identifiers."""
    return _map_details(pypdf_doc, upstage_doc).mapping


def run(root: Path, *, out: TextIO = sys.stdout) -> dict[str, object]:
    """Analyze all locally paired M2 outputs and write the text-free manifest."""
    targets_path = root / "data/manifest/parse_targets.json"
    runs_path = root / "data/manifest/parse_runs.jsonl"
    table_golden_path = root / "data/manifest/table_golden.json"
    gold_queries_path = root / "data/golden/queries/gold.jsonl"
    targets_payload = _load_json_object(targets_path)
    targets = targets_payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise AnnexMappingError("parse_targets.json targets가 비어 있습니다.")

    # Use the same file hashing and target order as the shared parser runner.
    from kodocs.parsing.run import load_inputs

    inputs = load_inputs(root)
    if len(inputs) != len(targets):
        raise AnnexMappingError("parse_targets와 load_inputs 대상 수가 다릅니다.")
    latest_runs = _latest_parse_runs(runs_path)

    aggregate = Counter()
    role_counts = Counter()
    paired_role_counts = Counter()
    fallback_rows: list[dict[str, object]] = []
    normal_rows: list[dict[str, object]] = []
    unmapped_rows: list[dict[str, object]] = []
    missing_rows: list[dict[str, str]] = []
    variant_stats = {
        "pypdf": _empty_variant_stats(),
        "upstage": _empty_variant_stats(),
    }
    paired_hash_parts: list[str] = []
    parser_settings: dict[str, set[str]] = {"pypdf": set(), "upstage": set()}
    details_by_doc: dict[str, tuple[_MappingDetails, ParsedDocument]] = {}

    for index, (raw_target, item) in enumerate(zip(targets, inputs, strict=True), 1):
        if not isinstance(raw_target, Mapping):
            raise AnnexMappingError(f"targets[{index - 1}]가 객체가 아닙니다.")
        role = _required_text(raw_target, "role", index - 1)
        serial = _required_text(raw_target, "law_serial", index - 1)
        doc_id = f"law-{serial}"
        role_counts[role] += 1

        documents: dict[str, ParsedDocument] = {}
        missing_reasons: list[str] = []
        for parser_name in ("pypdf", "upstage"):
            record = latest_runs.get((parser_name, item.target_key))
            document, reason = _load_current_parsed(
                root,
                parser=parser_name,
                input_sha256=item.input_sha256,
                record=record,
            )
            if document is None:
                missing_reasons.append(f"{parser_name}:{reason}")
            else:
                documents[parser_name] = document
        if missing_reasons:
            missing_rows.append({"doc_id": doc_id, "reason": ",".join(missing_reasons)})
            _print_progress(index, len(targets), aggregate, out=out)
            continue

        pypdf_doc = documents["pypdf"]
        upstage_doc = documents["upstage"]
        details = _map_details(pypdf_doc, upstage_doc)
        regions = annex_regions(pypdf_doc)
        unique_regions = _unique_regions(regions)
        details_by_doc[doc_id] = (details, upstage_doc)
        paired_role_counts[role] += 1
        paired_hash_parts.append(f"{doc_id}:{item.input_sha256}")
        parser_settings["pypdf"].add(pypdf_doc.settings_hash)
        parser_settings["upstage"].add(upstage_doc.settings_hash)

        aggregate["paired_targets"] += 1
        aggregate["pypdf_annex_labels"] += len(regions)
        aggregate["pypdf_unique_annexes"] += len(unique_regions)
        aggregate["duplicate_annex_labels"] += len(regions) - len(unique_regions)
        aggregate["upstage_tables"] += len(upstage_doc.tables)
        aggregate["matched_annexes"] += len(details.mapping.matched)
        aggregate["fallback_annexes"] += len(details.mapping.fallback)
        aggregate["normal_no_table_annexes"] += len(details.normal_no_table)
        aggregate["unmapped_tables"] += len(details.mapping.unmapped_tables)
        _merge_variant_stats(variant_stats["pypdf"], _label_variant_stats(pypdf_doc))
        _merge_variant_stats(
            variant_stats["upstage"], _label_variant_stats(upstage_doc)
        )

        for annex_id in details.mapping.fallback:
            fallback_rows.append(
                {
                    "doc_id": doc_id,
                    "annex_id": annex_id,
                    "reason": details.fallback_reasons[annex_id],
                }
            )
        for annex_id in details.normal_no_table:
            normal_rows.append(
                {
                    "doc_id": doc_id,
                    "annex_id": annex_id,
                    "reason": details.normal_no_table_reasons[annex_id],
                }
            )
        for table_index in details.mapping.unmapped_tables:
            unmapped_rows.append(
                {
                    "doc_id": doc_id,
                    "table_index": table_index,
                    "reason": details.unmapped_reasons[table_index],
                }
            )
        _print_progress(index, len(targets), aggregate, out=out)

    if aggregate["matched_annexes"] == 0:
        raise AnnexMappingError("매핑 성공 별표가 0건입니다.")

    gold_check = _check_gold_tables(
        table_golden_path=table_golden_path,
        gold_queries_path=gold_queries_path,
        details_by_doc=details_by_doc,
    )
    manifest: dict[str, object] = {
        "generated_at": now_iso(),
        "rule_version": RULE_VERSION,
        "target_denominator": {
            "definition": (
                "parse_targets targets 중 load_inputs로 계산한 PDF SHA-256의 "
                "pypdf·upstage 현재 파싱 결과가 모두 있는 문서"
            ),
            "declared_target_count": len(targets),
            "declared_role_counts": dict(sorted(role_counts.items())),
            "paired_target_count": aggregate["paired_targets"],
            "paired_role_counts": dict(sorted(paired_role_counts.items())),
            "missing_target_count": len(missing_rows),
        },
        "counts": {
            "pypdf_annex_label_count": aggregate["pypdf_annex_labels"],
            "pypdf_unique_annex_count": aggregate["pypdf_unique_annexes"],
            "duplicate_annex_label_count": aggregate["duplicate_annex_labels"],
            "upstage_table_count": aggregate["upstage_tables"],
            "matched_annex_count": aggregate["matched_annexes"],
            "fallback_annex_count": aggregate["fallback_annexes"],
            "normal_no_table_annex_count": aggregate["normal_no_table_annexes"],
            "unmapped_table_count": aggregate["unmapped_tables"],
        },
        "label_variants": _plain_variant_stats(variant_stats),
        "fallback_reason_counts": dict(
            sorted(Counter(row["reason"] for row in fallback_rows).items())
        ),
        "unmapped_table_reason_counts": dict(
            sorted(Counter(row["reason"] for row in unmapped_rows).items())
        ),
        "fallbacks": fallback_rows,
        "normal_no_table": normal_rows,
        "unmapped_tables": unmapped_rows,
        "missing_targets": missing_rows,
        "gold_table_qrels": gold_check,
        "input_hashes": {
            "parse_targets_sha256": sha256_file(targets_path),
            "parse_runs_sha256": sha256_file(runs_path),
            "table_golden_sha256": sha256_file(table_golden_path),
            "gold_queries_sha256": sha256_file(gold_queries_path),
            "paired_pdf_set_sha256": hashlib.sha256(
                "\n".join(sorted(paired_hash_parts)).encode()
            ).hexdigest(),
            "parser_settings_hashes": {
                parser_name: sorted(values)
                for parser_name, values in parser_settings.items()
            },
        },
    }
    destination = root / DEFAULT_MANIFEST
    _write_json_atomic(destination, manifest)
    print(
        f"manifest={destination} paired={aggregate['paired_targets']} "
        f"matched={aggregate['matched_annexes']} "
        f"fallback={aggregate['fallback_annexes']} "
        f"unmapped_tables={aggregate['unmapped_tables']}",
        file=out,
        flush=True,
    )
    if gold_check["fallback_count"]:
        raise AnnexMappingError(
            "골드 표 qrels 중 업스테이지 표 매핑 실패가 있습니다: "
            f"{gold_check['fallback_count']}건"
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the offline M5 annex mapping pass."""
    parser = argparse.ArgumentParser(
        description="pypdf 별표 영역과 업스테이지 표를 결정론적으로 매핑합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        run(args.root)
    except (
        AnnexMappingError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        print(f"별표 매핑 실패: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def _map_details(
    pypdf_doc: ParsedDocument,
    upstage_doc: ParsedDocument,
) -> _MappingDetails:
    table_positions: dict[int, list[tuple[int, ParsedTable]]] = defaultdict(list)
    for table_index, table in enumerate(upstage_doc.tables):
        table_positions[table.block_position].append((table_index, table))

    groups: dict[str, list[tuple[int, ParsedTable]]] = defaultdict(list)
    label_ids: set[str] = set()
    unmapped_reasons: dict[int, str] = {}
    current_annex: str | None = None
    label_seen = False
    for block_index, block in enumerate(upstage_doc.blocks):
        label = _block_label(block.text)
        if label is not None:
            current_annex = label.annex_id
            label_ids.add(label.annex_id)
            label_seen = True

        for table_index, table in table_positions.get(block_index, ()):
            if current_annex is None:
                reason = (
                    "before_first_annex_label"
                    if not label_seen
                    else "outside_annex_region"
                )
                unmapped_reasons[table_index] = reason
            else:
                groups[current_annex].append((table_index, table))

        if (
            label is None
            and block.kind != "table"
            and _is_structural_boundary(block.text)
        ):
            current_annex = None

    regions = _unique_regions(annex_regions(pypdf_doc))
    expected_ids = {region.annex_id for region in regions}
    matched: dict[str, AnnexTable] = {}
    matched_indexes: dict[str, tuple[int, ...]] = {}
    fallback: list[str] = []
    fallback_reasons: dict[str, str] = {}
    normal_no_table: list[str] = []
    normal_reasons: dict[str, str] = {}

    for region in regions:
        candidates = groups.get(region.annex_id, ())
        valid: list[tuple[int, ParsedTable, str]] = []
        for table_index, table in candidates:
            if table.invalid_reason is not None or not table.cells:
                unmapped_reasons[table_index] = "invalid_table"
                continue
            try:
                markdown = grid_to_markdown(cells_to_grid(table.cells))
            except (IndexError, TypeError, ValueError):
                unmapped_reasons[table_index] = "invalid_table"
                continue
            if not markdown:
                unmapped_reasons[table_index] = "invalid_table"
                continue
            valid.append((table_index, table, markdown))

        if valid:
            matched[region.annex_id] = AnnexTable(
                annex_id=region.annex_id,
                tables=tuple(table for _, table, _ in valid),
                text=f"[{region.annex_id}]\n"
                + "\n\n".join(markdown for _, _, markdown in valid),
            )
            matched_indexes[region.annex_id] = tuple(index for index, _, _ in valid)
            continue

        reason = (
            "invalid_table"
            if candidates
            else (
                "upstage_label_without_table"
                if region.annex_id in label_ids
                else "upstage_label_missing"
            )
        )
        if (
            region.kind in _NORMAL_NO_TABLE_KINDS
            and not candidates
            and region.annex_id in label_ids
        ):
            normal_no_table.append(region.annex_id)
            normal_reasons[region.annex_id] = "no_table_expected_for_form"
        else:
            fallback.append(region.annex_id)
            fallback_reasons[region.annex_id] = reason

    for annex_id, candidates in groups.items():
        if annex_id in expected_ids:
            continue
        for table_index, _ in candidates:
            unmapped_reasons.setdefault(table_index, "upstage_annex_missing_in_pypdf")

    unmapped_tables = sorted(unmapped_reasons)
    return _MappingDetails(
        mapping=AnnexMapping(
            matched=matched,
            fallback=fallback,
            unmapped_tables=unmapped_tables,
        ),
        fallback_reasons=fallback_reasons,
        normal_no_table=tuple(normal_no_table),
        normal_no_table_reasons=normal_reasons,
        unmapped_reasons=unmapped_reasons,
        upstage_label_ids=frozenset(label_ids),
        matched_table_indexes=matched_indexes,
    )


def _unique_regions(regions: Sequence[AnnexRegion]) -> list[AnnexRegion]:
    unique: list[AnnexRegion] = []
    seen: set[str] = set()
    for region in regions:
        if region.annex_id not in seen:
            unique.append(region)
            seen.add(region.annex_id)
    return unique


def _block_label(text: str) -> _Label | None:
    for line in text.splitlines() or [text]:
        scan = _scan_label_line(line)
        if scan.label is not None:
            return scan.label
    return None


def _scan_label_line(line: str) -> _LabelScan:
    candidate = line
    fullwidth_angle = bool(re.match(r"^\s*〈", line))
    if fullwidth_angle:
        candidate = line.replace("〈", "<", 1)
        close_index = candidate.find("〉")
        if close_index < 0:
            return _LabelScan(label=None)
        candidate = f"{candidate[:close_index]}>{candidate[close_index + 1 :]}"

    match = ANNEX_RE.match(candidate)
    if match is None or _CLOSERS[match.group("open")] != match.group("close"):
        return _LabelScan(label=None)
    raw_label = line[match.start("label") : match.end("label")]
    kind = re.sub(r"\s+", "", match.group("kind"))
    bracket_style = _bracket_style(raw_label)
    numbering_style, annex_id = _numbering(kind, raw_label)
    remainder = line[match.end("label") :]
    if match.group("open") == "〔":
        return _LabelScan(
            label=None,
            excluded_reason="editorial_reference_brackets",
            bracket_style=bracket_style,
            numbering_style=numbering_style,
            kind=kind,
        )
    if _REFERENCE_SUFFIX_RE.match(remainder):
        return _LabelScan(
            label=None,
            excluded_reason="particle_suffix_reference",
            bracket_style=bracket_style,
            numbering_style=numbering_style,
            kind=kind,
        )
    return _LabelScan(
        label=_Label(
            annex_id=annex_id,
            kind=kind,
            label=raw_label,
            bracket_style=bracket_style,
            numbering_style=numbering_style,
        ),
        bracket_style=bracket_style,
        numbering_style=numbering_style,
        kind=kind,
    )


def _numbering(kind: str, label: str) -> tuple[str, str]:
    match = _NUMBER_RE.search(label)
    if match is None:
        return "unnumbered", kind
    number = str(int(match.group("number")))
    branch = match.group("branch_before") or match.group("branch_after")
    if branch is None:
        return "simple", f"{kind}{number}"
    if match.group("branch_mark") == "-":
        style = "hyphen_branch"
    elif match.group("branch_after") is not None:
        style = "branch_after_ho"
    else:
        style = "branch_before_ho"
    return style, f"{kind}{number}의{int(branch)}"


def _bracket_style(label: str) -> str:
    opener = label.lstrip()[:1]
    return {
        "[": "square",
        "【": "corner",
        "<": "angle_ascii",
        "〈": "angle_fullwidth",
        "〔": "editorial",
    }.get(opener, "other")


def _is_structural_boundary(text: str) -> bool:
    return any(
        ARTICLE_RE.match(line) or ADDENDUM_RE.match(line)
        for line in (text.splitlines() or [text])
    )


def _empty_variant_stats() -> dict[str, Counter[str]]:
    return {
        "bracket_style": Counter(),
        "numbering_style": Counter(),
        "kind": Counter(),
        "excluded_references": Counter(),
    }


def _label_variant_stats(document: ParsedDocument) -> dict[str, Counter[str]]:
    stats = _empty_variant_stats()
    for block in document.blocks:
        for line in block.text.splitlines() or [block.text]:
            scan = _scan_label_line(line)
            if scan.label is not None:
                stats["bracket_style"][scan.label.bracket_style] += 1
                stats["numbering_style"][scan.label.numbering_style] += 1
                stats["kind"][scan.label.kind] += 1
            elif scan.excluded_reason is not None:
                stats["excluded_references"][scan.excluded_reason] += 1
    return stats


def _merge_variant_stats(
    destination: dict[str, Counter[str]],
    source: dict[str, Counter[str]],
) -> None:
    for category, values in source.items():
        destination[category].update(values)


def _plain_variant_stats(
    values: Mapping[str, Mapping[str, Counter[str]]],
) -> dict[str, dict[str, dict[str, int]]]:
    return {
        parser: {
            category: dict(sorted(counts.items()))
            for category, counts in categories.items()
        }
        for parser, categories in values.items()
    }


def _latest_parse_runs(path: Path) -> dict[tuple[str, str], Mapping[str, object]]:
    latest: dict[tuple[str, str], Mapping[str, object]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise AnnexMappingError("data/manifest/parse_runs.jsonl이 없습니다.") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise AnnexMappingError(
                f"parse_runs.jsonl {line_number}행이 객체가 아닙니다."
            )
        parser = value.get("parser")
        target_key = value.get("target_key")
        if isinstance(parser, str) and isinstance(target_key, str):
            latest[(parser, target_key)] = value
    return latest


def _load_current_parsed(
    root: Path,
    *,
    parser: str,
    input_sha256: str,
    record: Mapping[str, object] | None,
) -> tuple[ParsedDocument | None, str | None]:
    if not input_sha256:
        return None, "input_missing"
    if record is None:
        return None, "parse_run_missing"
    if record.get("status") not in {"ok", "skipped"}:
        return None, f"parse_status_{record.get('status')}"
    if record.get("input_sha256") != input_sha256:
        return None, "parse_run_input_sha256_mismatch"
    expected_settings = record.get("settings_hash")
    if not isinstance(expected_settings, str):
        return None, "parse_run_settings_hash_missing"
    path = output_path(root, parser, input_sha256)
    try:
        document = from_json(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None, "parse_output_missing_or_invalid"
    if document.parser != parser or document.input_sha256 != input_sha256:
        return None, "parse_output_identity_mismatch"
    if document.settings_hash != expected_settings:
        return None, "parse_output_settings_hash_mismatch"
    return document, None


def _check_gold_tables(
    *,
    table_golden_path: Path,
    gold_queries_path: Path,
    details_by_doc: Mapping[str, tuple[_MappingDetails, ParsedDocument]],
) -> dict[str, object]:
    table_payload = _load_json_object(table_golden_path)
    raw_tables = table_payload.get("tables")
    if not isinstance(raw_tables, list):
        raise AnnexMappingError("table_golden.json tables가 배열이 아닙니다.")
    tables: dict[str, Mapping[str, object]] = {}
    for index, value in enumerate(raw_tables):
        if not isinstance(value, Mapping):
            raise AnnexMappingError(f"table_golden tables[{index}]가 객체가 아닙니다.")
        table_id = _required_text(value, "id", index)
        tables[table_id] = value

    queries = _load_json_lines(gold_queries_path)
    checks: list[dict[str, object]] = []
    for query in queries:
        qrels = query.get("qrels")
        if not isinstance(qrels, list):
            continue
        for qrel in qrels:
            if not isinstance(qrel, Mapping) or qrel.get("annex_id") is None:
                continue
            query_id = str(query.get("id", ""))
            doc_id = str(qrel.get("doc_id", ""))
            golden_id = str(qrel.get("annex_id", ""))
            table = tables.get(golden_id)
            if table is None:
                raise AnnexMappingError(
                    f"골드 표 id {golden_id}가 manifest에 없습니다."
                )
            label_text = table.get("annex_label")
            if not isinstance(label_text, str):
                raise AnnexMappingError(f"골드 표 {golden_id} annex_label이 없습니다.")
            label = _block_label(label_text)
            if label is None:
                raise AnnexMappingError(
                    f"골드 표 {golden_id} 라벨을 정규화할 수 없습니다."
                )

            status = "mapping_missing"
            mapped_count = 0
            ordinal = table.get("table_ordinal")
            details_pair = details_by_doc.get(doc_id)
            if details_pair is not None:
                details, upstage_doc = details_pair
                mapped = details.mapping.matched.get(label.annex_id)
                mapped_count = 0 if mapped is None else len(mapped.tables)
                if label.annex_id in details.mapping.fallback:
                    status = "fallback"
                elif label.annex_id in details.normal_no_table:
                    status = "normal_no_table"
                elif mapped is not None:
                    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                        status = "table_ordinal_invalid"
                    elif ordinal not in details.matched_table_indexes.get(
                        label.annex_id, ()
                    ):
                        status = "table_not_in_mapped_annex"
                    elif ordinal >= len(upstage_doc.tables):
                        status = "table_ordinal_out_of_range"
                    elif table.get("pdf_sha256") != upstage_doc.input_sha256:
                        status = "pdf_sha256_mismatch"
                    else:
                        status = "matched"
            checks.append(
                {
                    "query_id": query_id,
                    "doc_id": doc_id,
                    "qrel_annex_id": golden_id,
                    "annex_id": label.annex_id,
                    "upstage_table_index": ordinal,
                    "mapped_table_count": mapped_count,
                    "status": status,
                }
            )
    if len(checks) != 6:
        raise AnnexMappingError(f"골드 표 qrels가 6건이 아닙니다: {len(checks)}건")
    fallback = [row for row in checks if row["status"] != "matched"]
    return {
        "expected_count": 6,
        "checked_count": len(checks),
        "matched_count": len(checks) - len(fallback),
        "fallback_count": len(fallback),
        "all_matched": not fallback,
        "checks": checks,
        "fallbacks": fallback,
    }


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnnexMappingError(f"필수 파일이 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise AnnexMappingError(f"JSON 최상위 값이 객체가 아닙니다: {path}")
    return value


def _load_json_lines(path: Path) -> list[Mapping[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise AnnexMappingError(f"필수 파일이 없습니다: {path}") from exc
    values: list[Mapping[str, object]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise AnnexMappingError(f"{path} {line_number}행이 객체가 아닙니다.")
        values.append(value)
    return values


def _required_text(value: Mapping[str, object], key: str, index: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AnnexMappingError(f"항목 {index}의 {key}가 비어 있습니다.")
    return item


def _print_progress(
    index: int,
    total: int,
    counts: Mapping[str, int],
    *,
    out: TextIO,
) -> None:
    if index == total or index % 10 == 0:
        print(
            f"[{index}/{total}] paired={counts.get('paired_targets', 0)} "
            f"matched={counts.get('matched_annexes', 0)} "
            f"fallback={counts.get('fallback_annexes', 0)}",
            file=out,
            flush=True,
        )


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


if __name__ == "__main__":
    raise SystemExit(main())
