"""Tests for deterministic article-level parser scoring."""

import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from kodocs.eval.parsing_score import (
    ParsingScoreError,
    aggregate,
    main,
    run,
    score_document,
    splitter_ceiling,
)
from kodocs.ingest.manifest import sha256_file
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    output_path,
    to_json,
)


def _document(
    text: str,
    *,
    parser: str = "pypdf",
    input_sha256: str = "a" * 64,
) -> ParsedDocument:
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser,
        parser_version="1.0",
        settings={},
        settings_hash="settings",
        input_path="data/raw/pdf/test.pdf",
        input_sha256=input_sha256,
        input_kind="pdf",
        page_count=1,
        blocks=(Block(page=1, kind="text", text=text),),
        tables=(),
        elapsed_s=0.0,
    )


def _target(
    root: Path,
    *,
    rule: str,
    serial: str,
    source: str,
    role: str,
) -> tuple[dict[str, object], str, str]:
    pdf_path = root / "data/raw/pdf" / f"{rule}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    (root / "data/manifest").mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(f"pdf:{rule}:{serial}".encode())
    input_sha256 = sha256_file(pdf_path)
    target_key = f"{rule}:{serial}:{source}"
    return (
        {
            "law_rule_id": rule,
            "law_serial": serial,
            "source_sha256": source,
            "pdf_path": pdf_path.relative_to(root).as_posix(),
            "role": role,
        },
        target_key,
        input_sha256,
    )


def _write_law(root: Path, rule: str, serial: str, law: dict[str, object]) -> None:
    path = root / "data/interim/law" / rule / f"{serial}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(law, ensure_ascii=False), encoding="utf-8")


def _write_document(root: Path, document: ParsedDocument) -> None:
    path = output_path(root, document.parser, document.input_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(document), encoding="utf-8")


def _run_record(
    parser: str, target_key: str, input_sha256: str, status: str
) -> dict[str, object]:
    return {
        "parser": parser,
        "target_key": target_key,
        "input_sha256": input_sha256,
        "status": status,
    }


def test_score_document_pairs_first_unique_keys_and_reports_both_text_forms() -> None:
    law = {
        "articles": [
            "제1장 총칙",
            "제1조 (목적) “이 규정”은 기준이다. <개정 2021. 3. 16.>",
            "제2조(절차) 첫 법제처 조문",
            "제2조(중복) 짝지어지지 않는 법제처 중복",
        ],
        "addenda": [{"text": "부칙 시행한다."}],
    }
    parsed = _document(
        '제1조(목적) "이 규정"은 기준이다.\n'
        "제2조(절차) 첫 파싱 조문\n"
        "제2조(중복) 짝지어지지 않는 파싱 중복\n"
        "제3조(추가) 파싱에만 있는 조문"
    )

    score = score_document(
        parsed,
        law,
        status="ok",
        target_key="rule:serial:source",
        parser="pypdf",
    )

    assert score.law_articles == 3
    assert score.parsed_articles == 4
    assert score.matched == 2
    assert score.key_precision == pytest.approx(0.5)
    assert score.key_recall == pytest.approx(2 / 3)
    assert score.order_lcs_ratio == pytest.approx(1.0)
    assert score.structure_exact is False
    assert (score.addenda_law, score.addenda_parsed) == (1, 0)
    assert score.text_sim_raw_mean < score.text_sim_norm_mean
    assert score.text_exact_raw_rate == 0.0
    assert score.text_exact_norm_rate == 0.5
    assert (score.editorial_law_only, score.editorial_parsed_only) == (1, 0)
    assert score.editorial_both == 0


@pytest.mark.parametrize("status", ["failed", "empty", "missing", "missing_law"])
def test_non_ok_statuses_remain_in_denominator_with_zero_metrics(status: str) -> None:
    law = {"articles": ["제1조(목적) 본문"], "addenda": []}

    score = score_document(
        None,
        law,
        status=status,
        target_key="target",
        parser="pypdf",
    )

    assert score.status == status
    assert score.law_articles == (0 if status == "missing_law" else 1)
    assert score.parsed_articles == 0
    assert score.matched == 0
    assert score.key_precision == 0.0
    assert score.text_sim_raw_mean == 0.0


