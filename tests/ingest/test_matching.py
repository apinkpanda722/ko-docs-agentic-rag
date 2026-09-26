"""Tests for deterministic ALIO-to-Law-API version matching."""

import json
from pathlib import Path

import pytest

from kodocs.ingest import matching
from kodocs.ingest.matching import (
    Confidence,
    match_file,
    match_rules,
    normalize_rule_name,
    run,
)


def law_version(
    serial: str,
    *,
    rule_id: str = "law-1",
    name: str = "(한국지능정보사회진흥원) 테스트 규정",
    issued_date: str = "2024-10-24",
    effective_date: str | None = None,
    is_current: bool = False,
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "serial": serial,
        "name": name,
        "issued_date": issued_date,
        "effective_date": effective_date,
        "is_current": is_current,
    }


def alio_record(
    *,
    rule_no: str = "10",
    rule_title: str = "테스트 규정",
    file_no: str = "100",
    local_path: str = "data/raw/alio/10/100__테스트.hwp",
    sha256: str = "a" * 64,
    file_kind: str = "hwp5",
    filename_date: dict[str, object] | None = None,
    rule_last_revised: str | None = "2024-10-24",
    source: str = "alio",
    zip_member_path: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": source,
        "rule_no": rule_no,
        "rule_title": rule_title,
        "rule_last_revised": rule_last_revised,
        "file_no": file_no,
        "file_name": Path(local_path).name,
        "file_kind": file_kind,
        "filename_date": filename_date or {"date": "2024-10-24", "precision": "day"},
        "local_path": local_path,
        "sha256": sha256,
    }
    if zip_member_path is not None:
        record["parent_file_no"] = file_no
        record["zip_member_path"] = zip_member_path
    return record


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "name",
    [
        "(한국지능정보사회진흥원) 공직자의 이해충돌 방지제도 운영지침",
        "한국지능정보사회진흥원 공직자의 이해충돌 방지제도 운영지침",
        "공직자의이해충돌·방지제도ㆍ운영・지침",
    ],
)
def test_normalize_rule_name_removes_agency_whitespace_and_middle_dots(
    name: str,
) -> None:
    assert normalize_rule_name(name) == "공직자의이해충돌방지제도운영지침"


def test_match_rules_requires_exactly_one_distinct_law_rule_id() -> None:
    alio_records = [
        alio_record(rule_no="10", rule_title="테스트 규정"),
        alio_record(rule_no="20", rule_title="없는 규정"),
        alio_record(rule_no="30", rule_title="모호·규정"),
    ]
    law_records = [
        law_version("101"),
        law_version("102"),
        law_version("201", rule_id="law-2", name="모호 규정"),
        law_version("301", rule_id="law-3", name="모호ㆍ규정"),
    ]

    assert match_rules(alio_records, law_records) == {
        "10": "law-1",
        "20": None,
        "30": None,
    }


@pytest.mark.parametrize(
    ("issued_date", "effective_date", "basis_date_kind"),
    [
        ("2024-10-24", "2024-11-01", "발령일자"),
        ("2024-10-01", "2024-10-24", "시행일자"),
    ],
)
def test_match_file_day_exactly_matches_issued_or_effective_date(
    issued_date: str,
    effective_date: str,
    basis_date_kind: str,
) -> None:
    result = match_file(
        alio_record(),
        [
            law_version(
                "101",
                issued_date=issued_date,
                effective_date=effective_date,
                is_current=True,
            )
        ],
        is_latest_file=False,
    )

    assert result.law_serial == "101"
    assert result.law_issued_date == issued_date
    assert result.law_is_current is True
    assert result.confidence is Confidence.EXACT
    assert basis_date_kind in result.basis
    assert result.candidates == ()
    assert result.parse_eval_eligible is True


def test_match_file_does_not_choose_when_exact_day_has_multiple_candidates() -> None:
    result = match_file(
        alio_record(),
        [law_version("102"), law_version("101")],
        is_latest_file=False,
    )

    assert result.law_serial is None
    assert result.confidence is Confidence.FAILED
    assert result.candidates == ("101", "102")
    assert result.parse_eval_eligible is False


