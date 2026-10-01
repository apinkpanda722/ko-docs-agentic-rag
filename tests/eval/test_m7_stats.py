"""Preregistered M7 statistics, using only fabricated scores."""

from dataclasses import asdict

import pytest

from kodocs.eval import m5_stats
from kodocs.eval import m7_stats as stats


def aid(q, system, model="gpt-6-luna", split="test_gold"):
    return f"{split}|{q}|{system}|{model}"


def score(values, kind="gemini_acc"):
    return {"class": kind, "correctness": values, "faithfulness": values}


@pytest.mark.parametrize("weights", ["linear", "quadratic"])
def test_m7_stats_perfect_and_degenerate(weights):
    assert stats.weighted_kappa([0, 1, 2, 3], [0, 1, 2, 3], weights=weights) == 1
    assert stats.weighted_kappa([2, 2], [2, 2], weights=weights) == 1
    assert stats.weighted_kappa([0, 0], [1, 1], weights=weights) == 0


@pytest.mark.parametrize(
    "a,b,categories,quadratic,linear",
    [
        # Three observations: observed weighted distances 1/3, expected 1
        # (linear); squared observed 1/3, expected 5/3 (quadratic).
        ([0, 1, 2], [0, 2, 2], (0, 1, 2), 4 / 5, 2 / 3),
        # Four observations: observed distances 1/2, expected 5/4;
        # squared expected distance 5/2.
        ([0, 1, 2, 3], [0, 2, 1, 3], (0, 1, 2, 3), 4 / 5, 3 / 5),
    ],
)
def test_m7_stats_hand_calculated(a, b, categories, quadratic, linear):
    assert stats.weighted_kappa(
        a, b, weights="quadratic", categories=categories
    ) == pytest.approx(quadratic)
    assert stats.weighted_kappa(
        a, b, weights="linear", categories=categories
    ) == pytest.approx(linear)


def test_m7_stats_question_scores_and_median():
    scores = {
        aid("q", "agentic"): score([0, 2, 3]),
        aid("q", "simple"): score([], "det_zero"),
        aid("x", "simple"): score([], "not_scored"),
    }
    assert stats.question_scores(
        scores, {i: s["class"] for i, s in scores.items()}
    ) == {("q", "agentic"): 5 / 3, ("q", "simple"): 0}
    assert stats.median_int([3, 0, 2]) == 2
    with pytest.raises(ValueError):
        stats.median_int([1, 2])


def test_m7_stats_kappa_report():
    human = {str(i): i % 4 for i in range(8)}
    result = stats.kappa_report(human, {i: [v, v, v] for i, v in human.items()})
    assert result.passed and result.quadratic_kappa == 1
    assert result.individual_kappas == (1, 1, 1)
    assert result.ci_low == result.ci_high == 1
    assert result.seed == 20261001 and result.n_boot == 10000
    assert not result.uninterpretable
    human = {str(i): 0 if i < 8 else 1 for i in range(10)}
    assert stats.kappa_report(
        human, {i: [v] * 3 for i, v in human.items()}
    ).uninterpretable


def h5_fixture():
    ids = [f"q{i}" for i in range(13)]
    types = {
        q: "identifier" if i < 4 else "multi_doc" if i < 9 else "sql"
        for i, q in enumerate(ids)
    }
    scores = {
        aid(q, s): score([2] * 3 if s == "agentic" else [0] * 3)
        for q in ids
        for s in ("simple", "agentic")
    }
    human = {i: 1 if "|agentic|" in i else 0 for i in scores}
    return ids, types, scores, human


@pytest.mark.parametrize(
    "low,high,text",
    [
        (0.1, 1, stats.H5_ACCEPT),
        (-1, -0.1, stats.H5_REJECT),
        (0, 1, stats.H5_HOLD),
        (-1, 0, stats.H5_HOLD),
    ],
)
def test_m7_stats_verdict_boundaries(low, high, text):
    assert stats.h5_verdict(low, high) == text


