"""Tests for offline M2 parse-target selection."""

import json
from pathlib import Path

import pytest

from kodocs.ingest import parse_targets
from kodocs.ingest.parse_targets import select_targets


def law_record(
    serial: str,
    *,
    rule_id: str,
    name: str,
    issued_date: str,
    is_current: bool,
    annex_count: int = 0,
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "serial": serial,
        "name": name,
        "issued_date": issued_date,
        "is_current": is_current,
        "annex_count": annex_count,
        "annex_titles": [],
    }


def alio_record(
    file_no: str,
    *,
    rule_no: str,
    path: str,
    sha256: str,
    kind: str = "hwp5",
    source: str = "alio",
    zip_member_path: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": source,
        "file_no": file_no,
        "rule_no": rule_no,
        "rule_title": f"규정 {rule_no}",
        "file_name": Path(path).name,
        "local_path": path,
        "sha256": sha256,
        "file_kind": kind,
    }
    if zip_member_path is not None:
        record["zip_member_path"] = zip_member_path
    return record


def match_record(
    serial: str | None,
    alio: dict[str, object],
    *,
    confidence: str = "exact",
    eligible: bool = True,
) -> dict[str, object]:
    return {
        "law_serial": serial,
        "alio_file_no": alio["file_no"],
        "alio_local_path": alio["local_path"],
        "alio_sha256": alio["sha256"],
        "rule_no": alio["rule_no"],
        "confidence": confidence,
        "parse_eval_eligible": eligible,
    }


