from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_m6_dev import _dev_rows

from kodocs.eval import m7_grading as grading
from kodocs.eval.golden import write_questions
from kodocs.eval.m6_dev import analyze_dev
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.eval.m6_test import analyze_test
from kodocs.ingest.manifest import sha256_file
from kodocs.llm.answer_settings import load_answer_config


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def rows_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


@pytest.fixture
def inputs(tmp_path):
    # Only committed questions and the existing M6 fake-row helper are reused.
    questions = load_m6_questions(Path("."), split="dev")
    tests = load_m6_questions(Path("."), split="test")
    for name in ("gold", "silver"):
        write_questions(
            tmp_path / f"data/golden/queries/{name}.jsonl",
            [q for q in questions + tests if q.set == name],
        )
    dump(tmp_path / "data/manifest/m6_eval_inputs.json", {})
    dump(tmp_path / "data/synthetic/canaries.json", {})
    configs = [load_answer_config(n) for n in ("luna", "sol")]
    rows = _dev_rows(questions, configs)
    for i, row in enumerate(rows):
        row["cache_key"] = str(i)
        row["run_seq"] = 1
    rows_file(tmp_path / grading.DEV_RUNS_PATH, rows)
    dev = analyze_dev(
        questions,
        rows,
        docs_meta={},
        canaries={},
        model_configs=configs,
        concurrency=4,
        init_s=0.25,
        code_commit="fixture",
        input_manifest_sha256=sha256_file(
            tmp_path / "data/manifest/m6_eval_inputs.json"
        ),
    )
    dump(tmp_path / "data/manifest/m6_dev.json", dev)
    dump(
        tmp_path / "data/manifest/m6_config.json",
        {"m6_dev_sha256": sha256_file(tmp_path / "data/manifest/m6_dev.json")},
    )
    result = {}
    for name, run_set in [("gold", "test_gold"), ("silver", "test_silver")]:
        qs = [q for q in tests if q.set == name]
        rs = _dev_rows(qs, configs[:1])
        for i, row in enumerate(rs):
            row["cache_key"] = str(i)
            row["run_seq"] = 1
        rows_file(tmp_path / grading.RUN_PATHS[run_set], rs)
        summary = analyze_test(qs, rs, concurrency=4, init_s=0.5)
        if name == "gold":
            result.update(summary)
        else:
            result["silver_test"] = summary
    result.update(
        seq=1,
        run_id="m6-test-01",
        runs_sha256="historical",
        silver_runs_sha256="historical",
        set_counts={},
    )
    path = tmp_path / "data/manifest/m6_test_results/run-01.json"
    dump(path, result)
    rows_file(
        tmp_path / "data/manifest/m6_test_runs.jsonl",
        [
            {
                "seq": 1,
                "status": "completed",
                "result_path": str(path.relative_to(tmp_path)),
                "result_sha256": sha256_file(path),
            }
        ],
    )
    return tmp_path


def refreeze(root):
    dump(
        root / "data/manifest/m6_config.json",
        {"m6_dev_sha256": sha256_file(root / "data/manifest/m6_dev.json")},
    )


def test_m7_grading_verify_and_load(inputs):
    check = grading.verify_m6_inputs(inputs)
    assert check.dev_runs_rows == 300
    assert check.test_runs_rows == {"test_gold": 60, "test_silver": 166}
    assert check.dev_runs_sha256 == sha256_file(inputs / grading.DEV_RUNS_PATH)
    assert "dev.code_commit" in check.excluded_fields
    assert any("init_s" in field for field in check.excluded_fields)
    assert check.limitations == (grading.INPUT_LIMITATION,)
    assert (
        check.dev_runs_sha256
        != json.loads((inputs / "data/manifest/m6_config.json").read_text())[
            "m6_dev_sha256"
        ]
    )
    runs = grading.load_m6_answers(inputs)
    assert len(runs) == 526 and len({r.answer_id for r in runs}) == 526
    assert runs[0].answer_sha256 == hashlib.sha256(runs[0].answer.encode()).hexdigest()
    assert (
        sum(sum(counts.values()) for counts in grading.class_counts(runs).values())
        == 526
    )