def test_match_file_uses_unique_approximate_day_candidate() -> None:
    result = match_file(
        alio_record(),
        [
            law_version("101", issued_date="2024-10-30"),
            law_version("102", issued_date="2024-11-20"),
        ],
        is_latest_file=False,
        approx_days=7,
    )

    assert result.law_serial == "101"
    assert result.confidence is Confidence.APPROXIMATE
    assert "±7일" in result.basis


def test_match_file_does_not_choose_nearest_approximate_day_candidate() -> None:
    result = match_file(
        alio_record(),
        [
            law_version("101", issued_date="2024-10-23"),
            law_version("102", issued_date="2024-10-27"),
        ],
        is_latest_file=False,
        approx_days=7,
    )

    assert result.law_serial is None
    assert result.confidence is Confidence.FAILED
    assert result.candidates == ("101", "102")


def test_match_file_month_uses_issued_date_only_and_requires_one_candidate() -> None:
    record = alio_record(filename_date={"date": "2024-10", "precision": "month"})
    result = match_file(
        record,
        [
            law_version("101", issued_date="2024-10-05", effective_date="2024-11-01"),
            law_version("102", issued_date="2024-09-20", effective_date="2024-10-01"),
        ],
        is_latest_file=False,
    )

    assert result.law_serial == "101"
    assert result.confidence is Confidence.APPROXIMATE
    assert "발령" in result.basis


def test_match_file_month_with_multiple_issued_versions_fails() -> None:
    record = alio_record(filename_date={"date": "2024-10", "precision": "month"})
    result = match_file(
        record,
        [
            law_version("101", issued_date="2024-10-05"),
            law_version("102", issued_date="2024-10-20"),
        ],
        is_latest_file=False,
    )

    assert result.confidence is Confidence.FAILED
    assert result.candidates == ("101", "102")


def test_match_file_no_date_uses_retry_rvsn_ymd_only_for_latest_direct_file() -> None:
    record = alio_record(filename_date={"date": None, "precision": "none"})
    versions = [
        law_version("101", issued_date="2024-10-01", effective_date="2024-10-24")
    ]

    latest = match_file(record, versions, is_latest_file=True)
    older = match_file(record, versions, is_latest_file=False)

    assert latest.law_serial == "101"
    assert latest.confidence is Confidence.APPROXIMATE
    assert "retryRvsnYmd" in latest.basis
    assert older.confidence is Confidence.FAILED
    assert older.law_serial is None


def test_match_file_no_date_does_not_choose_between_retry_date_candidates() -> None:
    record = alio_record(filename_date={"date": None, "precision": "none"})

    result = match_file(
        record,
        [
            law_version("101", issued_date="2024-10-24"),
            law_version("102", issued_date="2024-10-01", effective_date="2024-10-24"),
        ],
        is_latest_file=True,
    )

    assert result.confidence is Confidence.FAILED
    assert result.candidates == ("101", "102")


@pytest.mark.parametrize(
    "filename_date",
    [
        {"date": "2024", "precision": "year"},
        {"date": None, "precision": "range", "range_years": [2020, 2024]},
        {"date": "2020-01-01", "precision": "day"},
    ],
)
def test_match_file_unsupported_precision_or_zero_candidates_fails(
    filename_date: dict[str, object],
) -> None:
    result = match_file(
        alio_record(filename_date=filename_date),
        [law_version("101")],
        is_latest_file=False,
    )

    assert result.confidence is Confidence.FAILED
    assert result.candidates == ()
    assert result.parse_eval_eligible is False


