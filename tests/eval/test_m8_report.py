"""Reports authenticate local evidence and preserve manual bytes."""

import json
import shutil
from pathlib import Path

import pytest
from test_m8_contract import completed, put

from kodocs.eval import m8_report as m
from kodocs.eval.m8_contract import LATENCY_PATH, H8ResultError

ROOT = Path(__file__).resolve().parents[2]


def source(root):
    # Real M8 run artifacts (ledger, results, latency) would pollute the fixtures
    # as soon as the experiment has actually been run.
    shutil.copytree(
        ROOT / "data/manifest",
        root / "data/manifest",
        ignore=shutil.ignore_patterns(
            "m8_test_runs.jsonl", "m8_test_results", "m8_latency.json"
        ),
    )
    completed(root, raw_present=True)
    put(
        root,
        LATENCY_PATH,
        {
            "rerank_s": {"p50_s": 0.111, "p95_s": 0.222, "max_s": 0.333},
            "conservative_sum_s": 4.123,
            "margin_to_5s": 0.877,
            "simple_p95_m7": 3.901,
        },
    )
    return root


def test_manifest_numbers_and_preservation(tmp_path):
    root = source(tmp_path)
    before = (root / "data/manifest/m5_test_results/run-01.json").read_bytes()
    m5_document = root / "docs/results/2026-09-30-m5-retrieval.md"
    m5_document.parent.mkdir(parents=True, exist_ok=True)
    m5_document.write_bytes(b"M5 frozen document")
    path = root / m.REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"manual prefix\n{m.BEGIN}\nold\n{m.END}\nmanual suffix")
    m.write_report(root)
    text = path.read_text()
    for value in ["H8 채택", "0.123", "0.020", "0.200", "0.222", "4.123"]:
        assert value in text
    assert text.startswith("manual prefix") and text.endswith("manual suffix")
    assert "gold | 2" in text and "silver | 3" in text
    assert before == (root / "data/manifest/m5_test_results/run-01.json").read_bytes()
    assert m5_document.read_bytes() == b"M5 frozen document"


def test_tampered_raw_refuses_write(tmp_path):
    source(tmp_path)
    (tmp_path / "data/interim/m8/test/run-01/runs.jsonl").write_text("{}\n")
    with pytest.raises(H8ResultError):
        m.write_report(tmp_path)
    assert not (tmp_path / m.REPORT_PATH).exists()


def test_latency_raw_tamper_refuses(tmp_path):
    root = source(tmp_path)
    from kodocs.eval.m7_rawhash import RawHashMismatch

    path = root / "data/interim/m8/latency/runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    with pytest.raises(RawHashMismatch):
        m.write_report(root)
    assert not (root / m.REPORT_PATH).exists()


def test_held_verdict_sentence_is_template_only(tmp_path):
    import hashlib

    from kodocs.eval.m8_contract import RESULT_PATH, RUNS_PATH

    root = source(tmp_path)
    result = json.loads((root / RESULT_PATH).read_bytes())
    result["h8"].update(verdict="H8 판정 보류(차이 입증 못 함)", ci_low=-0.1)
    p = put(root, RESULT_PATH, result)
    record = json.loads((root / RUNS_PATH).read_text())
    record["result_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    (root / RUNS_PATH).write_text(json.dumps(record) + "\n")
    text = m.render(root)
    assert (
        "n=21이라 판정 보류가 나올 수 있고 그 경우에도 결과를 그대로 보고한다" in text
    )
    assert result["h8"]["verdict"] == "H8 판정 보류(차이 입증 못 함)"


def test_middle_run_is_also_authenticated(tmp_path):
    from kodocs.eval.m8_contract import RESULTS_DIR

    root = source(tmp_path)
    completed(root, run=2, raw_present=True)
    completed(root, run=3, raw_present=True)
    (root / RESULTS_DIR / "run-02.json").write_text("{}")
    with pytest.raises(H8ResultError):
        m.write_report(root)
    assert not (root / m.REPORT_PATH).exists()


def test_every_rerun_metric_is_reported(tmp_path):
    import hashlib

    from kodocs.eval.m8_contract import RESULTS_DIR, RUNS_PATH

    root = source(tmp_path)
    completed(root, run=2)
    path = root / RESULTS_DIR / "run-02.json"
    payload = json.loads(path.read_text())
    payload["h8"]["diff"] = 0.145
    path.write_text(json.dumps(payload))
    ledger = [json.loads(line) for line in (root / RUNS_PATH).read_text().splitlines()]
    ledger[1]["result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / RUNS_PATH).write_text("".join(json.dumps(r) + "\n" for r in ledger))
    completed(root, run=3)
    assert "0.145" in m.render(root)


def failed_attempt(root, error_type="AttributeError"):
    """Prepend a failed attempt 1 whose raw archive is preserved (as m8_test does)."""
    import hashlib

    from kodocs.eval.m8_contract import RUNS_PATH

    raw_path = "data/interim/m8/test/run-01/failed-attempt-01/runs.jsonl"
    target = root / raw_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"question_id":"x"}\n')
    record = {
        "attempt": 1,
        "seq": 1,
        "status": "failed",
        "error_type": error_type,
        "raw_path": raw_path,
        "raw": {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "rows": 1},
    }
    existing = (root / RUNS_PATH).read_text()
    (root / RUNS_PATH).write_text(json.dumps(record) + "\n" + existing)
    return record


def test_failed_first_attempt_is_reported_and_its_record_is_kept(tmp_path):
    root = source(tmp_path)
    failed_attempt(root)
    text = m.render(root)
    assert "### 실패·중단 시도(원본 보존, 결과 파일 없음)" in text
    assert "AttributeError" in text and "failed-attempt-01/runs.jsonl" in text
    assert "첫 시도 실패 공개(사후 정정)" in text
    assert "class_descriptions.d1" in text and "명시 경로" in text


def test_other_failures_get_the_table_but_not_the_d1_explanation(tmp_path):
    root = source(tmp_path)
    failed_attempt(root, error_type="PreflightError")
    text = m.render(root)
    assert "PreflightError" in text
    assert "첫 시도 실패 공개(사후 정정)" not in text


def test_no_failed_attempt_means_no_failure_section(tmp_path):
    assert "실패·중단 시도" not in m.render(source(tmp_path))


def test_tampered_failed_archive_refuses_the_report(tmp_path):
    from kodocs.eval.m7_rawhash import RawHashMismatch

    root = source(tmp_path)
    record = failed_attempt(root)
    (root / record["raw_path"]).write_bytes(b'{"question_id":"changed"}\n')
    with pytest.raises(RawHashMismatch):
        m.render(root)
