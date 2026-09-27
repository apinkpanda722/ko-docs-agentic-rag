"""Shared append-only cost ledger and monthly budget enforcement."""

import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final

from kodocs.ingest.manifest import append_record

TAX_RATE: float = 0.10
DEFAULT_LEDGER_PATH: Final[Path] = Path("data/manifest/costs.jsonl")


@dataclass(frozen=True)
class Price:
    """Published pre-tax unit price for a billable service."""

    service: str
    unit: str
    unit_price_usd: float
    source_url: str
    checked_on: str
    # Budget-check multiplier for provider billing above recorded usage. It is
    # applied only to pre-call estimates (BudgetGuard, dry-run), never to the ledger.
    billing_margin: float = 1.0


_UPSTAGE_DOCUMENT_PARSE_STANDARD = Price(
    service="upstage.document-parse.standard",
    unit="page",
    unit_price_usd=0.01,
    source_url="https://console.upstage.ai/docs/models/compare/document-intelligence",
    checked_on="2026-09-26",
    # M2: 1st run ledger 1887 pages (response usage.pages) vs console 1947 (+60,
    # +3.2%); 2nd run 276 = 276 exactly with every request a single HTTP 200, so
    # the gap looks one-off (unrecorded retries, unconfirmed). Keep 5% margin.
    billing_margin=1.05,
)

PRICES: Mapping[str, Price] = MappingProxyType(
    {
        _UPSTAGE_DOCUMENT_PARSE_STANDARD.service: (_UPSTAGE_DOCUMENT_PARSE_STANDARD),
    }
)


def estimate_usd(
    service: str,
    quantity: float,
    *,
    tax_rate: float = TAX_RATE,
) -> float:
    """Estimate a service charge at list price, including tax."""
    try:
        price = PRICES[service]
    except KeyError:
        raise KeyError(f"Unknown priced service: {service}") from None

    _require_non_negative_finite(quantity, name="quantity")
    _require_non_negative_finite(tax_rate, name="tax_rate")
    return quantity * price.unit_price_usd * (1 + tax_rate)


def budget_estimate_usd(service: str, quantity: float) -> float:
    """Tax-inclusive estimate times the service billing margin, for budget checks."""
    return estimate_usd(service, quantity) * PRICES[service].billing_margin


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable billing event for one external API call."""

    call_id: str
    ts: str
    service: str
    model: str
    unit: str
    quantity: float
    unit_price_usd: float
    tax_rate: float
    amount_usd: float
    ref: str
    run_id: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CostLedger:
    """Append billing events and total them by UTC calendar month."""

    def __init__(
        self,
        path: Path = DEFAULT_LEDGER_PATH,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self._clock = clock

    def record(
        self,
        *,
        service: str,
        model: str,
        quantity: float,
        ref: str,
        run_id: str,
    ) -> LedgerEntry:
        """Append one tax-inclusive list-price billing event."""
        try:
            price = PRICES[service]
        except KeyError:
            raise KeyError(f"Unknown priced service: {service}") from None

        amount_usd = estimate_usd(service, quantity)
        entry = LedgerEntry(
            call_id=uuid.uuid4().hex,
            ts=_as_utc(self._clock(), name="clock result").isoformat(),
            service=price.service,
            model=model,
            unit=price.unit,
            quantity=quantity,
            unit_price_usd=price.unit_price_usd,
            tax_rate=TAX_RATE,
            amount_usd=amount_usd,
            ref=ref,
            run_id=run_id,
        )
        append_record(
            self.path,
            asdict(entry),
            required=LedgerEntry.__dataclass_fields__,
        )
        return entry

    def month_total_usd(self, when: datetime | None = None) -> float:
        """Return all charges in the UTC calendar month containing ``when``."""
        target = _as_utc(when if when is not None else self._clock(), name="when")
        month_start = target.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if month_start.month == 12:
            next_month_start = month_start.replace(
                year=month_start.year + 1,
                month=1,
            )
        else:
            next_month_start = month_start.replace(month=month_start.month + 1)

        amounts: list[float] = []
        for entry, line_number in self._entries():
            try:
                timestamp = _parse_timestamp(entry.ts)
                amount_usd = _require_non_negative_finite(
                    entry.amount_usd,
                    name="amount_usd",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid cost ledger entry in {self.path} "
                    f"line {line_number}: {exc}"
                ) from exc

            if month_start <= timestamp < next_month_start:
                amounts.append(amount_usd)
        return math.fsum(amounts)

    def _entries(self) -> list[tuple[LedgerEntry, int]]:
        if not self.path.exists():
            return []

        entries: list[tuple[LedgerEntry, int]] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                try:
                    raw_entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid cost ledger JSON in {self.path} "
                        f"line {line_number}: {exc.msg}"
                    ) from exc
                try:
                    if not isinstance(raw_entry, dict):
                        raise TypeError("entry must be a JSON object")
                    entry = LedgerEntry(**raw_entry)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid cost ledger entry in {self.path} "
                        f"line {line_number}: {exc}"
                    ) from exc
                entries.append((entry, line_number))
        return entries


class BudgetExceededError(RuntimeError):
    """Raised before an estimated call would exceed the monthly limit."""


class BudgetGuard:
    """Enforce a shared monthly cost limit before an external API call."""

    def __init__(self, ledger: CostLedger, monthly_budget_usd: float) -> None:
        self.ledger = ledger
        self.monthly_budget_usd = _require_non_negative_finite(
            monthly_budget_usd,
            name="monthly_budget_usd",
        )

    def check(self, estimate_usd: float) -> None:
        """Fail if current-month charges plus ``estimate_usd`` exceed the limit."""
        estimate = _require_non_negative_finite(
            estimate_usd,
            name="estimate_usd",
        )
        current = self.ledger.month_total_usd()
        if current + estimate > self.monthly_budget_usd:
            raise BudgetExceededError(
                "Monthly budget exceeded: "
                f"current {current:g} USD + estimate {estimate:g} USD "
                f"> budget {self.monthly_budget_usd:g} USD"
            )

    def remaining_usd(self) -> float:
        """Return the current UTC month's unspent budget."""
        return self.monthly_budget_usd - self.ledger.month_total_usd()


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("ts must be a string")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"ts is not ISO 8601: {value!r}") from exc
    return _as_utc(timestamp, name="ts")


def _as_utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value.astimezone(UTC)


def _require_non_negative_finite(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return number
