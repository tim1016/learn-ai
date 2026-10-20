"""E2E tests for the live IV30 endpoints (Step C of IV-ownership plan).

Mocks the Polygon snapshot at the ``polygon_client`` import boundary so the
tests are deterministic, network-free, and fast. The acceptance for live
SPY VIX-style ≈ published VIX is exercised separately as a slow live test
(skipped in CI).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.options.expiry import option_expiry_anchor_ms
from app.services.bs_greeks import bs_european_price
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import to_ms_utc


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
        # The router buckets expiries by the New York calendar date, so the
        # synthetic expiries are laid out on ET dates, matching the bucketing
        # whenever the test runs.
        exp_iso = (et_date_at_ms(to_ms_utc(asof)) + timedelta(days=days)).isoformat()
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
                        "expiration_ms": option_expiry_anchor_ms(date.fromisoformat(exp_iso)),
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
    async def test_recovers_constant_vol_from_synthetic_chain_and_serves_debug_payload(self, client, mock_snapshot):
        resp = await client.post(
            "/api/edge/iv30/vix-style", json={"symbol": "SPY", "target_calendar_days": 30}
        )
        debug_resp = await client.post(
            "/api/edge/iv30/vix-style",
            json={"symbol": "SPY", "target_calendar_days": 30, "debug": True},
        )
        assert resp.status_code == 200, f"recovery: {resp.text}"
        body = resp.json()
        assert body["method"] == "vix_style", "recovery: method is vix_style"
        assert body["symbol"] == "SPY", "recovery: symbol is echoed"
        assert body["target_calendar_days"] == 30, "recovery: target_calendar_days is echoed"

        # Recovery within 100 bps for the mocked σ=0.20 chain.
        assert abs(body["iv30_act365"] - mock_snapshot["sigma"]) < 0.01, "recovery: iv30 within 100 bps of the mocked sigma"

        # Provenance: all-opra-mid (no synthesis).
        prov = body["iv_provenance"]
        assert prov["iv_source"] == "internal_solver", "recovery: iv_source is internal_solver"
        assert prov["variance_contribution_synthetic"] == pytest.approx(0.0, abs=1e-12), "recovery: no synthetic variance contribution"
        assert prov["price_source_mix"].get("opra_mid") == pytest.approx(1.0), "recovery: every price is an opra mid"
        assert 0.0 <= prov["strike_coverage_score"] <= 1.0, "recovery: strike coverage score is in [0, 1]"
        # max_single_strike_share must reach the wire —
        # FastAPI response_model would silently drop it if not declared on
        # IvProvenancePayload.
        assert "max_single_strike_share" in prov, "recovery: max_single_strike_share reaches the wire"
        assert 0.0 <= prov["max_single_strike_share"] <= 1.0, "recovery: max_single_strike_share is in [0, 1]"

        # Rate / dividend / spot present.
        assert body["spot"] == pytest.approx(mock_snapshot["spot"]), "recovery: spot is present"
        assert body["rate"] == pytest.approx(mock_snapshot["rate"]), "recovery: rate is present"
        assert body["rate_source"] == "FRED", "recovery: rate_source is FRED"
        assert body["expiries_used_calendar_days"] == [28, 35], "recovery: the 28 and 35 day expiries straddle the target"

        assert debug_resp.status_code == 200, f"debug payload: {debug_resp.text}"
        debug_prov = debug_resp.json()["iv_provenance"]
        assert debug_prov["per_strike_contributions"] is not None, "debug payload: per_strike_contributions is present when requested"
        assert len(debug_prov["per_strike_contributions"]) > 0, "debug payload: per_strike_contributions is not empty"
        for entry in debug_prov["per_strike_contributions"]:
            assert {"strike", "kind", "c_i", "active_leg_sources"}.issubset(entry.keys()), (
                f"debug payload: contribution entry {entry!r} carries strike, kind, c_i and active_leg_sources"
            )

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
        # both 0 by construction.
        assert prov["strike_coverage_score"] == 0.0
        assert prov["max_single_strike_share"] == 0.0
        assert prov["price_source_mix"].get("opra_mid") == pytest.approx(1.0)


class TestPolygonFailureModes:
    async def test_a_contract_whose_expiry_is_no_instant_is_skipped_and_the_rest_is_used(self, client, mock_snapshot):
        """The vendor's integer can be any size. Its contract is dropped, not an error out of the route."""
        from app.routers import iv30 as iv30_router_module

        chain = _bs_chain_contracts(
            spot=mock_snapshot["spot"], sigma=mock_snapshot["sigma"], rate=mock_snapshot["rate"],
            asof=datetime.now(tz=UTC), expiry_days=[21, 28, 35, 42], strikes=[float(k) for k in range(540, 651, 5)],
        )
        junk = {"ticker": "O:SPY999C00591000", "contract_type": "call", "strike_price": 591.0,
                "expiration_ms": 10**20, "last_quote": {"bid": 1.0, "ask": 1.1}}
        payload = {"underlying": {"ticker": "SPY", "price": mock_snapshot["spot"]}, "contracts": [junk, *chain]}
        with patch.object(iv30_router_module.polygon_client, "list_snapshot_options_chain", return_value=payload):
            resp = await client.post("/api/edge/iv30/vix-style", json={"symbol": "SPY", "target_calendar_days": 30})

        assert resp.status_code == 200, resp.text
        assert resp.json()["expiries_used_calendar_days"] == [28, 35]

    async def test_no_spot_or_no_contracts_returns_502(self, client):
        from app.routers import iv30 as iv30_router_module

        with patch.object(
            iv30_router_module.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 0.0}, "contracts": []},
        ):
            no_spot = await client.post(
                "/api/edge/iv30/vix-style", json={"symbol": "SPY"}
            )
        with patch.object(
            iv30_router_module.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 591.0}, "contracts": []},
        ):
            no_contracts = await client.post(
                "/api/edge/iv30/vix-style", json={"symbol": "SPY"}
            )

        assert no_spot.status_code == 502, "no spot: the route answers 502"
        assert "spot" in no_spot.text.lower(), "no spot: the error names the spot"
        assert no_contracts.status_code == 502, "no contracts: the route answers 502"

    async def test_failed_dividend_lookup_returns_502(self, client, mock_snapshot):
        from app.services.rate_dividend_service import RateAndDividend

        no_dividend = RateAndDividend(
            rate=mock_snapshot["rate"], dividend_yield=None, source_rate="FRED", source_dividend=None
        )
        with patch("app.routers.iv30.get_rate_and_dividend", return_value=no_dividend):
            resp = await client.post("/api/edge/iv30/vix-style", json={"symbol": "SPY"})
        assert resp.status_code == 502
        assert "dividend" in resp.text.lower()


