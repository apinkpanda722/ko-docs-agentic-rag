"""Tests for the authorization-aware M5 retrieval runner."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kodocs.eval.golden import Asker, Qrel, Question
from kodocs.eval.m5_inputs import EvalQuestion
from kodocs.eval.m5_metrics import Span
from kodocs.eval.m5_runner import (
    HybridSetting,
    QuestionRunError,
    RunConfig,
    all_combo_configs,
    run_questions,
)
from kodocs.retrieval.search import ChunkHit, DocHit, SearchResult


def _question(*, qid: str = "q-1", split: str = "dev") -> EvalQuestion:
    question = Question(
        id=qid,
        set="gold",
        source="synthetic",
        type="semantic",
        tags=(),
        question="배포 절차는 무엇인가요?",
        asker=Asker("platform", 3),
        as_of=None,
        qrels=(Qrel("doc-a", 2, start=3, end=8),),
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
        answer_ranges=(Span("doc-a", 3, 8),),
        repeated=False,
    )


def test_all_combo_configs_has_14_runs_and_merges_knn_analyzers() -> None:
    configs = all_combo_configs(HybridSetting(candidate_chunks=100, bm25_weight=0.4))

    assert len(configs) == 14
    assert len(set(configs)) == 14
    assert sum(config.method == "bm25" for config in configs) == 4
    assert sum(config.method == "hybrid_rrf" for config in configs) == 4
    assert sum(config.method == "hybrid_weighted" for config in configs) == 4
    knn = [config for config in configs if config.method == "knn"]
    assert [(config.chunking, config.analyzer) for config in knn] == [
        ("fixed", "nori"),
        ("structural", "nori"),
    ]
    assert all(config.merged_analyzers for config in knn)
    assert not any(config.merged_analyzers for config in configs if config not in knn)


def test_runner_uses_question_asker_and_registered_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object, str, object]] = []

    def fake_search(client, index, ctx, query, params, embedder):
        calls.append((index, ctx, query, params))
        return SearchResult(
            chunks=(
                ChunkHit("chunk-a", "doc-a", 2.0, 0, 10, None, None),
                ChunkHit("chunk-b", "doc-b", 1.0, 0, 5, None, None),
            ),
            docs=(
                DocHit("doc-a", 2.0, "chunk-a"),
                DocHit("doc-b", 1.0, "chunk-b"),
            ),
            embed_ms=None,
            search_ms=1.0,
        )

    monkeypatch.setattr("kodocs.eval.m5_runner.authorized_search", fake_search)
    config = RunConfig("composite", "fixed", "bm25", "standard", 50, None)

    [result] = run_questions(object(), object(), [_question()], config)

    assert calls[0][0] == "kodocs-m5-composite-fixed"
    assert (calls[0][1].team, calls[0][1].grade) == ("platform", 3)
    assert calls[0][2] == "배포 절차는 무엇인가요?"
    assert calls[0][3].method == "bm25"
    assert result.ranked_docs == ("doc-a", "doc-b")
    assert result.top10_chunks == (Span("doc-a", 0, 10), Span("doc-b", 0, 5))
    assert result.recall10 == 1.0
    assert result.ndcg10 == 1.0
    assert result.span_hit is True
    assert result.top1 is True


def test_runner_uses_none_for_questions_without_answer_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kodocs.eval.m5_runner.authorized_search",
        lambda *_args, **_kwargs: SearchResult(
            chunks=(), docs=(), embed_ms=None, search_ms=0.0
        ),
    )
    question = replace(_question(), answer_ranges=())

    [result] = run_questions(
        object(),
        object(),
        [question],
        RunConfig("composite", "fixed", "bm25", "nori", 50, None),
    )

    assert result.span_hit is None


def test_runner_forwards_prefer_current_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_search(*_args, **kwargs):
        calls.append(kwargs)
        return SearchResult(chunks=(), docs=(), embed_ms=None, search_ms=0.0)

    monkeypatch.setattr("kodocs.eval.m5_runner.authorized_search", fake_search)
    config = RunConfig("composite", "fixed", "bm25", "nori", 50, None)

    run_questions(object(), object(), [_question()], config)
    run_questions(object(), object(), [_question()], config, prefer_current=True)

    assert calls == [{}, {"prefer_current": True}]


def test_one_search_error_fails_the_combination_without_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def broken_search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("local search failed")
        return SearchResult(chunks=(), docs=(), embed_ms=None, search_ms=0.0)

    monkeypatch.setattr("kodocs.eval.m5_runner.authorized_search", broken_search)

    with pytest.raises(QuestionRunError, match="q-2.*local search failed") as caught:
        run_questions(
            object(),
            object(),
            [_question(), _question(qid="q-2")],
            RunConfig("composite", "fixed", "bm25", "nori", 50, None),
        )

    assert caught.value.qid == "q-2"
    assert calls == 2
