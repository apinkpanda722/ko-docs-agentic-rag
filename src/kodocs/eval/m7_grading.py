"""Offline M7 input verification, blind grading inputs and draft rubric schemas.

No answer generation or grading calls occur here. Synthetic reference answers
live verbatim in the committed Question record; M4 did not store their hashes.
Real references live in the uncommitted real_drafts.jsonl and must match the
hash in the committed question. Evidence uses the same composite corpus loader
as the frozen M5 index, with a section-offset adapter (no new parsing).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kodocs.chunking.corpus import CorpusDoc, load_corpus
from kodocs.eval import m6_dev, m6_test
from kodocs.eval.golden import Asker, Question
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.eval.m6_runner import (
    _TERMINAL_CACHE_STATUSES,
    _load_records,
)
from kodocs.ingest.manifest import latest_records, load_records, sha256_file
from kodocs.llm.answer_settings import load_answer_config
from kodocs.tools import ToolDeps, UserContext, load_tool_config, run_tool
from kodocs.tools.deps import M5_CONFIG_PATH, TOOL_INDEX

DEV_RUNS_PATH = m6_dev.DEV_RUNS_PATH
RUN_PATHS = {
    "dev": DEV_RUNS_PATH,
    "test_gold": Path("data/interim/m6/test/run-01/runs.jsonl"),
    "test_silver": Path("data/interim/m6/test/run-01/silver/runs.jsonl"),
}
RUBRIC_PATH = Path(__file__).with_name("templates") / "m7_rubric.md"
REAL_REFERENCES_PATH = Path("data/interim/golden/real_drafts.jsonl")
INPUT_LIMITATION = (
    "M6 실행 당시의 runs.jsonl 해시가 없다. "
    "집계에 영향을 주지 않는 답변 텍스트 변경은 재집계 비교로 탐지하지 못한다."
)
DEV_COMPARE_FIELDS = (
    "answer_match",
    "citation_precision",
    "unanswerable_refusal",
    "gate",
    "guardrails_summary",
    "operations",
    "model_stats",
    "paired_bootstrap",
    "selection",
    "selection_reason",
)
RunClass = Literal["det_zero", "gemini_acc", "gemini_faith_only", "not_scored"]


class InputHashError(RuntimeError):
    """Frozen input hash, row identity or exact aggregate comparison failed."""


@dataclass(frozen=True)
class InputVerification:
    dev_runs_sha256: str
    dev_runs_rows: int
    test_runs_sha256: dict[str, str]
    test_runs_rows: dict[str, int]
    excluded_fields: tuple[str, ...]
    limitations: tuple[str, ...] = (INPUT_LIMITATION,)


@dataclass(frozen=True)
class AnsweredRun:
    answer_id: str
    run_set: Literal["dev", "test_gold", "test_silver"]
    question: Question
    answer: str
    citations: tuple[Mapping[str, object], ...]
    refused: bool
    refuse_reason: str | None
    status: str
    system: str
    model: str
    tool_refs: tuple[Mapping[str, object], ...]
    answer_sha256: str
    reference_answer_path: Path
    root: Path
    trace_id: str | None = None

    @property
    def question_id(self) -> str:
        return self.question.id

    @property
    def type(self) -> str:
        return self.question.type

    @property
    def source(self) -> str:
        return self.question.source

    @property
    def expected_response(self) -> str:
        return self.question.expected_response

    @property
    def asker(self) -> Asker:
        return self.question.asker

    @classmethod
    def from_record(cls, run_set, question, row, *, root):
        answer = row["answer"]
        if not isinstance(answer, str):
            raise InputHashError(f"{run_set}.answer must be a string")
        if not isinstance(row["refused"], bool):
            raise InputHashError(f"{run_set}.refused must be a boolean")
        return cls(
            answer_id=f"{run_set}|{question.id}|{row['system']}|{row['model']}",
            run_set=run_set,
            question=question,
            answer=answer,
            citations=tuple(row["citations"]),
            refused=row["refused"],
            refuse_reason=row["refuse_reason"],
            status=row["status"],
            system=row["system"],
            model=row["model"],
            tool_refs=tuple(row["tool_refs"]),
            answer_sha256=_text_hash(answer),
            reference_answer_path=root
            / (
                REAL_REFERENCES_PATH
                if question.source == "real"
                else Path(f"data/golden/queries/{question.set}.jsonl")
            ),
            root=root,
            trace_id=row.get("trace_id"),
        )


def _selected_rows(path: Path) -> list[dict[str, object]]:
    """Use the M6 row reader and terminal-cache precedence on append-only tries.

    M6 has no standalone selected-row loader: run_answers selects the last
    terminal row per cache_key, otherwise its latest infra_error. This small
    adapter uses the shared latest_records helper without running the runner.
    Distinct cache keys yielding one answer_id are rejected rather than merged.
    """
    raw = _load_records(path)
    if not raw:
        raise InputHashError(f"{path}: runs.jsonl rows missing")
    if any(not isinstance(r.get("cache_key"), str) for r in raw):
        raise InputHashError(f"{path}: cache_key missing")
    latest = latest_records(raw, key=lambda r: r["cache_key"])
    terminals = {
        r["cache_key"]: r
        for r in latest_records(
            [r for r in raw if r.get("status") in _TERMINAL_CACHE_STATUSES],
            key=lambda r: r["cache_key"],
        )
    }
    rows = [terminals.get(r["cache_key"], r) for r in latest]
    identities = [(r["question_id"], r["system"], r["model"]) for r in rows]
    if len(identities) != len(set(identities)):
        raise InputHashError(f"{path}: duplicate answer_id")
    return rows


def _json(path: Path) -> dict:
    return m6_dev._json_object(path)


def _recorded_init(summary: Mapping, label: str):
    values = [s["init_s"] for m in summary["operations"].values() for s in m.values()]
    if not values or any(v != values[0] for v in values):
        raise InputHashError(f"{label}.operations.init_s differs between entries")
    return values[0]


def _differences(left, right, prefix: str) -> list[str]:
    """Compare exact values and report field paths, never answer/evidence text."""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return [
            field
            for k in sorted(set(left) | set(right))
            for field in (
                [f"{prefix}.{k}"]
                if k not in left or k not in right
                else _differences(left[k], right[k], f"{prefix}.{k}")
            )
        ]
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [prefix]
        return [
            field
            for i, (a, b) in enumerate(zip(left, right, strict=True))
            for field in _differences(a, b, f"{prefix}[{i}]")
        ]
    return [] if left == right else [prefix]


def _compare(recorded, computed, fields, label):
    differences = [
        field
        for key in fields
        for field in _differences(
            recorded.get(key), computed.get(key), f"{label}.{key}"
        )
    ]
    if differences:
        raise InputHashError("M6 aggregate mismatch: " + ", ".join(differences))


def _verify_input_manifest_history(root: Path, recorded_hash: str) -> list[str]:
    """Authenticate dev-era bytes, allowing only later top-level additions."""
    path = m6_dev.M6_INPUTS_PATH.as_posix()

    def git(*args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", *args], cwd=root, check=True, capture_output=True
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            # Do not include git stderr or manifest contents in diagnostics.
            raise InputHashError(
                f"m6_eval_inputs_sha256: git history unavailable ({type(exc).__name__})"
            ) from exc

    for commit in git("log", "--format=%H", "--", path).decode("ascii").splitlines():
        historical_bytes = git("show", f"{commit}:{path}")
        if hashlib.sha256(historical_bytes).hexdigest() != recorded_hash:
            continue
        historical = json.loads(historical_bytes)
        if not isinstance(historical, dict):
            raise InputHashError("m6_eval_inputs_sha256: git version must be an object")
        current = _json(root / m6_dev.M6_INPUTS_PATH)
        keys = set(historical) - {"generated_at"}
        differences = _differences(
            {k: historical[k] for k in keys},
            {k: current[k] for k in keys if k in current},
            "inputs",
        )
        if differences:
            raise InputHashError(
                "m6_eval_inputs_sha256: historical input mismatch: "
                + ", ".join(differences)
            )
        return ["inputs.generated_at"] + [
            f"inputs.added_after_dev.{key}"
            for key in sorted(set(current) - set(historical))
        ]
    raise InputHashError("m6_eval_inputs_sha256: matching git version missing")


def verify_m6_inputs(root: Path = Path(".")) -> InputVerification:
    """Recompute M6 aggregates through their original offline analyzers."""
    root = Path(root)
    try:
        return _verify_m6_inputs(root)
    except InputHashError:
        raise
    except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
        # M6 hash guards report only field names, not response bodies.
        raise InputHashError(f"M6 input verification: {exc}") from exc


def _verify_m6_inputs(root: Path) -> InputVerification:
    dev = _json(root / m6_dev.DEV_MANIFEST_PATH)
    config = _json(root / "data/manifest/m6_config.json")
    if sha256_file(root / m6_dev.DEV_MANIFEST_PATH) != config["m6_dev_sha256"]:
        raise InputHashError("m6_dev_sha256 mismatch (m6_dev.json)")
    inputs_hash = dev["m6_eval_inputs_sha256"]
    input_excluded = _verify_input_manifest_history(root, inputs_hash)
    configs = [load_answer_config(name) for name in m6_dev.MODEL_CONFIG_NAMES]
    computed = m6_dev.analyze_dev(
        load_m6_questions(root, split="dev"),
        _selected_rows(root / DEV_RUNS_PATH),
        docs_meta=m6_dev._load_docs_meta(root / m6_dev.DOCS_PATH),
        canaries=_json(root / m6_dev.CANARIES_PATH),
        model_configs=configs,
        concurrency=dev["concurrency"],
        init_s=_recorded_init(dev, "dev"),
        input_manifest_sha256=inputs_hash,
        code_commit=dev["code_commit"],
    )
    _compare(dev, computed, DEV_COMPARE_FIELDS, "dev")
    excluded = input_excluded + [
        f"dev.{key}"
        for key in sorted(set(dev) | set(computed))
        if key not in DEV_COMPARE_FIELDS
    ]
    # Top-level init_s is an inherited runtime input, not independently measured.
    # Its nested operations values are nevertheless compared exactly above.
    excluded.append("dev.init_s (recorded runtime input)")
    completed = sorted(
        (
            r
            for r in latest_records(
                load_records(root / m6_test.RUNS_PATH), key=m6_test._record_seq
            )
            if r.get("status") == "completed"
        ),
        key=m6_test._record_seq,
    )
    if not completed:
        raise InputHashError("m6_test_runs.completed missing")
    latest = completed[-1]
    result_path = root / m6_test.RESULTS_DIR / "run-01.json"
    if latest.get("result_sha256") != sha256_file(result_path):
        raise InputHashError("test.result_sha256 mismatch (run-01.json)")
    result = _json(result_path)
    test_hashes, test_rows = {}, {}
    for run_set, set_name in [("test_gold", "gold"), ("test_silver", "silver")]:
        recorded = result if set_name == "gold" else result["silver_test"]
        path = root / RUN_PATHS[run_set]
        rows = _selected_rows(path)
        questions = load_m6_questions(root, split="test", set_name=set_name)
        expected = {
            (q.id, system, configs[0].model)
            for q in questions
            for system in m6_dev.SYSTEMS
        }
        actual = {(r["question_id"], r["system"], r["model"]) for r in rows}
        if actual != expected:
            raise InputHashError(f"{run_set}.answer_id matrix mismatch")
        computed = m6_test.analyze_test(
            questions,
            rows,
            init_s=_recorded_init(recorded, run_set),
            concurrency=recorded["concurrency"],
        )
        # execute_experiment embeds silver under silver_test; run_test adds
        # lock metadata and raw hashes. The analyzer keys are compared intact.
        _compare(recorded, computed, tuple(computed), run_set)
        excluded.extend(f"{run_set}.{k}" for k in sorted(set(recorded) - set(computed)))
        excluded.append(f"{run_set}.init_s (recorded runtime input)")
        test_hashes[run_set] = sha256_file(path)
        test_rows[run_set] = len(_load_records(path))
    return InputVerification(
        dev_runs_sha256=sha256_file(root / DEV_RUNS_PATH),
        dev_runs_rows=len(_load_records(root / DEV_RUNS_PATH)),
        test_runs_sha256=test_hashes,
        test_runs_rows=test_rows,
        excluded_fields=tuple(excluded),
    )


def load_m6_answers(root: Path = Path(".")) -> list[AnsweredRun]:
    """Verify frozen aggregates before exposing any answer to the grading code."""
    verify_m6_inputs(root)
    runs = []
    seen = set()
    for run_set, path in RUN_PATHS.items():
        questions = {
            q.id: q
            for q in load_m6_questions(
                root,
                split="dev" if run_set == "dev" else "test",
                set_name=None if run_set == "dev" else run_set.removeprefix("test_"),
            )
        }
        for row in _selected_rows(root / path):
            run = AnsweredRun.from_record(
                run_set, questions[row["question_id"]], row, root=root
            )
            if run.answer_id in seen:
                raise InputHashError(f"duplicate answer_id: {run.answer_id}")
            seen.add(run.answer_id)
            runs.append(run)
    return runs


def classify_run(run: AnsweredRun, question: Question) -> RunClass:
    if question.expected_response not in {
        "answer",
        "no_answer",
        "indistinguishable_from_no_answer",
    }:
        raise ValueError("unknown expected_response")
    if run.status != "completed":
        # A failed run on a no-answer question is a refusal-accuracy failure, not
        # a wrong answer: it is not graded for correctness (rule M7-G1).
        return "det_zero" if question.expected_response == "answer" else "not_scored"
    if question.expected_response == "answer":
        return "det_zero" if run.refused or not run.answer.strip() else "gemini_acc"
    return "not_scored" if run.refused else "gemini_faith_only"


def classify_all(runs: Sequence[AnsweredRun]) -> dict[str, RunClass]:
    result = {r.answer_id: classify_run(r, r.question) for r in runs}
    if len(result) != len(runs):
        raise ValueError("duplicate answer_id")
    return result


def class_counts(
    runs: Sequence[AnsweredRun], classes=None
) -> dict[str, dict[str, int]]:
    classes = classify_all(runs) if classes is None else classes
    counts = defaultdict(Counter)
    for run in runs:
        counts[f"{run.run_set}|{run.model}|{run.system}"][classes[run.answer_id]] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(counts.items())}


@dataclass(frozen=True)
class GradeItem:
    answer_id: str
    mode: Literal["acc", "faith"]
    request_text: str
    evidence_chars: int
    evidence_missing: int = 0
    evidence_sources: tuple[str, ...] = ()
    evidence_empty_reason: str | None = None
    evidence_reasons: tuple[str, ...] = ()


class EvidenceReconstructionError(RuntimeError):
    """A reconstructed tool result disagrees with the recorded trace."""


class EvidenceLoader:
    """Section-offset adapter over the frozen index's offline corpus loader.

    Evidence is what the model actually received. A real-document citation
    without a section key is expanded to the sections of the same document in
    the run's own tool_refs, never to the document head (which would add an
    uncited article). SQL citations are rebuilt by re-running the recorded
    ``run_sql`` call read-only with the same asker (no LLM involved).
    """

    def __init__(self, root: Path = Path(".")):
        self.root = Path(root)
        documents, _report = load_corpus("composite", root=root)
        self.documents: dict[str, CorpusDoc] = {d.doc_id: d for d in documents}
        self._traces: dict[str, list[Mapping[str, object]]] | None = None
        self._deps: ToolDeps | None = None

    def __call__(self, doc_id: str, section_key: str | None) -> str | None:
        document = self.documents.get(doc_id)
        if document is None:
            return None
        if document.source == "synthetic":
            return document.text
        if section_key is None:
            return None
        for section in document.sections:
            if section.key == section_key:
                return document.text[section.start : section.end]
        return None

    def section_outside(self, doc_id: str) -> str | None:
        """Body of a real document that belongs to no section (verbatim spans)."""
        document = self.documents.get(doc_id)
        if document is None or document.source == "synthetic":
            return None
        spans = sorted((sec.start, sec.end) for sec in document.sections)
        parts, cursor = [], 0
        for start, end in spans:
            if start > cursor:
                parts.append(document.text[cursor:start])
            cursor = max(cursor, end)
        parts.append(document.text[cursor:])
        return "\n".join(part for part in parts if part.strip()) or None

    def sql_evidence(
        self, run: AnsweredRun, citation: Mapping[str, object]
    ) -> str | None:
        """Re-run the cited ``run_sql`` call read-only; None if the trace is absent."""
        steps = self._trace_steps(run.trace_id)
        match = re.fullmatch(r"c(\d+)", str(citation.get("call_id")))
        if steps is None or match is None:
            return None
        tool_steps = sorted(
            (st for st in steps if st.get("kind") == "tool"),
            key=lambda st: st.get("index", 0),
        )
        number = int(match.group(1))
        if not 1 <= number <= len(tool_steps):
            return None
        step = tool_steps[number - 1]
        if step.get("name") != "run_sql":
            raise EvidenceReconstructionError(
                f"{run.answer_id}: citation {citation.get('call_id')} is not a run_sql step"
            )
        if step.get("error") is not None:
            return None
        result = run_tool(
            "run_sql",
            step.get("args") or {},
            UserContext(team=run.asker.team, grade=run.asker.grade),
            self._tool_deps(),
        )
        if result.is_error:
            raise EvidenceReconstructionError(
                f"{run.answer_id}: run_sql replay returned an error"
            )
        serialized = json.dumps(
            result.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        # The trace stores the serialized length of the original result.
        if len(serialized) != step.get("result_chars"):
            raise EvidenceReconstructionError(
                f"{run.answer_id}: sql replay size {len(serialized)} != "
                f"recorded {step.get('result_chars')}"
            )
        return serialized

    def _tool_deps(self) -> ToolDeps:
        if self._deps is None:
            _index, params = load_tool_config(self.root / M5_CONFIG_PATH)
            # run_sql only reads the SQLite file; no OpenSearch or embedder is used.
            self._deps = ToolDeps(
                client=None,  # type: ignore[arg-type]
                index=TOOL_INDEX,
                embedder=None,  # type: ignore[arg-type]
                db_path=self.root / "data" / "synthetic" / "ledger.sqlite",
                search_params=params,
            )
        return self._deps

    def _trace_steps(self, trace_id: str | None):
        if trace_id is None:
            return None
        if self._traces is None:
            self._traces = {}
            for path in sorted((self.root / "logs" / "traces").glob("*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        if isinstance(record.get("trace_id"), str):
                            self._traces[record["trace_id"]] = record.get("steps", [])
        return self._traces.get(trace_id)


def _reference_text(run: AnsweredRun, question: Question) -> str:
    if question.source == "synthetic":
        text = question.reference_answer
    else:
        records = _load_records(run.root / REAL_REFERENCES_PATH)
        matches = [r for r in records if r.get("id") == question.id]
        if len(matches) != 1:
            raise InputHashError(
                f"{question.id}.reference_answer record missing or duplicated"
            )
        text = matches[0].get("reference_answer")
        if question.reference_sha256 is None:
            raise InputHashError(f"{question.id}.reference_sha256 missing")
    if not isinstance(text, str):
        raise InputHashError(f"{question.id}.reference_answer missing")
    if (
        question.reference_sha256 is not None
        and _text_hash(text) != question.reference_sha256
    ):
        raise InputHashError(f"{question.id}.reference_sha256 mismatch")
    return text


def _collect_evidence(run: AnsweredRun, loader):
    """Evidence items (doc_id, label, text, source) and per-citation miss reasons."""
    outside = getattr(loader, "section_outside", None)
    sql_loader = getattr(loader, "sql_evidence", None)
    items: list[tuple[str | None, str | None, str, str]] = []
    reasons: list[str] = []
    seen: set[tuple[str | None, str | None]] = set()

    def add(doc_id, label, text, source):
        if (doc_id, label) not in seen:
            seen.add((doc_id, label))
            items.append((doc_id, label, text, source))

    for citation in run.citations:
        kind = citation.get("kind")
        if kind == "sql":
            text = sql_loader(run, citation) if sql_loader is not None else None
            if text:
                add(None, None, text, "reconstructed_sql")
            else:
                reasons.append(
                    "sql_replay_unavailable"
                    if sql_loader is None
                    else "sql_trace_missing"
                )
            continue
        doc_id, section = citation.get("doc_id"), citation.get("section_key")
        if kind != "doc" or not isinstance(doc_id, str):
            reasons.append("invalid_citation")
            continue
        text = loader(doc_id, section)
        if text:
            add(doc_id, section, text, "cited_section" if section else "document")
            continue
        if section is not None:
            reasons.append("section_not_found")
            continue
        # Document-level citation of a real document: use what the model received.
        keys = list(
            dict.fromkeys(
                ref.get("section_key")
                for ref in run.tool_refs
                if ref.get("kind") == "doc" and ref.get("doc_id") == doc_id
            )
        )
        if not keys:
            reasons.append("no_tool_refs")
            continue
        found = False
        for key in keys:
            if key is not None:
                body, label = loader(doc_id, key), key
            else:
                body = outside(doc_id) if outside is not None else None
                label = "섹션 밖 본문"
            if body:
                add(doc_id, label, body, "tool_refs")
                found = True
        if not found:
            reasons.append("tool_refs_unloadable")
    return items, reasons


def build_grade_item(
    run: AnsweredRun,
    question: Question,
    evidence_loader: Callable[[str, str | None], str | None],
) -> GradeItem:
    kind = classify_run(run, question)
    if kind not in {"gemini_acc", "gemini_faith_only"}:
        raise ValueError("run does not require Gemini grading")
    reference = _reference_text(run, question)
    evidence, reasons = _collect_evidence(run, evidence_loader)
    # One character per item per round, then join each contiguous prefix.
    # This gives equal access to the budget and redistributes exhausted shares.
    lengths = [0] * len(evidence)
    budget = 6000
    while budget:
        advanced = False
        for i, (_, _, text, _) in enumerate(evidence):
            if lengths[i] < len(text):
                lengths[i] += 1
                budget -= 1
                advanced = True
                if not budget:
                    break
        if not advanced:
            break
    evidence_text = "\n\n".join(
        (
            f"[{doc_id} / {label}]"
            if doc_id and label
            else f"[{doc_id}]"
            if doc_id
            else "[SQL 결과]"
        )
        + "\n"
        + text[:length]
        for (doc_id, label, text, _), length in zip(evidence, lengths, strict=True)
        if length
    )
    request = (
        f"## 질문\n{question.question}\n\n## 참조 답변\n{reference}"
        f"\n\n## 시스템 답변\n{run.answer}\n\n## 인용 근거\n{evidence_text}"
    )
    total = sum(lengths)
    empty = (
        None
        if total
        else (Counter(reasons).most_common(1)[0][0] if reasons else "no_citations")
    )
    return GradeItem(
        run.answer_id,
        "acc" if kind == "gemini_acc" else "faith",
        request,
        total,
        len(reasons),
        tuple(
            sorted(
                {src for (_, _, _, src), n in zip(evidence, lengths, strict=True) if n}
            )
        ),
        empty,
        tuple(reasons),
    )


def shuffle_order(answer_ids: Sequence[str], seed: int = 20261001) -> list[str]:
    ordered = sorted(answer_ids)
    random.Random(seed).shuffle(ordered)
    return ordered


def select_pilot(
    runs: Sequence[AnsweredRun],
    classes: Mapping[str, RunClass],
    *,
    seed: int = 20261001,
    n: int = 10,
) -> list[str]:
    pool = sorted(
        r.answer_id
        for r in runs
        if r.run_set == "dev"
        and r.model == "gpt-6-luna"
        and classes[r.answer_id] == "gemini_acc"
    )
    if len(pool) != len(set(pool)):
        raise ValueError("duplicate answer_id")
    return random.Random(seed).sample(pool, n)


class FaithGrade(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    faithfulness: int = Field(ge=0, le=3)
    rationale: str = Field(max_length=200)


class AccGrade(FaithGrade):
    correctness: int = Field(ge=0, le=3)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rubric_sha256(path: Path = RUBRIC_PATH) -> str:
    # Decode bytes directly to avoid universal-newline changes.
    return _text_hash(path.read_bytes().decode("utf-8"))


def schema_sha256() -> str:
    return _text_hash(
        json.dumps(
            {
                "AccGrade": AccGrade.model_json_schema(),
                "FaithGrade": FaithGrade.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def parse_rubric_sections(path: Path = RUBRIC_PATH) -> dict[str, str]:
    required = ("공통 지침", "정답성 앵커", "충실성 앵커", "출력 형식")
    sections: dict[str, str] = {}
    current = None
    for line in path.read_bytes().decode("utf-8").splitlines(keepends=True):
        if line.startswith("## "):
            current = line[3:].strip()
            if current not in required or current in sections:
                raise ValueError(f"invalid rubric section: {current}")
            sections[current] = ""
        elif current is not None:
            sections[current] += line
    if tuple(sections) != required or any(
        not text.strip() for text in sections.values()
    ):
        raise ValueError("rubric sections missing, empty or out of order")
    return sections


EVIDENCE_MANIFEST_PATH = Path("data/manifest/m7_evidence.json")


def check_evidence(
    root: Path = Path("."),
    *,
    answers: Sequence[AnsweredRun] | None = None,
    loader=None,
) -> dict[str, object]:
    """Offline report of grading items by evidence source and empty-evidence reason."""
    root = Path(root)
    answers = load_m6_answers(root) if answers is None else list(answers)
    classes = classify_all(answers)
    loader = EvidenceLoader(root) if loader is None else loader
    h5_path = root / "data/manifest/m6_eval_inputs.json"
    sample_path = root / "data/manifest/m7_human_sample.json"
    h5_ids = set(_json(h5_path)["h5_pool"]["ids"]) if h5_path.exists() else set()
    sample_ids = (
        set(_json(sample_path)["answer_ids"]) if sample_path.exists() else set()
    )
    sources, empty, failures = Counter(), defaultdict(list), []
    graded = 0
    for run in answers:
        if classes[run.answer_id] not in {"gemini_acc", "gemini_faith_only"}:
            continue
        graded += 1
        try:
            item = build_grade_item(run, run.question, loader)
        except EvidenceReconstructionError as exc:
            failures.append({"answer_id": run.answer_id, "error": str(exc)})
            continue
        sources.update(item.evidence_sources)
        if item.evidence_empty_reason is not None:
            empty[item.evidence_empty_reason].append(run.answer_id)

    def scoped(ids):
        return {
            reason: sorted(set(values) & ids)
            for reason, values in empty.items()
            if set(values) & ids
        }

    report = {
        "schema_version": 1,
        "graded_items": graded,
        "items_by_evidence_source": dict(sorted(sources.items())),
        "empty_evidence_total": sum(len(v) for v in empty.values()),
        "empty_evidence_by_reason": {k: len(v) for k, v in sorted(empty.items())},
        "empty_evidence_ids": {k: sorted(v) for k, v in sorted(empty.items())},
        "empty_in_kappa_sample": {k: len(v) for k, v in scoped(sample_ids).items()},
        "empty_in_h5_pool": {k: len(v) for k, v in scoped(h5_ids).items()},
        "reconstruction_failures": failures,
        "note": "빈 근거 항목은 충실성 집계에서 '근거 없음'으로 따로 표시하고 평균에서 제외한다. 정답성은 그대로 채점한다.",
    }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M7 채점 입력을 오프라인 검증합니다.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-inputs", action="store_true")
    mode.add_argument(
        "--check-evidence",
        action="store_true",
        help="채점 대상 근거 구성(출처별)과 근거 0자 건수(사유별)를 출력하고 manifest에 기록",
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_evidence:
            report = check_evidence(args.root)
        else:
            check = verify_m6_inputs(args.root)
    except InputHashError as exc:
        print(f"M7 입력 불일치: {exc}", file=sys.stderr)
        return 2
    if args.check_evidence:
        if not args.no_write:
            path = args.root / EVIDENCE_MANIFEST_PATH
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print(f"채점 대상 {report['graded_items']}건")
        print(f"근거 출처별 항목 수: {report['items_by_evidence_source']}")
        print(
            f"근거 0자: {report['empty_evidence_total']}건 "
            f"(사유별 {report['empty_evidence_by_reason']}, "
            f"κ 표본 {report['empty_in_kappa_sample']}, H5 풀 {report['empty_in_h5_pool']})"
        )
        print(f"SQL 재현 불일치: {len(report['reconstruction_failures'])}건")
        return 1 if report["reconstruction_failures"] else 0
    print(
        "OK\n" + json.dumps(asdict(check), ensure_ascii=False, indent=2, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
