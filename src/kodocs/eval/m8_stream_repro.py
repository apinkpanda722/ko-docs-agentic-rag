"""User-run serial M8 output-runaway reproduction; dry-run is offline."""

import argparse
import json
import math
import os
import random
import subprocess
import time
import unicodedata
from collections import Counter
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


def control_format_ratio(text):
    """Share of Unicode category Cf (format/zero-width) characters."""
    if not text:
        return 0
    return sum(unicodedata.category(c) == "Cf" for c in text) / len(text)


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
        # Added after the first reproduction; earlier records lack it.
        "control_format_ratio": control_format_ratio(text),
        "newline_ratio": text.count("\n") / len(text) if text else 0,
        "repetition_length": length,
        "repetition_count": count,
        "repetition_definition": "longest repeated substring, overlapping occurrences included",
        "citations_count": len(parsed["citations"])
        if isinstance(parsed.get("citations"), list)
        else 0,
    }


# Post-hoc shape classification: chosen AFTER seeing the first reproduction, so it
# is descriptive only and reported as such. Order matters (first match wins).
SHAPES = Path("data/manifest/m8_stream_repro_shapes.json")
FLOOD_WHITESPACE_RATIO = 0.5
FLOOD_FORMAT_RATIO = 0.5
LOOP_REPETITION_LENGTH = 500
CONNECTION_FAILURE_MAX_S = 0.05
SHAPE_DEFINITION = (
    "사후 분류(결과를 본 뒤 정한 규칙, 첫 일치 적용): 공백 홍수=whitespace_ratio≥0.5, "
    "제어 문자 반복=유니코드 범주 Cf 문자 비율≥0.5, 반복 루프=나머지 중 repetition_length≥500, 그 밖은 기타. "
    "공백 홍수의 시작 위치는 가장 긴 연속 공백 구간의 시작이며, 그 앞의 이스케이프되지 않은 따옴표 개수가 짝수면 문자열 밖(JSON 구조 위치). "
    "생성 중 키는 잘린 지점에서 값이 끝나지 않은 가장 안쪽 객체의 마지막 키(문자열 값 기준)."
)


def classify_shape(whitespace_ratio, format_ratio, repetition_length):
    if whitespace_ratio >= FLOOD_WHITESPACE_RATIO:
        return "공백 홍수"
    if format_ratio >= FLOOD_FORMAT_RATIO:
        return "제어 문자 반복"
    if repetition_length >= LOOP_REPETITION_LENGTH:
        return "반복 루프"
    return "기타(미분류)"


def _quotes_before(text, end):
    quotes, escaped = 0, False
    for char in text[:end]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quotes += 1
    return quotes


def flood_outside_string(text):
    """Did the longest whitespace run start outside a JSON string? None: no run."""
    best_start, best_length, start = 0, 0, None
    for index, char in enumerate([*text, "x"]):
        if char.isspace():
            start = index if start is None else start
        elif start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    if best_length == 0:
        return None
    return _quotes_before(text, best_start) % 2 == 0


def open_key(text):
    """Innermost JSON key whose value was still being written at truncation."""
    stack, buffer, in_string, escaped = [], [], False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                frame = stack[-1] if stack else None
                if frame and frame["type"] == "{":
                    if frame["expect"] == "key":
                        frame["key"], frame["expect"] = "".join(buffer), "colon"
                    elif frame["expect"] == "value":
                        frame["expect"] = "comma"
            else:
                buffer.append(char)
            continue
        if char == '"':
            in_string, buffer = True, []
        elif char in "{[":
            stack.append(
                {"type": char, "key": None, "expect": "key" if char == "{" else "value"}
            )
        elif char in "}]":
            if stack:
                stack.pop()
            if stack and stack[-1]["type"] == "{":
                stack[-1]["expect"] = "comma"
        elif stack and stack[-1]["type"] == "{":
            if char == ":" and stack[-1]["expect"] == "colon":
                stack[-1]["expect"] = "value"
            elif char == ",":
                stack[-1]["expect"] = "key"
    if (
        in_string
        and stack
        and stack[-1]["type"] == "{"
        and stack[-1]["expect"] == "key"
    ):
        return "(키 이름)"
    for frame in reversed(stack):
        if (
            frame["type"] == "{"
            and frame["key"] is not None
            and frame["expect"] in {"colon", "value"}
        ):
            return frame["key"]
    return None


