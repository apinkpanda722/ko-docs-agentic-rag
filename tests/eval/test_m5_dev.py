"""Tests for preregistered M5 dev tuning and immutable configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kodocs.eval.golden import Asker, Qrel, Question
from kodocs.eval.m5_dev import (
    DevGridRow,
    freeze_config,
    run_dev_tuning,
    select_setting,
    tuning_grid,
    verify_config,
)
from kodocs.eval.m5_inputs import EvalQuestion
from kodocs.eval.m5_metrics import Span
from kodocs.eval.m5_runner import QuestionRun
from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.index_build import m5_index_specs
from kodocs.retrieval.index_schema import index_name


def _question(qid: str, *, set_name: str, split: str = "dev") -> EvalQuestion:
    question = Question(
        id=qid,
        set=set_name,
        source="synthetic",
        type="semantic",
        tags=(),
        question=f"질문 {qid}",
        asker=Asker("platform", 2),
        as_of=None,
        qrels=(Qrel("doc-a", 2, start=0, end=2),),
        anchor_values=(),
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split=split,
    )
    return EvalQuestion(
        question=question,
        rel_by_doc={"doc-a": 2},
        n_relevant_total=1,
        n_relevant_visible=1,
        answer_ranges=(Span("doc-a", 0, 2),),
        repeated=False,
    )


def _row(
    method: str,
    candidate_chunks: int,
    weight: float | None,
    score: float = 0.5,
) -> DevGridRow:
    return DevGridRow(
        method=method,  # type: ignore[arg-type]
        candidate_chunks=candidate_chunks,
        bm25_weight=weight,
        gold_dev_mean_ndcg10=score,
        silver_dev_mean_ndcg10=score,
        selection_score=score,
    )


def test_tuning_grid_is_the_fixed_18_settings() -> None:
    grid = tuning_grid()

    assert len(grid) == 18
    assert {
        (setting.method, setting.candidate_chunks)
        for setting in grid
        if setting.method == "hybrid_rrf"
    } == {
        ("hybrid_rrf", 50),
        ("hybrid_rrf", 100),
        ("hybrid_rrf", 200),
    }
    assert {
        (setting.bm25_weight, setting.candidate_chunks)
        for setting in grid
        if setting.method == "hybrid_weighted"
    } == {
        (weight, size)
        for weight in (0.3, 0.4, 0.5, 0.6, 0.7)
        for size in (50, 100, 200)
    }


def test_selection_ties_prefer_rrf_then_smaller_candidates_then_weight() -> None:
    assert (
        select_setting(
            [_row("hybrid_weighted", 50, 0.3), _row("hybrid_rrf", 200, None)]
        ).method
        == "hybrid_rrf"
    )
    assert (
        select_setting(
            [_row("hybrid_rrf", 100, None), _row("hybrid_rrf", 50, None)]
        ).candidate_chunks
        == 50
    )
    assert (
        select_setting(
            [_row("hybrid_weighted", 50, 0.7), _row("hybrid_weighted", 50, 0.3)]
        ).bm25_weight
        == 0.3
    )


def test_selection_compares_scores_after_rounding_to_ten_places() -> None:
    weighted = _row("hybrid_weighted", 50, 0.3, 0.50000000004)
    rrf = _row("hybrid_rrf", 200, None, 0.5)

    assert select_setting([weighted, rrf]) == rrf


def test_dev_tuning_rejects_test_questions_before_any_search() -> None:
    called = False

    def fake_runner(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    with pytest.raises(ValueError, match="dev"):
        run_dev_tuning(
            object(),
            object(),
            [_question("g-test", set_name="gold", split="test")],
            [_question("s-dev", set_name="silver")],
            run_fn=fake_runner,
        )

    assert called is False


def test_dev_tuning_uses_nori_fixed_and_equal_set_weighting() -> None:
    gold = [_question("g-1", set_name="gold")]
    silver = [_question("s-1", set_name="silver")]
    seen: list[tuple[str, str, str, int]] = []

    def fake_runner(_client, _embedder, questions, config):
        seen.append((config.method, config.analyzer, config.chunking, len(questions)))
        ndcg = 0.8 if questions[0].question.set == "gold" else 0.2
        return [QuestionRun(questions[0].question.id, (), (), 0.0, ndcg, False, False)]

    result = run_dev_tuning(object(), object(), gold, silver, run_fn=fake_runner)

    assert len(result.rows) == 18
    assert all(
        analyzer == "nori" and chunking == "fixed" for _, analyzer, chunking, _ in seen
    )
    assert all(row.selection_score == pytest.approx(0.5) for row in result.rows)
    assert result.selected.method == "hybrid_rrf"
    assert result.selected.candidate_chunks == 50


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _freeze_inputs(root: Path) -> Path:
    embedder = FakeEmbedder(dim=4)
    index_entries = {}
    for number, spec in enumerate(m5_index_specs(), start=1):
        index_entries[index_name(spec.condition, spec.chunking)] = {
            "index_field_schema_hash": f"schema-{number}",
            "chunk_count": number * 10,
        }
    _write_json(
        root / "data/manifest/m5_index.json",
        {
            "chunking": {"fixed_size": 200, "fixed_overlap": 40},
            "indexes": index_entries,
        },
    )
    index_hash = hashlib.sha256(
        (root / "data/manifest/m5_index.json").read_bytes()
    ).hexdigest()
    _write_json(root / "data/manifest/m5_eval_inputs.json", {"status": "ok"})
    _write_json(
        root / "data/manifest/m5_dev.json",
        {
            "selected": {
                "method": "hybrid_rrf",
                "candidate_chunks": 50,
                "bm25_weight": None,
            },
            "embedder": {
                "model_id": embedder.model_id,
                "revision": embedder.revision,
                "settings_hash": hashlib.sha256(
                    json.dumps(
                        dict(embedder.settings), ensure_ascii=False, sort_keys=True
                    ).encode()
                ).hexdigest()[:12],
            },
            "m5_index_sha256": index_hash,
        },
    )
    return root / "data/manifest/m5_config.json"


def test_freeze_hash_is_deterministic_and_refuses_refreeze(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_path = _freeze_inputs(first_root)
    _freeze_inputs(second_root)

    first = freeze_config(
        root=first_root,
        frozen_at="2026-09-30T13:00:00+09:00",
        code_commit="abc123",
    )
    second = freeze_config(
        root=second_root,
        frozen_at="2026-09-30T13:00:00+09:00",
        code_commit="abc123",
    )

    assert first == second
    assert first["config_hash"] == second["config_hash"]
    assert first["fixed_size"] == 200
    assert first["rank_constant"] == 60
    assert len(first["indexes"]) == 4
    assert verify_config(first_path).ok is True
    with pytest.raises(FileExistsError, match="already exists"):
        freeze_config(root=first_root)


def test_verify_config_detects_config_and_input_tampering(tmp_path: Path) -> None:
    path = _freeze_inputs(tmp_path)
    freeze_config(
        root=tmp_path,
        frozen_at="2026-09-30T13:00:00+09:00",
        code_commit="abc123",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["candidate_chunks"] = 200
    _write_json(path, payload)

    check = verify_config(path)

    assert check.ok is False
    assert "config_hash" in check.mismatches

    payload["candidate_chunks"] = 50
    _write_json(path, payload)
    dev_path = tmp_path / "data/manifest/m5_dev.json"
    dev = json.loads(dev_path.read_text(encoding="utf-8"))
    dev["selected"]["candidate_chunks"] = 100
    _write_json(dev_path, dev)

    check = verify_config(path)

    assert "m5_dev_sha256" in check.mismatches


def test_verify_config_detects_eval_input_tampering(tmp_path: Path) -> None:
    path = _freeze_inputs(tmp_path)
    freeze_config(
        root=tmp_path,
        frozen_at="2026-09-30T13:00:00+09:00",
        code_commit="abc123",
    )
    _write_json(tmp_path / "data/manifest/m5_eval_inputs.json", {"status": "changed"})

    check = verify_config(path)

    assert check.ok is False
    assert "m5_eval_inputs_sha256" in check.mismatches
