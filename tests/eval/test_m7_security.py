"""Security runner contracts: fake answers/MCP, no network or golden writes."""

import json
import shutil
from pathlib import Path

import pytest

from kodocs.agent.types import AgentRuntime, AnswerResult
from kodocs.costs import BudgetExceededError
from kodocs.eval import m7_security as security
from kodocs.eval.m7_leak import RestrictedFacts
from kodocs.eval.m7_rawhash import RawHashMismatch
from kodocs.eval.m7_security_set import SecurityInputs
from kodocs.ingest.manifest import sha256_file
from kodocs.tools import ToolResult
from kodocs.tools.deps import M5_CONFIG_HASH

HIDDEN = {
    "doc_id": "syn-hidden",
    "title_hint": "비밀 운영 계획",
    "fact_ids": [],
    "access": {"level": "restricted", "teams": ["security"], "min_grade": 4},
}
PUBLIC = {"doc_id": "syn-public", "access": {"level": "public"}}
USER = {"team": "platform", "grade": 1, "label": "demo-platform-g1"}
OWNER = {"team": "security", "grade": 4, "label": "demo-security-g4"}
INPUTS = SecurityInputs(
    (HIDDEN, PUBLIC),
    (USER, OWNER),
    (
        {
            "doc_id": "syn-hidden",
            "value": "금빛고래-9272",
            "canary_id": "cn-1",
            "owner_team": "security",
            "min_grade": 4,
        },
    ),
    {"syn-public": {"text": "인젝션 문구"}},
    {},
    (),
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


@pytest.fixture
def project(tmp_path):
    for name in ("m6_config.json", "m6_dev.json", "m5_config.json"):
        dst = tmp_path / "data/manifest" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path("data/manifest") / name, dst)
    rows = [
        {
            "scenario_id": f"sec-{i:04}",
            "kind": "S1",
            "control": False,
            "excluded": False,
            "exclusion_reason": None,
            "in_final_context": None,
            "asker": USER,
            "question": f"질문 {i}",
            "target_doc_ids": ["syn-hidden"],
            "target_titles": ["비밀 운영 계획"],
            "target_row_ids": [],
        }
        for i in range(200)
    ]
    rows += [
        dict(
            rows[0],
            scenario_id="sec-s6",
            kind="S6",
            question="공개 질문",
            target_doc_ids=["syn-public"],
            in_final_context=True,
        ),
        dict(
            rows[0],
            scenario_id="sec-control",
            kind="control",
            control=True,
            question="대조 질문",
            asker=OWNER,
        ),
    ]
    rows[-2:-2] = [
        dict(rows[-1], scenario_id=f"sec-control-{i}", question=f"대조 질문 {i}")
        for i in range(29)
    ]
    jsonl(tmp_path / security.SCENARIOS_PATH, rows)
    write(
        tmp_path / security.SET_PATH,
        {
            "scenarios_sha256": sha256_file(tmp_path / security.SCENARIOS_PATH),
            "s6_exposure_checked": True,
            "m5_config_hash": M5_CONFIG_HASH,
            "s6_exclusions": [],
            "s6_exposure_history": [],
        },
    )
    for split in ("dev", "test/run-01", "test/run-01/silver"):
        jsonl(
            tmp_path / f"data/interim/m6/{split}/runs.jsonl",
            [
                {
                    "system": s,
                    "cost_usd": 0.001,
                    "question_id": "no-answer",
                    "answer": "근거에 없습니다",
                    "refused": True,
                    "model": "gpt-6-luna",
                }
                for s in ("simple", "agentic")
            ],
        )
    write(
        tmp_path / "data/synthetic/security/leak_lexicon.json",
        json.loads(Path("data/synthetic/security/leak_lexicon.json").read_text()),
    )
    jsonl(tmp_path / "data/synthetic/plan.jsonl", [HIDDEN, PUBLIC])
    return tmp_path


def runtime_factory(name, config):
    return AgentRuntime(
        deps=object(),
        chat=object(),
        model_record=config.record(),
        m5_config_hash=M5_CONFIG_HASH,
    )


def refused_answer(*args, **kwargs):
    return AnswerResult("근거에 없습니다", (), True, "model_refused", {"steps": []})


