"""Program legs: market DAY inside the regular session, a marketable DAY limit flagged for extended hours outside it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    LegShape,
    ProgramLeg,
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


def _at(hour: int, minute: int, *, day: date = _DAY) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


def _bar(hour: int, minute: int, *, close: str = "100.00", day: date = _DAY) -> RetainedSourceBar:
    end = _at(hour, minute, day=day)
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


_HALF_DAY = date(2026, 11, 27)  # the day after Thanksgiving: NYSE closes at 13:00


@pytest.mark.parametrize(("side", "purpose"), [(OrderSide.BUY, EffectPurpose.ENTER), (OrderSide.SELL, EffectPurpose.EXIT)])
@pytest.mark.parametrize(
    "decision_bar",
    [
        pytest.param(_bar(15, 30), id="inside-the-session"),
        pytest.param(_bar(16, 0), id="the-closing-bar"),
        pytest.param(_bar(13, 0, day=_HALF_DAY), id="a-half-days-closing-bar"),
        pytest.param(None, id="no-retained-bar"),
    ],
)
def test_a_regular_hours_leg_is_always_a_market_day_leg(
    side: OrderSide, purpose: EffectPurpose, decision_bar: RetainedSourceBar | None
) -> None:
    """ENTER and EXIT alike, whatever bar decided them (#2607).

    Neither runner sends a decision on the closing bar, so a regular-hours run
    has no program leg to price after the close. An EXIT that nonetheless
    reaches the broker after the close -- a delayed send -- is re-priced by the
    send-time rule in ``sqlite/exit_resolution.py``, not shaped here.
    """
    program_leg = shape_program_leg(
        side=side, purpose=purpose, use_rth=True, decision_bar=decision_bar, pricing_bar=decision_bar, policy=_POLICY
    )

    assert program_leg == ProgramLeg(regular_session_shape(side))
    leg = program_leg.shape.apply(symbol="SPY", quantity=3.0)
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("side", "purpose", "policy", "decision_bar", "pricing_bar"),
    [
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, _POLICY, _bar(10, 0), _bar(10, 0),
            id="extended-binding-inside-the-regular-session",
        ),
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, ProgramLegPolicy(window=_WINDOW, allowances=None), _bar(11, 0), _bar(11, 0),
            id="allowance-unset-inside-the-regular-session",
        ),
        # Inside the regular session a leg is a market order, so the decision's time is all it needs.
        pytest.param(
            OrderSide.SELL, EffectPurpose.EXIT, _POLICY, _bar(10, 0), None,
            id="a-market-leg-needs-no-bar-of-the-traded-asset",
        ),
    ],
)
def test_extended_binding_inside_the_regular_session_is_a_market_day_leg(
    side: OrderSide,
    purpose: EffectPurpose,
    policy: ProgramLegPolicy,
    decision_bar: RetainedSourceBar,
    pricing_bar: RetainedSourceBar | None,
) -> None:
    program_leg = shape_program_leg(
        side=side,
        purpose=purpose,
        use_rth=False,
        decision_bar=decision_bar,
        pricing_bar=pricing_bar,
        policy=policy,
    )

    assert program_leg == ProgramLeg(regular_session_shape(side))


@pytest.mark.parametrize(
    ("hour", "minute", "side", "purpose", "expected_limit", "valid_until"),
    [
        # PRE, entry allowance 10 bps up; sendable until the regular open
        (5, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10, (9, 30)),
        # the regular close itself is POST; sendable until the declared close
        (16, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10, (20, 0)),
        # POST, exit allowance 20 bps down
        (18, 30, OrderSide.SELL, EffectPurpose.EXIT, 99.80, (20, 0)),
    ],
)
def test_extended_binding_outside_the_regular_session_is_a_marketable_day_limit(
    hour: int,
    minute: int,
    side: OrderSide,
    purpose: EffectPurpose,
    expected_limit: float,
    valid_until: tuple[int, int],
) -> None:
    program_leg = shape_program_leg(
        side=side,
        purpose=purpose,
        use_rth=False,
        decision_bar=_bar(hour, minute),
        pricing_bar=_bar(hour, minute),
        policy=_POLICY,
    )

    shape = program_leg.shape
    assert shape.order_type is OrderType.LIMIT
    assert shape.time_in_force is TimeInForce.DAY
    assert shape.extended_hours is True
    assert shape.limit_price == expected_limit
    assert shape.side is side
    leg = shape.apply(symbol="SPY", quantity=2.5)
    assert leg.extended_hours is True
    assert leg.limit_price == expected_limit
    assert program_leg.valid_until_ms == _at(*valid_until)


def test_an_extended_leg_decided_at_a_half_days_close_is_bounded_by_its_after_hours_close() -> None:
    """The close is the canonical calendar's, never a 16:00 literal: 13:00 on a half-day.

    So is the after-hours close the leg is bounded by (#2440 review): 17:00 on
    that half-day, not the declared window's 20:00 — Alpaca's after-hours ends
    with the calendar's, and a leg sent at 18:00 would reach no session.
    """
    at_close = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=False,
        decision_bar=_bar(13, 0, day=_HALF_DAY),
        pricing_bar=_bar(13, 0, day=_HALF_DAY),
        policy=_POLICY,
    )
    inside = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=False,
        decision_bar=_bar(12, 59, day=_HALF_DAY),
        pricing_bar=_bar(12, 59, day=_HALF_DAY),
        policy=_POLICY,
    )

    assert (at_close.shape.order_type, at_close.shape.extended_hours) == (OrderType.LIMIT, True)
    assert at_close.valid_until_ms == _at(17, 0, day=_HALF_DAY)
    assert inside == ProgramLeg(regular_session_shape(OrderSide.SELL))


@pytest.mark.parametrize(
    ("side", "purpose", "policy", "bar", "reason_code"),
    [
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, ProgramLegPolicy(window=None, allowances=_ALLOWANCES), _bar(18, 0),
            "EXTENDED_HOURS_UNSUPPORTED",
            id="no-declared-window-is-extended-hours-unsupported",
        ),
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, _POLICY, None,
            "EXTENDED_ANCHOR_UNAVAILABLE",
            id="no-bar-is-extended-anchor-unavailable",
        ),
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, _POLICY, _bar(20, 0),
            "SESSION_CLOSED_AT_DECISION",
            id="bar-at-the-window-close-is-session-closed",
        ),
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, _POLICY, _bar(3, 30),
            "SESSION_CLOSED_AT_DECISION",
            id="bar-before-the-window-open-is-session-closed",
        ),
        pytest.param(
            OrderSide.BUY, EffectPurpose.ENTER, ProgramLegPolicy(window=_WINDOW, allowances=None), _bar(18, 0),
            "EXTENDED_HOURS_ALLOWANCE_UNSET",
            id="allowances-unset-is-extended-hours-allowance-unset",
        ),
        # 17:30 on 2026-11-27 is inside the declared 04:00-20:00 window but past the calendar's
        # after-hours close: no session accepts a leg, so nothing is priced for one.
        pytest.param(
            OrderSide.SELL, EffectPurpose.EXIT, _POLICY, _bar(17, 30, day=date(2026, 11, 27)),
            "SESSION_CLOSED_AT_DECISION",
            id="after-a-half-days-after-hours-close-is-session-closed-2440-review",
        ),
        # A quantised anchor at or below zero must reach the Clerk as a refusal: LegShape.apply
        # would otherwise raise pydantic's ValidationError (limit_price gt=0) from *outside* the
        # ``except ProgramLegRefused`` in ``runtime._execute_effect``, killing the shielded effect
        # task instead of writing a rejected receipt.
        pytest.param(
            OrderSide.SELL, EffectPurpose.EXIT,
            ProgramLegPolicy(
                window=_WINDOW,
                allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("9999.99")),
            ),
            _bar(18, 0, close="0.0100"),
            "EXTENDED_ANCHOR_UNPRICEABLE",
            id="unpriceable-anchor-is-a-typed-refusal-not-a-validation-error",
        ),
    ],
)
def test_refusals(
    side: OrderSide, purpose: EffectPurpose, policy: ProgramLegPolicy, bar: RetainedSourceBar | None, reason_code: str
) -> None:
    with pytest.raises(ProgramLegRefused) as caught:
        shape_program_leg(
            side=side,
            purpose=purpose,
            use_rth=False,
            decision_bar=bar,
            pricing_bar=bar,
            policy=policy,
        )

    assert caught.value.reason_code == reason_code, "the refusal carries its typed reason code"
    assert caught.value.next_step, "the refusal says what to do next"


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
        pricing_bar=_bar(18, 30),
        policy=_POLICY,
    )

    assert shape.shape.apply(symbol="SPY", quantity=1.0).side is OrderSide.SELL


@pytest.mark.parametrize(
    ("shape", "valid_until_ms"),
    [
        pytest.param(
            LegShape(
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=99.80,
                extended_hours=True,
                side=OrderSide.SELL,
            ),
            None,
            id="an-extended-limit-without-the-end-of-its-session",
        ),
        pytest.param(regular_session_shape(OrderSide.SELL), _at(20, 0), id="a-market-leg-with-a-bound"),
    ],
)
def test_a_program_leg_never_travels_without_the_bound_its_shape_needs(
    shape: LegShape, valid_until_ms: int | None
) -> None:
    """#2440 review: the shape and its bound are one value from the decision to the acceptance.

    An extended-hours shape recorded without the end of the session it was
    priced for is read at send time as priced for no session — expired on
    arrival, and re-priced off the live quote. The pair is refused where it
    is built instead of being split into two optional arguments downstream.
    """
    with pytest.raises(ValueError, match="end of the session"):
        ProgramLeg(shape, valid_until_ms=valid_until_ms)


def test_an_extended_limit_is_anchored_to_the_traded_assets_bar_not_the_signals() -> None:
    """The signal closed at $500 and the traded asset at $40: the limit is ten bps over $40 (#2912)."""
    program_leg = shape_program_leg(
        side=OrderSide.BUY,
        purpose=EffectPurpose.ENTER,
        use_rth=False,
        decision_bar=_bar(5, 0, close="500.00"),
        pricing_bar=_bar(5, 0, close="40.00"),
        policy=_POLICY,
    )

    assert program_leg.shape.limit_price == 40.04


@pytest.mark.parametrize(
    ("side", "purpose"),
    [
        pytest.param(OrderSide.BUY, EffectPurpose.ENTER, id="enter"),
        # Interim (#2912): an EXIT is never to be refused for want of a price,
        # and no source for an extended-session one is decided yet. What must
        # hold meanwhile is the other half: it is not anchored to the signal.
        pytest.param(OrderSide.SELL, EffectPurpose.EXIT, id="exit-interim"),
    ],
)
def test_an_extended_leg_with_no_bar_of_the_traded_asset_is_refused_never_priced_from_the_signal(
    side: OrderSide, purpose: EffectPurpose
) -> None:
    with pytest.raises(ProgramLegRefused) as refused:
        shape_program_leg(
            side=side,
            purpose=purpose,
            use_rth=False,
            decision_bar=_bar(18, 30),
            pricing_bar=None,
            policy=_POLICY,
        )

    assert refused.value.reason_code == "PRICING_BAR_UNAVAILABLE"
