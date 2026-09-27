"""Append-only API cost ledger and monthly budget tests."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kodocs.costs import (
    PRICES,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
    Price,
    budget_estimate_usd,
    estimate_usd,
)

SERVICE = "upstage.document-parse.standard"


def _entry(*, call_id: str, ts: str, amount_usd: float) -> dict[str, object]:
    return {
        "call_id": call_id,
        "ts": ts,
        "service": SERVICE,
        "model": "document-parse-250618",
        "unit": "page",
        "quantity": amount_usd / 0.011,
        "unit_price_usd": 0.01,
        "tax_rate": 0.10,
        "amount_usd": amount_usd,
        "ref": "a" * 64,
        "run_id": "run-001",
    }


def _write_entries(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(entry)}\n" for entry in entries),
        encoding="utf-8",
    )


def test_upstage_standard_price_and_tax_inclusive_estimate() -> None:
    assert PRICES[SERVICE] == Price(
        service=SERVICE,
        unit="page",
        unit_price_usd=0.01,
        source_url=(
            "https://console.upstage.ai/docs/models/compare/document-intelligence"
        ),
        checked_on="2026-09-26",
        billing_margin=1.05,
    )
    assert round(estimate_usd(SERVICE, 1887), 2) == 20.76


def test_budget_estimate_applies_billing_margin_only_to_the_estimate() -> None:
    # 원장 금액(estimate_usd)은 그대로, 예산 검사용만 여유 5%를 곱한다.
    assert budget_estimate_usd(SERVICE, 1887) == pytest.approx(
        estimate_usd(SERVICE, 1887) * 1.05
    )


def test_price_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        PRICES[SERVICE].unit_price_usd = 0.0  # type: ignore[misc]


def test_estimate_rejects_unknown_service() -> None:
    with pytest.raises(KeyError, match="unknown.service"):
        estimate_usd("unknown.service", 1)


def test_record_appends_one_safe_billing_event_per_call(tmp_path: Path) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    fixed_now = datetime(2026, 9, 26, 3, 4, 5, tzinfo=UTC)
    ledger = CostLedger(ledger_path, clock=lambda: fixed_now)

    first = ledger.record(
        service=SERVICE,
        model="document-parse-250618",
        quantity=12,
        ref="a" * 64,
        run_id="run-001",
    )
    second = ledger.record(
        service=SERVICE,
        model="document-parse-250618",
        quantity=3,
        ref="b" * 64,
        run_id="run-001",
    )

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(lines) == 2
    assert first.call_id != second.call_id
    assert first.ts == "2026-09-26T03:04:05+00:00"
    assert first.amount_usd == pytest.approx(12 * 0.01 * 1.10)
    assert records[0] == {
        "amount_usd": first.amount_usd,
        "call_id": first.call_id,
        "model": "document-parse-250618",
        "quantity": 12,
        "ref": "a" * 64,
        "run_id": "run-001",
        "service": SERVICE,
        "tax_rate": 0.10,
        "ts": "2026-09-26T03:04:05+00:00",
        "unit": "page",
        "unit_price_usd": 0.01,
    }
    assert not {
        "api_key",
        "authorization",
        "request_url",
        "request_text",
    }.intersection(records[0])


def test_record_normalizes_injected_clock_to_utc(tmp_path: Path) -> None:
    kst = timezone(offset=timedelta(hours=9))
    ledger = CostLedger(
        tmp_path / "costs.jsonl",
        clock=lambda: datetime(2026, 10, 1, 8, 59, 59, tzinfo=kst),
    )

    entry = ledger.record(
        service=SERVICE,
        model="document-parse-250618",
        quantity=1,
        ref="a" * 64,
        run_id="run-001",
    )

    assert entry.ts == "2026-09-30T23:59:59+00:00"


def test_month_total_is_zero_when_ledger_does_not_exist(tmp_path: Path) -> None:
    ledger = CostLedger(
        tmp_path / "missing.jsonl",
        clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert ledger.month_total_usd() == 0.0


def test_month_total_uses_utc_half_open_month_boundaries(tmp_path: Path) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    _write_entries(
        ledger_path,
        [
            _entry(call_id="august", ts="2026-08-31T23:59:59Z", amount_usd=1),
            _entry(call_id="start", ts="2026-09-01T00:00:00Z", amount_usd=2),
            _entry(call_id="end", ts="2026-09-30T23:59:59Z", amount_usd=3),
            _entry(
                call_id="kst-in",
                ts="2026-10-01T08:59:59+09:00",
                amount_usd=5,
            ),
            _entry(
                call_id="kst-out",
                ts="2026-10-01T09:00:00+09:00",
                amount_usd=7,
            ),
            _entry(call_id="october", ts="2026-10-01T00:00:00Z", amount_usd=11),
        ],
    )
    ledger = CostLedger(
        ledger_path,
        clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert ledger.month_total_usd() == 10


def test_month_total_handles_december_to_january_boundary(tmp_path: Path) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    _write_entries(
        ledger_path,
        [
            _entry(call_id="december", ts="2026-12-31T23:59:59Z", amount_usd=2),
            _entry(call_id="january", ts="2027-01-01T00:00:00Z", amount_usd=3),
        ],
    )

    assert (
        CostLedger(ledger_path).month_total_usd(datetime(2026, 12, 1, tzinfo=UTC)) == 2
    )


def test_month_total_sums_every_event_even_when_identifiers_repeat(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    _write_entries(
        ledger_path,
        [
            _entry(call_id="same-call", ts="2026-09-01T00:00:00Z", amount_usd=2),
            _entry(call_id="same-call", ts="2026-09-02T00:00:00Z", amount_usd=3),
        ],
    )

    assert (
        CostLedger(ledger_path).month_total_usd(datetime(2026, 9, 15, tzinfo=UTC)) == 5
    )


@pytest.mark.parametrize("broken_line", ["not-json", "   "])
def test_month_total_reports_broken_json_line_number(
    tmp_path: Path,
    broken_line: str,
) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    valid = json.dumps(_entry(call_id="valid", ts="2026-09-01T00:00:00Z", amount_usd=2))
    ledger_path.write_text(f"{valid}\n{broken_line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"costs\.jsonl line 2"):
        CostLedger(ledger_path).month_total_usd(datetime(2026, 9, 15, tzinfo=UTC))


def test_budget_guard_allows_exact_budget_and_reports_remaining(tmp_path: Path) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    _write_entries(
        ledger_path,
        [_entry(call_id="spent", ts="2026-09-01T00:00:00Z", amount_usd=40)],
    )
    ledger = CostLedger(
        ledger_path,
        clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
    )
    guard = BudgetGuard(ledger, monthly_budget_usd=45)

    guard.check(5)
    assert guard.remaining_usd() == 5


def test_budget_guard_rejects_estimate_that_would_exceed_budget(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "costs.jsonl"
    _write_entries(
        ledger_path,
        [_entry(call_id="spent", ts="2026-09-01T00:00:00Z", amount_usd=40)],
    )
    ledger = CostLedger(
        ledger_path,
        clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
    )
    guard = BudgetGuard(ledger, monthly_budget_usd=45)

    with pytest.raises(BudgetExceededError, match=r"40.*6.*45"):
        guard.check(6)


def test_zero_budget_rejects_every_positive_estimate(tmp_path: Path) -> None:
    ledger = CostLedger(
        tmp_path / "missing.jsonl",
        clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
    )
    guard = BudgetGuard(ledger, monthly_budget_usd=0)

    guard.check(0)
    with pytest.raises(BudgetExceededError):
        guard.check(0.01)
