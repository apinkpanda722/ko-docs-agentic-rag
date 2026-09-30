"""Tests for stable current-version ordering within one rule."""

from __future__ import annotations

from dataclasses import dataclass

from kodocs.retrieval.ordering import prefer_current_order


@dataclass(frozen=True)
class Item:
    name: str
    rule_id: str | None
    is_current: bool | None
    score: float


def _order(items: list[Item]) -> tuple[Item, ...]:
    return prefer_current_order(
        items,
        key_rule=lambda item: item.rule_id,
        key_is_current=lambda item: item.is_current,
    )


def test_prefer_current_order_reuses_only_the_same_rule_positions() -> None:
    prior_a1 = Item("prior-a1", "rule-a", False, 9.0)
    other = Item("other", "rule-b", True, 8.0)
    prior_a2 = Item("prior-a2", "rule-a", False, 7.0)
    current_a1 = Item("current-a1", "rule-a", True, 6.0)
    unrelated = Item("unrelated", None, False, 5.0)
    current_a2 = Item("current-a2", "rule-a", True, 4.0)

    ordered = _order([prior_a1, other, prior_a2, current_a1, unrelated, current_a2])

    assert ordered == (
        current_a1,
        other,
        current_a2,
        prior_a1,
        unrelated,
        prior_a2,
    )
    assert [item.score for item in ordered] == [6.0, 8.0, 4.0, 9.0, 5.0, 7.0]


def test_prefer_current_order_leaves_one_sided_and_unknown_groups_unchanged() -> None:
    items = [
        Item("current-a", "rule-a", True, 4.0),
        Item("prior-b1", "rule-b", False, 3.0),
        Item("prior-b2", "rule-b", False, 2.0),
        Item("unknown-version", "rule-c", None, 1.0),
        Item("synthetic", None, False, 0.5),
    ]

    assert _order(items) == tuple(items)


def test_prefer_current_order_never_exchanges_different_rules() -> None:
    items = [
        Item("prior-a", "rule-a", False, 4.0),
        Item("current-b", "rule-b", True, 3.0),
        Item("current-a", "rule-a", True, 2.0),
        Item("prior-b", "rule-b", False, 1.0),
    ]

    assert [item.name for item in _order(items)] == [
        "current-a",
        "current-b",
        "prior-a",
        "prior-b",
    ]
