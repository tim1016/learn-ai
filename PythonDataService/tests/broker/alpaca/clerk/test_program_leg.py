"""Program legs: market DAY inside the regular session, a marketable DAY limit flagged for extended hours outside it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    REGULAR_SESSION_SHAPE,
    LegShape,
    ProgramLegPolicy,
    ProgramLegRefused,
    shape_program_leg,
)
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_POLICY = ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES)


def _bar(hour: int, minute: int, *, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1,
        account_id="PA-TEST",
        provider="ibkr",
        symbol="SPY",
        bar_identity=f"ibkr:SPY:{end - 60_000}:{end}",
        bar_ref="bar-1",
        start_ms=end - 60_000,
        end_ms=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=end,
        session_phase="UNKNOWN",
    )


def test_rth_binding_is_always_a_market_day_leg() -> None:
    shape = shape_program_leg(
        side=OrderSide.BUY,
        purpose=EffectPurpose.ENTER,
        use_rth=True,
        decision_bar=None,
        policy=_POLICY,
    )

    assert shape is REGULAR_SESSION_SHAPE
    leg = shape.apply(symbol="SPY", side=OrderSide.BUY, quantity=3.0)
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )


def test_extended_binding_inside_the_regular_session_is_a_market_day_leg() -> None:
    shape = shape_program_leg(
        side=OrderSide.BUY,
        purpose=EffectPurpose.ENTER,
        use_rth=False,
        decision_bar=_bar(10, 0),
        policy=_POLICY,
    )

    assert shape is REGULAR_SESSION_SHAPE


@pytest.mark.parametrize(
    ("hour", "minute", "side", "purpose", "expected_limit"),
    [
        (5, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10),  # PRE, entry allowance 10 bps up
        (16, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10),  # the regular close itself is POST
        (18, 30, OrderSide.SELL, EffectPurpose.EXIT, 99.80),  # POST, exit allowance 20 bps down
    ],
)
def test_extended_binding_outside_the_regular_session_is_a_marketable_day_limit(
    hour: int,
    minute: int,
    side: OrderSide,
    purpose: EffectPurpose,
    expected_limit: float,
) -> None:
    shape = shape_program_leg(
        side=side,
        purpose=purpose,
        use_rth=False,
        decision_bar=_bar(hour, minute),
        policy=_POLICY,
    )

    assert shape.order_type is OrderType.LIMIT
    assert shape.time_in_force is TimeInForce.DAY
    assert shape.extended_hours is True
    assert shape.limit_price == expected_limit
    assert shape.side is side
    leg = shape.apply(symbol="SPY", side=side, quantity=2.5)
    assert leg.extended_hours is True
    assert leg.limit_price == expected_limit


@pytest.mark.parametrize(
    ("policy", "bar", "reason_code"),
    [
        (
            ProgramLegPolicy(window=None, allowances=_ALLOWANCES),
            _bar(18, 0),
            "EXTENDED_HOURS_UNSUPPORTED",
        ),
        (_POLICY, None, "EXTENDED_ANCHOR_UNAVAILABLE"),
        (_POLICY, _bar(20, 0), "SESSION_CLOSED_AT_DECISION"),
        (_POLICY, _bar(3, 30), "SESSION_CLOSED_AT_DECISION"),
        (
            ProgramLegPolicy(window=_WINDOW, allowances=None),
            _bar(18, 0),
            "EXTENDED_HOURS_ALLOWANCE_UNSET",
        ),
    ],
)
def test_refusals(policy: ProgramLegPolicy, bar: RetainedSourceBar | None, reason_code: str) -> None:
    with pytest.raises(ProgramLegRefused) as caught:
        shape_program_leg(
            side=OrderSide.BUY,
            purpose=EffectPurpose.ENTER,
            use_rth=False,
            decision_bar=bar,
            policy=policy,
        )

    assert caught.value.reason_code == reason_code
    assert caught.value.next_step


def test_allowance_unset_inside_the_regular_session_still_shapes_a_market_leg() -> None:
    policy = ProgramLegPolicy(window=_WINDOW, allowances=None)

    assert (
        shape_program_leg(
            side=OrderSide.BUY,
            purpose=EffectPurpose.ENTER,
            use_rth=False,
            decision_bar=_bar(11, 0),
            policy=policy,
        )
        is REGULAR_SESSION_SHAPE
    )


def test_apply_rejects_a_side_the_shape_was_not_priced_for() -> None:
    """Nothing else reconciles ``side`` with ``self.side``; ``apply`` must (review finding 5)."""
    shape = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=100.10,
        extended_hours=True,
        side=OrderSide.BUY,
    )

    with pytest.raises(ValueError, match="priced for the other side"):
        shape.apply(symbol="SPY", side=OrderSide.SELL, quantity=1.0)
