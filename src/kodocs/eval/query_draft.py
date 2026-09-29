"""Draft synthetic evaluation questions with a resumable Anthropic Batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from kodocs.config import MissingAPIKeyError, load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
    budget_estimate_usd,
    estimate_usd,
)
from kodocs.eval.golden import Question, load_questions, write_questions
from kodocs.eval.lexical import (
    CONTAINMENT_CAP,
    DOCS_PATH,
    LCS_CAP,
    MISMATCH_MAX,
    load_documents,
    overlap,
    summarize,
)
from kodocs.ingest.manifest import append_record, load_records
from kodocs.llm.anthropic_batch import (
    SERVICE_PREFIX,
    AnthropicBatchClient,
    BatchRequest,
    SubmitRejectedError,
    SubmitUncertainError,
    anthropic_client_factory,
)
from kodocs.llm.settings import GenerationConfig, load_generation_config
from kodocs.synth.seed import (
    GRADES,
    TEAMS,
    Person,
    Seed,
    load_seed,
    surface_forms,
)

DRAFTS_DIR = Path("data/synthetic/queries/drafts")
DRAFT_LOG = Path("data/synthetic/queries/draft_log.jsonl")
VALIDATION_PATH = Path("data/synthetic/queries/validated.jsonl")
VALIDATION_SUMMARY_PATH = Path("data/manifest/golden_query_validation.json")
FAILURES_PATH = Path("data/synthetic/queries/draft_failures.jsonl")
BATCHES_MANIFEST = Path("data/manifest/anthropic_batches_golden.jsonl")
DRY_RUNS_PATH = Path("data/manifest/golden_dry_runs.jsonl")
SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")
TEMPLATES_DIR = Path(__file__).parent / "templates" / "query_draft"

_CUSTOM_ID_RE = re.compile(r"^(?P<id>[A-Za-z0-9_-]+)-a(?P<attempt>[1-9]\d*)$")
_PERSON_ID_RE = re.compile(r"(?<![A-Za-z0-9])p-\d{3}(?!\d)", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"\?|(?<![0-9])\.(?=\s|$)|(?<=[0-9])\.$")
_FACT_ID_RE = re.compile(r"(?<![A-Za-z0-9])f-[a-z0-9]", re.IGNORECASE)
_FACT_WORD_RE = re.compile(r"(?<![A-Za-z0-9])fact(?![A-Za-z0-9])", re.IGNORECASE)
_DRAFT_LOG_REQUIRED = frozenset(
    {
        "id",
        "attempt",
        "custom_id",
        "cache_key",
        "batch_id",
        "status",
        "generation",
        "settings_hash",
        "template_hash",
        "prompt_sha256",
        "ts",
    }
)
_FAILURE_REQUIRED = frozenset({"id", "attempt", "codes", "ts"})
_DRY_RUN_REQUIRED = frozenset(
    {
        "ts",
        "n_requests",
        "type_counts",
        "estimated_input_tokens",
        "expected_cost_usd",
        "maximum_cost_usd",
        "budget_estimate_usd",
    }
)

_ATTRIBUTE_LABELS = {
    "action": "후속 조치",
    "cause": "원인",
    "decision": "결정 내용",
    "dependency": "의존 서비스",
    "incident": "관련 장애",
    "oncall": "당직 담당자",
    "owner": "담당자 또는 운영 담당 팀",
    "plan": "계획 내용",
    "policy": "정책 내용",
    "restored": "복구 시각",
    "result": "결과",
    "service": "관련 서비스",
    "severity": "심각도",
    "slo": "서비스 수준 목표",
    "start": "발생 시각",
    "ticket": "관련 티켓",
    "version": "변경 또는 배포 내용",
    "window": "적용 기간",
}
_SQL_ATTRIBUTE_LABELS = {
    "tickets_by_team": "팀별 티켓 수",
    "tickets_by_status": "상태별 티켓 수",
    "tickets_by_month": "월별 티켓 수",
    "approvals_by_team": "팀별 결재 수",
    "approvals_by_status": "상태별 결재 수",
    "approval_amount_by_team": "팀별 결재 금액 합계",
}

PrintFn = Callable[..., object]
DraftCheck = Callable[[Question, str, Seed, Mapping[str, Person]], bool]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def template_hash(*, templates_dir: Path | None = None) -> str:
    """Hash the exact system and user template bytes in their render order."""
    directory = templates_dir or TEMPLATES_DIR
    material = (directory / "system.md").read_bytes() + (
        directory / "user.md"
    ).read_bytes()
    return hashlib.sha256(material).hexdigest()


def build_request(
    spec: Question,
    *,
    config: GenerationConfig,
    attempt: int,
) -> BatchRequest:
    """Build one leak-minimized question-drafting request."""
    if attempt < 1:
        raise ValueError("attempt는 1 이상이어야 합니다")
    system = (TEMPLATES_DIR / "system.md").read_text(encoding="utf-8")
    user_template = (TEMPLATES_DIR / "user.md").read_text(encoding="utf-8")
    team = TEAMS.get(spec.asker.team)
    team_name = team.name_ko if team is not None else spec.asker.team
    grade = GRADES.get(spec.asker.grade, f"{spec.asker.grade}급")
    user = user_template.format(
        question_type=spec.type,
        anchors=" / ".join(spec.anchor_values),
        attribute=_asked_attribute(spec),
        as_of=spec.as_of or "없음",
        asker_team=team_name,
        asker_grade=grade,
    )
    prompt_sha256 = hashlib.sha256(
        system.encode("utf-8") + b"\0" + user.encode("utf-8")
    ).hexdigest()
    cache_material = json.dumps(
        {
            "id": spec.id,
            "attempt": attempt,
            "settings_hash": config.settings_hash(),
            "template_hash": template_hash(),
            "prompt_sha256": prompt_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BatchRequest(
        custom_id=f"{spec.id}-a{attempt}",
        system=system,
        user=user,
        cache_key=hashlib.sha256(cache_material.encode("utf-8")).hexdigest()[:32],
    )


def pending_specs(
    root: Path,
    specs: Sequence[Question],
    *,
    config: GenerationConfig,
) -> list[tuple[Question, int]]:
    """Return unvalidated specs with attempts left for the current templates."""
    root = Path(root)
    validated_path = root / VALIDATION_PATH
    validated = (
        {
            question.id
            for question in load_questions(validated_path)
            if question.question
        }
        if validated_path.exists()
        else set()
    )
    history = _draft_history(root)
    current_hash = template_hash()
    pending: list[tuple[Question, int]] = []
    for spec in sorted(specs, key=lambda item: item.id):
        if spec.id in validated:
            continue
        attempts = history.get(spec.id, {})
        current_attempts = sum(
            not _other_template(record, current_hash) for record in attempts.values()
        )
        if current_attempts >= config.max_attempts:
            continue
        next_attempt = max(attempts, default=0) + 1
        pending.append((spec, next_attempt))
    return pending


def validate_draft(
    spec: Question,
    text: str,
    *,
    seed: Seed,
    people: Mapping[str, Person] | Sequence[Person],
    docs: Mapping[str, Mapping[str, object]] | None = None,
) -> list[str]:
    """Return deterministic draft failure codes in stable check order."""
    people_by_id = (
        dict(people)
        if isinstance(people, Mapping)
        else {person.person_id: person for person in people}
    )
    candidate = text.strip()
    if _bad_shape(spec, candidate, seed, people_by_id):
        return ["empty_or_multiline"]

    checks: tuple[tuple[str, DraftCheck], ...] = (
        ("anchor_missing", _anchor_missing),
        ("answer_leak", _answer_leak),
        ("as_of_missing", _as_of_missing),
        ("prompt_artifact", _prompt_artifact),
    )
    codes = [
        code for code, check in checks if check(spec, candidate, seed, people_by_id)
    ]
    if docs is not None and _overlap_exceeds(overlap(spec, candidate, docs)):
        codes.append("overlap_cap")
    return codes


def run(
    root: Path,
    *,
    client: AnthropicBatchClient | None,
    config: GenerationConfig,
    dry_run: bool,
    assume_yes: bool,
    limit: int | None,
    print_fn: PrintFn = print,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Advance question drafting by one safe Batch state transition."""
    root = Path(root)
    if limit is not None and limit < 1:
        raise ValueError("limit는 1 이상이어야 합니다")

    if client is not None and not dry_run:
        uncertain = client.uncertain_submissions()
        if uncertain:
            identifiers = ", ".join(
                str(record.get("submission_id", "?")) for record in uncertain
            )
            print_fn(
                "불명 제출이 있어 새 Batch를 제출하지 않습니다: "
                f"{identifiers}. Anthropic 콘솔을 확인한 뒤 `--status`로 다시 확인하세요."
            )
            return 2

        opened = client.open_batches()
        if opened:
            return _handle_open_batches(
                root,
                opened=opened,
                client=client,
                config=config,
                print_fn=print_fn,
            )

    specs = load_questions(root / SPECS_PATH)
    targets = pending_specs(root, specs, config=config)
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        print_fn("질문 초안 대상 0건: 검증 통과했거나 현재 템플릿 시도를 소진했습니다.")
        return 0

    requests: list[BatchRequest] = []
    metadata: dict[str, dict[str, object]] = {}
    for index, (spec, attempt) in enumerate(targets, start=1):
        request = build_request(spec, config=config, attempt=attempt)
        requests.append(request)
        metadata[request.custom_id] = _request_metadata(request, config=config)
        _progress(print_fn, index, len(targets), "요청 생성")

    estimates = _cost_estimates(requests, config)
    settings = load_settings(os.environ)
    ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
    month_total = ledger.month_total_usd()
    remaining = settings.monthly_budget_usd - month_total
    remaining_after = remaining - float(estimates["budget_maximum_cost_usd"])
    _print_plan(
        targets,
        estimates=estimates,
        month_total=month_total,
        monthly_budget_usd=settings.monthly_budget_usd,
        remaining_after=remaining_after,
        print_fn=print_fn,
    )
    if dry_run:
        _append_dry_run(
            root,
            targets=targets,
            estimates=estimates,
            month_total=month_total,
            monthly_budget_usd=settings.monthly_budget_usd,
            remaining_after=remaining_after,
        )
    if float(estimates["budget_maximum_cost_usd"]) > remaining:
        print_fn("예산 초과: 여유를 포함한 최대 비용이 이번 달 남은 예산을 넘습니다.")
        return 1
    if dry_run:
        return 0
    if client is None:
        print_fn("Anthropic Batch 클라이언트가 없어 제출할 수 없습니다.")
        return 2
    if not assume_yes and input_fn("계속하려면 y 입력: ").strip().casefold() != "y":
        print_fn("사용자가 질문 초안 생성을 취소했습니다.")
        return 0

    try:
        batch_id = client.submit(requests)
    except (SubmitUncertainError, SubmitRejectedError, BudgetExceededError) as exc:
        print_fn(f"Batch 제출 중단: {_first_line(str(exc))}")
        return 2
    for index, request in enumerate(requests, start=1):
        spec_id, attempt = _parse_custom_id(request.custom_id)
        _append_draft_log(
            root,
            {
                **metadata[request.custom_id],
                "id": spec_id,
                "attempt": attempt,
                "custom_id": request.custom_id,
                "cache_key": request.cache_key,
                "batch_id": batch_id,
                "status": "submitted",
                "ts": _utc_now(),
            },
        )
        _progress(print_fn, index, len(requests), "제출 기록")
    print_fn(
        f"Batch 제출 완료: {batch_id}. 결과 준비 후 `make queries`를 다시 실행하세요."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; paid calls require a key and user confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    modes = sum((args.dry_run, args.status, args.validate_only))
    if modes > 1:
        parser.error("--dry-run, --status, --validate-only는 함께 쓸 수 없습니다")

    root = args.root.resolve()
    try:
        if args.validate_only:
            return validate_only(root)
        config = load_generation_config(section="golden_queries")
        if args.dry_run:
            return run(
                root,
                client=None,
                config=config,
                dry_run=True,
                assume_yes=args.yes,
                limit=args.limit,
            )

        settings = load_settings()
        try:
            api_key = settings.require_key("ANTHROPIC_API_KEY")
        except MissingAPIKeyError as exc:
            print(str(exc))
            return 2
        ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
        client = AnthropicBatchClient(
            client_factory=anthropic_client_factory(api_key),
            config=config,
            ledger=ledger,
            guard=BudgetGuard(ledger, settings.monthly_budget_usd),
            manifest_path=root / BATCHES_MANIFEST,
        )
        if args.status:
            return _print_status(client)
        return run(
            root,
            client=client,
            config=config,
            dry_run=False,
            assume_yes=args.yes,
            limit=args.limit,
        )
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"질문 초안 실행 실패: {_first_line(str(exc))}")
        return 1


