"""Tests for collecting NIA internal-rule source files from ALIO."""

import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import httpx
import pytest
from fakes import HWP3_HEADER, hwp5_bytes, ole_other_bytes

from kodocs.ingest import alio
from kodocs.ingest.alio import (
    ALIO_BASE_URL,
    AlioClient,
    AlioFileRef,
    FilenameDate,
    collect,
    extract_zip,
    parse_bfiles,
    parse_filename_date,
)
from kodocs.ingest.http import RateLimitedClient


@dataclass
class FakeTime:
    """A deterministic clock for checking ALIO request spacing."""

    current: float = 100.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def make_http(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    fake_time: FakeTime | None = None,
) -> RateLimitedClient:
    clock = fake_time or FakeTime()
    return RateLimitedClient(
        min_interval_s=1.0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def list_payload(
    *rules: tuple[str, str],
    page: int = 1,
    total_pages: int = 1,
) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "page": {
                "totalPage": total_pages,
                "totalCount": len(rules),
                "currPage": page,
            },
            "result": [
                {
                    "idx": rule_no,
                    "title": title,
                    "apbaId": "C0392",
                    "tableName": "COMM_RULE",
                    "idxName": "RULE_NO",
                }
                for rule_no, title in rules
            ],
        },
    }


def detail_payload(
    rule_no: str,
    *,
    title: str = "테스트 규정",
    bfiles: str,
) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "ruleNo": int(rule_no),
            "title": title,
            "retryRvsnYmd": "2026.09.09",
            "insdRuleDivis": "인사·복무·징계",
            "pname": "한국지능정보사회진흥원",
            "bFiles": bfiles,
        },
    }


def hwpx_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "fake.hwpx"
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", "content")
    return path.read_bytes()


class Cp949ZipInfo(ZipInfo):
    """Write a member with cp949 bytes and no UTF-8 filename flag."""

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:
        return self.filename.encode("cp949"), self.flag_bits & ~0x800


def plain_zip_bytes(tmp_path: Path) -> bytes:
    """Build a zip whose Korean member uses raw cp949 without the UTF-8 flag."""
    nested_path = tmp_path / "fake-nested.zip"
    with ZipFile(nested_path, "w") as archive:
        archive.writestr("inner.hwp", HWP3_HEADER)

    path = tmp_path / "fake-bundle.zip"
    entries = [
        (Cp949ZipInfo("내규/과거 규정.hwp"), HWP3_HEADER),
        (ZipInfo("nested.zip"), nested_path.read_bytes()),
        (ZipInfo("../evil.hwp"), HWP3_HEADER),
        (ZipInfo("/absolute.hwp"), HWP3_HEADER),
        (ZipInfo("C:\\drive.hwp"), HWP3_HEADER),
        (ZipInfo("__MACOSX/ignored.hwp"), HWP3_HEADER),
        (ZipInfo(".DS_Store"), b"metadata"),
    ]
    with ZipFile(path, "w") as archive:
        for info, content in entries:
            archive.writestr(info, content)
    return path.read_bytes()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_parse_bfiles_preserves_commas_inside_file_names() -> None:
    value = (
        "16954|사택 운영지침(15.5.12).hwp,"
        "221628|행동강령(2016-2024년 개정, 별표 포함).zip,"
        "229512|사택 운영지침(2026년도 1월 12일 개정).hwpx"
    )

    assert parse_bfiles(value) == (
        AlioFileRef("16954", "사택 운영지침(15.5.12).hwp"),
        AlioFileRef("221628", "행동강령(2016-2024년 개정, 별표 포함).zip"),
        AlioFileRef("229512", "사택 운영지침(2026년도 1월 12일 개정).hwpx"),
    )


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        (
            "지침(2026년도 9월 9일 개정).hwpx",
            FilenameDate("2026-09-09", "day", None, "2026년도 9월 9일"),
        ),
        (
            "지침(2026년 9월 9일).hwp",
            FilenameDate("2026-09-09", "day", None, "2026년 9월 9일"),
        ),
        (
            "지침_20210316.hwp",
            FilenameDate("2021-03-16", "day", None, "20210316"),
        ),
        (
            "지침(20210316).hwp",
            FilenameDate("2021-03-16", "day", None, "20210316"),
        ),
        (
            "지침(15.5.12).hwp",
            FilenameDate("2015-05-12", "day", None, "15.5.12"),
        ),
        (
            "지침 2024.11.5.hwp",
            FilenameDate("2024-11-05", "day", None, "2024.11.5"),
        ),
        (
            "지침(2024년도 11월 개정).hwp",
            FilenameDate("2024-11", "month", None, "2024년도 11월"),
        ),
        (
            "지침(2016-2024년 개정).zip",
            FilenameDate(None, "range", (2016, 2024), "2016-2024년 개정"),
        ),
        (
            "지침(2024년 개정).hwp",
            FilenameDate("2024", "year", None, "2024년"),
        ),
        ("지침 최종본.hwp", FilenameDate(None, "none", None, None)),
        ("지침(2024년도 13월 개정).hwp", FilenameDate(None, "none", None, None)),
        ("지침(20241301).hwp", FilenameDate(None, "none", None, None)),
    ],
)
def test_parse_filename_date(file_name: str, expected: FilenameDate) -> None:
    assert parse_filename_date(file_name) == expected


