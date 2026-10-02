"""Offline, exclusive M7-F1/F2/F3 failure classification of frozen M6 answers.

M6 schema v1 records tool refs, args, errors and result_chars, but neither
50-candidate lists nor result bodies. No corpus text, citation evidence, or
fresh retrieval is substituted for final context. Richer *input fixtures* may
supply candidates_50 and tool-step result payloads; these are optional analysis
adapter fields, not claims about fields present in the actual M6 trace schema.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from kodocs.agent.evidence import EvidenceLog, round_robin_context
from kodocs.chunking.corpus import load_corpus
from kodocs.eval.golden import QUESTION_TYPES, Question, can_read
from kodocs.eval.m7_analyze import verify_analysis_inputs
from kodocs.eval.m7_grading import (
    AnsweredRun,
    classify_all,
    classify_run,
    load_m6_answers,
)
from kodocs.eval.m7_stats import median_int
from kodocs.ingest.manifest import sha256_file
from kodocs.tools import ToolResult

MANIFEST_PATH = Path("data/manifest/m7_failures.json")
DETAILS_PATH = Path("data/interim/m7/failures.jsonl")
REQUIRED_TOOLS = {
    typ: frozenset({"lookup_id"})
    if typ == "identifier"
    else frozenset({"run_sql"})
    if typ == "sql"
    else frozenset({"search_docs", "get_document"})
    for typ in QUESTION_TYPES
}
CLASSES = (
    "guardrail_infra",
    "permission_overrefusal",
    "tool_selection",
    "tool_argument",
    "structural_tool_absence",
    "d1",
    "d2",
    "generation",
    "indeterminate",
)
CLASS_DESCRIPTIONS = dict(
    zip(
        CLASSES,
        (
            "가드레일·인프라 실패",
            "권한 오거절",
            "도구 선택 오류",
            "인자 오류",
            "도구 부재(구조적)",
            "d1 검색 실패: M8 리랭커 근거",
            "d2 예산 탈락: M8 small-to-big 근거",
            "생성 오류",
            "기록 부족으로 판정 불가",
        ),
        strict=True,
    )
)
# Only the explicitly human-confirmed section is listed. The other 13 section
# keys were not provided; the aggregate audit is a limitation, not inferred labels.
DATA_PREPARATION_NOTES = (
    {
        "doc_id": "law-2200000149621",
        "annex_key": "별표2",
        "annex_id": "17229_0-00",
        "loss_type": "annex_context_lost",
        "confirmed_by": "human",
        "stage": "m5_composite_assembly",
        "pypdf_chars": 797,
        "composite_chars": 293,
    },
)


@dataclass(frozen=True)
class FailureClass:
    category: str
    missing: tuple[str, ...] = ()
    partial: bool = False


def load_docs_meta(root: Path) -> dict[str, dict]:
    """Read access metadata locally, using the same composite corpus as M5."""
    documents, _ = load_corpus("composite", root=root)
    return {d.doc_id: {"access": asdict(d.access)} for d in documents}


def visible_relevance(question: Question, docs_meta: Mapping) -> dict[str, int]:
    """Maximum positive doc relevance after the M5 asker visibility filter."""
    result = {}
    for qrel in question.qrels:
        if qrel.rel < 1:
            continue
        document = docs_meta.get(qrel.doc_id)
        if document is None or not isinstance(document.get("access"), Mapping):
            raise ValueError(
                f"{question.id}: missing access metadata for {qrel.doc_id}"
            )
        if can_read(question.asker, document["access"]):
            result[qrel.doc_id] = max(result.get(qrel.doc_id, 0), qrel.rel)
    return result


def _doc_ids(refs: Sequence[Mapping]) -> set[str]:
    return {r["doc_id"] for r in refs if isinstance(r.get("doc_id"), str)}


def _tool_steps(trace):
    if trace is None or not isinstance(trace.get("steps"), list):
        return None
    return sorted(
        (s for s in trace["steps"] if s.get("kind") == "tool"), key=lambda s: s["index"]
    )


def _final_docs(run, steps):
    """Rebuild only from complete recorded payloads, with actual evidence rules.

    result_chars/refs are checked when reconstructing. Citations alone cannot
    prove context completeness. A citation outside the reconstruction invalidates
    it rather than moving a run down the ladder to d2.
    """
    log = EvidenceLog()
    for step in steps:
        error = step.get("error") is not None
        payload = step.get("result")
        if not error and not isinstance(payload, dict):
            return None, "final_context"
        if error:
            payload = {}
        elif "result_chars" in step:
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len(serialized) != step["result_chars"]:
                return None, "result_payload_conflict"
        refs = step.get("refs")
        if not isinstance(refs, list):
            return None, "tool_refs"
        name = step["name"]
        if not error:
            if name == "search_docs":
                items = payload.get("results")
            elif name == "lookup_id":
                items = payload.get("documents")
            elif name == "get_document":
                items = [payload]
            elif name == "run_sql":
                items = []
            else:
                return None, "final_context"
            if not isinstance(items, list) or _doc_ids(items) != _doc_ids(refs):
                return None, "result_refs_conflict"
        try:
            log.record(
                name,
                step.get("args") or {},
                ToolResult(payload, error, tuple(refs), None),
            )
        except (TypeError, KeyError, ValueError):
            return None, "final_context"
    context = round_robin_context(log, budget=6000)
    # Mirror the same rank-across-calls traversal to identify which labels have
    # nonempty text. Search payloads already carry their 8-chunk/6000-char cap.
    ordered = [
        call.items[rank]
        for rank in range(max((len(c.items) for c in log.calls), default=0))
        for call in log.calls
        if rank < len(call.items)
    ]
    remaining, docs, sql_calls, sections = 6000, set(), set(), set()
    for item in ordered:
        if remaining <= 0:
            break
        length = min(remaining, len(item.text))
        if length:
            if item.label.startswith("[SQL "):
                sql_calls.add(item.label[5:-1])
            else:
                doc_id, section, _title = item.label[1:-1].split(" | ", 2)
                docs.add(doc_id)
                sections.add((doc_id, section))
        remaining -= length
        if length < len(item.text):
            break
    if not context and (docs or sql_calls):
        return None, "final_context"
    for citation in run.citations:
        if (
            citation.get("kind") == "doc"
            and (
                citation.get("doc_id") not in docs
                or (
                    citation.get("section_key") is not None
                    and (citation.get("doc_id"), citation["section_key"])
                    not in sections
                )
            )
        ) or (
            citation.get("kind") == "sql" and citation.get("call_id") not in sql_calls
        ):
            return None, "citation_context_conflict"
    return docs, None


def classify_failure(
    run: AnsweredRun,
    question: Question,
    trace: Mapping | None,
    *,
    docs_meta: Mapping | None = None,
) -> FailureClass:
    """First matching M7-F2 rung; missing prerequisite data stops the ladder."""
    det_zero = classify_run(run, question) == "det_zero"
    reason = run.refuse_reason or ""
    if det_zero and (
        reason.startswith("guardrail_")
        or reason == "parse_error"
        or run.status != "completed"
    ):
        return FailureClass("guardrail_infra")
    relevance = visible_relevance(
        question, load_docs_meta(run.root) if docs_meta is None else docs_meta
    )
    if run.refused and relevance:
        return FailureClass("permission_overrefusal")
    if run.system == "simple" and question.type == "sql":
        return FailureClass("structural_tool_absence")
    steps = _tool_steps(trace)
    if run.system == "agentic":
        if steps is None:
            return FailureClass("indeterminate", ("tool_calls",))
        if not REQUIRED_TOOLS[question.type].intersection(s["name"] for s in steps):
            return FailureClass("tool_selection")
    if run.system == "agentic" and steps is not None:
        for step in steps:
            error = step.get("error")
            if error in {"invalid_argument", "limit_exceeded"} or (
                step.get("name") == "run_sql"
                and isinstance(error, str)
                and (
                    "sql" in error.casefold()
                    or error
                    in {
                        "OperationalError",
                        "ProgrammingError",
                        "DatabaseError",
                        "SQLValidationError",
                    }
                )
            ):
                return FailureClass("tool_argument")
    truth = {doc_id for doc_id, rel in relevance.items() if rel == 2}
    if not truth:
        return FailureClass("indeterminate", ("rel2_truth",))
    if run.system == "simple":
        candidates = None if trace is None else trace.get("candidates_50")
        if not isinstance(candidates, list):
            return FailureClass("indeterminate", ("candidates_50",))
        retrieved = _doc_ids(candidates)
    elif run.system == "agentic":
        if any(not isinstance(s.get("refs"), list) for s in steps):
            return FailureClass("indeterminate", ("tool_refs",))
        retrieved = _doc_ids([r for s in steps for r in s["refs"]])
    else:
        raise ValueError("unknown M6 system")
    if not truth <= retrieved:
        return FailureClass("d1", partial=bool(truth & retrieved))
    if steps is None:
        return FailureClass("indeterminate", ("final_context",))
    final_steps = [
        s
        for s in trace["steps"]
        if s.get("kind") == "model" and s.get("name") == "final_answer"
    ]
    if len(final_steps) != 1 or any(
        s["index"] >= final_steps[0]["index"] for s in steps
    ):
        return FailureClass("indeterminate", ("final_context",))
    context_docs, missing = _final_docs(run, steps)
    if missing:
        return FailureClass("indeterminate", (missing,))
    return FailureClass("generation" if truth <= context_docs else "d2")


def is_wrong(run: AnsweredRun, score: Mapping) -> bool:
    if run.expected_response != "answer":
        return False
    if score["class"] == "det_zero":
        return True
    if score["class"] != "gemini_acc":
        raise ValueError(f"{run.answer_id}: invalid correctness class")
    values = score["correctness"]
    median_int(values)  # Shared 3-slot validation, including one missing repeat.
    return statistics.mean(v for v in values if v is not None) < 2.0


def _bucket():
    return {
        "wrong_count": 0,
        "classification_total": 0,
        "classification_sum_matches": True,
        "classes": {c: {"count": 0, "ids": []} for c in CLASSES},
        "d1_partial": {"count": 0, "ids": []},
        "indeterminate_by_reason": {},
        "refusal_violations": {"count": 0, "ids": []},
    }


def _add(bucket, question_id, result):
    bucket["wrong_count"] += 1
    slot = bucket["classes"][result.category]
    slot["ids"].append(question_id)
    slot["count"] += 1
    if result.partial:
        bucket["d1_partial"]["ids"].append(question_id)
        bucket["d1_partial"]["count"] += 1
    for reason in result.missing:
        slot = bucket["indeterminate_by_reason"].setdefault(
            reason, {"count": 0, "ids": []}
        )
        slot["ids"].append(question_id)
        slot["count"] += 1


def analyze_failures(
    runs: Sequence[AnsweredRun], scores: Mapping, traces: Mapping, docs_meta: Mapping
) -> tuple[dict, list[dict]]:
    test_runs = sorted(
        (r for r in runs if r.run_set in {"test_gold", "test_silver"}),
        key=lambda r: r.answer_id,
    )
    classes = classify_all(test_runs)
    seen, groups, details = set(), {}, []
    for run in test_runs:
        key = run.system, run.run_set, run.question_id
        if key in seen:
            raise ValueError("duplicate question/system/set")
        seen.add(key)
        score = scores.get(run.answer_id)
        if score is None or score.get("class") != classes[run.answer_id]:
            raise ValueError(f"{run.answer_id}: missing or inconsistent score")
        group = groups.setdefault(
            f"{run.system}|{run.run_set}",
            {
                **_bucket(),
                "by_type": {t: _bucket() for t in QUESTION_TYPES},
                "total_runs": 0,
            },
        )
        group["total_runs"] += 1
        buckets = group, group["by_type"][run.type]
        if run.expected_response != "answer" and not run.refused:
            for bucket in buckets:
                bucket["refusal_violations"]["count"] += 1
                bucket["refusal_violations"]["ids"].append(run.question_id)
        if not is_wrong(run, score):
            continue
        result = classify_failure(
            run, run.question, traces.get(run.trace_id), docs_meta=docs_meta
        )
        for bucket in buckets:
            _add(bucket, run.question_id, result)
        notes = [
            dict(note)
            for note in DATA_PREPARATION_NOTES
            if any(
                q.doc_id == note["doc_id"] and q.annex_id == note["annex_id"]
                for q in run.question.qrels
            )
        ]
        details.append(
            {
                "answer_id": run.answer_id,
                "question_id": run.question_id,
                "system": run.system,
                "run_set": run.run_set,
                "type": run.type,
                **asdict(result),
                "data_preparation_notes": notes,
            }
        )
    for group in groups.values():
        for bucket in (group, *group["by_type"].values()):
            ids = [i for slot in bucket["classes"].values() for i in slot["ids"]]
            bucket["classification_total"] = sum(
                s["count"] for s in bucket["classes"].values()
            )
            bucket["classification_sum_matches"] = (
                bucket["classification_total"] == bucket["wrong_count"] == len(set(ids))
            )
            if not bucket["classification_sum_matches"]:
                raise ValueError(
                    "failure classifications must be mutually exclusive and exhaustive"
                )
    notes = [
        {
            **note,
            "wrong_answer_ids": sorted(
                d["answer_id"] for d in details if note in d["data_preparation_notes"]
            ),
        }
        for note in DATA_PREPARATION_NOTES
    ]
    report = {
        "schema_version": 1,
        "rules": ["M7-F1", "M7-F2", "M7-F3"],
        "class_descriptions": CLASS_DESCRIPTIONS,
        "by_system_set": groups,
        "data_preparation_notes": notes,
        "measurement_limitations": {
            "annex_context_audit": {
                "sections_compared": 493,
                "below_70_percent": 95,
                "unit_label_lost_sections": 14,
            },
            "shared_title_document_pairs": [
                ["syn-meeting-037", "syn-meeting-029"],
                ["syn-runbook-002", "syn-runbook-018"],
            ],
            "security_tool_error": {
                "scenario_id": "sec-0072",
                "tool": "get_document",
                "error": "not_found",
                "classification": "tool_error",
            },
            "note": "데이터 준비·측정 한계이며 실패 분류 사다리와 별도로 보고한다. 사람 메모는 판정 입력으로 사용하지 않는다.",
        },
    }
    return report, details


def load_traces(root: Path) -> tuple[dict, dict]:
    traces, hashes = {}, {}
    for path in sorted((root / "logs/traces").glob("*.jsonl")):
        hashes[path.relative_to(root).as_posix()] = sha256_file(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            trace_id = record["trace_id"]
            if trace_id in traces:
                raise ValueError("duplicate trace_id")
            traces[trace_id] = record
    return traces, hashes


def run_failures(root: Path = Path(".")) -> dict:
    root = Path(root)
    scores = verify_analysis_inputs(root)["scores"]
    runs = load_m6_answers(root)
    traces, hashes = load_traces(root)
    report, details = analyze_failures(runs, scores, traces, load_docs_meta(root))
    report["input_sha256"] = {
        **hashes,
        **{
            p: sha256_file(root / p)
            for p in (
                "data/manifest/m7_grade_scores.json",
                "data/manifest/m5_config.json",
                "data/manifest/m6_config.json",
                "data/manifest/m5_eval_inputs.json",
                "docs/results/hypotheses.md",
            )
            if (root / p).exists()
        },
    }
    for path, content in (
        (
            MANIFEST_PATH,
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        ),
        (
            DETAILS_PATH,
            "".join(
                json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n"
                for d in details
            ),
        ),
    ):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        report = run_failures(args.root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"M7 failure analysis refused: {type(exc).__name__}: {exc}")
        return 2
    print(
        "M7 failures: "
        + ", ".join(
            f"{k}={v['wrong_count']}" for k, v in report["by_system_set"].items()
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