@pytest.mark.parametrize(
    "change,field",
    [
        ("manifest", "m6_dev_sha256"),
        ("input", "m6_eval_inputs_sha256"),
        ("answer", "answer_match"),
        ("citation", "citation_precision"),
        ("finalizer", "finalizer"),
        ("epsilon", "citation_precision"),
    ],
)
def test_m7_grading_rejects_changes(inputs, change, field):
    path = inputs / grading.DEV_RUNS_PATH
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    if change in {"answer", "citation", "finalizer"}:
        if change == "answer":
            qs = {q.id: q for q in load_m6_questions(inputs)}
            row = next(r for r in rows if qs[r["question_id"]].answer_values)
            row["answer"] = " ".join(qs[row["question_id"]].answer_values)
        elif change == "citation":
            next(r for r in rows if r["citations"])["citations"] = []
        else:
            rows[0]["finalizer_hash"] = "changed"
        rows_file(path, rows)
    elif change == "input":
        dump(inputs / "data/manifest/m6_eval_inputs.json", {"changed": True})
    else:
        path = inputs / "data/manifest/m6_dev.json"
        dev = json.loads(path.read_text())
        dev["citation_precision"]["gpt-6-luna"]["simple"]["mean"] += 1e-15
        dump(path, dev)
        if change == "epsilon":
            refreeze(inputs)
    with pytest.raises(grading.InputHashError, match=field):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_init_values_must_all_agree(inputs):
    path = inputs / "data/manifest/m6_dev.json"
    dev = json.loads(path.read_text())
    dev["operations"]["gpt-6-luna"]["simple"]["init_s"] += 1e-15
    dump(path, dev)
    refreeze(inputs)
    with pytest.raises(grading.InputHashError, match="init_s"):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_test_hash_and_metrics(inputs):
    path = inputs / grading.RUN_PATHS["test_silver"]
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows[0]["citations"] = []
    rows_file(path, rows)
    with pytest.raises(grading.InputHashError, match="test_silver"):
        grading.verify_m6_inputs(inputs)
    records = inputs / "data/manifest/m6_test_runs.jsonl"
    rows_file(records, [{"seq": 1, "status": "completed", "result_sha256": "bad"}])
    with pytest.raises(grading.InputHashError, match="result_sha256"):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_retries_and_duplicate_ids(inputs):
    path = inputs / grading.DEV_RUNS_PATH
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows_file(path, [dict(rows[0], status="infra_error"), *rows, rows[0]])
    assert len(grading.load_m6_answers(inputs)) == 526
    rows_file(path, [*rows, dict(rows[0], cache_key="other")])
    with pytest.raises(grading.InputHashError, match="answer_id"):
        grading.load_m6_answers(inputs)


def run_for(question, **kwargs):
    return grading.AnsweredRun.from_record(
        "dev",
        question,
        {
            "question_id": question.id,
            "system": "simple",
            "model": "gpt-6-luna",
            "answer": "원문",
            "citations": [],
            "refused": False,
            "refuse_reason": None,
            "status": "completed",
            "tool_refs": [],
            **kwargs,
        },
        root=Path("."),
    )


@pytest.mark.parametrize(
    "expected,status,refused,answer,kind",
    [
        ("answer", "completed", False, "답", "gemini_acc"),
        ("answer", "completed", True, "답", "det_zero"),
        ("answer", "completed", False, "", "det_zero"),
        ("answer", "completed", False, "  \n", "det_zero"),
        ("no_answer", "infra_error", False, "", "det_zero"),
        ("answer", "billing_uncertain", False, "답", "det_zero"),
        ("no_answer", "completed", False, "부분 답", "gemini_faith_only"),
        (
            "indistinguishable_from_no_answer",
            "completed",
            False,
            "부분 답",
            "gemini_faith_only",
        ),
        ("no_answer", "completed", True, "거절", "not_scored"),
        ("indistinguishable_from_no_answer", "completed", True, "", "not_scored"),
    ],
)
def test_m7_grading_classification(expected, status, refused, answer, kind):
    q = replace(load_m6_questions()[0], expected_response=expected)
    r = run_for(q, status=status, refused=refused, answer=answer)
    assert grading.classify_run(r, q) == kind
    assert grading.classify_all([r]) == {r.answer_id: kind}
    with pytest.raises(ValueError, match="answer_id"):
        grading.classify_all([r, r])


