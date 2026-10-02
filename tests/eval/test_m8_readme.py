"""README derives results from committed manifests without external calls."""

import json
import re
import shutil
from pathlib import Path

import pytest
from test_m8_contract import completed, put

from kodocs.eval.m8_contract import LATENCY_PATH, RESULTS_DIR, H8ResultError
from kodocs.eval.m8_readme import BEGIN, END, main, render, write_readme

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source(tmp_path):
    shutil.copytree(ROOT / "data/manifest", tmp_path / "data/manifest")
    shutil.copytree(ROOT / "docs/results", tmp_path / "docs/results")
    shutil.copy(ROOT / "README.md", tmp_path / "README.md")
    for path in (tmp_path / RESULTS_DIR).glob("*"):
        path.unlink()
    if (tmp_path / RESULTS_DIR).exists():
        (tmp_path / RESULTS_DIR).rmdir()
    (tmp_path / "data/manifest/m8_test_runs.jsonl").unlink(missing_ok=True)
    return tmp_path


def alter(root, name, mutate):
    path = root / "data/manifest" / name
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))


def test_manifest_values(source):
    def analysis(d):
        d["h5"]["primary"].update(mean_diff=1.234, ci_low=0.321, ci_high=2.345)
        d["kappa"]["quadratic_kappa"] = 0.876
        for group in d["h6"]["by_path_system"].values():
            group.update(leaks=7, n=123)

    alter(source, "m7_analysis.json", analysis)

    def latency(d):
        d["history"] = []
        d["systems"]["simple"]["p95_s"] = 4.567

    alter(source, "m7_latency.json", latency)
    alter(
        source,
        "m5_test_results/run-01.json",
        lambda d: d["analysis"]["ladder"]["stages"][0].update(mean_recall10=0.654),
    )
    body = render(source)
    for text in ("+1.234 [0.321, 2.345]", "0.876", "4.567", "7/123", "0.654"):
        assert text in body


def test_repository_is_current():
    original = (ROOT / "README.md").read_text()
    assert original.split(BEGIN)[1].split(END)[0] == "\n\n" + render(ROOT) + "\n\n"


def test_h8_states_and_error_does_not_write(source):
    assert "리랭커 실험(H8)은 미실행" in render(source)
    completed(source)
    body = render(source)
    assert "H8 채택" in body
    assert "+0.123 [0.020, 0.200]" in body
    assert "원본 해시 대조 생략(로컬 원본 없음)" in body
    put(
        source,
        LATENCY_PATH,
        {
            "rerank_s": {"p50_s": 0.1, "p95_s": 0.2, "max_s": 0.3},
            "conservative_sum_s": 4.0,
            "margin_to_5s": 1.0,
            "simple_p95_m7": 3.8,
        },
    )
    assert "리랭커 지연" in render(source)
    (source / RESULTS_DIR / "run-01.json").write_text("{}")
    before = (source / "README.md").read_bytes()
    with pytest.raises(H8ResultError):
        write_readme(source)
    assert main(["--root", str(source)]) != 0
    assert (source / "README.md").read_bytes() == before


def test_images(source):
    assert render(source).count("촬영 예정") == 2
    images = source / "docs/images"
    images.mkdir(parents=True, exist_ok=True)
    for name in ("demo-claude-code-mcp.gif", "demo-streamlit.png"):
        (images / name).touch()
    body = render(source)
    assert "촬영 예정" not in body
    assert "![" in body and "docs/images/demo-streamlit.png" in body


def test_idempotent_preserves_manual_bytes(source):
    original = (source / "README.md").read_bytes()
    write_readme(source)
    first = (source / "README.md").read_bytes()
    write_readme(source)
    assert first == (source / "README.md").read_bytes()
    for index, separator in ((0, BEGIN.encode()), (-1, END.encode())):
        assert original.split(separator)[index] == first.split(separator)[index]


def test_editorial_contract():
    text = (ROOT / "README.md").read_text()
    assert len(text.splitlines()) <= 400
    assert "```mermaid\nflowchart" in text
    assert "이 README는 구현이 진행되면서" not in text
    expected = "[harness_framework_for_test](https://github.com/apinkpanda722/harness_framework_for_test)의 파일을 복사해 시작했다. Claude Code에서 `/harness`로 phase와 step을 설계하고, `scripts/execute.py`가 step마다 Codex(`codex exec`)로 코드를 수정한 뒤 `make verify`로 검증하고 커밋한다. 하네스 사용법은 [`scripts/README.md`](scripts/README.md)를 참고한다."
    assert expected in text
    assert text.index("## 개발 방식") < text.index("## 문서 링크")
    limits = text.split("## 한계")[1].split("## 사후 정정")[0]
    assert "n-gram" in limits and "ADR-028" in limits
    assert "docs/results/2026-09-30-m5-retrieval.md" in limits
    manual = text.split(BEGIN)[0] + text.split(END)[1]
    # Explicit version allowlist; dates in link targets are not result numbers.
    manual = re.sub(r"\]\([^)]*\)", "]", manual)
    manual = manual.replace("Python 3.12", "Python").replace(
        "OpenSearch 3.x", "OpenSearch"
    )
    assert not re.search(r"\d+\.\d+|\d+\s*%|κ|P95|NDCG@?\d|Recall@?\d", manual)
    targets = set(
        re.findall(r"^([\w-]+):", (ROOT / "Makefile").read_text(), re.MULTILINE)
    )
    commands = set(re.findall(r"make ([\w-]+)", text))
    assert commands <= targets


def test_readme_reports_runaway_ui_cap_ledger_gap_and_next_improvement():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert "## 다음 개선" in text and "근거별 enum" in text
    assert "답변 계약 변경이라 재평가가 필요" in text
    assert "최종 답변 출력 상한은 GUI에서 1500토큰(평가 설정은 4000토큰)" in text
    assert "1회 자동 재생성" in text
    assert "원장에 기록되지 않았다" in text and "$0.01" in text
    assert "비스트리밍 폭주 0건 근거 정정" in text
    assert (
        "스트리밍·스키마와 무관하게 발생했고 건수가 작아 경로 간 차이는 판단하지 않는다"
        in text
    )


@pytest.mark.parametrize("field", ["config_hash", "config_file_sha256"])
def test_h8_internal_and_file_hash_errors_do_not_rewrite_readme(source, field):
    import hashlib

    from kodocs.eval.m8_contract import RESULT_PATH, RUNS_PATH

    payload = completed(source)
    payload[field] = "a" * 64
    result = put(source, RESULT_PATH, payload)
    record = json.loads((source / RUNS_PATH).read_text())
    record["result_sha256"] = hashlib.sha256(result.read_bytes()).hexdigest()
    (source / RUNS_PATH).write_text(json.dumps(record) + "\n")
    original = (source / "README.md").read_bytes()
    with pytest.raises(H8ResultError, match=field):
        write_readme(source)
    assert original == (source / "README.md").read_bytes()
