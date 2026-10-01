"""Offline M7 analysis of authenticated scores and registered human grades."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from kodocs.eval import m7_stats
from kodocs.eval.m7_config import CONFIG_PATH, read_json
from kodocs.eval.m7_grade import GRADES_PATH, SCORES_PATH, SUMMARY_PATH, tier
from kodocs.eval.m7_grading import (
    InputHashError,
    classify_all,
    load_m6_answers,
    verify_m6_inputs,
)
from kodocs.eval.m7_human import CSV_PATH, HumanGradesNotRegistered, load_human_grades
from kodocs.eval.m7_rawhash import RawHashMismatch, verify_fingerprint

ANALYSIS_PATH = Path("data/manifest/m7_analysis.json")
HUMAN_PATH = Path("data/manifest/m7_human_grades.json")


class IncompleteGrades(ValueError):
    """Missing or invalid repetitions; no statistical verdict may be published."""


def verify_analysis_inputs(root=Path(".")):
    root = Path(root)
    actual = verify_fingerprint(root / SUMMARY_PATH, "grades", root / GRADES_PATH)
    scores = read_json(root / SCORES_PATH)
    if scores.get("raw") != actual:
        raise RawHashMismatch("grades: scores fingerprint differs from current raw")
    frozen = read_json(root / CONFIG_PATH)
    verified = asdict(verify_m6_inputs(root))
    for key in (
        "dev_runs_sha256",
        "dev_runs_rows",
        "test_runs_sha256",
        "test_runs_rows",
    ):
        if frozen.get(key) != verified[key]:
            raise InputHashError(f"M7 frozen {key} mismatch")
    return scores


def _validate_scores(scores, runs, classes, h5_ids, human):
    missing = Counter()
    for run in runs:
        kind = classes[run.answer_id]
        row = scores.get(run.answer_id)
        valid = row is not None and row.get("class") == kind
        if valid:
            try:
                if kind == "gemini_acc":
                    m7_stats.median_int(row["correctness"])
                if kind in {"gemini_acc", "gemini_faith_only"}:
                    m7_stats.median_int(row["faithfulness"])
            except (TypeError, KeyError, ValueError):
                valid = False
        if not valid:
            missing[tier(run, h5_ids, human)] += 1
    # Kappa must never silently omit a human card even if it is not in runs.
    for answer_id in set(human) - {r.answer_id for r in runs}:
        missing[2] += 1
    if missing:
        counts = ", ".join(f"tier {t}: {n} answers" for t, n in sorted(missing.items()))
        raise IncompleteGrades(f"grading incomplete; missing/invalid {counts}")
    if set(scores) != set(classes):
        raise IncompleteGrades("grading manifest contains unexpected answer IDs")


def _metadata(runs):
    return {
        r.answer_id: {
            "question_id": r.question_id,
            "system": r.system,
            "type": r.type,
            "model": r.model,
            "run_set": r.run_set,
            "expected_response": r.expected_response,
        }
        for r in runs
    }


def run_analysis(*, root=Path("."), csv_path=None):
    root = Path(root)
    document = verify_analysis_inputs(root)
    if not (root / HUMAN_PATH).exists():
        raise HumanGradesNotRegistered("human grades manifest missing")
    human = load_human_grades(
        root / HUMAN_PATH,
        csv_path or root / CSV_PATH,
        root / "data/interim/m7/cards/key.json",
    )
    runs = load_m6_answers(root)
    classes = classify_all(runs)
    inputs = read_json(root / "data/manifest/m6_eval_inputs.json")
    scores = document["scores"]
    _validate_scores(scores, runs, classes, inputs["h5_pool"]["ids"], human)
    metadata = _metadata(runs)
    triples = {i: scores[i]["correctness"] for i in human}
    kappa = m7_stats.kappa_report(
        human, {i: m7_stats.median_int(v) for i, v in triples.items()}, repeats=triples
    )
    types = {r.question_id: r.type for r in runs}
    h5 = m7_stats.h5_report(
        scores,
        inputs["h5_pool"]["ids"],
        inputs["h5_pool_ex_sql"]["ids"],
        inputs["h5_silver_direction"]["ids"],
        types,
        human=human,
        kappa_passed=kappa.passed,
    )
    dev = {i: s for i, s in scores.items() if metadata[i]["run_set"] == "dev"}
    router_scores = {
        i: s
        for i, s in dev.items()
        if metadata[i]["model"] == "gpt-6-luna"
        and metadata[i]["expected_response"] == "answer"
    }
    router_pairs = m7_stats.question_scores(
        router_scores, {i: classes[i] for i in router_scores}
    )
    # Scope auxiliary tables by split/model so test and dev cannot be pooled.
    faithfulness, variability = {}, {}
    for split, model in sorted({(r.run_set, r.model) for r in runs}):
        subset = {
            i: s
            for i, s in scores.items()
            if metadata[i]["run_set"] == split and metadata[i]["model"] == model
        }
        key = f"{split}|{model}"
        faithfulness[key] = m7_stats.faithfulness_table(subset, metadata)
        variability[key] = m7_stats.repeat_variability(subset)
    result = {
        "schema_version": 1,
        "raw": document["raw"],
        "scores": {
            i: {
                "class": s["class"],
                "correctness": [0, 0, 0]
                if s["class"] == "det_zero"
                else s["correctness"]
                if s["class"] == "gemini_acc"
                else [],
                "faithfulness": s["faithfulness"]
                if s["class"] in {"gemini_acc", "gemini_faith_only"}
                else [],
            }
            for i, s in scores.items()
        },
        "kappa": asdict(kappa),
        "h5": asdict(h5),
        "sol": m7_stats.sol_report(dev, metadata),
        "router": m7_stats.router_report(router_pairs, types),
        "faithfulness": faithfulness,
        "variability": variability,
    }
    path = root / ANALYSIS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result


def print_summary(result):
    k = result["kappa"]
    print(
        f"κ quadratic={k['quadratic_kappa']:.4f}, linear={k['linear_kappa']:.4f}, 95% CI=[{k['ci_low']:.4f}, {k['ci_high']:.4f}]"
    )
    print("채점 검증 통과" if k["passed"] else "채점 모델 검증 미달")
    if k["uninterpretable"]:
        print("κ 해석 불가(사람 점수 한 값에 80% 이상)")
    h = result["h5"]
    print(h["verdict_text"])
    for label, comparison in (
        ("H5", h["primary"]),
        ("sql 제외(판정 아님)", h["sql_excluded"]),
        ("실버 방향(판정 아님)", h["silver_direction"]["comparison"]),
        ("사람 확인용", h["human"]),
    ):
        if comparison:
            print(
                f"{label}: Δ={comparison['mean_diff']:.4f}, 95% CI=[{comparison['ci_low']:.4f}, {comparison['ci_high']:.4f}], n={comparison['n']}"
            )
    if h["depends_on_sql"]:
        print("채택이 sql에 의존")
    print(result["sol"]["note"])
    print(f"라우터 착수={result['router']['launch']}: {result['router']['note']}")
    print(m7_stats.FAITH_NOTE)
    print("H6: 보안 결과 없음(step 9 이후)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--csv-path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_analysis(root=args.root, csv_path=args.csv_path)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"M7 analysis refused: {type(exc).__name__}: {exc}")
        return 2
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
