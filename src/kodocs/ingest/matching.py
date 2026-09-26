"""Deterministically match ALIO source files to Law Open API versions."""

import argparse
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path

from kodocs.ingest.law_api import NIA_AGENCY_NAME
from kodocs.ingest.manifest import append_record, load_records, now_iso

_MATCHABLE_FILE_KINDS = frozenset({"hwp5", "hwpx"})
_MATCH_REQUIRED_FIELDS = frozenset(
    {
        "alio_file_no",
        "alio_local_path",
        "alio_sha256",
        "rule_no",
        "law_rule_id",
        "law_serial",
        "law_issued_date",
        "law_is_current",
        "confidence",
        "basis",
        "candidates",
        "parse_eval_eligible",
        "matched_at",
    }
)
_MIDDLE_DOTS = str.maketrans("", "", "·ㆍ・")


class Confidence(StrEnum):
    """Confidence assigned to one ALIO file-to-law-version match."""

    EXACT = "exact"
    APPROXIMATE = "approximate"
    FAILED = "failed"


@dataclass(frozen=True)
class VersionMatch:
    """One ALIO file's match result and the evidence used to reach it."""

    alio_file_no: str
    alio_local_path: str
    alio_sha256: str
    rule_no: str
    law_rule_id: str | None
    law_serial: str | None
    law_issued_date: str | None
    law_is_current: bool | None
    confidence: Confidence
    basis: str
    candidates: tuple[str, ...]
    parse_eval_eligible: bool


@dataclass(frozen=True)
class MatchSummary:
    """Counts and audit lists from one manifest-only matching run."""

    confidence_counts: dict[str, int]
    excluded_file_kinds: dict[str, int]
    rule_match_failures: tuple[str, ...]
    unmatched_law_version_count: int
    current_matched_rule_count: int


class MatchingError(RuntimeError):
    """Input manifests are unavailable or cannot support matching."""


def normalize_rule_name(name: str) -> str:
    """Remove the NIA prefix, whitespace, and middle-dot variants."""
    normalized = "".join(name.split())
    parenthesized_prefix = f"({NIA_AGENCY_NAME})"
    if normalized.startswith(parenthesized_prefix):
        normalized = normalized[len(parenthesized_prefix) :]
    elif normalized.startswith(NIA_AGENCY_NAME):
        normalized = normalized[len(NIA_AGENCY_NAME) :]
    return normalized.translate(_MIDDLE_DOTS)


def match_rules(
    alio_records: Iterable[Mapping[str, object]],
    law_records: Iterable[Mapping[str, object]],
) -> dict[str, str | None]:
    """Match each ALIO rule number only when its normalized name is unambiguous."""
    law_ids_by_name = _law_ids_by_name(law_records)
    titles_by_rule = _alio_titles_by_rule(alio_records)
    matches: dict[str, str | None] = {}
    for rule_no, title in titles_by_rule.items():
        candidates = law_ids_by_name.get(normalize_rule_name(title), set())
        matches[rule_no] = next(iter(candidates)) if len(candidates) == 1 else None
    return matches