def validate_only(root: Path, *, print_fn: PrintFn = print) -> int:
    """Revalidate collected immutable draft caches without API access."""
    root = Path(root)
    if not sorted((root / DRAFTS_DIR).glob("*.json")):
        print_fn("수거한 초안 없음, `make queries` 먼저")
        return 1
    specs = load_questions(root / SPECS_PATH)
    seed = load_seed(root)
    docs = load_documents(root / DOCS_PATH)
    summary = _write_validation_outputs(
        root,
        specs=specs,
        seed=seed,
        people={person.person_id: person for person in seed.people},
        docs=docs,
    )
    print_fn(
        f"질문 초안 재검증 완료: 통과 {summary['validated']} 실패 {summary['failed']}"
    )
    return 0


def _asked_attribute(spec: Question) -> str:
    if spec.type == "permission":
        return "제한 문서 확인 코드"
    if spec.type == "unanswerable":
        return "비용 또는 영향 사용자 수 중 하나"
    if spec.type == "sql":
        return _SQL_ATTRIBUTE_LABELS.get(spec.anchor_values[0], "집계 결과")
    labels: list[str] = []
    for qrel in spec.qrels:
        if qrel.rel != 2 or qrel.fact_id is None:
            continue
        attribute = qrel.fact_id.rsplit("-", 1)[-1]
        label = _ATTRIBUTE_LABELS.get(attribute, "관련 정보")
        if label not in labels:
            labels.append(label)
    return " 및 ".join(labels) if labels else "관련 정보"