def conversion_record(
    alio: dict[str, object],
    *,
    status: str = "ok",
    page_count: int = 1,
    fonts: list[str] | None = None,
) -> dict[str, object]:
    return {
        "source_sha256": alio["sha256"],
        "output_path": f"data/raw/pdf/{alio['file_no']}.pdf",
        "page_count": page_count,
        "status": status,
        "embedded_fonts": fonts or [],
        "hangul_distinct": 10,
    }


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def test_select_targets_applies_current_history_and_conversion_rules() -> None:
    law = [
        law_record(
            "r1-old",
            rule_id="rule-1",
            name="첫 규정",
            issued_date="2020-01-01",
            is_current=False,
        ),
        law_record(
            "r1-mid",
            rule_id="rule-1",
            name="첫 규정",
            issued_date="2022-01-01",
            is_current=False,
        ),
        law_record(
            "r1-current",
            rule_id="rule-1",
            name="첫 규정",
            issued_date="2024-01-01",
            is_current=True,
            annex_count=3,
        ),
        law_record(
            "r2-old",
            rule_id="rule-2",
            name="둘째 규정",
            issued_date="2018-01-01",
            is_current=False,
        ),
        law_record(
            "r2-mid",
            rule_id="rule-2",
            name="둘째 규정",
            issued_date="2020-01-01",
            is_current=False,
        ),
        law_record(
            "r2-recent",
            rule_id="rule-2",
            name="둘째 규정",
            issued_date="2023-01-01",
            is_current=False,
        ),
        law_record(
            "r2-current",
            rule_id="rule-2",
            name="둘째 규정",
            issued_date="2025-01-01",
            is_current=True,
            annex_count=1,
        ),
        law_record(
            "r3-current",
            rule_id="rule-3",
            name="누락 규정",
            issued_date="2025-02-01",
            is_current=True,
        ),
    ]

    r1_hwp = alio_record(
        "99", rule_no="10", path="data/raw/alio/10/99__현행.hwp", sha256="a" * 64
    )
    r1_hwpx_low = alio_record(
        "2",
        rule_no="10",
        path="data/raw/alio/10/2__현행.hwpx",
        sha256="b" * 64,
        kind="hwpx",
    )
    r1_hwpx_high = alio_record(
        "3",
        rule_no="10",
        path="data/raw/alio/10/3__현행.hwpx",
        sha256="c" * 64,
        kind="hwpx",
    )
    r2_current = alio_record(
        "40", rule_no="20", path="data/raw/alio/20/40__현행.hwp", sha256="d" * 64
    )
    r2_old = alio_record(
        "11", rule_no="20", path="data/raw/alio/20/11__과거.hwp", sha256="e" * 64
    )
    r2_mid = alio_record(
        "12", rule_no="20", path="data/raw/alio/20/12__과거.hwp", sha256="f" * 64
    )
    r2_recent = alio_record(
        "13",
        rule_no="20",
        path="data/raw/alio/20/13__과거.hwpx",
        sha256="1" * 64,
        kind="hwpx",
    )
    failed_current = alio_record(
        "50", rule_no="30", path="data/raw/alio/30/50__현행.hwp", sha256="2" * 64
    )
    alio = [
        r1_hwp,
        r1_hwpx_low,
        r1_hwpx_high,
        r2_current,
        r2_old,
        r2_mid,
        r2_recent,
        failed_current,
    ]
    matches = [
        match_record("r1-current", r1_hwp),
        match_record("r1-current", r1_hwpx_low, confidence="approximate"),
        match_record("r1-current", r1_hwpx_high),
        match_record("r2-current", r2_current, confidence="approximate"),
        match_record("r2-old", r2_old),
        match_record("r2-mid", r2_mid),
        match_record("r2-recent", r2_recent),
        # A malformed eligible flag must never override failed confidence.
        match_record("r3-current", failed_current, confidence="failed", eligible=True),
    ]
    conversions = [
        conversion_record(
            r1_hwpx_high,
            page_count=5,
            fonts=["Arial", "HCR Batang", "HY견고딕"],
        ),
        conversion_record(r2_current, page_count=10, fonts=["HCR Dotum"]),
        conversion_record(r2_recent, page_count=7, fonts=["HCR Batang"]),
    ]

    selection = select_targets(
        law,
        alio,
        matches,
        conversions,
        history_rules=1,
        versions_per_rule=2,
    )

    assert [
        (target.role, target.rule_no, target.law_serial) for target in selection.targets
    ] == [
        ("current", "10", "r1-current"),
        ("current", "20", "r2-current"),
        ("history", "20", "r2-old"),
        ("history", "20", "r2-recent"),
    ]
    chosen = selection.targets[0]
    assert chosen.source_path == r1_hwpx_high["local_path"]
    assert chosen.source_kind == "hwpx"
    assert chosen.pdf_path == "data/raw/pdf/3.pdf"
    assert chosen.page_count == 5
    assert chosen.law_annex_count == 3
    assert chosen.embedded_fonts == ("Arial", "HCR Batang", "HY견고딕")
    assert chosen.non_hcr_fonts == ("Arial", "HY견고딕")
    assert selection.targets[2].pdf_path is None
    assert selection.targets[2].page_count is None
    assert selection.current_count == 2
    assert selection.history_count == 2
    assert selection.pages_known == 22
    assert selection.targets_without_page_count == 1
    assert [(item.rule_id, item.name) for item in selection.missing_current] == [
        ("rule-3", "누락 규정")
    ]
    assert "eligible" in selection.missing_current[0].reason


def test_history_ranking_and_one_version_choose_most_recent_past() -> None:
    law: list[dict[str, object]] = []
    alio: list[dict[str, object]] = []
    matches: list[dict[str, object]] = []
    for rule_id, rule_no, dates in (
        ("a-rule", "10", ("2019-01-01", "2020-01-01", "2021-01-01")),
        (
            "z-rule",
            "20",
            ("2018-01-01", "2019-01-01", "2020-01-01", "2021-01-01"),
        ),
    ):
        for index, issued_date in enumerate(dates):
            serial = f"{rule_id}-{index}"
            law.append(
                law_record(
                    serial,
                    rule_id=rule_id,
                    name=rule_id,
                    issued_date=issued_date,
                    is_current=index == len(dates) - 1,
                )
            )
            source = alio_record(
                str(index),
                rule_no=rule_no,
                path=f"data/raw/alio/{rule_no}/{index}.hwp",
                sha256=f"{rule_no}{index}".ljust(64, "0"),
            )
            alio.append(source)
            matches.append(match_record(serial, source))

    selection = select_targets(
        law,
        alio,
        matches,
        [],
        history_rules=2,
        versions_per_rule=1,
    )

    history = [target for target in selection.targets if target.role == "history"]
    assert [(target.law_rule_id, target.issued_date) for target in history] == [
        ("z-rule", "2020-01-01")
    ]


