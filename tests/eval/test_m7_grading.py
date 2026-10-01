from __future__ import annotations

import hashlib
import json
import random
import subprocess
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


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True
    ).stdout


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
    dump(
        tmp_path / "data/manifest/m6_eval_inputs.json",
        {
            "generated_at": "dev-time",
            "dev_set": {"ids": [q.id for q in questions]},
            "optional": None,
        },
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "data/manifest/m6_eval_inputs.json")
    git(
        tmp_path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture dev inputs",
    )
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


def test_m7_grading_manifest_history_allows_only_added_sections(inputs, monkeypatch):
    path = inputs / "data/manifest/m6_eval_inputs.json"
    current = json.loads(path.read_text())
    current.update(generated_at="test-time", silver_test={"ids": ["s-1"]})
    dump(path, current)
    git(inputs, "add", str(path.relative_to(inputs)))
    git(
        inputs,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture test inputs",
    )
    original = grading.m6_dev.analyze_dev
    recorded_hash = json.loads((inputs / "data/manifest/m6_dev.json").read_text())[
        "m6_eval_inputs_sha256"
    ]

    def analyze(*args, **kwargs):
        assert kwargs["input_manifest_sha256"] == recorded_hash
        assert recorded_hash != sha256_file(path)
        return original(*args, **kwargs)

    monkeypatch.setattr(grading.m6_dev, "analyze_dev", analyze)
    check = grading.verify_m6_inputs(inputs)
    assert "inputs.added_after_dev.silver_test" in check.excluded_fields
    assert "inputs.generated_at" in check.excluded_fields


def test_m7_grading_manifest_history_rejects_changed_dev_section(inputs):
    path = inputs / "data/manifest/m6_eval_inputs.json"
    current = json.loads(path.read_text())
    current["dev_set"]["ids"][0] = "changed"
    dump(path, current)
    with pytest.raises(grading.InputHashError, match=r"inputs.dev_set.ids\[0\]"):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_manifest_history_rejects_missing_null_key(inputs):
    path = inputs / "data/manifest/m6_eval_inputs.json"
    current = json.loads(path.read_text())
    del current["optional"]
    dump(path, current)
    with pytest.raises(grading.InputHashError, match="inputs.optional"):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_manifest_history_requires_recorded_version(inputs):
    path = inputs / "data/manifest/m6_dev.json"
    dev = json.loads(path.read_text())
    dev["m6_eval_inputs_sha256"] = "0" * 64
    dump(path, dev)
    refreeze(inputs)
    with pytest.raises(grading.InputHashError, match="m6_eval_inputs_sha256.*git"):
        grading.verify_m6_inputs(inputs)


def test_m7_grading_manifest_history_requires_git(inputs, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(grading.InputHashError, match="git.*unavailable"):
        grading.verify_m6_inputs(inputs)


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
        ("answer", "infra_error", False, "", "det_zero"),
        ("no_answer", "infra_error", False, "", "not_scored"),
        ("no_answer", "guardrail", True, "", "not_scored"),
        (
            "indistinguishable_from_no_answer",
            "billing_uncertain",
            False,
            "",
            "not_scored",
        ),
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
    dump(inputs / "data/manifest/m6_eval_inputs.json", {"dev_set": {"ids": []}})
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


def _real_loader(texts, outside=None):
    class Loader:
        def __call__(self, doc_id, section_key):
            return texts.get((doc_id, section_key))

        def section_outside(self, doc_id):
            return outside

    return Loader()


def _real_question():
    return replace(
        load_m6_questions()[0],
        source="real",
        reference_answer="참조",
        reference_sha256=None,
    )


def test_m7_evidence_document_citation_uses_tool_refs_sections(monkeypatch):
    q = _real_question()
    monkeypatch.setattr(grading, "_reference_text", lambda *_: "참조")
    refs = [
        {"kind": "doc", "doc_id": "law-1", "section_key": "제11조"},
        {"kind": "doc", "doc_id": "law-1", "section_key": "제4조"},
        {"kind": "doc", "doc_id": "law-1", "section_key": "제11조"},
        {"kind": "doc", "doc_id": "law-2", "section_key": "제7조"},
        {"kind": "doc", "doc_id": "law-1", "section_key": None},
    ]
    run = run_for(
        q,
        citations=[{"kind": "doc", "doc_id": "law-1", "section_key": None}],
        tool_refs=refs,
    )
    loader = _real_loader(
        {("law-1", "제11조"): "A조문", ("law-1", "제4조"): "B조문"}, outside="머리말"
    )
    item = grading.build_grade_item(run, q, loader)
    text = item.request_text.split("## 인용 근거\n")[1]
    # Same-document refs in tool order, deduplicated; section-less ref -> outside body.
    assert (
        text.index("[law-1 / 제11조]\nA조문")
        < text.index("[law-1 / 제4조]\nB조문")
        < text.index("[law-1 / 섹션 밖 본문]\n머리말")
    )
    assert "law-2" not in text and text.count("A조문") == 1
    assert item.evidence_sources == ("tool_refs",)
    assert item.evidence_empty_reason is None and item.evidence_missing == 0


def test_m7_evidence_never_falls_back_to_document_head(monkeypatch):
    q = _real_question()
    monkeypatch.setattr(grading, "_reference_text", lambda *_: "참조")
    cited = [{"kind": "doc", "doc_id": "law-1", "section_key": None}]
    only_ref = [{"kind": "doc", "doc_id": "law-1", "section_key": "제4조"}]
    # Section text unloadable and no outside body: empty, never a doc-head guess.
    item = grading.build_grade_item(
        run_for(q, citations=cited, tool_refs=only_ref), q, _real_loader({})
    )
    assert item.evidence_chars == 0
    assert item.evidence_empty_reason == "tool_refs_unloadable"
    # No tool_refs for the cited document at all.
    item = grading.build_grade_item(
        run_for(q, citations=cited, tool_refs=[]), q, _real_loader({})
    )
    assert item.evidence_empty_reason == "no_tool_refs"
    item = grading.build_grade_item(
        run_for(
            q,
            citations=[{"kind": "doc", "doc_id": "law-1", "section_key": "제9조"}],
        ),
        q,
        _real_loader({}),
    )
    assert item.evidence_empty_reason == "section_not_found"
    item = grading.build_grade_item(run_for(q, citations=[]), q, _real_loader({}))
    assert item.evidence_empty_reason == "no_citations"


def _sql_loader(tmp_path, monkeypatch, *, payload, trace_steps, error=False):
    from types import SimpleNamespace

    trace = tmp_path / "logs/traces/2026-10-01.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        json.dumps({"trace_id": "t1", "steps": trace_steps}, ensure_ascii=False) + "\n"
    )
    calls = []

    def fake_run_tool(name, args, ctx, deps):
        calls.append((name, dict(args), ctx.team, ctx.grade, deps.db_path))
        return SimpleNamespace(payload=payload, is_error=error)

    monkeypatch.setattr(grading, "load_corpus", lambda *a, **k: ([], None))
    monkeypatch.setattr(grading, "load_tool_config", lambda path: ("idx", None))
    monkeypatch.setattr(grading, "run_tool", fake_run_tool)
    return grading.EvidenceLoader(tmp_path), calls


