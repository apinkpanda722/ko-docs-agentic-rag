"""Extract parser-neutral table-review drafts directly from HWPX XML."""

import argparse
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO
from zipfile import BadZipFile, ZipFile

from kodocs.ingest.filetype import FileKind, sniff
from kodocs.ingest.manifest import repo_relative, sha256_file
from kodocs.parsing.articles import ANNEX_RE
from kodocs.parsing.schema import TableCell
from kodocs.parsing.tables import cells_to_grid, grid_to_markdown, join_cell_text

_SECTION_RE = re.compile(r"^Contents/section(?P<number>\d+)\.xml$")
_ANNEX_KIND_RANK = {"별표": 0, "별지": 1, "서식": 2, "부록": 3, None: 4}


class TableDraftError(RuntimeError):
    """The draft target list or an HWPX input is invalid."""


@dataclass(frozen=True)
class TableDraft:
    """One top-level HWPX table and the metadata needed for human review."""

    rule_no: str
    source_sha256: str
    pdf_sha256: str | None
    section: str
    table_ordinal: int
    annex_label: str | None
    annex_kind: str | None
    rows: int
    cols: int
    nested: bool
    cells: tuple[TableCell, ...]


def extract_hwpx_tables(path: Path) -> list[TableDraft]:
    """Read top-level ``hp:tbl`` elements from a signature-verified HWPX file."""
    kind = sniff(path)
    if kind is not FileKind.HWPX:
        # ``sniff`` deliberately turns a corrupt ZIP into UNKNOWN. Reopening a ZIP
        # signature here preserves BadZipFile as the actionable per-file failure.
        with path.open("rb") as source:
            signature = source.read(4)
        if signature == b"PK\x03\x04":
            with ZipFile(path):
                pass
        raise TableDraftError(f"HWPX mimetype이 아닙니다: {kind.value}")

    source_sha256 = sha256_file(path)
    drafts: list[TableDraft] = []
    with ZipFile(path) as archive:
        sections = sorted(
            (
                (int(match.group("number")), name)
                for name in archive.namelist()
                if (match := _SECTION_RE.fullmatch(name)) is not None
            ),
            key=lambda item: (item[0], item[1]),
        )
        for _, section in sections:
            root = ET.fromstring(archive.read(section))
            drafts.extend(
                _extract_section(
                    root,
                    rule_no=path.parent.name,
                    source_sha256=source_sha256,
                    section=section,
                )
            )
    return drafts


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for user-initiated, offline HWPX draft extraction."""
    parser = argparse.ArgumentParser(
        description="HWPX 원본 XML에서 파서 중립 표 정답 초안을 추출합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        return _generate_drafts(args.root, out=sys.stdout)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        TableDraftError,
    ) as exc:
        print(
            f"표 초안 추출 실패: {type(exc).__name__}: {_first_line(exc)}",
            file=sys.stderr,
        )
        return 1


def parse_markdown_document(text: str) -> tuple[dict[str, object], str]:
    """Parse the small front-matter subset used by drafts and reviewed tables."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("표 Markdown에 front matter 시작 구분자가 없습니다.")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("표 Markdown에 front matter 종료 구분자가 없습니다.") from exc

    metadata: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, raw_value = line.partition(":")
        if not separator or not key.strip():
            raise ValueError(f"잘못된 front matter 줄: {line}")
        metadata[key.strip()] = _front_matter_value(raw_value.strip())
    markdown = "\n".join(lines[end + 1 :]).strip()
    return metadata, markdown


def render_markdown_document(metadata: Mapping[str, object], markdown: str) -> str:
    """Render deterministic single-line front matter plus a Markdown table."""
    lines = ["---"]
    lines.extend(
        f"{key}: {_front_matter_scalar(value)}" for key, value in metadata.items()
    )
    lines.extend(["---", markdown])
    return "\n".join(lines).rstrip() + "\n"