def match_file(
    alio_record: Mapping[str, object],
    law_versions_of_rule: Iterable[Mapping[str, object]],
    *,
    is_latest_file: bool,
    approx_days: int = 7,
) -> VersionMatch:
    """Match one eligible ALIO file by the ordered ADR-022 date rules."""
    if approx_days < 0:
        raise ValueError("approx_days must be non-negative")

    versions = _unique_law_versions(law_versions_of_rule)
    filename_date = alio_record.get("filename_date")
    date_metadata = filename_date if isinstance(filename_date, Mapping) else {}
    precision = _string(date_metadata.get("precision")) or "none"
    date_text = _optional_string(date_metadata.get("date"))

    if precision == "day" and date_text is not None:
        exact = _versions_on_date(versions, date_text)
        if exact:
            return _resolve_candidates(
                alio_record,
                exact,
                confidence=Confidence.EXACT,
                success_basis=lambda version: (
                    f"filename day {date_text} == "
                    f"{_matching_date_kinds(version, date_text)}"
                ),
                failure_basis=(
                    f"filename day {date_text}와 같은 발령일자·시행일자 후보 "
                    f"{len(exact)}개"
                ),
            )

        approximate = _versions_within_days(versions, date_text, approx_days)
        if approximate:
            return _resolve_candidates(
                alio_record,
                approximate,
                confidence=Confidence.APPROXIMATE,
                success_basis=lambda version: (
                    f"filename day {date_text}가 발령일자·시행일자의 "
                    f"±{approx_days}일 범위"
                ),
                failure_basis=(
                    f"filename day {date_text}의 ±{approx_days}일 후보 "
                    f"{len(approximate)}개"
                ),
            )
        return _failed_match(
            alio_record,
            f"filename day {date_text}에 대응하는 법제처 버전 후보 0개",
        )

    if precision == "month" and date_text is not None:
        candidates = [
            version
            for version in versions
            if _optional_string(version.get("issued_date")) is not None
            and _string(version.get("issued_date")).startswith(f"{date_text}-")
        ]
        if candidates:
            return _resolve_candidates(
                alio_record,
                candidates,
                confidence=Confidence.APPROXIMATE,
                success_basis=lambda version: (
                    f"filename month {date_text}에 발령된 버전 1개"
                ),
                failure_basis=(
                    f"filename month {date_text}에 발령된 후보 {len(candidates)}개"
                ),
            )
        return _failed_match(
            alio_record,
            f"filename month {date_text}에 발령된 법제처 버전 후보 0개",
        )

    if precision == "none":
        if not is_latest_file:
            return _failed_match(
                alio_record,
                "파일명 날짜가 없고 같은 규정의 가장 최근 직접 파일이 아님",
            )
        revised_date = _optional_string(alio_record.get("rule_last_revised"))
        if revised_date is None:
            return _failed_match(
                alio_record,
                "파일명 날짜와 ALIO retryRvsnYmd가 모두 없음",
            )
        candidates = _versions_on_date(versions, revised_date)
        if candidates:
            return _resolve_candidates(
                alio_record,
                candidates,
                confidence=Confidence.APPROXIMATE,
                success_basis=lambda version: (
                    f"retryRvsnYmd {revised_date} == "
                    f"{_matching_date_kinds(version, revised_date)} "
                    "(날짜 없는 최신 직접 파일)"
                ),
                failure_basis=(
                    f"retryRvsnYmd {revised_date}와 같은 발령일자·시행일자 "
                    f"후보 {len(candidates)}개"
                ),
            )
        return _failed_match(
            alio_record,
            f"retryRvsnYmd {revised_date}에 대응하는 법제처 버전 후보 0개",
        )

    return _failed_match(
        alio_record,
        f"파일명 날짜 정밀도 {precision!r}는 매칭 대상이 아님",
    )