def _sql_steps(result_chars):
    return [
        {"kind": "model", "index": 1, "name": "agent"},
        {"kind": "tool", "index": 2, "name": "search_docs", "args": {"query": "q"}},
        {
            "kind": "tool",
            "index": 3,
            "name": "run_sql",
            "args": {"sql": "SELECT 1"},
            "error": None,
            "result_chars": result_chars,
        },
    ]


def test_m7_evidence_sql_replay_reads_with_asker_and_matches_trace(
    tmp_path, monkeypatch
):
    payload = {"rows": [[3]], "columns": ["n"]}
    size = len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    loader, calls = _sql_loader(
        tmp_path, monkeypatch, payload=payload, trace_steps=_sql_steps(size)
    )
    q = replace(
        load_m6_questions()[0],
        source="synthetic",
        reference_answer="참조",
        reference_sha256=None,
    )
    run = run_for(
        q,
        citations=[
            {"kind": "sql", "call_id": "c2", "doc_id": None, "section_key": None}
        ],
        trace_id="t1",
    )
    item = grading.build_grade_item(run, q, loader)
    assert item.evidence_sources == ("reconstructed_sql",)
    assert (
        "[SQL 결과]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        in item.request_text
    )
    assert "run_sql" not in item.request_text and "SELECT" not in item.request_text
    name, args, team, grade, db = calls[0]
    assert (name, args) == ("run_sql", {"sql": "SELECT 1"})
    assert (team, grade) == (q.asker.team, q.asker.grade)
    assert db == tmp_path / "data/synthetic/ledger.sqlite"


def test_m7_evidence_sql_replay_mismatch_and_missing_trace(tmp_path, monkeypatch):
    payload = {"rows": [[3]]}
    loader, _ = _sql_loader(
        tmp_path, monkeypatch, payload=payload, trace_steps=_sql_steps(1)
    )
    q = replace(
        load_m6_questions()[0],
        source="synthetic",
        reference_answer="참조",
        reference_sha256=None,
    )
    cite = {"kind": "sql", "call_id": "c2", "doc_id": None, "section_key": None}
    with pytest.raises(grading.EvidenceReconstructionError, match="sql replay size"):
        grading.build_grade_item(run_for(q, citations=[cite], trace_id="t1"), q, loader)
    # call id pointing at a non-run_sql step is a hard error.
    with pytest.raises(grading.EvidenceReconstructionError, match="not a run_sql"):
        grading.build_grade_item(
            run_for(q, citations=[{**cite, "call_id": "c1"}], trace_id="t1"), q, loader
        )
    # Trace absent: empty evidence with a reason, not a guess.
    item = grading.build_grade_item(
        run_for(q, citations=[cite], trace_id="missing"), q, loader
    )
    assert item.evidence_empty_reason == "sql_trace_missing"


def test_m7_evidence_check_reports_empty_reasons_in_manifest_form():
    q = _real_question()
    answers = [
        run_for(
            replace(q, id="q-a"),
            citations=[{"kind": "doc", "doc_id": "law-1", "section_key": None}],
        ),
        run_for(
            replace(q, id="q-b"),
            citations=[{"kind": "doc", "doc_id": "law-1", "section_key": "제1조"}],
        ),
    ]
    original = grading._reference_text
    grading._reference_text = lambda *_: "참조"
    try:
        report = grading.check_evidence(
            Path("/nonexistent-root"),
            answers=answers,
            loader=_real_loader({("law-1", "제1조"): "본문"}),
        )
    finally:
        grading._reference_text = original
    assert report["graded_items"] == 2
    assert report["empty_evidence_by_reason"] == {"no_tool_refs": 1}
    assert report["items_by_evidence_source"] == {"cited_section": 1}
    assert report["reconstruction_failures"] == []