@pytest.fixture
def vendor_chain(monkeypatch):
    """The route's real client, over a fake SDK that filters expiries as the vendor does (#3221).

    The chain lists expiries 21..42 days out, which straddle 30, and one at 120 days that
    no IV30 needs.
    """
    from app.routers import iv30 as iv30_router_module
    from app.services.rate_dividend_service import RateAndDividend
    from tests._helpers.polygon_snapshot_sdk import VendorFilteringSnapshotSdk

    spot, sigma, rate = 591.0, 0.20, 0.045
    contracts = _bs_chain_contracts(
        spot=spot, sigma=sigma, rate=rate, asof=datetime.now(tz=UTC),
        expiry_days=[21, 28, 35, 42, 120], strikes=[float(k) for k in range(540, 651, 5)],
    )
    monkeypatch.setattr(
        iv30_router_module.polygon_client, "client", VendorFilteringSnapshotSdk(contracts, spot=spot)
    )
    with patch(
        "app.routers.iv30.get_rate_and_dividend",
        return_value=RateAndDividend(
            rate=rate, dividend_yield=0.012, source_rate="FRED", source_dividend="Polygon TTM"
        ),
    ):
        yield {"sigma": sigma}


class TestLiveRoutesAgainstAVendorThatFiltersExpiries:
    """The routes used to ask for today's expiry only, which the pairing drops: 502 "got 0"."""

    async def test_routes_find_the_straddle_around_30_days_and_normalize_the_symbol(self, client, vendor_chain):
        vix_style = await client.post("/api/edge/iv30/vix-style", json={"symbol": "SPY", "target_calendar_days": 30})
        parametric = await client.post("/api/edge/iv30/parametric", json={"symbol": "SPY", "target_calendar_days": 30})
        lower_case = await client.post("/api/edge/iv30/vix-style", json={"symbol": " spy ", "target_calendar_days": 30})

        assert vix_style.status_code == 200, f"vix-style: {vix_style.text}"
        vix_style_body = vix_style.json()
        assert vix_style_body["expiries_used_calendar_days"] == [28, 35], "vix-style: finds the straddle around 30 days"
        assert abs(vix_style_body["iv30_act365"] - vendor_chain["sigma"]) < 0.01, "vix-style: iv30 recovers the chain's sigma"

        assert parametric.status_code == 200, f"parametric: {parametric.text}"
        parametric_body = parametric.json()
        assert {28, 35} <= set(parametric_body["expiries_used_calendar_days"]), "parametric: finds the straddle around 30 days"
        assert abs(parametric_body["iv30_act365"] - vendor_chain["sigma"]) < 0.01, "parametric: iv30 recovers the chain's sigma"

        assert lower_case.status_code == 200, f"lower-case symbol: {lower_case.text}"
        assert lower_case.json()["symbol"] == "SPY", "lower-case symbol: a lower-case symbol is the upper-case ticker"

    @pytest.mark.parametrize("symbol", ["", "../SPY", "SPY/QQQ", "A" * 13])
    async def test_a_symbol_that_is_not_a_ticker_is_a_422(self, client, symbol):
        resp = await client.post("/api/edge/iv30/vix-style", json={"symbol": symbol})

        assert resp.status_code == 422