def _request_metadata(
    request: BatchRequest, *, config: GenerationConfig
) -> dict[str, object]:
    return {
        "generation": config.record(),
        "settings_hash": config.settings_hash(),
        "template_hash": template_hash(),
        "prompt_sha256": hashlib.sha256(
            request.system.encode("utf-8") + b"\0" + request.user.encode("utf-8")
        ).hexdigest(),
    }


def _bad_shape(
    _spec: Question,
    text: str,
    _seed: Seed,
    _people: Mapping[str, Person],
) -> bool:
    if not text or "\n" in text or "\r" in text or "!" in text:
        return True
    ends = [m.start() for m in _SENTENCE_END_RE.finditer(text)]
    return ends != [len(text) - 1]


def _anchor_missing(
    spec: Question,
    text: str,
    _seed: Seed,
    _people: Mapping[str, Person],
) -> bool:
    normalized = _normalize_spaces(text)
    return any(
        _normalize_spaces(anchor) not in normalized for anchor in spec.anchor_values
    )


def _answer_leak(
    spec: Question,
    text: str,
    seed: Seed,
    people: Mapping[str, Person],
) -> bool:
    answer_fact_ids = tuple(
        dict.fromkeys(
            qrel.fact_id
            for qrel in spec.qrels
            if qrel.rel == 2 and qrel.fact_id is not None
        )
    )
    resolved = [
        seed.facts[fact_id] for fact_id in answer_fact_ids if fact_id in seed.facts
    ]
    atomic_forms: list[str] = []
    narrative_sentences: list[str] = []
    for fact in resolved:
        if fact.kind == "text":
            narrative_sentences.append(str(fact.value))
        else:
            atomic_forms.extend(
                surface_forms(fact, people=people) or (str(fact.value),)
            )
    if not resolved:
        atomic_forms.extend(spec.answer_values)

    anchors = {_normalize_spaces(value).casefold() for value in spec.anchor_values}
    normalized_text = _normalize_spaces(text).casefold()
    for value in dict.fromkeys(atomic_forms):
        normalized_value = _normalize_spaces(value).casefold()
        # The subject is mandatory even when a linked multi-document answer
        # fact happens to repeat it (16 step-2 specs do). Identifier lookup keys
        # are the common case called out by ADR-025; the same invariant keeps
        # every required anchor draftable.
        if normalized_value in anchors:
            continue
        if normalized_value and normalized_value in normalized_text:
            return True

    without_subject = _normalize_spaces(text)
    for anchor in spec.anchor_values:
        without_subject = without_subject.replace(_normalize_spaces(anchor), " ")
    question_bigrams = _content_bigrams(without_subject)
    if not question_bigrams:
        return False
    for sentence in narrative_sentences:
        answer_bigrams = set(_content_bigrams(sentence))
        overlap = sum(bigram in answer_bigrams for bigram in question_bigrams)
        if overlap / len(question_bigrams) > 0.5:
            return True
    return False


