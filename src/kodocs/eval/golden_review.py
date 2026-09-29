"""Build private M4 review packets and promote human-confirmed questions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import yaml

from kodocs.eval.golden import (
    ATTENTION_CHECK_PREFIX,
    GOLD_QUOTA,
    GOLD_SPLIT_QUOTA,
    SILVER_QUOTA,
    Qrel,
    Question,
    load_questions,
    question_to_dict,
    span_sha256,
)
from kodocs.eval.golden_real import article_texts
from kodocs.eval.golden_split import SPLIT_SEED_OFFSET, assign_split
from kodocs.eval.lexical import (
    CONTAINMENT_CAP,
    LCS_CAP,
    bigram_containment,
    longest_common_substring,
    overlap,
    strip_values,
)
from kodocs.ingest.manifest import sha256_file

PACKETS_DIR = Path("data/interim/golden/packets")
CLAUDE_VERDICTS = Path("data/interim/golden/claude_verdicts.jsonl")
TEMPLATE_CSV = Path("data/interim/golden/review_template.csv")
ATTENTION_KEY = Path("data/interim/golden/attention_checks.json")
REVIEW_KEYS = Path("data/interim/golden/review_keys.json")
REVIEW_GLOB = "data/golden/m4_review/*-review.csv"
GOLD_PATH = Path("data/golden/queries/gold.jsonl")
SILVER_PATH = Path("data/golden/queries/silver.jsonl")
SUMMARY_PATH = Path("data/manifest/golden_review.json")
REVIEW_ITEMS = ("q1_answerable", "q2_qrels_complete", "q3_labels_correct")
CSV_COLUMNS = (
    "review_key",
    "set",
    "type",
    "claude_q1",
    "claude_q2",
    "claude_q3",
    "claude_note",
    "reviewer",
    "reviewed_on",
    "disagree",
    "correction",
)

_SYNTHETIC_PATH = Path("data/synthetic/queries/validated.jsonl")
_REAL_DRAFTS_PATH = Path("data/interim/golden/real_drafts.jsonl")
_REAL_PROJECTION_PATH = Path("data/synthetic/queries/real_projection.jsonl")
_REAL_SUMMARY_PATH = Path("data/manifest/golden_real.json")
_DOCS_DIR = Path("data/synthetic/docs")
_LAW_DIR = Path("data/interim/law")
_TABLES_DIR = Path("data/golden/tables")
_COMPANY_PATH = Path("data/seed/company.yaml")
_PACKET_PATH = PACKETS_DIR / "review_packet.md"
_SAMPLE_SEED_OFFSET = 7_025
_PACKET_SEED_OFFSET = 7_026
_VERDICT_KEYS = ("q1", "q2", "q3")
_CORRECTION_RE = re.compile(r"^(add|remove):([^,;\s]+)$")
_LAW_ID_RE = re.compile(r"^law-(.+)$")
_QUESTION_ID_RE = re.compile(r"(?<![\w-])(?:[gs]-(?:syn|real)|chk)-\d{3}")


class GoldenReviewError(RuntimeError):
    """Review inputs or human rows violate the M4 contract."""


def sample_size(population: int) -> int:
    """20% of a type's silver questions, rounded half up, at least one."""
    return max(1, math.floor(population * 0.2 + 0.5))


def silver_sample(silver: Sequence[Question], *, random_seed: int) -> list[Question]:
    """Return a deterministic per-type sample of ``sample_size`` questions each."""
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise TypeError("random_seed는 정수여야 합니다.")
    by_type: dict[str, list[Question]] = defaultdict(list)
    for question in silver:
        if question.set != "silver":
            raise ValueError(f"실버 표본에 다른 set이 있습니다: {question.id}")
        by_type[question.type].append(question)
    unexpected = sorted(set(by_type) - set(SILVER_QUOTA))
    if unexpected:
        raise ValueError(f"실버 표본의 알 수 없는 유형: {unexpected}")
    rng = random.Random(random_seed)
    selected: list[Question] = []
    for question_type, expected in SILVER_QUOTA.items():
        candidates = sorted(by_type.get(question_type, ()), key=lambda item: item.id)
        if len(candidates) != expected:
            raise ValueError(
                f"실버 {question_type} 수량 불일치: {len(candidates)}/{expected}"
            )
        rng.shuffle(candidates)
        selected.extend(candidates[: sample_size(expected)])
    return sorted(selected, key=lambda item: item.id)


