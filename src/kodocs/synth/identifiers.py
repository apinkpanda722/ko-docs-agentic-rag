"""Exact identifier patterns shared by synthesis, ingestion, and retrieval."""

from __future__ import annotations

import re
from collections.abc import Sequence

TICKET_RE = re.compile(
    r"(?<![A-Z0-9-])(PLT|PAY|DAI|SRE|SEC|HRM|FIN|LGL)-\d{4}(?![\d-])"
)
APPROVAL_RE = re.compile(r"(?<![A-Z0-9-])EXP-(2025|2026)-\d{4}(?![\d-])")
INCIDENT_RE = re.compile(r"(?<![A-Z0-9-])INC-(2025|2026)-\d{3}(?![\d-])")
REGULATION_REF_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9·])"
    r"(?P<name>(?!\s)[가-힣A-Za-z0-9·\s]+?(규칙|규정|지침|기준|헌장))"
    r"\s*제(?P<num>\d+)조(?:의(?P<branch>\d+))?"
)

_SERVICE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def service_pattern(names: Sequence[str]) -> re.Pattern[str]:
    """Compile an exact, boundary-safe pattern from registered service names."""
    unique_names = sorted(set(names), key=lambda name: (-len(name), name))
    invalid = [name for name in unique_names if not _SERVICE_NAME_RE.fullmatch(name)]
    if invalid:
        raise ValueError(f"invalid kebab-case service name: {invalid[0]}")
    if not unique_names:
        return re.compile(r"(?!)")
    alternatives = "|".join(re.escape(name) for name in unique_names)
    return re.compile(rf"(?<![a-z0-9-])(?:{alternatives})(?![a-z0-9-])")


SERVICE_RE = service_pattern(())


def extract_identifiers(text: str) -> dict[str, list[tuple[str, int, int]]]:
    """Return exact identifier values and half-open source spans by kind.

    Service names are seed-dependent and are therefore extracted by callers with
    :func:`service_pattern`; this seed-independent function uses the empty default
    ``SERVICE_RE`` while preserving the common result key.
    """
    patterns = {
        "ticket": TICKET_RE,
        "approval": APPROVAL_RE,
        "incident": INCIDENT_RE,
        "service": SERVICE_RE,
        "regulation": REGULATION_REF_RE,
    }
    return {
        kind: [
            (match.group(), match.start(), match.end())
            for match in pattern.finditer(text)
        ]
        for kind, pattern in patterns.items()
    }
