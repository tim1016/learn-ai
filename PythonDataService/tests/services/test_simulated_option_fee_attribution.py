"""A simulated account's option fills are charged by the option fee model and shared among its bots (#2913).

Every figure is derived by hand from the rates in force on 2026-09-08: SEC
$0.0000206 a dollar sold, TAF $0.00329 a contract sold, CAT $0.000003 an
equivalent share, ORF $0.015 and OCC $0.025 a contract.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.broker.contract.models import OrderSide
from app.services.alpaca_fee_attribution import FeeFill, attribute_simulated_option_fees

_DAY = date(2026, 9, 8)


def _fill(fill_id: str, subject: str, side: OrderSide, contracts: int, premium: str) -> FeeFill:
    return FeeFill(
        fill_id=fill_id, subject_id=subject, side=side, quantity=Decimal(contracts), price=Decimal(premium),
        multiplier=100,
    )


def _charged(fills: list[FeeFill], *, session_ended: bool = False) -> dict[tuple[str, str], Decimal]:
    result = attribute_simulated_option_fees(
        trade_date=_DAY, fills=fills, population_complete=True, session_ended=session_ended, settlement_at_ms=7,
    )
    assert result.known, result.unresolved
    return {(share.subject_id, share.charge_id.rsplit(":", 1)[-1]): share.amount for share in result.shares}


def test_one_bots_round_trip_is_charged_each_fee_type_rounded_up_once_for_the_day() -> None:
    """Bought at $1.94 and sold at $2.40: SEC $0.004944, TAF $0.00329, CAT $0.0006, ORF $0.03, OCC $0.05."""
    charged = _charged([
        _fill("buy", "bot:a", OrderSide.BUY, 1, "1.94"),
        _fill("sell", "bot:a", OrderSide.SELL, 1, "2.40"),
    ])

    assert charged == {
        ("bot:a", "sec"): Decimal("0.01"), ("bot:a", "taf"): Decimal("0.01"), ("bot:a", "cat"): Decimal("0.01"),
        ("bot:a", "orf"): Decimal("0.03"), ("bot:a", "occ"): Decimal("0.05"),
    }


def test_two_bots_share_each_fee_types_cents_by_what_each_accrued() -> None:
    """Bot A buys ten contracts and bot B one. ORF: $0.165, charged $0.17; OCC: $0.275, charged $0.28.

    Of the ORF's 17 cents A's exact share is 15.45 and B's 1.55: fifteen and
    one whole cents, and the odd cent goes to the larger remainder, B's. Of
    the OCC's 28 cents A's 25.45 is twenty-five and B's 2.55 takes the odd
    cent. CAT, $0.0033, is one cent: A's share of it is 0.91 and B's 0.09, so
    it goes to A.
    """
    charged = _charged([
        _fill("a", "bot:a", OrderSide.BUY, 10, "1.94"),
        _fill("b", "bot:b", OrderSide.BUY, 1, "1.94"),
    ])

    assert charged == {
        ("bot:a", "cat"): Decimal("0.01"),
        ("bot:a", "orf"): Decimal("0.15"), ("bot:b", "orf"): Decimal("0.02"),
        ("bot:a", "occ"): Decimal("0.25"), ("bot:b", "occ"): Decimal("0.03"),
    }


def test_a_day_still_open_is_an_estimate_and_a_day_over_is_settled() -> None:
    fills = [_fill("buy", "bot:a", OrderSide.BUY, 1, "1.94")]

    open_day = attribute_simulated_option_fees(
        trade_date=_DAY, fills=fills, population_complete=True, session_ended=False, settlement_at_ms=7
    )
    closed_day = attribute_simulated_option_fees(
        trade_date=_DAY, fills=fills, population_complete=True, session_ended=True, settlement_at_ms=7
    )

    assert {share.state for share in open_day.shares} == {"estimated"}
    assert {share.state for share in closed_day.shares} == {"modelled_settled"}
    assert open_day.predicted_total == closed_day.predicted_total == Decimal("0.06")


def test_a_day_before_any_option_rate_is_pinned_is_unresolved_never_free() -> None:
    result = attribute_simulated_option_fees(
        trade_date=date(2025, 6, 2), fills=[_fill("buy", "bot:a", OrderSide.BUY, 1, "1.94")],
        population_complete=True, session_ended=True, settlement_at_ms=7,
    )

    assert not result.known and result.shares == () and result.predicted_total is None


def test_a_day_whose_fills_are_not_all_known_charges_no_bot_and_says_so() -> None:
    result = attribute_simulated_option_fees(
        trade_date=_DAY, fills=[_fill("buy", "bot:a", OrderSide.BUY, 1, "1.94")],
        population_complete=False, session_ended=True, settlement_at_ms=7,
    )

    assert not result.known and result.shares == ()


def test_no_option_fills_is_no_charge() -> None:
    result = attribute_simulated_option_fees(
        trade_date=_DAY, fills=[], population_complete=True, session_ended=True, settlement_at_ms=7
    )

    assert result.known and result.shares == () and result.predicted_total == 0
