"""Collect unmodified NIA internal-rule source files from ALIO."""

import argparse
import json
import math
import re
import sys
import unicodedata
import warnings
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import BadZipFile, ZipFile

import httpx

from kodocs.ingest.filetype import FileKind, sniff, sniff_bytes
from kodocs.ingest.http import RateLimitedClient
from kodocs.ingest.manifest import (
    LICENSE_NOTE_NO_KOGL,
    append_record,
    load_records,
    now_iso,
    repo_relative,
    sha256_file,
)

ALIO_BASE_URL = "https://www.alio.go.kr"
NIA_APBA_ID = "C0392"
RULE_REPORT_FORM_ROOT_NO = "21110"

_LIST_URL = f"{ALIO_BASE_URL}/item/itemReportListSusi.json"
_DETAIL_URL = f"{ALIO_BASE_URL}/occasional/findRuleDtl.json"
_DOWNLOAD_URL = f"{ALIO_BASE_URL}/download/rulefiledown.json"
_BFILE_START = re.compile(r"^(\d+)\|")
_RANGE_DATE = re.compile(r"(?<!\d)(\d{4})\s*-\s*(\d{4})년(?:도)?(?:\s*개정)?")
_KOREAN_DAY_DATE = re.compile(r"(?<!\d)(\d{4})년(?:도)?\s*(\d{1,2})월\s*(\d{1,2})일")
_COMPACT_DAY_DATE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_DOTTED_DAY_DATE = re.compile(r"(?<!\d)(\d{2}|\d{4})\.(\d{1,2})\.(\d{1,2})(?!\d)")
_KOREAN_MONTH_DATE = re.compile(r"(?<!\d)(\d{4})년(?:도)?\s*(\d{1,2})월")
_KOREAN_YEAR_DATE = re.compile(r"(?<![\d-])(\d{4})년(?:도)?")
_RULE_DATE = re.compile(r"^(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})$")
_COMPACT_RULE_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_FAILURE_REQUIRED_FIELDS = frozenset(
    {"rule_no", "file_no", "file_name", "reason", "collected_at"}
)


class AlioError(RuntimeError):
    """ALIO returned an invalid or unsuccessful response."""


@dataclass(frozen=True)
class AlioFileRef:
    """A file reference as represented in an ALIO ``bFiles`` value."""

    file_no: str
    file_name: str


@dataclass(frozen=True)
class AlioRule:
    """One ALIO rule and every source-file version attached to it."""

    rule_no: str
    title: str
    category: str | None
    last_revised: str | None
    files: tuple[AlioFileRef, ...]


@dataclass(frozen=True)
class FilenameDate:
    """An explicitly written date fragment found in a source filename."""

    date: str | None
    precision: str
    range_years: tuple[int, int] | None
    raw: str | None


@dataclass(frozen=True)
class CollectSummary:
    """Counts from one ALIO collection run."""

    rule_count: int
    file_count: int
    file_kinds: dict[str, int]
    zip_member_count: int
    failure_count: int
    skipped_count: int


def parse_bfiles(value: str) -> tuple[AlioFileRef, ...]:
    """Parse ALIO's comma-delimited file references without losing name commas."""
    if not value.strip():
        return ()

    references: list[AlioFileRef] = []
    current_no: str | None = None
    current_name = ""
    for fragment in value.split(","):
        match = _BFILE_START.match(fragment)
        if match is not None:
            if current_no is not None:
                references.append(AlioFileRef(current_no, current_name))
            current_no = match.group(1)
            current_name = fragment[match.end() :]
        elif current_no is not None:
            current_name = f"{current_name},{fragment}"
        elif fragment.strip():
            raise AlioError("ALIO bFiles 값이 fileNo|파일명 형식이 아닙니다")

    if current_no is None:
        raise AlioError("ALIO bFiles 값에 파일 번호가 없습니다")
    references.append(AlioFileRef(current_no, current_name))
    if any(not reference.file_name for reference in references):
        raise AlioError("ALIO bFiles 값에 파일명이 없습니다")
    return tuple(references)


