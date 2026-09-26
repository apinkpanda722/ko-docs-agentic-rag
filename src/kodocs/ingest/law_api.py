"""Collect NIA rule versions from the Korean Law Open API."""

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import httpx

from kodocs.config import MissingAPIKeyError, load_settings
from kodocs.ingest.http import RateLimitedClient
from kodocs.ingest.manifest import (
    LICENSE_NOTE_NO_KOGL,
    append_record,
    load_records,
    now_iso,
    repo_relative,
    sanitize_url,
    sha256_file,
)

LAW_BASE_URL = "https://www.law.go.kr"
NIA_AGENCY_NAME = "한국지능정보사회진흥원"
PERSONAL_FIELDS: frozenset[str] = frozenset({"담당자명", "전화번호"})

_SEARCH_URL = f"{LAW_BASE_URL}/DRF/lawSearch.do"
_SERVICE_URL = f"{LAW_BASE_URL}/DRF/lawService.do"
_DISPLAY_SIZE = 100
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPACT_DATE_PATTERN = re.compile(r"^\d{8}$")
_SECRET_PARAMETER_PATTERN = re.compile(r"(?i)(?:[?&]?OC\s*=\s*)[^\s&<>\"']+")


class LawApiError(RuntimeError):
    """The Law Open API returned an invalid or unsuccessful response."""


class LawApiAuthError(LawApiError):
    """인증값·등록 도메인/IP·Referer 확인이 필요한 인증 실패."""


@dataclass(frozen=True)
class RuleVersion:
    """One current or historical version of an administrative rule."""

    rule_id: str
    serial: str
    name: str
    issued_date: str
    effective_date: str | None
    is_current: bool
    revision_type: str


@dataclass(frozen=True)
class CollectSummary:
    """Counts from one collection run."""

    current_version_count: int
    history_version_count: int
    rule_count: int
    downloaded_count: int
    skipped_count: int


