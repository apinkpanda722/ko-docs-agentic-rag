from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import prompt_hashes
from kodocs.eval.m6_config import freeze_config
from kodocs.llm.answer_settings import load_answer_config
from kodocs.tools.deps import M5_CONFIG_HASH


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    config = load_answer_config("luna")
    _write(
        root / "data/manifest/m6_dev.json",
        {
            "selection": config.model,
            "models": {
                config.model: {
                    "settings": config.record(),
                    "settings_hash": config.settings_hash(),
                }
            },
            "prompt_hashes": prompt_hashes(),
            "finalizer_hash": finalizer_hash(),
            "guardrails": {
                "settings": GuardrailConfig().record(),
                "settings_hash": GuardrailConfig().settings_hash(),
            },
            "m5_config_hash": M5_CONFIG_HASH,
            "code_commit": "fixture",
        },
    )
    return root


def test_freeze_is_exclusive_and_rejects_changed_hashes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    frozen = freeze_config(root=root, frozen_at="2026-10-01T00:00:00+09:00")
    assert frozen["answer"]["model"] == "gpt-6-luna"
    assert isinstance(frozen["config_hash"], str)
    with pytest.raises(FileExistsError):
        freeze_config(root=root)

    mismatch = _root(tmp_path / "other")
    path = mismatch / "data/manifest/m6_dev.json"
    value = json.loads(path.read_text())
    value["prompt_hashes"]["common"] = "changed"
    _write(path, value)
    with pytest.raises(ValueError, match="prompt"):
        freeze_config(root=mismatch)


def test_freeze_rejects_a_dev_comparison_from_an_older_finalizer(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path = root / "data/manifest/m6_dev.json"
    value = json.loads(path.read_text())
    value["finalizer_hash"] = "0" * 64
    _write(path, value)

    with pytest.raises(ValueError, match="finalizer"):
        freeze_config(root=root)