def run(root: Path, *, approx_days: int = 7) -> MatchSummary:
    """Regenerate the version-match manifest from the two local manifests."""
    if approx_days < 0:
        raise ValueError("approx_days must be non-negative")

    law_manifest = root / "data/manifest/law_api.jsonl"
    alio_manifest = root / "data/manifest/alio.jsonl"
    missing_guidance = []
    if not law_manifest.is_file():
        missing_guidance.append(
            "data/manifest/law_api.jsonl이 없습니다. 먼저 make collect-law를 실행하세요."
        )
    if not alio_manifest.is_file():
        missing_guidance.append(
            "data/manifest/alio.jsonl이 없습니다. 먼저 make collect-alio를 실행하세요."
        )
    if missing_guidance:
        raise MatchingError(" ".join(missing_guidance))

    law_records = load_records(law_manifest)
    alio_records = load_records(alio_manifest)
    rule_matches = match_rules(alio_records, law_records)
    titles_by_rule = _alio_titles_by_rule(alio_records)
    law_by_rule_id: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    law_by_name: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in law_records:
        law_by_rule_id[_string(record.get("rule_id"))].append(record)
        law_by_name[normalize_rule_name(_string(record.get("name")))].append(record)

    latest_direct_file_nos = _latest_direct_file_nos(alio_records)
    excluded = Counter(
        _string(record.get("file_kind")) or "missing"
        for record in alio_records
        if _string(record.get("file_kind")) not in _MATCHABLE_FILE_KINDS
    )
    target_records = sorted(
        (
            record
            for record in alio_records
            if _string(record.get("file_kind")) in _MATCHABLE_FILE_KINDS
        ),
        key=_alio_sort_key,
    )

    matches: list[VersionMatch] = []
    for alio in target_records:
        rule_no = _string(alio.get("rule_no"))
        law_rule_id = rule_matches.get(rule_no)
        if law_rule_id is None:
            title = titles_by_rule.get(rule_no, _string(alio.get("rule_title")))
            normalized_title = normalize_rule_name(title)
            candidate_versions = law_by_name.get(normalized_title, [])
            candidate_rule_ids = {
                _string(record.get("rule_id")) for record in candidate_versions
            }
            if candidate_rule_ids:
                basis = (
                    "규정명 정규화가 "
                    f"{len(candidate_rule_ids)}개 법제처 rule_id와 일치해 모호함: "
                    f"{title}"
                )
            else:
                basis = f"정규화 규정명과 일치하는 법제처 규정 0개: {title}"
            matches.append(
                _failed_match(
                    alio,
                    basis,
                    candidates=_candidate_serials(candidate_versions),
                )
            )
            continue

        is_latest_file = _is_direct_file(alio) and _string(
            alio.get("file_no")
        ) == latest_direct_file_nos.get(rule_no)
        matches.append(
            match_file(
                alio,
                law_by_rule_id[law_rule_id],
                is_latest_file=is_latest_file,
                approx_days=approx_days,
            )
        )

    matches = _mark_duplicate_version_matches(matches)
    matched_at = now_iso()
    output_path = root / "data/manifest/version_matches.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    for match in matches:
        append_record(
            output_path,
            {**asdict(match), "matched_at": matched_at},
            required=_MATCH_REQUIRED_FIELDS,
        )

    confidence_counter = Counter(match.confidence.value for match in matches)
    confidence_counts = {
        confidence.value: confidence_counter[confidence.value]
        for confidence in Confidence
    }
    rule_match_failures = tuple(
        sorted(
            {
                title
                for rule_no, title in titles_by_rule.items()
                if rule_matches.get(rule_no) is None
            }
        )
    )
    matched_serials = {
        match.law_serial
        for match in matches
        if match.parse_eval_eligible and match.law_serial is not None
    }
    all_serials = {
        serial
        for record in law_records
        if (serial := _optional_string(record.get("serial"))) is not None
    }
    current_matched_rule_ids = {
        match.law_rule_id
        for match in matches
        if match.parse_eval_eligible
        and match.law_is_current is True
        and match.law_rule_id is not None
    }
    summary = MatchSummary(
        confidence_counts=confidence_counts,
        excluded_file_kinds=dict(sorted(excluded.items())),
        rule_match_failures=rule_match_failures,
        unmatched_law_version_count=len(all_serials - matched_serials),
        current_matched_rule_count=len(current_matched_rule_ids),
    )
    _print_summary(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for offline manifest matching."""
    parser = argparse.ArgumentParser(
        description="ALIO 파일을 법제처 발령·시행일자 기준 버전에 매칭합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--approx-days", type=_non_negative_int, default=7)
    args = parser.parse_args(argv)

    try:
        run(args.root, approx_days=args.approx_days)
    except (MatchingError, OSError, ValueError) as exc:
        print(f"버전 매칭 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _law_ids_by_name(
    law_records: Iterable[Mapping[str, object]],
) -> dict[str, set[str]]:
    rule_ids: dict[str, set[str]] = defaultdict(set)
    for record in law_records:
        name = normalize_rule_name(_string(record.get("name")))
        rule_id = _optional_string(record.get("rule_id"))
        if name and rule_id is not None:
            rule_ids[name].add(rule_id)
    return rule_ids


def _alio_titles_by_rule(
    alio_records: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    titles: dict[str, str] = {}
    for record in alio_records:
        rule_no = _optional_string(record.get("rule_no"))
        title = _optional_string(record.get("rule_title"))
        if rule_no is not None and title is not None:
            titles.setdefault(rule_no, title)
    return titles


def _unique_law_versions(
    versions: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    unique: dict[str, Mapping[str, object]] = {}
    for version in versions:
        serial = _optional_string(version.get("serial"))
        if serial is not None:
            unique.setdefault(serial, version)
    return list(unique.values())


def _versions_on_date(
    versions: Iterable[Mapping[str, object]],
    date_text: str,
) -> list[Mapping[str, object]]:
    return [
        version
        for version in versions
        if date_text
        in {
            _optional_string(version.get("issued_date")),
            _optional_string(version.get("effective_date")),
        }
    ]


def _versions_within_days(
    versions: Iterable[Mapping[str, object]],
    date_text: str,
    approx_days: int,
) -> list[Mapping[str, object]]:
    target = _parse_iso_date(date_text)
    if target is None:
        return []
    candidates = []
    for version in versions:
        version_dates = (
            _parse_iso_date(_optional_string(version.get("issued_date"))),
            _parse_iso_date(_optional_string(version.get("effective_date"))),
        )
        if any(
            version_date is not None
            and abs((version_date - target).days) <= approx_days
            for version_date in version_dates
        ):
            candidates.append(version)
    return candidates


def _matching_date_kinds(version: Mapping[str, object], date_text: str) -> str:
    kinds = []
    if _optional_string(version.get("issued_date")) == date_text:
        kinds.append("발령일자")
    if _optional_string(version.get("effective_date")) == date_text:
        kinds.append("시행일자")
    return "·".join(kinds)


def _resolve_candidates(
    alio_record: Mapping[str, object],
    candidates: list[Mapping[str, object]],
    *,
    confidence: Confidence,
    success_basis: Callable[[Mapping[str, object]], str],
    failure_basis: str,
) -> VersionMatch:
    if len(candidates) != 1:
        return _failed_match(
            alio_record,
            failure_basis,
            candidates=_candidate_serials(candidates),
        )
    version = candidates[0]
    is_current = version.get("is_current")
    return VersionMatch(
        **_alio_match_fields(alio_record),
        law_rule_id=_optional_string(version.get("rule_id")),
        law_serial=_optional_string(version.get("serial")),
        law_issued_date=_optional_string(version.get("issued_date")),
        law_is_current=is_current if isinstance(is_current, bool) else None,
        confidence=confidence,
        basis=success_basis(version),
        candidates=(),
        parse_eval_eligible=True,
    )


def _failed_match(
    alio_record: Mapping[str, object],
    basis: str,
    *,
    candidates: tuple[str, ...] = (),
) -> VersionMatch:
    return VersionMatch(
        **_alio_match_fields(alio_record),
        law_rule_id=None,
        law_serial=None,
        law_issued_date=None,
        law_is_current=None,
        confidence=Confidence.FAILED,
        basis=basis,
        candidates=candidates,
        parse_eval_eligible=False,
    )


def _alio_match_fields(alio_record: Mapping[str, object]) -> dict[str, str]:
    return {
        "alio_file_no": _string(alio_record.get("file_no")),
        "alio_local_path": _string(alio_record.get("local_path")),
        "alio_sha256": _string(alio_record.get("sha256")),
        "rule_no": _string(alio_record.get("rule_no")),
    }


def _candidate_serials(
    candidates: Iterable[Mapping[str, object]],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                serial
                for candidate in candidates
                if (serial := _optional_string(candidate.get("serial"))) is not None
            },
            key=_identifier_sort_key,
        )
    )


def _latest_direct_file_nos(
    alio_records: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    file_nos: dict[str, list[str]] = defaultdict(list)
    for record in alio_records:
        if _is_direct_file(record):
            file_nos[_string(record.get("rule_no"))].append(
                _string(record.get("file_no"))
            )
    return {
        rule_no: max(numbers, key=_identifier_sort_key)
        for rule_no, numbers in file_nos.items()
        if numbers
    }


def _is_direct_file(record: Mapping[str, object]) -> bool:
    return record.get("source") == "alio" and "parent_file_no" not in record


def _mark_duplicate_version_matches(
    matches: list[VersionMatch],
) -> list[VersionMatch]:
    counts = Counter(
        match.law_serial
        for match in matches
        if match.parse_eval_eligible and match.law_serial is not None
    )
    return [
        replace(
            match,
            basis=(
                f"{match.basis}; 동일 법제처 버전 중복 매칭: "
                f"ALIO 파일 {counts[match.law_serial]}개"
            ),
        )
        if match.law_serial is not None and counts[match.law_serial] > 1
        else match
        for match in matches
    ]


def _alio_sort_key(record: Mapping[str, object]) -> tuple[object, ...]:
    return (
        _identifier_sort_key(_string(record.get("rule_no"))),
        _identifier_sort_key(_string(record.get("file_no"))),
        _string(record.get("zip_member_path")),
    )


def _identifier_sort_key(value: str) -> tuple[int, object]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _string(value: object) -> str:
    return "" if value is None else str(value)


def _optional_string(value: object) -> str | None:
    text = _string(value).strip()
    return text or None


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("정수를 입력해야 합니다") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수를 입력해야 합니다")
    return parsed


def _print_summary(summary: MatchSummary) -> None:
    confidence_text = ", ".join(
        f"{confidence} {summary.confidence_counts[confidence]}개"
        for confidence in ("exact", "approximate", "failed")
    )
    excluded_text = ", ".join(
        f"{kind} {count}개" for kind, count in summary.excluded_file_kinds.items()
    )
    failed_rules = ", ".join(summary.rule_match_failures) or "없음"
    print(f"신뢰도: {confidence_text}")
    print(f"제외 형식: {excluded_text or '없음'}")
    print(f"규정 매칭 실패: {failed_rules}")
    print(
        f"미매칭 법제처 버전 {summary.unmatched_law_version_count}개, "
        f"현행 버전 매칭 규정 {summary.current_matched_rule_count}개"
    )


if __name__ == "__main__":
    raise SystemExit(main())