class LawApiClient:
    """Small adapter around the common rate-limited HTTP client."""

    def __init__(
        self,
        oc: str,
        http: RateLimitedClient,
        *,
        referer: str | None = None,
    ) -> None:
        self._oc = oc
        self._http = http
        self._headers = {"Referer": referer} if referer is not None else None
        self._body_bytes: dict[str, bytes] = {}
        self._body_urls: dict[str, str] = {}

    def list_versions(
        self,
        agency: str = NIA_AGENCY_NAME,
        *,
        history: bool,
    ) -> list[RuleVersion]:
        """List every current or historical page, filtering by exact agency."""
        versions: list[RuleVersion] = []
        page = 1

        while True:
            response = self._get(
                _SEARCH_URL,
                params={
                    "OC": self._oc,
                    "target": "pi",
                    "type": "JSON",
                    "query": agency,
                    "nw": "2" if history else "1",
                    "display": str(_DISPLAY_SIZE),
                    "page": str(page),
                },
            )
            payload = self._decode_json(response)
            search = payload.get("AdmRulSearch")
            if not isinstance(search, Mapping):
                if _json_contains_auth_failure(payload):
                    raise LawApiAuthError(_auth_guidance())
                raise LawApiError("법제처 목록 API 응답에 AdmRulSearch가 없습니다")

            result_code = str(search.get("resultCode", ""))
            result_message = str(search.get("resultMsg", ""))
            if result_code != "00":
                if _is_auth_failure_text(result_message) or _json_contains_auth_failure(
                    search
                ):
                    raise LawApiAuthError(_auth_guidance())
                raise LawApiError(
                    f"법제처 목록 API 오류(resultCode={result_code or '없음'})"
                )
            if response.status_code >= 400:
                raise LawApiError(f"법제처 목록 API 오류(HTTP {response.status_code})")

            items = _mapping_list(search.get("admrul"))
            for item in items:
                if str(item.get("소관부처명", "")) != agency:
                    continue
                versions.append(_rule_version(item))

            total_count = _non_negative_int(search.get("totalCnt"), "totalCnt")
            if page * _DISPLAY_SIZE >= total_count or not items:
                break
            page += 1

        return versions

    def fetch_body(self, serial: str) -> dict:
        """Fetch one body as its original JSON object."""
        response = self._get(
            _SERVICE_URL,
            params={
                "OC": self._oc,
                "target": "pi",
                "ID": serial,
                "type": "JSON",
            },
        )
        payload = self._decode_json(response)
        service = payload.get("AdmRulService")
        if not isinstance(service, Mapping):
            if _json_contains_auth_failure(payload):
                raise LawApiAuthError(_auth_guidance())
            raise LawApiError("법제처 본문 API 응답에 AdmRulService가 없습니다")
        result_code = service.get("resultCode")
        result_message = str(service.get("resultMsg", ""))
        if result_code is not None and str(result_code) != "00":
            if _json_contains_auth_failure(service):
                raise LawApiAuthError(_auth_guidance())
            raise LawApiError(
                f"법제처 본문 API 오류(resultCode={str(result_code) or '없음'})"
            )
        if _is_auth_failure_text(result_message):
            raise LawApiAuthError(_auth_guidance())
        if response.status_code >= 400:
            raise LawApiError(f"법제처 본문 API 오류(HTTP {response.status_code})")

        self._body_bytes[serial] = response.content
        self._body_urls[serial] = sanitize_url(str(response.request.url))
        return payload

    def body_bytes(self, serial: str) -> bytes:
        """Return the exact bytes from the most recent successful body request."""
        try:
            return self._body_bytes[serial]
        except KeyError as exc:
            raise LawApiError(f"본문 원본 바이트가 없습니다(serial={serial})") from exc

    def body_source_url(self, serial: str) -> str:
        """Return the sanitized URL from the most recent body request."""
        try:
            return self._body_urls[serial]
        except KeyError as exc:
            raise LawApiError(f"본문 요청 URL이 없습니다(serial={serial})") from exc

    def _get(self, url: str, *, params: Mapping[str, str]) -> httpx.Response:
        try:
            return self._http.get(url, params=params, headers=self._headers)
        except httpx.HTTPStatusError as exc:
            return exc.response
        except httpx.RequestError as exc:
            raise LawApiError(
                f"법제처 Open API 요청 실패: {type(exc).__name__}"
            ) from exc

    def _decode_json(self, response: httpx.Response) -> dict[str, object]:
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            text = response.text
            if "검증" in text or "인증" in text:
                raise LawApiAuthError(_auth_guidance()) from exc
            excerpt = _safe_excerpt(text, self._oc)
            raise LawApiError(
                f"법제처 API 비JSON 응답(HTTP {response.status_code}): {excerpt}"
            ) from exc
        if not isinstance(payload, dict):
            raise LawApiError("법제처 API JSON 최상위 값이 객체가 아닙니다")
        return payload