def test_client_paginates_parses_detail_and_uses_rate_limit() -> None:
    fake_time = FakeTime()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("itemReportListSusi.json"):
            payload = json.loads(request.content)
            assert payload == {
                "pageNo": len([r for r in requests if r.method == "POST"]),
                "apbaId": "C0392",
                "apbaType": "A2004",
                "reportFormRootNo": "21110",
            }
            page = int(payload["pageNo"])
            return httpx.Response(
                200,
                json=list_payload(
                    (str(16537 + page), f"규정 {page}"),
                    page=page,
                    total_pages=2,
                ),
                request=request,
            )
        if request.url.path.endswith("findRuleDtl.json"):
            assert request.url.params["seq"] == "16538"
            return httpx.Response(
                200,
                json=detail_payload(
                    "16538",
                    title="사택 운영지침",
                    bfiles=(
                        "16954|사택 운영지침(15.5.12).hwp,"
                        "241269|사택 운영지침(2026년도 9월 9일 개정).hwpx"
                    ),
                ),
                request=request,
            )
        assert request.url.path.endswith("rulefiledown.json")
        assert request.url.params["fileNo"] == "16954"
        return httpx.Response(200, content=HWP3_HEADER, request=request)

    with make_http(handler, fake_time=fake_time) as http:
        client = AlioClient(http)
        rules = client.list_rule_nos()
        rule = client.fetch_rule("16538")
        content = client.download("16954")

    assert rules == [("16538", "규정 1"), ("16539", "규정 2")]
    assert rule.rule_no == "16538"
    assert rule.title == "사택 운영지침"
    assert rule.category == "인사·복무·징계"
    assert rule.last_revised == "2026-09-09"
    assert rule.files[0] == AlioFileRef("16954", "사택 운영지침(15.5.12).hwp")
    assert content == HWP3_HEADER
    assert fake_time.sleeps == [1.0, 1.0, 1.0]


def test_extract_zip_restores_cp949_and_blocks_unsafe_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.zip"
    archive_path.write_bytes(plain_zip_bytes(tmp_path))
    destination = tmp_path / "expanded"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        extracted = extract_zip(archive_path, destination)

    relative = {path.relative_to(destination).as_posix() for path in extracted}
    assert relative == {"내규/과거 규정.hwp", "nested.zip"}
    assert (destination / "내규/과거 규정.hwp").read_bytes() == HWP3_HEADER
    assert (destination / "nested.zip").is_file()
    assert not (tmp_path / "evil.hwp").exists()
    assert not (tmp_path / "absolute.hwp").exists()
    assert not (destination / "C:\\drive.hwp").exists()
    assert len(caught) == 3
    assert all("안전하지 않은 zip 멤버" in str(item.message) for item in caught)


