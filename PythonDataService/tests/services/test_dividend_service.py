"""Tests for trailing-12-month dividend-yield service (Step 2 of IV-RV alignment)."""

from __future__ import annotations

import pytest

from app.services.dividend_service import (
    clear_cache,
    compute_dividend_yield,
    get_trailing_12m_cash_dividends,
)


class FakePolygon:
    """Minimal stand-in for PolygonClient.list_dividends used by tests."""

    def __init__(self, events: list[dict] | Exception | None = None) -> None:
        self._events = events
        self.calls: list[dict] = []

    def list_dividends(
        self,
        ticker: str,
        ex_dividend_date_gte: str | None = None,
        ex_dividend_date_lte: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        self.calls.append(
            {
                "ticker": ticker,
                "gte": ex_dividend_date_gte,
                "lte": ex_dividend_date_lte,
                "limit": limit,
            }
        )
        if isinstance(self._events, Exception):
            raise self._events
        return self._events or []


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


class TestTrailingTwelveMonth:
    def test_sums_cash_amounts(self):
        polygon = FakePolygon(
            [
                {"cash_amount": 1.62, "ex_dividend_date": "2024-03-15"},
                {"cash_amount": 1.65, "ex_dividend_date": "2024-06-21"},
                {"cash_amount": 1.68, "ex_dividend_date": "2024-09-20"},
                {"cash_amount": 1.74, "ex_dividend_date": "2024-12-20"},
            ]
        )
        total = get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20")
        assert total == pytest.approx(6.69, abs=1e-12)

    def test_window_is_365_days(self):
        polygon = FakePolygon([])
        get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20")
        call = polygon.calls[0]
        assert call["lte"] == "2024-12-20"
        assert call["gte"] == "2023-12-21"  # 2024-12-20 minus 365 days

    def test_zero_for_non_payer(self):
        polygon = FakePolygon([])
        assert get_trailing_12m_cash_dividends("XYZ", polygon, "2024-12-20") == 0.0

    def test_polygon_error_returns_zero(self):
        polygon = FakePolygon(RuntimeError("polygon down"))
        assert get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20") == 0.0

    def test_skips_null_amounts(self):
        polygon = FakePolygon(
            [
                {"cash_amount": None, "ex_dividend_date": "2024-03-15"},
                {"cash_amount": 1.65, "ex_dividend_date": "2024-06-21"},
                {"cash_amount": "not-a-number", "ex_dividend_date": "2024-09-20"},
            ]
        )
        assert get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20") == pytest.approx(1.65)


class TestComputeDividendYield:
    def test_yield_is_ttm_over_spot(self):
        polygon = FakePolygon(
            [
                {"cash_amount": 1.62, "ex_dividend_date": "2024-03-15"},
                {"cash_amount": 1.65, "ex_dividend_date": "2024-06-21"},
                {"cash_amount": 1.68, "ex_dividend_date": "2024-09-20"},
                {"cash_amount": 1.74, "ex_dividend_date": "2024-12-20"},
            ]
        )
        # SPY ~ $590 on 2024-12-20 → q ≈ 6.69 / 590 ≈ 0.01134
        yld = compute_dividend_yield("SPY", spot_price=590.0, polygon=polygon, observation_date="2024-12-20")
        assert yld == pytest.approx(6.69 / 590.0, abs=1e-12)

    def test_yield_is_cached(self):
        polygon = FakePolygon([{"cash_amount": 2.0, "ex_dividend_date": "2024-06-01"}])
        compute_dividend_yield("SPY", 100.0, polygon, "2024-12-20")
        compute_dividend_yield("SPY", 100.0, polygon, "2024-12-20")
        # Cache hit on second call → exactly one polygon call.
        assert len(polygon.calls) == 1

    def test_cache_keyed_by_date_and_symbol(self):
        polygon = FakePolygon([{"cash_amount": 2.0, "ex_dividend_date": "2024-06-01"}])
        compute_dividend_yield("SPY", 100.0, polygon, "2024-12-20")
        compute_dividend_yield("SPY", 100.0, polygon, "2024-12-19")
        compute_dividend_yield("QQQ", 100.0, polygon, "2024-12-20")
        # Different (symbol, date) → three polygon calls.
        assert len(polygon.calls) == 3

    def test_negative_spot_raises(self):
        polygon = FakePolygon([])
        with pytest.raises(ValueError):
            compute_dividend_yield("SPY", -1.0, polygon, "2024-12-20")

    def test_zero_spot_raises(self):
        polygon = FakePolygon([])
        with pytest.raises(ValueError):
            compute_dividend_yield("SPY", 0.0, polygon, "2024-12-20")

    def test_ticker_case_insensitive_in_cache(self):
        polygon = FakePolygon([{"cash_amount": 2.0, "ex_dividend_date": "2024-06-01"}])
        compute_dividend_yield("spy", 100.0, polygon, "2024-12-20")
        compute_dividend_yield("SPY", 100.0, polygon, "2024-12-20")
        assert len(polygon.calls) == 1
