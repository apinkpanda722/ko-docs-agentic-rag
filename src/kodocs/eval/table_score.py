"""Score parser table cells against reviewed Markdown goldens."""

import argparse
import json
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TextIO

from kodocs.eval.table_drafts import parse_markdown_document
from kodocs.eval.text_normalize import normalize
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.parsing.schema import ParsedDocument, from_json, output_path
from kodocs.parsing.tables import cells_to_grid, markdown_to_grid

DEFAULT_PARSERS: tuple[str, ...] = ("pypdf", "docling", "upstage")
NO_TABLE_PARSERS: frozenset[str] = frozenset({"pypdf"})


class TableScoreError(RuntimeError):
    """Table golden or parsed inputs are absent or invalid."""


@dataclass(frozen=True)
class TableScore:
    """Text-free result for one golden and parser."""

    golden_id: str
    parser: str
    applicable: bool
    status: str
    best_table_index: int | None
    shape_match: bool
    cell_exact_rate: float | None
    cell_sim_mean: float | None
    invalid_candidates: int
    reason: str | None


def score_table(golden_grid: list[list[str]], parsed: ParsedDocument) -> TableScore:
    """Select and score one parser table, isolating every invalid candidate."""
    if parsed.parser in NO_TABLE_PARSERS:
        return _not_applicable(parsed.parser)
    if not parsed.tables:
        return _zero_score(parser=parsed.parser, status="no_tables")

    candidates: list[tuple[int, list[list[str]]]] = []
    invalid_reasons: list[str] = []
    for index, table in enumerate(parsed.tables):
        if table.invalid_reason:
            invalid_reasons.append(_first_line(table.invalid_reason))
            continue
        try:
            candidates.append((index, cells_to_grid(table.cells)))
        except Exception as exc:  # noqa: BLE001 - FP-030 isolates parser data defects
            invalid_reasons.append(f"{type(exc).__name__}: {_first_line(str(exc))}")

    invalid_count = len(invalid_reasons)
    reason = _summarize_reasons(invalid_reasons)
    if not candidates:
        return TableScore(
            golden_id="",
            parser=parsed.parser,
            applicable=True,
            status="all_invalid",
            best_table_index=None,
            shape_match=False,
            cell_exact_rate=0.0,
            cell_sim_mean=0.0,
            invalid_candidates=invalid_count,
            reason=reason,
        )

    best_index, best_grid = candidates[0]
    best_exact = _overlap_exact_count(golden_grid, best_grid)
    for index, grid in candidates[1:]:
        exact = _overlap_exact_count(golden_grid, grid)
        if exact > best_exact:
            best_index, best_grid, best_exact = index, grid, exact

    exact_rate, similarity = _cell_metrics(golden_grid, best_grid)
    return TableScore(
        golden_id="",
        parser=parsed.parser,
        applicable=True,
        status="ok",
        best_table_index=best_index,
        shape_match=_shape(best_grid) == _shape(golden_grid),
        cell_exact_rate=exact_rate,
        cell_sim_mean=similarity,
        invalid_candidates=invalid_count,
        reason=reason,
    )


