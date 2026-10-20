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
    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            pytest.param(
                ((1.62, "2024-03-15"), (1.65, "2024-06-21"), (1.68, "2024-09-20"), (1.74, "2024-12-20")),
                6.69,
                id="sums-cash-amounts",
            ),
            pytest.param(
                ((None, "2024-03-15"), (1.65, "2024-06-21"), ("not-a-number", "2024-09-20")),
                1.65,
                id="skips-null-amounts",
            ),
        ],
    )
    def test_total_of_the_cash_amounts_the_events_carry(self, events, expected):
        polygon = FakePolygon([{"cash_amount": amount, "ex_dividend_date": ex_date} for amount, ex_date in events])
        total = get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20")
        assert total == pytest.approx(expected, abs=1e-12), "the total is the sum of the usable cash amounts"

    def test_window_is_365_days(self):
        polygon = FakePolygon([])
        get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20")
        call = polygon.calls[0]
        assert call["lte"] == "2024-12-20"
        assert call["gte"] == "2023-12-21"  # 2024-12-20 minus 365 days

    def test_zero_for_non_payer(self):
        polygon = FakePolygon([])
        assert get_trailing_12m_cash_dividends("XYZ", polygon, "2024-12-20") == 0.0

    def test_polygon_error_is_not_a_non_payer(self):
        polygon = FakePolygon(RuntimeError("polygon down"))
        with pytest.raises(RuntimeError, match="polygon down"):
            get_trailing_12m_cash_dividends("SPY", polygon, "2024-12-20")

    def test_a_failed_lookup_is_not_cached(self):
        with pytest.raises(RuntimeError):
            compute_dividend_yield("SPY", 590.0, FakePolygon(RuntimeError("polygon down")), "2024-12-20")
        recovered = FakePolygon([{"cash_amount": 1.65, "ex_dividend_date": "2024-06-21"}])
        assert compute_dividend_yield("SPY", 590.0, recovered, "2024-12-20") == pytest.approx(1.65 / 590.0, abs=1e-12)


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

    @pytest.mark.parametrize(
        ("lookups", "polygon_calls"),
        [
            # Cache hit on second call → exactly one polygon call.
            pytest.param((("SPY", "2024-12-20"), ("SPY", "2024-12-20")), 1, id="yield-is-cached"),
            # Different (symbol, date) → three polygon calls.
            pytest.param(
                (("SPY", "2024-12-20"), ("SPY", "2024-12-19"), ("QQQ", "2024-12-20")),
                3,
                id="cache-is-keyed-by-date-and-symbol",
            ),
            pytest.param((("spy", "2024-12-20"), ("SPY", "2024-12-20")), 1, id="ticker-is-case-insensitive-in-cache"),
        ],
    )
    def test_yield_lookups_hit_polygon_once_per_symbol_and_date(self, lookups, polygon_calls):
        polygon = FakePolygon([{"cash_amount": 2.0, "ex_dividend_date": "2024-06-01"}])
        for ticker, observation_date in lookups:
            compute_dividend_yield(ticker, 100.0, polygon, observation_date)
        assert len(polygon.calls) == polygon_calls, "polygon is asked once per distinct (symbol, date)"

    @pytest.mark.parametrize("spot", [-1.0, 0.0], ids=["negative-spot-raises", "zero-spot-raises"])
    def test_non_positive_spot_raises(self, spot):
        polygon = FakePolygon([])
        with pytest.raises(ValueError):
            compute_dividend_yield("SPY", spot, polygon, "2024-12-20")
