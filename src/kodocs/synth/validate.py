"""Validate generated synthetic documents without an LLM."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    sha256_file,
)
from kodocs.llm.settings import load_generation_config
from kodocs.synth import db
from kodocs.synth.generate import GENERATION_LOG, GENERATIONS_DIR, _load_plan
from kodocs.synth.identifiers import REGULATION_REF_RE, extract_identifiers
from kodocs.synth.plan import CANARIES_PATH, INJECTIONS_PATH, DocPlan
from kodocs.synth.regulations import INDEX_PATH, load_index
from kodocs.synth.seed import (
    PERIOD_END,
    PERIOD_START,
    Seed,
    find_banned_names,
    load_banned_names,
    load_seed,
    surface_forms,
)

DOCS_DIR = Path("data/synthetic/docs")
FAILURES_PATH = Path("data/synthetic/validation_failures.jsonl")
SUMMARY_PATH = Path("data/manifest/synth_validation.json")
MARKER_RE = re.compile(
    r'<fact id="(?P<fid>f-[a-z0-9]+(?:-[a-z0-9]+)*)">(?P<body>.*?)</fact>',
    re.DOTALL,
)
# 2: 사실 표시를 ⟦F:…⟧…⟦/F⟧에서 <fact id="…">…</fact>로 교체(파일럿 1차 4/10 깨짐),
#    계획이 준 식별자·규정 사실 인정, prompt_artifact 추가.
# 3: 배정 사실이 속한 장애(INC-*) 번호를 배정 식별자로 인정(파일럿 2차).
# 4: prompt_artifact에 프롬프트 지시문 문구 "배정된 사실" 추가(파일럿 4차).
VALIDATOR_VERSION = 4

_MARKER_TOKEN_RE = re.compile(
    r'<fact id="(?P<fid>f-[a-z0-9]+(?:-[a-z0-9]+)*)">|(?P<close></fact>)'
)
# Anything marker-like left after stripping valid tokens is malformed. No lenient
# repair: a guessed fix would silently move fact spans.
_MARKER_LEFTOVER_RE = re.compile(r"<fact|</fact>|⟦|⟧")
_PROMPT_ARTIFACT_RE = re.compile(
    r"(?<![A-Za-z0-9-])p-\d{3}(?!\d)|문서 날짜 기준|배정된 사실"
)
# A number inside an identifier (INC-2025-007 원장) is not an amount.
_AMOUNT_RE = re.compile(r"(?<![\dA-Za-z-])(?P<number>\d[\d,]*)\s*(?P<unit>억|만)?\s*원")
_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?P<iso_y>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_d>\d{1,2})"
    r"|(?P<dot_y>\d{4})\s*\.\s*(?P<dot_m>\d{1,2})\s*\.\s*"
    r"(?P<dot_d>\d{1,2})\.?"
    r"|(?P<ko_y>\d{4})년\s*(?P<ko_m>\d{1,2})월\s*"
    r"(?P<ko_d>\d{1,2})일"
    r")(?!\d)"
)
_FAILURE_CODES = frozenset(
    {
        "empty_body",
        "fact_missing",
        "fact_value_mismatch",
        "unassigned_identifier",
        "unassigned_amount",
        "date_out_of_period",
        "banned_name",
        "regulation_unknown",
        "regulation_unplanned",
        "canary_missing",
        "canary_leak",
        "truncated",
        "marker_malformed",
        "marker_unknown_fact",
        "prompt_artifact",
        "internal_error",
    }
)
_WARNING_CODES = frozenset({"length_off_target"})

type Span = tuple[str, int, int]
type PrintFn = Callable[..., object]


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str
    start: int | None = None
    end: int | None = None


def strip_markers(text: str) -> tuple[str, list[Span], list[Issue]]:
    """Remove valid inline fact markers and calculate stripped-text spans."""
    pieces: list[str] = []
    spans: list[Span] = []
    issues: list[Issue] = []
    source_at = 0
    output_length = 0
    opened: tuple[str, int, int] | None = None

    for token in _MARKER_TOKEN_RE.finditer(text):
        between = text[source_at : token.start()]
        pieces.append(between)
        output_length += len(between)
        fact_id = token.group("fid")
        if fact_id is not None:
            if opened is not None:
                issues.append(
                    Issue(
                        "marker_malformed",
                        "사실 표시가 중첩되었습니다.",
                        token.start(),
                        token.end(),
                    )
                )
                literal = token.group()
                pieces.append(literal)
                output_length += len(literal)
            else:
                opened = (fact_id, output_length, token.start())
        elif opened is None:
            issues.append(
                Issue(
                    "marker_malformed",
                    "짝이 없는 닫는 사실 표시가 있습니다.",
                    token.start(),
                    token.end(),
                )
            )
        else:
            fact_id, start, _ = opened
            spans.append((fact_id, start, output_length))
            opened = None
        source_at = token.end()

    tail = text[source_at:]
    pieces.append(tail)
    if opened is not None:
        issues.append(
            Issue(
                "marker_malformed",
                f"짝이 없는 여는 사실 표시가 있습니다: {opened[0]}",
                opened[2],
                None,
            )
        )
    stripped = "".join(pieces)
    for leftover in _MARKER_LEFTOVER_RE.finditer(stripped):
        issues.append(
            Issue(
                "marker_malformed",
                f"형식이 잘못된 사실 표시가 남아 있습니다: {leftover.group()}",
                leftover.start(),
                leftover.end(),
            )
        )
    return stripped, spans, _unique_issues(issues)


def insert_injection(body: str, text: str, rule: str) -> tuple[str, int, int]:
    """Insert one registry-controlled injection and return its exact text span."""
    if rule != "before_last_h2":
        raise ValueError(f"지원하지 않는 인젝션 삽입 규칙: {rule}")
    insertion_at = body.rfind("\n## ")
    if insertion_at < 0:
        insertion_at = len(body)
    inserted = f"\n\n{text}\n\n"
    start = insertion_at + 2
    end = start + len(text)
    return body[:insertion_at] + inserted + body[insertion_at:], start, end


def shift_spans(spans: Sequence[Span], at: int, length: int) -> list[Span]:
    """Shift spans wholly after an insertion point by the inserted length."""
    shifted: list[Span] = []
    for fact_id, start, end in spans:
        if start >= at:
            shifted.append((fact_id, start + length, end + length))
        elif end > at:
            shifted.append((fact_id, start, end + length))
        else:
            shifted.append((fact_id, start, end))
    return shifted


def validate_document(
    doc: DocPlan,
    generation: Mapping[str, object],
    *,
    seed: Seed,
    index: Mapping[str, object],
    banned_names: Sequence[str],
    canaries: Sequence[Mapping[str, object]],
    injection: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, list[Issue]]:
    """Validate one generation; warnings do not turn a document into failure."""
    document, issues = _validate_document_candidate(
        doc,
        generation,
        seed=seed,
        index=index,
        banned_names=banned_names,
        canaries=canaries,
        injection=injection,
        validated_with={
            "validator_version": VALIDATOR_VERSION,
            "seed_files_sha256": {},
            "regulation_index_sha256": "",
        },
    )
    if _has_failure(issues):
        return None, issues
    return document, issues


def _validate_document_candidate(
    doc: DocPlan,
    generation: Mapping[str, object],
    *,
    seed: Seed,
    index: Mapping[str, object],
    banned_names: Sequence[str],
    canaries: Sequence[Mapping[str, object]],
    injection: Mapping[str, object] | None,
    validated_with: Mapping[str, object],
) -> tuple[dict[str, object], list[Issue]]:
    issues: list[Issue] = []
    if generation.get("stop_reason") == "max_tokens":
        issues.append(Issue("truncated", "생성이 최대 토큰 제한에서 중단되었습니다."))
    text = generation.get("text")
    if not isinstance(text, str):
        raise TypeError("succeeded 생성 캐시의 text가 문자열이 아닙니다")

    body, spans, marker_issues = strip_markers(text)
    issues.extend(marker_issues)
    planned_fact_ids = set(doc.fact_ids)
    marked_fact_ids = {fact_id for fact_id, _, _ in spans}
    for fact_id, start, end in spans:
        if fact_id not in planned_fact_ids:
            issues.append(
                Issue(
                    "marker_unknown_fact",
                    f"계획에 없는 사실 표시입니다: {fact_id}",
                    start,
                    end,
                )
            )
    for fact_id in doc.fact_ids:
        if fact_id not in marked_fact_ids:
            issues.append(Issue("fact_missing", f"사실 표시가 없습니다: {fact_id}"))

    injection_record: dict[str, object] | None = None
    if doc.injection_id is not None:
        if injection is None or injection.get("injection_id") != doc.injection_id:
            raise ValueError(f"인젝션 레지스트리 행이 없습니다: {doc.injection_id}")
        injection_text = _required_string(injection, "text")
        insert_rule = _required_string(injection, "insert_rule")
        insertion_at = body.rfind("\n## ")
        if insertion_at < 0:
            insertion_at = len(body)
        original_span_texts = [body[start:end] for _, start, end in spans]
        new_body, start, end = insert_injection(body, injection_text, insert_rule)
        spans = shift_spans(spans, insertion_at, len(new_body) - len(body))
        body = new_body
        for original, (fact_id, fact_start, fact_end) in zip(
            original_span_texts, spans, strict=True
        ):
            if body[fact_start:fact_end] != original:
                issues.append(
                    Issue(
                        "marker_malformed",
                        f"인젝션 삽입 지점이 사실 표시 구간과 겹칩니다: {fact_id}",
                        fact_start,
                        fact_end,
                    )
                )
        injection_record = {
            "injection_id": doc.injection_id,
            "variant": _required_string(injection, "variant"),
            "start": start,
            "end": end,
        }

    content = body.partition("\n")[2]
    if len(content.strip()) < 200:
        issues.append(
            Issue(
                "empty_body",
                f"제목 줄을 제외한 본문이 {len(content.strip())}자로 200자 미만입니다.",
            )
        )

    people = {person.person_id: person for person in seed.people}
    for fact_id, start, end in spans:
        fact = seed.facts.get(fact_id)
        if fact is None or fact.kind == "text":
            continue
        marked_text = body[start:end]
        normalized_marked = _normalize_value(marked_text)
        if not any(
            _normalize_value(form) in normalized_marked
            for form in surface_forms(fact, people=people)
        ):
            issues.append(
                Issue(
                    "fact_value_mismatch",
                    f"표시 구간에 배정 사실 값이 없습니다: {fact_id}",
                    start,
                    end,
                )
            )

    # Identifiers the plan itself hands to the model (assigned id facts, the
    # title hint, e.g. an incident report's own INC number, and the incident an
    # assigned incident fact belongs to) count as assigned.
    assigned_ids = {
        str(seed.facts[fact_id].value)
        for fact_id in doc.fact_ids
        if fact_id in seed.facts and seed.facts[fact_id].kind == "id"
    }
    title_identifiers = extract_identifiers(doc.title_hint)
    assigned_ids |= {
        value
        for kind in ("ticket", "approval", "incident")
        for value, _, _ in title_identifiers[kind]
    }
    assigned_ids |= {
        value
        for fact_id in doc.fact_ids
        if fact_id in seed.facts
        for value, _, _ in extract_identifiers(seed.facts[fact_id].subject)["incident"]
    }
    identifiers = extract_identifiers(body)
    for kind in ("ticket", "approval", "incident"):
        for value, start, end in identifiers[kind]:
            if value not in assigned_ids:
                issues.append(
                    Issue(
                        "unassigned_identifier",
                        f"배정되지 않은 {kind} 식별자입니다: {value}",
                        start,
                        end,
                    )
                )

    assigned_amounts = {
        int(seed.facts[fact_id].value)
        for fact_id in doc.fact_ids
        if fact_id in seed.facts and seed.facts[fact_id].kind == "amount"
    }
    for match in _AMOUNT_RE.finditer(body):
        amount = int(match.group("number").replace(",", ""))
        if match.group("unit") == "만":
            amount *= 10_000
        elif match.group("unit") == "억":
            amount *= 100_000_000
        if amount not in assigned_amounts:
            issues.append(
                Issue(
                    "unassigned_amount",
                    f"배정되지 않은 금액입니다: {match.group()} ({amount}원)",
                    match.start(),
                    match.end(),
                )
            )

    for match in _DATE_RE.finditer(body):
        groups = match.groupdict()
        prefix = "iso" if groups["iso_y"] else "dot" if groups["dot_y"] else "ko"
        try:
            parsed = date(
                int(groups[f"{prefix}_y"]),
                int(groups[f"{prefix}_m"]),
                int(groups[f"{prefix}_d"]),
            )
        except ValueError:
            parsed = None
        if parsed is None or not PERIOD_START <= parsed <= PERIOD_END:
            issues.append(
                Issue(
                    "date_out_of_period",
                    f"유효 기간 밖이거나 존재하지 않는 날짜입니다: {match.group()}",
                    match.start(),
                    match.end(),
                )
            )

    for name, start, end in find_banned_names(body, banned_names):
        issues.append(
            Issue(
                "banned_name", f"금지된 실존 기관·인물 이름입니다: {name}", start, end
            )
        )

    planned_regulations = [
        *doc.regulation_refs,
        *(
            str(seed.facts[fact_id].value)
            for fact_id in doc.fact_ids
            if fact_id in seed.facts and seed.facts[fact_id].kind == "regulation"
        ),
    ]
    issues.extend(_regulation_issues(body, planned_regulations, index))
    for artifact in _PROMPT_ARTIFACT_RE.finditer(body):
        issues.append(
            Issue(
                "prompt_artifact",
                f"프롬프트 내부 표기가 본문에 남았습니다: {artifact.group()}",
                artifact.start(),
                artifact.end(),
            )
        )

    canary = _find_canary(doc, canaries)
    canary_record: dict[str, object] | None = None
    if doc.canary_fact_id is not None:
        if canary is None:
            raise ValueError(f"카나리아 레지스트리 행이 없습니다: {doc.canary_fact_id}")
        canary_value = _required_string(canary, "value")
        if canary_value not in body:
            issues.append(
                Issue(
                    "canary_missing",
                    f"제한 문서의 카나리아가 없습니다: {doc.canary_fact_id}",
                )
            )
        canary_start = body.find(canary_value)
        canary_span = (
            (canary_start, canary_start + len(canary_value))
            if canary_start >= 0
            else (None, None)
        )
        canary_record = {
            "canary_id": _required_string(canary, "canary_id"),
            "fact_id": doc.canary_fact_id,
            "value": canary_value,
            "start": canary_span[0],
            "end": canary_span[1],
        }

    target = int(doc.style["length_target_chars"])
    if len(body) < target * 0.5 or len(body) > target * 2:
        issues.append(
            Issue(
                "length_off_target",
                f"본문 {len(body)}자가 목표 {target}자의 50%~200% 범위를 벗어났습니다.",
            )
        )

    first_line = body.split("\n", 1)[0]
    title = re.sub(r"^\s*#+\s*", "", first_line).strip()
    generation_settings = generation.get("generation")
    if not isinstance(generation_settings, Mapping):
        raise TypeError("생성 캐시의 generation 설정이 객체가 아닙니다")
    document = {
        "doc_id": doc.doc_id,
        "doc_type": doc.doc_type,
        "title": title,
        "team": doc.team,
        "access": asdict(doc.access),
        "author_id": doc.author_id,
        "date": doc.date,
        "body": body,
        "fact_spans": [
            {"fact_id": fact_id, "start": start, "end": end}
            for fact_id, start, end in sorted(
                spans, key=lambda item: (item[1], item[2])
            )
        ],
        "source_fact_ids": list(doc.fact_ids),
        "regulation_refs": list(doc.regulation_refs),
        "canary": canary_record,
        "injection": injection_record,
        "related_doc_ids": list(doc.related_doc_ids),
        "generation": {
            "model": generation.get("model"),
            "generation": dict(generation_settings),
            "template_hash": generation.get("template_hash"),
            "prompt_sha256": generation.get("prompt_sha256"),
            "doc_seed": generation.get("doc_seed"),
            "attempt": generation.get("attempt"),
            "cache_key": generation.get("cache_key"),
            "batch_id": generation.get("batch_id"),
            "custom_id": generation.get("custom_id"),
            "usage": generation.get("usage"),
        },
        "validated_with": dict(validated_with),
    }
    return document, _unique_issues(issues)


def check_canary_isolation(
    docs: Mapping[str, str], canaries: Sequence[Mapping[str, object]]
) -> dict[str, list[Issue]]:
    """Return leaks only for documents containing another document's canary."""
    found: dict[str, list[Issue]] = defaultdict(list)
    for doc_id, body in docs.items():
        for canary in canaries:
            owner = canary.get("doc_id")
            value = canary.get("value")
            if owner == doc_id or not isinstance(value, str) or not value:
                continue
            start = body.find(value)
            if start >= 0:
                found[doc_id].append(
                    Issue(
                        "canary_leak",
                        f"다른 문서({owner})의 카나리아가 포함되었습니다.",
                        start,
                        start + len(value),
                    )
                )
    return {doc_id: _unique_issues(issues) for doc_id, issues in found.items()}


