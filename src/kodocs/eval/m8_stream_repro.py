"""User-run serial M8 output-runaway reproduction; dry-run is offline."""

import argparse
import json
import math
import os
import random
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import httpx
from langchain_core.utils.json import parse_partial_json

from kodocs.agent.answer import answer
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.types import AgentRuntime, canonical_hash
from kodocs.config import load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
)
from kodocs.eval.m6_config import CONFIG_PATH, verify_config
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.eval.m7_rawhash import fingerprint, verify_fingerprint
from kodocs.eval.m8_parse_error import LATENCY, LEDGER, RAW, ledger_requests, load_rows
from kodocs.ingest.manifest import now_iso
from kodocs.llm.answer_settings import AnswerModelConfig
from kodocs.llm.openai_chat import (
    ChatParseError,
    FatalLLMError,
    ManagedChat,
    _stream_diagnostics,
    _text_deltas,
)
from kodocs.tools import UserContext, build_tool_deps

SEED = 20261003
MANIFEST = Path("data/manifest/m8_stream_repro.json")
RAW_DIR = Path("data/interim/m8/repro")
TARGETS = (
    ("s-syn-050", "simple"),
    ("s-syn-139", "simple"),
    ("g-syn-022", "agentic"),
    ("s-syn-034", "agentic"),
)
PROGRESS_EVERY = 10


# Three request paths. Schemas come from installed libraries only
# (openai_chat.callback_response_format); "A" is the frozen M6 request untouched.
PATHS = {
    "A": ("nonstreaming", "sdk"),  # M6: with_structured_output -> responses.parse
    "B": ("streaming", "langchain"),  # M7: streamed pydantic class schema
    "C": ("streaming", "sdk"),  # UI path: streaming + the M6 (sdk) schema
}


def _item(question_id, system, path):
    mode, schema = PATHS[path]
    return {
        "question_id": question_id,
        "system": system,
        "path": path,
        "mode": mode,
        "schema": schema,
    }


def stage_one():
    """4 questions x 3 paths x 25; seeded shuffle, every triple crosses paths."""
    rng = random.Random(SEED)
    pairs = list(TARGETS) * 25
    rng.shuffle(pairs)
    result = []
    for question_id, system in pairs:
        paths = list(PATHS)
        rng.shuffle(paths)
        result.extend(_item(question_id, system, path) for path in paths)
    return result


def is_runaway(output_tokens, parse_failed):
    return parse_failed or (output_tokens is not None and output_tokens >= 4000)


def _repetition(text):
    # Suffix automaton: longest substring with >=2 occurrences, overlaps allowed.
    states = [{"length": 0, "link": -1, "next": {}, "count": 0}]
    last = 0
    for char in text:
        current = len(states)
        states.append(
            {"length": states[last]["length"] + 1, "link": 0, "next": {}, "count": 1}
        )
        p = last
        while p >= 0 and char not in states[p]["next"]:
            states[p]["next"][char] = current
            p = states[p]["link"]
        if p >= 0:
            q = states[p]["next"][char]
            if states[p]["length"] + 1 == states[q]["length"]:
                states[current]["link"] = q
            else:
                clone = len(states)
                states.append(
                    {
                        **states[q],
                        "length": states[p]["length"] + 1,
                        "next": dict(states[q]["next"]),
                        "count": 0,
                    }
                )
                while p >= 0 and states[p]["next"].get(char) == q:
                    states[p]["next"][char] = clone
                    p = states[p]["link"]
                states[q]["link"] = states[current]["link"] = clone
        last = current
    for i in sorted(
        range(1, len(states)), key=lambda i: states[i]["length"], reverse=True
    ):
        states[states[i]["link"]]["count"] += states[i]["count"]
    repeated = [s for s in states[1:] if s["count"] >= 2]
    best = max(repeated, key=lambda s: (s["length"], s["count"]), default=None)
    return (best["length"], best["count"]) if best else (0, 0)