def test_run_uses_latest_manifest_records_writes_fresh_json_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale_law = law_record(
        "serial-1",
        rule_id="rule-1",
        name="이전 이름",
        issued_date="2020-01-01",
        is_current=False,
    )
    current_law = law_record(
        "serial-1",
        rule_id="rule-1",
        name="현행 이름",
        issued_date="2024-01-01",
        is_current=True,
        annex_count=2,
    )
    stale_alio = alio_record(
        "7", rule_no="10", path="data/raw/alio/10/old.hwp", sha256="a" * 64
    )
    current_alio = {
        **stale_alio,
        "local_path": "data/raw/alio/10/current.hwp",
        "sha256": "b" * 64,
    }
    matched = match_record("serial-1", current_alio)
    old_conversion = conversion_record(current_alio, page_count=3)
    new_conversion = conversion_record(
        current_alio, page_count=4, fonts=["HCR Batang", "TimesNewRoman"]
    )
    manifest_root = tmp_path / "data/manifest"
    write_jsonl(manifest_root / "law_api.jsonl", [stale_law, current_law])
    write_jsonl(manifest_root / "alio.jsonl", [stale_alio, current_alio])
    write_jsonl(manifest_root / "version_matches.jsonl", [matched])
    write_jsonl(manifest_root / "conversions.jsonl", [old_conversion, new_conversion])
    output_path = manifest_root / "parse_targets.json"
    output_path.write_text('{"stale": true}\n', encoding="utf-8")

    selection = parse_targets.run(tmp_path, history_rules=0, versions_per_rule=0)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["params"] == {"history_rules": 0, "versions_per_rule": 0}
    assert payload["totals"] == {
        "targets": 1,
        "current": 1,
        "history": 0,
        "pages_known": 4,
        "targets_without_page_count": 0,
    }
    assert payload["missing_current"] == []
    assert payload["targets"][0] == {
        "rule_no": "10",
        "law_rule_id": "rule-1",
        "law_serial": "serial-1",
        "issued_date": "2024-01-01",
        "role": "current",
        "source_path": "data/raw/alio/10/current.hwp",
        "source_sha256": "b" * 64,
        "source_kind": "hwp5",
        "pdf_path": "data/raw/pdf/7.pdf",
        "page_count": 4,
        "match_confidence": "exact",
        "law_annex_count": 2,
        "embedded_fonts": ["HCR Batang", "TimesNewRoman"],
        "non_hcr_fonts": ["TimesNewRoman"],
    }
    serialized = output_path.read_text(encoding="utf-8")
    assert "이전 이름" not in serialized
    assert "담당자" not in serialized
    assert selection.pages_known == 4
    output = capsys.readouterr().out
    assert "대상 1개(현행 1개, 과거 0개)" in output
    assert "알려진 총 페이지 수 4" in output
    assert "페이지 수 미상 0개" in output
    assert "현행 누락 규정 0개" in output


def test_latest_failed_conversion_is_treated_as_not_converted() -> None:
    law = [
        law_record(
            "serial-1",
            rule_id="rule-1",
            name="현행 규정",
            issued_date="2024-01-01",
            is_current=True,
        )
    ]
    source = alio_record(
        "1", rule_no="10", path="data/raw/alio/10/1.hwp", sha256="a" * 64
    )

    selection = select_targets(
        law,
        [source],
        [match_record("serial-1", source)],
        [
            conversion_record(source, page_count=9),
            conversion_record(source, status="failed", page_count=0),
        ],
    )

    assert selection.targets[0].pdf_path is None
    assert selection.targets[0].page_count is None
    assert selection.pages_known == 0
    assert selection.targets_without_page_count == 1


def test_main_lists_commands_for_all_missing_input_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = parse_targets.main(["--root", str(tmp_path)])

    assert exit_code != 0
    error = capsys.readouterr().err
    assert "make collect-law" in error
    assert "make collect-alio" in error
    assert "make match-versions" in error
    assert "make convert-hwp" in error
    assert not (tmp_path / "data/manifest/parse_targets.json").exists()
