"""Wrapper around Polygon.io REST client with error handling"""
from polygon import RESTClient
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime

from app.config import settings

logger = logging.getLogger(__name__)


class PolygonClientService:
    """Wrapper around Polygon.io REST client with error handling"""

    def __init__(self):
        self.client = RESTClient(api_key=settings.POLYGON_API_KEY)

    def fetch_aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        limit: int = 50000
    ) -> List[Dict[str, Any]]:
        """Fetch aggregate bars (OHLCV) from Polygon"""
        try:
            logger.info(f"Fetching aggregates for {ticker}: {from_date} to {to_date}")

            aggs = []
            for agg in self.client.list_aggs(
                ticker=ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_=from_date,
                to=to_date,
                limit=limit
            ):
                # Convert to dict for serialization
                aggs.append({
                    'timestamp': agg.timestamp,
                    'open': agg.open,
                    'high': agg.high,
                    'low': agg.low,
                    'close': agg.close,
                    'volume': agg.volume,
                    'vwap': agg.vwap if hasattr(agg, 'vwap') else None,
                    'transactions': agg.transactions if hasattr(agg, 'transactions') else None,
                })

            logger.info(f"Fetched {len(aggs)} aggregates for {ticker}")
            return aggs

        except Exception as e:
            logger.error(f"Error fetching aggregates for {ticker}: {str(e)}")
            raise

    def fetch_trades(
        self,
        ticker: str,
        timestamp: Optional[str] = None,
        limit: int = 50000
    ) -> List[Dict[str, Any]]:
        """Fetch real-time trades from Polygon"""
        try:
            logger.info(f"Fetching trades for {ticker}")

            trades = []
            for trade in self.client.list_trades(
                ticker=ticker,
                timestamp=timestamp,
                limit=limit
            ):
                trades.append({
                    'timestamp': trade.sip_timestamp if hasattr(trade, 'sip_timestamp') else trade.timestamp,
                    'price': trade.price,
                    'size': trade.size,
                    'exchange': trade.exchange if hasattr(trade, 'exchange') else None,
                    'conditions': trade.conditions if hasattr(trade, 'conditions') else None,
                    'sequence_number': trade.sequence_number if hasattr(trade, 'sequence_number') else None,
                    'trade_id': trade.id if hasattr(trade, 'id') else None,
                })

            logger.info(f"Fetched {len(trades)} trades for {ticker}")
            return trades

        except Exception as e:
            logger.error(f"Error fetching trades for {ticker}: {str(e)}")
            raise

    def list_options_contracts(
        self,
        underlying_ticker: str,
        as_of_date: Optional[str] = None,
        contract_type: Optional[str] = None,
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        expiration_date: Optional[str] = None,
        expiration_date_gte: Optional[str] = None,
        expiration_date_lte: Optional[str] = None,
        expired: Optional[bool] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """List options contracts from Polygon for a given underlying ticker"""
        try:
            logger.info(f"Listing options contracts for {underlying_ticker}, as_of={as_of_date}")

            contracts = []
            for c in self.client.list_options_contracts(
                underlying_ticker=underlying_ticker,
                as_of=as_of_date,
                contract_type=contract_type,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                expiration_date=expiration_date,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                expired=expired,
                limit=limit,
            ):
                contracts.append({
                    'ticker': c.ticker,
                    'underlying_ticker': c.underlying_ticker,
                    'contract_type': c.contract_type,
                    'strike_price': c.strike_price,
                    'expiration_date': c.expiration_date,
                    'exercise_style': getattr(c, 'exercise_style', None),
                    'shares_per_contract': getattr(c, 'shares_per_contract', None),
                    'primary_exchange': getattr(c, 'primary_exchange', None),
                })

            logger.info(f"Found {len(contracts)} options contracts for {underlying_ticker}")
            return contracts

        except Exception as e:
            logger.error(f"Error listing options contracts for {underlying_ticker}: {str(e)}")
            raise

    def list_snapshot_options_chain(
        self,
        underlying_asset: str,
        expiration_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch snapshot of options chain for an underlying asset.

        Args:
            underlying_asset: Ticker symbol (e.g., AAPL)
            expiration_date: Filter to only this expiration date (YYYY-MM-DD).
                             Defaults to today if not specified.
        """
        try:
            # Default to today's date to avoid fetching thousands of contracts
            if expiration_date is None:
                expiration_date = datetime.now().strftime('%Y-%m-%d')

            logger.info(f"Fetching options chain snapshot for {underlying_asset}, expiration={expiration_date}")

            contracts = []
            underlying_info = None

            params: Dict[str, Any] = {}
            if expiration_date:
                params['expiration_date'] = expiration_date

            for snapshot in self.client.list_snapshot_options_chain(
                underlying_asset=underlying_asset,
                params=params if params else None,
            ):
                # Capture underlying asset info from first result
                if underlying_info is None and hasattr(snapshot, 'underlying_asset'):
                    ua = snapshot.underlying_asset
                    underlying_info = {
                        'ticker': getattr(ua, 'ticker', None) or underlying_asset,
                        'price': getattr(ua, 'price', None) or 0,
                        'change': getattr(ua, 'change_to_break_even', None) or 0,
                        'change_percent': getattr(ua, 'change_to_break_even', None) or 0,
                    }

                greeks = getattr(snapshot, 'greeks', None)
                day = getattr(snapshot, 'day', None)
                details = getattr(snapshot, 'details', None)

                contract = {
                    'ticker': getattr(details, 'ticker', None) if details else None,
                    'contract_type': getattr(details, 'contract_type', None) if details else None,
                    'strike_price': getattr(details, 'strike_price', None) if details else None,
                    'expiration_date': getattr(details, 'expiration_date', None) if details else None,
                    'break_even_price': getattr(snapshot, 'break_even_price', None),
                    'implied_volatility': getattr(snapshot, 'implied_volatility', None),
                    'open_interest': getattr(snapshot, 'open_interest', None),
                    'greeks': {
                        'delta': getattr(greeks, 'delta', None),
                        'gamma': getattr(greeks, 'gamma', None),
                        'theta': getattr(greeks, 'theta', None),
                        'vega': getattr(greeks, 'vega', None),
                    } if greeks else None,
                    'day': {
                        'open': getattr(day, 'open', None),
                        'high': getattr(day, 'high', None),
                        'low': getattr(day, 'low', None),
                        'close': getattr(day, 'close', None),
                        'volume': getattr(day, 'volume', None),
                        'vwap': getattr(day, 'vwap', None),
                    } if day else None,
                }
                contracts.append(contract)

            if underlying_info is None:
                underlying_info = {'ticker': underlying_asset, 'price': 0, 'change': 0, 'change_percent': 0}

            logger.info(f"Fetched {len(contracts)} options chain snapshots for {underlying_asset}")
            return {
                'underlying': underlying_info,
                'contracts': contracts,
            }

        except Exception as e:
            logger.error(f"Error fetching options chain snapshot for {underlying_asset}: {str(e)}")
            raise

    def fetch_technical_indicator(
        self,
        ticker: str,
        indicator_type: str,  # sma, ema, rsi, macd
        timestamp: Optional[str] = None,
        timespan: str = "day",
        window: int = 50,
        **kwargs
    ) -> Dict[str, Any]:
        """Fetch technical indicators from Polygon"""
        try:
            logger.info(f"Fetching {indicator_type.upper()} for {ticker}")

            # Map indicator types to client methods
            indicator_methods = {
                'sma': self.client.get_sma,
                'ema': self.client.get_ema,
                'rsi': self.client.get_rsi,
                'macd': self.client.get_macd,
            }

            if indicator_type.lower() not in indicator_methods:
                raise ValueError(f"Unsupported indicator type: {indicator_type}")

            method = indicator_methods[indicator_type.lower()]

            # Call appropriate method
            result = method(
                ticker=ticker,
                timestamp=timestamp,
                timespan=timespan,
                window=window,
                **kwargs
            )

            # Convert to serializable format
            return {
                'ticker': ticker,
                'indicator_type': indicator_type,
                'timestamp': result.timestamp if hasattr(result, 'timestamp') else None,
                'values': result.values if hasattr(result, 'values') else None,
                'metadata': {
                    'timespan': timespan,
                    'window': window,
                }
            }

        except Exception as e:
            logger.error(f"Error fetching {indicator_type} for {ticker}: {str(e)}")
            raise
