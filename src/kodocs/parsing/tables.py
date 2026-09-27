"""Common table placement, span expansion, and Markdown conversion."""

import re
from collections.abc import Iterable, Sequence

from kodocs.parsing.schema import TableCell

_LINE_BREAK = re.compile(r"\r\n?|\n")


def join_cell_text(parts: Iterable[str]) -> str:
    """Join one cell's text parts in document order with single spaces.

    Shared nested-table rule: an inner table's cell texts are appended to the
    containing outer cell in document order (HWPX golden drafts and Upstage
    HTML use the same function).
    """
    return " ".join(stripped for part in parts if (stripped := part.strip()))


def cells_to_grid(cells: Sequence[TableCell]) -> list[list[str]]:
    """Expand anchored cells to a grid, copying merged text to every covered slot."""
    if not cells:
        return []

    for cell in cells:
        _validate_cell(cell)

    height = max(cell.row + cell.row_span for cell in cells)
    width = max(cell.col + cell.col_span for cell in cells)
    grid = [["" for _ in range(width)] for _ in range(height)]
    occupied: set[tuple[int, int]] = set()

    for cell in cells:
        for row in range(cell.row, cell.row + cell.row_span):
            for col in range(cell.col, cell.col + cell.col_span):
                position = (row, col)
                if position in occupied:
                    raise ValueError(f"Table cells overlap at row {row}, col {col}")
                occupied.add(position)
                grid[row][col] = cell.text
    return grid


def place_html_rows(
    rows: Sequence[Sequence[tuple[str, int, int]]],
) -> list[TableCell]:
    """Assign anchor coordinates to row-oriented HTML-style cells."""
    cells: list[TableCell] = []
    occupied: set[tuple[int, int]] = set()

    for row, source_cells in enumerate(rows):
        col = 0
        for text, row_span, col_span in source_cells:
            if row_span < 1 or col_span < 1:
                raise ValueError("Table cell spans must be at least 1")
            while _rectangle_overlaps(
                occupied,
                row=row,
                col=col,
                row_span=row_span,
                col_span=col_span,
            ):
                col += 1

            cell = TableCell(
                row=row,
                col=col,
                row_span=row_span,
                col_span=col_span,
                text=text,
            )
            cells.append(cell)
            for covered_row in range(row, row + row_span):
                for covered_col in range(col, col + col_span):
                    occupied.add((covered_row, covered_col))
            col += col_span
    return cells


def grid_to_markdown(grid: Sequence[Sequence[str]]) -> str:
    """Serialize a grid as a Markdown table whose first row is the header."""
    if not grid:
        return ""

    lines = [_markdown_row(grid[0])]
    lines.append(_markdown_row(["---"] * len(grid[0])))
    lines.extend(_markdown_row(row) for row in grid[1:])
    return "\n".join(lines)


def markdown_to_grid(text: str) -> list[list[str]]:
    """Parse Markdown emitted by :func:`grid_to_markdown` back into a grid."""
    if not text:
        return []

    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError("Markdown table must include a header separator")
    header = _parse_markdown_row(lines[0])
    separator = _parse_markdown_row(lines[1])
    if len(separator) != len(header) or any(
        len(cell) < 3 or set(cell) != {"-"} for cell in separator
    ):
        raise ValueError("Invalid Markdown header separator")
    return [header, *(_parse_markdown_row(line) for line in lines[2:])]


def _validate_cell(cell: TableCell) -> None:
    if cell.row < 0 or cell.col < 0:
        raise ValueError("Table cell coordinates must be non-negative")
    if cell.row_span < 1 or cell.col_span < 1:
        raise ValueError("Table cell spans must be at least 1")


def _rectangle_overlaps(
    occupied: set[tuple[int, int]],
    *,
    row: int,
    col: int,
    row_span: int,
    col_span: int,
) -> bool:
    return any(
        (covered_row, covered_col) in occupied
        for covered_row in range(row, row + row_span)
        for covered_col in range(col, col + col_span)
    )


def _markdown_row(cells: Sequence[str]) -> str:
    escaped = []
    for cell in cells:
        one_line = _LINE_BREAK.sub(" ", cell)
        escaped.append(one_line.replace("\\", "\\\\").replace("|", "\\|"))
    return f"|{'|'.join(escaped)}|"


def _parse_markdown_row(line: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("Markdown table rows must start and end with '|'")

    cells: list[str] = []
    buffer: list[str] = []
    escaped = False
    for character in line[1:-1]:
        if escaped:
            buffer.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(buffer))
            buffer = []
        else:
            buffer.append(character)
    if escaped:
        buffer.append("\\")
    cells.append("".join(buffer))
    return cells
