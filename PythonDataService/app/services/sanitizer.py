"""Data sanitization using pandas-dq Fix_DQ"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any
import logging

from pandas_dq import Fix_DQ
from app.config import settings

logger = logging.getLogger(__name__)

# Initialize Fix_DQ at module level for performance (reused across requests)
_fix_dq = Fix_DQ(quantile=0.99, cat_fill_value="missing", num_fill_value="median",
                 rare_threshold=0.01, correlation_threshold=0.9)


class DataSanitizer:
    """Sanitize market data using pandas-dq Fix_DQ"""

    @staticmethod
    def _run_fix_dq(df: pd.DataFrame, target_col: str | None = None) -> pd.DataFrame:
        """Run Fix_DQ on a DataFrame. Returns the cleaned DataFrame."""
        try:
            if target_col and target_col in df.columns:
                target = df[target_col]
                features = df.drop(columns=[target_col])
                cleaned = _fix_dq.fit_transform(features, target)
                cleaned[target_col] = target.loc[cleaned.index]
            else:
                cleaned = _fix_dq.fit_transform(df)
            return cleaned
        except Exception as e:
            logger.warning(f"Fix_DQ encountered an issue: {e}. Returning original data.")
            return df

    @staticmethod
    def sanitize_aggregates(raw_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Clean and validate OHLCV aggregate data using Fix_DQ"""
        try:
            if not raw_data:
                return {'data': [], 'summary': {'original_count': 0, 'cleaned_count': 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} aggregate records with Fix_DQ")

            # Convert timestamp from ms to datetime for proper time-series handling
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Remove duplicates
            if settings.REMOVE_DUPLICATES:
                df = df.drop_duplicates(subset=['timestamp'])

            # Sort by timestamp
            df = df.sort_values('timestamp').reset_index(drop=True)

            # Separate timestamp before Fix_DQ (it works on numeric/categorical data)
            timestamps = df['timestamp']
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            optional_cols = [c for c in ['vwap', 'transactions'] if c in df.columns]
            dq_cols = numeric_cols + optional_cols

            dq_df = df[dq_cols].copy()

            # Run Fix_DQ: handles missing values, outliers (0.99 quantile), type enforcement
            cleaned_dq = DataSanitizer._run_fix_dq(dq_df, target_col='close')

            # Reassemble with timestamps (aligned by index)
            df = cleaned_dq.copy()
            df['timestamp'] = timestamps.loc[df.index]

            # Basic OHLCV integrity filter (keep rows where high >= low and volume >= 0)
            mask = (df['high'] >= df['low']) & (df['volume'] >= 0)
            df = df[mask]

            # Convert timestamp to ISO format for JSON serialization
            df['timestamp'] = df['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

            cleaned_count = len(df)
            removed_count = original_count - cleaned_count

            logger.info(f"Fix_DQ sanitization complete: {cleaned_count} records retained, {removed_count} removed")

            return {
                'data': df.to_dict('records'),
                'summary': {
                    'original_count': original_count,
                    'cleaned_count': cleaned_count,
                    'removed_count': removed_count,
                    'removal_percentage': round((removed_count / original_count) * 100, 2) if original_count > 0 else 0
                }
            }

        except Exception as e:
            logger.error(f"Error sanitizing aggregates: {str(e)}")
            raise

    @staticmethod
    def sanitize_trades(raw_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Clean and validate trade data using Fix_DQ"""
        try:
            if not raw_data:
                return {'data': [], 'summary': {'original_count': 0, 'cleaned_count': 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} trade records with Fix_DQ")

            # Convert timestamp (nanoseconds for trades)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ns')

            # Remove duplicates
            if settings.REMOVE_DUPLICATES:
                if 'trade_id' in df.columns:
                    df = df.drop_duplicates(subset=['timestamp', 'trade_id'])
                else:
                    df = df.drop_duplicates(subset=['timestamp'])

            df = df.sort_values('timestamp').reset_index(drop=True)

            # Run Fix_DQ on numeric trade columns
            timestamps = df['timestamp']
            numeric_cols = [c for c in ['price', 'size'] if c in df.columns]
            dq_df = df[numeric_cols].copy()

            cleaned_dq = DataSanitizer._run_fix_dq(dq_df)

            df = cleaned_dq.copy()
            df['timestamp'] = timestamps.loc[df.index]

            # Restore non-numeric columns that Fix_DQ didn't process
            for col in ['exchange', 'conditions', 'sequence_number', 'trade_id']:
                if col in pd.DataFrame(raw_data).columns:
                    original = pd.DataFrame(raw_data)[col]
                    df[col] = original.loc[df.index].values

            # Basic validity: price > 0, size > 0
            df = df[(df['price'] > 0) & (df['size'] > 0)]

            df['timestamp'] = df['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

            cleaned_count = len(df)

            return {
                'data': df.to_dict('records'),
                'summary': {
                    'original_count': original_count,
                    'cleaned_count': cleaned_count,
                    'removed_count': original_count - cleaned_count,
                }
            }

        except Exception as e:
            logger.error(f"Error sanitizing trades: {str(e)}")
            raise

    @staticmethod
    def sanitize_indicator(raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean and validate technical indicator data using Fix_DQ"""
        try:
            logger.info(f"Sanitizing {raw_data.get('indicator_type')} indicator with Fix_DQ")

            if 'values' in raw_data and raw_data['values']:
                values_df = pd.DataFrame(raw_data['values'])
                cleaned = DataSanitizer._run_fix_dq(values_df)
                raw_data['values'] = cleaned.to_dict('records')

            return {
                'data': raw_data,
                'summary': {
                    'indicator_type': raw_data.get('indicator_type'),
                    'ticker': raw_data.get('ticker'),
                    'values_count': len(raw_data.get('values', []))
                }
            }

        except Exception as e:
            logger.error(f"Error sanitizing indicator: {str(e)}")
            raise

    @staticmethod
    def sanitize_generic(raw_data: List[Dict[str, Any]], quantile: float = 0.99) -> Dict[str, Any]:
        """Sanitize arbitrary market data using Fix_DQ.
        Used by the standalone /api/sanitize endpoint."""
        try:
            if not raw_data:
                return {'data': [], 'summary': {'original_count': 0, 'cleaned_count': 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Generic Fix_DQ sanitization on {original_count} records (quantile={quantile})")

            # Handle timestamp column if present (convert from Unix ms)
            has_timestamp = 'timestamp' in df.columns
            timestamps = None
            if has_timestamp:
                timestamps = pd.to_datetime(df['timestamp'], unit='ms')
                df = df.drop(columns=['timestamp'])

            # Handle symbol/string columns — Fix_DQ works on numeric data
            string_cols = df.select_dtypes(include=['object']).columns.tolist()
            string_data = df[string_cols].copy() if string_cols else None
            if string_cols:
                df = df.drop(columns=string_cols)

            # Create a per-request Fix_DQ if quantile differs from default
            if quantile != 0.99:
                local_fixer = Fix_DQ(quantile=quantile, cat_fill_value="missing",
                                     num_fill_value="median", rare_threshold=0.01,
                                     correlation_threshold=0.9)
                cleaned = local_fixer.fit_transform(df)
            else:
                cleaned = _fix_dq.fit_transform(df)

            # Reassemble
            if string_data is not None:
                for col in string_cols:
                    cleaned[col] = string_data[col].loc[cleaned.index].values

            if has_timestamp and timestamps is not None:
                # Return as Unix ms (long) for C# interop
                cleaned['timestamp'] = timestamps.loc[cleaned.index].astype(np.int64) // 10**6

            cleaned_count = len(cleaned)

            return {
                'data': cleaned.to_dict('records'),
                'summary': {
                    'original_count': original_count,
                    'cleaned_count': cleaned_count,
                    'removed_count': original_count - cleaned_count,
                    'removal_percentage': round(((original_count - cleaned_count) / original_count) * 100, 2) if original_count > 0 else 0,
                    'columns_processed': list(df.columns)
                }
            }

        except Exception as e:
            logger.error(f"Error in generic sanitization: {str(e)}")
            raise
