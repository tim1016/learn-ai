"""Tests for the (r, q) facade (Step 2 of IV-RV alignment)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import dividend_service, fred_service, rate_dividend_service
from app.services.fred_service import RATE_SOURCE_DEFAULT
from app.services.rate_dividend_service import RateAndDividend, get_rate_and_dividend
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE


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
        monkeypatch.setattr(
            rate_dividend_service, "get_risk_free_rate_and_source", lambda dte_days, observation_date: (0.0512, "FRED")
        )
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
            return 0.05, "FRED"

        monkeypatch.setattr(rate_dividend_service, "get_risk_free_rate_and_source", fake_rate)
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
        monkeypatch.setattr(
            rate_dividend_service, "get_risk_free_rate_and_source", lambda dte_days, observation_date: (0.05, "FRED")
        )
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

    def test_a_failed_dividend_read_leaves_q_unset(self, monkeypatch):
        """A Polygon outage is not a non-payer: q is unset, never 0 (#2764)."""
        monkeypatch.setattr(
            rate_dividend_service, "get_risk_free_rate_and_source", lambda dte_days, observation_date: (0.05, "FRED")
        )

        class DownPolygon(FakePolygon):
            def list_dividends(self, *args: object, **kwargs: object) -> list[dict]:
                raise RuntimeError("polygon down")

        out = get_rate_and_dividend(
            ticker="SPY", spot_price=590.0, polygon=DownPolygon([]), dte_days=30, observation_date="2024-12-20"
        )

        assert out == RateAndDividend(rate=0.05, dividend_yield=None, source_rate="FRED", source_dividend=None)

    def test_rate_resolves_when_the_dividend_lookup_cannot_run(self):
        """With no spot the rate still resolves, and a FRED fallback is labelled as the default (#2764)."""
        with patch.object(fred_service, "_fetch_all_tenors", return_value={}):
            out = get_rate_and_dividend(
                ticker="SPY",
                spot_price=0.0,
                polygon=FakePolygon([]),
                dte_days=30,
                observation_date="2024-12-20",
            )

        assert out == RateAndDividend(
            rate=DEFAULT_RISK_FREE_RATE,
            dividend_yield=None,
            source_rate=RATE_SOURCE_DEFAULT,
            source_dividend=None,
        )