def test_m7_stats_h5_bootstrap_and_human_fallback():
    ids, types, scores, human = h5_fixture()
    result = stats.h5_report(
        scores, ids, ids[:9], [], types, human=human, kappa_passed=True
    )
    assert asdict(result.primary) == asdict(
        m5_stats.paired_bootstrap([2] * 13, [0] * 13, seed=20260930, n_boot=10000)
    )
    assert result.verdict_text == stats.H5_ACCEPT
    result = stats.h5_report(
        scores, ids, ids[:9], [], types, human=human, kappa_passed=False
    )
    assert result.primary.mean_diff == 1
    assert "채점 모델 검증 미달, 사람 채점 기반" in result.verdict_text
    assert result.by_type["sql"]["n"] == 4
    del human[aid(ids[0], "agentic")]
    with pytest.raises(stats.HumanCoverageError):
        stats.h5_report(scores, ids, ids[:9], [], types, human=human, kappa_passed=True)


def test_m7_stats_sql_dependence():
    ids, types, scores, human = h5_fixture()
    for q in ids[:9]:
        scores[aid(q, "simple")] = score([2] * 3)
    result = stats.h5_report(
        scores, ids, ids[:9], [], types, human=human, kappa_passed=True
    )
    assert result.depends_on_sql
    assert result.sql_excluded.ci_low == result.sql_excluded.ci_high == 0


def test_m7_stats_deterministic_zero_human_coverage():
    ids, types, scores, human = h5_fixture()
    i = aid(ids[-1], "simple")
    scores[i] = score([], "det_zero")
    del human[i]
    assert (
        stats.h5_report(
            scores, ids, ids[:9], [], types, human=human, kappa_passed=False
        ).human.n
        == 13
    )


def test_m7_stats_router_boundaries_and_small_types():
    pairs, types = {}, {}
    for t, n, diff in [
        ("a", 8, 0.5),
        ("b", 8, -0.5),
        ("small", 7, -2),
        ("neutral", 8, 0.1),
    ]:
        for j in range(n):
            q = f"{t}{j}"
            types[q] = t
            pairs[q, "simple"] = 1
            pairs[q, "agentic"] = 1 + diff
    result = stats.router_report(pairs, types)
    assert result["launch"]
    assert result["by_type"]["small"]["eligible"] is False
    assert result["routing"] == {
        "a": "agentic",
        "b": "simple",
        "small": "agentic",
        "neutral": "agentic",
    }
    assert "m7b-router" in result["note"]
    assert not stats.router_report(
        {k: v for k, v in pairs.items() if types[k[0]] != "b"}, types
    )["launch"]


def test_m7_stats_faithfulness_and_variability():
    scores = {
        aid("q", "simple"): score([0, 1, 2]),
        aid("z", "simple"): score([], "det_zero"),
    }
    meta = {
        i: {"system": "simple", "type": "identifier", "model": "gpt-6-luna"}
        for i in scores
    }
    table = stats.faithfulness_table(scores, meta)
    assert "사람 검증 없음" in table["note"]
    assert table["rows"][0]["mean"] == 1
    variation = stats.repeat_variability(scores)
    assert variation["correctness"]["n"] == 2
    assert variation["correctness"]["all_equal_rate"] == 0.5
    assert variation["faithfulness"]["n"] == 1


def test_m7_stats_sol_signal():
    scores, meta = {}, {}
    for model, value in [("gpt-6-luna", 1), ("gpt-6.1-sol", 2)]:
        for s in ("simple", "agentic"):
            i = aid("q", s, model, "dev")
            scores[i] = score([value] * 3)
            meta[i] = {
                "question_id": "q",
                "system": s,
                "type": "identifier",
                "model": model,
            }
    report = stats.sol_report(scores, meta)
    assert report["recheck_signal"]
    assert "판정 아님" in report["note"]
    assert all(r["comparison"]["mean_diff"] == 1 for r in report["comparisons"])