def run(root: Path, *, print_fn: PrintFn = print) -> int:
    """Validate the latest generation attempt for every planned document."""
    root = Path(root).resolve()
    plans = _load_plan(root)
    canaries = _load_registry(root / CANARIES_PATH, "canaries")
    injections = _load_registry(root / INJECTIONS_PATH, "injections")
    canary_expected = len(canaries)
    injection_expected = len(injections)
    cache_paths = sorted((root / GENERATIONS_DIR).glob("*.json"))
    if not cache_paths:
        summary = _build_summary(
            plans,
            {},
            by_code={},
            warnings={},
            exhausted=0,
            canary_expected=canary_expected,
            canary_present=0,
            canary_leaks=0,
            injection_expected=injection_expected,
            injections_inserted=0,
            regressed=0,
        )
        _atomic_write_json(root / SUMMARY_PATH, summary)
        print_fn("생성 결과 없음, `make generate` 먼저 실행하세요.")
        return 1

    seed = load_seed(root)
    index = load_index(root)
    banned_names = load_banned_names(root)
    validated_with = {
        "validator_version": VALIDATOR_VERSION,
        "seed_files_sha256": {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in sorted((root / "data/seed").rglob("*.yaml"))
        },
        "regulation_index_sha256": sha256_file(root / INDEX_PATH),
    }
    injection_by_id = {
        row.get("injection_id"): row
        for row in injections
        if isinstance(row.get("injection_id"), str)
    }

    log_records = latest_records(
        load_records(root / GENERATION_LOG),
        key=lambda row: (row.get("doc_id"), row.get("attempt")),
    )
    history: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in log_records:
        if isinstance(record.get("doc_id"), str) and isinstance(
            record.get("attempt"), int
        ):
            history[str(record["doc_id"])].append(record)

    results: dict[str, dict[str, object]] = {}
    preliminary_passed = 0
    preliminary_failed = 0
    total = len(plans)
    for number, plan in enumerate(plans, start=1):
        attempts = history.get(plan.doc_id, [])
        if not attempts:
            results[plan.doc_id] = {"status": "not_generated", "attempt": 0}
        else:
            latest = max(attempts, key=lambda row: int(row["attempt"]))
            attempt = int(latest["attempt"])
            cache_key = latest.get("cache_key")
            if latest.get("result_type") != "succeeded":
                results[plan.doc_id] = {
                    "status": "not_generated",
                    "attempt": attempt,
                    "latest": latest,
                }
            elif not isinstance(cache_key, str) or not cache_key:
                issue = Issue("internal_error", "생성 로그의 cache_key가 없습니다.")
                results[plan.doc_id] = {
                    "status": "failed",
                    "attempt": attempt,
                    "cache_key": str(cache_key or ""),
                    "issues": [issue],
                    "document": None,
                    "latest": latest,
                }
                preliminary_failed += 1
            else:
                try:
                    generation = _load_generation_cache(
                        root / GENERATIONS_DIR / f"{cache_key}.json",
                        plan=plan,
                        attempt=attempt,
                        cache_key=cache_key,
                    )
                    document, issues = _validate_document_candidate(
                        plan,
                        generation,
                        seed=seed,
                        index=index,
                        banned_names=banned_names,
                        canaries=canaries,
                        injection=injection_by_id.get(plan.injection_id),
                        validated_with=validated_with,
                    )
                    failed = _has_failure(issues)
                    results[plan.doc_id] = {
                        "status": "failed" if failed else "passed",
                        "attempt": attempt,
                        "cache_key": cache_key,
                        "issues": issues,
                        "document": document,
                        "latest": latest,
                    }
                    preliminary_failed += int(failed)
                    preliminary_passed += int(not failed)
                except Exception as exc:  # noqa: BLE001 - isolate corrupt caches
                    issue = Issue(
                        "internal_error",
                        f"{type(exc).__name__}: {_first_line(str(exc))}",
                    )
                    results[plan.doc_id] = {
                        "status": "failed",
                        "attempt": attempt,
                        "cache_key": cache_key,
                        "issues": [issue],
                        "document": None,
                        "latest": latest,
                    }
                    preliminary_failed += 1
        if number % 20 == 0:
            print_fn(
                f"[{number}/{total}] 통과 {preliminary_passed} 실패 {preliminary_failed}",
                flush=True,
            )

    candidate_bodies = {
        doc_id: str(result["document"]["body"])
        for doc_id, result in results.items()
        if isinstance(result.get("document"), Mapping)
    }
    for doc_id, leak_issues in check_canary_isolation(
        candidate_bodies, canaries
    ).items():
        result = results[doc_id]
        result["issues"] = _unique_issues(
            [*result.get("issues", []), *leak_issues]  # type: ignore[list-item]
        )
        result["status"] = "failed"

    ledger_leaks = _canaries_in_rows(db.build_rows(seed), canaries)
    if ledger_leaks:
        detail = "SQLite 행에 카나리아가 포함되었습니다: " + ", ".join(ledger_leaks)
        for result in results.values():
            if result.get("status") == "not_generated":
                continue
            result["issues"] = _unique_issues(
                [*result.get("issues", []), Issue("canary_leak", detail)]  # type: ignore[list-item]
            )
            result["status"] = "failed"

    existing_failure_keys = {
        (row.get("doc_id"), row.get("attempt"))
        for row in latest_records(
            load_records(root / FAILURES_PATH),
            key=lambda row: (row.get("doc_id"), row.get("attempt")),
        )
    }
    regressed = 0
    for plan in plans:
        result = results[plan.doc_id]
        status = result["status"]
        output_path = root / DOCS_DIR / f"{plan.doc_id}.json"
        if status == "passed":
            document = result.get("document")
            if not isinstance(document, Mapping):
                raise TypeError(f"통과 문서가 객체가 아닙니다: {plan.doc_id}")
            _assert_span_invariants(document)
            _atomic_write_json(output_path, document)
        elif status == "failed":
            if output_path.exists():
                regressed += 1
            key = (plan.doc_id, result["attempt"])
            if key not in existing_failure_keys:
                issues = result.get("issues", [])
                append_record(
                    root / FAILURES_PATH,
                    {
                        "doc_id": plan.doc_id,
                        "attempt": result["attempt"],
                        "cache_key": result.get("cache_key", ""),
                        "issues": [
                            {"code": issue.code, "detail": issue.detail}
                            for issue in issues
                            if isinstance(issue, Issue) and issue.code in _FAILURE_CODES
                        ],
                        "ts": now_iso(),
                    },
                    required={"doc_id", "attempt", "cache_key", "issues", "ts"},
                )
                existing_failure_keys.add(key)

    by_code: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    for result in results.values():
        codes = {
            issue.code for issue in result.get("issues", []) if isinstance(issue, Issue)
        }
        by_code.update(code for code in codes if code in _FAILURE_CODES)
        warnings.update(code for code in codes if code in _WARNING_CODES)

    max_attempts = load_generation_config().max_attempts
    exhausted = sum(
        1
        for result in results.values()
        if result.get("status") != "passed"
        and isinstance(result.get("attempt"), int)
        and int(result["attempt"]) >= max_attempts
    )
    canary_present = sum(
        1
        for row in canaries
        if isinstance(row.get("doc_id"), str)
        and isinstance(row.get("value"), str)
        and row["value"] in candidate_bodies.get(str(row["doc_id"]), "")
    )
    injections_inserted = sum(
        1
        for result in results.values()
        if isinstance(result.get("document"), Mapping)
        and result["document"].get("injection") is not None  # type: ignore[union-attr]
    )
    canary_leaks = sum(
        1
        for result in results.values()
        if any(
            isinstance(issue, Issue) and issue.code == "canary_leak"
            for issue in result.get("issues", [])
        )
    )
    summary = _build_summary(
        plans,
        results,
        by_code=dict(sorted(by_code.items())),
        warnings=dict(sorted(warnings.items())),
        exhausted=exhausted,
        canary_expected=canary_expected,
        canary_present=canary_present,
        canary_leaks=canary_leaks,
        injection_expected=injection_expected,
        injections_inserted=injections_inserted,
        regressed=regressed,
    )
    _atomic_write_json(root / SUMMARY_PATH, summary)
    print_fn(
        f"검증 완료: 통과 {summary['passed']} 실패 {summary['failed']} "
        f"미생성 {summary['not_generated']}"
    )
    if regressed:
        print_fn(f"회귀 {regressed}건: 기존 통과 파일은 보존했습니다.")
    return 1 if summary["passed"] == 0 or regressed or ledger_leaks else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for deterministic synthetic document validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return run(args.root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"합성 문서 검증 실패: {_first_line(str(exc))}")
        return 1