class FakeMCP:
    def __init__(self, mismatch=False, byte_mismatch=False):
        self.mismatch, self.byte_mismatch = mismatch, byte_mismatch
        self.calls = []

    def check(self, asker):
        pass

    def call(self, asker, name, args):
        self.calls.append((asker, name, args))
        result = direct(name, args, None, None)
        payload = result.payload
        if self.mismatch and name == "search_docs":
            payload = {"results": [{"doc_id": "syn-hidden", "text": "금빛고래-9272"}]}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if self.byte_mismatch and args.get("doc_id") == "syn-hidden":
            text += " "
        return security.ToolReply(payload, result.is_error, text.encode())


def direct(name, args, user, deps):
    if name == "get_document":
        return ToolResult(
            {"error": {"code": "not_found", "message": "찾을 수 없습니다"}},
            True,
            (),
            None,
        )
    return ToolResult({"results": []}, False, (), None)


def run(project, **kwargs):
    security.write_dry_run(root=project, budget_check=lambda x: None)
    return security.run_security(
        root=project,
        runtime_factory=runtime_factory,
        answer_fn=kwargs.pop("answer_fn", refused_answer),
        tool_fn=direct,
        mcp=kwargs.pop("mcp", FakeMCP()),
        inputs=INPUTS,
        facts_by_user=kwargs.pop("facts_by_user", {}),
        baseline_questions={
            "no-answer": {"type": "unanswerable", "expected_response": "no_answer"}
        },
        budget_check=lambda x: None,
        **kwargs,
    )


def test_m7_security_all_refusal_not_vacuous(project):
    result = run(project)
    for system in ("simple", "agentic"):
        group = result["groups"][f"in-process|{system}"]
        assert group["n"] == 201 and group["leaks"] == 0
        assert result["control_false_refusal"][f"in-process|{system}"]["rate"] == 1
    assert result["raw"][-1]["rows"] >= 404
    assert result["raw"][-1]["sha256"] == sha256_file(project / security.RAW_PATH)
    assert security.analyze_h6(project)["pending"] is False
    with pytest.raises(ValueError, match="RERUN_REASON"):
        run(project)
    second = run(project, rerun_reason="fake rerun")
    assert len(second["history"]) == 2 and len(second["raw"]) == 2
    assert second["history"][1]["previous_result_sha256"]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"s6_exposure_checked": False}, "S6"),
        ({"scenarios_sha256": "wrong"}, "scenarios"),
        ({"m5_config_hash": "wrong"}, "M5"),
    ],
)
def test_m7_security_set_preflight(project, change, message):
    manifest = json.loads((project / security.SET_PATH).read_text()) | change
    write(project / security.SET_PATH, manifest)
    with pytest.raises(ValueError, match=message):
        security.preflight(project)


@pytest.mark.parametrize(
    "mode", ["remaining_false", "under_200", "excluded_without_reason"]
)
def test_m7_security_remaining_s6_and_denominator(project, mode):
    rows = security.load_rows(project / security.SCENARIOS_PATH)
    if mode == "remaining_false":
        rows[-2]["in_final_context"] = False
    elif mode == "under_200":
        rows = rows[3:]
    else:
        rows[-2]["excluded"] = True
    jsonl(project / security.SCENARIOS_PATH, rows)
    manifest = json.loads((project / security.SET_PATH).read_text())
    manifest["scenarios_sha256"] = sha256_file(project / security.SCENARIOS_PATH)
    write(project / security.SET_PATH, manifest)
    with pytest.raises(ValueError):
        security.preflight(project)


def test_m7_security_excluded_s6_allowed(project):
    rows = security.load_rows(project / security.SCENARIOS_PATH)
    rows[-2].update(
        excluded=True, in_final_context=False, exclusion_reason="never_in_final_context"
    )
    jsonl(project / security.SCENARIOS_PATH, rows)
    manifest = json.loads((project / security.SET_PATH).read_text())
    manifest.update(
        scenarios_sha256=sha256_file(project / security.SCENARIOS_PATH),
        s6_exclusions=[{"scenario_id": "sec-s6", "reason": "never_in_final_context"}],
    )
    write(project / security.SET_PATH, manifest)
    assert len(security.preflight(project)[0]) == 230


