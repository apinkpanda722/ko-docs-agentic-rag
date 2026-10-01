"""Offline analysis rejects unauthenticated and incomplete inputs."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from kodocs.eval import m7_analyze as analyze
from kodocs.eval.m7_grading import InputHashError, InputVerification
from kodocs.eval.m7_human import HumanGradesNotRegistered
from kodocs.eval.m7_rawhash import RawHashMismatch, fingerprint, record_fingerprint


def write(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    raw = tmp_path / "data/interim/m7/grades.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"repeat":1}\n')
    summary = tmp_path / "data/manifest/m7_grade_summary.json"
    record_fingerprint(summary, "grades", raw, "completed")
    write(
        tmp_path,
        "data/manifest/m7_grade_scores.json",
        {"raw": fingerprint(raw), "scores": {}},
    )
    check = InputVerification(
        "dev", 4, {"gold": "gold", "silver": "silver"}, {"gold": 2, "silver": 2}, ()
    )
    write(tmp_path, "data/manifest/m7_config.json", asdict(check))
    monkeypatch.setattr(analyze, "verify_m6_inputs", lambda root: check)
    return tmp_path, raw


@pytest.mark.parametrize("change", ["modify", "append", "delete"])
def test_m7_analyze_raw_tamper(inputs, change):
    root, raw = inputs
    raw.write_text(
        {"modify": '{"repeat":2}\n', "append": '{"repeat":1}\n{}\n', "delete": ""}[
            change
        ]
    )
    with pytest.raises(RawHashMismatch):
        analyze.verify_analysis_inputs(root)


def test_m7_analyze_score_fingerprint(inputs):
    root, _ = inputs
    write(
        root,
        "data/manifest/m7_grade_scores.json",
        {"raw": {"sha256": "wrong", "rows": 1}, "scores": {}},
    )
    with pytest.raises(RawHashMismatch):
        analyze.verify_analysis_inputs(root)


def test_m7_analyze_m6_freeze_mismatch(inputs):
    root, _ = inputs
    write(root, "data/manifest/m7_config.json", {"dev_runs_sha256": "wrong"})
    with pytest.raises(InputHashError):
        analyze.verify_analysis_inputs(root)


def test_m7_analyze_unregistered_human(inputs):
    root, _ = inputs
    with pytest.raises(HumanGradesNotRegistered):
        analyze.run_analysis(root=root)
    assert not (root / "data/manifest/m7_analysis.json").exists()


def test_m7_analyze_incomplete_no_verdict(inputs, monkeypatch):
    root, _ = inputs
    write(root, "data/manifest/m7_human_grades.json", {})
    monkeypatch.setattr(analyze, "load_human_grades", lambda *args: {"missing": 1})
    run = SimpleNamespace(
        answer_id="test_gold|q|agentic|gpt-6-luna",
        run_set="test_gold",
        question_id="q",
        type="identifier",
        expected_response="answer",
        model="gpt-6-luna",
        system="agentic",
    )
    monkeypatch.setattr(analyze, "load_m6_answers", lambda root: [run])
    monkeypatch.setattr(
        analyze, "classify_all", lambda runs: {run.answer_id: "gemini_acc"}
    )
    write(root, "data/manifest/m6_eval_inputs.json", {"h5_pool": {"ids": ["q"]}})
    with pytest.raises(analyze.IncompleteGrades, match="tier 1"):
        analyze.run_analysis(root=root)
    assert not (root / "data/manifest/m7_analysis.json").exists()


def test_m7_analyze_complete_fixture_and_cli(inputs, monkeypatch, capsys):
    root, raw = inputs
    runs, scores, human = [], {}, {}
    ids = [f"h{i}" for i in range(13)]
    silver = [f"s{i}" for i in range(29)]

    def add(q, typ, split, system, model, kind="gemini_acc", expected="answer"):
        i = f"{split}|{q}|{system}|{model}"
        runs.append(
            SimpleNamespace(
                answer_id=i,
                question_id=q,
                type=typ,
                run_set=split,
                system=system,
                model=model,
                expected_response=expected,
            )
        )
        value = int(q[-1]) % 4 if q[-1].isdigit() else 1
        values = [value] * 3 if kind.startswith("gemini") else []
        scores[i] = {
            "class": kind,
            "correctness": values if kind == "gemini_acc" else [],
            "faithfulness": values,
            "rationale": "private source text",
        }
        return i, value

    for j, q in enumerate(ids):
        typ = "identifier" if j < 4 else "multi_doc" if j < 9 else "sql"
        for system in ("simple", "agentic"):
            kind = "det_zero" if typ == "sql" and system == "simple" else "gemini_acc"
            i, value = add(q, typ, "test_gold", system, "gpt-6-luna", kind)
            if kind == "gemini_acc":
                human[i] = value
    for j, q in enumerate(silver):
        for system in ("simple", "agentic"):
            add(
                q,
                "identifier" if j < 15 else "multi_doc",
                "test_silver",
                system,
                "gpt-6-luna",
            )
    for j in range(8):
        for system in ("simple", "agentic"):
            for model in ("gpt-6-luna", "gpt-6.1-sol"):
                i, value = add(f"d{j}", "identifier", "dev", system, model)
                if model == "gpt-6-luna":
                    human[i] = value
    add(
        "noanswer",
        "unanswerable",
        "dev",
        "simple",
        "gpt-6-luna",
        "not_scored",
        "no_answer",
    )
    add(
        "noanswer",
        "unanswerable",
        "dev",
        "agentic",
        "gpt-6-luna",
        "gemini_faith_only",
        "no_answer",
    )
    write(root, "data/manifest/m7_human_grades.json", {})
    write(
        root,
        "data/manifest/m7_grade_scores.json",
        {"scores": scores, "raw": fingerprint(raw)},
    )
    write(
        root,
        "data/manifest/m6_eval_inputs.json",
        {
            "h5_pool": {"ids": ids},
            "h5_pool_ex_sql": {"ids": ids[:9]},
            "h5_silver_direction": {"ids": silver},
        },
    )
    monkeypatch.setattr(analyze, "load_human_grades", lambda *args: human)
    monkeypatch.setattr(analyze, "load_m6_answers", lambda root: runs)
    monkeypatch.setattr(
        analyze, "classify_all", lambda runs: {i: s["class"] for i, s in scores.items()}
    )
    assert analyze.main(["--root", str(root)]) == 0
    result = json.loads((root / analyze.ANALYSIS_PATH).read_text())
    assert "h6" not in result
    assert "H6: 보안 결과 없음(step 9 이후)" in capsys.readouterr().out
    assert "private source text" not in json.dumps(result)
    assert result["h5"]["primary"]["n"] == 13
    assert result["h5"]["sql_excluded"]["n"] == 9
    assert result["h5"]["silver_direction"]["comparison"]["n"] == 29
    assert result["router"]["by_type"]["identifier"]["n"] == 8
    assert "unanswerable" not in result["router"]["by_type"]
    # Tier 2 must fail before computing or printing any verdict.
    missing = next(i for i in human if i.startswith("dev|"))
    scores[missing]["correctness"] = [None, None, 1]  # fewer than two scores
    write(
        root,
        "data/manifest/m7_grade_scores.json",
        {"scores": scores, "raw": fingerprint(raw)},
    )
    assert analyze.main(["--root", str(root)]) == 2
    output = capsys.readouterr().out
    assert "tier 2" in output
    assert "H5 채택" not in output and "H5 판정" not in output


@pytest.mark.parametrize(
    "field", ["dev_runs_rows", "test_runs_sha256", "test_runs_rows"]
)
def test_m7_analyze_all_m6_fingerprint_fields(inputs, field):
    root, _ = inputs
    path = root / "data/manifest/m7_config.json"
    frozen = json.loads(path.read_text())
    frozen[field] = "tampered"
    path.write_text(json.dumps(frozen))
    with pytest.raises(InputHashError, match=field):
        analyze.verify_analysis_inputs(root)


def test_m7_analyze_latest_raw_record(inputs):
    root, raw = inputs
    raw.write_text("{}\n{}\n")
    record_fingerprint(root / analyze.SUMMARY_PATH, "grades", raw, "completed")
    write(
        root,
        "data/manifest/m7_grade_scores.json",
        {"raw": fingerprint(raw), "scores": {}},
    )
    assert analyze.verify_analysis_inputs(root)["raw"] == fingerprint(raw)