def _as_of_missing(
    spec: Question,
    text: str,
    _seed: Seed,
    _people: Mapping[str, Person],
) -> bool:
    if spec.type != "temporal" or spec.as_of is None:
        return False
    normalized = _normalize_spaces(text)
    return not any(
        _normalize_spaces(form) in normalized for form in _date_forms(spec.as_of)
    )


def _prompt_artifact(
    _spec: Question,
    text: str,
    _seed: Seed,
    _people: Mapping[str, Person],
) -> bool:
    return bool(
        _PERSON_ID_RE.search(text)
        or _FACT_ID_RE.search(text)
        or _FACT_WORD_RE.search(text)
        or "명세" in text
        or "주어 값" in text
    )


def _overlap_exceeds(metrics: Mapping[str, object]) -> bool:
    containment = metrics.get("containment")
    lcs = metrics.get("lcs")
    return (
        isinstance(containment, (int, float))
        and not isinstance(containment, bool)
        and containment > CONTAINMENT_CAP
    ) or (isinstance(lcs, int) and not isinstance(lcs, bool) and lcs > LCS_CAP)


def _normalize_spaces(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _content_bigrams(value: str) -> list[str]:
    content = "".join(
        character.casefold()
        for character in unicodedata.normalize("NFC", value)
        if character.isalnum()
    )
    return [content[index : index + 2] for index in range(len(content) - 1)]


def _date_forms(value: str) -> tuple[str, ...]:
    parsed = date.fromisoformat(value)
    return (
        parsed.isoformat(),
        f"{parsed.year}.{parsed.month}.{parsed.day}",
        f"{parsed.year}. {parsed.month}. {parsed.day}.",
        f"{parsed.year}년 {parsed.month}월 {parsed.day}일",
        f"{parsed.month}월 {parsed.day}일",
    )


def _draft_history(root: Path) -> dict[str, dict[int, dict[str, object]]]:
    history: dict[str, dict[int, dict[str, object]]] = defaultdict(dict)
    records = load_records(root / DRAFT_LOG)
    stale = {
        record.get("custom_id")
        for record in records
        if record.get("status") == "stale_spec"
    }
    for record in records:
        if record.get("custom_id") in stale:
            continue
        spec_id = record.get("id")
        attempt = record.get("attempt")
        if isinstance(spec_id, str) and isinstance(attempt, int) and attempt >= 1:
            history[spec_id][attempt] = record
    directory = root / DRAFTS_DIR
    if not directory.exists():
        return history
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"초안 캐시는 객체여야 합니다: {path}")
        if payload.get("custom_id") in stale:
            continue
        spec_id = payload.get("id")
        attempt = payload.get("attempt")
        if isinstance(spec_id, str) and isinstance(attempt, int) and attempt >= 1:
            history[spec_id][attempt] = payload
    return history