def _active_field(text):
    decoder = json.JSONDecoder()
    position = 1
    if not text.lstrip().startswith("{"):
        return None
    position = text.index("{") + 1
    while position < len(text):
        while position < len(text) and text[position] in " \n\r\t,":
            position += 1
        try:
            key, end = decoder.raw_decode(text, position)
        except ValueError:
            return None
        position = end
        while position < len(text) and text[position] in " \n\r\t:":
            position += 1
        try:
            _, position = decoder.raw_decode(text, position)
        except ValueError:
            return key if key in {"answer", "citations", "refused"} else None
    return None


def shape_statistics(text):
    try:
        parsed = parse_partial_json(text) or {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    length, count = _repetition(text)
    return {
        "active_field": _active_field(text),
        "field_chars": {
            key: len(value)
            if isinstance(value, str)
            else len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            for key, value in parsed.items()
            if key in {"answer", "citations", "refused"}
        },
        "whitespace_ratio": sum(c.isspace() for c in text) / len(text) if text else 0,
        "newline_ratio": text.count("\n") / len(text) if text else 0,
        "repetition_length": length,
        "repetition_count": count,
        "repetition_definition": "longest repeated substring, overlapping occurrences included",
        "citations_count": len(parsed["citations"])
        if isinstance(parsed.get("citations"), list)
        else 0,
    }


def read_result(root):
    path = root / MANIFEST
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if result.get("result_sha256") != canonical_hash(
        {k: v for k, v in result.items() if k != "result_sha256"}
    ):
        raise ValueError("M8 reproduction result hash mismatch")
    previous = None
    for entry in result.get("history", []):
        if entry["previous_result_sha256"] != previous or entry[
            "result_sha256"
        ] != canonical_hash({k: v for k, v in entry.items() if k != "result_sha256"}):
            raise ValueError("M8 reproduction history hash mismatch")
        previous = entry["result_sha256"]
        for row in entry["runs"]:
            raw = row.get("raw")
            if raw and fingerprint(root / raw["path"]) != {
                k: raw[k] for k in ("sha256", "rows")
            }:
                raise ValueError("M8 reproduction raw hash mismatch")
    return result


def preflight(root, stage):
    check = verify_config(root / CONFIG_PATH)
    if not check.ok:
        raise ValueError("M6 config mismatch: " + ",".join(check.mismatches))
    frozen = json.loads((root / CONFIG_PATH).read_text())
    model = AnswerModelConfig(**frozen["answer"]["settings"])
    if (model.model, model.effort, model.max_output_tokens) != (
        "gpt-6-luna",
        "low",
        4000,
    ):
        raise ValueError("M6 model settings mismatch")
    if stage == 1:
        plan = stage_one()
    elif stage == 2:
        old = read_result(root)
        completed = [
            e
            for e in (old or {}).get("history", [])
            if e["stage"] == 1 and e["status"] == "completed"
        ]
        if not completed:
            raise ValueError("stage 1 completion required")
        count = sum(r["runaway"] for r in completed[0]["runs"])
        print(f"1단계 폭주 {count}건", flush=True)
        if count:
            raise ValueError("stage 2 requires zero stage 1 runaways")
        # m7_latency has no IDs field: its authenticated runs.jsonl pins the
        # original 113-question population and systems. Do not infer a new set.
        verify_fingerprint(root / LATENCY, "latency", root / RAW)
        latency = json.loads((root / LATENCY).read_text())
        seq = latency["history"][-1]["seq"]
        source = [r for r in load_rows(root / RAW) if r["run_seq"] == seq]
        if len(source) != 226 or len({r["question_id"] for r in source}) != 113:
            raise ValueError("M7 latency population mismatch")
        population = {r["question_id"] for r in source}
        if {(r["question_id"], r["system"]) for r in source} != {
            (q, s) for q in population for s in ("simple", "agentic")
        }:
            raise ValueError("M7 latency system/question pairs mismatch")
        rng = random.Random(SEED)
        plan = []
        for r in source:
            paths = ["B", "C"]
            rng.shuffle(paths)
            plan.extend(_item(r["question_id"], r["system"], path) for path in paths)
    else:
        raise ValueError("stage must be 1 or 2")
    questions = {
        q.id: q
        for split in ("dev", "test")
        for q in load_m6_questions(root, split=split)
    }
    if any(row["question_id"] not in questions for row in plan):
        raise ValueError("question IDs missing")
    return frozen, model, questions, plan


def estimate(root, stage):
    frozen, _, _, plan = preflight(root, stage)
    requests = [
        r
        for r in ledger_requests(load_rows(root / LEDGER))
        if r["run_id"] == "m7-latency"
    ]
    runaway = [r["amount_usd"] for r in requests if r["output_tokens"] >= 4000]
    if not requests or not runaway:
        raise ValueError("latency cost basis missing")
    mean = math.fsum(r["amount_usd"] for r in requests) / len(requests)
    verify_fingerprint(root / LATENCY, "latency", root / RAW)
    rows = load_rows(root / RAW)
    executions = (
        len(rows) + json.loads((root / LATENCY).read_text())["warmup_excluded_count"]
    )
    # At most one agent turn per reserved tool, one exit turn, one final call.
    # Use the frozen guard limit, not an observed call count as an upper bound.
    max_calls = frozen["guardrails"]["settings"]["max_steps"] + 2
    upper_requests = sum(1 if p["system"] == "simple" else max_calls for p in plan)
    return {
        "stage": stage,
        "execution_count": len(plan),
        "request_mean_usd": mean,
        "runaway_request_cost_usd": max(runaway),
        "max_model_calls": max_calls,
        "upper_request_count": upper_requests,
        "estimated_cost_usd": mean * len(requests) / executions * len(plan),
        "upper_cost_usd": max(runaway) * upper_requests,
        "basis": "M7 latency request mean × requests/answer (warmups included); all requests at maximum recorded 4000-token request cost, with frozen model-call ceiling",
    }


def budget(root, *, execute=False):
    settings = load_settings() if execute else load_settings(env=os.environ)
    return BudgetGuard(
        CostLedger(root / DEFAULT_LEDGER_PATH), settings.monthly_budget_usd
    )


def dry_run(root, stage=1):
    result = estimate(root, stage)
    guard = budget(root)
    guard.check(result["upper_cost_usd"])
    return result


class RecordingTransport(httpx.BaseTransport):
    """Keep the last NON-streaming Responses body in memory (path A only).

    The frozen M6 path raises before returning anything when a truncated JSON
    fails SDK parsing, so neither raw text nor usage would survive. The request
    is untouched; streaming bodies are never read (that would defeat streaming).
    Never written to disk or committed from here.
    """

    def __init__(self, inner=None):
        self.inner = inner or httpx.HTTPTransport()
        self.last_body = None

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        try:
            non_streaming = json.loads(request.content or b"{}").get("stream") is False
        except ValueError:
            non_streaming = False
        if non_streaming and response.status_code == 200:
            response.read()
            self.last_body = response.content
        return response

    def close(self):
        self.inner.close()


def parse_recorded_body(body):
    """Text and content-free diagnostics from a Responses JSON body."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = "".join(
        part.get("text", "")
        for item in data.get("output") or []
        if isinstance(item, dict)
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text"
    )
    usage = data.get("usage") or {}
    details = data.get("incomplete_details") or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    status = data.get("status")
    return {
        "text": text,
        "diagnostics": {
            "status": status
            if status in {"completed", "incomplete", "failed", "in_progress"}
            else None,
            "incomplete_reason": reason
            if reason in {"max_output_tokens", "content_filter"}
            else None,
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            "output_chars": len(text),
        },
    }


class CaptureChat:
    """Final-only path selection (A/B/C); raw text stays in memory only."""

    def __init__(self, inner, path, recorder=None):
        self.inner, self.path, self.recorder = inner, path, recorder
        self.text = []
        self.final_usage = None
        self.diagnostics = {}

    def invoke(
        self,
        messages,
        *,
        tools=None,
        response_schema=None,
        stream=False,
        on_text_delta=None,
    ):
        kwargs = {"tools": tools} if tools is not None else {}
        mode, schema = PATHS[self.path]
        if response_schema is not None:
            kwargs["response_schema"] = response_schema
            if mode == "streaming":
                kwargs.update(
                    stream=True, on_text_delta=self.text.append, schema_style=schema
                )
            elif self.recorder is not None:
                self.recorder.last_body = None
            # Path A adds nothing: the request is the frozen M6 one.
        try:
            result = self.inner.invoke(messages, **kwargs)
        except ChatParseError as exc:
            self.diagnostics = dict(exc.diagnostics or {})
            raw = getattr(exc, "response_message", None)
            if raw is not None:
                self.diagnostics = self.diagnostics or _stream_diagnostics(raw)
                if not self.text:
                    self.text.extend(_text_deltas(raw))
            if mode == "nonstreaming" and self.recorder is not None:
                recorded = parse_recorded_body(self.recorder.last_body)
                if recorded is not None:
                    self.diagnostics = recorded["diagnostics"]
                    self.text[:] = [recorded["text"]]
            raise
        if response_schema is not None:
            self.final_usage = result.usage
            self.diagnostics = _stream_diagnostics(result.message)
            if mode == "nonstreaming" and not self.text:
                self.text.extend(_text_deltas(result.message))
        return result


def _write(root, result):
    path = root / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    result["result_sha256"] = canonical_hash(
        {k: v for k, v in result.items() if k != "result_sha256"}
    )
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def run(
    root,
    stage=1,
    rerun_reason=None,
    *,
    runtime_factory=None,
    answer_fn=answer,
    clock=time.perf_counter,
):
    root = Path(root)
    frozen, model, questions, plan = preflight(root, stage)
    forecast = estimate(root, stage)
    budget(root, execute=True).check(forecast["upper_cost_usd"])
    old = read_result(root) or {"history": []}
    previous_stage = [e for e in old["history"] if e["stage"] == stage]
    if previous_stage and not (rerun_reason and rerun_reason.strip()):
        raise ValueError("RERUN_REASON required for this stage")
    raw_dir = root / RAW_DIR
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", str(RAW_DIR / "probe.txt")],
        cwd=root,
        check=False,
    )
    if ignored.returncode != 0:
        raise ValueError("raw output path must be gitignored")
    raw_dir.mkdir(parents=True, exist_ok=True)
    lock = raw_dir / f".stage-{stage}.lock"
    with lock.open("x") as handle:
        handle.write(str(os.getpid()))
    entry = {
        "stage": stage,
        "seq": len(old["history"]) + 1,
        "ran_at": now_iso(),
        "status": "running",
        "rerun_reason": rerun_reason,
        "config_hash": frozen["config_hash"],
        "seed": SEED,
        "concurrency": 1,
        "runs": [],
        "previous_result_sha256": old["history"][-1]["result_sha256"]
        if old["history"]
        else None,
    }
    deps = None
    recorder = None
    try:
        if runtime_factory is None:
            settings = load_settings()  # FP-056: real .env, never the offline mapping
            recorder = RecordingTransport()
            chat = ManagedChat(
                model,
                settings,
                http_client=httpx.Client(transport=recorder),
                ledger_path=root / DEFAULT_LEDGER_PATH,
                run_id=f"m8-stream-repro-stage-{stage}",
            )
            deps = build_tool_deps(root=root)
            runtime = AgentRuntime(
                deps,
                chat,
                model.record() | {"settings_hash": model.settings_hash()},
                frozen["m5_config_hash"],
                guardrails=GuardrailConfig(**frozen["guardrails"]["settings"]),
                trace_dir=root / "logs/traces",
            )
        else:
            runtime = runtime_factory(model)
        if (
            runtime.m5_config_hash != frozen["m5_config_hash"]
            or runtime.guardrails.settings_hash()
            != frozen["guardrails"]["settings_hash"]
        ):
            raise ValueError("runtime frozen config mismatch")
        for index, item in enumerate(plan):
            capture = CaptureChat(runtime.chat, item["path"], recorder)
            q = questions[item["question_id"]]
            row = {
                **item,
                "status": "completed",
                "output_tokens": None,
                "reasoning_tokens": None,
                "incomplete_reason": None,
                "latency_s": None,
                "runaway": False,
            }
            started = clock()
            stop = None
            try:
                result = answer_fn(
                    q.question,
                    UserContext(q.asker.team, q.asker.grade),
                    system=item["system"],
                    runtime=replace(runtime, chat=capture),
                    question_id=q.id,
                )
                parse_failed = result.refuse_reason == "parse_error"
                if parse_failed:
                    row["status"] = "parse_error"
            except BaseException as exc:  # noqa: BLE001 - persist each failure, including user stop
                row["status"] = type(exc).__name__
                parse_failed = isinstance(exc, ChatParseError)
                if isinstance(
                    exc, (FatalLLMError, BudgetExceededError)
                ) or not isinstance(exc, Exception):
                    stop = exc
            row["latency_s"] = clock() - started
            row.update(
                {
                    k: capture.diagnostics.get(k)
                    for k in ("output_tokens", "reasoning_tokens", "incomplete_reason")
                }
            )
            if capture.final_usage:
                row.update(
                    output_tokens=capture.final_usage.output_tokens,
                    reasoning_tokens=capture.final_usage.reasoning_tokens,
                )
            row["runaway"] = is_runaway(row["output_tokens"], parse_failed)
            if row["runaway"] or row["status"] != "completed":
                text = "".join(capture.text)
                path = RAW_DIR / f"stage-{stage}-run-{entry['seq']:02d}-{index:04d}.txt"
                (root / path).write_text(text)
                # FP-053: registered immediately, before reporting or stop handling.
                row["raw"] = {"path": str(path), **fingerprint(root / path)}
                row["shape"] = shape_statistics(text)
            entry["runs"].append(row)
            if (index + 1) % PROGRESS_EVERY == 0:
                print(f"M8 재현 {index + 1}/{len(plan)}건", flush=True)
            if stop:
                raise stop
        entry["status"] = "completed"
    except BaseException as exc:
        entry["status"] = "stopped"
        entry["error_type"] = type(exc).__name__
        raise
    finally:
        entry["result_sha256"] = canonical_hash(entry)
        _write(root, {"schema_version": 1, "history": old["history"] + [entry]})
        lock.unlink()
        if deps is not None:
            deps.client.close()
    return read_result(root)


def report(root):
    result = read_result(root)
    if result is None:
        return "재현 실행 미실행"
    lines = [
        (
            "경로별·질문별 폭주 건수·비율은 기술 통계로만 보고한다(가설 검정 없음). "
            "경로 A=비스트리밍+sdk 스키마(M6 동일), B=스트리밍+langchain 스키마(M7 동일), "
            "C=스트리밍+sdk 스키마(UI 경로). 해석은 B 대 C=스트리밍 아래 스키마 차이, "
            "A 대 C=같은 스키마에서 스트리밍 여부로만 한다. 원시 출력은 인용하지 않는다."
        )
    ]
    for entry in result["history"]:
        runs = entry["runs"]
        total = sum(r["runaway"] for r in runs)
        label = f"{entry['stage']}단계 {len(runs)}회"
        lines.append(
            f"{label}: 재현 안 됨({label})"
            if entry["status"] == "completed" and total == 0
            else f"{label}: {entry['status']}, 폭주 {total}건"
        )
        # First-run rows (HTTP 400 stop) predate the path field.
        for path in sorted({r.get("path", r["mode"]) for r in runs}):
            group = [r for r in runs if r.get("path", r["mode"]) == path]
            lines.append(
                f"경로 {path}: {sum(r['runaway'] for r in group)}/{len(group)} ({sum(r['runaway'] for r in group) / len(group):.2%})"
            )
            for q in sorted({r["question_id"] for r in group}):
                subset = [r for r in group if r["question_id"] == q]
                count = sum(r["runaway"] for r in subset)
                lines.append(
                    f"{q} 경로 {path}: {count}/{len(subset)} ({count / len(subset):.2%})"
                )
    return "\n\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stage", type=int, choices=(1, 2), default=1)
    parser.add_argument("--rerun-reason", default=os.environ.get("RERUN_REASON"))
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            print(
                json.dumps(dry_run(args.root, args.stage), ensure_ascii=False, indent=2)
            )
        else:
            run(args.root, args.stage, args.rerun_reason)
        return 0
    except Exception as exc:  # noqa: BLE001 - no credentials/transport text
        print(f"M8 재현 실행 거부: {type(exc).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
