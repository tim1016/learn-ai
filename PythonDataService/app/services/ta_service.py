"""Technical analysis service using pandas-ta"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta
import logging
from typing import List, Dict, Any, Optional

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
            elif name == 'stoch':
                result = TechnicalAnalysisService._calc_stoch(df, window)
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
    def _calc_stoch(df: pd.DataFrame, window: int) -> List[Dict]:
        stoch_df = ta.stoch(df['high'], df['low'], df['close'], k=window, d=3, smooth_k=1)
        if stoch_df is None or stoch_df.empty:
            return []
        cols = stoch_df.columns.tolist()
        points = []
        for i in range(len(df)):
            ts = int(df['timestamp'].iloc[i])
            k_val = stoch_df[cols[0]].iloc[i]
            d_val = stoch_df[cols[1]].iloc[i]
            if pd.notna(k_val):
                points.append({
                    'timestamp': ts,
                    'value': round(float(k_val), 6),
                    'signal': round(float(d_val), 6) if pd.notna(d_val) else None,
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

    # ------------------------------------------------------------------
    # Full TradingView-style indicator table generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_indicator_table(
        bars: List[Dict[str, Any]],
        ema_periods: Optional[List[int]] = None,
        bb_length: int = 20,
        bb_std: float = 2.0,
        supertrend_length: int = 10,
        supertrend_multiplier: float = 3.0,
        rsi_length: int = 14,
        rsi_ma_length: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        adx_length: int = 14,
    ) -> List[Dict[str, Any]]:
        """
        Generate a full indicator table matching TradingView CSV export format.

        Returns a list of row dicts with columns:
          time, open, high, low, close, volume,
          bb_basis, bb_upper, bb_lower,
          supertrend_up, supertrend_down,
          ema_5, ema_10, ... (for each period),
          rsi, rsi_ma,
          macd, macd_signal, macd_histogram,
          adx
        """
        if ema_periods is None:
            ema_periods = [5, 10, 20, 30, 40, 50, 100, 200]

        df = pd.DataFrame(bars)
        df = df.sort_values('timestamp').reset_index(drop=True)

        result_df = pd.DataFrame()
        result_df['time'] = df['timestamp']
        result_df['open'] = df['open']
        result_df['high'] = df['high']
        result_df['low'] = df['low']
        result_df['close'] = df['close']
        result_df['volume'] = df['volume']

        # Bollinger Bands
        bb = ta.bbands(df['close'], length=bb_length, std=bb_std)
        if bb is not None and not bb.empty:
            cols = bb.columns.tolist()
            result_df['bb_lower'] = bb[cols[0]]
            result_df['bb_basis'] = bb[cols[1]]
            result_df['bb_upper'] = bb[cols[2]]
        else:
            result_df['bb_basis'] = None
            result_df['bb_upper'] = None
            result_df['bb_lower'] = None

        # Supertrend
        st = ta.supertrend(
            df['high'], df['low'], df['close'],
            length=supertrend_length, multiplier=supertrend_multiplier,
        )
        if st is not None and not st.empty:
            cols = st.columns.tolist()
            # pandas-ta supertrend returns: SUPERT, SUPERTd, SUPERTl, SUPERTs
            # SUPERTl = long (support/up), SUPERTs = short (resistance/down)
            st_trend = st[cols[0]]    # trend line value
            st_dir = st[cols[1]]      # direction: 1 = up, -1 = down
            st_long = st[cols[2]]     # support (up trend)
            st_short = st[cols[3]]    # resistance (down trend)

            # TradingView shows Up Trend when bullish, Down Trend when bearish
            result_df['supertrend_up'] = st_long.where(st_dir == 1)
            result_df['supertrend_down'] = st_short.where(st_dir == -1)
        else:
            result_df['supertrend_up'] = None
            result_df['supertrend_down'] = None

        # EMAs
        for period in sorted(ema_periods):
            ema = ta.ema(df['close'], length=period)
            result_df[f'ema_{period}'] = ema

        # RSI + RSI-based MA
        rsi = ta.rsi(df['close'], length=rsi_length)
        result_df['rsi'] = rsi
        if rsi is not None:
            rsi_ma = ta.sma(rsi, length=rsi_ma_length)
            result_df['rsi_ma'] = rsi_ma
        else:
            result_df['rsi_ma'] = None

        # MACD
        macd_df = ta.macd(df['close'], fast=macd_fast, slow=macd_slow, signal=macd_signal)
        if macd_df is not None and not macd_df.empty:
            cols = macd_df.columns.tolist()
            result_df['macd'] = macd_df[cols[0]]
            result_df['macd_histogram'] = macd_df[cols[1]]
            result_df['macd_signal'] = macd_df[cols[2]]
        else:
            result_df['macd'] = None
            result_df['macd_histogram'] = None
            result_df['macd_signal'] = None

        # ADX (tvmode=True matches TradingView's Pine Script implementation)
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=adx_length, tvmode=True)
        if adx_df is not None and not adx_df.empty:
            # First column is ADX, rest are DI+ and DI-
            result_df['adx'] = adx_df.iloc[:, 0]
        else:
            result_df['adx'] = None

        # Convert to list of dicts, replacing NaN with None for JSON serialization
        raw_rows = result_df.to_dict(orient='records')
        rows = [
            {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
            for row in raw_rows
        ]

        logger.info(
            f"Generated indicator table: {len(rows)} rows, "
            f"EMAs={ema_periods}, BB({bb_length},{bb_std}), "
            f"ST({supertrend_length},{supertrend_multiplier}), "
            f"RSI({rsi_length}), MACD({macd_fast},{macd_slow},{macd_signal}), "
            f"ADX({adx_length})"
        )

        return rows
