from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval.golden import load_questions
from kodocs.eval.m6_inputs import M6InputError, build_input_manifest

ROOT = Path(__file__).resolve().parents[2]


def _questions():
    gold = load_questions(ROOT / "data/golden/queries/gold.jsonl")
    silver = load_questions(ROOT / "data/golden/queries/silver.jsonl")
    return gold, silver


def test_real_m6_input_sets_match_preregistered_counts() -> None:
    gold, silver = _questions()

    manifest = build_input_manifest(
        gold,
        silver,
        gold_sha256="g" * 64,
        silver_sha256="s" * 64,
        m5_config_hash=(
            "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
        ),
    )

    assert manifest["dev_set"]["count"] == 75
    assert manifest["h5_pool"]["count"] == 13
    ex_sql = manifest["h5_pool_ex_sql"]
    sql_ids = {
        question.id
        for question in gold
        if question.split == "test" and question.type == "sql"
    }
    assert ex_sql["count"] == 9
    assert ex_sql["by_type"] == {"identifier": 4, "multi_doc": 5}
    assert set(ex_sql["ids"]) < set(manifest["h5_pool"]["ids"])
    assert not set(ex_sql["ids"]) & sql_ids
    assert len(sql_ids) == 4
    denominator = manifest["answer_match_denominator"]
    assert denominator["count"] == 56
    assert denominator["by_set_source"] == {
        "gold": {"real": 3, "synthetic": 10},
        "silver": {"real": 0, "synthetic": 43},
    }
    assert manifest["gate_permission_dev"]["count"] == 8
    assert manifest["unanswerable_dev"]["count"] == 8
    assert {
        name: value["count"]
        for name, value in manifest["router_types"]["eligible"].items()
    } == {"identifier": 13, "multi_doc": 12, "semantic": 20, "temporal": 10}
    assert set(manifest["router_types"]["below_threshold"]) == {"sql", "table"}


def test_m6_input_count_drift_fails_closed() -> None:
    gold, silver = _questions()
    removed = next(question for question in silver if question.split == "dev")
    changed = [question for question in silver if question.id != removed.id]

    with pytest.raises(M6InputError, match="dev_set"):
        build_input_manifest(
            gold,
            changed,
            gold_sha256="g" * 64,
            silver_sha256="s" * 64,
            m5_config_hash=(
                "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
            ),
        )

    # A non-dev mutation is also checked by the fixed H5 pool.
    h5 = next(
        question
        for question in gold
        if question.split == "test" and question.type == "identifier"
    )
    changed_gold = [
        replace(question, type="semantic") if question.id == h5.id else question
        for question in gold
    ]
    with pytest.raises(M6InputError, match="h5_pool"):
        build_input_manifest(
            changed_gold,
            silver,
            gold_sha256="g" * 64,
            silver_sha256="s" * 64,
            m5_config_hash=(
                "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
            ),
        )


def test_h5_pool_ex_sql_count_drift_fails_closed() -> None:
    gold, silver = _questions()
    identifier = next(
        question
        for question in gold
        if question.split == "test" and question.type == "identifier"
    )
    # Retyping to sql keeps the 13-question H5 pool but leaves 8 non-sql questions.
    changed = [
        replace(question, type="sql") if question.id == identifier.id else question
        for question in gold
    ]

    with pytest.raises(M6InputError, match="h5_pool_ex_sql"):
        build_input_manifest(
            changed,
            silver,
            gold_sha256="g" * 64,
            silver_sha256="s" * 64,
            m5_config_hash=(
                "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
            ),
        )