def is_connection_failure(row):
    """Error status within 0.05 s: the request never produced a response."""
    return (
        row["status"] not in {"completed", "parse_error"}
        and isinstance(row.get("latency_s"), (int, float))
        and row["latency_s"] <= CONNECTION_FAILURE_MAX_S
    )


def build_shapes(root):
    """Derived, content-free shape classification of the local raw outputs."""
    root = Path(root)
    result = read_result(root)  # verifies the raw fingerprints first (FP-053)
    if result is None:
        raise ValueError("reproduction result missing")
    items = []
    for entry in result["history"]:
        for index, row in enumerate(entry["runs"]):
            if not row["runaway"]:
                continue
            text = (root / row["raw"]["path"]).read_text(encoding="utf-8")
            shape = row["shape"]
            cf = control_format_ratio(text)
            kind = classify_shape(
                shape["whitespace_ratio"], cf, shape["repetition_length"]
            )
            items.append(
                {
                    "seq": entry["seq"],
                    "index": index,
                    "question_id": row["question_id"],
                    "system": row["system"],
                    "path": row.get("path"),
                    "class": kind,
                    "whitespace_ratio": shape["whitespace_ratio"],
                    "control_format_ratio": cf,
                    "repetition_length": shape["repetition_length"],
                    "answer_chars": (shape.get("field_chars") or {}).get("answer"),
                    "active_field": shape.get("active_field"),
                    "open_key": open_key(text),
                    "flood_outside_string": flood_outside_string(text)
                    if kind == "공백 홍수"
                    else None,
                    "raw_sha256": row["raw"]["sha256"],
                }
            )
    return {
        "schema_version": 1,
        "source_result_sha256": result["result_sha256"],
        "definition": SHAPE_DEFINITION,
        "items": items,
    }


def write_shapes(root):
    root = Path(root)
    payload = build_shapes(root)
    path = root / SHAPES
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def read_shapes(root, result):
    path = Path(root) / SHAPES
    if not path.exists():
        return None
    shapes = json.loads(path.read_text(encoding="utf-8"))
    if shapes.get("source_result_sha256") != result["result_sha256"]:
        raise ValueError("M8 reproduction shapes are stale; run make m8-repro-shapes")
    return shapes


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
            # Verified when the (non-committed) raw file is present; a clone
            # without it still authenticates the committed hash chain.
            if (
                raw
                and (root / raw["path"]).exists()
                and fingerprint(root / raw["path"])
                != {k: raw[k] for k in ("sha256", "rows")}
            ):
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


def _rate(count, total):
    return f"{count / total:.1%}" if total else "—"


def _row(label, group):
    attempts = len(group)
    connection = sum(is_connection_failure(r) for r in group)
    runaway = sum(r["runaway"] for r in group)
    return (
        f"| {label} | {attempts} | {connection} | {runaway} | "
        f"{_rate(runaway, attempts)} | {_rate(runaway, attempts - connection)} |"
    )


def _shape_lines(shapes):
    """Blocks (joined by blank lines later); a table or bullet list is one block."""
    items = shapes["items"]
    if not items:
        return ["폭주 건이 없어 형태 분류 대상이 없다."]
    classes = ["공백 홍수", "제어 문자 반복", "반복 루프", "기타(미분류)"]
    paths = sorted({i["path"] for i in items})
    table = [
        "| 형태 | " + " | ".join(f"경로 {p}" for p in paths) + " | 합계 |",
        "|---|" + "---|" * (len(paths) + 1),
    ]
    for kind in classes:
        counts = [
            sum(i["class"] == kind and i["path"] == p for i in items) for p in paths
        ]
        table.append(
            f"| {kind} | " + " | ".join(map(str, counts)) + f" | {sum(counts)} |"
        )
    answers = [i["answer_chars"] for i in items if isinstance(i["answer_chars"], int)]
    active = Counter(i["active_field"] for i in items)
    # A flood that started outside a JSON string has no key being written.
    keys = Counter(
        "(문자열 밖 구조 위치)" if i["flood_outside_string"] else i["open_key"]
        for i in items
    )
    floods = [i for i in items if i["class"] == "공백 홍수"]
    outside = sum(bool(i["flood_outside_string"]) for i in floods)
    bullets = [
        f"- 폭주 {len(items)}건의 answer 필드 길이: {min(answers)}~{max(answers)}자({len(answers)}건 기록)."
        if answers
        else "- answer 필드 길이 기록 없음.",
        "- 폭주 시 생성 중이던 필드: "
        + ", ".join(f"{k} {v}건" for k, v in sorted(active.items(), key=str))
        + ".",
        "- 폭주 시 생성 중이던 키(값이 끝나지 않은 가장 안쪽 키): "
        + ", ".join(f"{k} {v}건" for k, v in sorted(keys.items(), key=str))
        + ".",
        f"- 공백 홍수 {len(floods)}건 중 문자열 밖(JSON 구조 위치)에서 시작한 건수: {outside}건.",
    ]
    return [
        "### 폭주 건의 형태(사후 분류)",
        f"{SHAPE_DEFINITION} 이 분류는 결과를 본 뒤 정한 사후 분류이며 경로 간 형태 차이를 해석하지 않는다. 원시 출력은 인용하지 않는다.",
        "\n".join(table),
        "\n".join(bullets),
    ]