def _regulation_issues(
    body: str, planned_refs: Sequence[str], index: Mapping[str, object]
) -> list[Issue]:
    rules = index.get("rules")
    if not isinstance(rules, Mapping):
        raise TypeError("규정 색인의 rules가 객체가 아닙니다")
    rule_names = sorted(
        (str(name) for name in rules), key=lambda value: (-len(value), value)
    )
    planned = set(planned_refs)
    issues: list[Issue] = []
    for match in REGULATION_REF_RE.finditer(body):
        raw_name = match.group("name").strip()
        name = next(
            (candidate for candidate in rule_names if raw_name.endswith(candidate)),
            raw_name,
        )
        key = f"제{match.group('num')}조"
        if match.group("branch"):
            key += f"의{match.group('branch')}"
        rule = rules.get(name)
        articles = rule.get("articles") if isinstance(rule, Mapping) else None
        if not isinstance(articles, Mapping) or key not in articles:
            issues.append(
                Issue(
                    "regulation_unknown",
                    f"현행 색인에 없는 규정 조항입니다: {name} {key}",
                    match.start(),
                    match.end(),
                )
            )
        elif f"{name} {key}" not in planned:
            issues.append(
                Issue(
                    "regulation_unplanned",
                    f"계획에 배정되지 않은 규정 조항입니다: {name} {key}",
                    match.start(),
                    match.end(),
                )
            )
    return issues


