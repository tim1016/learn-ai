"""Tests for ADR 0009's policy-application adapter and discriminated union."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.execution.order_sizer import (
    FixedNotional,
    FixedShares,
    OrderSizer,
    SetHoldings,
    SizingKindNotWiredError,
    StrategyExplicit,
    WholeAccountPortfolioValueProvider,
    default_sizing_provenance,
    governed_by,
    parse_sizing_policy,
    policy_to_ledger_dict,
)
from app.engine.execution.sizing import SimpleFloorSizing

# ─────────────────────────── parse_sizing_policy ───────────────────────────


def test_parse_fixed_shares_round_trip() -> None:
    policy = parse_sizing_policy({"kind": "FixedShares", "value": 1})
    assert isinstance(policy, FixedShares)
    assert policy.value == 1
    assert policy_to_ledger_dict(policy) == {"kind": "FixedShares", "value": 1}


def test_parse_fixed_shares_rejects_zero() -> None:
    with pytest.raises(ValueError, match=r"invalid live_config\.sizing"):
        parse_sizing_policy({"kind": "FixedShares", "value": 0})


def test_parse_set_holdings_accepts_decimal_string() -> None:
    policy = parse_sizing_policy({"kind": "SetHoldings", "fraction": "1.0"})
    assert isinstance(policy, SetHoldings)
    assert policy.fraction == Decimal("1.0")
    # Round-trip preserves decimal string form (no float noise on the wire).
    assert policy_to_ledger_dict(policy) == {"kind": "SetHoldings", "fraction": "1.0"}


def test_parse_set_holdings_rejects_zero_and_above_one() -> None:
    with pytest.raises(ValueError):
        parse_sizing_policy({"kind": "SetHoldings", "fraction": "0"})
    with pytest.raises(ValueError):
        parse_sizing_policy({"kind": "SetHoldings", "fraction": "1.5"})


def test_parse_fixed_notional_rejects_raw_float() -> None:
    # Money on the wire must be a decimal string to keep run_id hashing stable.
    with pytest.raises(ValueError):
        parse_sizing_policy({"kind": "FixedNotional", "value": 1000.5})


def test_parse_fixed_notional_accepts_string() -> None:
    policy = parse_sizing_policy({"kind": "FixedNotional", "value": "1000.50"})
    assert isinstance(policy, FixedNotional)
    assert policy.value == Decimal("1000.50")
    assert policy_to_ledger_dict(policy) == {"kind": "FixedNotional", "value": "1000.50"}


def test_parse_strategy_explicit() -> None:
    policy = parse_sizing_policy({"kind": "StrategyExplicit"})
    assert isinstance(policy, StrategyExplicit)
    assert policy_to_ledger_dict(policy) == {"kind": "StrategyExplicit"}


def test_parse_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        parse_sizing_policy({"kind": "Bogus", "value": 1})


def test_parse_rejects_extra_keys() -> None:
    with pytest.raises(ValueError):
        parse_sizing_policy({"kind": "FixedShares", "value": 1, "extra": "noise"})


# ─────────────────────────── engine-derived stamps ─────────────────────────


def test_governed_by_for_each_kind() -> None:
    assert governed_by(FixedShares(value=1)) == "live_config"
    assert governed_by(SetHoldings(fraction=Decimal("1.0"))) == "live_config"
    assert governed_by(FixedNotional(value=Decimal("100"))) == "live_config"
    assert governed_by(StrategyExplicit()) == "strategy_explicit"


def test_governed_by_none_is_legacy_live_config() -> None:
    # Absence ⇒ legacy/unknown era; the legacy SimpleFloorSizing path WAS the
    # set_holdings boundary, so it's de facto live_config-governed.
    assert governed_by(None) == "live_config"


def test_default_sizing_provenance_is_live_override_until_pr3() -> None:
    # PR1 has no proof path — every policy defaults to live_override
    # (the fail-closed default; PR3 wires reference_native).
    assert default_sizing_provenance(FixedShares(value=1)) == "live_override"
    assert default_sizing_provenance(StrategyExplicit()) == "live_override"
    assert default_sizing_provenance(None) == "live_override"


# ─────────────────────────── OrderSizer (FixedShares) ──────────────────────


def test_order_sizer_resolves_fixed_shares_when_fraction_positive() -> None:
    sizer = OrderSizer(FixedShares(value=7))
    # FixedShares is direction-only on the fraction: positive ⇒ target value.
    assert sizer.resolve_set_holdings_quantity(target_fraction=Decimal("1.0")) == 7
    assert sizer.resolve_set_holdings_quantity(target_fraction=Decimal("0.5")) == 7


def test_order_sizer_returns_zero_when_fraction_zero() -> None:
    sizer = OrderSizer(FixedShares(value=7))
    # Flat target ⇒ zero shares (engine logs the sizing-skip elsewhere).
    assert sizer.resolve_set_holdings_quantity(target_fraction=Decimal("0")) == 0


def test_order_sizer_rejects_negative_fraction_for_fixed_shares() -> None:
    """Long-only in v1 — a negative fraction is short intent that FixedShares
    would otherwise silently invert to a positive target. Fail fast."""
    sizer = OrderSizer(FixedShares(value=7))
    with pytest.raises(ValueError, match="long-only"):
        sizer.resolve_set_holdings_quantity(target_fraction=Decimal("-0.5"))


@pytest.mark.parametrize(
    ("policy", "lands_in"),
    [
        (FixedNotional(value=Decimal("100")), "PR4"),
        (StrategyExplicit(), "PR7"),
    ],
)
def test_order_sizer_raises_for_unwired_kinds(policy, lands_in) -> None:
    sizer = OrderSizer(policy)
    with pytest.raises(SizingKindNotWiredError) as exc:
        sizer.resolve_set_holdings_quantity(
            target_fraction=Decimal("1.0"), reference_price=Decimal("500")
        )
    assert exc.value.lands_in_pr == lands_in


# ─────────────────────────── OrderSizer (SetHoldings) — PR2 ────────────────


def _set_holdings_sizer(
    *, portfolio_value: Decimal, fraction: str = "1.0"
) -> OrderSizer:
    """Build an OrderSizer with a static whole-account provider for the test."""

    def _value() -> Decimal:
        return portfolio_value

    return OrderSizer(
        SetHoldings(fraction=Decimal(fraction)),
        portfolio_value_provider=WholeAccountPortfolioValueProvider(_value),
    )


def test_set_holdings_routes_through_lean_buffered_fee_aware_resolver() -> None:
    """ADR 0009 PR2 — SetHoldings(1.0) routes through the LEAN-equivalent
    resolver: portfolio_value * (1 - 0.0025) − IBKR per-share fee, NOT the
    legacy SimpleFloor path that bought one share more on every trade.
    """
    portfolio_value = Decimal("100000")
    price = Decimal("500")
    sizer = _set_holdings_sizer(portfolio_value=portfolio_value)

    qty = sizer.resolve_set_holdings_quantity(
        target_fraction=Decimal("1.0"), reference_price=price
    )

    # SimpleFloor would buy floor(100_000 / 500) = 200 shares.
    # Lean buffered: floor((100_000 * 0.9975 - fee(qty,500)) / 500). With
    # IBKR's tiered $0.005/share at qty around 200, fee ≈ $1.00, so
    # buying_power = 99_750, budget = 99_749, qty = 199.
    legacy = SimpleFloorSizing().target_quantity(
        portfolio_value=portfolio_value,
        price=price,
        target_fraction=Decimal("1.0"),
        order_fee=Decimal(0),
    )
    assert legacy == 200, "SimpleFloor baseline pinned for the contrast"
    assert qty == 199, "Lean path should buy 199, not 200 — the documented cutover"


def test_set_holdings_zero_fraction_is_flat() -> None:
    sizer = _set_holdings_sizer(portfolio_value=Decimal("100000"))
    assert (
        sizer.resolve_set_holdings_quantity(
            target_fraction=Decimal("0"), reference_price=Decimal("500")
        )
        == 0
    )


def test_set_holdings_requires_reference_price() -> None:
    sizer = _set_holdings_sizer(portfolio_value=Decimal("100000"))
    with pytest.raises(ValueError, match="reference price"):
        sizer.resolve_set_holdings_quantity(target_fraction=Decimal("1.0"))


def test_set_holdings_requires_portfolio_value_provider() -> None:
    sizer = OrderSizer(SetHoldings(fraction=Decimal("1.0")))
    with pytest.raises(RuntimeError, match="PortfolioValueProvider"):
        sizer.resolve_set_holdings_quantity(
            target_fraction=Decimal("1.0"), reference_price=Decimal("500")
        )


def test_whole_account_provider_reads_fresh_value_each_call() -> None:
    """The seam the future capital sleeve drops into — value resolves at call
    time, not at construction (so changes to portfolio mid-session register).
    """
    portfolio_value = Decimal("100000")

    def _value() -> Decimal:
        return portfolio_value

    provider = WholeAccountPortfolioValueProvider(_value)
    assert provider.portfolio_value() == Decimal("100000")
    portfolio_value = Decimal("110000")
    assert provider.portfolio_value() == Decimal("110000")
