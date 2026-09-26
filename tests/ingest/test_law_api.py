"""Tests for collecting NIA rule versions from the Korean Law Open API."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from kodocs.config import load_settings
from kodocs.ingest import law_api
from kodocs.ingest.http import RateLimitedClient
from kodocs.ingest.law_api import (
    LAW_BASE_URL,
    NIA_AGENCY_NAME,
    LawApiAuthError,
    LawApiClient,
    LawApiError,
    RuleVersion,
    clean_body,
    collect,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEST_OC = "test-oc-secret"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict[str, object]:
    return json.loads(fixture_bytes(name))


def make_http(handler: Callable[[httpx.Request], httpx.Response]) -> RateLimitedClient:
    return RateLimitedClient(
        min_interval_s=0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


def search_response(*versions: dict[str, str]) -> dict[str, object]:
    return {
        "AdmRulSearch": {
            "resultCode": "00",
            "resultMsg": "success",
            "totalCnt": str(len(versions)),
            "page": "1",
            "target": "pi",
            "admrul": list(versions),
        }
    }


def version_item(serial: str, *, current: bool = True) -> dict[str, str]:
    return {
        "현행연혁구분": "현행" if current else "연혁",
        "행정규칙명": f"({NIA_AGENCY_NAME}) 테스트 규정 {serial}",
        "발령일자": "20241024",
        "시행일자": "20241101",
        "소관부처명": NIA_AGENCY_NAME,
        "제개정구분명": "일부개정",
        "행정규칙ID": "1001",
        "행정규칙일련번호": serial,
    }


def test_list_versions_paginates_normalizes_single_object_and_filters_agency() -> None:
    pages: list[str] = []
    requested_nw: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        nw = request.url.params["nw"]
        pages.append(page)
        requested_nw.append(nw)
        if nw == "2":
            content = fixture_bytes("law_search_history.json")
        elif page == "1":
            content = fixture_bytes("law_search_current_page1.json")
        else:
            content = fixture_bytes("law_search_current_page2.json")
        return httpx.Response(200, content=content, request=request)

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        current = client.list_versions(history=False)
        history = client.list_versions(history=True)

    assert pages == ["1", "2", "1"]
    assert requested_nw == ["1", "1", "2"]
    assert [item.serial for item in current] == ["2001", "2002"]
    assert current[0] == RuleVersion(
        rule_id="1001",
        serial="2001",
        name=f"({NIA_AGENCY_NAME}) 테스트 규정 A",
        issued_date="2024-10-24",
        effective_date="2024-11-01",
        is_current=True,
        revision_type="일부개정",
    )
    assert current[1].effective_date is None
    assert len(history) == 1
    assert history[0].serial == "2000"
    assert history[0].is_current is False


def test_referer_is_sent_on_every_list_and_body_request() -> None:
    received_referers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_referers.append(request.headers.get("Referer"))
        if request.url.path.endswith("lawSearch.do"):
            payload = search_response(version_item("2001"))
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(
            200,
            content=fixture_bytes("law_body_full.json"),
            request=request,
        )

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http, referer="https://registered.test/")
        client.list_versions(history=False)
        client.fetch_body("2001")

    assert received_referers == [
        "https://registered.test/",
        "https://registered.test/",
    ]


@pytest.mark.parametrize(
    ("content", "content_type", "operation"),
    [
        (
            "<html>사용자 정보 검증에 실패하였습니다</html>".encode(),
            "text/html; charset=utf-8",
            "body",
        ),
        (
            json.dumps(
                {
                    "AdmRulSearch": {
                        "resultCode": "99",
                        "resultMsg": "사용자 인증 검증에 실패하였습니다",
                    }
                },
                ensure_ascii=False,
            ).encode(),
            "application/json",
            "list",
        ),
    ],
)
def test_auth_failure_formats_raise_safe_guidance(
    content: bytes,
    content_type: str,
    operation: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": content_type},
            request=request,
        )

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        with pytest.raises(LawApiAuthError) as exc_info:
            if operation == "body":
                client.fetch_body("2001")
            else:
                client.list_versions(history=False)

    message = str(exc_info.value)
    assert "LAW_OPEN_API_KEY" in message
    assert "도메인/IP" in message
    assert "--referer" in message
    assert TEST_OC not in message


def test_collect_stops_after_first_body_auth_failure(tmp_path: Path) -> None:
    body_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal body_requests
        if request.url.path.endswith("lawSearch.do"):
            return httpx.Response(
                200,
                json=search_response(version_item("2001"), version_item("2002")),
                request=request,
            )
        body_requests += 1
        return httpx.Response(
            200,
            text="사용자 정보 검증에 실패하였습니다",
            request=request,
        )

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        with pytest.raises(LawApiAuthError):
            collect(tmp_path, client, include_history=False)

    assert body_requests == 1


def test_clean_body_removes_personal_data_and_normalizes_full_response() -> None:
    version = RuleVersion(
        rule_id="1001",
        serial="2001",
        name=f"({NIA_AGENCY_NAME}) 테스트 규정 A",
        issued_date="2024-10-24",
        effective_date="2024-11-01",
        is_current=True,
        revision_type="일부개정",
    )

    cleaned = clean_body(fixture_json("law_body_full.json"), version)
    serialized = json.dumps(cleaned, ensure_ascii=False)

    assert cleaned == {
        "rule_id": "1001",
        "serial": "2001",
        "name": f"({NIA_AGENCY_NAME}) 테스트 규정 A",
        "name_without_agency": "테스트 규정 A",
        "kind": "공단규정",
        "issued_date": "2024-10-24",
        "effective_date": "2024-11-01",
        "is_current": True,
        "revision_type": "일부개정",
        "articles": ["제1장 총칙", "제1조(목적) 이 규정은 테스트를 목적으로 한다."],
        "addenda": [
            {"date": "2021-11-26", "text": "부칙 <제1호, 2021.11.26.>"},
            {"date": "2024-10-24", "text": "부칙 <제2호, 2024.10.24.>"},
        ],
        "annexes": [
            {
                "key": "000100",
                "number": "0001",
                "branch": "00",
                "kind": "별지",
                "title": "테스트 서식",
                "file_link": "/LSW/flDownload.do?flSeq=123",
            }
        ],
    }
    assert "담당자명" not in serialized
    assert "전화번호" not in serialized
    assert "테스트담당자" not in serialized
    assert "000-0000-0000" not in serialized
    assert TEST_OC not in serialized


def test_clean_body_keeps_article_and_addendum_text_byte_identical() -> None:
    """정제는 담당자 필드만 뺀다. URL처럼 보이는 조문·부칙 텍스트를 바꾸지 않는다."""
    tricky_article = "제5조(정의) 다음 각 호?가&oc=나 & OC=다 Note: 기준?비율=1&oc"
    tricky_addendum = "부칙 <제3호> 시행?일&oc=2024 Note: 경과조치#1"
    raw = {
        "AdmRulService": {
            "행정규칙기본정보": {
                "행정규칙종류": "공단규정",
                "담당자명": "테스트담당자",
                "전화번호": "000-0000-0000",
            },
            "조문내용": [tricky_article],
            "부칙": {"부칙공포일자": ["20240101"], "부칙내용": [tricky_addendum]},
            "별표": {
                "별표단위": {
                    "별표제목": "서식",
                    "별표서식파일링크": f"/LSW/flDownload.do?OC={TEST_OC}&flSeq=789",
                }
            },
        }
    }
    version = RuleVersion(
        rule_id="1001",
        serial="2001",
        name=f"({NIA_AGENCY_NAME}) 테스트 규정",
        issued_date="2024-01-01",
        effective_date=None,
        is_current=True,
        revision_type="일부개정",
    )

    cleaned = clean_body(raw, version)
    serialized = json.dumps(cleaned, ensure_ascii=False)

    assert cleaned["articles"] == [tricky_article]
    assert cleaned["addenda"] == [{"date": "2024-01-01", "text": tricky_addendum}]
    assert cleaned["annexes"][0]["file_link"] == "/LSW/flDownload.do?flSeq=789"
    assert TEST_OC not in serialized
    assert "테스트담당자" not in serialized
    assert "000-0000-0000" not in serialized


def test_collect_fails_when_no_current_version_survives_agency_filter(
    tmp_path: Path,
) -> None:
    other_agency = {**version_item("2001"), "소관부처명": "다른기관"}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=search_response(other_agency), request=request)

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        with pytest.raises(LawApiError, match="현행 규정 0개"):
            collect(tmp_path, client, include_history=True)

    assert [request.url.params["nw"] for request in requests] == ["1"]
    assert not (tmp_path / "data/manifest/law_api.jsonl").exists()


@pytest.mark.parametrize(
    ("fixture_name", "article_count", "annex_count"),
    [
        ("law_body_scalar.json", 1, 1),
        ("law_body_no_annex.json", 1, 0),
    ],
)
def test_clean_body_normalizes_scalar_values_and_missing_annex(
    fixture_name: str,
    article_count: int,
    annex_count: int,
) -> None:
    version = RuleVersion(
        rule_id="1001",
        serial="2001",
        name=f"({NIA_AGENCY_NAME}) 단일 항목 규정",
        issued_date="2020-01-02",
        effective_date=None,
        is_current=False,
        revision_type="제정",
    )

    cleaned = clean_body(fixture_json(fixture_name), version)

    assert len(cleaned["articles"]) == article_count
    assert len(cleaned["annexes"]) == annex_count
    if fixture_name == "law_body_scalar.json":
        assert cleaned["addenda"] == [{"date": "2020-01-02", "text": "부칙 단일 항목"}]
        assert cleaned["annexes"][0]["title"] == "단일 별표"
    else:
        assert cleaned["addenda"] == []


def test_collect_preserves_raw_bytes_but_keeps_outputs_and_logs_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_requests = 0
    raw_body = fixture_bytes("law_body_full.json")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal body_requests
        if request.url.path.endswith("lawSearch.do"):
            return httpx.Response(
                200,
                json=search_response(version_item("2001")),
                request=request,
            )
        body_requests += 1
        return httpx.Response(200, content=raw_body, request=request)

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        first = collect(tmp_path, client, include_history=False)
        second = collect(tmp_path, client, include_history=False)

    raw_path = tmp_path / "data/raw/law/1001/2001.json"
    interim_path = tmp_path / "data/interim/law/1001/2001.json"
    manifest_path = tmp_path / "data/manifest/law_api.jsonl"
    interim_text = interim_path.read_text(encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out

    assert raw_path.read_bytes() == raw_body
    assert "담당자명" in raw_path.read_text(encoding="utf-8")
    assert "테스트담당자" in raw_path.read_text(encoding="utf-8")
    for safe_text in (interim_text, manifest_text, output):
        assert "담당자명" not in safe_text
        assert "전화번호" not in safe_text
        assert "테스트담당자" not in safe_text
        assert "000-0000-0000" not in safe_text
        assert TEST_OC not in safe_text
    assert "OC=" not in interim_text
    assert "OC=" not in manifest_text
    assert body_requests == 1
    assert first.downloaded_count == 1
    assert first.skipped_count == 0
    assert second.downloaded_count == 0
    assert second.skipped_count == 1
    assert "현행 버전 1개" in output
    assert "연혁 버전 0개" in output
    assert "규정 1개" in output

    records = [json.loads(line) for line in manifest_text.splitlines()]
    assert len(records) == 1
    assert records[0]["source"] == "law_api"
    assert records[0]["serial"] == "2001"
    assert records[0]["article_count"] == 2
    assert records[0]["annex_count"] == 1
    assert records[0]["annex_titles"] == ["테스트 서식"]
    assert records[0]["source_url"] == (
        f"{LAW_BASE_URL}/DRF/lawService.do?target=pi&ID=2001&type=JSON"
    )


def test_non_json_non_auth_error_reports_safe_excerpt() -> None:
    response_text = f"broken response OC={TEST_OC} body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=response_text, request=request)

    with make_http(handler) as http:
        client = LawApiClient(TEST_OC, http)
        with pytest.raises(LawApiError) as exc_info:
            client.fetch_body("2001")

    message = str(exc_info.value)
    assert "502" in message
    assert "broken response" in message
    assert TEST_OC not in message


def test_cli_without_key_fails_before_constructing_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    network_constructed = False

    def forbidden_http(**kwargs: object) -> None:
        nonlocal network_constructed
        network_constructed = True
        raise AssertionError(f"network client was constructed: {kwargs}")

    monkeypatch.setattr(law_api, "load_settings", lambda: load_settings({}))
    monkeypatch.setattr(law_api, "RateLimitedClient", forbidden_http)

    exit_code = law_api.main(["--root", str(tmp_path)])

    assert exit_code != 0
    assert network_constructed is False
    assert "LAW_OPEN_API_KEY" in capsys.readouterr().err
