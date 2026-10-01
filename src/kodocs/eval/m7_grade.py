"""Resumable, pointwise M7 pilot and frozen Gemini grading (user-run only)."""

import argparse
import fcntl
import json
import math
import os
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from kodocs.config import load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
    PriceExpiredError,
    estimate_usd,
)
from kodocs.eval.m7_config import (
    CONFIG_PATH,
    SEED,
    check_price,
    read_json,
    verify_config,
)
from kodocs.eval.m7_grading import (
    RUBRIC_PATH,
    AccGrade,
    EvidenceLoader,
    FaithGrade,
    build_grade_item,
    classify_all,
    load_m6_answers,
    rubric_sha256,
    select_pilot,
    shuffle_order,
)
from kodocs.eval.m7_rawhash import fingerprint, record_fingerprint, verify_fingerprint
from kodocs.llm.gemini_chat import (
    BillingUncertainError,
    CreditExhausted,
    FatalLLMError,
    GeminiGrader,
    GradeParseError,
    GradeRequest,
    GradeStopError,
)
from kodocs.llm.grade_settings import load_grade_config

GRADES_PATH = Path("data/interim/m7/grades.jsonl")
PILOT_GRADES_PATH = Path("data/interim/m7/pilot/grades.jsonl")
PILOT_PATH = Path("data/manifest/m7_pilot.json")
SCORES_PATH = Path("data/manifest/m7_grade_scores.json")
SUMMARY_PATH = Path("data/manifest/m7_grade_summary.json")
DRY_PATH = Path("data/manifest/m7_dry_runs.jsonl")
CACHE_FIELDS = (
    "answer_sha256",
    "rubric_sha256",
    "model_settings_hash",
    "mode",
    "repeat",
)
TERMINAL = {"succeeded", "parse_error", "billing_uncertain"}


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def append(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def cache_key(row):
    return tuple(row[k] for k in CACHE_FIELDS)


def load_grades(path):
    """FP-032: last row wins for each output-determining FP-033 key."""
    latest = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                latest[cache_key(row)] = row
    return latest


def tier(run, h5_ids, sample_ids):
    if run.run_set == "test_gold" and run.question_id in h5_ids:
        return 1
    if run.answer_id in sample_ids:
        return 2
    return 4 if run.run_set == "dev" else 3


def prioritize(answers, h5_ids, sample_ids):
    by_id = {r.answer_id: r for r in answers}
    if len(by_id) != len(answers):
        raise ValueError("duplicate answer_id")
    return [
        by_id[i]
        for t in range(1, 5)
        for i in shuffle_order(
            [r.answer_id for r in answers if tier(r, h5_ids, sample_ids) == t], SEED
        )
    ]


def paid_spent(ledger):
    return math.fsum(
        e.amount_usd
        for e, _ in ledger._entries()
        if e.tier == "paid" and e.service.startswith("google.")
    )


class CreditBudgetGuard(BudgetGuard):
    """Check shared credit before every wrapper send, including retries."""

    def __init__(self, ledger, monthly_budget_usd, credit_usd):
        super().__init__(ledger, monthly_budget_usd)
        self.credit_usd = credit_usd

    def check(self, estimate_usd):
        super().check(estimate_usd)
        if (
            self.credit_usd is not None
            and paid_spent(self.ledger) + estimate_usd > self.credit_usd
        ):
            raise CreditExhausted("Paid Gemini credit would be exceeded")


def _requests(answers, classes, loader):
    rubric = RUBRIC_PATH.read_text(encoding="utf-8")
    result = {}
    for run in answers:
        if classes[run.answer_id] not in {"gemini_acc", "gemini_faith_only"}:
            continue
        item = build_grade_item(run, run.question, loader)
        schema = AccGrade if item.mode == "acc" else FaithGrade
        result[run.answer_id] = (
            item.mode,
            GradeRequest(
                run.answer_id,
                rubric + f"\n이번 요청 모드: {item.mode}",
                item.request_text,
                schema,
            ),
            {
                "evidence_chars": item.evidence_chars,
                "evidence_sources": list(item.evidence_sources),
                "evidence_empty_reason": item.evidence_empty_reason,
            },
        )
    return result


def _active_rows(latest, hashes, tasks):
    keys = {cache_key(base) for _, _, base in tasks}
    return [
        r
        for k, r in latest.items()
        if k in keys
        and all(
            r[name] == hashes[name] for name in ("rubric_sha256", "model_settings_hash")
        )
    ]


def _report(rows, classes):
    distribution, concentration, agreement = {}, {}, {}
    for field in ("correctness", "faithfulness"):
        values = [r[field] for r in rows if r.get(field) is not None]
        counts = Counter(values)
        distribution[field] = {str(i): counts[i] for i in range(4)}
        peak = max(counts.values(), default=0) / len(values) if values else 0
        concentration[field] = {
            "max_share": peak,
            "concentrated": bool(values) and peak >= 0.9,
        }
        groups = {}
        for r in rows:
            groups.setdefault(r["answer_id"], {})[r["repeat"]] = r.get(field)
        triples = [
            list(g.values())
            for g in groups.values()
            if len(g) == 3 and all(v is not None for v in g.values())
        ]
        agreement[field] = (
            sum(len(set(g)) == 1 for g in triples) / len(triples) if triples else None
        )
    errors = sum(r["status"] == "parse_error" for r in rows)
    return {
        "calls": len(rows),
        "class_counts": dict(Counter(classes.values())),
        "format_errors": errors,
        "format_error_rate": errors / len(rows) if rows else 0,
        "score_distribution": distribution,
        "concentration": concentration,
        "agreement": agreement,
        "cost_usd": math.fsum(r.get("cost_usd", 0) for r in rows),
    }


def _format_stop(rows):
    return (
        len(rows) >= 50
        and sum(r["status"] == "parse_error" for r in rows) / len(rows) > 0.02
    )


def _dry(root, grader, tasks, classes, h5, sample, settings, ledger, credit_usd, pilot):
    estimates = [grader.estimate(request) for _, request, _ in tasks]
    input_tokens = sum(e.estimated_input_tokens for e in estimates)
    # Expected output is an explicit planning assumption; upper bound includes thinking.
    expected_output = len(tasks) * min(1000, grader.config.max_output_tokens)
    maximum_output = sum(e.maximum_output_tokens for e in estimates)
    prefix = f"google.{grader.config.model}"
    expected_cost = estimate_usd(prefix + ".input", input_tokens) + estimate_usd(
        prefix + ".output", expected_output
    )
    upper_cost = sum(e.maximum_cost_usd for e in estimates)
    spent = paid_spent(ledger)
    report = {
        "at": now(),
        "mode": "pilot" if pilot else "grade",
        "calls": len(tasks),
        "class_counts": dict(Counter(classes.values())),
        "calls_by_class": dict(Counter(classes[r.answer_id] for r, _, _ in tasks)),
        "calls_by_tier": dict(Counter(str(tier(r, h5, sample)) for r, _, _ in tasks)),
        "calls_by_mode": dict(Counter(base["mode"] for _, _, base in tasks)),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": expected_output,
        "maximum_output_tokens": maximum_output,
        "expected_output_tokens_per_call": min(1000, grader.config.max_output_tokens),
        "estimated_cost_usd": expected_cost,
        "maximum_cost_usd": upper_cost,
        "price_includes_vat": True,
        "price_valid": True,
        "monthly_budget_usd": settings.monthly_budget_usd,
        "month_spent_usd": ledger.month_total_usd(),
        "within_monthly_budget": ledger.month_total_usd() + upper_cost
        <= settings.monthly_budget_usd,
        "credit_usd": credit_usd,
        "gemini_paid_spent_usd": spent,
        "expected_with_spent_usd": spent + expected_cost,
        "maximum_with_spent_usd": spent + upper_cost,
        "within_credit": None
        if credit_usd is None
        else spent + upper_cost <= credit_usd,
        "minimum_seconds": max(0, len(tasks) - 1) * 60 / grader.config.rpm_pacing,
    }
    append(root / DRY_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def _execute(
    *,
    root,
    pilot,
    answers,
    grader,
    settings,
    evidence_loader,
    dry_run,
    credit_usd,
    progress_every,
):
    root = Path(root)
    if credit_usd is not None and (not math.isfinite(credit_usd) or credit_usd < 0):
        raise ValueError("credit_usd must be non-negative and finite")
    if progress_every <= 0:
        raise ValueError("progress_every must be positive")
    if pilot and (root / CONFIG_PATH).exists():
        raise ValueError("pilot refused after M7 freeze")
    if not pilot and (not dry_run or (root / CONFIG_PATH).exists()):
        verify_config(root=root)
    model = load_grade_config() if grader is None else grader.config
    check_price(model)
    settings = settings or load_settings(env=os.environ if dry_run else None)
    ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
    guard = CreditBudgetGuard(ledger, settings.monthly_budget_usd, credit_usd)
    grader = grader or GeminiGrader(
        model,
        settings,
        ledger_path=root / DEFAULT_LEDGER_PATH,
        rate_log_path=root / "data/interim/m7/gemini_429.jsonl",
    )
    if isinstance(grader, GeminiGrader):
        grader._guard = guard  # Wrapper rechecks this guard before every paced send.
    answers = load_m6_answers(root) if answers is None else answers
    classes = classify_all(answers)
    hashes = {
        "rubric_sha256": rubric_sha256(),
        "model_settings_hash": model.settings_hash(),
    }
    path = root / (PILOT_GRADES_PATH if pilot else GRADES_PATH)
    manifest = root / (PILOT_PATH if pilot else SUMMARY_PATH)
    previous = read_json(manifest) if manifest.exists() else {}
    h5, sample = [], []
    if pilot:
        ids = select_pilot(answers, classes, seed=SEED)
        if previous and (previous.get("ids") != ids or previous.get("seed") != SEED):
            raise ValueError("pilot selection mismatch; existing IDs must be reused")
        previous.update({"ids": ids, "seed": SEED, **hashes})
        selected = set(ids)
        answers = [r for r in answers if r.answer_id in selected]
        classes = {i: classes[i] for i in ids}
        answers = [next(r for r in answers if r.answer_id == i) for i in ids]
    else:
        h5 = read_json(root / "data/manifest/m6_eval_inputs.json")["h5_pool"]["ids"]
        sample = read_json(root / "data/manifest/m7_human_sample.json")["answer_ids"]
        answers = prioritize(answers, h5, sample)
    if (
        path.exists()
        and (path.stat().st_size or previous.get("raw"))
        or previous.get("raw")
    ):
        verify_fingerprint(manifest, "pilot_grades" if pilot else "grades", path)
    loader = EvidenceLoader(root) if evidence_loader is None else evidence_loader
    requests = _requests(answers, classes, loader)
    tasks = [
        (
            run,
            requests[run.answer_id][1],
            {
                "answer_id": run.answer_id,
                "answer_sha256": run.answer_sha256,
                **hashes,
                "mode": requests[run.answer_id][0],
                "repeat": repeat,
            },
        )
        for run in answers
        if run.answer_id in requests
        for repeat in range(1, 4)
    ]
    if dry_run:
        latest = load_grades(path)
        pending = [
            task
            for task in tasks
            if latest.get(cache_key(task[2]), {}).get("status") not in TERMINAL
        ]
        return _dry(
            root,
            grader,
            pending,
            classes,
            h5,
            sample,
            settings,
            ledger,
            credit_usd,
            pilot,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock the append-only output itself so simultaneous resumes cannot double-send.
    with path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("M7 grading already running") from exc
        if path.stat().st_size or previous.get("raw"):
            verify_fingerprint(manifest, "pilot_grades" if pilot else "grades", path)
        previous = read_json(manifest) if manifest.exists() else previous
        if pilot:
            previous.update({"ids": ids, "seed": SEED, **hashes})
            write_json(manifest, previous)  # Selection persists BEFORE any send.
        latest = load_grades(path)
        stops = list(previous.get("stops", []))
        status = "completed"

        def active():
            return _active_rows(latest, hashes, tasks)

        def remaining():
            return sum(
                latest.get(cache_key(b), {}).get("status") not in TERMINAL
                for _, _, b in tasks
            )

        def stop(error, exc=None):
            nonlocal status
            status = "stopped"
            entry = {"at": now(), "error_type": error, "remaining": remaining()}
            reset = getattr(exc, "next_reset_kst", None)
            if reset:
                entry["next_reset_kst"] = reset.isoformat()
            stops.append(entry)
            print(
                f"M7 stopped: {error}; remaining={entry['remaining']}; next_reset_kst={entry.get('next_reset_kst')}",
                flush=True,
            )

        try:
            if _format_stop(active()):
                stop("FormatErrorRateExceeded")
            else:
                sent = 0
                for run, request, base in tasks:
                    key = cache_key(base)
                    if latest.get(key, {}).get("status") in TERMINAL:
                        continue
                    check_price(model)
                    estimate = grader.estimate(request)
                    guard.check(estimate.maximum_cost_usd)
                    grader.check_budget(estimate)
                    grader.remaining = remaining()
                    before = paid_spent(ledger)
                    row = {
                        **base,
                        "at": now(),
                        "correctness": None,
                        "faithfulness": None,
                        "rationale": None,
                        "usage": None,
                        "cost_usd": 0,
                        "attempts": [],
                    }
                    stopping = None
                    try:
                        result = grader.grade(request)
                        parsed = request.schema.model_validate(
                            result.parsed.model_dump(), strict=True
                        )
                        if "\n" in parsed.rationale or "\r" in parsed.rationale:
                            raise GradeParseError("rationale must be one line")
                    except (GradeParseError, ValidationError) as exc:
                        row["status"] = "parse_error"
                        row["attempts"] = [
                            asdict(a) for a in getattr(exc, "attempts", ())
                        ]
                        if getattr(exc, "usage", None) is not None:
                            row["usage"] = asdict(exc.usage)
                    except BillingUncertainError as exc:
                        row["status"] = "billing_uncertain"
                        row["attempts"] = [asdict(a) for a in exc.attempts]
                    except (GradeStopError, FatalLLMError) as exc:
                        row["status"] = "stopped"
                        row["error_type"] = type(exc).__name__
                        row["attempts"] = [asdict(a) for a in exc.attempts]
                        stopping = exc
                    else:
                        row.update(parsed.model_dump())
                        row.update(
                            {
                                "status": "succeeded",
                                "usage": asdict(result.usage),
                                "cost_usd": result.cost_usd,
                                "attempts": [asdict(a) for a in result.attempts],
                            }
                        )
                    row["cost_usd"] = max(row["cost_usd"], paid_spent(ledger) - before)
                    append(path, row)
                    latest[key] = row
                    sent += 1
                    if sent % progress_every == 0:
                        print(
                            f"M7 progress={len(active())}; remaining={remaining()}; cost_usd={_report(active(), classes)['cost_usd']:.6f}",
                            flush=True,
                        )
                    if stopping is not None:
                        stop(type(stopping).__name__, stopping)
                        break
                    if _format_stop(active()):
                        stop("FormatErrorRateExceeded")
                        break
        except (
            GradeStopError,
            BudgetExceededError,
            PriceExpiredError,
            FatalLLMError,
        ) as exc:
            stop(type(exc).__name__, exc)
        except BaseException as exc:
            stop(type(exc).__name__, exc)
            raise
        finally:
            summary = {
                **previous,
                **_report(active(), classes),
                "status": status,
                "remaining": remaining(),
                "stops": stops,
                "updated_at": now(),
                **hashes,
            }
            write_json(manifest, summary)
            record_fingerprint(
                manifest, "pilot_grades" if pilot else "grades", path, status
            )
        summary = read_json(manifest)
        if status == "completed" and not pilot:
            scores = {}
            for run in answers:
                kind = classes[run.answer_id]
                mode = (
                    "acc"
                    if kind in {"det_zero", "gemini_acc"}
                    else "faith"
                    if kind == "gemini_faith_only"
                    else None
                )
                rs = [
                    latest.get(
                        cache_key(
                            {
                                "answer_sha256": run.answer_sha256,
                                **hashes,
                                "mode": mode,
                                "repeat": n,
                            }
                        ),
                        {},
                    )
                    for n in range(1, 4)
                ]
                scores[run.answer_id] = {
                    "mode": mode,
                    "class": kind,
                    **requests.get(run.answer_id, (None, None, {}))[2],
                    "correctness": [0, 0, 0]
                    if kind == "det_zero"
                    else [r.get("correctness") for r in rs]
                    if kind == "gemini_acc"
                    else [],
                    "faithfulness": [r.get("faithfulness") for r in rs]
                    if kind in {"gemini_acc", "gemini_faith_only"}
                    else [],
                }
            write_json(root / SCORES_PATH, {"scores": scores, "raw": fingerprint(path)})
        return summary


def run_grade(
    *,
    root=Path("."),
    answers=None,
    grader=None,
    settings=None,
    evidence_loader=None,
    dry_run=False,
    credit_usd=None,
    progress_every=10,
):
    return _execute(
        root=root,
        pilot=False,
        answers=answers,
        grader=grader,
        settings=settings,
        evidence_loader=evidence_loader,
        dry_run=dry_run,
        credit_usd=credit_usd,
        progress_every=progress_every,
    )


def run_pilot(
    *,
    root=Path("."),
    answers=None,
    grader=None,
    settings=None,
    evidence_loader=None,
    dry_run=False,
    credit_usd=None,
    progress_every=10,
):
    return _execute(
        root=root,
        pilot=True,
        answers=answers,
        grader=grader,
        settings=settings,
        evidence_loader=evidence_loader,
        dry_run=dry_run,
        credit_usd=credit_usd,
        progress_every=progress_every,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pilot", "grade"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--credit-usd", type=float)
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        runner = run_pilot if args.command == "pilot" else run_grade
        summary = runner(
            root=args.root,
            dry_run=args.dry_run,
            credit_usd=args.credit_usd,
            progress_every=args.progress_every,
        )
    except (OSError, TypeError, ValueError, RuntimeError, KeyError) as exc:
        print(f"M7 refused: {type(exc).__name__}", flush=True)
        return 2
    return 0 if args.dry_run or summary["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
