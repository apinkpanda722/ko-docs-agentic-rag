"""Keep the M5 operator workflow explicit and outside ``make verify``."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
TARGETS = (
    "m5-annex-map",
    "m5-chunking",
    "m5-eval-inputs",
    "embed-models",
    "m5-index-dry",
    "m5-index",
    "m5-dev",
    "m5-freeze",
    "m5-test",
    "m5-report",
)


def _recipe(target: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(target)}:\s*\n(?P<body>(?:\t.*(?:\n|$))*)",
        MAKEFILE,
    )
    assert match is not None, f"missing Makefile target: {target}"
    return match.group("body")


def test_all_m5_targets_are_phony_defined_and_documented() -> None:
    phony_line = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    )
    phony = set(phony_line.removeprefix(".PHONY:").split())

    for target in TARGETS:
        assert target in phony
        assert re.search(rf"(?m)^{re.escape(target)}:", MAKEFILE)
        assert f"make {target}" in AGENTS


def test_m5_test_forwards_optional_rerun_reason() -> None:
    recipe = _recipe("m5-test")

    assert "RERUN_REASON" in MAKEFILE
    assert '--rerun-reason "$(RERUN_REASON)"' in MAKEFILE
    assert "$(M5_RERUN_FLAGS)" in recipe


def test_verify_does_not_invoke_operator_only_m5_targets() -> None:
    recipe = _recipe("verify")

    for target in TARGETS:
        assert target not in recipe


def test_embed_models_is_declared_but_not_installed_by_make() -> None:
    recipe = _recipe("embed-models")

    assert "uv run --group embed python -c" in recipe
    assert "snapshot_download" in recipe
    assert "BAAI/bge-m3" in recipe
    assert "uv add" not in recipe
    assert "embed 그룹" in MAKEFILE
