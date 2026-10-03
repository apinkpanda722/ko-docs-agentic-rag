"""Impact of the lookup_id chunk/document mix-up (read-only; no model calls).

``size`` of ``authorized_lookup_id`` counts OpenSearch chunks, but the tool passed
``LOOKUP_DOCUMENT_LIMIT + 1`` as if it counted documents, so a few newest
multi-chunk documents filled the window and an older single-chunk document (the
incident report of INC-2026-010) could be missing. This module measures

* the corpus: for every incident/ticket identifier of the index, whether the
  tool's document list changed with the fix (owner-team grade-6 context), and
* the three silver questions that mention INC-2026-010, run by run, from the
  local M6/M7 traces: lookup_id calls, calls that omitted the missed document
  although the asker may read it, and the recorded score.

Only IDs, counts and scores are written (no question or answer text).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

OUTPUT = Path("data/manifest/m8_lookup_impact.json")
TARGET_ID = "INC-2026-010"
MISSED_DOC = "syn-incident-020"
QUESTIONS = ("s-syn-009", "s-syn-078", "s-syn-085")
GRADES = Path("data/manifest/m7_grade_scores.json")
DEV_RUNS = Path("data/interim/m6/dev/runs.jsonl")
TEST_SILVER_RUNS = Path("data/interim/m6/test/run-01/silver")
TRACE_GLOB = "logs/traces/*.jsonl"
RUN_SETS = {"dev": "dev", "test_silver": "test_silver"}


def lookup_calls(trace: Mapping[str, object], value: str) -> list[Mapping[str, object]]:
    """lookup_id tool steps of one trace for an identifier value."""
    steps = trace.get("steps") or []
    return [
        s
        for s in steps  # type: ignore[union-attr]
        if s.get("kind") == "tool"
        and s.get("name") == "lookup_id"
        and (s.get("args") or {}).get("value") == value
    ]


def omitted(call: Mapping[str, object], doc_id: str) -> bool:
    return doc_id not in {r.get("doc_id") for r in call.get("refs") or []}  # type: ignore[union-attr]


def score_of(grades: Mapping[str, object], answer_id: str) -> dict[str, object]:
    """Recorded score class and mean correctness (None when not graded)."""
    entry = grades.get(answer_id)
    if not isinstance(entry, Mapping):
        return {"class": None, "correctness_mean": None}
    values = entry.get("correctness") or []
    mean = sum(values) / len(values) if values else None
    return {"class": entry.get("class"), "correctness_mean": mean}


def run_rows(
    runs: Iterable[Mapping[str, object]],
    traces: Mapping[str, Mapping[str, object]],
    grades: Mapping[str, object],
    can_read: Callable[[str], bool],
) -> list[dict[str, object]]:
    rows = []
    for run in runs:
        question = str(run["question_id"])
        if question not in QUESTIONS:
            continue
        run_set = str(run["_run_set"])
        system, model = str(run["system"]), str(run["model"])
        trace = traces.get(str(run.get("trace_id")))
        calls = lookup_calls(trace, TARGET_ID) if trace else []
        readable = can_read(question)
        rows.append(
            {
                "question_id": question,
                "run_set": run_set,
                "system": system,
                "model": model,
                "asker_can_read_missed_doc": readable,
                "trace_found": trace is not None,
                # The simple system has no lookup_id tool.
                "lookup_id_calls_for_target": len(calls)
                if system == "agentic"
                else None,
                "calls_missing_report": (
                    sum(omitted(c, MISSED_DOC) for c in calls) if readable else 0
                )
                if system == "agentic"
                else None,
                "refused": bool(run.get("refused")),
                **score_of(grades, f"{run_set}|{question}|{system}|{model}"),
            }
        )
    return sorted(
        rows,
        key=lambda r: (r["question_id"], r["run_set"], r["system"], r["model"]),
    )


def corpus_measurement(
    targets: Sequence[tuple[str, str, str]],
    tool_documents: Callable[[str, str, str, int], list[str]],
    top5_documents: Callable[[str, str, str], list[str]],
) -> dict[str, object]:
    """Per kind: how many identifiers change with the fix, and agreement with top 5.

    ``tool_documents(kind, value, team, chunk_limit)`` is the tool's shaped list.
    ``top5_documents`` is the first five documents of a 50-chunk lookup. A tool
    list may be a PREFIX of the top five when the 6,000-character serialized
    budget dropped the last document (reported as ``budget_prefix``).
    """
    from kodocs.tools.deps import LOOKUP_CHUNK_REQUEST_LIMIT, LOOKUP_DOCUMENT_LIMIT

    result: dict[str, dict[str, object]] = {}
    changed: list[dict[str, object]] = []
    for kind, value, team in targets:
        part = result.setdefault(
            kind,
            {
                "identifiers": 0,
                "changed_by_fix": 0,
                "equal_top5": 0,
                "budget_prefix": 0,
            },
        )
        new = tool_documents(kind, value, team, LOOKUP_CHUNK_REQUEST_LIMIT)
        old = tool_documents(kind, value, team, LOOKUP_DOCUMENT_LIMIT + 1)
        top5 = top5_documents(kind, value, team)
        part["identifiers"] += 1  # type: ignore[operator]
        if new != old:
            part["changed_by_fix"] += 1  # type: ignore[operator]
            changed.append(
                {
                    "kind": kind,
                    "value": value,
                    "added": [d for d in new if d not in old],
                }
            )
        if new == top5:
            part["equal_top5"] += 1  # type: ignore[operator]
        elif new == top5[: len(new)]:
            part["budget_prefix"] += 1  # type: ignore[operator]
    return {"by_kind": result, "changed": changed}


def build(root: Path = Path(".")) -> dict[str, object]:
    """Real run: local traces/runs, golden askers and a read-only OpenSearch."""
    import glob

    import httpx

    from kodocs.retrieval.access import UserContext
    from kodocs.retrieval.document import authorized_get_document
    from kodocs.retrieval.lookup import authorized_lookup_id
    from kodocs.tools import run as run_module
    from kodocs.tools import run_tool
    from kodocs.tools.deps import LOOKUP_DOCUMENT_LIMIT, TOOL_INDEX, build_tool_deps

    root = Path(root)
    deps = build_tool_deps(root=root)
    traces = {}
    for path in sorted(glob.glob(str(root / TRACE_GLOB))):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("question_id") in QUESTIONS:
                    traces[record["trace_id"]] = record
    runs = []
    sources = [(root / DEV_RUNS, "dev")] + [
        (p, "test_silver") for p in sorted((root / TEST_SILVER_RUNS).glob("*.jsonl"))
    ]
    for path, run_set in sources:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("question_id") in QUESTIONS:
                    runs.append({**row, "_run_set": run_set})
    askers = {}
    for name in ("gold", "silver"):
        for line in (
            (root / f"data/golden/queries/{name}.jsonl").read_text().splitlines()
        ):
            q = json.loads(line)
            if q["id"] in QUESTIONS:
                askers[q["id"]] = UserContext(q["asker"]["team"], q["asker"]["grade"])
    grades = json.loads((root / GRADES).read_text(encoding="utf-8"))["scores"]

    def can_read(question: str) -> bool:
        return (
            authorized_get_document(
                deps.client, TOOL_INDEX, askers[question], MISSED_DOC, size=1
            )
            is not None
        )

    owners = _owners(root)
    aggs = httpx.post(
        "http://localhost:9200/" + TOOL_INDEX + "/_search",
        json={
            "size": 0,
            "aggs": {
                "incident": {"terms": {"field": "ids_incident", "size": 100}},
                "ticket": {"terms": {"field": "ids_ticket", "size": 200}},
            },
        },
    ).json()["aggregations"]
    targets = [
        (kind, bucket["key"], owners[(kind, bucket["key"])])
        for kind in ("incident", "ticket")
        for bucket in aggs[kind]["buckets"]
    ]

    original = run_module.LOOKUP_CHUNK_REQUEST_LIMIT

    def tool_documents(kind: str, value: str, team: str, chunk_limit: int) -> list[str]:
        run_module.LOOKUP_CHUNK_REQUEST_LIMIT = chunk_limit
        try:
            out = run_tool(
                "lookup_id", {"kind": kind, "value": value}, UserContext(team, 6), deps
            )
        finally:
            run_module.LOOKUP_CHUNK_REQUEST_LIMIT = original
        return [d["doc_id"] for d in out.payload["documents"]]

    def top5(kind: str, value: str, team: str) -> list[str]:
        hits = authorized_lookup_id(
            deps.client,
            TOOL_INDEX,
            UserContext(team, 6),
            kind,
            value,
            size=50,
            prefer_current=deps.prefer_current,
        )
        return [h.doc_id for h in hits[:LOOKUP_DOCUMENT_LIMIT]]

    return {
        "schema_version": 1,
        "target": {"identifier": TARGET_ID, "missed_document": MISSED_DOC},
        "context": "owner team, grade 6 (corpus measurement); read-only OpenSearch/SQLite",
        "corpus": corpus_measurement(targets, tool_documents, top5),
        "runs": run_rows(runs, traces, grades, can_read),
        "no_rerun_reason": "방향 확인용 실버 문항이고, 결과를 본 뒤 일부만 다시 돌리면 선택적 재실행이 된다.",
    }


def _owners(root: Path) -> dict[tuple[str, str], str]:
    import sqlite3

    import yaml

    owners: dict[tuple[str, str], str] = {}
    for path in sorted((root / "data/seed/incidents").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        owners[("incident", document["id"])] = document["owner_team"]
    connection = sqlite3.connect(
        f"file:{root / 'data/synthetic/ledger.sqlite'}?mode=ro", uri=True
    )
    try:
        for ticket_id, team in connection.execute(
            "select ticket_id, owner_team from tickets"
        ):
            owners[("ticket", ticket_id)] = team
    finally:
        connection.close()
    return owners


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = build(args.root)
    path = args.root / OUTPUT
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    corpus = result["corpus"]["by_kind"]  # type: ignore[index]
    print(f"lookup 영향 측정: {corpus} → {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