def parse_filename_date(file_name: str) -> FilenameDate:
    """Extract only date information explicitly present in an ALIO filename."""
    range_match = _RANGE_DATE.search(file_name)
    if range_match is not None:
        start, end = (int(value) for value in range_match.groups())
        if _valid_year(start) and _valid_year(end) and start <= end:
            return FilenameDate(
                date=None,
                precision="range",
                range_years=(start, end),
                raw=range_match.group(0),
            )
        return _no_filename_date()

    korean_day_match = _KOREAN_DAY_DATE.search(file_name)
    if korean_day_match is not None:
        year, month, day = (int(value) for value in korean_day_match.groups())
        return _day_filename_date(year, month, day, korean_day_match.group(0))

    compact_match = _COMPACT_DAY_DATE.search(file_name)
    if compact_match is not None:
        compact = compact_match.group(1)
        return _day_filename_date(
            int(compact[:4]),
            int(compact[4:6]),
            int(compact[6:]),
            compact_match.group(0),
        )

    dotted_match = _DOTTED_DAY_DATE.search(file_name)
    if dotted_match is not None:
        year_text, month_text, day_text = dotted_match.groups()
        year = int(year_text) + 2000 if len(year_text) == 2 else int(year_text)
        return _day_filename_date(
            year,
            int(month_text),
            int(day_text),
            dotted_match.group(0),
        )

    month_match = _KOREAN_MONTH_DATE.search(file_name)
    if month_match is not None:
        year, month = (int(value) for value in month_match.groups())
        try:
            date(year, month, 1)
        except ValueError:
            return _no_filename_date()
        return FilenameDate(
            date=f"{year:04d}-{month:02d}",
            precision="month",
            range_years=None,
            raw=month_match.group(0),
        )

    year_match = _KOREAN_YEAR_DATE.search(file_name)
    if year_match is not None:
        year = int(year_match.group(1))
        if not _valid_year(year):
            return _no_filename_date()
        return FilenameDate(
            date=f"{year:04d}",
            precision="year",
            range_years=None,
            raw=year_match.group(0),
        )
    return _no_filename_date()


class AlioClient:
    """Small adapter around the common rate-limited HTTP client."""

    def __init__(
        self,
        http: RateLimitedClient,
        *,
        apba_id: str = NIA_APBA_ID,
    ) -> None:
        self._http = http
        self._apba_id = apba_id

    def list_rule_nos(self) -> list[tuple[str, str]]:
        """Return every ``(rule_no, title)`` pair across all list pages."""
        rules: list[tuple[str, str]] = []
        page_no = 1
        while True:
            response = self._http.post_json(
                _LIST_URL,
                {
                    "pageNo": page_no,
                    "apbaId": self._apba_id,
                    "apbaType": "A2004",
                    "reportFormRootNo": RULE_REPORT_FORM_ROOT_NO,
                },
            )
            data = _response_data(response, "목록")
            page = _required_mapping(data, "page", "목록")
            total_pages = _non_negative_int(page.get("totalPage"), "totalPage")
            results = data.get("result", [])
            if not isinstance(results, list):
                raise AlioError("ALIO 목록 응답의 result가 배열이 아닙니다")
            for item in results:
                if not isinstance(item, Mapping):
                    raise AlioError("ALIO 목록 항목이 객체가 아닙니다")
                rule_no = _required_digits(item.get("idx"), "idx")
                title = _required_text(item.get("title"), "title")
                rules.append((rule_no, title))

            if page_no >= total_pages:
                break
            page_no += 1
        return rules

    def fetch_rule(self, rule_no: str) -> AlioRule:
        """Fetch and normalize one rule's detail metadata."""
        safe_rule_no = _required_digits(rule_no, "rule_no")
        response = self._http.get(_DETAIL_URL, params={"seq": safe_rule_no})
        data = _response_data(response, "상세")
        response_rule_no = _required_digits(data.get("ruleNo"), "ruleNo")
        if response_rule_no != safe_rule_no:
            raise AlioError("ALIO 상세 응답의 ruleNo가 요청값과 다릅니다")
        return AlioRule(
            rule_no=response_rule_no,
            title=_required_text(data.get("title"), "title"),
            category=_optional_text(data.get("insdRuleDivis")),
            last_revised=_normalize_rule_date(data.get("retryRvsnYmd")),
            files=parse_bfiles(str(data.get("bFiles") or "")),
        )

    def download(self, file_no: str) -> bytes:
        """Download the complete bytes for one ALIO file number."""
        safe_file_no = _required_digits(file_no, "file_no")
        response = self._http.get(_DOWNLOAD_URL, params={"fileNo": safe_file_no})
        return response.content


def extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Safely extract one plain zip, restoring legacy cp949 member names."""
    destination = dest_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with ZipFile(zip_path) as archive:
            for member in archive.infolist():
                member_name = _decode_zip_member_name(member.filename, member.flag_bits)
                normalized_name = member_name.replace("\\", "/")
                member_path = PurePosixPath(normalized_name)
                if member.is_dir() or normalized_name.endswith("/"):
                    continue
                if (
                    member_path.parts
                    and member_path.parts[0] == "__MACOSX"
                    or member_path.name == ".DS_Store"
                ):
                    continue
                if _unsafe_zip_member(member_name, member_path):
                    warnings.warn(
                        f"안전하지 않은 zip 멤버를 건너뜁니다: {member_name!r}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue

                target = destination.joinpath(*member_path.parts)
                try:
                    target.resolve().relative_to(destination)
                except ValueError:
                    warnings.warn(
                        f"안전하지 않은 zip 멤버를 건너뜁니다: {member_name!r}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                content = archive.read(member)
                if not target.is_file() or target.read_bytes() != content:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                extracted.append(target)
    except (BadZipFile, OSError) as exc:
        raise AlioError(f"ALIO zip 압축 해제 실패: {type(exc).__name__}") from exc
    return extracted


def collect(
    root: Path,
    client: AlioClient,
    *,
    limit_rules: int | None = None,
) -> CollectSummary:
    """Collect missing originals, expand plain zips, and append provenance."""
    if limit_rules is not None and limit_rules <= 0:
        raise ValueError("limit_rules must be positive")
    listed_rules = client.list_rule_nos()
    selected_rules = (
        listed_rules[:limit_rules] if limit_rules is not None else listed_rules
    )

    manifest_path = root / "data/manifest/alio.jsonl"
    failure_path = root / "data/manifest/alio_failures.jsonl"
    existing_records = load_records(manifest_path)
    existing_direct = {
        str(record.get("file_no")): record
        for record in existing_records
        if record.get("source") == "alio"
    }
    existing_members = {
        (str(record.get("file_no")), str(record.get("zip_member_path"))): record
        for record in existing_records
        if record.get("source") == "alio_zip_member"
    }

    file_kinds: Counter[str] = Counter()
    file_count = 0
    zip_member_count = 0
    failure_count = 0
    skipped_count = 0

    for rule_no, _listed_title in selected_rules:
        rule = client.fetch_rule(rule_no)
        for file_ref in rule.files:
            download_url = _download_url(file_ref.file_no)
            raw_path = (
                root
                / "data/raw/alio"
                / rule.rule_no
                / f"{file_ref.file_no}__{_safe_file_name(file_ref.file_name)}"
            )
            existing = existing_direct.get(file_ref.file_no)
            if _matches_existing_file(raw_path, existing):
                skipped_count += 1
                kind = sniff(raw_path)
            else:
                try:
                    content = client.download(file_ref.file_no)
                except (httpx.HTTPError, AlioError) as exc:
                    _append_failure(
                        failure_path,
                        rule,
                        file_ref,
                        f"download failed: {type(exc).__name__}: {exc}",
                    )
                    failure_count += 1
                    continue

                kind = sniff_bytes(content)
                if kind in {FileKind.HTML, FileKind.UNKNOWN}:
                    raw_path.unlink(missing_ok=True)
                    _append_failure(
                        failure_path,
                        rule,
                        file_ref,
                        f"invalid file kind: {kind.value}",
                    )
                    failure_count += 1
                    continue

                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(content)
                append_record(
                    manifest_path,
                    _file_record(
                        root=root,
                        path=raw_path,
                        source="alio",
                        source_url=download_url,
                        rule=rule,
                        file_ref=file_ref,
                        file_kind=kind,
                    ),
                )
                file_count += 1
                file_kinds[kind.value] += 1

            if kind is FileKind.ZIP:
                zip_dir = (
                    root / "data/raw/alio" / rule.rule_no / f"{file_ref.file_no}__zip"
                )
                for member_path in extract_zip(raw_path, zip_dir):
                    member_relative = member_path.relative_to(
                        zip_dir.resolve()
                    ).as_posix()
                    member_kind = sniff(member_path)
                    member_existing = existing_members.get(
                        (file_ref.file_no, member_relative)
                    )
                    if _matches_existing_file(member_path, member_existing):
                        continue
                    append_record(
                        manifest_path,
                        _file_record(
                            root=root,
                            path=member_path,
                            source="alio_zip_member",
                            source_url=download_url,
                            rule=rule,
                            file_ref=AlioFileRef(
                                file_no=file_ref.file_no,
                                file_name=member_relative,
                            ),
                            file_kind=member_kind,
                            parent_file_no=file_ref.file_no,
                            zip_member_path=member_relative,
                        ),
                    )
                    zip_member_count += 1

    summary = CollectSummary(
        rule_count=len(selected_rules),
        file_count=file_count,
        file_kinds=dict(file_kinds),
        zip_member_count=zip_member_count,
        failure_count=failure_count,
        skipped_count=skipped_count,
    )
    kinds_text = ", ".join(
        f"{kind} {count}개" for kind, count in sorted(summary.file_kinds.items())
    )
    print(
        f"완료: 규정 {summary.rule_count}개, 파일 {summary.file_count}개"
        f" ({kinds_text or '없음'}), zip 멤버 {summary.zip_member_count}개, "
        f"실패 {summary.failure_count}개, 건너뜀 {summary.skipped_count}개"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for collection initiated by the user."""
    parser = argparse.ArgumentParser(
        description="ALIO에서 NIA 내부규정 원본 파일 전 버전을 수집합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument("--limit-rules", type=_positive_int)
    args = parser.parse_args(argv)

    if not math.isfinite(args.min_interval) or args.min_interval < 0.5:
        print(
            "ALIO 공개 사이트 부하 방지를 위해 --min-interval은 0.5 이상이어야 합니다.",
            file=sys.stderr,
        )
        return 2

    try:
        with RateLimitedClient(min_interval_s=args.min_interval) as http:
            collect(
                args.root,
                AlioClient(http),
                limit_rules=args.limit_rules,
            )
    except (AlioError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"ALIO 수집 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _response_data(response: httpx.Response, operation: str) -> Mapping[object, object]:
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AlioError(f"ALIO {operation} 응답이 JSON이 아닙니다") from exc
    if not isinstance(payload, Mapping):
        raise AlioError(f"ALIO {operation} 응답의 최상위 값이 객체가 아닙니다")
    if payload.get("status") != "success":
        raise AlioError(f"ALIO {operation} 응답 status가 success가 아닙니다")
    return _required_mapping(payload, "data", operation)


def _required_mapping(
    source: Mapping[object, object],
    field: str,
    operation: str,
) -> Mapping[object, object]:
    value = source.get(field)
    if not isinstance(value, Mapping):
        raise AlioError(f"ALIO {operation} 응답에 {field} 객체가 없습니다")
    return value


def _required_digits(value: object, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text.isdigit():
        raise AlioError(f"ALIO 응답의 {field} 값이 숫자가 아닙니다")
    return text


def _required_text(value: object, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise AlioError(f"ALIO 응답에 {field} 값이 없습니다")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _non_negative_int(value: object, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise AlioError(f"ALIO 목록 응답의 {field} 값이 숫자가 아닙니다") from exc
    if parsed < 0:
        raise AlioError(f"ALIO 목록 응답의 {field} 값이 음수입니다")
    return parsed


def _normalize_rule_date(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    match = _RULE_DATE.fullmatch(text) or _COMPACT_RULE_DATE.fullmatch(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.isoformat()


def _day_filename_date(year: int, month: int, day: int, raw: str) -> FilenameDate:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return _no_filename_date()
    return FilenameDate(parsed.isoformat(), "day", None, raw)


def _no_filename_date() -> FilenameDate:
    return FilenameDate(None, "none", None, None)


def _valid_year(year: int) -> bool:
    try:
        date(year, 1, 1)
    except ValueError:
        return False
    return True


def _safe_file_name(file_name: str) -> str:
    safe = "".join(
        "_"
        if character in {"/", "\\"} or unicodedata.category(character) == "Cc"
        else character
        for character in file_name
    )
    return safe or "_"


def _decode_zip_member_name(file_name: str, flag_bits: int) -> str:
    if flag_bits & 0x800:
        return file_name
    try:
        return file_name.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return file_name


def _unsafe_zip_member(member_name: str, member_path: PurePosixPath) -> bool:
    windows_path = PureWindowsPath(member_name)
    return (
        member_path.is_absolute()
        or bool(windows_path.drive)
        or windows_path.is_absolute()
        or ".." in member_path.parts
        or ".." in PurePosixPath(member_name.replace("\\", "/")).parts
    )


def _file_record(
    *,
    root: Path,
    path: Path,
    source: str,
    source_url: str,
    rule: AlioRule,
    file_ref: AlioFileRef,
    file_kind: FileKind,
    parent_file_no: str | None = None,
    zip_member_path: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": source,
        "source_url": source_url,
        "local_path": repo_relative(path, root),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "collected_at": now_iso(),
        "license_note": LICENSE_NOTE_NO_KOGL,
        "rule_no": rule.rule_no,
        "rule_title": rule.title,
        "rule_category": rule.category,
        "rule_last_revised": rule.last_revised,
        "file_no": file_ref.file_no,
        "file_name": file_ref.file_name,
        "file_kind": file_kind.value,
        "filename_date": asdict(parse_filename_date(file_ref.file_name)),
    }
    if parent_file_no is not None:
        record["parent_file_no"] = parent_file_no
    if zip_member_path is not None:
        record["zip_member_path"] = zip_member_path
    return record


def _append_failure(
    failure_path: Path,
    rule: AlioRule,
    file_ref: AlioFileRef,
    reason: str,
) -> None:
    append_record(
        failure_path,
        {
            "rule_no": rule.rule_no,
            "file_no": file_ref.file_no,
            "file_name": file_ref.file_name,
            "reason": reason,
            "collected_at": now_iso(),
        },
        required=_FAILURE_REQUIRED_FIELDS,
    )


def _matches_existing_file(
    path: Path,
    record: Mapping[str, object] | None,
) -> bool:
    if record is None or not path.is_file():
        return False
    expected_hash = record.get("sha256")
    return isinstance(expected_hash, str) and sha256_file(path) == expected_hash


def _download_url(file_no: str) -> str:
    return f"{_DOWNLOAD_URL}?fileNo={file_no}"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("정수를 입력해야 합니다") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("1 이상의 정수를 입력해야 합니다")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