def test_m7_stats_kappa_medians_individual_and_ci_deterministic():
    human = {str(i): v for i, v in enumerate([0, 1, 2, 3])}
    triples = {
        str(i): v for i, v in enumerate([[0, 3, 0], [1, 0, 1], [2, 0, 2], [3, 0, 3]])
    }
    result = stats.kappa_report(human, triples)
    assert result.quadratic_kappa == 1
    assert result.individual_kappas[0] == 1
    assert result.individual_kappas[1] < 1
    assert result.individual_kappas[2] == 1
    assert result == stats.kappa_report(human, triples)


def test_m7_stats_h5_nonconstant_matches_m5_and_seed(monkeypatch):
    ids, types, scores, human = h5_fixture()
    for i, q in enumerate(ids):
        scores[aid(q, "agentic")] = score([i % 4] * 3)
    calls = []
    original = m5_stats.paired_bootstrap

    def spy(a, b, **kwargs):
        calls.append(kwargs)
        return original(a, b, **kwargs)

    monkeypatch.setattr(m5_stats, "paired_bootstrap", spy)
    result = stats.h5_report(
        scores, ids, ids[:9], [], types, human=human, kappa_passed=True
    )
    expected = original(
        [i % 4 for i in range(13)], [0] * 13, seed=20260930, n_boot=10000
    )
    assert result.primary == expected
    assert all(c == {"seed": 20260930, "n_boot": 10000} for c in calls)


def test_m7_stats_verdict_constants_match_preregistration():
    from pathlib import Path

    rules = Path("docs/results/hypotheses.md").read_text()
    assert all(
        f'"{v}"' in rules for v in (stats.H5_ACCEPT, stats.H5_REJECT, stats.H5_HOLD)
    )


@pytest.mark.parametrize("values", [[0, None, 2], [0, 4, 2], [0, True, 2]])
def test_m7_stats_reject_invalid_scores(values):
    with pytest.raises(ValueError):
        stats.median_int(values)


def test_m7_stats_kappa_threshold_inclusive():
    human = {str(i): i for i in range(4)}
    triples = {str(i): [v] * 3 for i, v in enumerate([1, 0, 3, 2])}
    result = stats.kappa_report(human, triples)
    assert result.quadratic_kappa == 0.6
    assert result.passed


def test_m7_stats_sol_does_not_fabricate_unscored_pairs():
    scores, meta = {}, {}
    for model, kind in [("gpt-6-luna", "det_zero"), ("gpt-6.1-sol", "not_scored")]:
        i = aid("q", "simple", model, "dev")
        scores[i] = score([], kind)
        meta[i] = {
            "question_id": "q",
            "system": "simple",
            "type": "unanswerable",
            "model": model,
            "expected_response": "no_answer",
        }
    result = stats.sol_report(scores, meta)
    assert result["rows"][0]["mean"] == 0
    assert not result["comparisons"]
    assert result["unpaired"] == [
        {"question_id": "q", "system": "simple", "type": "unanswerable"}
    ]
    assert not result["recheck_signal"]


def test_m7_stats_sol_overall_pairs_questions_before_signal():
    scores, metadata = {}, {}
    for typ, luna, sol in [("identifier", 1, 2), ("multi_doc", 2, 1)]:
        for j in range(4):
            q = f"{typ}{j}"
            for system in ("simple", "agentic"):
                for model, value in [("gpt-6-luna", luna), ("gpt-6.1-sol", sol)]:
                    i = aid(q, system, model, "dev")
                    scores[i] = score([value] * 3)
                    metadata[i] = {
                        "question_id": q,
                        "system": system,
                        "type": typ,
                        "model": model,
                    }
    result = stats.sol_report(scores, metadata)
    assert result["overall"]["n"] == 8
    assert result["overall"]["mean_diff"] == 0
    assert result["overall"]["ci_low"] <= 0 <= result["overall"]["ci_high"]
    assert not result["recheck_signal"]
