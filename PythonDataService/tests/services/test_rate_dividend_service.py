"""Tests for the (r, q) facade (Step 2 of IV-RV alignment)."""

from __future__ import annotations

import pytest

from app.services import dividend_service, fred_service, rate_dividend_service
from app.services.rate_dividend_service import RateAndDividend, get_rate_and_dividend


class FakePolygon:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def list_dividends(
        self,
        ticker: str,
        ex_dividend_date_gte: str | None = None,
        ex_dividend_date_lte: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        return self._events


@pytest.fixture(autouse=True)
def _clear_caches():
    dividend_service.clear_cache()
    fred_service.clear_cache()
    yield
    dividend_service.clear_cache()
    fred_service.clear_cache()


class TestRateAndDividendFacade:
    def test_composes_fred_and_dividend(self, monkeypatch):
        monkeypatch.setattr(rate_dividend_service, "get_risk_free_rate", lambda dte_days, observation_date: 0.0512)
        polygon = FakePolygon(
            [
                {"cash_amount": 1.62, "ex_dividend_date": "2024-03-15"},
                {"cash_amount": 1.65, "ex_dividend_date": "2024-06-21"},
                {"cash_amount": 1.68, "ex_dividend_date": "2024-09-20"},
                {"cash_amount": 1.74, "ex_dividend_date": "2024-12-20"},
            ]
        )
        out = get_rate_and_dividend(
            ticker="SPY",
            spot_price=590.0,
            polygon=polygon,
            dte_days=30,
            observation_date="2024-12-20",
        )
        assert isinstance(out, RateAndDividend)
        assert out.rate == pytest.approx(0.0512)
        assert out.dividend_yield == pytest.approx(6.69 / 590.0, abs=1e-12)
        assert out.source_rate == "FRED"
        assert out.source_dividend == "Polygon TTM"

    def test_passes_dte_to_fred(self, monkeypatch):
        captured: dict = {}

        def fake_rate(dte_days, observation_date):
            captured["dte_days"] = dte_days
            captured["observation_date"] = observation_date
            return 0.05

        monkeypatch.setattr(rate_dividend_service, "get_risk_free_rate", fake_rate)
        polygon = FakePolygon([])
        get_rate_and_dividend(
            ticker="SPY",
            spot_price=590.0,
            polygon=polygon,
            dte_days=60,
            observation_date="2024-12-20",
        )
        assert captured == {"dte_days": 60, "observation_date": "2024-12-20"}

    def test_non_payer_returns_zero_yield(self, monkeypatch):
        monkeypatch.setattr(rate_dividend_service, "get_risk_free_rate", lambda dte_days, observation_date: 0.05)
        polygon = FakePolygon([])
        out = get_rate_and_dividend(
            ticker="TSLA",
            spot_price=400.0,
            polygon=polygon,
            dte_days=30,
            observation_date="2024-12-20",
        )
        assert out.dividend_yield == 0.0
        assert out.rate == 0.05
