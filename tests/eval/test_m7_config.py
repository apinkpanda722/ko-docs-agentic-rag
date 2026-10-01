import json
from types import SimpleNamespace

import pytest

from kodocs.eval import m7_config as config
from kodocs.eval.m7_grading import InputVerification, rubric_sha256
from kodocs.llm.grade_settings import GradeModelConfig


def put(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))


@pytest.fixture
def frozen_inputs(tmp_path, monkeypatch):
    model = GradeModelConfig("gemini-3.8-flash", thinking_level="minimal")
    monkeypatch.setattr(config, "load_grade_config", lambda: model)
    check = InputVerification(
        config.DEV_SHA256,
        300,
        {"test_gold": "gold", "test_silver": "silver"},
        {"test_gold": 60, "test_silver": 166},
        (),
    )
    monkeypatch.setattr(config, "verify_m6_inputs", lambda root: check)
    monkeypatch.setattr(config, "rules_sha256", lambda path: "rules")
    monkeypatch.setattr(config, "load_human_grades", lambda *args: {"id": 3})
    for path, value in {
        "data/manifest/m7_human_grades.json": {"csv_sha256": "csv"},
        "data/manifest/m7_human_sample.json": {"answer_ids": ["id"]},
        "data/interim/m7/cards/cards_manifest.json": {"rubric_sha256": rubric_sha256()},
        "data/manifest/m7_pilot.json": {"ids": list(map(str, range(10)))},
        "data/manifest/m7_probe.json": {
            "calls": [{"accepted": True, "settings": model.record()}]
        },
        "data/manifest/m6_config.json": {"config_hash": "m6", "m6_dev_sha256": "dev"},
    }.items():
        put(tmp_path, path, value)
    return SimpleNamespace(root=tmp_path, model=model, check=check)


def test_m7_config_freeze_exclusive_and_hashes(frozen_inputs):
    env = frozen_inputs
    record = config.freeze_config(root=env.root)
    assert record["dev_runs_sha256"] == config.DEV_SHA256
    assert record["dev_runs_rows"] == 300
    assert record["test_runs_rows"]["test_silver"] == 166
    assert record["model_settings_hash"] == env.model.settings_hash()
    assert config.verify_config(root=env.root) == record
    with pytest.raises(FileExistsError):
        config.freeze_config(root=env.root)


@pytest.mark.parametrize(
    "path,change,match",
    [
        ("data/manifest/m7_human_grades.json", None, "human"),
        ("data/manifest/m7_human_grades.json", {}, "CSV"),
        (
            "data/interim/m7/cards/cards_manifest.json",
            {"rubric_sha256": "old"},
            "rubric",
        ),
        ("data/manifest/m7_pilot.json", None, "pilot"),
        ("data/manifest/m7_probe.json", None, "probe"),
        ("data/manifest/m7_probe.json", {"calls": []}, "probe"),
    ],
)
def test_m7_config_rejects_each_prerequisite(frozen_inputs, path, change, match):
    root = frozen_inputs.root
    if change is None:
        (root / path).unlink()
    else:
        put(root, path, change)
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        config.freeze_config(root=root)


def test_m7_config_rejects_dev_original_and_expired_price(frozen_inputs, monkeypatch):
    from dataclasses import replace

    from kodocs.costs import PriceExpiredError

    monkeypatch.setattr(
        config,
        "verify_m6_inputs",
        lambda root: replace(frozen_inputs.check, dev_runs_sha256="changed"),
    )
    with pytest.raises(ValueError, match="M7-D1"):
        config.freeze_config(root=frozen_inputs.root)
    monkeypatch.setattr(config, "verify_m6_inputs", lambda root: frozen_inputs.check)

    def expired(service):
        raise PriceExpiredError("expired")

    monkeypatch.setattr(config, "ensure_price_valid", expired)
    with pytest.raises(PriceExpiredError):
        config.freeze_config(root=frozen_inputs.root)


def test_m7_config_requires_freeze_and_detects_changes(frozen_inputs):
    root = frozen_inputs.root
    with pytest.raises(FileNotFoundError):
        config.verify_config(root=root)
    config.freeze_config(root=root)
    put(root, "data/manifest/m7_human_sample.json", {"answer_ids": ["changed"]})
    with pytest.raises(ValueError, match="sample"):
        config.verify_config(root=root)


@pytest.mark.parametrize(
    "field,value", [("temperature", 0.5), ("seed", 8), ("thinking_level", "low")]
)
def test_m7_config_probe_settings_must_match(frozen_inputs, monkeypatch, field, value):
    from dataclasses import replace

    monkeypatch.setattr(
        config,
        "load_grade_config",
        lambda: replace(frozen_inputs.model, **{field: value}),
    )
    with pytest.raises(ValueError, match="probe"):
        config.freeze_config(root=frozen_inputs.root)


def test_m7_config_empty_model_rejected():
    with pytest.raises(ValueError, match="model"):
        GradeModelConfig("")


@pytest.mark.parametrize(
    "changed", ["rubric_sha256", "schema_sha256", "rules_sha256", "model_settings_hash"]
)
def test_m7_config_refuses_current_hash_changes(frozen_inputs, monkeypatch, changed):
    root = frozen_inputs.root
    config.freeze_config(root=root)
    original = config.current_hashes
    monkeypatch.setattr(
        config,
        "current_hashes",
        lambda root, model: original(root, model) | {changed: "changed"},
    )
    with pytest.raises(ValueError, match=changed):
        config.verify_config(root=root)


def test_m7_config_actual_price_expiration(frozen_inputs, monkeypatch):
    from datetime import date

    from kodocs.costs import PriceExpiredError, ensure_price_valid

    monkeypatch.setattr(
        config,
        "ensure_price_valid",
        lambda service: ensure_price_valid(service, on=date(2027, 1, 1)),
    )
    with pytest.raises(PriceExpiredError):
        config.freeze_config(root=frozen_inputs.root)


def test_m7_config_minimum_thinking_required(frozen_inputs, monkeypatch):
    from dataclasses import replace

    model = replace(frozen_inputs.model, thinking_level="high")
    probe = {
        "calls": [
            {"accepted": True, "settings": m.record()}
            for m in [frozen_inputs.model, model]
        ]
    }
    put(frozen_inputs.root, "data/manifest/m7_probe.json", probe)
    monkeypatch.setattr(config, "load_grade_config", lambda: model)
    with pytest.raises(ValueError, match="minimum"):
        config.freeze_config(root=frozen_inputs.root)
