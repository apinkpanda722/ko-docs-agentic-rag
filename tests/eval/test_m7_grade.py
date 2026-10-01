import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kodocs.config import Settings
from kodocs.eval import m7_grade as grade
from kodocs.eval import m7_rawhash as raw
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.eval.m7_grading import AccGrade, AnsweredRun
from kodocs.llm.gemini_chat import (
    BillingUncertainError,
    DailyQuotaExhausted,
    FatalLLMError,
    GradeAttempt,
    GradeCostEstimate,
    GradeParseError,
    GradeResult,
    GradeUsage,
)
from kodocs.llm.grade_settings import GradeModelConfig


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class Fake:
    def __init__(self, errors=None):
        self.config = GradeModelConfig("gemini-3.8-flash")
        self.calls = []
        self.errors = errors or {}

    def estimate(self, request):
        return GradeCostEstimate(100, 4000, 0.02)

    def check_budget(self, estimate):
        pass

    def grade(self, request):
        self.calls.append(request)
        if len(self.calls) in self.errors:
            raise self.errors[len(self.calls)]
        parsed = request.schema.model_validate(
            {
                "faithfulness": 3,
                "rationale": "근거 일치",
                **({"correctness": 3} if request.schema is AccGrade else {}),
            }
        )
        return GradeResult(
            parsed, GradeUsage(100, 10, 10, 0, 110), 0.001, 1, (), None, {}
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    questions = [
        q
        for q in load_m6_questions(split="dev")
        if q.expected_response == "answer" and q.source == "synthetic"
    ]
    answers = [
        AnsweredRun.from_record(
            "dev",
            questions[i % len(questions)],
            {
                "answer": "답변",
                "citations": [],
                "refused": False,
                "refuse_reason": None,
                "status": "completed",
                "system": "simple",
                "model": "gpt-6-luna",
                "tool_refs": [],
            },
            root=tmp_path,
        )
        for i in range(20)
    ]
    answers = [
        replace(r, answer_id=f"a{i}", answer_sha256=f"hash{i}")
        for i, r in enumerate(answers)
    ]
    monkeypatch.setattr(grade, "verify_config", lambda **kwargs: {})
    dump(tmp_path / "data/manifest/m6_eval_inputs.json", {"h5_pool": {"ids": []}})
    dump(tmp_path / "data/manifest/m7_human_sample.json", {"answer_ids": []})
    return SimpleNamespace(root=tmp_path, answers=answers, settings=Settings({}, 45))


def run(env, fake, **kwargs):
    return grade.run_grade(
        root=env.root,
        answers=env.answers,
        grader=fake,
        settings=env.settings,
        evidence_loader=lambda *a: None,
        progress_every=kwargs.pop("progress_every", 1000),
        **kwargs,
    )


def test_m7_grade_resume_and_score_projection(env):
    fake = Fake()
    summary = run(env, fake)
    assert len(fake.calls) == 60
    assert summary["concentration"]["correctness"]["concentrated"]
    assert summary["agreement"]["correctness"] == 1
    assert len(summary["raw"]) == 1
    assert raw.fingerprint(env.root / grade.GRADES_PATH) == {
        k: summary["raw"][0][k] for k in ("sha256", "rows")
    }
    second = Fake()
    summary = run(env, second)
    assert not second.calls
    assert len(summary["raw"]) == 2
    scores = json.loads((env.root / grade.SCORES_PATH).read_text())
    assert scores["scores"]["a0"]["correctness"] == [3, 3, 3]
    assert "rationale" not in json.dumps(scores)
    assert "답변" not in json.dumps(scores, ensure_ascii=False)


def test_m7_grade_daily_stop_and_resume(env):
    first = Fake({2: DailyQuotaExhausted()})
    summary = run(env, first)
    assert len(first.calls) == 2
    assert summary["remaining"] == 59
    assert summary["stops"][0]["next_reset_kst"]
    assert summary["raw"][0]["rows"] == 2
    second = Fake()
    summary = run(env, second)
    assert len(second.calls) == 59
    assert len(summary["raw"]) == 2
    assert len(summary["stops"]) == 1


@pytest.mark.parametrize(
    "error", [BillingUncertainError("safe"), GradeParseError("safe")]
)
def test_m7_grade_nonretryable_call_cached(env, error):
    first = Fake({1: error})
    run(env, first)
    second = Fake()
    run(env, second)
    assert not second.calls


def test_m7_grade_format_gate_at_fifty(env):
    first = Fake({i: GradeParseError("invalid") for i in range(1, 61)})
    summary = run(env, first)
    assert len(first.calls) == 50
    assert summary["format_error_rate"] == 1
    assert summary["remaining"] == 10
    second = Fake()
    run(env, second)
    assert not second.calls


def test_m7_grade_under_fifty_and_exact_two_percent(env):
    env.answers = env.answers[:16]
    fake = Fake({1: GradeParseError("invalid")})
    assert run(env, fake)["status"] == "completed"
    assert len(fake.calls) == 48


def test_m7_grade_credit_stops_before_send(env):
    fake = Fake()
    summary = run(env, fake, credit_usd=0.01)
    assert not fake.calls
    assert summary["stops"][0]["error_type"] == "CreditExhausted"
    assert summary["raw"][0]["rows"] == 0


def test_m7_grade_fatal_stop_fingerprinted(env):
    summary = run(env, Fake({1: FatalLLMError("secret URL must not print")}))
    assert summary["status"] == "stopped"
    assert summary["raw"][0]["rows"] == 1
    assert summary["stops"][0]["error_type"] == "FatalLLMError"


def test_m7_grade_skips_deterministic_and_not_scored(env):
    env.answers = [
        replace(env.answers[0], status="failed"),
        replace(
            env.answers[1],
            question=replace(env.answers[1].question, expected_response="no_answer"),
            refused=True,
        ),
    ]
    fake = Fake()
    run(env, fake)
    assert not fake.calls
    scores = json.loads((env.root / grade.SCORES_PATH).read_text())["scores"]
    assert scores["a0"]["correctness"] == [0, 0, 0]
    assert scores["a1"]["correctness"] == []


def test_m7_grade_priority_and_consecutive_repeats(env):
    a = env.answers
    a[0] = replace(a[0], run_set="test_gold")
    a[1] = replace(a[1], run_set="test_silver")
    a[2] = replace(a[2], run_set="test_gold")
    dump(
        env.root / "data/manifest/m6_eval_inputs.json",
        {"h5_pool": {"ids": [a[0].question_id]}},
    )
    dump(env.root / "data/manifest/m7_human_sample.json", {"answer_ids": ["a1"]})
    order = grade.prioritize(a, [a[0].question_id], ["a1"])
    assert order[0].answer_id == "a0"
    assert order[1].answer_id == "a1"
    assert order[2].answer_id == "a2"
    fake = Fake()
    run(env, fake)
    assert [r.item_id for r in fake.calls[:3]] == ["a0"] * 3


@pytest.mark.parametrize("mutate", ["edit", "append", "delete"])
def test_m7_grade_rawhash_detects_changes(env, mutate):
    run(env, Fake())
    path = env.root / grade.GRADES_PATH
    lines = path.read_text().splitlines(True)
    if mutate == "edit":
        lines[0] = (
            lines[0].replace("hash0", "changed").replace("hash1", "changed") + " "
        )
    elif mutate == "append":
        lines.append(lines[0])
    else:
        lines.pop()
    path.write_text("".join(lines))
    with pytest.raises(raw.RawHashMismatch):
        raw.verify_fingerprint(env.root / grade.SUMMARY_PATH, "grades", path)
    with pytest.raises(raw.RawHashMismatch):
        run(env, Fake())


def test_m7_grade_latest_duplicate_used(env):
    run(env, Fake())
    path = env.root / grade.GRADES_PATH
    row = json.loads(path.read_text().splitlines()[0])
    row["correctness"] = 1
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    raw.record_fingerprint(env.root / grade.SUMMARY_PATH, "grades", path, "fixture")
    fake = Fake()
    summary = run(env, fake)
    assert not fake.calls
    assert summary["calls"] == 60
    scores = json.loads((env.root / grade.SCORES_PATH).read_text())["scores"]
    assert 1 in scores[row["answer_id"]]["correctness"]


@pytest.mark.parametrize("stop", [False, True])
def test_m7_grade_pilot_records_before_send_and_raw(env, stop):
    fake = Fake({1: DailyQuotaExhausted()} if stop else {})
    original = fake.grade

    def check(request):
        manifest = json.loads((env.root / grade.PILOT_PATH).read_text())
        assert len(manifest["ids"]) == 10
        return original(request)

    fake.grade = check
    summary = grade.run_pilot(
        root=env.root,
        answers=env.answers,
        grader=fake,
        settings=env.settings,
        evidence_loader=lambda *a: None,
    )
    assert summary["raw"][0]["rows"] == (1 if stop else 30)
    assert "답변" not in json.dumps(summary, ensure_ascii=False)
    fake = Fake()
    summary = grade.run_pilot(
        root=env.root,
        answers=env.answers,
        grader=fake,
        settings=env.settings,
        evidence_loader=lambda *a: None,
    )
    assert len(fake.calls) == (30 if stop else 0)
    assert len(summary["raw"]) == 2


def test_m7_grade_dry_run_no_calls(env):
    fake = Fake()
    result = run(env, fake, dry_run=True, credit_usd=2)
    assert not fake.calls
    assert result["calls"] == 60
    assert result["calls_by_mode"] == {"acc": 60}
    assert result["minimum_seconds"] == 354
    assert result["credit_usd"] == 2
    assert not (env.root / grade.GRADES_PATH).exists()
    assert (env.root / grade.DRY_PATH).exists()


def test_m7_grade_credit_uses_paid_ledger_and_tax(env):
    from kodocs.costs import CostLedger

    ledger = CostLedger(env.root / grade.DEFAULT_LEDGER_PATH)
    # A previous paid probe/pilot also consumes credit, even outside this run.
    ledger.record(
        service="google.gemini-3.8-flash.input",
        model="gemini-3.8-flash",
        quantity=100_000,
        ref="probe",
        run_id="probe",
        tier="paid",
    )
    assert grade.paid_spent(ledger) == pytest.approx(0.0825)
    fake = Fake()
    summary = run(env, fake, credit_usd=0.10)
    assert not fake.calls
    assert summary["stops"][-1]["error_type"] == "CreditExhausted"
    dry = run(env, Fake(), dry_run=True, credit_usd=0.10)
    assert not dry["within_credit"]
    assert dry["gemini_paid_spent_usd"] == pytest.approx(0.0825)


def test_m7_grade_credit_stops_during_run(env):
    from kodocs.costs import CostLedger

    ledger = CostLedger(env.root / grade.DEFAULT_LEDGER_PATH)
    fake = Fake()
    original = fake.grade

    def bill(request):
        result = original(request)
        ledger.record(
            service="google.gemini-3.8-flash.output",
            model=fake.config.model,
            quantity=4000,
            ref=request.item_id,
            run_id="grade",
            tier="paid",
        )
        return result

    fake.grade = bill
    summary = run(env, fake, credit_usd=0.04)
    assert len(fake.calls) == 2
    assert summary["stops"][-1]["error_type"] == "CreditExhausted"
    assert grade.paid_spent(ledger) == pytest.approx(0.033)


def test_m7_grade_exact_two_percent_does_not_stop():
    rows = [{"status": "parse_error"}] + [{"status": "succeeded"}] * 49
    assert not grade._format_stop(rows)
    rows[1]["status"] = "parse_error"
    assert grade._format_stop(rows)
    assert not grade._format_stop(rows[:49])


def test_m7_grade_requires_actual_freeze(env, monkeypatch):
    from kodocs.eval.m7_config import verify_config

    monkeypatch.setattr(grade, "verify_config", verify_config)
    fake = Fake()
    with pytest.raises(FileNotFoundError):
        run(env, fake)
    assert not fake.calls


def test_m7_grade_interruption_records_stop_raw(env):
    fake = Fake({1: KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        run(env, fake)
    summary = json.loads((env.root / grade.SUMMARY_PATH).read_text())
    assert summary["status"] == "stopped"
    assert summary["raw"][-1]["event"] == "stopped"


def test_m7_grade_cache_has_all_five_output_keys(env):
    first = Fake()
    run(env, first)
    env.answers[0] = replace(env.answers[0], answer_sha256="new-answer")
    second = Fake()
    run(env, second)
    assert len(second.calls) == 3
    assert set(grade.CACHE_FIELDS) == {
        "answer_sha256",
        "rubric_sha256",
        "model_settings_hash",
        "mode",
        "repeat",
    }


def test_m7_grade_pilot_refuses_selection_change_and_after_freeze(env):
    grade.run_pilot(
        root=env.root,
        answers=env.answers,
        grader=Fake(),
        settings=env.settings,
        evidence_loader=lambda *a: None,
    )
    manifest = json.loads((env.root / grade.PILOT_PATH).read_text())
    manifest["ids"][0] = "changed"
    dump(env.root / grade.PILOT_PATH, manifest)
    with pytest.raises(ValueError, match="selection"):
        grade.run_pilot(
            root=env.root, answers=env.answers, grader=Fake(), settings=env.settings
        )
    dump(env.root / grade.CONFIG_PATH, {})
    with pytest.raises(ValueError, match="freeze"):
        grade.run_pilot(
            root=env.root, answers=env.answers, grader=Fake(), settings=env.settings
        )


def test_m7_grade_faith_only_and_safe_progress(env, capsys):
    env.answers = [
        replace(
            env.answers[0],
            question=replace(env.answers[0].question, expected_response="no_answer"),
        )
    ]
    fake = Fake()
    run(env, fake, progress_every=1)
    assert len(fake.calls) == 3
    assert all(request.schema is grade.FaithGrade for request in fake.calls)
    output = capsys.readouterr().out
    assert "remaining=" in output
    assert "답변" not in output
    assert "http" not in output
    scores = json.loads((env.root / grade.SCORES_PATH).read_text())["scores"]["a0"]
    assert scores["mode"] == "faith"
    assert scores["correctness"] == []
    assert scores["faithfulness"] == [3, 3, 3]


def test_m7_grade_monthly_budget_prevents_send(env):
    env.settings = Settings({}, 0)
    fake = Fake()
    summary = run(env, fake)
    assert not fake.calls
    assert summary["stops"][-1]["error_type"] == "BudgetExceededError"


def test_m7_grade_stopped_400_records_message_and_is_retried_on_resume(env, capsys):
    message = '{"message": "Unknown name additionalProperties at response_schema"}'
    error = FatalLLMError(
        "Gemini request failed; not retried",
        attempts=[GradeAttempt(1, "stopped", 1.0, "ClientError", 400, message)],
        status_code=400,
    )
    pilot = {
        "root": env.root,
        "answers": env.answers,
        "settings": env.settings,
        "evidence_loader": lambda *a: None,
    }
    first = Fake({1: error})
    summary = grade.run_pilot(grader=first, **pilot)
    assert len(first.calls) == 1
    row = json.loads((env.root / grade.PILOT_GRADES_PATH).read_text().splitlines()[-1])
    assert row["status"] == "stopped"
    assert (row["status_code"], row["error_message"]) == (400, message)
    assert row["attempts"][0]["error_message"] == message
    out = capsys.readouterr().out
    assert "status_code=400" in out and "additionalProperties" in out
    assert summary["stops"][0]["error_message"] == message
    ids = json.loads((env.root / grade.PILOT_PATH).read_text())["ids"]

    # A stopped row is not terminal: resuming calls everything again, same pilot ids.
    second = Fake()
    summary = grade.run_pilot(grader=second, **pilot)
    assert len(second.calls) == 30
    assert json.loads((env.root / grade.PILOT_PATH).read_text())["ids"] == ids
    latest = grade.load_grades(env.root / grade.PILOT_GRADES_PATH)
    assert {r["status"] for r in latest.values()} == {"succeeded"}
    assert summary["status"] == "completed"