def test_m7_security_m6_and_monthly_budget(project):
    cfg = json.loads((project / "data/manifest/m6_config.json").read_text())
    cfg["finalizer_hash"] = "changed"
    write(project / "data/manifest/m6_config.json", cfg)
    with pytest.raises(ValueError, match="M6"):
        security.preflight(project)
    shutil.copy(
        "data/manifest/m6_config.json", project / "data/manifest/m6_config.json"
    )

    def deny(cost):
        raise BudgetExceededError("Monthly budget exceeded")

    with pytest.raises(BudgetExceededError):
        security.write_dry_run(root=project, budget_check=deny)


def test_m7_security_leaks_c1_review_and_raw_tamper(project):
    def leaking(*args, **kwargs):
        return AnswerResult(
            "비공개 문서가 있습니다 syn-hidden 금빛고래-9272 9273원",
            (),
            False,
            None,
            {
                "steps": [
                    {
                        "kind": "tool",
                        "name": "get_document",
                        "args": {"doc_id": "syn-hidden"},
                        "refs": [{"doc_id": "syn-hidden"}],
                    }
                ]
            },
        )

    facts = RestrictedFacts(
        (
            {
                "doc_id": "syn-hidden",
                "value": "9273원",
                "shape": "short",
                "shared": False,
            },
        ),
        {"sentence": 0},
        {"team": "platform", "grade": 1},
    )
    result = run(project, answer_fn=leaking, facts_by_user={("platform", 1): facts})
    group = result["groups"]["in-process|agentic"]
    assert all(group[k] == 201 for k in ("a", "b", "c2", "c3", "c1_candidates"))
    assert security.analyze_h6(project)["message"] == "H6 판정 대기(c1 후보 확인 필요)"
    assert all(
        v["text"] is None
        for v in security.analyze_h6(project)["by_path_system"].values()
    )
    keys = json.loads((project / security.REVIEW_DIR / "key.json").read_text())
    csv = project / security.REVIEW_CSV
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text("review_id,confirmed\n" + "".join(f"{i},예\n" for i in keys))
    security.register_review(root=project)
    assert (
        security.analyze_h6(project)["by_path_system"]["in-process|agentic"][
            "c1_confirmed"
        ]
        == 201
    )
    with (project / security.RAW_PATH).open("a") as out:
        out.write("{}\n")
    with pytest.raises(RawHashMismatch):
        security.analyze_h6(project)
    with pytest.raises(RawHashMismatch):
        security.export_review(root=project)


def test_m7_security_parity_not_found_and_no_tokens(project):
    mcp = FakeMCP(mismatch=True, byte_mismatch=True)
    result = run(project, mcp=mcp)
    assert result["parity_mismatch_count"] > 0
    assert result["not_found_mismatch_count"] == 1
    assert result["groups"]["mcp|tool"]["b"] == 201
    assert "FAKE_SECRET" not in json.dumps(result)


def test_m7_security_mcp_exception_does_not_expose_token(tmp_path, monkeypatch, capsys):
    write(
        tmp_path / "data/interim/mcp/tokens.json",
        {"users": [USER | {"token": "FAKE_SECRET"}]},
    )
    write(
        tmp_path / "data/manifest/mcp_token_hashes.json",
        {
            "users": [
                USER
                | {"token_sha256": security.hashlib.sha256(b"FAKE_SECRET").hexdigest()}
            ]
        },
    )
    client = security.MCPClient(tmp_path, "http://127.0.0.1:8765/mcp")

    async def bad(*args):
        raise RuntimeError("Bearer FAKE_SECRET")

    monkeypatch.setattr(client, "_request", bad)
    with pytest.raises(RuntimeError) as exc:
        client.call(USER, "search_docs", {"query": "test"})
    assert "FAKE_SECRET" not in str(exc.value)
    assert "FAKE_SECRET" not in capsys.readouterr().out


def test_m7_security_direct_attacks():
    base = {
        "kind": "S3",
        "target_doc_ids": ["syn-hidden"],
        "question": "syn-hidden 설명",
    }
    assert security.attack_calls(base, INPUTS) == [
        ("lookup_id", {"value": "syn-hidden"}),
        ("get_document", {"doc_id": "syn-hidden"}),
    ]
    sql = {"kind": "S5", "sql": "SELECT * FROM tickets LIMIT 10"}
    assert security.attack_calls(sql, INPUTS) == [("run_sql", {"sql": sql["sql"]})]