def _extract_section(
    root: ET.Element,
    *,
    rule_no: str,
    source_sha256: str,
    section: str,
) -> list[TableDraft]:
    parents = {child: parent for parent in root.iter() for child in parent}
    current_label: str | None = None
    current_kind: str | None = None
    table_ordinal = 0
    drafts: list[TableDraft] = []

    for element in root.iter():
        name = _local_name(element.tag)
        if name == "t":
            text = "".join(element.itertext()).strip()
            match = ANNEX_RE.match(text)
            if match is not None:
                current_label = text
                current_kind = re.sub(r"\s+", "", match.group("kind"))
        elif name == "tbl" and not _has_ancestor_named(element, "tbl", parents):
            cells = tuple(_table_cells(element, parents))
            grid = cells_to_grid(cells)
            drafts.append(
                TableDraft(
                    rule_no=rule_no,
                    source_sha256=source_sha256,
                    pdf_sha256=None,
                    section=section,
                    table_ordinal=table_ordinal,
                    annex_label=current_label,
                    annex_kind=current_kind,
                    rows=len(grid),
                    cols=len(grid[0]) if grid else 0,
                    nested=any(
                        nested is not element and _local_name(nested.tag) == "tbl"
                        for nested in element.iter()
                    ),
                    cells=cells,
                )
            )
            table_ordinal += 1
    return drafts


def _table_cells(
    table: ET.Element, parents: Mapping[ET.Element, ET.Element]
) -> list[TableCell]:
    cells: list[TableCell] = []
    for cell in table.iter():
        if _local_name(cell.tag) != "tc":
            continue
        if _closest_ancestor_named(cell, "tbl", parents) is not table:
            continue
        address = _first_child_named(cell, "cellAddr")
        span = _first_child_named(cell, "cellSpan")
        if address is None:
            raise ValueError("hp:tc에 hp:cellAddr가 없습니다.")
        cells.append(
            TableCell(
                row=_integer_attribute(address, "rowAddr"),
                col=_integer_attribute(address, "colAddr"),
                row_span=_integer_attribute(span, "rowSpan", default=1),
                col_span=_integer_attribute(span, "colSpan", default=1),
                text=_cell_text(cell, parents),
            )
        )
    return cells


def _cell_text(cell: ET.Element, parents: Mapping[ET.Element, ET.Element]) -> str:
    parts: list[str] = []
    for sublist in cell.iter():
        if _local_name(sublist.tag) != "subList":
            continue
        if _closest_ancestor_named(sublist, "tc", parents) is not cell:
            continue
        for paragraph in sublist.iter():
            if _local_name(paragraph.tag) != "p":
                continue
            if _has_intermediate_ancestor_named(paragraph, sublist, "p", parents):
                continue
            parts.extend(_paragraph_parts(paragraph, parents))
    return join_cell_text(parts)


def _paragraph_parts(
    paragraph: ET.Element, parents: Mapping[ET.Element, ET.Element]
) -> list[str]:
    """Split a paragraph at inline tables: text runs, then one part per inner cell."""
    parts: list[str] = []
    buffer: list[str] = []

    def walk(element: ET.Element) -> None:
        for child in element:
            name = _local_name(child.tag)
            if name == "tbl":
                parts.append("".join(buffer))
                buffer.clear()
                parts.extend(
                    _cell_text(inner, parents)
                    for inner in child.iter()
                    if _local_name(inner.tag) == "tc"
                    and _closest_ancestor_named(inner, "tbl", parents) is child
                )
                continue
            if name == "t":
                buffer.append(child.text or "")
            walk(child)

    walk(paragraph)
    parts.append("".join(buffer))
    return parts


def _generate_drafts(root: Path, *, out: TextIO) -> int:
    target_path = root / "data/manifest/parse_targets.json"
    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TableDraftError(
            "data/manifest/parse_targets.json이 없습니다. 먼저 make parse-targets를 실행하세요."
        ) from exc
    targets = payload.get("targets") if isinstance(payload, Mapping) else None
    if not isinstance(targets, list):
        raise TableDraftError("parse_targets.json의 targets가 목록이 아닙니다.")

    candidates: list[tuple[TableDraft, Path]] = []
    failures = 0
    selected = [target for target in targets if _source_kind(target) == "hwpx"]
    for target in selected:
        rule_no = _string(target.get("rule_no"))
        try:
            source_path = _required_repo_path(root, target, "source_path")
            pdf_path = _optional_repo_path(root, target.get("pdf_path"))
            pdf_sha256 = sha256_file(pdf_path) if pdf_path is not None else None
            drafts = [
                replace(
                    draft,
                    rule_no=rule_no,
                    source_sha256=sha256_file(source_path),
                    pdf_sha256=pdf_sha256,
                )
                for draft in extract_hwpx_tables(source_path)
            ]
            for draft in drafts:
                destination = _draft_path(root, draft)
                metadata = _draft_metadata(draft)
                markdown = grid_to_markdown(cells_to_grid(draft.cells))
                _write_atomic(
                    destination,
                    render_markdown_document(metadata, markdown),
                )
                candidates.append((draft, destination))
            print(f"{rule_no}: {len(drafts)}개 표", file=out, flush=True)
        except (BadZipFile, ET.ParseError, OSError, ValueError, TableDraftError) as exc:
            failures += 1
            print(
                f"{rule_no}: 실패 {type(exc).__name__}: {_first_line(exc)}",
                file=out,
                flush=True,
            )

    candidates.sort(key=lambda item: _candidate_key(item[0]))
    _write_candidates(root, candidates)
    return 1 if failures else 0


