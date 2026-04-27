"""Tests for the multi-snapshot IV recorder (Step D of IV-ownership plan).

Three layers:

1. Pure store contracts (in-memory + JSONL).
2. The recorder service against a mocked Polygon client.
3. The HTTP router with the in-memory store injected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.bs_greeks import bs_european_price
from app.services.iv_recorder import (
    SLOT_CHOICES,
    InMemoryIvSnapshotStore,
    JsonlIvSnapshotStore,
    RecordedIvSnapshot,
    record_iv_snapshot,
)


def _bs_chain_payload(
    *, spot: float, sigma: float, rate: float, asof: datetime,
    expiry_days: list[int], strikes: list[float], half_spread: float = 0.05,
) -> dict:
    contracts = []
    for d in expiry_days:
        T = d / 365.0
        exp_iso = (asof + timedelta(days=d)).date().isoformat()
        for k in strikes:
            for is_call in (True, False):
                price = bs_european_price(
                    spot=spot, strike=k, ttm_years=T, rate=rate,
                    volatility=sigma, is_call=is_call,
                )
                contracts.append(
                    {
                        "ticker": f"O:SPY{d}{'C' if is_call else 'P'}{int(k*100):08d}",
                        "contract_type": "call" if is_call else "put",
                        "strike_price": float(k),
                        "expiration_date": exp_iso,
                        "last_quote": {
                            "bid": float(max(0.0, price - half_spread)),
                            "ask": float(price + half_spread),
                        },
                        "implied_volatility": float(sigma),
                    }
                )
    return {
        "underlying": {"ticker": "SPY", "price": spot},
        "contracts": contracts,
    }


class TestInMemoryStore:
    def test_round_trip(self):
        store = InMemoryIvSnapshotStore()
        snap = RecordedIvSnapshot(
            ticker="SPY", snapshot_ts_ms=1, slot="09:35", spot=590.0,
            rate=0.045, dividend_yield=0.012,
            rate_source="FRED", dividend_source="Polygon TTM",
            iv30_vix_style=0.18, iv30_parametric=0.17,
            iv_provenance={"iv_source": "internal_solver"},
            raw_chain=[], error=None,
        )
        store.write(snap)
        rows = store.read_series("SPY")
        assert len(rows) == 1
        assert rows[0] == snap

    def test_filter_by_window(self):
        store = InMemoryIvSnapshotStore()
        for ts in (100, 200, 300, 400):
            store.write(RecordedIvSnapshot(
                ticker="SPY", snapshot_ts_ms=ts, slot="09:35", spot=0.0,
                rate=0.0, dividend_yield=0.0, rate_source="x", dividend_source="x",
                iv30_vix_style=None, iv30_parametric=None,
                iv_provenance={}, raw_chain=[], error=None,
            ))
        assert len(store.read_series("SPY", start_ms=200, end_ms=300)) == 2
        assert len(store.read_series("SPY", start_ms=350)) == 1
        assert len(store.read_series("SPY", end_ms=150)) == 1


class TestJsonlStore:
    def test_round_trip_persists_to_disk(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        snap = RecordedIvSnapshot(
            ticker="QQQ", snapshot_ts_ms=42, slot="12:30", spot=400.0,
            rate=0.045, dividend_yield=0.0,
            rate_source="FRED", dividend_source="Polygon TTM",
            iv30_vix_style=0.20, iv30_parametric=None,
            iv_provenance={"iv_source": "internal_solver"},
            raw_chain=[{"strike_price": 400.0}], error=None,
        )
        store.write(snap)
        f = tmp_path / "QQQ.jsonl"
        assert f.exists()
        line = f.read_text().strip()
        assert json.loads(line)["ticker"] == "QQQ"

        store2 = JsonlIvSnapshotStore(tmp_path)
        rows = store2.read_series("QQQ")
        assert len(rows) == 1
        assert rows[0].snapshot_ts_ms == 42

    def test_unknown_ticker_returns_empty(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        assert store.read_series("XYZ") == []


class TestRecorderService:
    def test_writes_full_provenance_on_success(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        spot = 591.0
        sigma = 0.20
        rate = 0.045
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = _bs_chain_payload(
            spot=spot, sigma=sigma, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )

        with patch("app.services.iv_recorder.get_rate_and_dividend") as rd:
            from app.services.rate_dividend_service import RateAndDividend
            rd.return_value = RateAndDividend(
                rate=rate, dividend_yield=0.0,
                source_rate="FRED", source_dividend="Polygon TTM",
            )
            row = record_iv_snapshot(
                ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof,
            )

        assert row.error is None
        assert row.spot == pytest.approx(spot)
        assert row.iv30_vix_style is not None
        assert abs(row.iv30_vix_style - sigma) < 0.01
        prov = row.iv_provenance
        assert prov["iv_source"] == "internal_solver"
        assert prov["variance_contribution_synthetic"] == pytest.approx(0.0, abs=1e-12)
        assert prov["price_source_mix"]["opra_mid"] == pytest.approx(1.0)
        assert len(row.raw_chain) > 0
        for r in row.raw_chain:
            assert "bid" in r and "ask" in r
            assert "polygon_iv_diagnostic" in r
        assert row.snapshot_ts_ms == int(asof.timestamp() * 1000)

    def test_polygon_failure_persists_error_row(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        polygon.list_snapshot_options_chain.side_effect = RuntimeError("polygon timeout")

        row = record_iv_snapshot(
            ticker="SPY", slot="12:30", store=store, polygon=polygon,
            asof=datetime(2026, 4, 28, 16, 30, tzinfo=UTC),
        )
        assert row.error is not None
        assert "polygon" in row.error.lower()
        rows = store.read_series("SPY")
        assert len(rows) == 1
        assert rows[0].error is not None

    def test_invalid_slot_rejected(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        with pytest.raises(ValueError, match="slot must be one of"):
            record_iv_snapshot(
                ticker="SPY", slot="10:00", store=store, polygon=polygon,
            )


@pytest.fixture
def in_memory_store():
    """Replace the module-level store with an in-memory one for the test."""
    from app.services.iv_recorder import get_iv_store, set_iv_store

    new_store = InMemoryIvSnapshotStore()
    original = get_iv_store()
    set_iv_store(new_store)
    try:
        yield new_store
    finally:
        set_iv_store(original)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class TestRecorderRoutes:
    async def test_snapshot_route_invalid_slot_returns_400(self, client):
        resp = await client.post(
            "/api/iv-recorder/snapshot",
            json={"ticker": "SPY", "slot": "10:00", "target_calendar_days": 30},
        )
        assert resp.status_code == 400

    async def test_snapshot_then_read_back(self, client, in_memory_store):
        from app.routers import iv_recorder as iv_recorder_router

        spot = 591.0
        sigma = 0.20
        rate = 0.045
        asof = datetime.now(tz=UTC)
        polygon_payload = _bs_chain_payload(
            spot=spot, sigma=sigma, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        with (
            patch.object(iv_recorder_router.polygon_client, "list_snapshot_options_chain",
                         return_value=polygon_payload),
            patch("app.services.iv_recorder.get_rate_and_dividend") as rd,
        ):
            from app.services.rate_dividend_service import RateAndDividend
            rd.return_value = RateAndDividend(
                rate=rate, dividend_yield=0.0,
                source_rate="FRED", source_dividend="Polygon TTM",
            )
            resp = await client.post(
                "/api/iv-recorder/snapshot",
                json={"ticker": "SPY", "slot": "09:35"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["success"] is True
            assert body["snapshot"]["iv30_vix_style"] is not None
            assert body["snapshot"]["error"] is None

        read_resp = await client.get("/api/iv-recorder/series/SPY")
        assert read_resp.status_code == 200
        data = read_resp.json()
        assert data["n_snapshots"] == 1
        assert data["snapshots"][0]["slot"] == "09:35"

    async def test_series_window_filters(self, client, in_memory_store):
        for ts in (100, 200, 300):
            in_memory_store.write(
                RecordedIvSnapshot(
                    ticker="SPY", snapshot_ts_ms=ts, slot="09:35", spot=0.0,
                    rate=0.0, dividend_yield=0.0,
                    rate_source="x", dividend_source="x",
                    iv30_vix_style=None, iv30_parametric=None,
                    iv_provenance={}, raw_chain=[], error=None,
                )
            )
        resp = await client.get("/api/iv-recorder/series/SPY", params={"start_ms": 150, "end_ms": 250})
        body = resp.json()
        assert body["n_snapshots"] == 1
        assert body["snapshots"][0]["snapshot_ts_ms"] == 200


class TestSlotChoicesContract:
    def test_three_default_slots(self):
        assert SLOT_CHOICES == ("09:35", "12:30", "16:00")
