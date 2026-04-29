"""E2E tests for the live IV30 endpoints (Step C of IV-ownership plan).

Mocks the Polygon snapshot at the ``polygon_client`` import boundary so the
tests are deterministic, network-free, and fast. The acceptance for live
SPY VIX-style ≈ published VIX is exercised separately as a slow live test
(skipped in CI).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.bs_greeks import bs_european_price


def _bs_chain_contracts(
    *,
    spot: float,
    sigma: float,
    rate: float,
    asof: datetime,
    expiry_days: list[int],
    strikes: list[float],
    half_spread: float = 0.01,
) -> list[dict]:
    """Synthesize a Polygon-style snapshot's ``contracts`` list from BS prices.

    Mirrors the Polygon SDK output shape — each contract is a dict with
    ``last_quote.bid`` / ``last_quote.ask`` populated. Used to drive the
    router under a mocked client without hitting the wire.
    """
    out: list[dict] = []
    for days in expiry_days:
        T = days / 365.0
        exp_iso = (asof + timedelta(days=days)).date().isoformat()
        for k in strikes:
            for is_call in (True, False):
                price = bs_european_price(
                    spot=spot, strike=k, ttm_years=T, rate=rate,
                    volatility=sigma, is_call=is_call,
                )
                bid = max(0.0, price - half_spread)
                ask = price + half_spread
                out.append(
                    {
                        "ticker": f"O:SPY{days:03d}{'C' if is_call else 'P'}{int(k*100):08d}",
                        "contract_type": "call" if is_call else "put",
                        "strike_price": float(k),
                        "expiration_date": exp_iso,
                        "last_quote": {"bid": float(bid), "ask": float(ask)},
                    }
                )
    return out


@pytest.fixture
def mock_snapshot():
    """Mock polygon_client.list_snapshot_options_chain at the router's import.

    Uses BS prices for SPY-shaped chain straddling 30 days at σ=0.20.
    """
    spot = 591.0
    sigma = 0.20
    rate = 0.045
    asof = datetime.now(tz=UTC)

    strikes = list(range(540, 651, 5))  # 540..650 step 5
    contracts = _bs_chain_contracts(
        spot=spot, sigma=sigma, rate=rate, asof=asof,
        expiry_days=[21, 28, 35, 42], strikes=[float(k) for k in strikes],
    )

    snapshot_payload = {
        "underlying": {"ticker": "SPY", "price": spot, "change": 0.0, "change_percent": 0.0},
        "contracts": contracts,
    }

    from app.routers import iv30 as iv30_router_module

    with (
        patch.object(iv30_router_module.polygon_client, "list_snapshot_options_chain",
                     return_value=snapshot_payload),
        patch("app.routers.iv30.get_rate_and_dividend") as rd_mock,
    ):
        from app.services.rate_dividend_service import RateAndDividend
        rd_mock.return_value = RateAndDividend(
            rate=rate,
            dividend_yield=0.012,
            source_rate="FRED",
            source_dividend="Polygon TTM",
        )
        yield {"sigma": sigma, "spot": spot, "rate": rate}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class TestVixStyleRoute:
    async def test_recovers_constant_vol_from_synthetic_chain(self, client, mock_snapshot):
        resp = await client.post(
            "/api/edge/iv30/vix-style", json={"symbol": "SPY", "target_calendar_days": 30}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["method"] == "vix_style"
        assert body["symbol"] == "SPY"
        assert body["target_calendar_days"] == 30

        # Recovery within 100 bps for the mocked σ=0.20 chain.
        assert abs(body["iv30_act365"] - mock_snapshot["sigma"]) < 0.01

        # Provenance: all-opra-mid (no synthesis).
        prov = body["iv_provenance"]
        assert prov["iv_source"] == "internal_solver"
        assert prov["variance_contribution_synthetic"] == pytest.approx(0.0, abs=1e-12)
        assert prov["price_source_mix"].get("opra_mid") == pytest.approx(1.0)
        assert 0.0 <= prov["strike_coverage_score"] <= 1.0
        # max_single_strike_share (research-doc §8.2.5) must reach the wire —
        # FastAPI response_model would silently drop it if not declared on
        # IvProvenancePayload.
        assert "max_single_strike_share" in prov
        assert 0.0 <= prov["max_single_strike_share"] <= 1.0

        # Rate / dividend / spot present.
        assert body["spot"] == pytest.approx(mock_snapshot["spot"])
        assert body["rate"] == pytest.approx(mock_snapshot["rate"])
        assert body["rate_source"] == "FRED"
        assert body["expiries_used_calendar_days"] == [28, 35]

    async def test_debug_payload_when_requested(self, client, mock_snapshot):
        resp = await client.post(
            "/api/edge/iv30/vix-style",
            json={"symbol": "SPY", "target_calendar_days": 30, "debug": True},
        )
        assert resp.status_code == 200, resp.text
        prov = resp.json()["iv_provenance"]
        assert prov["per_strike_contributions"] is not None
        assert len(prov["per_strike_contributions"]) > 0
        for entry in prov["per_strike_contributions"]:
            assert {"strike", "kind", "c_i", "active_leg_sources"}.issubset(entry.keys())

    async def test_no_straddle_returns_400(self, client):
        """If the snapshot has no expiries above the target, we return 400."""
        from app.routers import iv30 as iv30_router_module

        spot = 100.0
        asof = datetime.now(tz=UTC)
        contracts = _bs_chain_contracts(
            spot=spot, sigma=0.20, rate=0.05, asof=asof,
            expiry_days=[7, 14],  # both below 30
            strikes=[80.0, 90.0, 100.0, 110.0, 120.0],
        )
        snapshot_payload = {
            "underlying": {"ticker": "FOO", "price": spot},
            "contracts": contracts,
        }
        with (
            patch.object(iv30_router_module.polygon_client, "list_snapshot_options_chain",
                         return_value=snapshot_payload),
            patch("app.routers.iv30.get_rate_and_dividend") as rd_mock,
        ):
            from app.services.rate_dividend_service import RateAndDividend
            rd_mock.return_value = RateAndDividend(
                rate=0.05, dividend_yield=0.0,
                source_rate="FRED", source_dividend="Polygon TTM",
            )
            resp = await client.post(
                "/api/edge/iv30/vix-style", json={"symbol": "FOO", "target_calendar_days": 30}
            )
            assert resp.status_code == 400
            assert "straddle" in resp.text.lower()


class TestParametricRoute:
    async def test_recovers_constant_vol_from_atm_chain(self, client, mock_snapshot):
        resp = await client.post(
            "/api/edge/iv30/parametric", json={"symbol": "SPY", "target_calendar_days": 30}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["method"] == "parametric"
        # Parametric recovers the constant σ within solver tolerance.
        assert abs(body["iv30_act365"] - mock_snapshot["sigma"]) < 0.01

        prov = body["iv_provenance"]
        assert prov["iv_source"] == "internal_solver"
        # All ATM legs are real OPRA → 0% synthetic.
        assert prov["variance_contribution_synthetic"] == pytest.approx(0.0)
        # Parametric samples no wings — coverage and per-strike-share are
        # both 0 by construction (research-doc §8.2.5).
        assert prov["strike_coverage_score"] == 0.0
        assert prov["max_single_strike_share"] == 0.0
        assert prov["price_source_mix"].get("opra_mid") == pytest.approx(1.0)


class TestPolygonFailureModes:
    async def test_no_spot_returns_502(self, client):
        from app.routers import iv30 as iv30_router_module

        with patch.object(
            iv30_router_module.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 0.0}, "contracts": []},
        ):
            resp = await client.post(
                "/api/edge/iv30/vix-style", json={"symbol": "SPY"}
            )
            assert resp.status_code == 502
            assert "spot" in resp.text.lower()

    async def test_no_contracts_returns_502(self, client):
        from app.routers import iv30 as iv30_router_module

        with patch.object(
            iv30_router_module.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 591.0}, "contracts": []},
        ):
            resp = await client.post(
                "/api/edge/iv30/vix-style", json={"symbol": "SPY"}
            )
            assert resp.status_code == 502
