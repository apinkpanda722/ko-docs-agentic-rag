"""Pure, preregistered M7 statistics; no I/O, calls, or new dependencies."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Literal

from kodocs.eval import m5_stats

H5_ACCEPT = "H5 채택: Agentic RAG의 정답성이 단순 RAG보다 높다(차이, 95% CI, n=13)."
H5_REJECT = (
    "H5 기각(반대 방향): 단순 RAG의 정답성이 Agentic RAG보다 높다(차이, 95% CI, n=13)."
)
H5_HOLD = "H5 판정 보류(차이 입증 못 함): CI가 0을 포함한다. 동등성 주장이 아니다."
HUMAN_FALLBACK = "채점 모델 검증 미달, 사람 채점 기반"
FAITH_NOTE = "사람 검증 없음(보조 지표)"


class HumanCoverageError(ValueError):
    """An H5 answer lacks an independently registered human score."""


def _score(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(4):
        raise ValueError("scores must be integer categories 0..3")
    return value


def _triple(values):
    """Scores present in the three repeat slots.

    A slot is None only when its format error repeated after the single retry
    (implementation-defect handling recorded in hypotheses.md, not a rule change);
    at least two scores must remain.
    """
    if len(values) != 3:
        raise ValueError("exactly three score slots required")
    present = tuple(_score(v) for v in values if v is not None)
    if len(present) < 2:
        raise ValueError("at least two scores required")
    return present


def median_int(scores3) -> int:
    """Median of three; with one missing slot the lower of the two scores."""
    present = sorted(_triple(scores3))
    return present[1] if len(present) == 3 else present[0]


def _identity(answer_id):
    split, question, system, model = answer_id.split("|")
    return split, question, system, model


def question_scores(scores, classes) -> dict[tuple[str, str], float]:
    """Aggregate one split/model; callers must scope before combining identities."""
    result = {}
    for answer_id, kind in classes.items():
        if kind not in {"gemini_acc", "det_zero"}:
            continue
        _, question, system, _ = _identity(answer_id)
        key = question, system
        if key in result:
            raise ValueError("scope question_scores to one split and model")
        result[key] = (
            0.0
            if kind == "det_zero"
            else statistics.mean(_triple(scores[answer_id]["correctness"]))
        )
    return result


def weighted_kappa(
    a, b, *, weights: Literal["quadratic", "linear"], categories=(0, 1, 2, 3)
) -> float:
    """Weighted disagreement κ; identical constant samples use the limit 1.

    Zero expected disagreement otherwise has no defined κ. The constant
    agreement convention is accompanied by the concentration warning in reports.
    """
    if not a or len(a) != len(b):
        raise ValueError("nonempty equally sized paired scores required")
    if weights not in {"quadratic", "linear"}:
        raise ValueError("unknown weights")
    if len(categories) < 2 or len(set(categories)) != len(categories):
        raise ValueError("distinct ordinal categories required")
    positions = {v: i for i, v in enumerate(categories)}
    if any(v not in positions for v in (*a, *b)):
        raise ValueError("score outside categories")
    left, right = Counter(a), Counter(b)
    power = 2 if weights == "quadratic" else 1

    def distance(x, y):
        return abs(positions[x] - positions[y]) ** power

    observed = sum(distance(x, y) for x, y in zip(a, b)) / len(a)
    expected = (
        sum(
            nx * ny * distance(x, y)
            for x, nx in left.items()
            for y, ny in right.items()
        )
        / len(a) ** 2
    )
    return 1.0 if expected == 0 else 1 - observed / expected


@dataclass(frozen=True)
class KappaReport:
    n: int
    quadratic_kappa: float
    linear_kappa: float
    exact_agreement: float
    within_one_agreement: float
    individual_kappas: tuple[float, ...]
    ci_low: float
    ci_high: float
    human_mode_share: float
    uninterpretable: bool
    passed: bool
    n_boot: int = 10000
    seed: int = 20261001


def kappa_report(human, gemini_median, *, repeats=None) -> KappaReport:
    """Compare humans with medians; triples may be supplied directly or separately."""
    if not human or not set(human) <= set(gemini_median):
        raise HumanCoverageError("human/median pairs missing")
    ids = sorted(human)
    a = [_score(human[i]) for i in ids]
    triples = repeats
    if all(isinstance(gemini_median[i], (list, tuple)) for i in ids):
        triples = gemini_median
        b = [median_int(gemini_median[i]) for i in ids]
    else:
        b = [_score(gemini_median[i]) for i in ids]
    individual = ()
    if triples is not None:
        checked = [triples[i] for i in ids]
        for t in checked:
            _triple(t)
        if [median_int(t) for t in checked] != b:
            raise ValueError("median/repeats mismatch")
        individual = tuple(
            weighted_kappa(
                [x for x, t in zip(a, checked) if t[j] is not None],
                [t[j] for t in checked if t[j] is not None],
                weights="quadratic",
            )
            for j in range(3)
        )
    quadratic = weighted_kappa(a, b, weights="quadratic")
    rng = random.Random(20261001)
    boot = []
    for _ in range(10000):
        indices = [rng.randrange(len(a)) for _ in a]
        boot.append(
            weighted_kappa(
                [a[j] for j in indices], [b[j] for j in indices], weights="quadratic"
            )
        )
    boot.sort()
    low, high = boot[math.floor(0.025 * 9999)], boot[math.ceil(0.975 * 9999)]
    peak = max(Counter(a).values()) / len(a)
    return KappaReport(
        len(a),
        quadratic,
        weighted_kappa(a, b, weights="linear"),
        sum(x == y for x, y in zip(a, b)) / len(a),
        sum(abs(x - y) <= 1 for x, y in zip(a, b)) / len(a),
        individual,
        low,
        high,
        peak,
        peak >= 0.8,
        quadratic >= 0.6,
    )


def h5_verdict(ci_low, ci_high):
    return H5_ACCEPT if ci_low > 0 else H5_REJECT if ci_high < 0 else H5_HOLD


def _scoped(scores, split, model="gpt-6-luna"):
    scoped = {
        i: s
        for i, s in scores.items()
        if _identity(i)[0] == split and _identity(i)[3] == model
    }
    return question_scores(scoped, {i: s["class"] for i, s in scoped.items()})


def _paired(pairs, ids):
    if not ids:
        return None
    return m5_stats.paired_bootstrap(
        [pairs[q, "agentic"] for q in ids],
        [pairs[q, "simple"] for q in ids],
        n_boot=10000,
        seed=20260930,
    )


def _description(pairs, ids):
    return {
        "n": len(ids),
        "agentic_mean": statistics.mean(pairs[q, "agentic"] for q in ids),
        "simple_mean": statistics.mean(pairs[q, "simple"] for q in ids),
        "mean_diff": statistics.mean(
            pairs[q, "agentic"] - pairs[q, "simple"] for q in ids
        ),
    }


@dataclass(frozen=True)
class H5Report:
    primary: m5_stats.BootstrapResult
    verdict_text: str
    input_source: str
    by_type: dict
    sql_excluded: m5_stats.BootstrapResult | None
    depends_on_sql: bool
    silver_direction: dict
    human: m5_stats.BootstrapResult
    variability: dict
    auxiliary_note: str = "sql 제외·실버 방향·사람 확인용 값은 판정 아님"


def h5_report(
    scores, pool_ids, ex_sql_ids, silver_ids, types, *, human, kappa_passed
) -> H5Report:
    if len(pool_ids) != 13 or len(set(pool_ids)) != 13:
        raise ValueError("H5 requires 13 distinct questions")
    if not set(ex_sql_ids) <= set(pool_ids):
        raise ValueError("sql exclusion must be a subset of H5")
    gemini = _scoped(scores, "test_gold")
    human_pairs = {}
    for q in pool_ids:
        for system in ("simple", "agentic"):
            answer_id = f"test_gold|{q}|{system}|gpt-6-luna"
            if scores.get(answer_id, {}).get("class") == "det_zero":
                human_pairs[q, system] = 0
            elif answer_id in human:
                human_pairs[q, system] = _score(human[answer_id])
            else:
                raise HumanCoverageError(f"H5 human score missing: {answer_id}")
    pairs = gemini if kappa_passed else human_pairs
    primary = _paired(pairs, pool_ids)
    excluded = _paired(pairs, ex_sql_ids)
    verdict = h5_verdict(primary.ci_low, primary.ci_high)
    if not kappa_passed:
        verdict += " " + HUMAN_FALLBACK
    by_type = {
        t: _description(pairs, [q for q in pool_ids if types[q] == t])
        for t in sorted({types[q] for q in pool_ids})
    }
    silver = _scoped(scores, "test_silver")
    direction = {
        "comparison": asdict(_paired(silver, silver_ids)) if silver_ids else None,
        "by_type": {
            t: _description(silver, [q for q in silver_ids if types[q] == t])
            for t in sorted({types[q] for q in silver_ids})
        },
        "note": "방향 확인(판정 아님)",
    }
    selected = {
        i: s
        for i, s in scores.items()
        if _identity(i)[0] == "test_gold"
        and _identity(i)[1] in pool_ids
        and _identity(i)[3] == "gpt-6-luna"
    }
    return H5Report(
        primary,
        verdict,
        "gemini_mean" if kappa_passed else "human",
        by_type,
        excluded,
        primary.ci_low > 0
        and excluded is not None
        and excluded.ci_low <= 0 <= excluded.ci_high,
        direction,
        _paired(human_pairs, pool_ids),
        repeat_variability(selected),
    )


def repeat_variability(scores):
    """Sample SD across three repetitions, including deterministic correctness zeros."""
    result = {}
    for field in ("correctness", "faithfulness"):
        triples = []
        for row in scores.values():
            kind = row["class"]
            if field == "correctness" and kind == "det_zero":
                triples.append((0, 0, 0))
            elif (
                kind == "gemini_acc"
                or field == "faithfulness"
                and kind == "gemini_faith_only"
            ):
                triples.append(_triple(row[field]))
        result[field] = {
            "n": len(triples),
            "sd_convention": "sample (ddof=1)",
            "mean_sd": statistics.mean(statistics.stdev(t) for t in triples)
            if triples
            else None,
            "all_equal_rate": sum(len(set(t)) == 1 for t in triples) / len(triples)
            if triples
            else None,
        }
    return result


def faithfulness_table(scores, metadata):
    """Mean faithfulness; answers with no evidence text are listed, not averaged."""
    groups, no_evidence = defaultdict(list), defaultdict(int)
    for i, row in scores.items():
        if row["class"] in {"gemini_acc", "gemini_faith_only"}:
            m = metadata[i]
            if row.get("evidence_empty_reason"):
                no_evidence[m["system"], m["type"]] += 1
                continue
            groups[m["system"], m["type"]].append(
                statistics.mean(_triple(row["faithfulness"]))
            )
    keys = sorted(set(groups) | set(no_evidence))
    return {
        "note": FAITH_NOTE,
        "rows": [
            {
                "system": s,
                "type": t,
                "n": len(groups.get((s, t), [])),
                "n_no_evidence": no_evidence.get((s, t), 0),
                "mean": statistics.mean(groups[s, t]) if groups.get((s, t)) else None,
                "note": FAITH_NOTE,
            }
            for s, t in keys
        ],
    }


def sol_report(scores, metadata):
    groups, pairs = defaultdict(list), defaultdict(dict)
    for i, row in scores.items():
        if row["class"] not in {"gemini_acc", "det_zero"}:
            continue
        m = metadata[i]
        value = (
            0
            if row["class"] == "det_zero"
            else statistics.mean(_triple(row["correctness"]))
        )
        groups[m["model"], m["system"], m["type"]].append(value)
        pairs[m["system"], m["type"]].setdefault(m["question_id"], {})[m["model"]] = (
            value
        )
    comparisons, unpaired = [], []
    question_pairs = defaultdict(dict)
    for (system, typ), questions in sorted(pairs.items()):
        complete = [
            v
            for _, v in sorted(questions.items())
            if {"gpt-6.1-sol", "gpt-6-luna"} <= v.keys()
        ]
        unpaired.extend(
            {"system": system, "type": typ, "question_id": q}
            for q, values in sorted(questions.items())
            if not {"gpt-6.1-sol", "gpt-6-luna"} <= values.keys()
        )
        for q, values in sorted(questions.items()):
            if {"gpt-6.1-sol", "gpt-6-luna"} <= values.keys():
                question_pairs[q][system] = values
        if not complete:
            continue
        comparison = m5_stats.paired_bootstrap(
            [v["gpt-6.1-sol"] for v in complete],
            [v["gpt-6-luna"] for v in complete],
            n_boot=10000,
            seed=20260930,
        )
        comparisons.append(
            {
                "system": system,
                "type": typ,
                "comparison": asdict(comparison),
                "recheck_signal": comparison.ci_low > 0,
                "note": "판정 아님",
            }
        )
    # Match M6's model comparison unit: mean across two systems per question.
    paired_questions = [
        v
        for _, v in sorted(question_pairs.items())
        if {"simple", "agentic"} <= v.keys()
    ]
    overall = (
        m5_stats.paired_bootstrap(
            [
                statistics.mean(v[s]["gpt-6.1-sol"] for s in ("simple", "agentic"))
                for v in paired_questions
            ],
            [
                statistics.mean(v[s]["gpt-6-luna"] for s in ("simple", "agentic"))
                for v in paired_questions
            ],
            n_boot=10000,
            seed=20260930,
        )
        if paired_questions
        else None
    )
    signal = overall is not None and overall.ci_low > 0
    return {
        "rows": [
            {
                "model": m,
                "system": s,
                "type": t,
                "n": len(v),
                "mean": statistics.mean(v),
                "note": "판정 아님",
            }
            for (m, s, t), v in sorted(groups.items())
        ],
        "comparisons": comparisons,
        "overall": asdict(overall) if overall else None,
        "overall_unit": "문항별 두 시스템 평균(Sol − Luna, 판정 아님)",
        "unpaired": unpaired,
        "recheck_signal": signal,
        "note": "선택 재검토 신호(판정 아님)"
        if signal
        else "모델 선택 사후 확인(판정 아님)",
    }


def router_report(pairs, types):
    groups = defaultdict(list)
    for question, system in pairs:
        if system == "agentic":
            groups[types[question]].append(question)
    by_type = {}
    for typ, ids in sorted(groups.items()):
        by_type[typ] = _description(pairs, ids) | {"eligible": len(ids) >= 8}
    launch = any(
        r["eligible"] and r["mean_diff"] >= 0.5 for r in by_type.values()
    ) and any(r["eligible"] and r["mean_diff"] <= -0.5 for r in by_type.values())
    routing = {
        t: "simple" if r["eligible"] and r["mean_diff"] <= -0.5 else "agentic"
        for t, r in by_type.items()
    }
    return {
        "by_type": by_type,
        "launch": launch,
        "routing": routing,
        "note": "m7b-router phase를 별도 설계해 승인받아야 한다."
        if launch
        else "라우터 미착수(dev 유형별 차이 조건 미충족).",
    }