def test_run_filters_sorts_marks_duplicates_and_reports_summary(
    tmp_path: Path,
) -> None:
    law_records = [
        law_version("100", issued_date="2024-01-01"),
        law_version("101", issued_date="2024-02-01", is_current=True),
        law_version("200", rule_id="law-2", name="모호 규정", issued_date="2024-03-01"),
        law_version(
            "300", rule_id="law-3", name="모호ㆍ규정", issued_date="2024-03-01"
        ),
        law_version(
            "400", rule_id="law-4", name="짝 없는 규정", issued_date="2024-04-01"
        ),
    ]
    alio_records = [
        alio_record(
            rule_no="10",
            file_no="10",
            local_path="data/raw/alio/10/10__현행.hwpx",
            file_kind="hwpx",
            filename_date={"date": None, "precision": "none"},
            rule_last_revised="2024-02-01",
        ),
        alio_record(
            rule_no="10",
            file_no="8",
            local_path="data/raw/alio/10/8__zip/과거(2024년 1월 1일).hwp",
            source="alio_zip_member",
            zip_member_path="과거(2024년 1월 1일).hwp",
            filename_date={"date": "2024-01-01", "precision": "day"},
        ),
        alio_record(
            rule_no="10",
            file_no="8",
            file_kind="zip",
            filename_date={
                "date": None,
                "precision": "range",
                "range_years": [2020, 2024],
            },
        ),
        alio_record(rule_no="10", file_no="4", file_kind="hwp3"),
        alio_record(rule_no="10", file_no="3", file_kind="pdf"),
        alio_record(
            rule_no="10",
            file_no="2",
            local_path="data/raw/alio/10/2__과거.hwp",
            filename_date={"date": "2024-01-01", "precision": "day"},
        ),
        alio_record(
            rule_no="20",
            rule_title="모호 규정",
            file_no="1",
            local_path="data/raw/alio/20/1__모호.hwp",
            filename_date={"date": "2024-03-01", "precision": "day"},
        ),
    ]
    write_jsonl(tmp_path / "data/manifest/law_api.jsonl", law_records)
    write_jsonl(tmp_path / "data/manifest/alio.jsonl", alio_records)
    output_path = tmp_path / "data/manifest/version_matches.jsonl"
    output_path.write_text('{"stale": true}\n', encoding="utf-8")

    summary = run(tmp_path)

    records = read_jsonl(output_path)
    assert [
        (record["rule_no"], record["alio_file_no"], record["alio_local_path"])
        for record in records
    ] == [
        ("10", "2", "data/raw/alio/10/2__과거.hwp"),
        ("10", "8", "data/raw/alio/10/8__zip/과거(2024년 1월 1일).hwp"),
        ("10", "10", "data/raw/alio/10/10__현행.hwpx"),
        ("20", "1", "data/raw/alio/20/1__모호.hwp"),
    ]
    assert records[0]["law_serial"] == records[1]["law_serial"] == "100"
    assert "중복" in records[0]["basis"]
    assert "중복" in records[1]["basis"]
    assert records[2]["law_serial"] == "101"
    assert records[2]["confidence"] == "approximate"
    assert records[3]["confidence"] == "failed"
    assert records[3]["law_rule_id"] is None
    assert records[3]["candidates"] == ["200", "300"]
    assert len({record["matched_at"] for record in records}) == 1
    assert summary.confidence_counts == {
        "exact": 2,
        "approximate": 1,
        "failed": 1,
    }
    assert summary.excluded_file_kinds == {"hwp3": 1, "pdf": 1, "zip": 1}
    assert summary.rule_match_failures == ("모호 규정",)
    assert summary.unmatched_law_version_count == 3
    assert summary.current_matched_rule_count == 1


def test_main_reports_each_missing_manifest_and_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = matching.main(["--root", str(tmp_path)])

    assert exit_code != 0
    error = capsys.readouterr().err
    assert "make collect-law" in error
    assert "make collect-alio" in error