def test_splitter_ceiling_uses_original_articles_as_truth() -> None:
    law = {
        "articles": [
            "제1장 총칙",
            "제1조(목적) 본문",
            "제2조의2(특례) 특례 본문",
        ],
        "addenda": [{"text": "부칙 <제1호, 2020.1.1.>"}],
        "annexes": [],
    }

    score = splitter_ceiling(law, target_key="target")

    assert score.parser == "splitter-ceiling"
    assert score.status == "ok"
    assert score.structure_exact is True
    assert score.law_articles == score.parsed_articles == score.matched == 2
    assert score.text_exact_raw_rate == score.text_exact_norm_rate == 1.0


def test_aggregate_reports_all_and_ok_denominators_with_micro_and_macro_text() -> None:
    exact = score_document(
        _document("제1조(목적) 본문"),
        {"articles": ["제1조(목적) 본문"], "addenda": []},
        status="ok",
        target_key="exact",
        parser="pypdf",
    )
    changed = score_document(
        _document("제1조(목적) 완전히 다른 글"),
        {"articles": ["제1조(목적) 본문"], "addenda": []},
        status="ok",
        target_key="changed",
        parser="pypdf",
    )
    failed = score_document(
        None,
        {"articles": ["제1조(목적) 본문"], "addenda": []},
        status="failed",
        target_key="failed",
        parser="pypdf",
    )

    result = aggregate([exact, changed, failed])

    assert result["status_counts"] == {"failed": 1, "ok": 2}
    assert result["all_targets"]["documents"] == 3
    assert result["ok_only"]["documents"] == 2
    assert result["all_targets"]["matched_articles"] == 2
    assert result["ok_only"]["matched_articles"] == 2
    assert result["all_targets"]["text_exact_norm_rate_micro"] == 0.5
    assert result["all_targets"]["text_sim_norm_mean_macro"] == pytest.approx(
        (exact.text_sim_norm_mean + changed.text_sim_norm_mean) / 3
    )
    assert result["ok_only"]["text_sim_norm_mean_macro"] == pytest.approx(
        (exact.text_sim_norm_mean + changed.text_sim_norm_mean) / 2
    )