def test_m7_grading_blind_verbatim_and_reference_hash(tmp_path):
    text = "?&oc=literal  가  가\r\n끝\n"
    q = replace(
        load_m6_questions()[0],
        reference_answer=text,
        source="synthetic",
        reference_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    r = run_for(q, answer=text, tool_refs=[{"tool": "run_sql", "steps": 8}])
    item = grading.build_grade_item(r, q, lambda *_: None)
    assert item.mode == "acc"
    assert (
        item.request_text.split("## 참조 답변\n")[1]
        .split("\n\n## 시스템 답변")[0]
        .encode()
        == text.encode()
    )
    assert (
        item.request_text.split("## 시스템 답변\n")[1]
        .split("\n\n## 인용 근거")[0]
        .encode()
        == text.encode()
    )
    for secret in (
        "simple",
        "agentic",
        "gpt-6-luna",
        "run_sql",
        "lookup_id",
        "tool_refs",
        "steps",
        "score",
    ):
        assert secret not in item.request_text
    with pytest.raises(grading.InputHashError, match="reference_sha256"):
        grading.build_grade_item(r, replace(q, reference_sha256="bad"), lambda *_: None)
    real = replace(q, source="real", reference_answer=None)
    dump(tmp_path / "data/interim/golden/real_drafts.jsonl", {})
    rows_file(
        tmp_path / "data/interim/golden/real_drafts.jsonl",
        [{"id": q.id, "reference_answer": text}],
    )
    real_run = replace(r, root=tmp_path)
    assert (
        text in grading.build_grade_item(real_run, real, lambda *_: None).request_text
    )
    with pytest.raises(grading.InputHashError):
        grading.build_grade_item(
            real_run, replace(real, reference_sha256="bad"), lambda *_: None
        )


def test_m7_grading_evidence_round_robin_missing():
    q = replace(
        load_m6_questions()[0],
        source="synthetic",
        reference_answer="참조",
        reference_sha256=None,
    )
    citations = [
        {"kind": "doc", "doc_id": name, "section_key": None}
        for name in ("a", "b", "c", "missing")
    ]
    r = run_for(q, citations=citations)
    texts = {"a": "A" * 10000, "b": "B" * 10000, "c": "C" * 10}
    item = grading.build_grade_item(r, q, lambda doc, section: texts.get(doc))
    assert item.evidence_chars == 6000 and item.evidence_missing == 1
    assert item.request_text.count("A") == 2995 and item.request_text.count("B") == 2995
    assert item.request_text.count("C") == 10
    assert (
        item.request_text.index("A")
        < item.request_text.index("B")
        < item.request_text.index("C")
    )
    fq = replace(q, expected_response="no_answer")
    assert grading.build_grade_item(r, fq, lambda *_: None).mode == "faith"
    with pytest.raises(ValueError):
        grading.build_grade_item(replace(r, refused=True), q, lambda *_: None)


def test_m7_grading_shuffle_pilot():
    q = replace(load_m6_questions()[0], source="synthetic")
    runs = [run_for(replace(q, id=f"q-{i:03}")) for i in range(25)]
    runs += [
        replace(runs[0], answer_id="test", run_set="test_gold"),
        replace(runs[0], answer_id="sol", model="gpt-6.1-sol"),
    ]
    classes = grading.classify_all(runs)
    pool = sorted(r.answer_id for r in runs[:25])
    expected = random.Random(20261001).sample(pool, 10)
    assert grading.select_pilot(runs, classes) == expected
    assert grading.select_pilot(list(reversed(runs)), classes) == expected
    assert grading.shuffle_order(pool) == grading.shuffle_order(list(reversed(pool)))


def test_m7_grading_rubric_schema():
    sections = grading.parse_rubric_sections(grading.RUBRIC_PATH)
    assert list(sections) == ["공통 지침", "정답성 앵커", "충실성 앵커", "출력 형식"]
    assert "초안" in grading.RUBRIC_PATH.read_text()
    assert (
        grading.rubric_sha256()
        == hashlib.sha256(grading.RUBRIC_PATH.read_bytes()).hexdigest()
    )
    assert grading.schema_sha256() == grading.schema_sha256()
    for score in range(4):
        assert (
            grading.AccGrade(
                correctness=score, faithfulness=score, rationale="이유"
            ).correctness
            == score
        )
        grading.FaithGrade(faithfulness=score, rationale="이유")
    for value in (-1, 4, 0.5, True, "1"):
        with pytest.raises(ValidationError):
            grading.AccGrade(correctness=value, faithfulness=3, rationale="")
        with pytest.raises(ValidationError):
            grading.FaithGrade(faithfulness=value, rationale="")
    with pytest.raises(ValidationError):
        grading.FaithGrade(faithfulness=3, rationale="가" * 201)


def test_m7_grading_evidence_loader_reuses_corpus(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from kodocs.chunking.corpus import Section

    text = "머리\n제1조 원문  가\r\n끝\n꼬리"
    doc = SimpleNamespace(
        doc_id="law-1",
        source="real",
        text=text,
        sections=(Section("article", "제1조", (), 3, len(text) - 2),),
    )
    syn = SimpleNamespace(doc_id="syn-1", source="synthetic", text="본문  원문\r\n")
    calls = []

    def corpus(condition, *, root):
        calls.append((condition, root))
        return [doc, syn], None

    monkeypatch.setattr(grading, "load_corpus", corpus)
    loader = grading.EvidenceLoader(tmp_path)
    assert calls == [("composite", tmp_path)]
    assert loader("law-1", "제1조") == text[3 : len(text) - 2]
    assert loader("law-1", None) is None
    assert loader("law-1", "제2조") is None
    assert loader("missing", None) is None
    assert loader("syn-1", None) == syn.text


def test_m7_grading_excluded_metadata_does_not_change_aggregates(inputs):
    path = inputs / "data/manifest/m6_dev.json"
    dev = json.loads(path.read_text())
    dev["code_commit"] = "recorded-old-commit"
    dev["unused_metadata"] = 42
    dump(path, dev)
    refreeze(inputs)
    check = grading.verify_m6_inputs(inputs)
    assert "dev.unused_metadata" in check.excluded_fields


def test_m7_grading_test_exact_numeric_comparison(inputs):
    path = inputs / "data/manifest/m6_test_results/run-01.json"
    result = json.loads(path.read_text())
    result["silver_test"]["deterministic_metrics"]["simple"]["citation_precision"][
        "mean"
    ] += 1e-15
    dump(path, result)
    rows_file(
        inputs / "data/manifest/m6_test_runs.jsonl",
        [{"seq": 1, "status": "completed", "result_sha256": sha256_file(path)}],
    )
    with pytest.raises(
        grading.InputHashError,
        match="test_silver.deterministic_metrics.simple.citation_precision.mean",
    ):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_latest_completed_test_record(inputs):
    path = inputs / "data/manifest/m6_test_runs.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    rows_file(
        path, [{"seq": 1, "status": "failed"}, *records, {"seq": 2, "status": "failed"}]
    )
    assert grading.verify_m6_inputs(inputs).test_runs_rows["test_gold"] == 60


def test_m7_grading_cli(inputs, capsys):
    assert grading.main(["--verify-inputs", "--root", str(inputs)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK\n")
    assert "excluded_fields" in out and "dev_runs_sha256" in out
    assert "answer" not in json.loads(out.split("\n", 1)[1])
    dump(inputs / "data/manifest/m6_eval_inputs.json", {"change": 1})
    assert grading.main(["--verify-inputs", "--root", str(inputs)]) == 2
    assert "m6_eval_inputs_sha256" in capsys.readouterr().err


def test_m7_grading_rubric_sections_are_verbatim_and_hashes_change(tmp_path):
    rubric = grading.RUBRIC_PATH.read_bytes()
    path = tmp_path / "rubric.md"
    path.write_bytes(rubric)
    sections = grading.parse_rubric_sections(path)
    assert (
        sections["정답성 앵커"]
        == rubric.decode().split("## 정답성 앵커\n")[1].split("## 충실성 앵커\n")[0]
    )
    assert grading.rubric_sha256(path) == grading.rubric_sha256()
    path.write_bytes(rubric + b"\n")
    assert grading.rubric_sha256(path) != grading.rubric_sha256()
    expected = json.dumps(
        {
            "AccGrade": grading.AccGrade.model_json_schema(),
            "FaithGrade": grading.FaithGrade.model_json_schema(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert grading.schema_sha256() == hashlib.sha256(expected.encode()).hexdigest()
    path.write_text("## 공통 지침\n내용\n")
    with pytest.raises(ValueError, match="missing"):
        grading.parse_rubric_sections(path)


def test_m7_grading_terminal_precedence(inputs):
    path = inputs / grading.DEV_RUNS_PATH
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows_file(
        path, [*rows, dict(rows[0], status="infra_error", answer="later failure")]
    )
    runs = grading.load_m6_answers(inputs)
    assert runs[0].answer == rows[0]["answer"]
    assert grading.verify_m6_inputs(inputs).dev_runs_rows == 301
