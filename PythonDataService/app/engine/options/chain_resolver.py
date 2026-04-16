"""ChainResolver — mode-aware option chain data resolution for the backtest engine.

Resolves option chain data using one of three pricing modes:

* ``QUANTLIB_ONLY``    — contract metadata from Polygon + QuantLib pricing.
                         Deterministic, fast, zero per-bar API calls.
* ``MARKET_PREFERRED`` — tries live snapshot first, then historical data,
                         falls back to QuantLib synthetic.
* ``MARKET_REQUIRED``  — real market data only; returns None if unavailable.

The strategy sees a uniform ``ResolvedChain`` regardless of which source
was used. Each ``PricedContract`` carries a ``source`` tag for logging.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.engine.options.pricer import (
    OptionGreeks,
    PricedContract,
    PricingMode,
    price_contract,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolved chain — what the strategy receives
# ---------------------------------------------------------------------------


@dataclass
class ResolvedChain:
    """A filtered, priced option chain for one underlying at one point in time.

    Contains all contracts that survived DTE and basic validity filtering.
    The strategy applies further filters (liquidity, delta targeting) on top.
    """

    underlying: str
    underlying_price: float
    evaluation_date: date
    contracts: list[PricedContract] = field(default_factory=list)
    source: str = "quantlib_synthetic"

    @property
    def calls(self) -> list[PricedContract]:
        return [c for c in self.contracts if c.option_type == "call"]

    @property
    def puts(self) -> list[PricedContract]:
        return [c for c in self.contracts if c.option_type == "put"]

    def filter_by_expiration(self, expiration: date) -> list[PricedContract]:
        return [c for c in self.contracts if c.expiration == expiration]

    def available_expirations(self) -> list[date]:
        return sorted({c.expiration for c in self.contracts})


# ---------------------------------------------------------------------------
# Liquidity and DTE filters (ported from contract_finder.py)
# ---------------------------------------------------------------------------


def passes_liquidity_filter(
    contract: PricedContract,
    min_open_interest: int = 100,
    min_volume: int = 10,
    max_bid_ask_spread_pct: float = 0.20,
) -> bool:
    """Apply liquidity filters to a priced contract.

    In QUANTLIB_ONLY mode, OI and volume are None — the filter is relaxed
    to only check that the theoretical price is positive (since there's no
    real market data to filter on).
    """
    # In synthetic mode, skip liquidity filters (no market data to check)
    if contract.source == "quantlib_synthetic":
        return contract.theoretical_price > 0.001

    # Real data available — apply full filters
    if contract.open_interest is not None and contract.open_interest < min_open_interest:
        return False
    if contract.volume is not None and contract.volume < min_volume:
        return False

    if contract.bid is not None and contract.ask is not None:
        if contract.bid <= 0 or contract.ask <= 0:
            return False
        mid = (contract.bid + contract.ask) / 2.0
        if mid > 0:
            spread_pct = (contract.ask - contract.bid) / mid
            if spread_pct > max_bid_ask_spread_pct:
                return False

    return True


def select_expiration(
    chain: ResolvedChain,
    min_dte: int = 7,
    max_dte: int = 30,
) -> date | None:
    """Select the nearest valid expiration within the DTE window.

    Returns the closest expiration that satisfies min_dte <= DTE <= max_dte,
    or None if no valid expiration exists.
    """
    expirations = chain.available_expirations()
    valid = []
    for exp in expirations:
        dte = (exp - chain.evaluation_date).days
        if min_dte <= dte <= max_dte:
            valid.append((dte, exp))

    if not valid:
        return None

    # Nearest valid expiration (smallest DTE)
    valid.sort(key=lambda x: x[0])
    return valid[0][1]


def select_by_delta(
    contracts: list[PricedContract],
    target_delta: float,
) -> PricedContract | None:
    """Select the contract with delta closest to the target.

    Tie-breaker: highest open interest, then smallest bid-ask spread.
    """
    if not contracts:
        return None

    def sort_key(c: PricedContract) -> tuple[float, int, float]:
        delta_dist = abs(c.greeks.delta - target_delta)
        # Negate OI so higher OI sorts first
        oi = -(c.open_interest or 0)
        # Spread: lower is better
        spread = 999.0
        if c.bid is not None and c.ask is not None and c.bid > 0:
            spread = c.ask - c.bid
        return (delta_dist, oi, spread)

    return min(contracts, key=sort_key)


# ---------------------------------------------------------------------------
# Chain resolver
# ---------------------------------------------------------------------------


class ChainResolver:
    """Resolves option chains using the configured pricing mode.

    For V1, the primary path is QUANTLIB_ONLY which generates a synthetic
    chain from strike/expiration metadata and QuantLib pricing.
    """

    def __init__(
        self,
        pricing_mode: PricingMode = PricingMode.QUANTLIB_ONLY,
        pricing_engine: str = "analytic_bs",
        risk_free_rate: float = 0.05,
        dividend_yield: float = 0.0,
        default_iv: float = 0.20,
        half_spread_pct: float = 0.01,
    ) -> None:
        self.pricing_mode = pricing_mode
        self.pricing_engine = pricing_engine
        self.risk_free_rate = risk_free_rate
        self.dividend_yield = dividend_yield
        self.default_iv = default_iv
        self.half_spread_pct = half_spread_pct

    def resolve(
        self,
        underlying: str,
        underlying_price: float,
        evaluation_date: date,
        min_dte: int = 7,
        max_dte: int = 30,
        strike_range_pct: float = 0.15,
        iv_override: float | None = None,
    ) -> ResolvedChain | None:
        """Resolve an option chain for the given underlying and date.

        Args:
            underlying: Ticker symbol (e.g., "SPY").
            underlying_price: Current price of the underlying.
            evaluation_date: The date to resolve the chain for.
            min_dte: Minimum days to expiration.
            max_dte: Maximum days to expiration.
            strike_range_pct: Search strikes within ±this % of spot.
            iv_override: If provided, use this IV for all contracts.
                         Otherwise uses ``self.default_iv``.

        Returns:
            ResolvedChain with priced contracts, or None if resolution fails.
        """
        if self.pricing_mode == PricingMode.QUANTLIB_ONLY:
            return self._resolve_quantlib_only(
                underlying,
                underlying_price,
                evaluation_date,
                min_dte,
                max_dte,
                strike_range_pct,
                iv_override,
            )
        elif self.pricing_mode == PricingMode.MARKET_PREFERRED:
            # Try market data first, fall back to QuantLib
            chain = self._resolve_market(
                underlying,
                underlying_price,
                evaluation_date,
                min_dte,
                max_dte,
                strike_range_pct,
            )
            if chain is not None and len(chain.contracts) > 0:
                return chain
            logger.info(
                "[ChainResolver] Market data unavailable for %s on %s, falling back to QuantLib synthetic",
                underlying,
                evaluation_date,
            )
            return self._resolve_quantlib_only(
                underlying,
                underlying_price,
                evaluation_date,
                min_dte,
                max_dte,
                strike_range_pct,
                iv_override,
            )
        elif self.pricing_mode == PricingMode.MARKET_REQUIRED:
            return self._resolve_market(
                underlying,
                underlying_price,
                evaluation_date,
                min_dte,
                max_dte,
                strike_range_pct,
            )
        else:
            raise ValueError(f"Unknown pricing mode: {self.pricing_mode}")

    # ------------------------------------------------------------------
    # QUANTLIB_ONLY — synthetic chain from strike grid + QuantLib pricing
    # ------------------------------------------------------------------

    def _resolve_quantlib_only(
        self,
        underlying: str,
        underlying_price: float,
        evaluation_date: date,
        min_dte: int,
        max_dte: int,
        strike_range_pct: float,
        iv_override: float | None = None,
    ) -> ResolvedChain:
        """Build a synthetic chain using a strike grid and QuantLib pricing.

        Generates a grid of strikes around the underlying price at standard
        intervals, creates expirations within the DTE window (weekly on
        Fridays), and prices every (strike, expiration, type) combination
        via QuantLib.
        """
        iv = iv_override or self.default_iv
        contracts: list[PricedContract] = []

        # Generate expirations: weekly Fridays within the DTE window
        expirations = self._generate_expirations(evaluation_date, min_dte, max_dte)

        # Generate strikes: $1 increments within ±strike_range_pct of spot
        strikes = self._generate_strikes(underlying_price, strike_range_pct)

        for expiration in expirations:
            for strike in strikes:
                for option_type in ("call", "put"):
                    contract = price_contract(
                        underlying_price=underlying_price,
                        strike=strike,
                        expiration=expiration,
                        option_type=option_type,
                        volatility=iv,
                        evaluation_date=evaluation_date,
                        risk_free_rate=self.risk_free_rate,
                        dividend_yield=self.dividend_yield,
                        engine=self.pricing_engine,
                        symbol=self._make_symbol(underlying, strike, option_type, expiration),
                        underlying=underlying,
                    )
                    # Skip contracts with effectively zero price
                    if contract.theoretical_price > 0.001:
                        contracts.append(contract)

        return ResolvedChain(
            underlying=underlying,
            underlying_price=underlying_price,
            evaluation_date=evaluation_date,
            contracts=contracts,
            source="quantlib_synthetic",
        )

    # ------------------------------------------------------------------
    # MARKET_PREFERRED / MARKET_REQUIRED — live or historical data
    # ------------------------------------------------------------------

    def _resolve_market(
        self,
        underlying: str,
        underlying_price: float,
        evaluation_date: date,
        min_dte: int,
        max_dte: int,
        strike_range_pct: float,
    ) -> ResolvedChain | None:
        """Attempt to resolve using real market data.

        Tries the Polygon live snapshot endpoint. For historical dates,
        this will only work if the date is recent enough for the snapshot
        to be valid. Full historical resolution (aggs + IV solving) is a
        V2 feature.
        """
        try:
            from app.services.polygon_client import PolygonClientService

            client = PolygonClientService()

            # Try to fetch contract metadata for this date
            exp_gte = (evaluation_date + timedelta(days=min_dte)).isoformat()
            exp_lte = (evaluation_date + timedelta(days=max_dte)).isoformat()
            strike_lo = underlying_price * (1 - strike_range_pct)
            strike_hi = underlying_price * (1 + strike_range_pct)

            raw_contracts = client.list_options_contracts(
                underlying_ticker=underlying,
                as_of_date=evaluation_date.isoformat(),
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                strike_price_gte=strike_lo,
                strike_price_lte=strike_hi,
                limit=500,
            )

            if not raw_contracts:
                return None

            # Try live snapshot for Greeks/IV/bid-ask
            snapshot = None
            try:
                snapshot = client.list_snapshot_options_chain(underlying)
            except Exception:
                logger.debug("[ChainResolver] Live snapshot unavailable for %s", underlying)

            # Build snapshot lookup by ticker
            snap_by_ticker: dict = {}
            if snapshot and "contracts" in snapshot:
                for sc in snapshot["contracts"]:
                    snap_by_ticker[sc.get("ticker", "")] = sc

            contracts: list[PricedContract] = []
            for rc in raw_contracts:
                ticker = rc.get("ticker", "")
                strike = float(rc.get("strike_price", 0))
                exp_str = rc.get("expiration_date", "")
                ctype = rc.get("contract_type", "call")

                if not exp_str or strike <= 0:
                    continue

                expiration = date.fromisoformat(exp_str)
                snap = snap_by_ticker.get(ticker)

                if snap:
                    # Real market data available
                    greeks_data = snap.get("greeks") or {}
                    last_quote = snap.get("last_quote") or {}
                    day_data = snap.get("day") or {}

                    contract = PricedContract(
                        symbol=ticker,
                        underlying=underlying,
                        strike=Decimal(str(strike)),
                        expiration=expiration,
                        option_type=ctype,
                        theoretical_price=float(day_data.get("close", 0) or 0),
                        bid=float(last_quote.get("bid", 0) or 0),
                        ask=float(last_quote.get("ask", 0) or 0),
                        implied_volatility=float(snap.get("implied_volatility", 0) or 0),
                        greeks=OptionGreeks(
                            delta=float(greeks_data.get("delta", 0) or 0),
                            gamma=float(greeks_data.get("gamma", 0) or 0),
                            theta=float(greeks_data.get("theta", 0) or 0),
                            vega=float(greeks_data.get("vega", 0) or 0),
                        ),
                        open_interest=int(snap.get("open_interest", 0) or 0),
                        volume=int(day_data.get("volume", 0) or 0),
                        source="live",
                    )
                else:
                    # Contract metadata only — price with QuantLib
                    iv = self.default_iv
                    contract = price_contract(
                        underlying_price=underlying_price,
                        strike=strike,
                        expiration=expiration,
                        option_type=ctype,
                        volatility=iv,
                        evaluation_date=evaluation_date,
                        risk_free_rate=self.risk_free_rate,
                        dividend_yield=self.dividend_yield,
                        engine=self.pricing_engine,
                        symbol=ticker,
                        underlying=underlying,
                    )

                if contract.theoretical_price > 0.001 or (contract.bid is not None and contract.bid > 0):
                    contracts.append(contract)

            source = "live" if snap_by_ticker else "quantlib_synthetic"
            return ResolvedChain(
                underlying=underlying,
                underlying_price=underlying_price,
                evaluation_date=evaluation_date,
                contracts=contracts,
                source=source,
            )

        except ImportError:
            logger.warning("[ChainResolver] PolygonClientService not available")
            return None
        except Exception as e:
            logger.warning("[ChainResolver] Market resolution failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_expirations(
        evaluation_date: date,
        min_dte: int,
        max_dte: int,
    ) -> list[date]:
        """Generate weekly Friday expirations within the DTE window."""
        expirations: list[date] = []
        start = evaluation_date + timedelta(days=min_dte)
        end = evaluation_date + timedelta(days=max_dte)

        # Find the first Friday on or after start
        current = start
        days_until_friday = (4 - current.weekday()) % 7
        if days_until_friday == 0 and current.weekday() != 4:
            days_until_friday = 7
        current = current + timedelta(days=days_until_friday)

        while current <= end:
            expirations.append(current)
            current += timedelta(weeks=1)

        return expirations

    @staticmethod
    def _generate_strikes(
        underlying_price: float,
        range_pct: float,
        increment: float = 1.0,
    ) -> list[float]:
        """Generate a grid of strikes around the underlying price.

        For prices above $100, uses $1 increments. For prices below $100,
        uses $0.50 increments. This roughly matches SPY's standard strike
        spacing.
        """
        if underlying_price >= 100:
            increment = 1.0
        else:
            increment = 0.50

        low = underlying_price * (1 - range_pct)
        high = underlying_price * (1 + range_pct)

        # Round to nearest increment
        low = math.floor(low / increment) * increment
        high = math.ceil(high / increment) * increment

        strikes: list[float] = []
        s = low
        while s <= high:
            strikes.append(round(s, 2))
            s += increment

        return strikes

    @staticmethod
    def _make_symbol(
        underlying: str,
        strike: float,
        option_type: str,
        expiration: date,
    ) -> str:
        """Generate a synthetic OCC-style option symbol."""
        type_char = "C" if option_type == "call" else "P"
        exp_str = expiration.strftime("%y%m%d")
        strike_int = int(strike * 1000)
        return f"O:{underlying}{exp_str}{type_char}{strike_int:08d}"
