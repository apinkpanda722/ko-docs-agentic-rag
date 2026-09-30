"""Single-source document authorization policy for retrieval queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class UserContext:
    """Server-supplied user attributes used to authorize retrieval."""

    team: str
    grade: int

    def __post_init__(self) -> None:
        if not isinstance(self.team, str) or not self.team:
            raise ValueError("team must be a non-empty string")
        if not isinstance(self.grade, int) or isinstance(self.grade, bool):
            raise TypeError("grade must be an integer")


def access_filter(ctx: UserContext) -> dict:
    """Return the OpenSearch clause equivalent to the document access policy."""
    team_membership = {"term": {"access_teams": ctx.team}}
    return {
        "bool": {
            "should": [
                {"term": {"access_level": "public"}},
                {
                    "bool": {
                        "filter": [
                            {"term": {"access_level": "team"}},
                            team_membership,
                        ]
                    }
                },
                {
                    "bool": {
                        "filter": [
                            {"term": {"access_level": "restricted"}},
                            team_membership,
                            {"range": {"access_min_grade": {"lte": ctx.grade}}},
                        ]
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    }


def can_read_doc(
    ctx: UserContext,
    *,
    level: str,
    teams: Sequence[str],
    min_grade: int,
) -> bool:
    """Apply the same policy in Python for checks that do not query OpenSearch."""
    if level == "public":
        return True
    if isinstance(teams, (str, bytes)) or ctx.team not in teams:
        return False
    if level == "team":
        return True
    if level != "restricted":
        return False
    return (
        isinstance(min_grade, int)
        and not isinstance(min_grade, bool)
        and ctx.grade >= min_grade
    )