def test_run_scores_latest_results_roles_ceiling_and_keeps_raw_text_out_of_manifest(
    tmp_path: Path,
) -> None:
    current, current_key, current_sha = _target(
        tmp_path,
        rule="rule-1",
        serial="serial-1",
        source="1" * 64,
        role="current",
    )
    history, history_key, history_sha = _target(
        tmp_path,
        rule="rule-2",
        serial="serial-2",
        source="2" * 64,
        role="history",
    )
    target_path = tmp_path / "data/manifest/parse_targets.json"
    target_path.write_text(
        json.dumps({"targets": [current, history]}, ensure_ascii=False),
        encoding="utf-8",
    )
    current_law = {
        "articles": [
            "제1조(목적) 법제처 원문 비밀표식",
            "제2조(절차) 법제처 두 번째 조문",
        ],
        "addenda": [],
        "annexes": [],
    }
    history_law = {
        "articles": ["제1조(목적) 과거 법제처 원문"],
        "addenda": [],
        "annexes": [],
    }
    _write_law(tmp_path, "rule-1", "serial-1", current_law)
    _write_law(tmp_path, "rule-2", "serial-2", history_law)
    parsed = _document(
        "제1조(목적) 파싱 원문 비밀표식\n제3조(추가) 파싱 추가 조문",
        input_sha256=current_sha,
    )
    _write_document(tmp_path, parsed)
    records = [
        _run_record("pypdf", current_key, current_sha, "failed"),
        _run_record("pypdf", history_key, history_sha, "failed"),
        _run_record("pypdf", current_key, current_sha, "skipped"),
    ]
    runs_path = tmp_path / "data/manifest/parse_runs.jsonl"
    runs_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    out = io.StringIO()

    payload = run(tmp_path, parsers=["pypdf", "docling"], out=out)

    score_path = tmp_path / "data/manifest/parse_scores.json"
    written = json.loads(score_path.read_text(encoding="utf-8"))
    assert payload == written
    assert written["parse_targets_sha256"] == sha256_file(target_path)
    assert set(written["normalization_rules"]) == {"N1", "N2", "N3", "N4", "N5"}
    pypdf = written["parsers"]["pypdf"]
    assert pypdf["status"] == "scored"
    assert [item["status"] for item in pypdf["documents"]] == ["ok", "failed"]
    assert pypdf["aggregate"]["overall"]["all_targets"]["documents"] == 2
    assert pypdf["aggregate"]["overall"]["ok_only"]["documents"] == 1
    assert pypdf["aggregate"]["roles"]["current"]["all_targets"]["documents"] == 1
    assert pypdf["aggregate"]["roles"]["history"]["all_targets"]["documents"] == 1
    assert written["parsers"]["docling"] == {"status": "not_run"}
    assert written["parsers"]["splitter-ceiling"]["status"] == "scored"
    serialized = score_path.read_text(encoding="utf-8")
    assert "법제처 원문 비밀표식" not in serialized
    assert "파싱 원문 비밀표식" not in serialized
    diff_path = tmp_path / "data/interim/parse_diffs/pypdf" / f"{current_sha}.json"
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    assert diff["unmatched_law_keys"] == [[2, 0]]
    assert diff["unmatched_parsed_keys"] == [[3, 0]]
    assert "법제처 원문 비밀표식" in json.dumps(diff, ensure_ascii=False)
    assert "| pypdf | 1/2 |" in out.getvalue()
    assert "docling: 실행 안 됨" in out.getvalue()
    assert "법제처 원문 비밀표식" not in out.getvalue()


def test_run_keeps_missing_law_target_and_reports_ceiling_unmatched_keys(
    tmp_path: Path,
) -> None:
    target, target_key, input_sha256 = _target(
        tmp_path,
        rule="missing-law",
        serial="serial",
        source="f" * 64,
        role="current",
    )
    target_path = tmp_path / "data/manifest/parse_targets.json"
    target_path.write_text(json.dumps({"targets": [target]}), encoding="utf-8")
    record = _run_record("pypdf", target_key, input_sha256, "ok")
    runs_path = tmp_path / "data/manifest/parse_runs.jsonl"
    runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    out = io.StringIO()

    payload = run(tmp_path, parsers=["pypdf"], out=out)

    assert payload["parsers"]["pypdf"]["documents"][0]["status"] == "missing_law"
    assert (
        payload["parsers"]["splitter-ceiling"]["documents"][0]["status"]
        == "missing_law"
    )


def test_all_requested_parsers_without_records_fail_before_writing_scores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _, _ = _target(
        tmp_path,
        rule="rule",
        serial="serial",
        source="a" * 64,
        role="current",
    )
    target_path = tmp_path / "data/manifest/parse_targets.json"
    target_path.write_text(json.dumps({"targets": [target]}), encoding="utf-8")

    with pytest.raises(ParsingScoreError, match="make parse-pypdf"):
        run(tmp_path, parsers=["pypdf"], out=io.StringIO())
    assert not (tmp_path / "data/manifest/parse_scores.json").exists()

    assert main(["--root", str(tmp_path), "--parser", "pypdf"]) == 1
    assert "make parse-pypdf" in capsys.readouterr().err


def test_docscore_serialization_contains_only_declared_numeric_key_status_fields() -> (
    None
):
    score = score_document(
        _document("제1조(목적) 본문"),
        {"articles": ["제1조(목적) 본문"], "addenda": []},
        status="ok",
        target_key="target",
        parser="pypdf",
    )

    assert set(asdict(score)) == {
        "parser",
        "target_key",
        "input_sha256",
        "status",
        "law_articles",
        "parsed_articles",
        "matched",
        "key_precision",
        "key_recall",
        "order_lcs_ratio",
        "structure_exact",
        "addenda_law",
        "addenda_parsed",
        "text_sim_raw_mean",
        "text_sim_norm_mean",
        "text_exact_raw_rate",
        "text_exact_norm_rate",
        "editorial_law_only",
        "editorial_parsed_only",
        "editorial_both",
    }
    assert "본문" not in json.dumps(asdict(score), ensure_ascii=False)