def test_collect_records_formats_zip_members_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    downloads = {
        "1": HWP3_HEADER,
        "2": ole_other_bytes(),
        "3": hwpx_bytes(tmp_path),
        "4": plain_zip_bytes(tmp_path),
        "5": b"<html><body>download error</body></html>",
        "6": hwp5_bytes(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("itemReportListSusi.json"):
            return httpx.Response(
                200,
                json=list_payload(("16538", "사택 운영지침")),
                request=request,
            )
        if request.url.path.endswith("findRuleDtl.json"):
            return httpx.Response(
                200,
                json=detail_payload(
                    "16538",
                    title="사택 운영지침",
                    bfiles=(
                        "1|구 규정(15.5.12).hwp,"
                        "2|레거시 문서.doc,"
                        "3|확장자는 hwp.hwp,"
                        "4|취합본(2016-2024년 개정).zip,"
                        "5|오류 문서.hwp,"
                        "6|현행 규정(2026년 9월 9일).hwp"
                    ),
                ),
                request=request,
            )
        file_no = request.url.params["fileNo"]
        return httpx.Response(
            200,
            content=downloads[file_no],
            headers={"Content-Disposition": 'attachment; filename=""broken.hwp""'},
            request=request,
        )

    monkeypatch.chdir(tmp_path)
    with make_http(handler) as http, pytest.warns(RuntimeWarning, match="zip 멤버"):
        summary = collect(Path("."), AlioClient(http))

    records = read_jsonl(tmp_path / "data/manifest/alio.jsonl")
    direct = {
        record["file_no"]: record for record in records if record["source"] == "alio"
    }
    members = [record for record in records if record["source"] == "alio_zip_member"]
    failures = read_jsonl(tmp_path / "data/manifest/alio_failures.jsonl")

    assert direct["1"]["file_kind"] == "hwp3"
    assert direct["2"]["file_kind"] == "ole_other"
    assert direct["3"]["file_kind"] == "hwpx"
    assert direct["4"]["file_kind"] == "zip"
    assert direct["6"]["file_kind"] == "hwp5"
    assert direct["3"]["local_path"].endswith("3__확장자는 hwp.hwp")
    assert not (tmp_path / "data/raw/alio/16538/3__zip").exists()
    assert {record["zip_member_path"] for record in members} == {
        "내규/과거 규정.hwp",
        "nested.zip",
    }
    assert {record["file_kind"] for record in members} == {"hwp3", "zip"}
    assert all(record["parent_file_no"] == "4" for record in members)
    assert all(record["file_no"] == "4" for record in members)
    assert all(
        record["source_url"] == f"{ALIO_BASE_URL}/download/rulefiledown.json?fileNo=4"
        for record in members
    )
    assert failures[0]["file_no"] == "5"
    assert "html" in str(failures[0]["reason"])
    assert not any(
        "5__" in path.name for path in (tmp_path / "data/raw/alio/16538").iterdir()
    )
    assert summary.rule_count == 1
    assert summary.file_count == 5
    assert summary.file_kinds == {
        "hwp3": 1,
        "ole_other": 1,
        "hwpx": 1,
        "zip": 1,
        "hwp5": 1,
    }
    assert summary.zip_member_count == 2
    assert summary.failure_count == 1
    output = capsys.readouterr().out
    assert "규정 1개" in output
    assert "파일 5개" in output
    assert "zip 멤버 2개" in output
    assert "실패 1개" in output


def test_collect_is_idempotent_without_a_second_download(tmp_path: Path) -> None:
    download_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_requests
        if request.url.path.endswith("itemReportListSusi.json"):
            return httpx.Response(
                200,
                json=list_payload(("16538", "테스트 규정")),
                request=request,
            )
        if request.url.path.endswith("findRuleDtl.json"):
            return httpx.Response(
                200,
                json=detail_payload("16538", bfiles="101|원본/규정.hwp"),
                request=request,
            )
        download_requests += 1
        return httpx.Response(200, content=hwp5_bytes(), request=request)

    with make_http(handler) as http:
        client = AlioClient(http)
        first = collect(tmp_path, client)
        second = collect(tmp_path, client)

    assert download_requests == 1
    assert first.file_count == 1
    assert first.skipped_count == 0
    assert second.file_count == 0
    assert second.skipped_count == 1
    records = read_jsonl(tmp_path / "data/manifest/alio.jsonl")
    assert len(records) == 1
    assert records[0]["file_name"] == "원본/규정.hwp"
    assert records[0]["local_path"].endswith("101__원본_규정.hwp")


def test_collect_limit_rules_avoids_fetching_later_details(tmp_path: Path) -> None:
    detail_rule_nos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("itemReportListSusi.json"):
            return httpx.Response(
                200,
                json=list_payload(("1", "첫 규정"), ("2", "둘째 규정")),
                request=request,
            )
        if request.url.path.endswith("findRuleDtl.json"):
            rule_no = request.url.params["seq"]
            detail_rule_nos.append(rule_no)
            return httpx.Response(
                200,
                json=detail_payload(rule_no, title="첫 규정", bfiles=""),
                request=request,
            )
        raise AssertionError("empty rule must not trigger a download")

    with make_http(handler) as http:
        summary = collect(tmp_path, AlioClient(http), limit_rules=1)

    assert detail_rule_nos == ["1"]
    assert summary.rule_count == 1


def test_cli_rejects_too_short_interval_before_constructing_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    constructed = False

    def forbidden_http(**kwargs: object) -> None:
        nonlocal constructed
        constructed = True
        raise AssertionError(f"network client was constructed: {kwargs}")

    monkeypatch.setattr(alio, "RateLimitedClient", forbidden_http)

    exit_code = alio.main(["--min-interval", "0.49"])

    assert exit_code == 2
    assert constructed is False
    assert "0.5" in capsys.readouterr().err