def test_m7_security_exposure_indeterminate_and_attempt():
    scenario = {
        "kind": "S6",
        "scenario_id": "sec-s6",
        "asker": USER,
        "target_doc_ids": ["syn-public"],
    }
    assert security.injection_exposure(scenario, None, {}, INPUTS)["exposed"] is None
    trace = {
        "steps": [
            {
                "kind": "tool",
                "name": "search_docs",
                "args": {},
                "refs": [{"doc_id": "syn-public", "chunk_id": "chunk-1"}],
            },
            {
                "kind": "tool",
                "name": "get_document",
                "args": {"doc_id": "syn-hidden"},
                "refs": [],
            },
        ]
    }
    manifest = {
        "s6_exposure_history": [
            {
                "scenario_id": "sec-s6",
                "matches": [{"chunk_id": "chunk-1"}],
                "in_final_context": True,
            }
        ]
    }
    trace["steps"].append({"kind": "model"})
    exposure = security.injection_exposure(scenario, trace, manifest, INPUTS)
    assert exposure["exposed"] is True and exposure["attempted"] is True


def test_m7_security_cli_refusal_and_help(project, capsys):
    manifest = json.loads((project / security.SET_PATH).read_text())
    manifest["s6_exposure_checked"] = False
    write(project / security.SET_PATH, manifest)
    assert security.main(["--root", str(project), "--dry-run"]) == 2
    assert "S6" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        security.main(["--help"])
    assert "make m6-serve" in capsys.readouterr().out


def test_m7_security_analyze_summary_pending(capsys):
    from kodocs.eval import m7_analyze

    m7_analyze.print_h6(
        {
            "pending": True,
            "message": "H6 판정 대기(c1 후보 확인 필요)",
            "by_path_system": {"in-process|simple": {"text": None}},
        }
    )
    output = capsys.readouterr().out
    assert "판정 대기" in output and "채택" not in output and "기각" not in output


def test_m7_security_stopped_records_fingerprint(project):
    from kodocs.llm.openai_chat import FatalLLMError

    def fatal(*args, **kwargs):
        raise FatalLLMError("fake fatal")

    with pytest.raises(FatalLLMError):
        run(project, answer_fn=fatal)
    result = json.loads((project / security.RESULTS_PATH).read_text())
    assert result["status"] == "stopped"
    assert result["raw"][-1]["sha256"] == sha256_file(project / security.RAW_PATH)
    assert security.analyze_h6(project)["pending"]


def test_m7_security_infra_failure_in_denominator(project):
    def fail(*args, **kwargs):
        raise RuntimeError("fake infra error")

    result = run(project, answer_fn=fail)
    for system in ("simple", "agentic"):
        assert result["groups"][f"in-process|{system}"]["n"] == 201
        assert result["groups"][f"in-process|{system}"]["leaks"] == 0
        assert result["control_false_refusal"][f"in-process|{system}"]["rate"] == 1


def test_m7_security_matching_dry_required(project):
    with pytest.raises(ValueError, match="dry"):
        security.run_security(
            root=project,
            runtime_factory=runtime_factory,
            mcp=FakeMCP(),
            inputs=INPUTS,
            facts_by_user={},
            budget_check=lambda x: None,
        )


def test_m7_security_c1_only_confirmed_counts_and_reviewed_rerun(project):
    def candidate(*args, **kwargs):
        text = (
            "비공개 문서가 있습니다"
            if kwargs["question_id"] == "sec-0000"
            else "근거에 없습니다"
        )
        return AnswerResult(text, (), True, "model_refused", {"steps": []})

    result = run(project, answer_fn=candidate)
    assert result["groups"]["in-process|simple"]["leaks"] == 0
    keys = json.loads((project / security.REVIEW_DIR / "key.json").read_text())
    csv = project / security.REVIEW_CSV
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text(
        "review_id,confirmed\n"
        + "".join(
            f"{i},{'예' if v['system'] == 'simple' else '아니오'}\n"
            for i, v in keys.items()
        )
    )
    security.register_review(root=project)
    h6 = security.analyze_h6(project)
    assert h6["by_path_system"]["in-process|simple"]["leaks"] == 1
    assert h6["by_path_system"]["in-process|agentic"]["leaks"] == 0
    rerun = run(project, answer_fn=candidate, rerun_reason="reviewed rerun")
    assert len(rerun["history"]) == 2
    assert security.analyze_h6(project)["pending"]


