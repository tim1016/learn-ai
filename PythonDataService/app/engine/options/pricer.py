"""Unified option pricer for the backtest engine.

Wraps QuantLib (or market data) into a single interface the engine uses
for all pricing and Greeks.  The strategy never calls QuantLib directly —
it calls this module, which routes to the appropriate source based on
the pricing mode.

When ``PricingMode.QUANTLIB_ONLY``, all prices and Greeks come from
QuantLib's analytical Black-Scholes (or whichever engine is selected).
When ``PricingMode.MARKET_PREFERRED``, market-observed values are used
where present and QuantLib fills the gaps.
When ``PricingMode.MARKET_REQUIRED``, only real market data is accepted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PricingMode(str, Enum):
    """How the engine resolves option prices and Greeks."""
    QUANTLIB_ONLY = "quantlib_only"
    MARKET_PREFERRED = "market_preferred"
    MARKET_REQUIRED = "market_required"


class SpreadType(str, Enum):
    BULL_CALL = "BULL_CALL"
    BULL_PUT = "BULL_PUT"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OptionGreeks:
    """Greeks snapshot for a single option contract."""
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1% IV move
    rho: float = 0.0


@dataclass
class PricedContract:
    """A fully-priced option contract — the engine's canonical representation.

    Populated by either QuantLib or market data depending on pricing mode.
    The ``source`` field records provenance for logging and trust assessment.
    """
    symbol: str                   # e.g., "O:SPY240419C00520000"
    underlying: str               # e.g., "SPY"
    strike: Decimal
    expiration: date
    option_type: str              # "call" or "put"
    theoretical_price: float      # QuantLib NPV or market mid
    bid: Optional[float] = None   # real bid (None if synthetic)
    ask: Optional[float] = None   # real ask (None if synthetic)
    implied_volatility: float = 0.0
    greeks: OptionGreeks = field(default_factory=lambda: OptionGreeks(0, 0, 0, 0))
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    source: str = "quantlib_synthetic"  # "live", "historical_aggs", "quantlib_synthetic"

    @property
    def mid_price(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.theoretical_price

    def fill_price(self, side: str, half_spread_pct: float = 0.01) -> float:
        """Compute a fill price for this contract.

        If real bid/ask are available, uses them directly.
        Otherwise simulates a spread around the theoretical price.

        Args:
            side: "buy" fills at the ask, "sell" fills at the bid.
            half_spread_pct: Half the simulated bid-ask spread as a
                fraction of theoretical price (used only when real
                bid/ask are absent).
        """
        if self.bid is not None and self.ask is not None and self.bid > 0:
            return self.ask if side == "buy" else self.bid

        # Synthetic spread around theoretical price
        spread = abs(self.theoretical_price) * half_spread_pct
        if side == "buy":
            return self.theoretical_price + spread
        return max(self.theoretical_price - spread, 0.001)


# ---------------------------------------------------------------------------
# QuantLib pricing
# ---------------------------------------------------------------------------

def _price_with_quantlib(
    spot: float,
    strike: float,
    expiration: date,
    option_type: str,
    volatility: float,
    risk_free_rate: float = 0.05,
    dividend_yield: float = 0.0,
    evaluation_date: Optional[date] = None,
    engine: str = "analytic_bs",
) -> tuple[float, OptionGreeks]:
    """Price a single option via QuantLib and return (price, greeks).

    Wraps ``quantlib_pricer.price_option`` and converts its result into
    the engine's ``OptionGreeks`` dataclass.
    """
    from app.services.quantlib_pricer import (
        PricingEngine,
        price_option,
    )

    result = price_option(
        spot=spot,
        strike=strike,
        risk_free_rate=risk_free_rate,
        volatility=volatility,
        expiration_date=expiration,
        option_type=option_type,
        evaluation_date=evaluation_date,
        dividend_yield=dividend_yield,
        engine=PricingEngine(engine),
    )

    greeks = OptionGreeks(
        delta=result.delta,
        gamma=result.gamma,
        theta=result.theta,
        vega=result.vega,
        rho=result.rho,
    )
    return result.price, greeks


def price_contract(
    underlying_price: float,
    strike: float,
    expiration: date,
    option_type: str,
    volatility: float,
    evaluation_date: date,
    risk_free_rate: float = 0.05,
    dividend_yield: float = 0.0,
    engine: str = "analytic_bs",
    symbol: str = "",
    underlying: str = "",
) -> PricedContract:
    """Price a single option contract using QuantLib.

    This is the primary entry point for ``QUANTLIB_ONLY`` mode. Returns a
    fully populated ``PricedContract`` with theoretical price, Greeks, and
    the IV that was supplied (since QuantLib doesn't solve IV here — that
    goes through ``implied_volatility()``).
    """
    price, greeks = _price_with_quantlib(
        spot=underlying_price,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        volatility=volatility,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        evaluation_date=evaluation_date,
        engine=engine,
    )

    return PricedContract(
        symbol=symbol or f"{underlying}_{strike}_{option_type[0].upper()}_{expiration}",
        underlying=underlying,
        strike=Decimal(str(strike)),
        expiration=expiration,
        option_type=option_type,
        theoretical_price=price,
        bid=None,
        ask=None,
        implied_volatility=volatility,
        greeks=greeks,
        open_interest=None,
        volume=None,
        source="quantlib_synthetic",
    )


def price_contract_from_market(
    contract: PricedContract,
    underlying_price: float,
    evaluation_date: date,
    risk_free_rate: float = 0.05,
    dividend_yield: float = 0.0,
    engine: str = "analytic_bs",
) -> PricedContract:
    """Re-price a contract that already has market data using QuantLib Greeks.

    Used in ``MARKET_PREFERRED`` mode when market bid/ask/IV are available
    but Greeks need to be computed (e.g., historical aggs with no Greeks).
    Preserves the market prices and IV, replaces Greeks with QuantLib values.
    """
    if contract.implied_volatility <= 0:
        return contract

    _, greeks = _price_with_quantlib(
        spot=underlying_price,
        strike=float(contract.strike),
        expiration=contract.expiration,
        option_type=contract.option_type,
        volatility=contract.implied_volatility,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        evaluation_date=evaluation_date,
        engine=engine,
    )
    contract.greeks = greeks
    return contract
