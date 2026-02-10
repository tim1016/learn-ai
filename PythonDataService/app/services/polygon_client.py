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
