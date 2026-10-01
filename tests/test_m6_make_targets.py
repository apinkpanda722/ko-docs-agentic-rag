"""Keep the M6 temporal operator workflow outside ``make verify``."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
RUNNER_TARGETS = ("m6-inputs", "m6-dry", "m6-dev", "m6-freeze", "m6-test")


def _recipe(target: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(target)}:\s*\n(?P<body>(?:\t.*(?:\n|$))*)",
        MAKEFILE,
    )
    assert match is not None, f"missing Makefile target: {target}"
    return match.group("body")


def test_m6_temporal_target_is_phony_documented_and_operator_only() -> None:
    phony_line = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    )

    assert "m6-temporal" in phony_line.removeprefix(".PHONY:").split()
    assert _recipe("m6-temporal").strip() == (
        "uv run python -m kodocs.eval.m6_temporal"
    )
    assert "make m6-temporal" in AGENTS
    assert "m6-temporal" not in _recipe("verify")


def test_m6_mcp_targets_are_phony_documented_and_operator_only() -> None:
    phony_line = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    )
    phony = phony_line.removeprefix(".PHONY:").split()

    assert "m6-tokens" in phony
    assert "m6-serve" in phony
    assert _recipe("m6-tokens").strip() == ("uv run python -m kodocs.mcp_server.tokens")
    assert _recipe("m6-serve").strip() == "uv run python -m kodocs.mcp_server"
    assert "make m6-tokens" in AGENTS
    assert "make m6-serve" in AGENTS
    assert "m6-tokens" not in _recipe("verify")
    assert "m6-serve" not in _recipe("verify")


def test_m6_runner_targets_are_phony_documented_and_operator_only() -> None:
    phony_line = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    )
    phony = set(phony_line.removeprefix(".PHONY:").split())

    for target in RUNNER_TARGETS:
        assert target in phony
        assert f"make {target}" in AGENTS
        assert target not in _recipe("verify")

    assert _recipe("m6-inputs").strip() == "uv run python -m kodocs.eval.m6_inputs"
    assert _recipe("m6-dry").strip() == (
        "uv run python -m kodocs.eval.m6_dev --dry-run"
    )
    assert _recipe("m6-freeze").strip() == ("uv run python -m kodocs.eval.m6_config")


def test_m6_dev_and_test_forward_optional_operator_values() -> None:
    assert "LIMIT" in MAKEFILE
    assert "--limit $(LIMIT)" in MAKEFILE
    assert "$(M6_LIMIT_FLAGS)" in _recipe("m6-dev")
    assert "RERUN_REASON" in MAKEFILE
    assert '--rerun-reason "$(RERUN_REASON)"' in MAKEFILE
    assert "$(M6_RERUN_FLAGS)" in _recipe("m6-test")