def test_law_without_article_text_is_excluded_from_parser_and_ceiling_denominators(
    tmp_path: Path,
) -> None:
    image, image_key, image_sha = _target(
        tmp_path, rule="rule-img", serial="serial-img", source="3" * 64, role="current"
    )
    normal, normal_key, normal_sha = _target(
        tmp_path, rule="rule-ok", serial="serial-ok", source="4" * 64, role="current"
    )
    (tmp_path / "data/manifest/parse_targets.json").write_text(
        json.dumps({"targets": [image, normal]}), encoding="utf-8"
    )
    _write_law(
        tmp_path,
        "rule-img",
        "serial-img",
        {
            "articles": ['<img alt="img1" src="/LSW/flDownload.do?flSeq=1">'],
            "addenda": [{"text": "부칙 시행한다."}],
            "annexes": [],
        },
    )
    _write_law(
        tmp_path,
        "rule-ok",
        "serial-ok",
        {"articles": ["제1조(목적) 정상 조문"], "addenda": [], "annexes": []},
    )
    # A version outside parse_targets also counts toward the corpus-wide total.
    _write_law(
        tmp_path,
        "rule-ok",
        "serial-old",
        {"articles": ["제1장 총칙"], "addenda": [], "annexes": []},
    )
    _write_document(
        tmp_path,
        _document("제1조(목적) 이미지 문서의 PDF 조문", input_sha256=image_sha),
    )
    _write_document(
        tmp_path, _document("제1조(목적) 정상 조문", input_sha256=normal_sha)
    )
    (tmp_path / "data/manifest/parse_runs.jsonl").write_text(
        json.dumps(_run_record("pypdf", image_key, image_sha, "ok"))
        + "\n"
        + json.dumps(_run_record("pypdf", normal_key, normal_sha, "ok"))
        + "\n",
        encoding="utf-8",
    )

    payload = run(tmp_path, parsers=["pypdf"], out=io.StringIO())

    pypdf = payload["parsers"]["pypdf"]
    overall = pypdf["aggregate"]["overall"]
    assert [d["status"] for d in pypdf["documents"]] == ["no_law_text", "ok"]
    assert overall["status_counts"] == {"no_law_text": 1, "ok": 1}
    assert overall["all_targets"]["documents"] == 1
    assert overall["ok_only"]["documents"] == 1
    assert overall["all_targets"]["structure_exact_rate"] == 1.0
    assert overall["excluded"] == {"no_law_text": 1}

    ceiling = payload["parsers"]["splitter-ceiling"]
    assert [d["status"] for d in ceiling["documents"]] == ["no_law_text", "ok"]
    assert ceiling["aggregate"]["overall"]["all_targets"]["documents"] == 1
    assert ceiling["aggregate"]["overall"]["excluded"] == {"no_law_text": 1}

    assert payload["excluded_targets"] == [
        {
            "target_key": image_key,
            "law_rule_id": "rule-img",
            "law_serial": "serial-img",
            "role": "current",
            "status": "no_law_text",
            "reason": "법제처 본문이 이미지 한 줄",
        }
    ]
    corpus = payload["law_versions_without_article_text"]
    assert corpus["versions_total"] == 3
    assert corpus["versions_without_article_text"] == 2
    assert {
        (v["law_rule_id"], v["law_serial"], v["reason"]) for v in corpus["versions"]
    } == {
        ("rule-img", "serial-img", "법제처 본문이 이미지 한 줄"),
        ("rule-ok", "serial-old", "법제처 본문에 조 머리가 없음"),
    }
    assert not (
        tmp_path / "data/interim/parse_diffs/pypdf" / f"{image_sha}.json"
    ).exists()
