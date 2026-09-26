"""Select the bounded M2 parsing corpus from local ingestion manifests."""

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from kodocs.ingest.manifest import latest_records, load_records, now_iso

_ELIGIBLE_CONFIDENCES = frozenset({"exact", "approximate"})
_SUPPORTED_SOURCE_KINDS = frozenset({"hwp5", "hwpx"})


@dataclass(frozen=True)
class ParseTarget:
    """One ALIO source selected for the M2 parser comparison."""

    rule_no: str
    law_rule_id: str
    law_serial: str
    issued_date: str
    role: str
    source_path: str
    source_sha256: str
    source_kind: str
    pdf_path: str | None
    page_count: int | None
    match_confidence: str
    law_annex_count: int
    embedded_fonts: tuple[str, ...]
    non_hcr_fonts: tuple[str, ...]


@dataclass(frozen=True)
class MissingCurrent:
    """A current Law API version without an eligible ALIO parsing source."""

    rule_id: str
    name: str
    reason: str


@dataclass(frozen=True)
class TargetSelection:
    """Selected targets plus explicit omissions and budget-relevant totals."""

    targets: tuple[ParseTarget, ...]
    missing_current: tuple[MissingCurrent, ...]

    @property
    def current_count(self) -> int:
        return sum(target.role == "current" for target in self.targets)

    @property
    def history_count(self) -> int:
        return sum(target.role == "history" for target in self.targets)

    @property
    def pages_known(self) -> int:
        return sum(
            target.page_count
            for target in self.targets
            if target.page_count is not None
        )

    @property
    def targets_without_page_count(self) -> int:
        return sum(target.page_count is None for target in self.targets)


class ParseTargetsError(RuntimeError):
    """Required local manifests are unavailable or inconsistent."""


def select_targets(
    law: Iterable[Mapping[str, object]],
    alio: Iterable[Mapping[str, object]],
    matches: Iterable[Mapping[str, object]],
    conversions: Iterable[Mapping[str, object]],
    *,
    history_rules: int = 10,
    versions_per_rule: int = 2,
) -> TargetSelection:
    """Select current and bounded historical sources without network access."""
    if history_rules < 0:
        raise ValueError("history_rules must be non-negative")
    if versions_per_rule < 0:
        raise ValueError("versions_per_rule must be non-negative")

    law_records = latest_records(law, lambda record: record.get("serial"))
    alio_records = latest_records(
        alio,
        lambda record: (
            record.get("source"),
            record.get("file_no"),
            record.get("zip_member_path"),
        ),
    )
    conversion_records = latest_records(
        conversions, lambda record: record.get("source_sha256")
    )
    alio_by_source = {
        (_text(record.get("local_path")), _text(record.get("sha256"))): record
        for record in alio_records
    }
    conversions_by_sha = {
        _text(record.get("source_sha256")): record
        for record in conversion_records
        if record.get("status") == "ok"
    }

    sources_by_serial: dict[
        str, list[tuple[Mapping[str, object], Mapping[str, object]]]
    ] = defaultdict(list)
    for match in matches:
        confidence = _text(match.get("confidence"))
        serial = _text(match.get("law_serial"))
        if (
            match.get("parse_eval_eligible") is not True
            or confidence not in _ELIGIBLE_CONFIDENCES
            or not serial
        ):
            continue
        source_key = (
            _text(match.get("alio_local_path")),
            _text(match.get("alio_sha256")),
        )
        source = alio_by_source.get(source_key)
        if source is None:
            continue
        if _text(source.get("file_kind")) not in _SUPPORTED_SOURCE_KINDS:
            continue
        sources_by_serial[serial].append((source, match))

    selected: list[ParseTarget] = []
    missing_current: list[MissingCurrent] = []
    for version in law_records:
        if version.get("is_current") is not True:
            continue
        candidate = _choose_source(
            sources_by_serial.get(_text(version.get("serial")), [])
        )
        if candidate is None:
            missing_current.append(
                MissingCurrent(
                    rule_id=_text(version.get("rule_id")),
                    name=_text(version.get("name")),
                    reason="parse_eval_eligible=true인 exact/approximate ALIO 매칭 없음",
                )
            )
            continue
        selected.append(
            _make_target(
                version,
                candidate,
                role="current",
                conversions_by_sha=conversions_by_sha,
            )
        )

    versions_by_rule: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for version in law_records:
        versions_by_rule[_text(version.get("rule_id"))].append(version)

    history_candidates: list[
        tuple[str, list[Mapping[str, object]], list[Mapping[str, object]]]
    ] = []
    for rule_id, versions in versions_by_rule.items():
        history_versions = [
            version for version in versions if version.get("is_current") is False
        ]
        eligible_history = [
            version
            for version in history_versions
            if sources_by_serial.get(_text(version.get("serial")))
        ]
        if len(history_versions) >= 3 and eligible_history:
            history_candidates.append((rule_id, history_versions, eligible_history))

    history_candidates.sort(key=lambda item: (-len(item[1]), item[0]))
    for _, _, eligible_history in history_candidates[:history_rules]:
        for version in _history_versions(eligible_history, versions_per_rule):
            candidate = _choose_source(sources_by_serial[_text(version.get("serial"))])
            if candidate is not None:
                selected.append(
                    _make_target(
                        version,
                        candidate,
                        role="history",
                        conversions_by_sha=conversions_by_sha,
                    )
                )

    selected.sort(key=lambda target: (target.role, target.rule_no, target.issued_date))
    missing_current.sort(key=lambda item: (item.rule_id, item.name))
    return TargetSelection(tuple(selected), tuple(missing_current))