def clean_body(raw: dict, version: RuleVersion) -> dict:
    """Build a normalized body without personal fields or secret URLs.

    Article and addendum text is copied verbatim. The only link in the output is
    ``annexes[].file_link``, which is sanitized explicitly.
    """
    sanitized_raw = _sanitize_body_value(raw)
    if not isinstance(sanitized_raw, Mapping):
        raise LawApiError("정제할 응답의 최상위 값이 객체가 아닙니다")
    service = sanitized_raw.get("AdmRulService")
    if not isinstance(service, Mapping):
        raise LawApiError("정제할 응답에 AdmRulService가 없습니다")

    basic_value = service.get("행정규칙기본정보")
    basic = basic_value if isinstance(basic_value, Mapping) else {}
    articles = _string_list(service.get("조문내용"))

    addenda_value = service.get("부칙")
    addenda_source = addenda_value if isinstance(addenda_value, Mapping) else {}
    addenda_dates = _string_list(addenda_source.get("부칙공포일자"))
    addenda_texts = _string_list(addenda_source.get("부칙내용"))
    addenda = [
        {
            "date": _format_optional_date(date),
            "text": text,
        }
        for date, text in zip_longest(addenda_dates, addenda_texts, fillvalue="")
    ]

    annexes = []
    for annex_source in _mapping_list(service.get("별표")):
        for unit in _mapping_list(annex_source.get("별표단위")):
            link = str(unit.get("별표서식파일링크", ""))
            annexes.append(
                {
                    "key": str(unit.get("별표키", "")),
                    "number": str(unit.get("별표번호", "")),
                    "branch": str(unit.get("별표가지번호", "")),
                    "kind": str(unit.get("별표구분", "")),
                    "title": str(unit.get("별표제목", "")),
                    "file_link": sanitize_url(link),
                }
            )

    agency_prefix = f"({NIA_AGENCY_NAME}) "
    return {
        "rule_id": version.rule_id,
        "serial": version.serial,
        "name": version.name,
        "name_without_agency": version.name.removeprefix(agency_prefix),
        "kind": str(basic.get("행정규칙종류", "")),
        "issued_date": version.issued_date,
        "effective_date": version.effective_date,
        "is_current": version.is_current,
        "revision_type": version.revision_type,
        "articles": articles,
        "addenda": addenda,
        "annexes": annexes,
    }


