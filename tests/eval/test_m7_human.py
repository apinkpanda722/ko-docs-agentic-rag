from __future__ import annotations

import ast
import csv
import hashlib
import json
import random
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval import m7_human as human
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.eval.m7_grading import AnsweredRun, classify_all, parse_rubric_sections


def make_runs(sizes=(30, 30, 30, 30)):
    questions = load_m6_questions(Path("."), split="dev")
    templates = {
        source: next(
            q
            for q in questions
            if q.source == source and q.expected_response == "answer"
        )
        for source in ("real", "synthetic")
    }
    runs = []
    for (system, source), size in zip(human.STRATA, sizes, strict=True):
        for i in range(size):
            q = replace(
                templates[source],
                id=f"{system}-{source}-{i:03}",
                question="질문 <script>alert(1)</script>",
                reference_answer="참조 답변",
                reference_sha256=None,
            )
            runs.append(
                AnsweredRun(
                    f"dev|{q.id}|{system}|gpt-6-luna",
                    "dev",
                    q,
                    "답변",
                    ({"kind": "doc", "doc_id": "문서", "section_key": "제1조"},),
                    False,
                    None,
                    "completed",
                    system,
                    "gpt-6-luna",
                    (),
                    "hash",
                    Path("unused"),
                    Path("."),
                )
            )
    return runs


def test_m7_human_sample_forced_exclusions_determinism():
    runs = make_runs()
    pilot = [r.answer_id for r in runs[:10]]
    h5 = {r.question_id for r in runs[8:21]}
    classes = classify_all(runs)
    classes[runs[15].answer_id] = "det_zero"
    runs += [replace(runs[22], answer_id="other-model", model="gpt-6.1-sol")]
    classes["other-model"] = "gemini_acc"
    a = human.select_kappa_sample(runs, classes, h5, pilot)
    b = human.select_kappa_sample(list(reversed(runs)), classes, h5, pilot)
    assert a == b
    assert len(a.answer_ids) == 50
    assert list(a.answer_ids) == sorted(a.answer_ids)
    assert not set(pilot) & set(a.answer_ids)
    assert "other-model" not in a.answer_ids
    assert runs[15].answer_id not in a.answer_ids
    assert a.forced_h5 == 10
    assert {
        r.answer_id for r in runs[10:21] if r.answer_id != runs[15].answer_id
    } <= set(a.answer_ids)
    assert sum(a.strata.values()) == 50


def test_m7_human_uniform_and_short_strata():
    runs = make_runs()
    sample = human.select_kappa_sample(runs, classify_all(runs), [], [], n=28)
    assert set(sample.strata.values()) == {7}
    runs = make_runs((1, 3, 40, 50))
    sample = human.select_kappa_sample(runs, classify_all(runs), [], [])
    assert sample.strata == {
        "simple|real": 1,
        "simple|synthetic": 3,
        "agentic|real": 23,
        "agentic|synthetic": 23,
    }
    assert (
        human.select_kappa_sample(
            runs, classify_all(runs), [], [], seed=123
        ).sample_sha256
        != sample.sample_sha256
    )


def test_m7_human_remainders_largest_first():
    runs = make_runs((20, 30, 40, 50))
    sample = human.select_kappa_sample(runs, classify_all(runs), [], [], n=6)
    assert sample.strata == {
        "simple|real": 1,
        "simple|synthetic": 1,
        "agentic|real": 2,
        "agentic|synthetic": 2,
    }


def test_m7_human_sample_invalid():
    runs = make_runs((1, 1, 1, 1))
    with pytest.raises(ValueError):
        human.select_kappa_sample(runs, classify_all(runs), [], [])
    with pytest.raises(ValueError):
        human.select_kappa_sample(
            runs, classify_all(runs), [r.question_id for r in runs], [], n=2
        )
    with pytest.raises(ValueError):
        human.select_kappa_sample(runs + runs, classify_all(runs), [], [], n=2)


@pytest.fixture
def cards(tmp_path, monkeypatch):
    runs = make_runs()
    # Reference loader is separately covered by step 3; no original text fixture.
    monkeypatch.setattr(
        "kodocs.eval.m7_grading._reference_text", lambda *args: "참조 답변"
    )
    classes = classify_all(runs)
    sample = human.select_kappa_sample(runs, classes, [], [])
    human.write_cards(
        runs,
        sample,
        root=tmp_path,
        pilot_ids=[],
        evidence_loader=lambda *args: '제1조 근거 <img src="https://example.test/x">',
    )
    return tmp_path, tmp_path / human.CARDS_DIR, sample


