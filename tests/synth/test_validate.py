"""Deterministic validation contracts for generated synthetic documents."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from types import MappingProxyType

import pytest

from kodocs.llm.settings import load_generation_config
from kodocs.synth import validate
from kodocs.synth.generate import pending_docs, template_hash
from kodocs.synth.plan import DocPlan
from kodocs.synth.seed import Access, Fact, Person, Seed, surface_forms
from kodocs.synth.validate import (
    DOCS_DIR,
    FAILURES_PATH,
    SUMMARY_PATH,
    check_canary_isolation,
    insert_injection,
    strip_markers,
    validate_document,
)


def _fact(
    fact_id: str = "f-topic",
    *,
    kind: str = "text",
    value: object = "검증 대상 사실",
) -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=kind,  # type: ignore[arg-type]
        value=value,
        subject="evt-test",
        date=None,
        sensitivity="normal",
        source="test",
    )


def _seed(*facts: Fact) -> Seed:
    person = Person("p-001", "가상인", "platform", 4, "담당자", validate.PERIOD_START)
    return Seed(
        company=MappingProxyType({"random_seed": 1}),
        people=(person,),
        services=(),
        facts=MappingProxyType({fact.fact_id: fact for fact in facts}),
        entities=MappingProxyType({}),
        regulation_refs=(),
        banned_names=("금지기관",),
        ledger_rules=MappingProxyType({}),
    )


def _plan(
    *fact_ids: str,
    doc_id: str = "syn-guide-001",
    regulation_refs: tuple[str, ...] = (),
    canary_fact_id: str | None = None,
    injection_id: str | None = None,
) -> DocPlan:
    restricted = canary_fact_id is not None
    return DocPlan(
        doc_id=doc_id,
        doc_type="guide",
        team="platform",
        access=(
            Access("restricted", ("platform",), 4)
            if restricted
            else Access("public", (), 1)
        ),
        author_id="p-001",
        date="2026-01-15",
        title_hint="시험 문서",
        fact_ids=tuple(fact_ids),
        regulation_refs=regulation_refs,
        canary_fact_id=canary_fact_id,
        injection_id=injection_id,
        style={
            "tone": "간결한",
            "structure": "개요-절차",
            "length_target_chars": 400,
            "persona": "담당자",
            "frame": "일반",
        },
        seed=7,
        related_doc_ids=(),
        fact_age_days={},
        fact_time_phrases={},
    )


def _markdown(marked: str, *, extra: str = "") -> str:
    filler = "정상 업무 내용을 충분하고 명확하게 설명합니다. " * 14
    return f"# 시험 문서\n\n{filler}{marked}{extra}\n\n## 마무리\n{filler}"


def _generation(text: str, *, stop_reason: str = "end_turn") -> dict[str, object]:
    return {
        "doc_id": "syn-guide-001",
        "attempt": 1,
        "custom_id": "syn-guide-001-a1",
        "cache_key": "cache-1",
        "batch_id": "batch-1",
        "result_type": "succeeded",
        "text": text,
        "model": "claude-sonnet-5-20260901",
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "generation": {
            "model": "claude-sonnet-5",
            "max_tokens": 6000,
            "thinking": "disabled",
            "effort": None,
            "temperature": None,
            "sampling": "unsupported_by_model",
        },
        "template_hash": "a" * 64,
        "prompt_sha256": "b" * 64,
        "doc_seed": 7,
    }


INDEX = {
    "rules": {
        "출장규칙": {"articles": {"제8조": "출장 신청"}},
        "정보보안 기본지침": {"articles": {"제37조": "접근 통제"}},
    }
}


def _validate(
    plan: DocPlan,
    generation: dict[str, object],
    seed: Seed,
    *,
    canaries: tuple[dict[str, object], ...] = (),
    injection: dict[str, object] | None = None,
) -> tuple[dict[str, object] | None, list[validate.Issue]]:
    return validate_document(
        plan,
        generation,
        seed=seed,
        index=INDEX,
        banned_names=seed.banned_names,
        canaries=canaries,
        injection=injection,
    )


def _codes(issues: list[validate.Issue]) -> set[str]:
    return {issue.code for issue in issues}


def test_strip_markers_returns_offsets_in_stripped_body() -> None:
    source = '앞 <fact id="f-topic">표시된 사실</fact> 뒤'
    body, spans, issues = strip_markers(source)

    assert issues == []
    assert spans == [("f-topic", 2, 8)]
    assert body[spans[0][1] : spans[0][2]] == "표시된 사실"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("empty", "empty_body"),
        ("missing", "fact_missing"),
        ("mismatch", "fact_value_mismatch"),
        ("identifier", "unassigned_identifier"),
        ("amount", "unassigned_amount"),
        ("date", "date_out_of_period"),
        ("banned", "banned_name"),
        ("regulation_unknown", "regulation_unknown"),
        ("regulation_unplanned", "regulation_unplanned"),
        ("canary", "canary_missing"),
        ("truncated", "truncated"),
        ("malformed", "marker_malformed"),
        ("unknown_fact", "marker_unknown_fact"),
    ],
)
def test_each_document_failure_code_has_a_fixture(case: str, expected: str) -> None:
    topic = _fact()
    plan = _plan(topic.fact_id)
    seed = _seed(topic)
    text = _markdown('<fact id="f-topic">검증 대상 사실</fact>')
    canaries: tuple[dict[str, object], ...] = ()
    stop_reason = "end_turn"

    if case == "empty":
        text = '# 짧음\n<fact id="f-topic">검증 대상 사실</fact>'
    elif case == "missing":
        text = _markdown("검증 대상 사실")
    elif case == "mismatch":
        topic = _fact(kind="id", value="PAY-1234")
        plan, seed = _plan(topic.fact_id), _seed(topic)
        text = _markdown('<fact id="f-topic">다른 값</fact>')
    elif case == "identifier":
        text += "\n배정되지 않은 PAY-9999"
    elif case == "amount":
        amount = _fact(kind="amount", value=1_250_000)
        plan, seed = _plan(amount.fact_id), _seed(amount)
        text = _markdown('<fact id="f-topic">125만 원</fact>', extra=" 추가 2만 원")
    elif case == "date":
        text += "\n2026-02-30 및 2027년 1월 1일"
    elif case == "banned":
        text += "\n금지기관"
    elif case == "regulation_unknown":
        text += "\n가상규칙 제1조"
    elif case == "regulation_unplanned":
        text += "\n출장규칙 제8조"
    elif case == "canary":
        plan = _plan(
            topic.fact_id,
            "f-canary-001",
            canary_fact_id="f-canary-001",
        )
        canaries = (
            {
                "canary_id": "cn-001",
                "fact_id": "f-canary-001",
                "value": "청록고래-1000",
                "doc_id": plan.doc_id,
            },
        )
        text = _markdown(
            '<fact id="f-topic">검증 대상 사실</fact> <fact id="f-canary-001">다른 내부 코드</fact>'
        )
    elif case == "truncated":
        stop_reason = "max_tokens"
    elif case == "malformed":
        text = _markdown('<fact id="f-topic">검증 대상 사실')
    elif case == "unknown_fact":
        text += '\n<fact id="f-other">계획 밖 사실</fact>'

    document, issues = _validate(
        plan,
        _generation(text, stop_reason=stop_reason),
        seed,
        canaries=canaries,
    )

    assert expected in _codes(issues)
    assert document is None


def test_valid_fixture_produces_document_and_preserves_every_fact_span() -> None:
    identifier = _fact(kind="id", value="PAY-1234")
    plan = _plan(identifier.fact_id, regulation_refs=("출장규칙 제8조",))
    text = _markdown(
        '<fact id="f-topic">PAY-1234</fact>',
        extra="\n출장규칙 제8조",
    )

    document, issues = _validate(plan, _generation(text), _seed(identifier))

    assert document is not None
    assert _codes(issues).issubset({"length_off_target"})
    assert document["title"] == "시험 문서"
    for span in document["fact_spans"]:
        assert document["body"][span["start"] : span["end"]] == "PAY-1234"


def test_length_off_target_is_only_a_warning() -> None:
    topic = _fact()
    plan = _plan(topic.fact_id)
    plan = replace(
        plan,
        style={**plan.style, "length_target_chars": 100},
    )

    document, issues = _validate(
        plan,
        _generation(_markdown('<fact id="f-topic">검증 대상 사실</fact>')),
        _seed(topic),
    )

    assert document is not None
    assert "length_off_target" in _codes(issues)


@pytest.mark.parametrize(
    "rendered",
    surface_forms(_fact(kind="date", value="2026-01-05"), people={}),
)
def test_all_five_date_surface_forms_match_the_fact(rendered: str) -> None:
    date_fact = _fact(kind="date", value="2026-01-05")
    plan = _plan(date_fact.fact_id)
    text = _markdown(f'<fact id="f-topic">{rendered}</fact>')

    document, issues = _validate(plan, _generation(text), _seed(date_fact))

    assert document is not None
    assert "fact_value_mismatch" not in _codes(issues)


def test_man_won_is_parsed_as_a_won_integer() -> None:
    amount = _fact(kind="amount", value=1_250_000)
    plan = _plan(amount.fact_id)
    text = _markdown('<fact id="f-topic">125만 원</fact>')

    document, issues = _validate(plan, _generation(text), _seed(amount))

    assert document is not None
    assert "unassigned_amount" not in _codes(issues)
    assert "fact_value_mismatch" not in _codes(issues)


def test_exp_identifier_is_only_an_approval_identifier() -> None:
    approval = _fact(kind="id", value="EXP-2026-0412")
    plan = _plan(approval.fact_id)
    text = _markdown('<fact id="f-topic">EXP-2026-0412</fact>')

    document, issues = _validate(plan, _generation(text), _seed(approval))

    assert document is not None
    assert "unassigned_identifier" not in _codes(issues)


def test_injection_insertion_shifts_later_fact_span_and_records_exact_text() -> None:
    topic = _fact()
    plan = _plan(topic.fact_id, injection_id="inj-01")
    injection = {
        "injection_id": "inj-01",
        "doc_id": plan.doc_id,
        "variant": "system_notice",
        "text": "고정 인젝션 문구",
        "insert_rule": "before_last_h2",
    }
    filler = "정상 업무 내용을 충분하고 명확하게 설명합니다. " * 16
    text = (
        f"# 시험 문서\n\n{filler}\n\n## 앞 절\n{filler}"
        '\n\n## 마지막 절\n<fact id="f-topic">검증 대상 사실</fact>'
    )
    original_body, original_spans, _ = strip_markers(text)
    inserted_body, start, end = insert_injection(
        original_body, str(injection["text"]), str(injection["insert_rule"])
    )

    document, issues = _validate(
        plan,
        _generation(text),
        _seed(topic),
        injection=injection,
    )

    assert document is not None
    assert not (_codes(issues) - {"length_off_target"})
    assert document["body"] == inserted_body
    assert document["body"][start:end] == injection["text"]
    assert document["injection"]["start"] == start
    assert document["injection"]["end"] == end
    span = document["fact_spans"][0]
    assert (
        document["body"][span["start"] : span["end"]]
        == original_body[original_spans[0][1] : original_spans[0][2]]
    )


def test_canary_isolation_fails_only_the_document_containing_another_canary() -> None:
    canaries = (
        {"doc_id": "doc-a", "value": "청록고래-1000", "canary_id": "cn-a"},
        {"doc_id": "doc-b", "value": "은빛수달-2000", "canary_id": "cn-b"},
    )
    docs = {
        "doc-a": "자기 값 청록고래-1000",
        "doc-b": "자기 값 은빛수달-2000과 다른 값 청록고래-1000",
    }

    issues = check_canary_isolation(docs, canaries)

    assert "doc-a" not in issues
    assert _codes(issues["doc-b"]) == {"canary_leak"}


def _write_plan(root: Path, plans: list[DocPlan]) -> None:
    path = root / "data/synthetic/plan.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(asdict(plan), ensure_ascii=False) + "\n" for plan in plans),
        encoding="utf-8",
    )
    (root / "data/synthetic/canaries.json").write_text(
        '{"canaries":[],"count":0}\n', encoding="utf-8"
    )
    (root / "data/synthetic/injections.json").write_text(
        '{"injections":[],"count":0}\n', encoding="utf-8"
    )
    manifest = root / "data/manifest/regulation_refs.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(INDEX, ensure_ascii=False), encoding="utf-8")


def _write_cache(
    root: Path,
    plan: DocPlan,
    text: str,
    *,
    cache_key: str,
    broken: bool = False,
    attempt: int = 1,
) -> None:
    generation = {
        **_generation(text),
        "doc_id": plan.doc_id,
        "attempt": attempt,
        "custom_id": f"{plan.doc_id}-a{attempt}",
        "cache_key": cache_key,
        # Same prompt revision as the current templates.
        "template_hash": template_hash(plan.doc_type),
    }
    cache_path = root / "data/synthetic/generations" / f"{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        "{broken" if broken else json.dumps(generation, ensure_ascii=False),
        encoding="utf-8",
    )
    log_path = root / "data/synthetic/generation_log.jsonl"
    with log_path.open("a", encoding="utf-8") as destination:
        destination.write(
            json.dumps(
                {
                    "doc_id": plan.doc_id,
                    "attempt": attempt,
                    "custom_id": f"{plan.doc_id}-a{attempt}",
                    "cache_key": cache_key,
                    "batch_id": "batch-1",
                    "status": "stored",
                    "result_type": "succeeded",
                    "ts": "2026-09-27T00:00:00+00:00",
                }
            )
            + "\n"
        )


def _prepare_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    plans: list[DocPlan],
    seed: Seed,
) -> None:
    _write_plan(root, plans)
    monkeypatch.setattr(validate, "load_seed", lambda _root: seed)
    monkeypatch.setattr(validate, "load_banned_names", lambda _root: seed.banned_names)
    monkeypatch.setattr(validate, "load_index", lambda _root: INDEX)
    monkeypatch.setattr(validate.db, "build_rows", lambda _seed: {})


def test_same_run_twice_keeps_docs_summary_and_failure_log_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = _fact()
    seed = _seed(topic)
    good = _plan(topic.fact_id, doc_id="syn-guide-001")
    bad = replace(good, doc_id="syn-guide-002")
    _prepare_run(tmp_path, monkeypatch, [good, bad], seed)
    _write_cache(
        tmp_path,
        good,
        _markdown('<fact id="f-topic">검증 대상 사실</fact>'),
        cache_key="good",
    )
    _write_cache(
        tmp_path,
        bad,
        _markdown("표시가 없는 검증 대상 사실"),
        cache_key="bad",
    )

    assert validate.run(tmp_path, print_fn=lambda *_args, **_kwargs: None) == 0
    first = {
        "doc": (tmp_path / DOCS_DIR / f"{good.doc_id}.json").read_bytes(),
        "summary": (tmp_path / SUMMARY_PATH).read_bytes(),
        "failures": (tmp_path / FAILURES_PATH).read_bytes(),
    }
    assert validate.run(tmp_path, print_fn=lambda *_args, **_kwargs: None) == 0

    assert (tmp_path / DOCS_DIR / f"{good.doc_id}.json").read_bytes() == first["doc"]
    assert (tmp_path / SUMMARY_PATH).read_bytes() == first["summary"]
    assert (tmp_path / FAILURES_PATH).read_bytes() == first["failures"]
    assert len(first["failures"].splitlines()) == 1


def test_broken_cache_does_not_stop_two_valid_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = _fact()
    seed = _seed(topic)
    plans = [
        _plan(topic.fact_id, doc_id=f"syn-guide-{index:03d}") for index in range(1, 4)
    ]
    _prepare_run(tmp_path, monkeypatch, plans, seed)
    valid_text = _markdown('<fact id="f-topic">검증 대상 사실</fact>')
    _write_cache(tmp_path, plans[0], valid_text, cache_key="broken", broken=True)
    _write_cache(tmp_path, plans[1], valid_text, cache_key="good-2")
    _write_cache(tmp_path, plans[2], valid_text, cache_key="good-3")

    code = validate.run(tmp_path, print_fn=lambda *_args, **_kwargs: None)
    summary = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))
    failures = [
        json.loads(line)
        for line in (tmp_path / FAILURES_PATH).read_text(encoding="utf-8").splitlines()
    ]

    assert code == 0
    assert summary["passed"] == 2
    assert summary["failed"] == 1
    assert summary["by_code"]["internal_error"] == 1
    assert _codes([validate.Issue(**issue) for issue in failures[0]["issues"]]) == {
        "internal_error"
    }
    assert pending_docs(tmp_path, plans, config=load_generation_config()) == [
        (plans[0], 2)
    ]


def test_zero_passing_documents_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = _fact()
    seed = _seed(topic)
    plan = _plan(topic.fact_id)
    _prepare_run(tmp_path, monkeypatch, [plan], seed)
    _write_cache(
        tmp_path,
        plan,
        _markdown("표시 없는 검증 대상 사실"),
        cache_key="invalid",
    )

    assert validate.run(tmp_path, print_fn=lambda *_args, **_kwargs: None) == 1


_OLD_PROMPT_ARTIFACT_RE = re.compile(r"(?<![A-Za-z0-9-])p-\d{3}(?!\d)|문서 날짜 기준")
LEAK = "본 문서에서는 배정된 사실 범위 내에서 원인을 단정하지 않는다."


def _quiet(*_args: object, **_kwargs: object) -> None:
    return None


def test_passed_doc_failing_the_current_validator_is_removed_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) A doc that passed an earlier validator but fails a newer rule
    # (pilot 4: "배정된 사실") leaves docs/ and becomes a retry target.
    topic = _fact()
    seed = _seed(topic)
    stable = _plan(topic.fact_id, doc_id="syn-guide-001")
    leaking = replace(stable, doc_id="syn-guide-002")
    _prepare_run(tmp_path, monkeypatch, [stable, leaking], seed)
    marked = '<fact id="f-topic">검증 대상 사실</fact>'
    _write_cache(tmp_path, stable, _markdown(marked), cache_key="stable")
    _write_cache(
        tmp_path, leaking, _markdown(marked, extra=f"\n{LEAK}"), cache_key="leaking"
    )
    with monkeypatch.context() as old_rules:
        old_rules.setattr(validate, "_PROMPT_ARTIFACT_RE", _OLD_PROMPT_ARTIFACT_RE)
        assert validate.run(tmp_path, print_fn=_quiet) == 0
    removed = tmp_path / DOCS_DIR / f"{leaking.doc_id}.json"
    assert removed.exists()
    messages: list[str] = []

    code = validate.run(
        tmp_path, print_fn=lambda message, **_kwargs: messages.append(message)
    )
    summary = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))

    assert code == 0
    assert not removed.exists()
    assert (tmp_path / DOCS_DIR / f"{stable.doc_id}.json").exists()
    assert summary["invalidated"] == 1
    assert summary["regressed"] == 0
    assert (summary["passed"], summary["failed"]) == (1, 1)
    assert any("무효화 1건" in message for message in messages)
    assert pending_docs(
        tmp_path, [stable, leaking], config=load_generation_config()
    ) == [(leaking, 2)]


def test_current_pass_is_kept_when_a_newer_attempt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (b) Regression preservation applies only when the kept doc still passes
    # the current validator.
    topic = _fact()
    seed = _seed(topic)
    stable = _plan(topic.fact_id, doc_id="syn-guide-001")
    regressing = replace(stable, doc_id="syn-guide-002")
    _prepare_run(tmp_path, monkeypatch, [stable, regressing], seed)
    valid_text = _markdown('<fact id="f-topic">검증 대상 사실</fact>')
    _write_cache(tmp_path, stable, valid_text, cache_key="stable")
    _write_cache(tmp_path, regressing, valid_text, cache_key="regressing-a1")
    assert validate.run(tmp_path, print_fn=_quiet) == 0
    kept = tmp_path / DOCS_DIR / f"{regressing.doc_id}.json"
    before = kept.read_bytes()
    _write_cache(
        tmp_path,
        regressing,
        _markdown("표시가 없는 검증 대상 사실"),
        cache_key="regressing-a2",
        attempt=2,
    )
    messages: list[str] = []

    code = validate.run(
        tmp_path, print_fn=lambda message, **_kwargs: messages.append(message)
    )
    summary = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))

    assert code == 1
    assert kept.read_bytes() == before
    assert summary["regressed"] == 1
    assert summary["invalidated"] == 0
    assert (summary["passed"], summary["failed"]) == (1, 1)
    assert any("회귀 1건" in message for message in messages)


PILOT_BATCHES = {
    "msgbatch_01RtBBxu4rZaYfgpGcGb92MP",
    "msgbatch_01WPEWG8ZgfC24tMViS2LLwj",
    "msgbatch_01RLrpyr7gp6zqma9K6oho1L",
    "msgbatch_01Qm4eX1ehsyw18ASU48iEvy",
}


def test_real_pilot_revalidation_invalidates_only_incident_001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (c) The committed pilot caches: all 10 passed validator v3; v4 removes
    # syn-incident-001 ("배정된 사실" leak) and makes it the only retry target.
    import shutil

    from kodocs.synth.generate import _load_plan

    repo = Path(__file__).parents[2]
    shutil.copytree(repo / "data/seed", tmp_path / "data/seed")
    for name in ("plan.jsonl", "canaries.json", "injections.json"):
        target = tmp_path / "data/synthetic" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(repo / "data/synthetic" / name, target)
    # Only the pilot batches; later full-run attempts are also committed.
    log_lines = [
        line
        for line in (repo / "data/synthetic/generation_log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line).get("batch_id") in PILOT_BATCHES
    ]
    (tmp_path / "data/synthetic/generation_log.jsonl").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    (tmp_path / "data/synthetic/generations").mkdir()
    for cache in (repo / "data/synthetic/generations").glob("*.json"):
        if (
            json.loads(cache.read_text(encoding="utf-8")).get("batch_id")
            in PILOT_BATCHES
        ):
            shutil.copy(cache, tmp_path / "data/synthetic/generations" / cache.name)
    (tmp_path / "data/manifest").mkdir(parents=True)
    shutil.copy(
        repo / "data/manifest/regulation_refs.json",
        tmp_path / "data/manifest/regulation_refs.json",
    )
    with monkeypatch.context() as old_rules:
        old_rules.setattr(validate, "_PROMPT_ARTIFACT_RE", _OLD_PROMPT_ARTIFACT_RE)
        validate.run(tmp_path, print_fn=_quiet)
    before = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))
    assert before["passed"] == 10

    validate.run(tmp_path, print_fn=_quiet)
    summary = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))

    assert summary["invalidated"] == 1
    assert summary["regressed"] == 0
    assert summary["passed"] == 9
    assert summary["by_code"] == {"prompt_artifact": 1}
    assert not (tmp_path / DOCS_DIR / "syn-incident-001.json").exists()
    assert len(list((tmp_path / DOCS_DIR).glob("*.json"))) == 9
    pilot = [
        plan
        for plan in _load_plan(tmp_path)
        if (tmp_path / DOCS_DIR / f"{plan.doc_id}.json").exists()
        or plan.doc_id == "syn-incident-001"
    ]
    assert [
        (plan.doc_id, attempt)
        for plan, attempt in pending_docs(
            tmp_path, pilot, config=load_generation_config()
        )
    ] == [("syn-incident-001", 5)]


@pytest.mark.parametrize(
    "marked",
    [
        # Pilot 1 shapes: closing written as ⟧/F⟧ or ⟧, old-format markers.
        '<fact id="f-topic">검증 대상 사실⟧/F⟧',
        "⟦F:f-topic⟧검증 대상 사실⟦/F⟧",
        '<fact id="f-topic">검증 대상 사실</fact></fact>',
        "<fact id='f-topic'>검증 대상 사실</fact>",
        '<fact id="f-topic">검증 대상 <fact id="f-topic">사실</fact></fact>',
        '<fact  id="f-topic">검증 대상 사실</fact>',
    ],
)
def test_malformed_or_leftover_markers_fail_without_lenient_repair(
    marked: str,
) -> None:
    topic = _fact()
    document, issues = _validate(
        _plan(topic.fact_id), _generation(_markdown(marked)), _seed(topic)
    )

    assert "marker_malformed" in _codes(issues)
    assert document is None


@pytest.mark.parametrize(
    "leak",
    [
        "작성자: 가상인 수석 (p-010)",
        "사실 A(문서 날짜 기준 3일 전)",
        "담당 p-001 확인",
        # Pilot 4 (incident-001): the prompt's own wording leaked into the body.
        "본 문서에서는 배정된 사실 범위 내에서 원인을 임의로 단정하지 않는다.",
        # Full run 1: narrative placeholder and incident meta remarks.
        "결정 배경은 서술 요지를 자연스럽게 정리했다.",
        "규칙 2의 서술 사실에 따라 정리했다.",
        "배정된 자료에는 영향 수치가 명시되어 있지 않다.",
        "배정된 수치가 없어 기재하지 않는다.",
    ],
)
def test_prompt_artifacts_in_body_fail(leak: str) -> None:
    topic = _fact()
    text = _markdown('<fact id="f-topic">검증 대상 사실</fact>', extra=f"\n{leak}")
    document, issues = _validate(_plan(topic.fact_id), _generation(text), _seed(topic))

    assert "prompt_artifact" in _codes(issues)
    assert document is None


@pytest.mark.parametrize(
    "ordinary",
    [
        "배정된 월말 집계 단계를 마쳤다.",
        "담당자 배정 예정이다.",
        "배정된 인력으로 자료를 정리했다.",
    ],
)
def test_ordinary_use_of_assignment_words_is_not_a_prompt_artifact(
    ordinary: str,
) -> None:
    topic = _fact()
    text = _markdown('<fact id="f-topic">검증 대상 사실</fact>', extra=f"\n{ordinary}")
    _, issues = _validate(_plan(topic.fact_id), _generation(text), _seed(topic))

    assert "prompt_artifact" not in _codes(issues)


def test_ids_that_merely_look_like_person_ids_are_not_prompt_artifacts() -> None:
    topic = _fact()
    text = _markdown(
        '<fact id="f-topic">검증 대상 사실</fact>', extra="\napp-100 cp-0012"
    )
    _, issues = _validate(_plan(topic.fact_id), _generation(text), _seed(topic))

    assert "prompt_artifact" not in _codes(issues)


def test_identifier_given_by_title_hint_counts_as_assigned() -> None:
    topic = _fact()
    plan = replace(_plan(topic.fact_id), title_hint="[장애보고] INC-2025-001 청구 지연")
    text = _markdown(
        '<fact id="f-topic">검증 대상 사실</fact>', extra="\nINC-2025-001 영향 범위"
    )
    _, issues = _validate(plan, _generation(text), _seed(topic))

    assert "unassigned_identifier" not in _codes(issues)


def test_incident_number_of_assigned_incident_fact_counts_as_assigned() -> None:
    cause = replace(_fact("f-inc-2025-001-cause"), subject="INC-2025-001")
    text = _markdown(
        '<fact id="f-inc-2025-001-cause">검증 대상 사실</fact>',
        extra="\nINC-2025-001 재발 방지",
    )
    _, issues = _validate(_plan(cause.fact_id), _generation(text), _seed(cause))

    assert "unassigned_identifier" not in _codes(issues)


def test_other_identifiers_of_that_incident_stay_unassigned() -> None:
    cause = replace(_fact("f-inc-2025-001-cause"), subject="INC-2025-001")
    ticket = replace(
        _fact("f-inc-2025-001-ticket", kind="id", value="SRE-1004"),
        subject="INC-2025-001",
    )
    text = _markdown(
        '<fact id="f-inc-2025-001-cause">검증 대상 사실</fact>',
        extra="\nINC-2025-001 후속 SRE-1004",
    )
    _, issues = _validate(_plan(cause.fact_id), _generation(text), _seed(cause, ticket))

    assert [
        issue.detail for issue in issues if issue.code == "unassigned_identifier"
    ] == ["배정되지 않은 ticket 식별자입니다: SRE-1004"]


def test_assigned_regulation_fact_counts_as_planned_regulation() -> None:
    regulation = _fact(kind="regulation", value="출장규칙 제8조")
    text = _markdown('<fact id="f-topic">출장규칙 제8조</fact>에 따라 정산한다.')
    _, issues = _validate(
        _plan(regulation.fact_id), _generation(text), _seed(regulation)
    )

    assert "regulation_unplanned" not in _codes(issues)


def _faithful_body(plan: DocPlan, seed: Seed, canary_values: dict[str, str]) -> str:
    """A body that uses exactly what the plan gives the model, nothing else."""
    people = {person.person_id: person for person in seed.people}
    filler = "업무 진행 상황을 정리하고 후속 조치를 공유합니다. " * 30
    marked: list[str] = []
    for fact_id in plan.fact_ids:
        fact = seed.facts.get(fact_id)
        if fact is None:
            value = canary_values[fact_id]
        elif fact.kind == "text":
            value = str(fact.value)
        else:
            value = surface_forms(fact, people=people)[0]
        marked.append(f'<fact id="{fact_id}">{value}</fact>')
    references = "".join(
        f"\n{reference}를 참조한다." for reference in plan.regulation_refs
    )
    # A model naturally names the incident an assigned incident fact belongs to.
    incidents = "".join(
        f"\n{subject} 관련 내용이다."
        for subject in sorted(
            {
                seed.facts[fact_id].subject
                for fact_id in plan.fact_ids
                if fact_id in seed.facts
                and seed.facts[fact_id].subject.startswith("INC-")
            }
        )
    )
    return (
        f"# {plan.title_hint}\n\n{filler}\n\n"
        + "\n".join(marked)
        + references
        + incidents
        + f"\n\n## 마무리\n{filler}"
    )


def test_committed_plan_alone_produces_no_validation_failures() -> None:
    repo = Path(__file__).parents[2]
    from kodocs.synth.generate import _load_plan
    from kodocs.synth.regulations import load_index
    from kodocs.synth.seed import load_seed

    seed = load_seed(repo)
    plans = _load_plan(repo)
    index = load_index(repo)
    canaries = json.loads(
        (repo / "data/synthetic/canaries.json").read_text(encoding="utf-8")
    )["canaries"]
    injections = {
        row["doc_id"]: row
        for row in json.loads(
            (repo / "data/synthetic/injections.json").read_text(encoding="utf-8")
        )["injections"]
    }
    canary_values = {row["fact_id"]: row["value"] for row in canaries}

    failures: dict[str, set[str]] = {}
    for plan in plans:
        generation = {
            **_generation(_faithful_body(plan, seed, canary_values)),
            "doc_id": plan.doc_id,
            "custom_id": f"{plan.doc_id}-a1",
        }
        document, issues = validate_document(
            plan,
            generation,
            seed=seed,
            index=index,
            banned_names=seed.banned_names,
            canaries=canaries,
            injection=injections.get(plan.doc_id),
        )
        failing = _codes(issues) - {"length_off_target"}
        if document is None or failing:
            failures[plan.doc_id] = failing

    assert len(plans) == 200
    assert failures == {}


def test_number_inside_identifier_followed_by_won_word_is_not_an_amount() -> None:
    topic = _fact()
    text = _markdown(
        '<fact id="f-topic">검증 대상 사실</fact>', extra="\nINC-2025-007 원장 집계"
    )
    plan = replace(
        _plan(topic.fact_id), title_hint="[장애보고] INC-2025-007 원장 집계 지연"
    )
    _, issues = _validate(plan, _generation(text), _seed(topic))

    assert "unassigned_amount" not in _codes(issues)
    _, still_checked = _validate(
        plan, _generation(text + "\n추가 비용 3만 원"), _seed(topic)
    )
    assert "unassigned_amount" in _codes(still_checked)


def test_exhausted_counts_only_attempts_with_the_current_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Full run 1: syn-incident-001 failed its 5th attempt, but only 2 used the
    # current templates, so it is still a retry target, not exhausted.
    topic = _fact()
    seed = _seed(topic)
    plan = _plan(topic.fact_id)
    _prepare_run(tmp_path, monkeypatch, [plan], seed)
    failing = _markdown("표시 없는 검증 대상 사실")
    for attempt in (1, 2, 3):
        _write_cache(
            tmp_path, plan, failing, cache_key=f"old-{attempt}", attempt=attempt
        )
        cache = tmp_path / f"data/synthetic/generations/old-{attempt}.json"
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["template_hash"] = "0" * 64
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    for attempt in (4, 5):
        _write_cache(
            tmp_path, plan, failing, cache_key=f"new-{attempt}", attempt=attempt
        )

    validate.run(tmp_path, print_fn=_quiet)
    summary = json.loads((tmp_path / SUMMARY_PATH).read_text(encoding="utf-8"))

    assert summary["exhausted"] == 0
    assert summary["exhausted_docs"] == []
    assert pending_docs(tmp_path, [plan], config=load_generation_config()) == [
        (plan, 6)
    ]
