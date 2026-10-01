"""Tests for options chain snapshot endpoint"""

from datetime import date
from unittest.mock import patch

import pytest

from app.services import fred_service
from app.utils.session_anchors import et_midnight_ms


@pytest.mark.anyio
async def test_snapshot_returns_contracts(client):
    """Snapshot endpoint should return formatted options chain data"""
    mock_result = {
        "underlying": {
            "ticker": "AAPL",
            "price": 185.50,
            "change": 2.30,
            "change_percent": 1.25,
        },
        "contracts": [
            {
                "ticker": "O:AAPL250221C00185000",
                "contract_type": "call",
                "strike_price": 185.0,
                "expiration_date": "2025-02-21",
                "break_even_price": 187.50,
                "implied_volatility": 0.25,
                "open_interest": 1500,
                "greeks": {
                    "delta": 0.52,
                    "gamma": 0.03,
                    "theta": -0.15,
                    "vega": 0.20,
                },
                "day": {
                    "open": 3.00,
                    "high": 3.50,
                    "low": 2.80,
                    "close": 3.20,
                    "volume": 5000,
                    "vwap": 3.10,
                },
            },
            {
                "ticker": "O:AAPL250221P00185000",
                "contract_type": "put",
                "strike_price": 185.0,
                "expiration_date": "2025-02-21",
                "break_even_price": 182.50,
                "implied_volatility": 0.28,
                "open_interest": 1200,
                "greeks": {
                    "delta": -0.48,
                    "gamma": 0.03,
                    "theta": -0.12,
                    "vega": 0.19,
                },
                "day": None,
            },
        ],
    }

    with patch(
        "app.routers.snapshot.polygon_client.list_snapshot_options_chain",
        return_value=mock_result,
    ):
        response = await client.post(
            "/api/snapshot/options-chain",
            json={"underlying_ticker": "AAPL"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["count"] == 2
    assert data["underlying"]["ticker"] == "AAPL"
    assert data["underlying"]["price"] == 185.50

    call_contract = data["contracts"][0]
    assert call_contract["contract_type"] == "call"
    assert call_contract["strike_price"] == 185.0
    assert call_contract["implied_volatility"] == 0.25
    assert call_contract["greeks"]["delta"] == 0.52
    assert call_contract["day"]["close"] == 3.20

    put_contract = data["contracts"][1]
    assert put_contract["contract_type"] == "put"
    assert put_contract["day"] is None


@pytest.mark.anyio
async def test_snapshot_empty_ticker_returns_422(client):
    """Empty ticker should fail validation"""
    response = await client.post(
        "/api/snapshot/options-chain",
        json={"underlying_ticker": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_snapshot_handles_polygon_error(client):
    """Polygon API errors should return 500"""
    with patch(
        "app.routers.snapshot.polygon_client.list_snapshot_options_chain",
        side_effect=Exception("API rate limit exceeded"),
    ):
        response = await client.post(
            "/api/snapshot/options-chain",
            json={"underlying_ticker": "AAPL"},
        )

    assert response.status_code == 500


@pytest.mark.anyio
async def test_snapshot_empty_chain(client):
    """Empty options chain should return success with 0 contracts"""
    mock_result = {
        "underlying": {
            "ticker": "XYZ",
            "price": 0,
            "change": 0,
            "change_percent": 0,
        },
        "contracts": [],
    }

    with patch(
        "app.routers.snapshot.polygon_client.list_snapshot_options_chain",
        return_value=mock_result,
    ):
        response = await client.post(
            "/api/snapshot/options-chain",
            json={"underlying_ticker": "XYZ"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert len(data["contracts"]) == 0


@pytest.mark.anyio
async def test_snapshot_carries_the_rate_when_there_is_no_spot(client):
    """Pricing Lab and the Strategy Builder price at the snapshot's rate, so it is never missing (#2764)."""
    mock_result = {
        "underlying": {"ticker": "AAPL", "price": 0, "change": 0, "change_percent": 0},
        "contracts": [],
    }

    with (
        patch("app.routers.snapshot.polygon_client.list_snapshot_options_chain", return_value=mock_result),
        patch(
            "app.services.rate_dividend_service.get_risk_free_rate_and_source", return_value=(0.0371, "FRED")
        ) as fred_rate,
    ):
        response = await client.post("/api/snapshot/options-chain", json={"underlying_ticker": "AAPL"})

    assert response.status_code == 200
    data = response.json()
    assert (data["risk_free_rate"], data["rate_source"]) == (0.0371, "FRED")
    assert (data["dividend_yield"], data["dividend_source"]) == (None, None)
    fred_rate.assert_called_once_with(dte_days=30, observation_date=None)


@pytest.fixture
def fresh_fred_cache():
    fred_service.clear_cache()
    yield
    fred_service.clear_cache()


@pytest.mark.anyio
async def test_snapshot_prices_each_expiry_at_its_own_tenor_rate(client, fresh_fred_cache):
    """The rate is the Treasury curve at the requested expiry's DTE, not a fixed 30 days (#2789)."""
    noon_et_2026_10_01 = et_midnight_ms(date(2026, 10, 1)) + 12 * 3_600_000
    bills = {28: 0.0400, 91: 0.0463, 182: 0.0500, 365: 0.0520}
    chain = {"underlying": {"ticker": "SPY", "price": 0, "change": 0, "change_percent": 0}, "contracts": []}

    with (
        patch("app.routers.snapshot.polygon_client.list_snapshot_options_chain", return_value=chain),
        patch("app.services.fred_service._fetch_all_tenors", return_value=bills),
        patch("app.services.strategy_engine.now_ms_utc", return_value=noon_et_2026_10_01),
    ):
        near = await client.post(
            "/api/snapshot/options-chain", json={"underlying_ticker": "SPY", "expiration_date": "2026-10-30"}
        )
        far = await client.post(
            "/api/snapshot/options-chain", json={"underlying_ticker": "SPY", "expiration_date": "2027-04-01"}
        )

    assert (near.status_code, far.status_code) == (200, 200)
    near_rate, far_rate = near.json()["risk_free_rate"], far.json()["risk_free_rate"]
    # 29 days out sits 1/63 of the way from the 4-week bill (28 d) to the 3-month bill (91 d).
    assert near_rate == pytest.approx(0.0400 + (1 / 63) * (0.0463 - 0.0400), abs=1e-12, rel=0)
    # 182 days out is the 6-month bill exactly.
    assert far_rate == pytest.approx(0.0500, abs=1e-12, rel=0)
    assert near_rate != far_rate