def collect(
    root: Path,
    client: LawApiClient,
    *,
    include_history: bool = True,
) -> CollectSummary:
    """Collect missing body versions and append their provenance records."""
    current_versions = client.list_versions(history=False)
    if not current_versions:
        raise LawApiError("현행 규정 0개 — 소관부처명 필터·응답 필드 확인")
    history_versions = client.list_versions(history=True) if include_history else []
    all_versions = _unique_versions([*current_versions, *history_versions])
    rule_count = len({version.rule_id for version in all_versions})

    print(
        "목록: "
        f"현행 버전 {len(current_versions)}개, "
        f"연혁 버전 {len(history_versions)}개, "
        f"규정 {rule_count}개"
    )

    manifest_path = root / "data/manifest/law_api.jsonl"
    existing = {
        str(record.get("serial")): record for record in load_records(manifest_path)
    }
    downloaded_count = 0
    skipped_count = 0

    for version in all_versions:
        raw_path = root / f"data/raw/law/{version.rule_id}/{version.serial}.json"
        record = existing.get(version.serial)
        if _matches_existing_raw(raw_path, record):
            skipped_count += 1
            continue

        raw = client.fetch_body(version.serial)
        raw_bytes = client.body_bytes(version.serial)
        cleaned = clean_body(raw, version)
        interim_path = (
            root / f"data/interim/law/{version.rule_id}/{version.serial}.json"
        )

        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw_bytes)
        interim_path.parent.mkdir(parents=True, exist_ok=True)
        interim_path.write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        annexes = cleaned["annexes"]
        append_record(
            manifest_path,
            {
                "source": "law_api",
                "source_url": client.body_source_url(version.serial),
                "local_path": repo_relative(raw_path, root),
                "sha256": sha256_file(raw_path),
                "size_bytes": len(raw_bytes),
                "collected_at": now_iso(),
                "license_note": LICENSE_NOTE_NO_KOGL,
                "rule_id": version.rule_id,
                "serial": version.serial,
                "name": version.name,
                "issued_date": version.issued_date,
                "effective_date": version.effective_date,
                "is_current": version.is_current,
                "revision_type": version.revision_type,
                "article_count": len(cleaned["articles"]),
                "annex_count": len(annexes),
                "annex_titles": [annex["title"] for annex in annexes],
                "interim_path": repo_relative(interim_path, root),
                "interim_sha256": sha256_file(interim_path),
            },
        )
        downloaded_count += 1

    print(f"본문: 수집 {downloaded_count}개, 건너뜀 {skipped_count}개")
    return CollectSummary(
        current_version_count=len(current_versions),
        history_version_count=len(history_versions),
        rule_count=rule_count,
        downloaded_count=downloaded_count,
        skipped_count=skipped_count,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for collection initiated by the user."""
    parser = argparse.ArgumentParser(
        description="법제처 Open API에서 NIA 규정 현행·연혁 본문을 수집합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="현행 버전만 수집합니다.",
    )
    parser.add_argument(
        "--referer",
        help="법제처 Open API 신청 시 등록한 Referer URL입니다.",
    )
    parser.add_argument("--min-interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    try:
        oc = load_settings().require_key("LAW_OPEN_API_KEY")
    except MissingAPIKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        with RateLimitedClient(min_interval_s=args.min_interval) as http:
            client = LawApiClient(oc, http, referer=args.referer)
            collect(args.root, client, include_history=not args.no_history)
    except LawApiAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LawApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _rule_version(item: Mapping[object, object]) -> RuleVersion:
    return RuleVersion(
        rule_id=_required_string(item, "행정규칙ID"),
        serial=_required_string(item, "행정규칙일련번호"),
        name=_required_string(item, "행정규칙명"),
        issued_date=_format_required_date(item.get("발령일자"), "발령일자"),
        effective_date=_format_optional_date(item.get("시행일자")),
        is_current=str(item.get("현행연혁구분", "")) == "현행",
        revision_type=str(item.get("제개정구분명", "")),
    )


def _required_string(item: Mapping[object, object], field: str) -> str:
    value = str(item.get(field, "")).strip()
    if not value:
        raise LawApiError(f"법제처 목록 항목에 {field} 값이 없습니다")
    return value


def _format_required_date(value: object, field: str) -> str:
    formatted = _format_optional_date(value)
    if formatted is None:
        raise LawApiError(f"법제처 목록 항목에 {field} 값이 없습니다")
    return formatted


def _format_optional_date(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _COMPACT_DATE_PATTERN.fullmatch(text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if _DATE_PATTERN.fullmatch(text):
        return text
    raise LawApiError(f"법제처 날짜 형식이 올바르지 않습니다: {text!r}")


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _mapping_list(value: object) -> list[Mapping[object, object]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    raise LawApiError("법제처 API 목록 값이 객체 또는 배열이 아닙니다")


def _sanitize_body_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_body_value(nested)
            for key, nested in value.items()
            if str(key) not in PERSONAL_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize_body_value(nested) for nested in value]
    return value


def _non_negative_int(value: object, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise LawApiError(f"법제처 목록 응답의 {field} 값이 숫자가 아닙니다") from exc
    if parsed < 0:
        raise LawApiError(f"법제처 목록 응답의 {field} 값이 음수입니다")
    return parsed


def _unique_versions(versions: list[RuleVersion]) -> list[RuleVersion]:
    unique: dict[str, RuleVersion] = {}
    for version in versions:
        unique.setdefault(version.serial, version)
    return list(unique.values())


def _matches_existing_raw(
    raw_path: Path,
    record: Mapping[str, object] | None,
) -> bool:
    if record is None or not raw_path.is_file():
        return False
    expected_hash = record.get("sha256")
    return isinstance(expected_hash, str) and sha256_file(raw_path) == expected_hash


def _is_auth_failure_text(text: str) -> bool:
    return ("검증" in text or "인증" in text) and (
        "실패" in text
        or "오류" in text
        or "유효" in text
        or "올바르" in text
        or "필요" in text
    )


def _json_contains_auth_failure(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_json_contains_auth_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains_auth_failure(item) for item in value)
    return isinstance(value, str) and _is_auth_failure_text(value)


def _auth_guidance() -> str:
    return (
        "법제처 Open API 인증 실패: 인증값(LAW_OPEN_API_KEY), 신청 시 등록한 "
        "도메인/IP, --referer URL(Referer 헤더)을 확인하세요."
    )


def _safe_excerpt(text: str, oc: str) -> str:
    redacted = text.replace(oc, "[비밀 제거]") if oc else text
    redacted = _SECRET_PARAMETER_PATTERN.sub("[비밀 매개변수 제거]", redacted)
    return redacted[:200]


if __name__ == "__main__":
    raise SystemExit(main())