def report(root, shapes=None):
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
        label = f"{entry['stage']}단계 seq {entry['seq']}"
        if entry["status"] != "completed":
            lines.append(
                f"{label}: {entry['status']}({entry.get('error_type') or runs[-1]['status'] if runs else '기록 없음'}), "
                f"기록 {len(runs)}회. 결과 집계에서 제외한다."
            )
            continue
        total = sum(r["runaway"] for r in runs)
        lines.append(
            f"{label}: 재현 안 됨({entry['stage']}단계 {len(runs)}회)"
            if total == 0
            else f"{label}: 완료 {len(runs)}회, 폭주 {total}건."
        )
        connection = [(i, r) for i, r in enumerate(runs) if is_connection_failure(r)]
        if connection:
            lines.append(
                "시작 시점 연결 실패(오류 상태이고 지연 "
                f"{CONNECTION_FAILURE_MAX_S}초 이내): {len(connection)}건, 실행 순서 인덱스 "
                + ", ".join(str(i) for i, _ in connection)
                + ", 오류 유형 "
                + ", ".join(sorted({r["status"] for _, r in connection}))
                + ". 서버 응답 전의 연결 실패이므로 폭주가 아니다. 폭주율은 분모를 "
                "시도 기준(전체 시도 수)과 오류 제외 기준(시작 시점 연결 실패를 뺀 수) 둘 다로 쓴다."
            )
        paths = sorted({r["path"] for r in runs})
        questions = sorted({r["question_id"] for r in runs})
        table = [
            "",
            "| 경로 · 질문 | 시도 | 시작 시점 연결 실패 | 폭주 | 폭주율(시도 기준) | 폭주율(오류 제외 기준) |",
            "|---|---|---|---|---|---|",
        ]
        for path in paths:
            for q in questions:
                group = [r for r in runs if r["path"] == path and r["question_id"] == q]
                if group:
                    table.append(_row(f"경로 {path} · {q}", group))
            table.append(
                _row(f"**경로 {path} 합계**", [r for r in runs if r["path"] == path])
            )
        for q in questions:
            table.append(
                _row(
                    f"**질문 {q} 합계(전 경로)**",
                    [r for r in runs if r["question_id"] == q],
                )
            )
        lines.append("\n".join(table))
        counts = {p: sum(r["runaway"] for r in runs if r["path"] == p) for p in paths}
        if all(counts.values()):
            lines.append(
                "폭주는 세 경로 모두에서 관측되었다(스트리밍·스키마와 무관하게 발생). "
                "건수가 작아 경로 간 차이는 판단하지 않는다."
            )
        else:
            lines.append(
                "경로별 폭주 건수: "
                + ", ".join(f"{p} {c}" for p, c in counts.items())
                + ". 건수가 작아 경로 간 차이는 판단하지 않는다."
            )
        shapes = shapes if shapes is not None else read_shapes(root, result)
        lines += (
            _shape_lines(shapes)
            if shapes is not None
            else ["형태 분류 미생성(`make m8-repro-shapes` 필요)."]
        )
    return "\n\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--shapes",
        action="store_true",
        help="derive the content-free shape classification from local raw files",
    )
    parser.add_argument("--stage", type=int, choices=(1, 2), default=1)
    parser.add_argument("--rerun-reason", default=os.environ.get("RERUN_REASON"))
    args = parser.parse_args(argv)
    try:
        if args.shapes:
            payload = write_shapes(args.root)
            print(f"형태 분류 {len(payload['items'])}건 → {SHAPES}")
        elif args.dry_run:
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