def build_packets(root: Path, *, attention_checks: int = 0) -> int:
    """Write private packets and an unsigned, Claude-prefilled CSV template."""
    root = Path(root).resolve()
    if (
        isinstance(attention_checks, bool)
        or not isinstance(attention_checks, int)
        or attention_checks < 0
    ):
        print("attention_checks는 0 이상의 정수여야 합니다.", file=sys.stderr)
        return 1
    try:
        synthetic, real_drafts, _, silver = _load_populations(root, drafts=True)
        silver = [question for question in silver if question.set == "silver"]
        seed = _company_random_seed(root)
        sample = silver_sample(silver, random_seed=seed + _SAMPLE_SEED_OFFSET)
        gold = [question for question in synthetic if question.set == "gold"]
        gold.extend(real_drafts)
        _validate_gold_population(gold)
        verdicts, missing = _load_verdicts(root / CLAUDE_VERDICTS)
        if missing:
            print(
                f"Claude 판정 파일 없음: {CLAUDE_VERDICTS.as_posix()} "
                "(판정 칸을 (초안 없음)으로 둡니다.)",
                file=sys.stderr,
            )
        docs = _load_documents(root)
        real_summary = _load_optional_json(root / _REAL_SUMMARY_PATH)
        laws = _load_laws(root)
        sample_ids = {question.id for question in sample}
        unreviewed = [
            q for q in silver if q.id not in sample_ids and q.source == "synthetic"
        ]
        reviewed = [q for q in [*gold, *sample] if q.source == "synthetic"]
        try:
            checks, check_verdicts, check_key = _attention_checks(
                unreviewed,
                verdicts,
                count=attention_checks,
                random_seed=seed + _PACKET_SEED_OFFSET,
            )
            source_pool = "unreviewed_silver"
        except GoldenReviewError:
            checks, check_verdicts, check_key = _attention_checks(
                reviewed,
                verdicts,
                count=attention_checks,
                random_seed=seed + _PACKET_SEED_OFFSET,
            )
            source_pool = "reviewed_fallback"
            print(
                "점검 원본 부족: 검수 대상이 아닌 실버에서 고르지 못해 검수 대상에서 골랐습니다.",
                file=sys.stderr,
            )
        questions = [*gold, *sample, *checks]
        rng = random.Random(seed + _PACKET_SEED_OFFSET)
        rng.shuffle(questions)
        review_keys = {
            f"r-{number:03d}": question.id
            for number, question in enumerate(questions, start=1)
        }
        key_of = {qid: key for key, qid in review_keys.items()}
        all_verdicts = {
            qid: {**verdict, "note": _mask_ids(str(verdict.get("note", "")), key_of)}
            for qid, verdict in {**verdicts, **check_verdicts}.items()
        }
        packet = _render_packet(
            questions,
            keys=key_of,
            verdicts=all_verdicts,
            docs=docs,
            laws=laws,
            root=root,
            real_summary=real_summary,
        )
        _write_text_atomic(root / _PACKET_PATH, packet)
        _write_template_csv(root / TEMPLATE_CSV, questions, all_verdicts, key_of)
        _write_json_atomic(root / REVIEW_KEYS, review_keys)
        _write_json_atomic(
            root / ATTENTION_KEY,
            {
                "source_pool": source_pool,
                "attention_checks": check_key,
                "random_seed": seed + _PACKET_SEED_OFFSET,
                "silver_sample_ids": [question.id for question in sample],
            },
        )
        missing_ids = sorted(
            question.id for question in [*gold, *sample] if question.id not in verdicts
        )
        if missing_ids:
            print("Claude 판정 초안 없음: " + ", ".join(missing_ids), file=sys.stderr)
    except (
        GoldenReviewError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        print(f"골드 검수 패킷 생성 실패: {_first_line(exc)}", file=sys.stderr)
        return 1
    print(
        "골드 검수 패킷 생성 완료: "
        f"gold={len(gold)}, silver_sample={len(sample)}, "
        f"attention={len(checks)}, {_PACKET_PATH.as_posix()}"
    )
    return 0


def promote(root: Path) -> int:
    """Promote only signed human-confirmed questions into committed outputs."""
    root = Path(root).resolve()
    try:
        _, _, gold, silver = _load_populations(root, drafts=False)
        _validate_gold_population(gold)
        seed = _company_random_seed(root)
        sample = silver_sample(silver, random_seed=seed + _SAMPLE_SEED_OFFSET)
        sample_ids = {question.id for question in sample}
        verdicts, missing_verdict_file = _load_verdicts(root / CLAUDE_VERDICTS)
        if missing_verdict_file:
            raise GoldenReviewError(
                f"Claude 판정 파일이 없습니다: {CLAUDE_VERDICTS.as_posix()}"
            )
        attention_key = _load_attention_key(root / ATTENTION_KEY)
        review_paths = sorted(root.glob(REVIEW_GLOB))
        if not review_paths:
            raise GoldenReviewError(
                "검수 CSV가 없습니다: data/golden/m4_review/*-review.csv"
            )
        all_by_id = {question.id: question for question in [*gold, *sample]}
        allowed_ids = set(all_by_id) | set(attention_key)
        review_keys = _load_review_keys(root / REVIEW_KEYS)
        rows, invalid_rows = _latest_review_rows(
            root, review_paths, allowed_ids=allowed_ids, review_keys=review_keys
        )
        decisions: dict[str, _Decision] = {}
        docs = _load_documents(root)
        for question_id, review in rows.items():
            question = all_by_id.get(question_id)
            base_verdict = (
                attention_key[question_id]["verdict"]
                if question_id in attention_key
                else verdicts.get(question_id)
            )
            decisions[question_id] = _review_decision(
                review,
                question=question,
                base_verdict=base_verdict,
                docs=docs,
            )

        real_summary = _load_optional_json(root / _REAL_SUMMARY_PATH)
        gold_ready, gold_blocked = _confirmed_gold(
            gold, decisions, real_summary=real_summary
        )
        if gold_blocked:
            print("골드 확정 실패:")
            for question_id, reason in gold_blocked:
                print(f"- {question_id}: {reason}")
            return 1

        silver_ready, negative_sample_ids = _confirmed_silver(
            silver,
            decisions,
            sample_ids=sample_ids,
            real_summary=real_summary,
        )
        gold_assignment, gold_split = assign_split(
            gold_ready,
            GOLD_SPLIT_QUOTA,
            random_seed=seed + SPLIT_SEED_OFFSET,
        )
        silver_assignment, silver_split = assign_split(
            silver_ready,
            None,
            random_seed=seed + SPLIT_SEED_OFFSET,
            test_share=0.6,
        )
        final_gold = [
            replace(question, split=gold_assignment[question.id])
            for question in gold_ready
        ]
        final_silver = [
            replace(question, split=silver_assignment[question.id])
            for question in silver_ready
        ]
        gold_bytes = _questions_bytes(final_gold)
        gold_path = root / GOLD_PATH
        if gold_path.exists() and gold_path.read_bytes() != gold_bytes:
            print(
                "골드는 확정 후 수정하지 않는다: 기존 gold.jsonl과 새 내용이 "
                "달라 덮어쓰지 않았습니다."
            )
            return 1

        reviewed_gold = [
            decisions[question.id]
            for question in gold
            if question.id in decisions and decisions[question.id].valid
        ]
        reviewed_sample = [
            decisions[question.id]
            for question in sample
            if question.id in decisions and decisions[question.id].valid
        ]
        disagree_by_item = {
            item: sum(index in decision.disagree for decision in reviewed_gold)
            for index, item in enumerate(REVIEW_ITEMS, start=1)
        }
        detected_ids = sorted(
            question_id
            for question_id, item in attention_key.items()
            if question_id in decisions
            and decisions[question_id].valid
            and int(item["wrong_item"]) in decisions[question_id].disagree
        )
        missed_ids = sorted(set(attention_key) - set(detected_ids))
        input_paths = [
            root / _SYNTHETIC_PATH,
            root / _REAL_PROJECTION_PATH,
            root / CLAUDE_VERDICTS,
            root / ATTENTION_KEY,
            root / _REAL_SUMMARY_PATH,
            *review_paths,
        ]
        summary = {
            "gold": {
                "reviewed": len(reviewed_gold),
                "confirmed": len(final_gold),
                "disagree_by_item": disagree_by_item,
                "acceptance_rate": _ratio(len(final_gold), len(reviewed_gold)),
            },
            "silver_sample": {
                "n": len(reviewed_sample),
                "negative": len(negative_sample_ids),
                "error_rate": _ratio(len(negative_sample_ids), len(reviewed_sample)),
                "wilson95": _wilson95(len(negative_sample_ids), len(reviewed_sample)),
            },
            "attention_checks": {
                "n": len(attention_key),
                "detected": len(detected_ids),
                # Dummy IDs remain private. The committed manifest records only
                # deterministic ordinals, so no chk- identifier can leak.
                "missed_ids": [
                    question_id.removeprefix(ATTENTION_CHECK_PREFIX)
                    for question_id in missed_ids
                ],
            },
            "split": {"gold": gold_split, "silver": silver_split},
            "inputs": {
                path.relative_to(root).as_posix(): sha256_file(path)
                for path in input_paths
                if path.is_file()
            },
            "invalid_review_rows": invalid_rows,
        }

        if not gold_path.exists():
            _write_bytes_atomic(gold_path, gold_bytes)
        _write_bytes_atomic(root / SILVER_PATH, _questions_bytes(final_silver))
        _write_json_atomic(root / SUMMARY_PATH, summary)
    except (
        GoldenReviewError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        print(f"골드 검수 확정 실패: {_first_line(exc)}", file=sys.stderr)
        return 1

    print(
        "골드 검수 확정 완료: "
        f"gold={len(final_gold)}, silver={len(final_silver)}, "
        f"{SUMMARY_PATH.as_posix()}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for packet creation and human-confirmed promotion."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    packets_parser = subparsers.add_parser("packets")
    packets_parser.add_argument("--attention-checks", type=int, default=0)
    subparsers.add_parser("promote")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.command == "packets":
        return build_packets(args.root, attention_checks=args.attention_checks)
    return promote(args.root)


class _Decision:
    def __init__(
        self,
        *,
        valid: bool,
        disagree: frozenset[int] = frozenset(),
        positive: bool = False,
        question: Question | None = None,
        reason: str = "",
    ) -> None:
        self.valid = valid
        self.disagree = disagree
        self.positive = positive
        self.question = question
        self.reason = reason


def _load_populations(
    root: Path, *, drafts: bool
) -> tuple[list[Question], list[Question], list[Question], list[Question]]:
    synthetic_path = root / _SYNTHETIC_PATH
    real_path = root / (_REAL_DRAFTS_PATH if drafts else _REAL_PROJECTION_PATH)
    if not synthetic_path.is_file():
        raise GoldenReviewError(f"합성 검증 문항이 없습니다: {_SYNTHETIC_PATH}")
    if not real_path.is_file():
        raise GoldenReviewError(
            f"실제 규정 {'초안' if drafts else '투영'}이 없습니다: "
            f"{real_path.relative_to(root)}"
        )
    synthetic = load_questions(synthetic_path)
    real = load_questions(real_path)
    gold = [question for question in synthetic if question.set == "gold"] + real
    silver = [question for question in synthetic if question.set == "silver"]
    ids = [question.id for question in [*synthetic, *real]]
    duplicates = sorted(
        question_id for question_id, count in Counter(ids).items() if count > 1
    )
    if duplicates:
        raise GoldenReviewError(f"문항 ID가 중복되었습니다: {duplicates}")
    if any(
        question.id.startswith(ATTENTION_CHECK_PREFIX) for question in gold + silver
    ):
        raise GoldenReviewError("입력 문항에 chk- 점검용 ID가 포함되었습니다.")
    return synthetic, real, gold, silver


def _validate_gold_population(gold: Sequence[Question]) -> None:
    if len(gold) != sum(GOLD_QUOTA.values()):
        raise GoldenReviewError(f"골드 문항 수량 불일치: {len(gold)}/50")
    actual = Counter((question.type, question.source) for question in gold)
    differences = {
        f"{question_type}/{source}": actual[(question_type, source)] - expected
        for (question_type, source), expected in GOLD_QUOTA.items()
        if actual[(question_type, source)] != expected
    }
    if differences:
        raise GoldenReviewError(f"골드 유형·출처 수량 불일치: {differences}")


def _load_verdicts(
    path: Path,
) -> tuple[dict[str, dict[str, object]], bool]:
    if not path.is_file():
        return {}, True
    verdicts: dict[str, dict[str, object]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise TypeError(f"Claude 판정이 객체가 아닙니다: {path}:{line_number}")
            question_id = value.get("question_id")
            if not isinstance(question_id, str) or not question_id:
                raise ValueError(f"Claude 판정 question_id 누락: {path}:{line_number}")
            if question_id in verdicts:
                raise ValueError(f"Claude 판정 ID 중복: {question_id}")
            values = {key: value.get(key) for key in _VERDICT_KEYS}
            if any(not isinstance(item, bool) for item in values.values()):
                raise TypeError(f"Claude q1/q2/q3는 bool이어야 합니다: {question_id}")
            note = value.get("note", "")
            if not isinstance(note, str):
                raise TypeError(f"Claude note는 문자열이어야 합니다: {question_id}")
            verdicts[question_id] = {**values, "note": note}
    return verdicts, False


def _mask_ids(note: str, keys: Mapping[str, str]) -> str:
    """Show other questions only by review key so no identifier leaks."""
    return _QUESTION_ID_RE.sub(lambda m: keys.get(m.group(0), "다른 문항"), note)


def _attention_checks(
    synthetic: Sequence[Question],
    verdicts: Mapping[str, Mapping[str, object]],
    *,
    count: int,
    random_seed: int,
) -> tuple[list[Question], dict[str, dict[str, object]], list[dict[str, object]]]:
    # A flipped verdict is a guaranteed mistake only if every item was judged Y.
    eligible = [
        question
        for question in synthetic
        if all(verdicts.get(question.id, {}).get(key) is True for key in _VERDICT_KEYS)
    ]
    if count > len(eligible):
        raise GoldenReviewError(
            f"점검용 더미 원본(Claude 판정 3항목 모두 Y) 부족: {len(eligible)}/{count}"
        )
    candidates = sorted(eligible, key=lambda question: question.id)
    rng = random.Random(random_seed)
    rng.shuffle(candidates)
    questions: list[Question] = []
    check_verdicts: dict[str, dict[str, object]] = {}
    key: list[dict[str, object]] = []
    for index, source in enumerate(candidates[:count], start=1):
        question_id = f"{ATTENTION_CHECK_PREFIX}{index:03d}"
        wrong_item = (index - 1) % len(REVIEW_ITEMS) + 1
        original = verdicts.get(
            source.id, {"q1": True, "q2": True, "q3": True, "note": ""}
        )
        verdict = {key: original[key] for key in _VERDICT_KEYS}
        verdict[_VERDICT_KEYS[wrong_item - 1]] = not bool(
            verdict[_VERDICT_KEYS[wrong_item - 1]]
        )
        verdict["note"] = str(original.get("note", ""))
        questions.append(replace(source, id=question_id))
        check_verdicts[question_id] = verdict
        key.append(
            {
                "question_id": question_id,
                "source_question_id": source.id,
                "wrong_item": wrong_item,
                "verdict": verdict,
            }
        )
    return questions, check_verdicts, key


def _write_template_csv(
    path: Path,
    questions: Sequence[Question],
    verdicts: Mapping[str, Mapping[str, object]],
    keys: Mapping[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for question in questions:
                verdict = verdicts.get(question.id)
                writer.writerow(
                    {
                        "review_key": keys[question.id],
                        "set": question.set,
                        "type": question.type,
                        "claude_q1": _verdict_text(verdict, "q1"),
                        "claude_q2": _verdict_text(verdict, "q2"),
                        "claude_q3": _verdict_text(verdict, "q3"),
                        "claude_note": (
                            str(verdict.get("note", "")) if verdict else "(초안 없음)"
                        ),
                        # These four cells are exclusively human-authored.
                        "reviewer": "",
                        "reviewed_on": "",
                        "disagree": "",
                        "correction": "",
                    }
                )
            temporary = Path(destination.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _render_packet(
    questions: Sequence[Question],
    *,
    keys: Mapping[str, str],
    verdicts: Mapping[str, Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    laws: Mapping[str, Mapping[str, object]],
    root: Path,
    real_summary: Mapping[str, object],
) -> str:
    lines = [
        "# M4 골드·실버 사람 검수 패킷",
        "",
        "Claude 판정은 초안이다. 동의하지 않는 항목만 CSV의 disagree와 correction에 적는다. reviewer와 reviewed_on은 실제 검수자가 직접 적어야 한다.",
    ]
    for question in questions:
        lines.extend(
            [
                "",
                f"## {keys[question.id]}",
                "",
                f"- 세트: `{question.set}`",
                f"- 출처·유형: `{question.source}` / `{question.type}`",
                f"- 태그: {', '.join(question.tags) or '(없음)'}",
                f"- 기준일: {question.as_of or '(없음)'}",
                f"- 질문자: {question.asker.team} / {question.asker.grade}급",
                f"- 기대 응답: `{question.expected_response}`",
                "",
                "### 질문",
                "",
                question.question,
                "",
                "### 정답 구간",
                "",
                *_qrel_lines(question, docs=docs, laws=laws, root=root),
                "",
                "### 보조 검수 정보",
                "",
                f"- 미표시 후보 문서 ID: {', '.join(question.unmarked_candidates) or '(없음)'}",
                f"- 답 없음 확인: {_no_answer_text(question, real_summary)}",
                f"- 어휘 겹침: {_overlap_text(question, docs=docs, laws=laws)}",
                f"- 참조 답변: {question.reference_answer or question.summary80 or '(없음)'}",
            ]
        )
        if question.sql is not None:
            lines.extend(
                [
                    f"- SQL: `{question.sql.query}`",
                    f"- SQL 기대 값: `{json.dumps(question.sql.expected, ensure_ascii=False, sort_keys=True)}`",
                ]
            )
        verdict = verdicts.get(question.id)
        lines.extend(
            [
                "",
                "### Claude 판정",
                "",
                f"1. 답변 가능성: {_verdict_text(verdict, 'q1')}",
                f"2. qrels 완전성: {_verdict_text(verdict, 'q2')}",
                f"3. 라벨 정확성: {_verdict_text(verdict, 'q3')}",
                f"- 메모: {str(verdict.get('note', '')) if verdict else '(초안 없음)'}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _qrel_lines(
    question: Question,
    *,
    docs: Mapping[str, Mapping[str, object]],
    laws: Mapping[str, Mapping[str, object]],
    root: Path,
) -> list[str]:
    if not question.qrels:
        return ["- (정답 구간 없음)"]
    lines: list[str] = []
    for qrel in question.qrels:
        title = _document_title(qrel, docs=docs, laws=laws)
        if qrel.rel == 1:
            marker = (
                " (보정: qrels_overrides)"
                if question.source == "synthetic" and qrel.fact_id is None
                else ""
            )
            lines.append(f"- 관련도 1: {title}{marker}")
            continue
        excerpt = _qrel_excerpt(qrel, docs=docs, laws=laws, root=root)
        lines.append(f"- 관련도 2: {title} — {excerpt}")
    return lines


def _document_title(
    qrel: Qrel,
    *,
    docs: Mapping[str, Mapping[str, object]],
    laws: Mapping[str, Mapping[str, object]],
) -> str:
    document = docs.get(qrel.doc_id)
    if document is not None and isinstance(document.get("title"), str):
        return str(document["title"])
    match = _LAW_ID_RE.fullmatch(qrel.doc_id)
    law = laws.get(match.group(1)) if match else None
    if law is not None and isinstance(law.get("name"), str):
        return f"{law['name']} ({qrel.doc_id})"
    return qrel.doc_id


def _qrel_excerpt(
    qrel: Qrel,
    *,
    docs: Mapping[str, Mapping[str, object]],
    laws: Mapping[str, Mapping[str, object]],
    root: Path,
) -> str:
    document = docs.get(qrel.doc_id)
    body = document.get("body") if document is not None else None
    if (
        isinstance(body, str)
        and isinstance(qrel.start, int)
        and not isinstance(qrel.start, bool)
        and isinstance(qrel.end, int)
        and not isinstance(qrel.end, bool)
        and 0 <= qrel.start <= qrel.end <= len(body)
    ):
        left = max(0, qrel.start - 200)
        right = min(len(body), qrel.end + 200)
        return (
            body[left : qrel.start]
            + "**"
            + body[qrel.start : qrel.end]
            + "**"
            + body[qrel.end : right]
        ).replace("\n", " ")
    match = _LAW_ID_RE.fullmatch(qrel.doc_id)
    law = laws.get(match.group(1)) if match else None
    if law is not None and qrel.article_key:
        text = article_texts(law).get(qrel.article_key)
        if text:
            return f"**{text}**"
    if qrel.annex_id:
        table = root / _TABLES_DIR / f"{qrel.annex_id}.md"
        if table.is_file():
            text = table.read_text(encoding="utf-8").strip()
            return f"**{text}**"
    return "(발췌 없음)"


def _no_answer_text(question: Question, real_summary: Mapping[str, object]) -> str:
    checks = dict(question.no_answer_checks or {})
    if question.source == "synthetic":
        synthetic = real_summary.get("synthetic_no_answer_real_check")
        if isinstance(synthetic, Mapping) and question.id in synthetic:
            checks["real"] = synthetic[question.id]
    elif question.type == "unanswerable":
        real = real_summary.get("no_answer_checks")
        if isinstance(real, Mapping) and question.id in real:
            checks["real"] = real[question.id]
    return (
        json.dumps(checks, ensure_ascii=False, sort_keys=True)
        if checks
        else "(해당 없음)"
    )


def _overlap_text(
    question: Question,
    *,
    docs: Mapping[str, Mapping[str, object]],
    laws: Mapping[str, Mapping[str, object]],
) -> str:
    if question.source == "synthetic":
        metrics = overlap(question, question.question, docs)
    else:
        targets: list[str] = []
        for qrel in question.qrels:
            match = _LAW_ID_RE.fullmatch(qrel.doc_id)
            law = laws.get(match.group(1)) if match else None
            if law is not None and qrel.article_key:
                value = article_texts(law).get(qrel.article_key)
                if value:
                    targets.append(value)
        clean_question = strip_values(question.question, question.anchor_values)
        clean_target = strip_values("\n".join(targets), question.anchor_values)
        metrics = {
            "containment": (
                bigram_containment(clean_question, clean_target)
                if clean_target
                else None
            ),
            "lcs": (
                longest_common_substring(clean_question, clean_target)
                if clean_target
                else None
            ),
        }
    containment = metrics.get("containment")
    lcs = metrics.get("lcs")
    containment_text = (
        f"{float(containment):.4f}" if isinstance(containment, (int, float)) else "N/A"
    )
    lcs_text = str(lcs) if isinstance(lcs, int) else "N/A"
    return (
        f"포함률 {containment_text} (상한 {CONTAINMENT_CAP}), "
        f"LCS {lcs_text} (상한 {LCS_CAP})"
    )


def _load_review_keys(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise GoldenReviewError(
            f"검수 번호 대응표가 없습니다: {REVIEW_KEYS.as_posix()}"
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise GoldenReviewError("검수 번호 대응표 형식 오류")
    return dict(value)


def _latest_review_rows(
    root: Path,
    paths: Sequence[Path],
    *,
    allowed_ids: set[str],
    review_keys: Mapping[str, str],
) -> tuple[dict[str, dict[str, str]], list[dict[str, object]]]:
    candidates: dict[str, list[tuple[date, str, int, dict[str, str]]]] = defaultdict(
        list
    )
    invalid: list[dict[str, object]] = []
    for path in paths:
        source_name = path.relative_to(root).as_posix()
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise GoldenReviewError(f"검수 CSV 열 불일치: {source_name}")
            for row_number, raw in enumerate(reader, start=2):
                row = {
                    column: (raw.get(column) or "").strip() for column in CSV_COLUMNS
                }
                question_id = review_keys.get(row["review_key"], "")
                if question_id not in allowed_ids:
                    invalid.append(
                        {
                            "source": source_name,
                            "row": row_number,
                            "reason": "unknown_review_key",
                        }
                    )
                    continue
                # Both signatures are required. A blank or half-signed row is
                # deliberately unreviewed, never a default acceptance.
                if not row["reviewer"] or not row["reviewed_on"]:
                    continue
                try:
                    reviewed_on = date.fromisoformat(row["reviewed_on"])
                except ValueError:
                    invalid.append(
                        {
                            "source": source_name,
                            "row": row_number,
                            "reason": "invalid_reviewed_on",
                        }
                    )
                    continue
                if reviewed_on.isoformat() != row["reviewed_on"]:
                    invalid.append(
                        {
                            "source": source_name,
                            "row": row_number,
                            "reason": "invalid_reviewed_on",
                        }
                    )
                    continue
                candidates[question_id].append(
                    (reviewed_on, source_name, row_number, row)
                )
    latest = {
        question_id: max(values, key=lambda item: (item[0], item[1], item[2]))[3]
        for question_id, values in candidates.items()
    }
    return latest, sorted(
        invalid, key=lambda item: (str(item["source"]), int(item["row"]))
    )


def _review_decision(
    row: Mapping[str, str],
    *,
    question: Question | None,
    base_verdict: Mapping[str, object] | None,
    docs: Mapping[str, Mapping[str, object]],
) -> _Decision:
    if base_verdict is None:
        return _Decision(valid=False, reason="Claude 판정 초안 없음")
    if question is not None and (
        row["set"] != question.set or row["type"] != question.type
    ):
        return _Decision(valid=False, reason="set/type 불일치")
    try:
        disagree = _parse_disagree(row["disagree"])
    except ValueError as exc:
        return _Decision(valid=False, reason=str(exc))
    correction = row["correction"].strip()
    if disagree and not correction:
        return _Decision(valid=False, disagree=disagree, reason="correction 누락")
    corrected = question
    if 2 in disagree:
        try:
            if question is None:
                _parse_qrel_correction(correction)
            else:
                corrected = _apply_qrel_correction(question, correction, docs=docs)
        except ValueError as exc:
            return _Decision(
                valid=True,
                disagree=disagree,
                positive=False,
                question=question,
                reason=f"수정 필요: {exc}",
            )
    verdict_values: list[bool] = []
    for index, key in enumerate(_VERDICT_KEYS, start=1):
        value = base_verdict.get(key)
        if not isinstance(value, bool):
            return _Decision(
                valid=False, disagree=disagree, reason="Claude 판정 형식 오류"
            )
        # A syntactically valid q2 correction is applied to the promoted qrels,
        # so completeness is positive after the repair. Other disagreements
        # change only the human assessment and therefore toggle the draft.
        if index == 2 and index in disagree:
            verdict_values.append(True)
        else:
            verdict_values.append(not value if index in disagree else value)
    return _Decision(
        valid=True,
        disagree=disagree,
        positive=all(verdict_values),
        question=corrected,
        reason="" if all(verdict_values) else "최종 판정 부정",
    )


def _parse_disagree(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    parts = [part.strip() for part in value.split(",")]
    if any(part not in {"1", "2", "3"} for part in parts) or len(parts) != len(
        set(parts)
    ):
        raise ValueError("disagree는 1,2,3의 중복 없는 쉼표 목록이어야 합니다.")
    return frozenset(int(part) for part in parts)


def _apply_qrel_correction(
    question: Question,
    correction: str,
    *,
    docs: Mapping[str, Mapping[str, object]],
) -> Question:
    directives = _parse_qrel_correction(correction)
    qrels = list(question.qrels)
    for operation, identifier in directives:
        if operation == "remove":
            qrels = [
                qrel
                for qrel in qrels
                if qrel.doc_id != identifier and qrel.fact_id != identifier
            ]
            continue
        if any(
            qrel.doc_id == identifier or qrel.fact_id == identifier for qrel in qrels
        ):
            continue
        matching_fact = _qrels_for_fact(identifier, docs)
        if matching_fact:
            qrels.extend(matching_fact)
        elif identifier in docs:
            # A document-only addition records a related candidate, not an
            # invented fact span.
            qrels.append(Qrel(doc_id=identifier, rel=1))
        else:
            raise ValueError(f"add 대상 ID를 찾을 수 없습니다: {identifier}")
    return replace(
        question,
        qrels=tuple(
            sorted(
                dict.fromkeys(qrels),
                key=lambda qrel: (
                    qrel.doc_id,
                    -qrel.rel,
                    qrel.fact_id or "",
                    qrel.start if qrel.start is not None else -1,
                ),
            )
        ),
    )


def _parse_qrel_correction(correction: str) -> list[tuple[str, str]]:
    raw_directives = [
        value.strip() for value in re.split(r"[,;\s]+", correction) if value.strip()
    ]
    directives: list[tuple[str, str]] = []
    for raw in raw_directives:
        match = _CORRECTION_RE.fullmatch(raw)
        if match is None:
            raise ValueError("q2 correction은 add:ID 또는 remove:ID만 허용합니다.")
        directives.append((match.group(1), match.group(2)))
    if not directives:
        raise ValueError("q2 correction 지시가 없습니다.")
    return directives


def _qrels_for_fact(
    fact_id: str, docs: Mapping[str, Mapping[str, object]]
) -> list[Qrel]:
    result: list[Qrel] = []
    for doc_id, document in sorted(docs.items()):
        body = document.get("body")
        spans = document.get("fact_spans")
        if not isinstance(body, str) or not isinstance(spans, list):
            continue
        for item in spans:
            if not isinstance(item, Mapping) or item.get("fact_id") != fact_id:
                continue
            start, end = item.get("start"), item.get("end")
            if (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 0 <= start <= end <= len(body)
            ):
                result.append(
                    Qrel(
                        doc_id=doc_id,
                        rel=2,
                        fact_id=fact_id,
                        start=start,
                        end=end,
                        span_sha256=span_sha256(body[start:end]),
                    )
                )
    return result


def _confirmed_gold(
    gold: Sequence[Question],
    decisions: Mapping[str, _Decision],
    *,
    real_summary: Mapping[str, object],
) -> tuple[list[Question], list[tuple[str, str]]]:
    confirmed: list[Question] = []
    blocked: list[tuple[str, str]] = []
    for question in sorted(gold, key=lambda item: item.id):
        decision = decisions.get(question.id)
        if decision is None or not decision.valid:
            blocked.append(
                (question.id, decision.reason if decision is not None else "미검수")
            )
            continue
        if not decision.positive or decision.question is None:
            blocked.append((question.id, decision.reason or "최종 판정 부정"))
            continue
        hydrated = _with_real_no_answer_check(decision.question, real_summary)
        if hydrated.type == "unanswerable" and not _real_check_passed(hydrated):
            blocked.append((question.id, "답 없음 ③ 실제 규정 확인 누락/실패"))
            continue
        confirmed.append(hydrated)
    return confirmed, blocked


def _confirmed_silver(
    silver: Sequence[Question],
    decisions: Mapping[str, _Decision],
    *,
    sample_ids: set[str],
    real_summary: Mapping[str, object],
) -> tuple[list[Question], set[str]]:
    ready: list[Question] = []
    negative: set[str] = set()
    for question in sorted(silver, key=lambda item: item.id):
        candidate = _with_real_no_answer_check(question, real_summary)
        if question.id in sample_ids:
            decision = decisions.get(question.id)
            if decision is not None and decision.valid:
                if not decision.positive or decision.question is None:
                    negative.add(question.id)
                    continue
                candidate = _with_real_no_answer_check(decision.question, real_summary)
        if candidate.type == "unanswerable" and not _real_check_passed(candidate):
            if question.id in sample_ids:
                negative.add(question.id)
            continue
        ready.append(candidate)
    return ready, negative


def _with_real_no_answer_check(
    question: Question, summary: Mapping[str, object]
) -> Question:
    if question.type != "unanswerable":
        return question
    checks = dict(question.no_answer_checks or {})
    field = (
        "synthetic_no_answer_real_check"
        if question.source == "synthetic"
        else "no_answer_checks"
    )
    values = summary.get(field)
    if isinstance(values, Mapping) and isinstance(values.get(question.id), bool):
        checks["real"] = bool(values[question.id])
    return replace(question, no_answer_checks=checks)


def _real_check_passed(question: Question) -> bool:
    checks = question.no_answer_checks
    return isinstance(checks, Mapping) and checks.get("real") is True


def _load_attention_key(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError("attention_checks.json은 객체여야 합니다.")
    raw_checks = value.get("attention_checks", [])
    if not isinstance(raw_checks, list):
        raise TypeError("attention_checks는 배열이어야 합니다.")
    result: dict[str, dict[str, object]] = {}
    for item in raw_checks:
        if not isinstance(item, Mapping):
            raise TypeError("attention check 항목은 객체여야 합니다.")
        question_id = item.get("question_id")
        wrong_item = item.get("wrong_item")
        verdict = item.get("verdict")
        if (
            not isinstance(question_id, str)
            or not question_id.startswith(ATTENTION_CHECK_PREFIX)
            or wrong_item not in {1, 2, 3}
            or not isinstance(verdict, Mapping)
        ):
            raise ValueError("attention check 정답표 형식이 잘못되었습니다.")
        result[question_id] = {
            "wrong_item": wrong_item,
            "verdict": dict(verdict),
        }
    return result


def _load_documents(root: Path) -> dict[str, dict[str, object]]:
    documents: dict[str, dict[str, object]] = {}
    for path in sorted((root / _DOCS_DIR).glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError(f"합성 문서가 객체가 아닙니다: {path}")
        doc_id = value.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError(f"합성 문서 doc_id가 없습니다: {path}")
        documents[doc_id] = dict(value)
    return documents


def _load_laws(root: Path) -> dict[str, dict[str, object]]:
    laws: dict[str, dict[str, object]] = {}
    for path in sorted((root / _LAW_DIR).glob("*/*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError(f"법제처 문서가 객체가 아닙니다: {path}")
        serial = value.get("serial")
        if isinstance(serial, (str, int)) and not isinstance(serial, bool):
            laws[str(serial)] = dict(value)
    return laws


def _company_random_seed(root: Path) -> int:
    value = yaml.safe_load((root / _COMPANY_PATH).read_text(encoding="utf-8"))
    seed = value.get("random_seed") if isinstance(value, Mapping) else None
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("company.random_seed가 정수가 아닙니다.")
    return seed


def _questions_bytes(questions: Sequence[Question]) -> bytes:
    lines: list[str] = []
    for question in sorted(questions, key=lambda item: item.id):
        if question.id.startswith(ATTENTION_CHECK_PREFIX):
            raise GoldenReviewError("chk- 점검용 문항은 확정 파일에 쓸 수 없습니다.")
        row = question_to_dict(question)
        if question.source == "real":
            row.pop("reference_answer", None)
        lines.append(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _verdict_text(verdict: Mapping[str, object] | None, key: str) -> str:
    if verdict is None:
        return "(초안 없음)"
    return "1" if verdict.get(key) is True else "0"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _wilson95(successes: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    z = 1.959963984540054
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4)]


def _load_optional_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON 객체가 아닙니다: {path}")
    return dict(value)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _write_text_atomic(path: Path, value: str) -> None:
    _write_bytes_atomic(path, value.encode("utf-8"))


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0]


if __name__ == "__main__":  # pragma: no cover - CLI acceptance check
    raise SystemExit(main())
