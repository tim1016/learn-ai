"""Golden FEE-003: the option fee model charges the TAF Alpaca Paper posted, to the cent, on every recorded day."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.option_fees import (
    OptionFillFees,
    fees_for_option_fill,
    option_rates_for,
    settle_option_session,
)
from app.broker.contract.models import OrderSide
from app.utils.session_anchors import et_midnight_ms

FIXTURE_DIR = Path(__file__).parent / "golden" / "broker-fees" / "FEE-003" / "v1"
_CENT = Decimal("0.01")


def _days() -> list[tuple[dict, Decimal]]:
    """Each recorded day with the amount Alpaca Paper posted for it, in dollars."""
    fixture_input = json.loads((FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))
    posted = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))["posted_taf"]
    assert [day["trade_date_ms"] for day in fixture_input["days"]] == [row["trade_date_ms"] for row in posted]
    return [(day, Decimal(row["taf"])) for day, row in zip(fixture_input["days"], posted, strict=True)]


def _sells(day: dict) -> list[OptionFillFees]:
    """The day's sold executions, one per trade. TAF is per contract, so the premium is a placeholder."""
    per_trade = Decimal(day["sold_contracts"]) / Decimal(day["trades"])
    assert per_trade == per_trade.to_integral_value()
    return [
        fees_for_option_fill(
            trade_date_ms=day["trade_date_ms"], side=OrderSide.SELL, contracts=per_trade,
            premium=Decimal("1.00"), multiplier=100,
        )
        for _ in range(day["trades"])
    ]


def test_fee_003_the_models_day_taf_is_every_posted_row_to_the_cent() -> None:
    days = _days()
    assert len(days) == 6

    for day, posted in days:
        # Exact Decimal equality: atol=0, rtol=0 (manifest FEE-003).
        assert settle_option_session(_sells(day)).taf == posted, day["trade_date_ms"]


def test_fee_003_the_rows_bound_the_rate_and_the_pinned_rate_sits_inside_the_bound_the_regulators_is_outside() -> None:
    """A day of N contracts posted at C cents says (C - 0.01) / N < r <= C / N; the six days together: (0.08/30, 0.003]."""
    days = _days()
    lower = max((posted - _CENT) / day["sold_contracts"] for day, posted in days)
    upper = min(posted / day["sold_contracts"] for day, posted in days)

    pinned = {option_rates_for(day["trade_date_ms"]).taf_per_contract for day, _ in days}
    regulators = option_rates_for(et_midnight_ms(date(2026, 9, 30))).taf_per_contract

    assert (lower, upper) == (Decimal("0.08") / 30, Decimal("0.003"))
    assert len(pinned) == 1 and lower < pinned.pop() <= upper
    # FINRA's own rate, in force the day before its pause, is one the rows rule out (19 x it rounds up to $0.07).
    assert regulators > upper
