"""Freeze and verify the selected M6 model, prompts, and guardrails."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.types import canonical_hash
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.llm.answer_settings import load_answer_config
from kodocs.tools.deps import M5_CONFIG_HASH

DEV_PATH = Path("data/manifest/m6_dev.json")
CONFIG_PATH = Path("data/manifest/m6_config.json")


@dataclass(frozen=True)
class ConfigCheck:
    """Current-code mismatches against one frozen M6 config."""

    mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def freeze_config(
    *, root: Path = Path("."), frozen_at: str | None = None
) -> dict[str, object]:
    """Create ``m6_config.json`` once from the preregistered dev selection."""
    destination = root / CONFIG_PATH
    if destination.exists():
        raise FileExistsError(f"frozen M6 config already exists: {destination}")
    dev_path = root / DEV_PATH
    dev = _read_json_object(dev_path)
    selection = dev.get("selection")
    if selection is None:
        raise ValueError("M6 dev selection is null; user decision is required")
    if not isinstance(selection, str) or not selection:
        raise ValueError("M6 dev selection must be a model ID")
    if dev.get("m5_config_hash") != M5_CONFIG_HASH:
        raise ValueError("M6 dev M5 config hash mismatch")

    current_prompts = prompt_hashes()
    recorded_prompts = _mapping(dev.get("prompt_hashes"), "prompt_hashes")
    if dict(recorded_prompts) != current_prompts:
        raise ValueError("prompt hashes changed after M6 dev comparison")
    if dev.get("finalizer_hash") != finalizer_hash():
        raise ValueError("answer finalizer changed after M6 dev comparison")
    current_guard = GuardrailConfig()
    recorded_guard = _mapping(dev.get("guardrails"), "guardrails")
    if recorded_guard.get("settings_hash") != current_guard.settings_hash():
        raise ValueError("guardrail hash changed after M6 dev comparison")
    if recorded_guard.get("settings") != current_guard.record():
        raise ValueError("guardrail settings changed after M6 dev comparison")

    models = _mapping(dev.get("models"), "models")
    selected_record = _mapping(models.get(selection), f"models.{selection}")
    config = _config_for_model(selection)
    if selected_record.get("settings_hash") != config.settings_hash():
        raise ValueError("answer model settings hash changed after M6 dev comparison")
    if selected_record.get("settings") != config.record():
        raise ValueError("answer model settings changed after M6 dev comparison")

    payload: dict[str, object] = {
        "answer": {
            "model": selection,
            "settings": config.record(),
            "settings_hash": config.settings_hash(),
        },
        "prompt_hashes": current_prompts,
        "finalizer_hash": finalizer_hash(),
        "guardrails": {
            "settings": current_guard.record(),
            "settings_hash": current_guard.settings_hash(),
        },
        "m5_config_hash": M5_CONFIG_HASH,
        "code_commit": dev.get("code_commit"),
        "m6_dev_sha256": sha256_file(dev_path),
        "frozen_at": frozen_at or now_iso(),
    }
    payload["config_hash"] = canonical_hash(payload)
    _write_json_exclusive(destination, payload)
    return payload


def verify_config(path: Path | str) -> ConfigCheck:
    """Check canonical integrity and current model/prompt/guardrail hashes."""
    config_path = Path(path)
    mismatches: list[str] = []
    try:
        config = _read_json_object(config_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ConfigCheck(("config_file",))
    recorded_hash = config.get("config_hash")
    unhashed = {key: value for key, value in config.items() if key != "config_hash"}
    if not isinstance(recorded_hash, str) or canonical_hash(unhashed) != recorded_hash:
        mismatches.append("config_hash")
    if config.get("m5_config_hash") != M5_CONFIG_HASH:
        mismatches.append("m5_config_hash")
    if config.get("prompt_hashes") != prompt_hashes():
        mismatches.append("prompt_hashes")
    if config.get("finalizer_hash") != finalizer_hash():
        mismatches.append("finalizer_hash")
    guard = config.get("guardrails")
    current_guard = GuardrailConfig()
    if not isinstance(guard, Mapping) or (
        guard.get("settings") != current_guard.record()
        or guard.get("settings_hash") != current_guard.settings_hash()
    ):
        mismatches.append("guardrails")
    answer = config.get("answer")
    if not isinstance(answer, Mapping):
        mismatches.append("answer")
    else:
        model = answer.get("model")
        try:
            model_config = _config_for_model(model)
        except (TypeError, ValueError):
            mismatches.append("answer")
        else:
            if (
                answer.get("settings") != model_config.record()
                or answer.get("settings_hash") != model_config.settings_hash()
            ):
                mismatches.append("answer")
    dev_path = config_path.parent / DEV_PATH.name
    try:
        dev_hash = sha256_file(dev_path)
    except OSError:
        dev_hash = None
    if config.get("m6_dev_sha256") != dev_hash:
        mismatches.append("m6_dev_sha256")
    return ConfigCheck(tuple(dict.fromkeys(mismatches)))


def _config_for_model(model: object):
    if not isinstance(model, str) or not model:
        raise TypeError("answer model must be a non-empty string")
    for name in ("luna", "sol"):
        config = load_answer_config(name)
        if config.model == model:
            return config
    raise ValueError(f"unsupported M6 answer model: {model}")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 선택 설정을 고정합니다.")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        payload = freeze_config(root=args.root)
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        print(f"M6 설정 고정 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"M6 설정 고정 완료: config_hash={payload['config_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