def _find_canary(
    doc: DocPlan, canaries: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    return next(
        (
            row
            for row in canaries
            if row.get("doc_id") == doc.doc_id
            and row.get("fact_id") == doc.canary_fact_id
        ),
        None,
    )


def _load_registry(path: Path, field: str) -> list[Mapping[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get(field) if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise TypeError(f"레지스트리 형식이 잘못되었습니다: {path}")
    return list(rows)


def _load_generation_cache(
    path: Path, *, plan: DocPlan, attempt: int, cache_key: str
) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("생성 캐시는 JSON 객체여야 합니다")
    expected = {
        "doc_id": plan.doc_id,
        "attempt": attempt,
        "cache_key": cache_key,
        "result_type": "succeeded",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(
                f"생성 캐시 {key} 불일치: {payload.get(key)!r} != {value!r}"
            )
    return payload


def _canaries_in_rows(
    rows: Mapping[str, Sequence[Mapping[str, object]]],
    canaries: Sequence[Mapping[str, object]],
) -> list[str]:
    values = sorted(
        str(row["value"])
        for row in canaries
        if isinstance(row.get("value"), str) and row["value"]
    )
    found: set[str] = set()
    for table, table_rows in rows.items():
        for row_number, row in enumerate(table_rows, start=1):
            for value in row.values():
                if not isinstance(value, str):
                    continue
                for canary in values:
                    if canary in value:
                        found.add(f"{table}[{row_number}]")
    return sorted(found)


def _build_summary(
    plans: Sequence[DocPlan],
    results: Mapping[str, Mapping[str, object]],
    *,
    by_code: Mapping[str, int],
    warnings: Mapping[str, int],
    exhausted: int,
    canary_expected: int,
    canary_present: int,
    canary_leaks: int,
    injection_expected: int,
    injections_inserted: int,
    regressed: int,
) -> dict[str, object]:
    statuses = {
        plan.doc_id: str(results.get(plan.doc_id, {}).get("status", "not_generated"))
        for plan in plans
    }

    def dimensions(attribute: str) -> dict[str, dict[str, int]]:
        values = sorted(
            {
                (
                    getattr(plan, attribute).level
                    if attribute == "access"
                    else str(getattr(plan, attribute))
                )
                for plan in plans
            }
        )
        output: dict[str, dict[str, int]] = {}
        for value in values:
            selected = [
                plan
                for plan in plans
                if (
                    plan.access.level
                    if attribute == "access"
                    else str(getattr(plan, attribute))
                )
                == value
            ]
            counts = Counter(statuses[plan.doc_id] for plan in selected)
            output[value] = {
                "planned": len(selected),
                "generated": counts["passed"] + counts["failed"],
                "passed": counts["passed"],
                "failed": counts["failed"],
                "not_generated": counts["not_generated"],
            }
        return output

    totals = Counter(statuses.values())
    generated = totals["passed"] + totals["failed"]
    return {
        "planned": len(plans),
        "generated": generated,
        "passed": totals["passed"],
        "failed": totals["failed"],
        "not_generated": totals["not_generated"],
        "exhausted": exhausted,
        "regressed": regressed,
        "by_code": dict(sorted(by_code.items())),
        "by_type": dimensions("doc_type"),
        "by_access": dimensions("access"),
        "warnings": dict(sorted(warnings.items())),
        "canary": {
            "expected": canary_expected,
            "present": canary_present,
            "leaks": canary_leaks,
        },
        "injections": {
            "expected": injection_expected,
            "inserted": injections_inserted,
        },
    }


def _assert_span_invariants(document: Mapping[str, object]) -> None:
    body = document.get("body")
    spans = document.get("fact_spans")
    if not isinstance(body, str) or not isinstance(spans, list):
        raise TypeError("문서 body/fact_spans 형식이 잘못되었습니다")
    previous_start = -1
    for span in spans:
        if not isinstance(span, Mapping):
            raise TypeError("fact span이 객체가 아닙니다")
        start, end = span.get("start"), span.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise TypeError("fact span의 start/end가 정수가 아닙니다")
        if not 0 <= start <= end <= len(body) or start < previous_start:
            raise ValueError("fact span 범위 또는 정렬이 잘못되었습니다")
        previous_start = start
    for field in ("canary", "injection"):
        record = document.get(field)
        if record is None:
            continue
        if not isinstance(record, Mapping):
            raise TypeError(f"{field} span 레코드가 객체가 아닙니다")
        start, end = record.get("start"), record.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise TypeError(f"{field} span이 완전하지 않습니다")
        expected = record.get("value") if field == "canary" else None
        if expected is not None and body[start:end] != expected:
            raise ValueError(f"{field} span 텍스트가 레지스트리 값과 다릅니다")


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _normalize_value(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def _required_string(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"필수 문자열 필드가 없습니다: {field}")
    return value


def _has_failure(issues: Sequence[Issue]) -> bool:
    return any(issue.code in _FAILURE_CODES for issue in issues)


def _unique_issues(issues: Sequence[Issue]) -> list[Issue]:
    unique: dict[tuple[str, str, int | None, int | None], Issue] = {}
    for issue in issues:
        unique[(issue.code, issue.detail, issue.start, issue.end)] = issue
    return list(unique.values())


def _first_line(value: str) -> str:
    return value.splitlines()[0] if value.splitlines() else type(value).__name__


if __name__ == "__main__":
    raise SystemExit(main())