def _source_kind(value: object) -> str:
    return _string(value.get("source_kind")) if isinstance(value, Mapping) else ""


def _required_repo_path(
    root: Path, target: Mapping[object, object], field: str
) -> Path:
    value = target.get(field)
    if not isinstance(value, str) or not value:
        raise TableDraftError(f"{field}가 없습니다.")
    return root / value


def _optional_repo_path(root: Path, value: object) -> Path | None:
    return root / value if isinstance(value, str) and value else None


def _draft_path(root: Path, draft: TableDraft) -> Path:
    match = _SECTION_RE.fullmatch(draft.section)
    if match is None:
        raise ValueError(f"지원하지 않는 section 경로: {draft.section}")
    return (
        root
        / "data/interim/table_drafts"
        / draft.rule_no
        / f"{match.group('number')}-{draft.table_ordinal:02d}.md"
    )


def _draft_metadata(draft: TableDraft) -> dict[str, object]:
    return {
        "rule_no": draft.rule_no,
        "source_sha256": draft.source_sha256,
        "pdf_sha256": draft.pdf_sha256,
        "section": draft.section,
        "table_ordinal": draft.table_ordinal,
        "annex_label": draft.annex_label,
        "annex_kind": draft.annex_kind,
        "shape": [draft.rows, draft.cols],
        "nested": draft.nested,
        "draft_generator": "kodocs.eval.table_drafts",
    }


def _candidate_key(draft: TableDraft) -> tuple[object, ...]:
    return (
        _ANNEX_KIND_RANK.get(draft.annex_kind, 3),
        draft.rule_no,
        draft.section,
        draft.table_ordinal,
    )


def _write_candidates(
    root: Path, candidates: Sequence[tuple[TableDraft, Path]]
) -> None:
    rows = [["rule_no", "파일", "annex_label", "크기", "nested"]]
    rows.extend(
        [
            draft.rule_no,
            repo_relative(path, root),
            draft.annex_label or "",
            f"{draft.rows}x{draft.cols}",
            str(draft.nested).lower(),
        ]
        for draft, path in candidates
    )
    path = root / "data/interim/table_drafts/candidates.md"
    _write_atomic(path, grid_to_markdown(rows) + "\n")


def _front_matter_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1].replace("''", "'")
        return value


def _front_matter_scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_child_named(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _integer_attribute(
    element: ET.Element | None, name: str, *, default: int | None = None
) -> int:
    if element is None:
        if default is not None:
            return default
        raise ValueError(f"hp:{name} 요소가 없습니다.")
    value = next(
        (raw for key, raw in element.attrib.items() if _local_name(key) == name),
        None,
    )
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{name} 속성이 없습니다.")
    return int(value)


def _has_ancestor_named(
    element: ET.Element,
    name: str,
    parents: Mapping[ET.Element, ET.Element],
) -> bool:
    return _closest_ancestor_named(element, name, parents) is not None


def _closest_ancestor_named(
    element: ET.Element,
    name: str,
    parents: Mapping[ET.Element, ET.Element],
) -> ET.Element | None:
    current = parents.get(element)
    while current is not None:
        if _local_name(current.tag) == name:
            return current
        current = parents.get(current)
    return None


def _has_intermediate_ancestor_named(
    element: ET.Element,
    stop: ET.Element,
    name: str,
    parents: Mapping[ET.Element, ET.Element],
) -> bool:
    current = parents.get(element)
    while current is not None and current is not stop:
        if _local_name(current.tag) == name:
            return True
        current = parents.get(current)
    return False


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _first_line(exc: BaseException) -> str:
    lines = str(exc).splitlines()
    return (lines[0] if lines else "")[:300]


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


if __name__ == "__main__":  # pragma: no cover - exercised through CLI AC
    raise SystemExit(main())