def test_m7_human_cards_blind_offline(cards):
    root, directory, sample = cards
    page = (directory / "cards.html").read_text()
    for forbidden in (
        "simple",
        "agentic",
        "gpt-6",
        "run_sql",
        "lookup_id",
        "gemini",
        "faithfulness",
    ):
        assert forbidden not in page.lower()
    assert 'src="https://' not in page
    assert "&lt;script&gt;" in page
    assert page.count('type="radio"') == 200
    assert page.count("<textarea") == 50
    assert parse_rubric_sections()["정답성 앵커"] in page
    assert "card_id,correctness" in page
    assert "미입력" in page
    assert "localStorage" in page
    assert "try {" in page and "catch" in page
    key = json.loads((directory / "key.json").read_text())
    expected = list(sample.answer_ids)
    random.Random(20261001).shuffle(expected)
    assert list(key.values()) == expected
    manifest = json.loads((root / human.SAMPLE_PATH).read_text())
    assert manifest["answer_ids"] == list(sample.answer_ids)
    assert '"c-001"' not in json.dumps(manifest)
    assert manifest["pilot_excluded"] == 0
    card_manifest = json.loads((directory / "cards_manifest.json").read_text())
    assert (
        card_manifest["key_sha256"]
        == hashlib.sha256((directory / "key.json").read_bytes()).hexdigest()
    )
    assert (
        subprocess.run(
            ["git", "check-ignore", "-q", str(human.CARDS_DIR / "cards.html")],
            check=False,
        ).returncode
        == 0
    )


def write_csv(root, directory, rows=None, fields=("card_id", "correctness")):
    path = root / "human.csv"
    key = json.loads((directory / "key.json").read_text())
    rows = (
        rows if rows is not None else [(card, str(i % 4)) for i, card in enumerate(key)]
    )
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)
    return path


def test_m7_human_register_load_and_history(cards):
    root, directory, sample = cards
    path = write_csv(root, directory)
    manifest_path = root / human.GRADES_PATH
    with pytest.raises(human.HumanGradesNotRegistered):
        human.load_human_grades(manifest_path, path, directory / "key.json")
    record = human.register_human_grades(path, directory)
    assert record["rows"] == 50
    assert record["score_distribution"] == {"0": 13, "1": 13, "2": 12, "3": 12}
    loaded = human.load_human_grades(manifest_path, path, directory / "key.json")
    assert sorted(loaded) == list(sample.answer_ids)
    with pytest.raises(FileExistsError):
        human.register_human_grades(path, directory)
    with pytest.raises(ValueError):
        human.register_human_grades(path, directory, force_reason=" ")
    key = json.loads((directory / "key.json").read_text())
    write_csv(root, directory, [(c, "3") for c in key])
    with pytest.raises(ValueError, match="hash"):
        human.load_human_grades(manifest_path, path, directory / "key.json")
    updated = human.register_human_grades(
        path, directory, force_reason="사람의 오입력 수정"
    )
    assert updated["history"][0]["csv_sha256"] == record["csv_sha256"]
    assert updated["history"][0]["reason"] == "사람의 오입력 수정"
    assert Counter(
        human.load_human_grades(manifest_path, path, directory / "key.json").values()
    ) == {3: 50}
    human.register_human_grades(path, directory, force_reason="두 번째 수정")
    assert len(json.loads(manifest_path.read_text())["history"]) == 2


@pytest.mark.parametrize(
    "kind",
    [
        "extra_column",
        "duplicate",
        "missing",
        "unknown",
        "float",
        "out_of_range",
        "blank",
        "extra_cell",
    ],
)
def test_m7_human_csv_rejections(cards, kind):
    root, directory, _ = cards
    key = json.loads((directory / "key.json").read_text())
    rows = [[c, "2"] for c in key]
    fields = ["card_id", "correctness"]
    if kind == "extra_column":
        fields += ["memo"]
    elif kind == "duplicate":
        rows[-1] = rows[0]
    elif kind == "missing":
        rows.pop()
    elif kind == "unknown":
        rows[0][0] = "c-999"
    elif kind == "extra_cell":
        rows[0].append("extra")
    else:
        rows[0][1] = {"float": "2.0", "out_of_range": "4", "blank": ""}[kind]
    with pytest.raises(ValueError):
        human.register_human_grades(write_csv(root, directory, rows, fields), directory)
    assert not (root / human.GRADES_PATH).exists()


@pytest.mark.parametrize("kind", ["sample", "key", "cards_sample"])
def test_m7_human_hash_rejections(cards, kind):
    root, directory, _ = cards
    path = write_csv(root, directory)
    target = (
        root / human.SAMPLE_PATH
        if kind == "sample"
        else directory / ("key.json" if kind == "key" else "cards_manifest.json")
    )
    data = json.loads(target.read_text())
    if kind == "key":
        data["c-001"] = "tampered"
    else:
        data["sample_sha256"] = "tampered"
    target.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="hash"):
        human.register_human_grades(path, directory)


def test_m7_human_forbidden_module_access():
    source = Path(human.__file__).read_text()
    tree = ast.parse(source)
    forbidden = ("grades.jsonl", "m7_grade", "gemini_chat", "m7_grade_scores.json")
    for token in forbidden:
        assert token not in source
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("gemini" in (module or "") for module in imports)