def run(
    root: Path,
    *,
    history_rules: int = 10,
    versions_per_rule: int = 2,
) -> TargetSelection:
    """Read current manifests, regenerate ``parse_targets.json``, and summarize."""
    manifest_dir = root / "data/manifest"
    inputs = (
        (manifest_dir / "law_api.jsonl", "make collect-law"),
        (manifest_dir / "alio.jsonl", "make collect-alio"),
        (manifest_dir / "version_matches.jsonl", "make match-versions"),
        (manifest_dir / "conversions.jsonl", "make convert-hwp"),
    )
    missing = [
        f"{path.relative_to(root).as_posix()}이 없습니다. 먼저 {command}를 실행하세요."
        for path, command in inputs
        if not path.is_file()
    ]
    if missing:
        raise ParseTargetsError(" ".join(missing))

    law_path, alio_path, matches_path, conversions_path = (item[0] for item in inputs)
    selection = select_targets(
        load_records(law_path),
        load_records(alio_path),
        load_records(matches_path),
        load_records(conversions_path),
        history_rules=history_rules,
        versions_per_rule=versions_per_rule,
    )
    payload = {
        "generated_at": now_iso(),
        "params": {
            "history_rules": history_rules,
            "versions_per_rule": versions_per_rule,
        },
        "totals": {
            "targets": len(selection.targets),
            "current": selection.current_count,
            "history": selection.history_count,
            "pages_known": selection.pages_known,
            "targets_without_page_count": selection.targets_without_page_count,
        },
        "missing_current": [asdict(item) for item in selection.missing_current],
        "targets": [asdict(target) for target in selection.targets],
    }
    output_path = manifest_dir / "parse_targets.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _print_summary(selection)
    return selection


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for manifest-only parse-target selection."""
    parser = argparse.ArgumentParser(
        description="현행 전부와 일부 과거 버전의 M2 파싱 대상 목록을 만듭니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--history-rules", type=_non_negative_int, default=10)
    parser.add_argument("--versions-per-rule", type=_non_negative_int, default=2)
    args = parser.parse_args(argv)

    try:
        run(
            args.root,
            history_rules=args.history_rules,
            versions_per_rule=args.versions_per_rule,
        )
    except (ParseTargetsError, OSError, ValueError) as exc:
        print(f"파싱 대상 선정 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _choose_source(
    candidates: Iterable[tuple[Mapping[str, object], Mapping[str, object]]],
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    choices = list(candidates)
    if not choices:
        return None
    return max(
        choices,
        key=lambda item: (
            1 if _text(item[0].get("file_kind")) == "hwpx" else 0,
            _file_no_key(_text(item[0].get("file_no"))),
            _text(item[0].get("local_path")),
        ),
    )


def _history_versions(
    versions: Iterable[Mapping[str, object]], limit: int
) -> list[Mapping[str, object]]:
    ordered = sorted(
        versions,
        key=lambda version: (
            _text(version.get("issued_date")),
            _text(version.get("serial")),
        ),
    )
    if not ordered or limit == 0:
        return []
    if limit == 1:
        return [ordered[-1]]

    prioritized = [ordered[0]]
    if len(ordered) > 1:
        prioritized.append(ordered[-1])
    prioritized.extend(reversed(ordered[1:-1]))
    return prioritized[:limit]


def _make_target(
    version: Mapping[str, object],
    candidate: tuple[Mapping[str, object], Mapping[str, object]],
    *,
    role: str,
    conversions_by_sha: Mapping[str, Mapping[str, object]],
) -> ParseTarget:
    source, match = candidate
    source_sha256 = _text(source.get("sha256"))
    conversion = conversions_by_sha.get(source_sha256)
    pdf_path: str | None = None
    page_count: int | None = None
    embedded_fonts: tuple[str, ...] = ()
    if conversion is not None:
        recorded_pdf = conversion.get("output_path")
        if isinstance(recorded_pdf, str) and recorded_pdf:
            pdf_path = recorded_pdf
        recorded_pages = conversion.get("page_count")
        if isinstance(recorded_pages, int) and not isinstance(recorded_pages, bool):
            page_count = recorded_pages
        recorded_fonts = conversion.get("embedded_fonts")
        if isinstance(recorded_fonts, list):
            embedded_fonts = tuple(
                sorted({font for font in recorded_fonts if isinstance(font, str)})
            )
    non_hcr_fonts = tuple(font for font in embedded_fonts if not font.startswith("HCR"))
    annex_count = version.get("annex_count")
    return ParseTarget(
        rule_no=_text(source.get("rule_no")),
        law_rule_id=_text(version.get("rule_id")),
        law_serial=_text(version.get("serial")),
        issued_date=_text(version.get("issued_date")),
        role=role,
        source_path=_text(source.get("local_path")),
        source_sha256=source_sha256,
        source_kind=_text(source.get("file_kind")),
        pdf_path=pdf_path,
        page_count=page_count,
        match_confidence=_text(match.get("confidence")),
        law_annex_count=(
            annex_count
            if isinstance(annex_count, int) and not isinstance(annex_count, bool)
            else 0
        ),
        embedded_fonts=embedded_fonts,
        non_hcr_fonts=non_hcr_fonts,
    )


def _file_no_key(value: str) -> tuple[int, int, str]:
    if value.isdigit():
        return (1, int(value), value)
    return (0, -1, value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("정수를 입력해야 합니다") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수를 입력해야 합니다")
    return parsed


def _print_summary(selection: TargetSelection) -> None:
    print(
        f"대상 {len(selection.targets)}개(현행 {selection.current_count}개, "
        f"과거 {selection.history_count}개), "
        f"알려진 총 페이지 수 {selection.pages_known}, "
        f"페이지 수 미상 {selection.targets_without_page_count}개, "
        f"현행 누락 규정 {len(selection.missing_current)}개"
    )


if __name__ == "__main__":
    raise SystemExit(main())
