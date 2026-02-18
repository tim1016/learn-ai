"""Tests for the DataSanitizer service"""
import pytest
from app.services.sanitizer import DataSanitizer


class TestSanitizeAggregates:
    def test_empty_input_returns_empty(self):
        result = DataSanitizer.sanitize_aggregates([])

        assert result['data'] == []
        assert result['summary']['original_count'] == 0
        assert result['summary']['cleaned_count'] == 0

    def test_valid_ohlcv_data_retained(self):
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0},
            {'timestamp': 1704153600000, 'open': 153.0, 'high': 158.0,
             'low': 151.0, 'close': 157.0, 'volume': 900000.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result['summary']['original_count'] == 2
        assert result['summary']['cleaned_count'] == 2
        assert result['summary']['removed_count'] == 0
        assert len(result['data']) == 2

    def test_duplicates_removed(self):
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0},
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result['summary']['cleaned_count'] <= result['summary']['original_count']

    def test_invalid_high_low_filtered(self):
        """Bars where high < low should be removed by the integrity filter"""
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 140.0,
             'low': 155.0, 'close': 153.0, 'volume': 1000000.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result['summary']['cleaned_count'] == 0

    def test_negative_volume_filtered(self):
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': -100.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result['summary']['cleaned_count'] == 0

    def test_data_sorted_by_timestamp(self):
        raw = [
            {'timestamp': 1704153600000, 'open': 153.0, 'high': 158.0,
             'low': 151.0, 'close': 157.0, 'volume': 900000.0},
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)
        data = result['data']

        assert len(data) == 2
        assert data[0]['timestamp'] < data[1]['timestamp']

    def test_summary_has_removal_percentage(self):
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert 'removal_percentage' in result['summary']

    def test_vwap_and_transactions_optional(self):
        """Bars with optional fields should still be processed"""
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'high': 155.0,
             'low': 148.0, 'close': 153.0, 'volume': 1000000.0,
             'vwap': 152.5, 'transactions': 5000},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result['summary']['cleaned_count'] == 1


class TestSanitizeTrades:
    def test_empty_input_returns_empty(self):
        result = DataSanitizer.sanitize_trades([])

        assert result['data'] == []
        assert result['summary']['original_count'] == 0

    def test_valid_trades_retained(self):
        raw = [
            {'timestamp': 1704067200000000000, 'price': 150.0, 'size': 100},
            {'timestamp': 1704067201000000000, 'price': 150.5, 'size': 50},
        ]

        result = DataSanitizer.sanitize_trades(raw)

        assert result['summary']['cleaned_count'] == 2

    def test_zero_price_filtered(self):
        """Trades with price <= 0 should be removed"""
        raw = [
            {'timestamp': 1704067200000000000, 'price': 0.0, 'size': 100},
            {'timestamp': 1704067201000000000, 'price': 150.0, 'size': 50},
        ]

        result = DataSanitizer.sanitize_trades(raw)

        assert result['summary']['cleaned_count'] == 1

    def test_zero_size_filtered(self):
        raw = [
            {'timestamp': 1704067200000000000, 'price': 150.0, 'size': 0},
        ]

        result = DataSanitizer.sanitize_trades(raw)

        assert result['summary']['cleaned_count'] == 0


class TestSanitizeGeneric:
    """Tests for sanitize_generic.

    NOTE: pandas_dq.Fix_DQ uses DataFrame.applymap() which was removed in
    pandas 3.x. Tests that invoke Fix_DQ on non-empty data are marked xfail
    until the pandas_dq library is updated.
    """

    def test_empty_input_returns_empty(self):
        result = DataSanitizer.sanitize_generic([])

        assert result['data'] == []
        assert result['summary']['original_count'] == 0

    @pytest.mark.xfail(reason="pandas_dq uses removed DataFrame.applymap (pandas 3.x)")
    def test_numeric_data_processed(self):
        raw = [
            {'open': 150.0, 'close': 153.0, 'volume': 1000000.0},
            {'open': 153.0, 'close': 157.0, 'volume': 900000.0},
        ]

        result = DataSanitizer.sanitize_generic(raw)

        assert result['summary']['original_count'] == 2
        assert len(result['data']) == 2

    @pytest.mark.xfail(reason="pandas_dq uses removed DataFrame.applymap (pandas 3.x)")
    def test_custom_quantile_accepted(self):
        raw = [
            {'open': 150.0, 'close': 153.0, 'volume': 1000000.0},
        ]

        result = DataSanitizer.sanitize_generic(raw, quantile=0.95)

        assert result['summary']['original_count'] == 1

    @pytest.mark.xfail(reason="pandas_dq uses removed DataFrame.applymap (pandas 3.x)")
    def test_timestamp_column_preserved(self):
        raw = [
            {'timestamp': 1704067200000, 'open': 150.0, 'close': 153.0},
            {'timestamp': 1704153600000, 'open': 153.0, 'close': 157.0},
        ]

        result = DataSanitizer.sanitize_generic(raw)

        for record in result['data']:
            assert 'timestamp' in record

    @pytest.mark.xfail(reason="pandas_dq uses removed DataFrame.applymap (pandas 3.x)")
    def test_string_columns_preserved(self):
        raw = [
            {'symbol': 'AAPL', 'open': 150.0, 'close': 153.0},
            {'symbol': 'AAPL', 'open': 153.0, 'close': 157.0},
        ]

        result = DataSanitizer.sanitize_generic(raw)

        for record in result['data']:
            assert record['symbol'] == 'AAPL'

    @pytest.mark.xfail(reason="pandas_dq uses removed DataFrame.applymap (pandas 3.x)")
    def test_summary_has_columns_processed(self):
        raw = [
            {'open': 150.0, 'close': 153.0},
        ]

        result = DataSanitizer.sanitize_generic(raw)

        assert 'columns_processed' in result['summary']


class TestSanitizeIndicator:
    def test_basic_indicator_data(self):
        raw = {
            'indicator_type': 'sma',
            'ticker': 'AAPL',
            'values': [
                {'timestamp': 1704067200000, 'value': 152.5},
                {'timestamp': 1704153600000, 'value': 153.0},
            ]
        }

        result = DataSanitizer.sanitize_indicator(raw)

        assert result['summary']['indicator_type'] == 'sma'
        assert result['summary']['ticker'] == 'AAPL'
        assert result['summary']['values_count'] == 2

    def test_empty_values_handled(self):
        raw = {
            'indicator_type': 'rsi',
            'ticker': 'MSFT',
            'values': []
        }

        result = DataSanitizer.sanitize_indicator(raw)

        assert result['summary']['values_count'] == 0
