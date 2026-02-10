"""Data sanitization using pandas"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime

from app.config import settings

logger = logging.getLogger(__name__)


class DataSanitizer:
    """Sanitize market data using pandas"""

    @staticmethod
    def sanitize_aggregates(raw_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Clean and validate OHLCV aggregate data"""
        try:
            if not raw_data:
                return {'data': [], 'summary': {'original_count': 0, 'cleaned_count': 0}}

            # Convert to DataFrame
            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} aggregate records")

            # Convert timestamp to datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Remove duplicates
            if settings.REMOVE_DUPLICATES:
                df = df.drop_duplicates(subset=['timestamp'])

            # Sort by timestamp
            df = df.sort_values('timestamp')

            # Validate OHLCV data integrity
            # High should be >= Open, Close, Low
            # Low should be <= Open, Close, High
            df = df[
                (df['high'] >= df['open']) &
                (df['high'] >= df['close']) &
                (df['high'] >= df['low']) &
                (df['low'] <= df['open']) &
                (df['low'] <= df['close']) &
                (df['low'] <= df['high']) &
                (df['volume'] >= 0)
            ]

            # Remove rows with excessive nulls
            null_threshold = settings.MAX_NULL_PERCENTAGE
            df = df.dropna(thresh=len(df.columns) * (1 - null_threshold))

            # Fill remaining nulls for optional fields
            if 'vwap' in df.columns:
                df['vwap'] = df['vwap'].ffill()
            if 'transactions' in df.columns:
                df['transactions'] = df['transactions'].fillna(0)

            # Convert timestamp back to ISO format for JSON serialization
            df['timestamp'] = df['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

            cleaned_count = len(df)
            removed_count = original_count - cleaned_count

            logger.info(f"Sanitization complete: {cleaned_count} records retained, {removed_count} removed")

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
        """Clean and validate trade data"""
        try:
            if not raw_data:
                return {'data': [], 'summary': {'original_count': 0, 'cleaned_count': 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} trade records")

            # Convert timestamp
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ns')

            # Remove duplicates
            if settings.REMOVE_DUPLICATES:
                # Use trade_id if available, otherwise timestamp
                if 'trade_id' in df.columns:
                    df = df.drop_duplicates(subset=['timestamp', 'trade_id'])
                else:
                    df = df.drop_duplicates(subset=['timestamp'])

            # Sort by timestamp
            df = df.sort_values('timestamp')

            # Validate data
            df = df[(df['price'] > 0) & (df['size'] > 0)]

            # Remove excessive nulls
            df = df.dropna(thresh=len(df.columns) * (1 - settings.MAX_NULL_PERCENTAGE))

            # Convert timestamp to ISO format
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
        """Clean and validate technical indicator data"""
        try:
            logger.info(f"Sanitizing {raw_data.get('indicator_type')} indicator")

            # Indicators come as single objects with values arrays
            # Validate and clean the values
            if 'values' in raw_data and raw_data['values']:
                values_df = pd.DataFrame(raw_data['values'])

                # Remove nulls
                values_df = values_df.dropna()

                raw_data['values'] = values_df.to_dict('records')

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