def test_run_ignores_duplicate_manifest_lines_and_uses_last_law_values(
    tmp_path: Path,
) -> None:
    """재수집으로 같은 키의 줄이 쌓여도 매칭 결과는 한 줄이고 마지막 줄이 이긴다."""
    law_records = [
        law_version("100", issued_date="2023-01-01"),
        law_version("100", issued_date="2024-01-01", is_current=True),
    ]
    alio = alio_record(
        file_no="2",
        local_path="data/raw/alio/10/2__규정.hwp",
        filename_date={"date": "2024-01-01", "precision": "day"},
    )
    member = alio_record(
        file_no="8",
        local_path="data/raw/alio/10/8__zip/과거.hwp",
        source="alio_zip_member",
        zip_member_path="과거.hwp",
        filename_date={"date": "2023-06-01", "precision": "day"},
    )
    write_jsonl(tmp_path / "data/manifest/law_api.jsonl", law_records)
    write_jsonl(
        tmp_path / "data/manifest/alio.jsonl",
        [alio, member, {**alio, "sha256": "b" * 64}, member],
    )

    summary = run(tmp_path)

    records = read_jsonl(tmp_path / "data/manifest/version_matches.jsonl")
    assert [(r["alio_file_no"], r["confidence"]) for r in records] == [
        ("2", "exact"),
        ("8", "failed"),
    ]
    assert records[0]["law_serial"] == "100"
    assert records[0]["law_issued_date"] == "2024-01-01"
    assert records[0]["law_is_current"] is True
    assert records[0]["alio_sha256"] == "b" * 64
    assert "중복" not in records[0]["basis"]
    assert summary.confidence_counts == {"exact": 1, "approximate": 0, "failed": 1}


CONFLICT_TITLE = "한국지능정보사회진흥원 공직자의 이해충돌 방지제도 운영지침"


def _conflict_law_records(*, second_has_current: bool) -> list[dict[str, object]]:
    """법제처에 같은 규정이 두 rule_id로 등록된 실측 형태."""
    return [
        law_version(
            "a-2022",
            rule_id="2126307",
            name="(한국지능정보사회진흥원) 공직자의 이해충돌 방지제도 운영지침",
            issued_date="2022-06-27",
        ),
        law_version(
            "a-2024",
            rule_id="2126307",
            name="(한국지능정보사회진흥원) 공직자의 이해충돌 방지제도 운영지침",
            issued_date="2024-09-23",
        ),
        law_version(
            "a-2026",
            rule_id="2126307",
            name="(한국지능정보사회진흥원) 공직자의 이해충돌 방지제도 운영지침",
            issued_date="2026-06-08",
            is_current=True,
        ),
        law_version(
            "b-2022",
            rule_id="2135703",
            name=CONFLICT_TITLE,
            issued_date="2022-06-27",
        ),
        law_version(
            "b-2024",
            rule_id="2135703",
            name=CONFLICT_TITLE,
            issued_date="2024-09-23",
            is_current=second_has_current,
        ),
    ]


def test_run_prefers_the_only_rule_id_with_a_current_version(tmp_path: Path) -> None:
    alio = alio_record(
        rule_no="48177",
        rule_title=CONFLICT_TITLE,
        file_no="5",
        local_path="data/raw/alio/48177/5__지침.hwp",
        filename_date={"date": "2024-09-23", "precision": "day"},
    )
    write_jsonl(
        tmp_path / "data/manifest/law_api.jsonl",
        _conflict_law_records(second_has_current=False),
    )
    write_jsonl(tmp_path / "data/manifest/alio.jsonl", [alio])

    summary = run(tmp_path)

    record = read_jsonl(tmp_path / "data/manifest/version_matches.jsonl")[0]
    assert record["law_rule_id"] == "2126307"
    assert record["law_serial"] == "a-2024"
    assert record["confidence"] == "exact"
    assert "현행 우선: 후보 rule_id 2126307, 2135703 중 현행 2126307" in record["basis"]
    assert summary.rule_match_failures == ()


def test_run_fails_when_more_than_one_candidate_rule_id_is_current(
    tmp_path: Path,
) -> None:
    alio = alio_record(
        rule_no="48177",
        rule_title=CONFLICT_TITLE,
        file_no="5",
        local_path="data/raw/alio/48177/5__지침.hwp",
        filename_date={"date": "2024-09-23", "precision": "day"},
    )
    write_jsonl(
        tmp_path / "data/manifest/law_api.jsonl",
        _conflict_law_records(second_has_current=True),
    )
    write_jsonl(tmp_path / "data/manifest/alio.jsonl", [alio])

    summary = run(tmp_path)

    record = read_jsonl(tmp_path / "data/manifest/version_matches.jsonl")[0]
    assert record["confidence"] == "failed"
    assert record["law_rule_id"] is None
    assert "모호" in record["basis"]
    assert summary.rule_match_failures == (CONFLICT_TITLE,)
