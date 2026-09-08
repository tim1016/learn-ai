"""Program legs: market DAY inside the regular session, a marketable DAY limit flagged for extended hours outside it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    ProgramLegPolicy,
    ProgramLegRefused,
    regular_session_shape,
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

    assert shape == regular_session_shape(OrderSide.BUY)
    leg = shape.apply(symbol="SPY", quantity=3.0)
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

    assert shape == regular_session_shape(OrderSide.BUY)


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
    leg = shape.apply(symbol="SPY", quantity=2.5)
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
        == regular_session_shape(OrderSide.BUY)
    )


def test_apply_uses_the_side_the_shape_was_priced_for() -> None:
    """``side`` is mandatory on the shape, so ``apply`` has no side to reconcile.

    The one place a shape can meet a side its author did not expect is
    ``exit_resolution._create_reducing_order`` (ruling R11), which holds the
    only mismatch policy in the codebase.
    """
    shape = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=False,
        decision_bar=_bar(18, 30),
        policy=_POLICY,
    )

    assert shape.apply(symbol="SPY", quantity=1.0).side is OrderSide.SELL


def test_an_unpriceable_anchor_is_a_typed_refusal_not_a_validation_error() -> None:
    """A quantised anchor at or below zero must reach the Clerk as a refusal.

    ``LegShape.apply`` would otherwise raise pydantic's ``ValidationError``
    (``limit_price gt=0``) from *outside* the ``except ProgramLegRefused`` in
    ``runtime._execute_effect``, killing the shielded effect task instead of
    writing a rejected receipt.
    """
    policy = ProgramLegPolicy(
        window=_WINDOW,
        allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("9999.99")),
    )

    with pytest.raises(ProgramLegRefused) as caught:
        shape_program_leg(
            side=OrderSide.SELL,
            purpose=EffectPurpose.EXIT,
            use_rth=False,
            decision_bar=_bar(18, 0, close="0.0100"),
            policy=policy,
        )

    assert caught.value.reason_code == "EXTENDED_ANCHOR_UNPRICEABLE"
    assert caught.value.next_step