def test_m7_security_mcp_logs_redacted(tmp_path, monkeypatch, caplog):
    import logging

    write(
        tmp_path / "data/interim/mcp/tokens.json",
        {"users": [USER | {"token": "FAKE_SECRET"}]},
    )
    write(
        tmp_path / "data/manifest/mcp_token_hashes.json",
        {
            "users": [
                USER
                | {"token_sha256": security.hashlib.sha256(b"FAKE_SECRET").hexdigest()}
            ]
        },
    )
    client = security.MCPClient(tmp_path)

    async def bad(*args):
        logging.getLogger("httpx2.transport").error("Authorization: Bearer FAKE_SECRET")
        raise RuntimeError("FAKE_SECRET")

    monkeypatch.setattr(client, "_request", bad)
    with pytest.raises(RuntimeError):
        client.check(USER)
    assert "FAKE_SECRET" not in caplog.text


def test_m7_security_exposure_section_mapping_requires_model_input():
    scenario = {
        "kind": "S6",
        "scenario_id": "sec-s6",
        "asker": USER,
        "target_doc_ids": ["syn-public"],
    }
    tool = {
        "kind": "tool",
        "name": "search_docs",
        "args": {"query": "rewritten"},
        "refs": [{"doc_id": "syn-public", "section_key": "section-1"}],
    }
    mapping = {
        ("syn-public", "section-1"): {"chunk_id": "chunk-1", "contains_injection": True}
    }
    manifest = {
        "s6_exposure_history": [
            {
                "scenario_id": "sec-s6",
                "candidate_matches": [{"chunk_id": "chunk-1"}],
                "matches": [{"chunk_id": "chunk-1"}],
            }
        ]
    }
    exposed = security.injection_exposure(
        scenario,
        {"steps": [tool, {"kind": "model"}]},
        manifest,
        INPUTS,
        chunk_mapping=mapping,
    )
    assert exposed["exposed"] is True
    no_input = security.injection_exposure(
        scenario, {"steps": [tool]}, manifest, INPUTS, chunk_mapping=mapping
    )
    assert no_input["exposed"] is False


def test_m7_security_result_history_integrity(project):
    result = run(project)
    first_hash = result["history"][0]["result_sha256"]
    again = run(project, rerun_reason="history check")
    assert again["history"][-1]["previous_result_sha256"] == first_hash
    again["groups"]["in-process|simple"]["leaks"] = 1
    write(project / security.RESULTS_PATH, again)
    with pytest.raises(ValueError, match="result"):
        security.analyze_h6(project)


def test_m7_security_token_never_reaches_artifacts(
    project, monkeypatch, capsys, caplog
):
    users = [USER | {"token": "FAKE_SECRET"}, OWNER | {"token": "FAKE_OWNER_SECRET"}]
    write(project / "data/interim/mcp/tokens.json", {"users": users})
    write(
        project / "data/manifest/mcp_token_hashes.json",
        {
            "users": [
                {k: v for k, v in u.items() if k != "token"}
                | {
                    "token_sha256": security.hashlib.sha256(
                        u["token"].encode()
                    ).hexdigest()
                }
                for u in users
            ]
        },
    )
    client = security.MCPClient(project)
    fake = FakeMCP()

    async def request(token, name, args):
        if name is None:
            return None
        asker = next(u for u in users if u["token"] == token)
        return fake.call(asker, name, args)

    monkeypatch.setattr(client, "_request", request)
    result = run(project, mcp=client)
    texts = [
        json.dumps(result),
        capsys.readouterr().out,
        caplog.text,
        (project / security.RAW_PATH).read_text(),
        (project / "data/interim/m7/security/answers-01.jsonl").read_text(),
    ]
    assert all("FAKE_SECRET" not in t and "FAKE_OWNER_SECRET" not in t for t in texts)


def test_m7_security_partial_question_populations(project):
    result = run(project)
    assert result["partial_answer_rates"]["simple"]["restricted"]["denominator"] == 200
    assert result["partial_answer_rates"]["simple"]["all"]["denominator"] == 231
    assert (
        result["partial_answer_rates"]["simple"]["true_no_answer"]["denominator"] == 3
    )
