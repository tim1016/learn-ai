"""Technical analysis service using pandas-ta"""
import pandas as pd
import pandas_ta as ta
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class TechnicalAnalysisService:
    """Calculates technical indicators using pandas-ta"""

    @staticmethod
    def calculate_indicators(
        bars: List[Dict[str, Any]],
        indicators: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Calculate multiple indicators from OHLCV bars.

        Args:
            bars: List of {timestamp, open, high, low, close, volume}
            indicators: List of {name, window}

        Returns:
            List of indicator results with data points
        """
        df = pd.DataFrame(bars)
        df = df.sort_values('timestamp').reset_index(drop=True)

        results = []

        for indicator in indicators:
            name = indicator['name'].lower()
            window = indicator.get('window', 14)

            logger.info(f"Calculating {name.upper()} (window={window}) on {len(df)} bars")

            if name == 'sma':
                result = TechnicalAnalysisService._calc_sma(df, window)
            elif name == 'ema':
                result = TechnicalAnalysisService._calc_ema(df, window)
            elif name == 'rsi':
                result = TechnicalAnalysisService._calc_rsi(df, window)
            elif name == 'macd':
                result = TechnicalAnalysisService._calc_macd(df, window)
            elif name == 'bbands':
                result = TechnicalAnalysisService._calc_bbands(df, window)
            else:
                logger.warning(f"Unknown indicator: {name}")
                continue

            results.append({
                'name': name,
                'window': window,
                'data': result
            })

        return results

    @staticmethod
    def _calc_sma(df: pd.DataFrame, window: int) -> List[Dict]:
        series = ta.sma(df['close'], length=window)
        return TechnicalAnalysisService._series_to_points(df['timestamp'], series)

    @staticmethod
    def _calc_ema(df: pd.DataFrame, window: int) -> List[Dict]:
        series = ta.ema(df['close'], length=window)
        return TechnicalAnalysisService._series_to_points(df['timestamp'], series)

    @staticmethod
    def _calc_rsi(df: pd.DataFrame, window: int) -> List[Dict]:
        series = ta.rsi(df['close'], length=window)
        return TechnicalAnalysisService._series_to_points(df['timestamp'], series)

    @staticmethod
    def _calc_macd(df: pd.DataFrame, window: int) -> List[Dict]:
        macd_df = ta.macd(df['close'], fast=12, slow=window, signal=9)
        cols = macd_df.columns.tolist()
        points = []
        for i in range(len(df)):
            ts = int(df['timestamp'].iloc[i])
            val = macd_df[cols[0]].iloc[i]
            hist = macd_df[cols[1]].iloc[i]
            sig = macd_df[cols[2]].iloc[i]
            if pd.notna(val):
                points.append({
                    'timestamp': ts,
                    'value': round(float(val), 6),
                    'signal': round(float(sig), 6) if pd.notna(sig) else None,
                    'histogram': round(float(hist), 6) if pd.notna(hist) else None,
                })
        return points

    @staticmethod
    def _calc_bbands(df: pd.DataFrame, window: int) -> List[Dict]:
        bb_df = ta.bbands(df['close'], length=window)
        cols = bb_df.columns.tolist()
        points = []
        for i in range(len(df)):
            ts = int(df['timestamp'].iloc[i])
            lower = bb_df[cols[0]].iloc[i]
            mid = bb_df[cols[1]].iloc[i]
            upper = bb_df[cols[2]].iloc[i]
            if pd.notna(mid):
                points.append({
                    'timestamp': ts,
                    'value': round(float(mid), 6),
                    'upper': round(float(upper), 6) if pd.notna(upper) else None,
                    'lower': round(float(lower), 6) if pd.notna(lower) else None,
                })
        return points

    @staticmethod
    def _series_to_points(timestamps: pd.Series, values: pd.Series) -> List[Dict]:
        """Convert a pandas Series to a list of {timestamp, value} dicts, skipping NaN."""
        points = []
        for i in range(len(timestamps)):
            if pd.notna(values.iloc[i]):
                points.append({
                    'timestamp': int(timestamps.iloc[i]),
                    'value': round(float(values.iloc[i]), 6)
                })
        return points
