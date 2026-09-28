"""Render and resume Anthropic Batch generation for synthetic documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kodocs.config import DEFAULT_MONTHLY_BUDGET_USD, MissingAPIKeyError, load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
    budget_estimate_usd,
    estimate_usd,
)
from kodocs.ingest.manifest import append_record, latest_records, load_records
from kodocs.llm.anthropic_batch import (
    BATCHES_MANIFEST,
    CUSTOM_ID_RE,
    SERVICE_PREFIX,
    AnthropicBatchClient,
    BatchRequest,
    SubmitRejectedError,
    SubmitUncertainError,
    anthropic_client_factory,
)
from kodocs.llm.settings import GenerationConfig, load_generation_config
from kodocs.synth.plan import CANARIES_PATH, DOC_TYPES, PLAN_PATH, DocPlan
from kodocs.synth.seed import (
    GRADES,
    TEAMS,
    Access,
    Fact,
    Seed,
    load_seed,
    surface_forms,
)

GENERATIONS_DIR = Path("data/synthetic/generations")
GENERATION_LOG = Path("data/synthetic/generation_log.jsonl")
VALIDATION_FAILURES = Path("data/synthetic/validation_failures.jsonl")
DOCS_DIR = Path("data/synthetic/docs")
TEMPLATES_DIR = Path(__file__).with_name("templates")

_GENERATION_LOG_REQUIRED = {
    "doc_id",
    "attempt",
    "custom_id",
    "cache_key",
    "batch_id",
    "status",
    "ts",
}
_FACT_KIND_NAMES = {
    "id": "식별자",
    "date": "날짜",
    "datetime": "일시",
    "amount": "금액",
    "person": "인물",
    "service": "서비스",
    "count": "건수",
    "text": "서술",
    "regulation": "규정",
}
_CUSTOM_ID_ATTEMPT_RE = re.compile(
    r"^(?P<doc_id>[a-zA-Z0-9_-]+)-a(?P<attempt>[1-9]\d*)$"
)

type PrintFn = Callable[..., object]
type PostCollectionFn = Callable[[Path], int]
type PostCollectionStage = tuple[str, PostCollectionFn]


def template_hash(doc_type: str, *, templates_dir: Path | None = None) -> str:
    """Hash the exact system and type-template bytes with an unambiguous join."""
    directory = templates_dir or TEMPLATES_DIR
    if doc_type not in DOC_TYPES:
        raise ValueError(f"지원하지 않는 문서 유형: {doc_type}")
    system_bytes = (directory / "system.md").read_bytes()
    user_bytes = (directory / f"{doc_type}.md").read_bytes()
    return hashlib.sha256(system_bytes + b"\0" + user_bytes).hexdigest()


def cache_key(
    *,
    template_hash: str,
    facts: Sequence[Fact],
    doc_seed: int,
    attempt: int,
    config: GenerationConfig,
    prompt_sha256: str = "",
) -> str:
    """Return the output-determining cache key from full fact values and settings."""
    fact_digests = sorted(
        (
            fact.fact_id,
            hashlib.sha256(_canonical_json(asdict(fact)).encode("utf-8")).hexdigest(),
        )
        for fact in facts
    )
    payload = {
        "template_hash": template_hash,
        "facts": fact_digests,
        "doc_seed": doc_seed,
        "attempt": attempt,
        "model": config.model,
        "settings_hash": config.settings_hash(),
        "prompt_sha256": prompt_sha256,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def build_request(
    doc: DocPlan,
    seed: Seed,
    *,
    config: GenerationConfig,
    attempt: int,
    templates_dir: Path | None = None,
) -> tuple[BatchRequest, dict[str, object]]:
    """Build one request and its persistent cache metadata without randomness."""
    return _build_request(
        doc,
        seed,
        config=config,
        attempt=attempt,
        canary_values=_default_canary_values(),
        templates_dir=templates_dir,
    )


def _build_request(
    doc: DocPlan,
    seed: Seed,
    *,
    config: GenerationConfig,
    attempt: int,
    canary_values: Mapping[str, str],
    templates_dir: Path | None = None,
) -> tuple[BatchRequest, dict[str, object]]:
    if attempt < 1:
        raise ValueError("attempt는 1 이상이어야 합니다")
    directory = templates_dir or TEMPLATES_DIR
    system = (directory / "system.md").read_text(encoding="utf-8")
    template = (directory / f"{doc.doc_type}.md").read_text(encoding="utf-8")
    people = {person.person_id: person for person in seed.people}
    try:
        author = people[doc.author_id]
    except KeyError:
        raise ValueError(
            f"계획의 작성자를 시드에서 찾을 수 없습니다: {doc.author_id}"
        ) from None

    assigned_facts: list[Fact] = []
    regular_facts: list[Fact] = []
    canary_value: str | None = None
    for fact_id in doc.fact_ids:
        fact = seed.facts.get(fact_id)
        if fact is not None:
            assigned_facts.append(fact)
            regular_facts.append(fact)
            continue
        if fact_id == doc.canary_fact_id:
            canary_value = canary_values.get(fact_id)
            if canary_value is None:
                raise ValueError(f"계획의 카나리아 값을 찾을 수 없습니다: {fact_id}")
            assigned_facts.append(
                Fact(
                    fact_id=fact_id,
                    kind="text",
                    value=canary_value,
                    subject=doc.doc_id,
                    date=None,
                    sensitivity="restricted",
                    source=CANARIES_PATH.as_posix(),
                )
            )
            continue
        raise ValueError(f"계획의 사실을 시드에서 찾을 수 없습니다: {fact_id}")

    values = {
        "title_hint": doc.title_hint,
        # person_id stays in the plan/document JSON (author_id); the prompt shows
        # only what may appear in the body (pilot 1 leaked p-010 into 9 of 10).
        "author": (
            f"{author.name} {GRADES[author.grade]} ({TEAMS[author.team].name_ko}팀)"
        ),
        "date": doc.date,
        "team": f"{TEAMS[doc.team].name_ko}팀 ({doc.team})",
        "facts_block": _facts_block(doc, regular_facts, people),
        "regulations_block": "\n".join(
            f"- {reference}" for reference in doc.regulation_refs
        )
        or "- 없음",
        "canary_block": (
            f"- {doc.canary_fact_id} | 내부 코드명 | {canary_value} | "
            f'`<fact id="{doc.canary_fact_id}">…</fact>`로 표시'
            if doc.canary_fact_id is not None
            else "- 없음. 카나리아 문자열을 만들거나 추측하지 않는다."
        ),
        "style_block": _style_block(doc),
    }
    user = template.format_map(values)
    current_template_hash = template_hash(doc.doc_type, templates_dir=directory)
    prompt_sha256 = hashlib.sha256(
        system.encode("utf-8") + b"\0" + user.encode("utf-8")
    ).hexdigest()
    current_cache_key = cache_key(
        template_hash=current_template_hash,
        facts=assigned_facts,
        doc_seed=doc.seed,
        attempt=attempt,
        config=config,
        prompt_sha256=prompt_sha256,
    )
    custom_id = f"{doc.doc_id}-a{attempt}"
    if CUSTOM_ID_RE.fullmatch(custom_id) is None:
        raise ValueError(
            f"custom_id는 ^[a-zA-Z0-9_-]{{1,64}}$ 형식이어야 합니다: {custom_id!r}"
        )
    metadata: dict[str, object] = {
        "generation": config.record(),
        "template_hash": current_template_hash,
        "prompt_sha256": prompt_sha256,
        "doc_seed": doc.seed,
        "fact_ids": list(doc.fact_ids),
    }
    return (
        BatchRequest(
            custom_id=custom_id,
            system=system,
            user=user,
            cache_key=current_cache_key,
        ),
        metadata,
    )


def pending_docs(
    root: Path,
    plan: Sequence[DocPlan],
    *,
    config: GenerationConfig,
) -> list[tuple[DocPlan, int]]:
    """Return retryable documents and their next monotonically increasing attempt.

    A document whose latest attempt used other prompt templates (a template
    revision) is regenerated even if it passed. ``max_attempts`` counts only
    attempts made with the current templates, so an earlier revision's failures
    do not use up the new revision's budget; attempt numbers keep increasing.
    """
    history = _generation_history(root)
    failures = _validation_failures(root)
    current_hashes: dict[str, str] = {}
    pending: list[tuple[DocPlan, int]] = []
    for doc in plan:
        attempts = history.get(doc.doc_id, {})
        if not attempts:
            pending.append((doc, 1))
            continue
        current = current_hashes.setdefault(doc.doc_type, template_hash(doc.doc_type))
        latest_attempt = max(attempts)
        latest = attempts[latest_attempt]
        if not _is_other_revision(latest, current):
            if _validated_doc_matches(root, doc.doc_id, latest):
                continue
            failed_validation = failures.get((doc.doc_id, latest_attempt), False)
            if (
                latest.get("_from_cache")
                and latest.get("result_type") == "succeeded"
                and not failed_validation
            ):
                continue
        if _revision_attempts(attempts, current) >= config.max_attempts:
            continue
        pending.append((doc, latest_attempt + 1))
    return pending


def _is_other_revision(record: Mapping[str, object], current_hash: str) -> bool:
    recorded = record.get("template_hash")
    return isinstance(recorded, str) and recorded != current_hash


def _revision_attempts(
    attempts: Mapping[int, Mapping[str, object]], current_hash: str
) -> int:
    return sum(
        1
        for record in attempts.values()
        if not _is_other_revision(record, current_hash)
    )


def run(
    root: Path,
    *,
    client: AnthropicBatchClient | None,
    config: GenerationConfig,
    dry_run: bool,
    limit: int | None,
    assume_yes: bool,
    input_fn: Callable[[str], str] = input,
    print_fn: PrintFn = print,
    only: Sequence[str] | None = None,
    monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD,
    after_collection: PostCollectionFn | None = None,
    only_attempted: bool = False,
) -> int:
    """Advance generation by exactly one safe state-machine transition."""
    root = Path(root)
    if limit is not None and limit < 1:
        raise ValueError("limit는 1 이상이어야 합니다")

    if client is not None:
        uncertain = client.uncertain_submissions()
        if uncertain:
            identifiers = ", ".join(
                str(row.get("submission_id", "?")) for row in uncertain
            )
            print_fn(
                "불명 제출이 있어 새 Batch를 제출하지 않습니다: "
                f"{identifiers}. 콘솔 확인 후 --adopt-batch SUBMISSION_ID BATCH_ID "
                "또는 --abandon-submission SUBMISSION_ID --reason TEXT를 실행하세요."
            )
            return 2

        opened = client.open_batches()
        if opened:
            any_save_failure = False
            any_in_progress = False
            collected_batches = 0
            loaded: tuple[list[DocPlan], Seed, Mapping[str, str]] | None = None
            for open_record in opened:
                batch_id = open_record.get("batch_id")
                if not isinstance(batch_id, str) or not batch_id:
                    print_fn("열린 Batch 기록에 batch_id가 없어 중단합니다.")
                    return 2
                status = client.status(batch_id)
                counts = dict(status.request_counts)
                print_fn(
                    f"Batch {batch_id}: {status.processing_status}, 요청 상태 {counts}"
                )
                for warning in status.warnings:
                    print_fn(f"경고: {warning}")
                if status.processing_status != "ended":
                    any_in_progress = True
                    continue
                if loaded is None:
                    loaded = (
                        _load_plan(root),
                        load_seed(root),
                        _load_canary_values(root),
                    )
                plans, seed, canary_values = loaded
                saved = _collect_batch(
                    root,
                    batch_id=batch_id,
                    client=client,
                    config=config,
                    plans=plans,
                    seed=seed,
                    canary_values=canary_values,
                    print_fn=print_fn,
                )
                any_save_failure = any_save_failure or not saved
                collected_batches += int(saved)
            if any_in_progress:
                command = _rerun_command(limit, only_attempted)
                print_fn(f"진행 중입니다. 나중에 `{command}`를 다시 실행하세요.")
                return 1 if any_save_failure else 0
            if any_save_failure:
                return 1
            if (
                collected_batches
                and after_collection is not None
                and any((root / GENERATIONS_DIR).glob("*.json"))
            ):
                return after_collection(root)
            return 0

    plans = _load_plan(root)
    seed = load_seed(root)
    canary_values = _load_canary_values(root)
    selected_scope = _only_plans(plans, only)
    if only_attempted:
        attempted = _generation_history(root)
        selected_scope = [plan for plan in selected_scope if plan.doc_id in attempted]
    targets = pending_docs(root, selected_scope, config=config)
    exhausted = _exhausted_docs(root, selected_scope, config=config)
    if limit is not None:
        targets = _balanced_limit(targets, limit)

    if not targets:
        if not any((root / GENERATIONS_DIR).glob("*.json")):
            print_fn("생성 0건: 계획 또는 생성 대상이 없고 생성 캐시도 없습니다.")
            return 1
        _print_completion_summary(root, selected_scope, exhausted, print_fn)
        return after_collection(root) if after_collection is not None else 0

    requests: list[BatchRequest] = []
    metadata_by_custom_id: dict[str, dict[str, object]] = {}
    total = len(targets)
    for index, (doc, attempt) in enumerate(targets, start=1):
        request, metadata = _build_request(
            doc,
            seed,
            config=config,
            attempt=attempt,
            canary_values=canary_values,
        )
        cache_path = root / GENERATIONS_DIR / f"{request.cache_key}.json"
        if cache_path.exists():
            print_fn(
                f"{doc.doc_id} 시도 {attempt}의 동일 캐시가 있어 요청에서 제외합니다."
            )
        else:
            requests.append(request)
            metadata_by_custom_id[request.custom_id] = metadata
        _progress(print_fn, index, total, "요청 생성")

    if not requests:
        _print_completion_summary(root, selected_scope, exhausted, print_fn)
        return after_collection(root) if after_collection is not None else 0

    ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
    estimates = _cost_estimates(requests, targets, config)
    month_total = ledger.month_total_usd()
    remaining_after = monthly_budget_usd - month_total - estimates["budget_max"]
    _print_plan(
        targets,
        requests=requests,
        estimates=estimates,
        month_total=month_total,
        monthly_budget_usd=monthly_budget_usd,
        remaining_after=remaining_after,
        print_fn=print_fn,
    )
    if exhausted:
        print_fn(f"exhausted {len(exhausted)}건: {', '.join(exhausted)}")
    if estimates["budget_max"] > monthly_budget_usd - month_total:
        print_fn("예산 초과: 여유를 포함한 최대 비용이 이번 달 남은 예산을 넘습니다.")
        return 1
    if dry_run:
        return 0
    if client is None:
        print_fn("Anthropic Batch 클라이언트가 없어 제출할 수 없습니다.")
        return 2
    if not assume_yes and input_fn("계속하려면 y 입력: ").strip().casefold() != "y":
        print_fn("사용자가 생성을 취소했습니다.")
        return 0

    try:
        batch_id = client.submit(requests)
    except (SubmitUncertainError, SubmitRejectedError, BudgetExceededError) as exc:
        print_fn(f"Batch 제출 중단: {_first_line(str(exc))}")
        return 2
    print_fn(f"Batch 제출 완료: {batch_id}")
    timestamp = _utc_now()
    for index, request in enumerate(requests, start=1):
        doc_id, attempt = _parse_custom_id(request.custom_id)
        _append_generation_log(
            root,
            {
                **metadata_by_custom_id[request.custom_id],
                "doc_id": doc_id,
                "attempt": attempt,
                "custom_id": request.custom_id,
                "cache_key": request.cache_key,
                "batch_id": batch_id,
                "status": "submitted",
                "ts": timestamp,
            },
        )
        _progress(print_fn, index, len(requests), "제출 기록")
    command = _rerun_command(limit, only_attempted)
    print_fn(
        f"결과는 최대 24시간 뒤 준비됩니다. `{command}`를 다시 실행하면 수거합니다."
    )
    return 0


def _rerun_command(limit: int | None, only_attempted: bool) -> str:
    """The make target that ran this invocation, to repeat for collection."""
    if limit is not None:
        return "make generate-pilot"
    if only_attempted:
        return "make generate-pilot-rerun"
    return "make generate"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; paid operations require a key and explicit confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--then-validate",
        action="store_true",
        help="수거 완료 후 검증, 품질 지표, 결과 문서를 차례로 생성",
    )
    parser.add_argument("--only", nargs="+", metavar="DOC_ID")
    parser.add_argument(
        "--only-attempted",
        action="store_true",
        help="이미 생성을 시도한 문서만 대상으로 한다(파일럿 재실행)",
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--adopt-batch",
        nargs=2,
        metavar=("SUBMISSION_ID", "BATCH_ID"),
    )
    parser.add_argument("--abandon-submission", metavar="SUBMISSION_ID")
    parser.add_argument("--reason", metavar="TEXT")
    args = parser.parse_args(argv)
    if args.dry_run and (
        args.status
        or args.adopt_batch
        or args.abandon_submission
        or args.reason
        or args.then_validate
    ):
        parser.error("--dry-run은 상태 해결 옵션과 함께 쓸 수 없습니다")
    if (args.abandon_submission is None) != (args.reason is None):
        parser.error("--abandon-submission과 --reason은 함께 지정해야 합니다")
    action_count = sum(
        bool(value)
        for value in (args.status, args.adopt_batch, args.abandon_submission)
    )
    if action_count > 1:
        parser.error(
            "--status, --adopt-batch, --abandon-submission 중 하나만 지정하세요"
        )
    if args.then_validate and action_count:
        parser.error("--then-validate는 상태 해결 옵션과 함께 쓸 수 없습니다")

    root = args.root.resolve()
    config = load_generation_config()
    if args.dry_run:
        # Dry-run intentionally ignores .env and never requires a credential.
        settings = load_settings(os.environ)
        try:
            return run(
                root,
                client=None,
                config=config,
                dry_run=True,
                limit=args.limit,
                assume_yes=args.yes,
                only=args.only,
                monthly_budget_usd=settings.monthly_budget_usd,
                only_attempted=args.only_attempted,
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"생성 계획 실패: {_first_line(str(exc))}")
            return 1

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
    try:
        if args.adopt_batch:
            submission_id, batch_id = args.adopt_batch
            client.adopt(submission_id, batch_id)
            print(f"불명 제출을 Batch에 연결했습니다: {submission_id} -> {batch_id}")
            return 0
        if args.abandon_submission:
            client.abandon(args.abandon_submission, args.reason)
            print(f"불명 제출을 포기 처리했습니다: {args.abandon_submission}")
            return 0
        if args.status:
            return _print_status(client)
        return run(
            root,
            client=client,
            config=config,
            dry_run=False,
            limit=args.limit,
            assume_yes=args.yes,
            only=args.only,
            monthly_budget_usd=settings.monthly_budget_usd,
            after_collection=_run_post_collection if args.then_validate else None,
            only_attempted=args.only_attempted,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"생성 실행 실패: {_first_line(str(exc))}")
        return 1


def _run_post_collection(
    root: Path,
    *,
    stages: Sequence[PostCollectionStage] | None = None,
) -> int:
    """Run deterministic post-collection stages in order, stopping on failure."""
    if stages is None:
        from kodocs.eval import synth_metrics, synth_report
        from kodocs.synth import validate

        stages = (
            ("검증", lambda stage_root: validate.run(stage_root)),
            ("품질 지표", lambda stage_root: synth_metrics.run(stage_root)),
            (
                "결과 문서",
                lambda stage_root: synth_report.main(
                    ["--root", str(stage_root.resolve())]
                ),
            ),
        )
    for label, stage in stages:
        print(f"수거 후 {label} 실행")
        code = stage(root)
        if code != 0:
            print(f"수거 후 {label} 실패(exit={code})")
            return code
    return 0


def _facts_block(
    doc: DocPlan,
    facts: Sequence[Fact],
    people: Mapping[str, Any],
) -> str:
    lines: list[str] = []
    for fact in facts:
        if fact.kind == "person":
            person = people[str(fact.value)]
            value = (
                f"{person.name} {GRADES[person.grade]} ({TEAMS[person.team].name_ko}팀)"
            )
        else:
            value = str(fact.value)
        forms = surface_forms(fact, people=people)
        allowed = " / ".join(forms) if forms else "서술 요지를 자연스럽게"
        age = doc.fact_age_days.get(fact.fact_id)
        phrase = doc.fact_time_phrases.get(fact.fact_id)
        # Only the decided phrase and the date; helper wording such as the day
        # count was copied into bodies in pilot 1. The phrase and date are
        # labelled as outside the marker: pilot 2 wrapped them in the fact id.
        age_text = (
            f" | 표시 밖 시점 표현: {phrase} (사실 날짜 {fact.date.isoformat()})"
            if age is not None and phrase is not None and fact.date is not None
            else ""
        )
        lines.append(
            f"- {fact.fact_id} | {_FACT_KIND_NAMES[fact.kind]} | {value} | "
            f"표시 안 허용 표기: {allowed}{age_text}"
        )
    return "\n".join(lines) or "- 없음"


def _style_block(doc: DocPlan) -> str:
    required = ("tone", "structure", "persona", "frame", "length_target_chars")
    missing = [name for name in required if name not in doc.style]
    if missing:
        raise ValueError(f"계획의 style 필드가 없습니다: {', '.join(missing)}")
    lines = [
        f"- 문체: {doc.style['tone']}",
        f"- 구성: {doc.style['structure']}",
        f"- 작성 관점: {doc.style['persona']}",
        f"- 길이 목표: 약 {doc.style['length_target_chars']}자",
    ]
    if doc.style["frame"] == "회고":
        lines.append("- 맥락: 오래전 장애를 되돌아보는 회고 회의로 작성한다.")
    else:
        lines.append("- 맥락: 현재 문서 날짜를 기준으로 일반 업무 문서로 작성한다.")
    return "\n".join(lines)


def _default_canary_values() -> Mapping[str, str]:
    return _load_canary_values(Path.cwd())


def _load_canary_values(root: Path) -> dict[str, str]:
    path = root / CANARIES_PATH
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("canaries") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise TypeError(f"카나리아 레지스트리 형식이 잘못되었습니다: {path}")
    values: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"카나리아 레지스트리 행 형식이 잘못되었습니다: {path}")
        fact_id = row.get("fact_id")
        value = row.get("value")
        if not isinstance(fact_id, str) or not isinstance(value, str):
            raise TypeError(f"카나리아 fact_id/value가 잘못되었습니다: {path}")
        values[fact_id] = value
    return values


def _load_plan(root: Path) -> list[DocPlan]:
    path = root / PLAN_PATH
    if not path.exists():
        return []
    plans: list[DocPlan] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                access_record = record["access"]
                if not isinstance(access_record, Mapping):
                    raise TypeError("access는 객체여야 합니다")
                plans.append(
                    DocPlan(
                        doc_id=str(record["doc_id"]),
                        doc_type=str(record["doc_type"]),
                        team=str(record["team"]),
                        access=Access(
                            str(access_record["level"]),  # type: ignore[arg-type]
                            tuple(str(value) for value in access_record["teams"]),
                            int(access_record["min_grade"]),
                        ),
                        author_id=str(record["author_id"]),
                        date=str(record["date"]),
                        title_hint=str(record["title_hint"]),
                        fact_ids=tuple(str(value) for value in record["fact_ids"]),
                        regulation_refs=tuple(
                            str(value) for value in record["regulation_refs"]
                        ),
                        canary_fact_id=(
                            None
                            if record.get("canary_fact_id") is None
                            else str(record["canary_fact_id"])
                        ),
                        injection_id=(
                            None
                            if record.get("injection_id") is None
                            else str(record["injection_id"])
                        ),
                        style=dict(record["style"]),
                        seed=int(record["seed"]),
                        related_doc_ids=tuple(
                            str(value) for value in record["related_doc_ids"]
                        ),
                        fact_age_days={
                            str(key): int(value)
                            for key, value in record["fact_age_days"].items()
                        },
                        fact_time_phrases={
                            str(key): str(value)
                            for key, value in record["fact_time_phrases"].items()
                        },
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"생성 계획 형식 오류 {path} {line_number}행: {exc}"
                ) from exc
    return plans


def _generation_history(root: Path) -> dict[str, dict[int, dict[str, object]]]:
    history: dict[str, dict[int, dict[str, object]]] = defaultdict(dict)
    for record in latest_records(
        load_records(root / GENERATION_LOG),
        key=lambda row: (row.get("doc_id"), row.get("attempt")),
    ):
        doc_id = record.get("doc_id")
        attempt = record.get("attempt")
        if isinstance(doc_id, str) and isinstance(attempt, int) and attempt >= 1:
            history[doc_id][attempt] = dict(record)
    generations_dir = root / GENERATIONS_DIR
    if generations_dir.exists():
        for path in sorted(generations_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # The validator records this attempt as internal_error. Keep the
                # append-only log authoritative so the next attempt can proceed.
                continue
            if not isinstance(payload, dict):
                continue
            doc_id = payload.get("doc_id")
            attempt = payload.get("attempt")
            if (
                not isinstance(doc_id, str)
                or not isinstance(attempt, int)
                or attempt < 1
            ):
                continue
            history[doc_id][attempt] = {**payload, "_from_cache": True}
    return history


def _validation_failures(root: Path) -> dict[tuple[str, int], bool]:
    path = root / VALIDATION_FAILURES
    failures: dict[tuple[str, int], bool] = {}
    for record in latest_records(
        load_records(path),
        key=lambda row: (row.get("doc_id"), row.get("attempt")),
    ):
        doc_id = record.get("doc_id")
        attempt = record.get("attempt")
        if not isinstance(doc_id, str) or not isinstance(attempt, int):
            continue
        status = str(record.get("status", record.get("result", ""))).casefold()
        failures[(doc_id, attempt)] = (
            status in {"failed", "failure", "invalid", "error"}
            or record.get("passed") is False
            or record.get("valid") is False
            or bool(record.get("issues"))
        )
    return failures


def _validated_doc_matches(
    root: Path, doc_id: str, latest: Mapping[str, object]
) -> bool:
    path = root / DOCS_DIR / f"{doc_id}.json"
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or not isinstance(latest.get("cache_key"), str):
        return False
    generation = payload.get("generation")
    nested_cache_key = (
        generation.get("cache_key") if isinstance(generation, Mapping) else None
    )
    return payload.get("cache_key") == latest.get(
        "cache_key"
    ) or nested_cache_key == latest.get("cache_key")


def _exhausted_docs(
    root: Path, plans: Sequence[DocPlan], *, config: GenerationConfig
) -> list[str]:
    history = _generation_history(root)
    failures = _validation_failures(root)
    exhausted: list[str] = []
    for doc in plans:
        attempts = history.get(doc.doc_id, {})
        current = template_hash(doc.doc_type)
        if _revision_attempts(attempts, current) < config.max_attempts:
            continue
        latest_attempt = max(attempts)
        latest = attempts[latest_attempt]
        if _is_other_revision(latest, current):
            continue
        succeeded = (
            latest.get("_from_cache") and latest.get("result_type") == "succeeded"
        )
        if not succeeded or failures.get((doc.doc_id, latest_attempt), False):
            exhausted.append(doc.doc_id)
    return exhausted


def _collect_batch(
    root: Path,
    *,
    batch_id: str,
    client: AnthropicBatchClient,
    config: GenerationConfig,
    plans: Sequence[DocPlan],
    seed: Seed,
    canary_values: Mapping[str, str],
    print_fn: PrintFn,
) -> bool:
    results = client.collect(batch_id)
    plan_by_id = {plan.doc_id: plan for plan in plans}
    log_by_custom_id = {
        str(record.get("custom_id")): record
        for record in latest_records(
            load_records(root / GENERATION_LOG),
            key=lambda row: row.get("custom_id"),
        )
        if isinstance(record.get("custom_id"), str)
    }
    failed = False
    total = len(results)
    for index, result in enumerate(results, start=1):
        try:
            doc_id, attempt = _parse_custom_id(result.custom_id)
            plan = plan_by_id.get(doc_id)
            if plan is None:
                raise ValueError(f"custom_id의 문서가 계획에 없습니다: {doc_id}")
            logged = log_by_custom_id.get(result.custom_id)
            if logged is None:
                request, metadata = _build_request(
                    plan,
                    seed,
                    config=config,
                    attempt=attempt,
                    canary_values=canary_values,
                )
                current_cache_key = request.cache_key
            else:
                current_cache_key = _required_log_string(logged, "cache_key")
                metadata = {
                    name: logged[name]
                    for name in (
                        "generation",
                        "template_hash",
                        "prompt_sha256",
                        "doc_seed",
                        "fact_ids",
                    )
                }
            path = root / GENERATIONS_DIR / f"{current_cache_key}.json"
            if not path.exists():
                if result.result_type == "succeeded" and not isinstance(
                    result.text, str
                ):
                    raise ValueError("succeeded 결과의 text가 없습니다")
                record = {
                    "doc_id": doc_id,
                    "attempt": attempt,
                    "custom_id": result.custom_id,
                    "cache_key": current_cache_key,
                    "batch_id": batch_id,
                    "result_type": result.result_type,
                    "text": result.text if result.result_type == "succeeded" else None,
                    "model": result.model or config.model,
                    "stop_reason": result.stop_reason,
                    "usage": dict(result.usage) if result.usage is not None else None,
                    **metadata,
                    "collected_at": _utc_now(),
                }
                _write_generation_cache(path, record)
            _append_generation_log(
                root,
                {
                    **metadata,
                    "doc_id": doc_id,
                    "attempt": attempt,
                    "custom_id": result.custom_id,
                    "cache_key": current_cache_key,
                    "batch_id": batch_id,
                    "status": "stored",
                    "result_type": result.result_type,
                    "ts": _utc_now(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - isolate each result write
            failed = True
            fallback_doc, fallback_attempt = _safe_parse_custom_id(result.custom_id)
            context = log_by_custom_id.get(result.custom_id, {})
            _append_generation_log(
                root,
                {
                    **context,
                    "doc_id": fallback_doc,
                    "attempt": fallback_attempt,
                    "custom_id": result.custom_id,
                    "cache_key": str(context.get("cache_key", "unknown")),
                    "batch_id": batch_id,
                    "status": "store_failed",
                    "error_type": type(exc).__name__,
                    "error": _first_line(str(exc)),
                    "ts": _utc_now(),
                },
            )
            print_fn(
                f"결과 저장 실패: {result.custom_id} ({type(exc).__name__}) — 계속 진행합니다."
            )
        _progress(print_fn, index, total, "결과 저장")
    if not failed:
        client.mark_collected(batch_id)
    else:
        print_fn("저장 실패가 있어 Batch를 collected로 표시하지 않았습니다.")
    return not failed


def _write_generation_cache(path: Path, record: Mapping[str, object]) -> None:
    text = (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    _atomic_write_text(path, text)


def _append_generation_log(root: Path, record: Mapping[str, object]) -> None:
    append_record(
        root / GENERATION_LOG,
        record,
        required=_GENERATION_LOG_REQUIRED,
    )


def _only_plans(plans: Sequence[DocPlan], only: Sequence[str] | None) -> list[DocPlan]:
    if only is None:
        return list(plans)
    requested = list(dict.fromkeys(only))
    by_id = {plan.doc_id: plan for plan in plans}
    unknown = [doc_id for doc_id in requested if doc_id not in by_id]
    if unknown:
        raise ValueError(f"계획에 없는 --only 문서: {', '.join(unknown)}")
    requested_set = set(requested)
    return [plan for plan in plans if plan.doc_id in requested_set]


def _balanced_limit(
    targets: Sequence[tuple[DocPlan, int]], limit: int
) -> list[tuple[DocPlan, int]]:
    """Pick ``limit`` targets that cover as many plan strata as possible.

    Coverage goals, in priority order: every document type (so ``limit >= 6``
    always includes all six), every access level, one canary (restricted) doc,
    and one retrospective meeting. Each pick takes the earliest target that adds
    the most uncovered goals (types weigh most); remaining slots are filled by a
    round-robin over (type, access) groups. No randomness, input order decides
    ties, and the result keeps the input order.
    """
    if len(targets) <= limit:
        return list(targets)

    def goals(doc: DocPlan) -> tuple[set[str], set[str]]:
        primary = {f"type:{doc.doc_type}"}
        secondary = {f"access:{doc.access.level}"}
        if doc.canary_fact_id:
            secondary.add("canary")
        if doc.style.get("frame") == "회고":
            secondary.add("retrospective")
        return primary, secondary

    open_primary: set[str] = set()
    open_secondary: set[str] = set()
    for doc, _ in targets:
        primary, secondary = goals(doc)
        open_primary |= primary
        open_secondary |= secondary

    chosen: set[int] = set()
    while len(chosen) < limit and (open_primary or open_secondary):
        best: tuple[int, int] | None = None
        best_index: int | None = None
        for index, (doc, _) in enumerate(targets):
            if index in chosen:
                continue
            primary, secondary = goals(doc)
            score = (len(primary & open_primary), len(secondary & open_secondary))
            if score > (0, 0) and (best is None or score > best):
                best, best_index = score, index
        if best_index is None:
            break
        chosen.add(best_index)
        primary, secondary = goals(targets[best_index][0])
        open_primary -= primary
        open_secondary -= secondary

    groups: dict[tuple[str, str], deque[int]] = {}
    for index, (doc, _) in enumerate(targets):
        if index not in chosen:
            groups.setdefault((doc.doc_type, doc.access.level), deque()).append(index)
    while len(chosen) < limit:
        changed = False
        for group in groups.values():
            if group and len(chosen) < limit:
                chosen.add(group.popleft())
                changed = True
        if not changed:
            break
    return [targets[index] for index in sorted(chosen)]


def _cost_estimates(
    requests: Sequence[BatchRequest],
    targets: Sequence[tuple[DocPlan, int]],
    config: GenerationConfig,
) -> dict[str, float | int]:
    input_tokens = sum(
        math.ceil(
            (len(request.system) + len(request.user)) / config.chars_per_token_estimate
        )
        for request in requests
    )
    target_by_custom_id = {f"{doc.doc_id}-a{attempt}": doc for doc, attempt in targets}
    expected_output_tokens = sum(
        min(
            config.max_tokens,
            math.ceil(
                int(target_by_custom_id[request.custom_id].style["length_target_chars"])
                / config.chars_per_token_estimate
            ),
        )
        for request in requests
    )
    maximum_output_tokens = len(requests) * config.max_tokens
    batch_expected = estimate_usd(
        f"{SERVICE_PREFIX}.input", input_tokens
    ) + estimate_usd(f"{SERVICE_PREFIX}.output", expected_output_tokens)
    batch_maximum = estimate_usd(
        f"{SERVICE_PREFIX}.input", input_tokens
    ) + estimate_usd(f"{SERVICE_PREFIX}.output", maximum_output_tokens)
    budget_maximum = budget_estimate_usd(
        f"{SERVICE_PREFIX}.input", input_tokens
    ) + budget_estimate_usd(f"{SERVICE_PREFIX}.output", maximum_output_tokens)
    general_expected = estimate_usd(
        "anthropic.claude-sonnet-5.input", input_tokens
    ) + estimate_usd("anthropic.claude-sonnet-5.output", expected_output_tokens)
    general_maximum = estimate_usd(
        "anthropic.claude-sonnet-5.input", input_tokens
    ) + estimate_usd("anthropic.claude-sonnet-5.output", maximum_output_tokens)
    return {
        "input_tokens": input_tokens,
        "expected_output_tokens": expected_output_tokens,
        "maximum_output_tokens": maximum_output_tokens,
        "batch_expected": batch_expected,
        "batch_maximum": batch_maximum,
        "budget_max": budget_maximum,
        "general_expected": general_expected,
        "general_maximum": general_maximum,
    }


# Dry-run and confirmation list every doc_id for small (pilot-sized) runs.
_LISTED_DOC_LIMIT = 20


def _print_plan(
    targets: Sequence[tuple[DocPlan, int]],
    *,
    requests: Sequence[BatchRequest],
    estimates: Mapping[str, float | int],
    month_total: float,
    monthly_budget_usd: float,
    remaining_after: float,
    print_fn: PrintFn,
) -> None:
    requested_ids = {request.custom_id for request in requests}
    requested_docs = [
        doc for doc, attempt in targets if f"{doc.doc_id}-a{attempt}" in requested_ids
    ]
    print_fn(
        f"생성 대상 {len(requests)}건 / 유형별 "
        f"{dict(Counter(doc.doc_type for doc in requested_docs))} / 권한별 "
        f"{dict(Counter(doc.access.level for doc in requested_docs))}"
    )
    if len(requested_docs) <= _LISTED_DOC_LIMIT:
        labels = []
        for doc in requested_docs:
            tags = [doc.doc_type, doc.access.level]
            if doc.canary_fact_id:
                tags.append("카나리아")
            if doc.style.get("frame") == "회고":
                tags.append("회고")
            labels.append(f"{doc.doc_id}({'/'.join(tags)})")
        print_fn(f"선택 문서 {len(requested_docs)}건: {', '.join(labels)}")
    print_fn(
        f"추정 입력 {int(estimates['input_tokens']):,}토큰 / 최대 출력 "
        f"{int(estimates['maximum_output_tokens']):,}토큰 / 예상 비용(Batch, VAT 포함) "
        f"${float(estimates['batch_expected']):.2f} / 최대 "
        f"${float(estimates['batch_maximum']):.2f} / 일반 호출 참고 "
        f"${float(estimates['general_expected']):.2f}~"
        f"${float(estimates['general_maximum']):.2f} / 이번 달 누적 "
        f"${month_total:.2f} / 상한 ${monthly_budget_usd:.2f} / "
        f"실행 후 남은 예산 ${remaining_after:.2f} (최대 비용·여유 포함)"
    )


def _print_completion_summary(
    root: Path,
    plans: Sequence[DocPlan],
    exhausted: Sequence[str],
    print_fn: PrintFn,
) -> None:
    history = _generation_history(root)
    cached = sum(bool(history.get(plan.doc_id)) for plan in plans)
    print_fn(f"처리 완료: 계획 {len(plans)}건 중 시도 기록 {cached}건")
    if exhausted:
        print_fn(f"exhausted {len(exhausted)}건: {', '.join(exhausted)}")


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
                f"Batch {batch_id}: {status.processing_status} {dict(status.request_counts)}"
            )
    if not uncertain and not opened:
        print("불명 제출이나 열린 Batch가 없습니다.")
    return 2 if uncertain else 0


def _progress(print_fn: PrintFn, index: int, total: int, label: str) -> None:
    if total and (index % 20 == 0 or index == total):
        print_fn(f"[{index}/{total}] {label}", flush=True)


def _parse_custom_id(custom_id: str) -> tuple[str, int]:
    match = _CUSTOM_ID_ATTEMPT_RE.fullmatch(custom_id)
    if match is None:
        raise ValueError(f"custom_id에서 문서와 시도를 읽을 수 없습니다: {custom_id!r}")
    return match.group("doc_id"), int(match.group("attempt"))


def _safe_parse_custom_id(custom_id: str) -> tuple[str, int]:
    try:
        return _parse_custom_id(custom_id)
    except ValueError:
        return "unknown", 1


def _required_log_string(record: Mapping[str, object], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"생성 로그에 {name}이 없습니다")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"JSON으로 직렬화할 수 없습니다: {type(value).__name__}")


def _atomic_write_text(path: Path, text: str) -> None:
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _first_line(value: str) -> str:
    return value.splitlines()[0] if value else "unknown error"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
