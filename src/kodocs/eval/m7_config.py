"""Exclusive M7 freeze with independent human evidence and M6 raw integrity."""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from kodocs.costs import PRICES, ensure_price_valid
from kodocs.eval.m7_grading import rubric_sha256, schema_sha256, verify_m6_inputs
from kodocs.eval.m7_human import CSV_PATH, load_human_grades
from kodocs.eval.m7_rules import rules_sha256
from kodocs.llm.grade_settings import load_grade_config

CONFIG_PATH = Path("data/manifest/m7_config.json")
DEV_SHA256 = "0f25104b068b7f29813db943493d2f37c70bdc3a490bad9f0359f22dadc3eb8b"
SEED = 20261001
PROOFS = {
    "sample_sha256": "data/manifest/m7_human_sample.json",
    "human_grades_sha256": "data/manifest/m7_human_grades.json",
    "pilot_sha256": "data/manifest/m7_pilot.json",
    "probe_sha256": "data/manifest/m7_probe.json",
}


def read_json(path):
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise TypeError(f"{Path(path).name}: object required")
    return value


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current_hashes(root, model):
    return {
        "model_settings_hash": model.settings_hash(),
        "rubric_sha256": rubric_sha256(),
        "schema_sha256": schema_sha256(),
        "rules_sha256": rules_sha256(root / "docs/results/hypotheses.md"),
    }


def check_price(model):
    for suffix in ("input", "output"):
        ensure_price_valid(f"google.{model.model}.{suffix}")


def _check_probe(model, probe):
    """Freeze only the probed, documented settings: baseline accepted, values equal."""
    calls = {c.get("name"): c for c in probe.get("calls", [])}
    baseline = calls.get("baseline")
    if not baseline or not baseline.get("accepted") or not baseline.get("parsed"):
        raise ValueError("probe: no accepted baseline configuration")
    settings = baseline.get("settings", {})
    if settings.get("model") != model.model:
        raise ValueError("probe: baseline model differs from configured model")
    confirmed = probe.get("confirmed_settings") or {}
    for field in ("thinking_level", "temperature", "seed"):
        value = getattr(model, field)
        if settings.get(field) != value or confirmed.get(field) != value:
            raise ValueError(f"probe: unsupported/unconfirmed {field}")
    if not model.thinking_level:
        raise ValueError("probe: thinking_level must be confirmed")


def freeze_config(*, root=Path("."), frozen_at=None):
    root = Path(root)
    target = root / CONFIG_PATH
    if target.exists():
        raise FileExistsError("frozen M7 config already exists; overwrite refused")
    model = load_grade_config()
    check_price(model)
    proofs = {key: sha256_file(root / path) for key, path in PROOFS.items()}
    human = read_json(root / PROOFS["human_grades_sha256"])
    if not human.get("csv_sha256"):
        raise ValueError("human CSV hash not registered")
    cards = read_json(root / "data/interim/m7/cards/cards_manifest.json")
    if cards.get("rubric_sha256") != rubric_sha256():
        raise ValueError("cards rubric hash mismatch")
    load_human_grades(
        root / PROOFS["human_grades_sha256"],
        root / CSV_PATH,
        root / "data/interim/m7/cards/key.json",
    )
    _check_probe(model, read_json(root / PROOFS["probe_sha256"]))
    inputs = verify_m6_inputs(root)
    if inputs.dev_runs_sha256 != DEV_SHA256:
        raise ValueError(
            "M7-D1(c): current dev runs.jsonl SHA-256 differs from preregistered 0f25104b…"
        )
    m6 = read_json(root / "data/manifest/m6_config.json")
    payload = {
        **model.record(),
        **current_hashes(root, model),
        **proofs,
        "human_csv_sha256": human["csv_sha256"],
        "m6_config_hash": m6["config_hash"],
        "m6_dev_sha256": m6["m6_dev_sha256"],
        **asdict(inputs),
        "valid_until": PRICES[f"google.{model.model}.input"].valid_until,
        "shuffle_seed": SEED,
        "frozen_at": frozen_at or datetime.now(UTC).isoformat(),
    }
    # JSON roundtrip normalizes tuple metadata for subsequent exact comparison.
    payload = json.loads(json.dumps(payload))
    payload["config_hash"] = _hash(payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def verify_config(*, root=Path(".")):
    root = Path(root)
    frozen = read_json(root / CONFIG_PATH)
    if frozen.get("config_hash") != _hash(
        {k: v for k, v in frozen.items() if k != "config_hash"}
    ):
        raise ValueError("M7 config_hash mismatch")
    model = load_grade_config()
    check_price(model)
    for key, value in current_hashes(root, model).items():
        if frozen.get(key) != value:
            raise ValueError(f"M7 frozen {key} mismatch")
    for key, path in PROOFS.items():
        if frozen.get(key) != sha256_file(root / path):
            raise ValueError(f"M7 frozen {key} mismatch")
    m6 = read_json(root / "data/manifest/m6_config.json")
    if (
        frozen["m6_config_hash"] != m6["config_hash"]
        or frozen["m6_dev_sha256"] != m6["m6_dev_sha256"]
    ):
        raise ValueError("M7 frozen M6 config mismatch")
    inputs = asdict(verify_m6_inputs(root))
    for key in (
        "dev_runs_sha256",
        "dev_runs_rows",
        "test_runs_sha256",
        "test_runs_rows",
    ):
        if frozen.get(key) != inputs[key]:
            raise ValueError(f"M7 frozen {key} mismatch")
    return frozen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        record = freeze_config(root=args.root)
    except (OSError, TypeError, ValueError, RuntimeError, KeyError) as exc:
        print(f"M7 freeze refused: {type(exc).__name__}: {exc}")
        return 2
    print(f"M7 frozen: {record['config_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