def run(root: Path, *, out: TextIO = sys.stdout) -> dict[str, object]:
    """Score all indexed goldens and write a committed, text-free result manifest."""
    manifest_path = root / "data/manifest/table_golden.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TableScoreError(
            "data/manifest/table_golden.json이 없습니다. 먼저 make table-golden을 실행하세요."
        ) from exc
    tables = payload.get("tables") if isinstance(payload, Mapping) else None
    if not isinstance(tables, list) or not tables:
        raise TableScoreError(
            "표 정답이 0개입니다. make table-drafts → 검수 → make table-golden 순서로 실행하세요."
        )

    scores: list[TableScore] = []
    for index, value in enumerate(tables):
        if not isinstance(value, Mapping):
            raise TableScoreError(f"tables[{index}]가 객체가 아닙니다.")
        golden_id = _required_string(value, "id", index)
        golden_path = root / "data/golden/tables" / f"{golden_id}.md"
        expected_hash = _required_string(value, "golden_file_sha256", index)
        changed = not golden_path.is_file() or sha256_file(golden_path) != expected_hash
        if changed:
            scores.extend(
                _changed_score(golden_id=golden_id, parser=parser)
                for parser in DEFAULT_PARSERS
            )
            continue

        _, markdown = parse_markdown_document(golden_path.read_text(encoding="utf-8"))
        golden_grid = markdown_to_grid(markdown)
        _validate_grid(golden_grid, golden_id)
        pdf_sha256 = value.get("pdf_sha256")
        pdf_sha = pdf_sha256 if isinstance(pdf_sha256, str) else ""
        for parser in DEFAULT_PARSERS:
            if parser in NO_TABLE_PARSERS:
                score = _not_applicable(parser)
            else:
                parsed = _load_parsed(root, parser, pdf_sha)
                score = (
                    _zero_score(parser=parser, status="parse_missing")
                    if parsed is None
                    else score_table(golden_grid, parsed)
                )
            scores.append(replace(score, golden_id=golden_id))

    summaries = {
        parser: _aggregate([score for score in scores if score.parser == parser])
        for parser in DEFAULT_PARSERS
    }
    result: dict[str, object] = {
        "generated_at": now_iso(),
        "table_golden_sha256": sha256_file(manifest_path),
        "parsers": summaries,
        "scores": [asdict(score) for score in scores],
    }
    _write_json_atomic(root / "data/manifest/table_scores.json", result)
    _print_summary(summaries, out=out)
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for offline table-cell scoring."""
    parser = argparse.ArgumentParser(
        description="사람이 검수한 표 정답과 파서별 셀 출력을 채점합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        run(args.root)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        TableScoreError,
    ) as exc:
        print(f"표 채점 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _load_parsed(root: Path, parser: str, pdf_sha256: str) -> ParsedDocument | None:
    if not pdf_sha256:
        return None
    path = output_path(root, parser, pdf_sha256)
    try:
        parsed = from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if parsed.parser != parser or parsed.input_sha256 != pdf_sha256:
        return None
    return parsed


def _overlap_exact_count(
    golden: Sequence[Sequence[str]], predicted: Sequence[Sequence[str]]
) -> int:
    rows = min(len(golden), len(predicted))
    return sum(
        normalize(golden[row][col]) == normalize(predicted[row][col])
        for row in range(rows)
        for col in range(min(len(golden[row]), len(predicted[row])))
    )


def _cell_metrics(
    golden: Sequence[Sequence[str]], predicted: Sequence[Sequence[str]]
) -> tuple[float, float]:
    denominator = sum(len(row) for row in golden)
    if not denominator:
        return 0.0, 0.0
    exact = 0
    similarities: list[float] = []
    for row_index, row in enumerate(golden):
        for col_index, golden_cell in enumerate(row):
            if row_index >= len(predicted) or col_index >= len(predicted[row_index]):
                similarities.append(0.0)
                continue
            predicted_cell = predicted[row_index][col_index]
            golden_normalized = normalize(golden_cell)
            predicted_normalized = normalize(predicted_cell)
            exact += golden_normalized == predicted_normalized
            similarities.append(
                SequenceMatcher(
                    None,
                    golden_normalized,
                    predicted_normalized,
                    autojunk=False,
                ).ratio()
            )
    return exact / denominator, sum(similarities) / denominator


def _shape(grid: Sequence[Sequence[str]]) -> tuple[int, int]:
    return len(grid), len(grid[0]) if grid else 0


def _validate_grid(grid: Sequence[Sequence[str]], golden_id: str) -> None:
    if not grid:
        raise TableScoreError(f"{golden_id}: 정답 표가 비어 있습니다.")
    width = len(grid[0])
    if not width or any(len(row) != width for row in grid):
        raise TableScoreError(f"{golden_id}: 정답 표가 직사각형이 아닙니다.")


def _not_applicable(parser: str) -> TableScore:
    return TableScore(
        golden_id="",
        parser=parser,
        applicable=False,
        status="no_tables",
        best_table_index=None,
        shape_match=False,
        cell_exact_rate=None,
        cell_sim_mean=None,
        invalid_candidates=0,
        reason="표 추출 기능 없음",
    )


def _zero_score(*, parser: str, status: str) -> TableScore:
    return TableScore(
        golden_id="",
        parser=parser,
        applicable=True,
        status=status,
        best_table_index=None,
        shape_match=False,
        cell_exact_rate=0.0,
        cell_sim_mean=0.0,
        invalid_candidates=0,
        reason=None,
    )


def _changed_score(*, golden_id: str, parser: str) -> TableScore:
    return TableScore(
        golden_id=golden_id,
        parser=parser,
        applicable=parser not in NO_TABLE_PARSERS,
        status="golden_changed",
        best_table_index=None,
        shape_match=False,
        cell_exact_rate=None,
        cell_sim_mean=None,
        invalid_candidates=0,
        reason="manifest 기록 후 정답 파일 변경 또는 누락",
    )


def _aggregate(scores: Sequence[TableScore]) -> dict[str, object]:
    applicable = [score for score in scores if score.applicable]
    measured = [score for score in applicable if score.cell_exact_rate is not None]
    return {
        "applicable": bool(applicable),
        "goldens": len(scores),
        "measured": len(measured),
        "status_counts": dict(
            sorted(Counter(score.status for score in scores).items())
        ),
        "shape_match_rate": (
            sum(score.shape_match for score in measured) / len(measured)
            if measured
            else None
        ),
        "cell_exact_rate_mean": (
            sum(score.cell_exact_rate or 0.0 for score in measured) / len(measured)
            if measured
            else None
        ),
        "cell_sim_mean": (
            sum(score.cell_sim_mean or 0.0 for score in measured) / len(measured)
            if measured
            else None
        ),
        "invalid_candidates": sum(score.invalid_candidates for score in applicable),
    }


def _print_summary(
    summaries: Mapping[str, Mapping[str, object]], *, out: TextIO
) -> None:
    print(
        "| 파서 | 채점 수 | 상태 | shape 일치율 | 셀 완전 일치율 | "
        "셀 유사도 | invalid 표 |",
        file=out,
    )
    print("| --- | ---: | --- | ---: | ---: | ---: | ---: |", file=out)
    for parser in DEFAULT_PARSERS:
        summary = summaries[parser]
        if not summary["applicable"]:
            print(
                f"| {parser} | 0 | 해당 없음(표 추출 기능 없음) | - | - | - | 0 |",
                file=out,
            )
            continue
        print(
            f"| {parser} | {summary['measured']} | {_status_text(summary['status_counts'])} | "
            f"{_percent(summary['shape_match_rate'])} | "
            f"{_number(summary['cell_exact_rate_mean'])} | "
            f"{_number(summary['cell_sim_mean'])} | "
            f"{summary['invalid_candidates']} |",
            file=out,
        )


def _status_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    return ", ".join(f"{key}={count}" for key, count in value.items())


def _percent(value: object) -> str:
    return f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else "-"


def _number(value: object) -> str:
    return f"{float(value):.4f}" if isinstance(value, (int, float)) else "-"


def _summarize_reasons(reasons: Sequence[str]) -> str | None:
    if not reasons:
        return None
    counts = Counter(reasons)
    return "; ".join(
        reason if count == 1 else f"{reason} (x{count})"
        for reason, count in counts.items()
    )[:500]


def _required_string(value: Mapping[object, object], key: str, index: int) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise TableScoreError(f"tables[{index}].{key}가 비어 있습니다.")
    return field


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _first_line(value: str) -> str:
    lines = value.splitlines()
    return (lines[0] if lines else "")[:300]


if __name__ == "__main__":  # pragma: no cover - exercised through CLI AC
    raise SystemExit(main())
