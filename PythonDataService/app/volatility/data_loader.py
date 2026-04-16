"""
Option Chain Data Loader
=========================

Fetches option chain data from Polygon.io and applies quality filters before
returning structured records suitable for the IV surface builder.

Responsibilities:
- Fetch full option chain for a given ticker and date
- Fetch the underlying spot price
- Apply quality filters (DTE, open interest, bid-ask spread)
- Convert raw contract data to the format expected by VolSurfaceBuilder
- Track rejection reasons and return structured results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status

from app.services.polygon_client import PolygonClientService
from app.volatility.cache import DataFilters
from app.volatility.conventions import dte_to_ttm

logger = logging.getLogger(__name__)


@dataclass
class ChainLoadResult:
    """Result of loading and filtering an option chain."""

    records: list[dict]
    spot: float
    eval_date: str
    total_quotes: int
    accepted: int
    rejected: int
    rejection_reasons: dict[str, int]

    def rejection_summary(self) -> str:
        """Return human-readable summary of rejections."""
        if not self.rejection_reasons:
            return "No rejections"

        items = [f"{reason}: {count}" for reason, count in self.rejection_reasons.items()]
        return ", ".join(items)


class OptionChainLoader:
    """Fetches and filters option chain data from Polygon.io."""

    def __init__(self) -> None:
        self.polygon = PolygonClientService()

    def fetch_chain(
        self,
        ticker: str,
        date: str,
        filters: DataFilters,
        day_count: str = "Actual365Fixed",
    ) -> ChainLoadResult:
        """
        Fetch option chain from Polygon, apply filters, return structured records.

        Args:
            ticker: Underlying ticker symbol (e.g., AAPL)
            date: Evaluation date (YYYY-MM-DD)
            filters: DataFilters instance with min_dte, max_dte, etc.
            day_count: Day count convention for TTM calculation

        Returns:
            ChainLoadResult with filtered records, spot price, and rejection tracking

        Raises:
            HTTPException: If spot price or contracts cannot be fetched
        """
        logger.info(
            "[ChainLoader] Fetching chain for %s on %s with filters: DTE=[%d, %d], min_OI=%d, max_spread=%.1f%%",
            ticker,
            date,
            filters.min_dte,
            filters.max_dte,
            filters.min_open_interest,
            filters.max_spread_pct * 100,
        )

        spot = self._fetch_spot(ticker, date)
        raw_contracts = self._fetch_contracts(ticker, date)

        records, rejection_reasons = self._apply_filters(
            raw_contracts,
            spot,
            date,
            filters,
            day_count,
        )

        total_quotes = len(raw_contracts)
        accepted = len(records)
        rejected = total_quotes - accepted

        logger.info(
            "[ChainLoader] Chain loaded: spot=%.2f, total=%d, accepted=%d, rejected=%d (%s)",
            spot,
            total_quotes,
            accepted,
            rejected,
            ChainLoadResult(
                records=records,
                spot=spot,
                eval_date=date,
                total_quotes=total_quotes,
                accepted=accepted,
                rejected=rejected,
                rejection_reasons=rejection_reasons,
            ).rejection_summary(),
        )

        return ChainLoadResult(
            records=records,
            spot=spot,
            eval_date=date,
            total_quotes=total_quotes,
            accepted=accepted,
            rejected=rejected,
            rejection_reasons=rejection_reasons,
        )

    def _fetch_spot(self, ticker: str, date: str) -> float:
        """
        Get closing price for ticker on date.

        Args:
            ticker: Underlying ticker symbol
            date: Date (YYYY-MM-DD)

        Returns:
            Closing price as float

        Raises:
            HTTPException: If price cannot be fetched
        """
        try:
            logger.debug(f"[ChainLoader] Fetching spot price for {ticker} on {date}")

            aggs = self.polygon.fetch_aggregates(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                from_date=date,
                to_date=date,
                adjusted=True,
            )

            if not aggs:
                logger.error(f"[ChainLoader] No aggregates found for {ticker} on {date}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No price data found for {ticker} on {date}",
                )

            spot = aggs[0].get("close")
            if not spot or spot <= 0:
                logger.error(f"[ChainLoader] Invalid spot price for {ticker} on {date}: {spot}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid spot price for {ticker} on {date}: {spot}",
                )

            logger.debug(f"[ChainLoader] Spot price: {spot}")
            return float(spot)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[ChainLoader] Error fetching spot price: {e!s}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch spot price for {ticker}: {e!s}",
            )

    def _fetch_contracts(self, ticker: str, date: str) -> list[dict]:
        """
        Fetch all option contracts for ticker from Polygon.

        Args:
            ticker: Underlying ticker symbol
            date: As-of date (YYYY-MM-DD)

        Returns:
            List of raw contract dicts

        Raises:
            HTTPException: If contracts cannot be fetched
        """
        try:
            logger.debug(f"[ChainLoader] Fetching contracts for {ticker} as_of {date}")

            contracts = self.polygon.list_options_contracts(
                underlying_ticker=ticker,
                as_of_date=date,
                limit=1000,
            )

            logger.debug(f"[ChainLoader] Fetched {len(contracts)} raw contracts")
            return contracts

        except Exception as e:
            logger.error(
                f"[ChainLoader] Error fetching contracts for {ticker}: {e!s}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch options contracts for {ticker}: {e!s}",
            )

    def _apply_filters(
        self,
        raw_contracts: list[dict],
        spot: float,
        eval_date: str,
        filters: DataFilters,
        day_count: str,
    ) -> tuple[list[dict], dict[str, int]]:
        """
        Apply quality filters, tracking rejection reasons.

        Filters:
        1. DTE must be in [min_dte, max_dte]
        2. Open interest must be >= min_open_interest
        3. Bid-ask spread as % of mid must be <= max_spread_pct

        Args:
            raw_contracts: Raw contract dicts from Polygon
            spot: Underlying spot price
            eval_date: Evaluation date (YYYY-MM-DD)
            filters: DataFilters with min_dte, max_dte, min_open_interest, max_spread_pct
            day_count: Day count convention

        Returns:
            Tuple of (filtered_records, rejection_reasons_dict)
        """
        records: list[dict] = []
        rejection_reasons: dict[str, int] = {}

        eval_dt = datetime.strptime(eval_date, "%Y-%m-%d")

        for contract in raw_contracts:
            rejection_reason = self._check_contract(contract, eval_dt, spot, filters, day_count)

            if rejection_reason is None:
                record = self._contract_to_record(contract, eval_dt, day_count)
                if record is not None:
                    records.append(record)
            else:
                rejection_reasons[rejection_reason] = rejection_reasons.get(rejection_reason, 0) + 1

        logger.debug(
            f"[ChainLoader] Applied filters: {len(records)} accepted, {sum(rejection_reasons.values())} rejected"
        )

        return records, rejection_reasons

    def _check_contract(
        self,
        contract: dict,
        eval_dt: datetime,
        spot: float,
        filters: DataFilters,
        day_count: str,
    ) -> str | None:
        """
        Check if contract passes all filters.

        Returns:
            None if contract passes, or rejection reason string
        """
        expiration_date = contract.get("expiration_date")
        if not expiration_date:
            return "missing_expiration"

        try:
            datetime.strptime(expiration_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return "invalid_expiration_format"

        dte = self._compute_dte(expiration_date, eval_dt.strftime("%Y-%m-%d"))

        if dte < filters.min_dte:
            return f"dte_below_min_{filters.min_dte}"
        if dte > filters.max_dte:
            return f"dte_above_max_{filters.max_dte}"

        open_interest = contract.get("open_interest")
        if open_interest is None or open_interest < filters.min_open_interest:
            return f"oi_below_min_{filters.min_open_interest}"

        return None

    def _contract_to_record(
        self,
        contract: dict,
        eval_dt: datetime,
        day_count: str,
    ) -> dict | None:
        """
        Convert raw contract to record format expected by VolSurfaceBuilder.

        Format:
        {
            "strike": float,
            "ttm": float,  # computed from expiration_date and eval_date
            "option_price": float,  # mid price = (bid + ask) / 2
            "is_call": bool,
            "bid": float,
            "ask": float,
            "open_interest": int,
            "volume": int,
        }

        Args:
            contract: Raw contract dict from Polygon
            eval_dt: Evaluation datetime
            day_count: Day count convention

        Returns:
            Record dict, or None if conversion fails (will be skipped)
        """
        try:
            contract_type = contract.get("contract_type", "").lower()
            is_call = contract_type == "call"

            strike = contract.get("strike_price")
            expiration_date = contract.get("expiration_date")

            if strike is None or strike <= 0:
                logger.debug(f"[ChainLoader] Skipping contract with invalid strike: {strike}")
                return None

            if not expiration_date:
                logger.debug("[ChainLoader] Skipping contract with missing expiration")
                return None

            dte = self._compute_dte(expiration_date, eval_dt.strftime("%Y-%m-%d"))
            ttm = dte_to_ttm(dte, day_count=day_count)

            bid = contract.get("bid", 0.0)
            ask = contract.get("ask", 0.0)

            if bid is None:
                bid = 0.0
            if ask is None:
                ask = 0.0

            bid = float(bid) if bid else 0.0
            ask = float(ask) if ask else 0.0

            mid_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0

            if mid_price <= 0:
                logger.debug(f"[ChainLoader] Skipping contract with invalid pricing: bid={bid}, ask={ask}")
                return None

            open_interest = contract.get("open_interest", 0)
            if open_interest is None:
                open_interest = 0

            volume = contract.get("volume", 0)
            if volume is None:
                volume = 0

            return {
                "strike": float(strike),
                "ttm": ttm,
                "option_price": mid_price,
                "is_call": is_call,
                "bid": bid,
                "ask": ask,
                "open_interest": int(open_interest),
                "volume": int(volume),
            }

        except Exception as e:
            logger.debug(f"[ChainLoader] Error converting contract to record: {e!s}, contract={contract}")
            return None

    def _compute_dte(self, expiration_date: str, eval_date: str) -> int:
        """
        Calendar days between eval_date and expiration_date.

        Args:
            expiration_date: Expiration date (YYYY-MM-DD)
            eval_date: Evaluation date (YYYY-MM-DD)

        Returns:
            DTE as positive integer (days between eval_date and expiration_date)
        """
        try:
            exp_dt = datetime.strptime(expiration_date, "%Y-%m-%d")
            eval_dt = datetime.strptime(eval_date, "%Y-%m-%d")
            delta = exp_dt - eval_dt
            dte = delta.days
            return max(dte, 0)
        except (ValueError, TypeError) as e:
            logger.warning(f"[ChainLoader] Error computing DTE for {expiration_date} vs {eval_date}: {e}")
            return 0
