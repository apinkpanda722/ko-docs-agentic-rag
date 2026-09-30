"""Pure result ordering policies shared by authorized retrieval paths."""

from __future__ import annotations

from collections.abc import Callable, Sequence


def prefer_current_order[T](
    items: Sequence[T],
    *,
    key_rule: Callable[[T], str | None],
    key_is_current: Callable[[T], bool | None],
) -> tuple[T, ...]:
    """Move current items before prior items in each mixed rule group.

    Each rule keeps its original set of positions.  Items with missing rule or
    version metadata, and groups containing only one version side, remain
    untouched.
    """
    ordered = list(items)
    positions_by_rule: dict[str, list[int]] = {}
    for position, item in enumerate(items):
        rule_id = key_rule(item)
        is_current = key_is_current(item)
        if rule_id is None or is_current is None:
            continue
        positions_by_rule.setdefault(rule_id, []).append(position)

    for positions in positions_by_rule.values():
        current = [
            items[position]
            for position in positions
            if key_is_current(items[position]) is True
        ]
        prior = [
            items[position]
            for position in positions
            if key_is_current(items[position]) is False
        ]
        if not current or not prior:
            continue
        for position, item in zip(positions, (*current, *prior), strict=True):
            ordered[position] = item
    return tuple(ordered)
