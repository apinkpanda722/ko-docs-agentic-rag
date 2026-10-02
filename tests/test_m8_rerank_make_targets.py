"""The model download remains an explicit operator command, outside verify."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rerank_models_target_is_declared_but_not_run_by_verify():
    text = (ROOT / "Makefile").read_text()
    assert (
        "rerank-models"
        in next(
            line for line in text.splitlines() if line.startswith(".PHONY:")
        ).split()
    )
    recipe = re.search(r"(?m)^rerank-models:\n((?:\t.*\n)+)", text)[1]
    assert "uv run --group embed python -c" in recipe
    assert "snapshot_download" in recipe
    assert "resolve_local_reranker_snapshot" in recipe
    for name in (
        "MODEL_ID",
        "REVISION",
        "ALLOW_PATTERNS",
        "IGNORE_PATTERNS",
        "EXPECTED_FILES",
    ):
        assert f"RERANKER_{name}" in recipe
    assert "revision=RERANKER_REVISION" in recipe
    assert "uv add" not in recipe
    verify = re.search(r"(?m)^verify:\n((?:\t.*\n)+)", text)[1]
    assert "rerank-models" not in verify
    assert "make rerank-models" in (ROOT / "AGENTS.md").read_text()
