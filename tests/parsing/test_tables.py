"""Tests for common table placement and serialization helpers."""

import pytest

from kodocs.parsing.schema import TableCell
from kodocs.parsing.tables import (
    cells_to_grid,
    grid_to_markdown,
    markdown_to_grid,
    place_html_rows,
)


def test_anchor_cells_and_html_rows_produce_same_grid_with_merged_cell() -> None:
    cells = [
        TableCell(row=0, col=0, row_span=2, col_span=2, text="병합"),
        TableCell(row=0, col=2, row_span=1, col_span=1, text="위"),
        TableCell(row=1, col=2, row_span=1, col_span=1, text="아래"),
    ]
    html_rows = [
        [("병합", 2, 2), ("위", 1, 1)],
        [("아래", 1, 1)],
    ]
    expected = [["병합", "병합", "위"], ["병합", "병합", "아래"]]

    placed = place_html_rows(html_rows)

    assert placed == cells
    assert cells_to_grid(cells) == expected
    assert cells_to_grid(placed) == expected


def test_cells_to_grid_uses_span_extent_and_leaves_uncovered_positions_empty() -> None:
    cells = [
        TableCell(row=0, col=1, row_span=1, col_span=1, text="가"),
        TableCell(row=1, col=0, row_span=1, col_span=3, text="나"),
    ]

    assert cells_to_grid(cells) == [["", "가", ""], ["나", "나", "나"]]
    assert cells_to_grid([]) == []


def test_cells_to_grid_rejects_overlapping_spans() -> None:
    cells = [
        TableCell(row=0, col=0, row_span=2, col_span=2, text="first"),
        TableCell(row=1, col=1, row_span=1, col_span=1, text="second"),
    ]

    with pytest.raises(ValueError, match="overlap"):
        cells_to_grid(cells)


@pytest.mark.parametrize(
    "cell",
    [
        TableCell(row=-1, col=0, row_span=1, col_span=1, text="bad"),
        TableCell(row=0, col=-1, row_span=1, col_span=1, text="bad"),
        TableCell(row=0, col=0, row_span=0, col_span=1, text="bad"),
        TableCell(row=0, col=0, row_span=1, col_span=0, text="bad"),
    ],
)
def test_cells_to_grid_rejects_invalid_coordinates_and_spans(cell: TableCell) -> None:
    with pytest.raises(ValueError):
        cells_to_grid([cell])


def test_markdown_uses_first_row_as_header_and_escapes_cell_content() -> None:
    grid = [["열|이름", "설명"], ["값", "첫째\n둘째"]]

    markdown = grid_to_markdown(grid)

    assert markdown == "|열\\|이름|설명|\n|---|---|\n|값|첫째 둘째|"
    assert markdown_to_grid(markdown) == [
        ["열|이름", "설명"],
        ["값", "첫째 둘째"],
    ]


def test_markdown_round_trip_preserves_empty_cells_and_literal_pipes() -> None:
    grid = [["", "A|B"], ["C", ""]]

    assert markdown_to_grid(grid_to_markdown(grid)) == grid


def test_empty_grid_has_empty_markdown_round_trip() -> None:
    assert grid_to_markdown([]) == ""
    assert markdown_to_grid("") == []