def _write_validation_outputs(
    root: Path,
    *,
    specs: Sequence[Question],
    seed: Seed,
    people: Mapping[str, Person],
    docs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    by_id = {spec.id: spec for spec in specs}
    attempts: dict[str, list[dict[str, object]]] = defaultdict(list)
    for path in sorted((root / DRAFTS_DIR).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"초안 캐시는 객체여야 합니다: {path}")
        spec_id = payload.get("id")
        attempt = payload.get("attempt")
        if not isinstance(spec_id, str) or spec_id not in by_id:
            raise ValueError(f"초안 캐시의 문항이 명세에 없습니다: {path}")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError(f"초안 캐시의 attempt가 잘못되었습니다: {path}")
        spec = by_id[spec_id]
        metrics: dict[str, float | int | None] = {
            "containment": None,
            "lcs": None,
            "target_chars": 0,
        }
        question: str | None = None
        if payload.get("result_type") == "succeeded":
            text = payload.get("text")
            if not isinstance(text, str):
                raise ValueError(f"succeeded 초안에 text가 없습니다: {path}")
            question = text.strip()
            metrics = overlap(spec, question, docs)
            codes = validate_draft(
                spec,
                question,
                seed=seed,
                people=people,
                docs=docs,
            )
        else:
            codes = [f"batch_{payload.get('result_type', 'missing')}"]
        attempts[spec_id].append(
            {
                "attempt": attempt,
                "path": path.name,
                "question": question,
                "codes": codes,
                "metrics": metrics,
            }
        )

    validated: list[Question] = []
    failed = 0
    by_code: Counter[str] = Counter()
    overlap_rows: list[dict[str, object]] = []
    validated_overlap_n = 0
    for spec_id in sorted(attempts):
        spec = by_id[spec_id]
        outcomes = sorted(
            attempts[spec_id],
            key=lambda item: (int(item["attempt"]), str(item["path"])),
        )
        passing = next((item for item in outcomes if not item["codes"]), None)
        selected = passing if passing is not None else outcomes[-1]
        metrics = selected["metrics"]
        assert isinstance(metrics, Mapping)
        overlap_rows.append({"id": spec.id, "type": spec.type, **metrics})
        if passing is not None:
            question = passing["question"]
            assert isinstance(question, str)
            passing_metrics = passing["metrics"]
            assert isinstance(passing_metrics, Mapping)
            validated.append(_tagged_question(spec, question, passing_metrics))
            containment = passing_metrics.get("containment")
            if isinstance(containment, (int, float)) and not isinstance(
                containment, bool
            ):
                validated_overlap_n += 1
        else:
            failed += 1
            codes = selected["codes"]
            assert isinstance(codes, Sequence)
            by_code.update(str(code) for code in codes)

    validated.sort(key=lambda item: item.id)
    write_questions(root / VALIDATION_PATH, validated)
    mismatch_n = sum("lexical_mismatch" in question.tags for question in validated)
    summary: dict[str, object] = {
        "validated": len(validated),
        "failed": failed,
        "by_code": dict(sorted(by_code.items())),
        "overlap": summarize(overlap_rows),
        "caps": {
            "containment": CONTAINMENT_CAP,
            "lcs": LCS_CAP,
            "mismatch_max": MISMATCH_MAX,
        },
        "lexical_mismatch": {
            "n": mismatch_n,
            "share": mismatch_n / validated_overlap_n if validated_overlap_n else None,
        },
    }
    _write_json(root / VALIDATION_SUMMARY_PATH, summary)
    return summary


def _tagged_question(
    spec: Question,
    question: str,
    metrics: Mapping[str, object],
) -> Question:
    tags = tuple(tag for tag in spec.tags if tag != "lexical_mismatch")
    containment = metrics.get("containment")
    if (
        isinstance(containment, (int, float))
        and not isinstance(containment, bool)
        and containment <= MISMATCH_MAX
    ):
        tags = (*tags, "lexical_mismatch")
    return replace(spec, question=question, tags=tags)


def _other_template(record: Mapping[str, object], current_hash: str) -> bool:
    recorded = record.get("template_hash")
    return isinstance(recorded, str) and recorded != current_hash


def _cost_estimates(
    requests: Sequence[BatchRequest], config: GenerationConfig
) -> dict[str, int | float]:
    input_tokens = sum(
        math.ceil(
            (len(request.system) + len(request.user)) / config.chars_per_token_estimate
        )
        for request in requests
    )
    maximum_output_tokens = len(requests) * config.max_tokens
    expected_cost = estimate_usd(f"{SERVICE_PREFIX}.input", input_tokens)
    maximum_cost = expected_cost + estimate_usd(
        f"{SERVICE_PREFIX}.output", maximum_output_tokens
    )
    budget_maximum = budget_estimate_usd(
        f"{SERVICE_PREFIX}.input", input_tokens
    ) + budget_estimate_usd(f"{SERVICE_PREFIX}.output", maximum_output_tokens)
    return {
        "estimated_input_tokens": input_tokens,
        "maximum_output_tokens": maximum_output_tokens,
        "expected_cost_usd": expected_cost,
        "maximum_cost_usd": maximum_cost,
        "budget_maximum_cost_usd": budget_maximum,
    }


def _print_plan(
    targets: Sequence[tuple[Question, int]],
    *,
    estimates: Mapping[str, int | float],
    month_total: float,
    monthly_budget_usd: float,
    remaining_after: float,
    print_fn: PrintFn,
) -> None:
    type_counts = dict(sorted(Counter(spec.type for spec, _ in targets).items()))
    print_fn(f"질문 초안 대상 {len(targets)}건 / 유형별 {type_counts}")
    print_fn(
        f"추정 입력 {int(estimates['estimated_input_tokens']):,}토큰 / 최대 출력 "
        f"{int(estimates['maximum_output_tokens']):,}토큰 / "
        "예상 비용(Batch, VAT 포함) "
        f"${float(estimates['expected_cost_usd']):.4f} / 최대 "
        f"${float(estimates['maximum_cost_usd']):.4f} / 이번 달 누적 "
        f"${month_total:.2f} / 상한 ${monthly_budget_usd:.2f} / "
        f"실행 후 남은 예산 ${remaining_after:.2f} (최대 비용·여유 포함)"
    )


def _append_dry_run(
    root: Path,
    *,
    targets: Sequence[tuple[Question, int]],
    estimates: Mapping[str, int | float],
    month_total: float,
    monthly_budget_usd: float,
    remaining_after: float,
) -> None:
    append_record(
        root / DRY_RUNS_PATH,
        {
            "ts": _utc_now(),
            "n_requests": len(targets),
            "type_counts": dict(
                sorted(Counter(spec.type for spec, _ in targets).items())
            ),
            "estimated_input_tokens": estimates["estimated_input_tokens"],
            "maximum_output_tokens": estimates["maximum_output_tokens"],
            "expected_cost_usd": estimates["expected_cost_usd"],
            "maximum_cost_usd": estimates["maximum_cost_usd"],
            "budget_estimate_usd": estimates["budget_maximum_cost_usd"],
            "month_total_usd": month_total,
            "monthly_budget_usd": monthly_budget_usd,
            "remaining_after_usd": remaining_after,
        },
        required=_DRY_RUN_REQUIRED,
    )


def _handle_open_batches(
    root: Path,
    *,
    opened: Sequence[Mapping[str, object]],
    client: AnthropicBatchClient,
    config: GenerationConfig,
    print_fn: PrintFn,
) -> int:
    in_progress = False
    save_failed = False
    for record in opened:
        batch_id = record.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            print_fn("열린 Batch 기록에 batch_id가 없어 중단합니다.")
            return 2
        status = client.status(batch_id)
        print_fn(
            f"Batch {batch_id}: {status.processing_status}, "
            f"요청 상태 {dict(status.request_counts)}"
        )
        for warning in status.warnings:
            print_fn(f"경고: {warning}")
        if status.processing_status != "ended":
            in_progress = True
            continue
        if not _collect_batch(
            root,
            batch_id=batch_id,
            client=client,
            config=config,
            print_fn=print_fn,
        ):
            save_failed = True
    if in_progress:
        print_fn("진행 중입니다. 나중에 `make queries`를 다시 실행하세요.")
    return 1 if save_failed else 0


def _collect_batch(
    root: Path,
    *,
    batch_id: str,
    client: AnthropicBatchClient,
    config: GenerationConfig,
    print_fn: PrintFn,
) -> bool:
    specs = load_questions(root / SPECS_PATH)
    by_id = {spec.id: spec for spec in specs}
    seed = load_seed(root)
    people = {person.person_id: person for person in seed.people}
    docs = load_documents(root / DOCS_PATH)
    log_by_custom_id = {
        str(record.get("custom_id")): record
        for record in load_records(root / DRAFT_LOG)
        if isinstance(record.get("custom_id"), str)
    }
    failed_keys = {
        (record.get("id"), record.get("attempt"))
        for record in load_records(root / FAILURES_PATH)
    }
    results = client.collect(batch_id)
    save_failed = False
    for index, result in enumerate(results, start=1):
        try:
            spec_id, attempt = _parse_custom_id(result.custom_id)
            spec = by_id.get(spec_id)
            if spec is None:
                raise ValueError(f"custom_id의 문항이 명세에 없습니다: {spec_id}")
            request = build_request(spec, config=config, attempt=attempt)
            logged = log_by_custom_id.get(result.custom_id)
            metadata = (
                {
                    name: logged[name]
                    for name in (
                        "generation",
                        "settings_hash",
                        "template_hash",
                        "prompt_sha256",
                    )
                }
                if logged is not None
                else _request_metadata(request, config=config)
            )
            cache_key = (
                str(logged["cache_key"])
                if logged is not None and isinstance(logged.get("cache_key"), str)
                else request.cache_key
            )
            if (
                logged is not None
                and logged.get("prompt_sha256")
                != _request_metadata(request, config=config)["prompt_sha256"]
            ):
                _append_draft_log(
                    root,
                    {
                        **metadata,
                        "id": spec_id,
                        "attempt": attempt,
                        "custom_id": result.custom_id,
                        "cache_key": cache_key,
                        "batch_id": batch_id,
                        "status": "stale_spec",
                        "ts": _utc_now(),
                    },
                )
                _progress(print_fn, index, len(results), "결과 저장")
                continue
            cache_path = root / DRAFTS_DIR / f"{result.custom_id}.json"
            if cache_path.exists():
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(cache, dict):
                    raise TypeError(f"초안 캐시는 객체여야 합니다: {cache_path}")
            else:
                cache = {
                    "id": spec_id,
                    "attempt": attempt,
                    "custom_id": result.custom_id,
                    "cache_key": cache_key,
                    "batch_id": batch_id,
                    "result_type": result.result_type,
                    "text": result.text if result.result_type == "succeeded" else None,
                    "model": result.model,
                    "stop_reason": result.stop_reason,
                    "usage": dict(result.usage) if result.usage is not None else None,
                    "error": result.error,
                    **metadata,
                    "collected_at": _utc_now(),
                }
                _write_json(cache_path, cache)

            codes: list[str]
            if cache.get("result_type") == "succeeded":
                text = cache.get("text")
                if not isinstance(text, str):
                    raise ValueError("succeeded 초안에 text가 없습니다")
                question = text.strip()
                codes = validate_draft(
                    spec,
                    question,
                    seed=seed,
                    people=people,
                    docs=docs,
                )
            else:
                codes = [f"batch_{cache.get('result_type', 'missing')}"]
            if codes and (spec_id, attempt) not in failed_keys:
                append_record(
                    root / FAILURES_PATH,
                    {
                        "id": spec_id,
                        "attempt": attempt,
                        "codes": codes,
                        "ts": _utc_now(),
                    },
                    required=_FAILURE_REQUIRED,
                )
                failed_keys.add((spec_id, attempt))
            _append_draft_log(
                root,
                {
                    **metadata,
                    "id": spec_id,
                    "attempt": attempt,
                    "custom_id": result.custom_id,
                    "cache_key": cache_key,
                    "batch_id": batch_id,
                    "status": "stored",
                    "result_type": cache.get("result_type"),
                    "validation_codes": codes,
                    "ts": _utc_now(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - isolate each persisted result
            save_failed = True
            print_fn(
                f"결과 저장 실패: {result.custom_id} ({type(exc).__name__}) — 계속 진행합니다."
            )
        _progress(print_fn, index, len(results), "결과 저장")
    _write_validation_outputs(
        root,
        specs=specs,
        seed=seed,
        people=people,
        docs=docs,
    )
    if not save_failed:
        client.mark_collected(batch_id)
    else:
        print_fn("저장 실패가 있어 Batch를 collected로 표시하지 않았습니다.")
    return not save_failed


def _append_draft_log(root: Path, record: Mapping[str, object]) -> None:
    append_record(root / DRAFT_LOG, record, required=_DRAFT_LOG_REQUIRED)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_custom_id(custom_id: str) -> tuple[str, int]:
    match = _CUSTOM_ID_RE.fullmatch(custom_id)
    if match is None:
        raise ValueError(f"custom_id에서 문항과 시도를 읽을 수 없습니다: {custom_id!r}")
    return match.group("id"), int(match.group("attempt"))


def _print_status(client: AnthropicBatchClient) -> int:
    uncertain = client.uncertain_submissions()
    if uncertain:
        print(
            "불명 제출: "
            + ", ".join(str(record.get("submission_id", "?")) for record in uncertain)
        )
    opened = client.open_batches()
    for record in opened:
        batch_id = record.get("batch_id")
        if isinstance(batch_id, str):
            status = client.status(batch_id)
            print(
                f"Batch {batch_id}: {status.processing_status} "
                f"{dict(status.request_counts)}"
            )
    if not uncertain and not opened:
        print("불명 제출이나 열린 Batch가 없습니다.")
    return 2 if uncertain else 0


def _progress(print_fn: PrintFn, index: int, total: int, label: str) -> None:
    if total and (index % 20 == 0 or index == total):
        print_fn(f"[{index}/{total}] {label}", flush=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다")
    return parsed


def _first_line(value: str) -> str:
    return value.splitlines()[0][:300] if value else "unknown error"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
