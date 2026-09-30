"""Access-policy equivalence tests for authorized retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from kodocs.eval.golden import Asker, can_read
from kodocs.retrieval.access import UserContext, access_filter, can_read_doc


def _matches_filter(clause: object, document: Mapping[str, object]) -> bool:
    """Interpret the small OpenSearch filter subset emitted by access_filter."""
    assert isinstance(clause, Mapping)
    if "term" in clause:
        term = clause["term"]
        assert isinstance(term, Mapping) and len(term) == 1
        field, expected = next(iter(term.items()))
        actual = document.get(field)
        return expected in actual if isinstance(actual, list) else actual == expected
    if "range" in clause:
        ranges = clause["range"]
        assert isinstance(ranges, Mapping) and len(ranges) == 1
        field, comparison = next(iter(ranges.items()))
        assert isinstance(comparison, Mapping) and set(comparison) == {"lte"}
        actual = document.get(field)
        return isinstance(actual, int) and actual <= comparison["lte"]
    if "bool" in clause:
        boolean = clause["bool"]
        assert isinstance(boolean, Mapping)
        filters = boolean.get("filter", ())
        should = boolean.get("should", ())
        if isinstance(filters, Mapping):
            filters = (filters,)
        if isinstance(should, Mapping):
            should = (should,)
        assert isinstance(filters, Sequence) and not isinstance(filters, str)
        assert isinstance(should, Sequence) and not isinstance(should, str)
        filters_match = all(_matches_filter(item, document) for item in filters)
        minimum = boolean.get("minimum_should_match", 0)
        should_match = sum(_matches_filter(item, document) for item in should)
        return filters_match and should_match >= minimum
    raise AssertionError(f"unsupported access-filter clause: {clause}")


@pytest.mark.parametrize("level", ["public", "team", "restricted"])
@pytest.mark.parametrize("team_matches", [False, True])
@pytest.mark.parametrize("grade_sufficient", [False, True])
def test_python_and_opensearch_access_policies_match_m4_reference(
    level: str,
    team_matches: bool,
    grade_sufficient: bool,
) -> None:
    ctx = UserContext(team="platform", grade=3 if grade_sufficient else 1)
    teams = ("platform",) if team_matches else ("finance",)
    min_grade = 2
    access = {"level": level, "teams": list(teams), "min_grade": min_grade}
    indexed = {
        "access_level": level,
        "access_teams": list(teams),
        "access_min_grade": min_grade,
    }
    expected = can_read(Asker(team=ctx.team, grade=ctx.grade), access)

    assert (
        can_read_doc(
            ctx,
            level=level,
            teams=teams,
            min_grade=min_grade,
        )
        is expected
    )
    assert _matches_filter(access_filter(ctx), indexed) is expected


@pytest.mark.parametrize("level", ["unknown", "", "PUBLIC"])
def test_unknown_access_levels_fail_closed(level: str) -> None:
    ctx = UserContext(team="platform", grade=99)

    assert (
        can_read_doc(
            ctx,
            level=level,
            teams=("platform",),
            min_grade=0,
        )
        is False
    )
    assert (
        _matches_filter(
            access_filter(ctx),
            {
                "access_level": level,
                "access_teams": ["platform"],
                "access_min_grade": 0,
            },
        )
        is False
    )


def test_public_access_does_not_depend_on_team_or_grade_metadata() -> None:
    ctx = UserContext(team="platform", grade=1)

    assert (
        can_read_doc(
            ctx,
            level="public",
            teams=(),
            min_grade=999,
        )
        is True
    )
