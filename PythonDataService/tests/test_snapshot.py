"""Tests for options chain snapshot endpoint"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.anyio
async def test_snapshot_returns_contracts(client):
    """Snapshot endpoint should return formatted options chain data"""
    mock_result = {
        'underlying': {
            'ticker': 'AAPL',
            'price': 185.50,
            'change': 2.30,
            'change_percent': 1.25,
        },
        'contracts': [
            {
                'ticker': 'O:AAPL250221C00185000',
                'contract_type': 'call',
                'strike_price': 185.0,
                'expiration_date': '2025-02-21',
                'break_even_price': 187.50,
                'implied_volatility': 0.25,
                'open_interest': 1500,
                'greeks': {
                    'delta': 0.52,
                    'gamma': 0.03,
                    'theta': -0.15,
                    'vega': 0.20,
                },
                'day': {
                    'open': 3.00,
                    'high': 3.50,
                    'low': 2.80,
                    'close': 3.20,
                    'volume': 5000,
                    'vwap': 3.10,
                },
            },
            {
                'ticker': 'O:AAPL250221P00185000',
                'contract_type': 'put',
                'strike_price': 185.0,
                'expiration_date': '2025-02-21',
                'break_even_price': 182.50,
                'implied_volatility': 0.28,
                'open_interest': 1200,
                'greeks': {
                    'delta': -0.48,
                    'gamma': 0.03,
                    'theta': -0.12,
                    'vega': 0.19,
                },
                'day': None,
            },
        ],
    }

    with patch(
        'app.routers.snapshot.polygon_client.list_snapshot_options_chain',
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
async def test_snapshot_missing_ticker_returns_422(client):
    """Missing ticker should fail validation"""
    response = await client.post(
        "/api/snapshot/options-chain",
        json={},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_snapshot_handles_polygon_error(client):
    """Polygon API errors should return 500"""
    with patch(
        'app.routers.snapshot.polygon_client.list_snapshot_options_chain',
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
        'underlying': {
            'ticker': 'XYZ',
            'price': 0,
            'change': 0,
            'change_percent': 0,
        },
        'contracts': [],
    }

    with patch(
        'app.routers.snapshot.polygon_client.list_snapshot_options_chain',
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
