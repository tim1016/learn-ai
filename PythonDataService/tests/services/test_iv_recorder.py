"""Tests for the multi-snapshot IV recorder (Step D of IV-ownership plan).

Three layers:

1. Pure store contracts (in-memory + JSONL).
2. The recorder service against a mocked Polygon client.
3. The HTTP router with the in-memory store injected.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.options.expiry import option_expiry_anchor_ms
from app.services.bs_greeks import bs_european_price
from app.services.iv_recorder import (
    InMemoryIvSnapshotStore,
    JsonlIvSnapshotStore,
    RecordedIvSnapshot,
    record_iv_snapshot,
)
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import to_ms_utc


def _bs_chain_payload(
    *, spot: float, sigma: float, rate: float, asof: datetime,
    expiry_days: list[int], strikes: list[float], half_spread: float = 0.05,
) -> dict:
    contracts = []
    for d in expiry_days:
        T = d / 365.0
        exp_iso = (et_date_at_ms(to_ms_utc(asof)) + timedelta(days=d)).isoformat()
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
                        "expiration_ms": option_expiry_anchor_ms(date.fromisoformat(exp_iso)),
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

    def test_read_series_refuses_a_ticker_that_escapes_the_store(self, tmp_path: Path):
        # The ticker reaches read_series from a request body (edge.py).
        store_dir = tmp_path / "store"
        (tmp_path / "secret.jsonl").write_text("{}\n")
        store = JsonlIvSnapshotStore(store_dir)
        with pytest.raises(ValueError, match="escapes root"):
            store.read_series("../secret")

    def test_legacy_jsonl_without_health_score_reads_back_as_none(
        self, tmp_path: Path
    ):
        # Write a JSONL line that pre-dates the health_score field — the
        # production /var/lib/iv-recorder/SPY.jsonl will have rows like
        # this from before this PR landed. RecordedIvSnapshot(**d) must
        # tolerate the missing key via the default.
        legacy_row = {
            "ticker": "SPY",
            "snapshot_ts_ms": 1700000000000,
            "slot": "16:00",
            "spot": 590.0,
            "rate": 0.045,
            "dividend_yield": 0.012,
            "rate_source": "FRED",
            "dividend_source": "Polygon TTM",
            "iv30_vix_style": 0.18,
            "iv30_parametric": None,
            "iv_provenance": {"iv_source": "internal_solver"},
            "raw_chain": [],
            "error": None,
            # NOTE: no health_score key — that's the whole point.
        }
        f = tmp_path / "SPY.jsonl"
        f.write_text(json.dumps(legacy_row) + "\n")

        store = JsonlIvSnapshotStore(tmp_path)
        rows = store.read_series("SPY")
        assert len(rows) == 1
        assert rows[0].health_score is None
        assert rows[0].iv30_vix_style == pytest.approx(0.18)


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
        # Health score is computed on the same chain and persisted. The
        # parity test in test_iv30_stability.py validates the math; here
        # we just confirm the field is populated and in range. (Score
        # quality depends on chain density / spacing — this synthetic
        # chain has 5-dollar strikes, so half-resolution resampling moves
        # IV non-trivially and drives the composite below 0.9.)
        assert row.health_score is not None
        assert 0.0 <= row.health_score <= 1.0

    def test_a_failed_dividend_lookup_keeps_the_real_rate(self):
        """IV30 is solved and stored at the FRED rate, not at r=0, when only q is missing (#2764)."""
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        rate = 0.045
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = _bs_chain_payload(
            spot=591.0, sigma=0.20, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )

        with (
            patch("app.services.rate_dividend_service.get_risk_free_rate_and_source", return_value=(rate, "FRED")),
            patch("app.services.rate_dividend_service.compute_dividend_yield", side_effect=RuntimeError("timeout")),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is None
        assert (row.rate, row.rate_source) == (rate, "FRED")
        assert (row.dividend_yield, row.dividend_source) == (0.0, "unknown")

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
        # Error rows have no health score — there's no chain to score.
        assert row.health_score is None
        rows = store.read_series("SPY")
        assert len(rows) == 1
        assert rows[0].error is not None
        assert rows[0].health_score is None

    def test_records_a_real_row_from_a_vendor_that_filters_expiries(self):
        """The client's own expiry default ran against a mocked client method, so a live slot was always an error row (#3221)."""
        from app.services.polygon_client import PolygonClientService
        from app.services.rate_dividend_service import RateAndDividend
        from tests._helpers.polygon_snapshot_sdk import VendorFilteringSnapshotSdk

        store = InMemoryIvSnapshotStore()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        payload = _bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asof,
            expiry_days=[21, 28, 35, 42, 120],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        polygon = PolygonClientService()
        polygon.client = VendorFilteringSnapshotSdk(payload["contracts"], spot=591.0)

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(
                rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
            ),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is None
        assert row.iv30_vix_style is not None
        assert abs(row.iv30_vix_style - 0.20) < 0.01
        assert store.read_series("SPY") == [row]

    def test_a_snapshot_with_no_spot_persists_an_insufficient_snapshot_row(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        polygon.list_snapshot_options_chain.return_value = {"underlying": {"price": 0.0}, "contracts": []}

        row = record_iv_snapshot(
            ticker="SPY", slot="16:00", store=store, polygon=polygon,
            asof=datetime(2026, 4, 28, 20, 0, tzinfo=UTC),
        )

        assert row.error is not None and row.error.startswith("insufficient_snapshot")
        assert row.iv30_vix_style is None
        assert store.read_series("SPY") == [row]

    def test_a_chain_that_cannot_straddle_the_target_persists_a_replication_failed_row(self):
        from app.services.rate_dividend_service import RateAndDividend

        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = _bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asof,
            expiry_days=[7, 14],  # both inside 30 days: nothing above the target
            strikes=[float(k) for k in range(540, 651, 5)],
        )

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(
                rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
            ),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is not None and row.error.startswith("replication_failed")
        assert row.iv30_vix_style is None
        assert len(row.raw_chain) > 0  # the quotes are kept, so the slot can be re-derived
        assert store.read_series("SPY") == [row]

    def test_a_quote_that_is_not_a_quote_still_writes_the_audit_row(self):
        """The grouping of quotes used to sit outside the try: a 500 and no row (#3221)."""
        from app.services.rate_dividend_service import RateAndDividend

        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = {
            "underlying": {"ticker": "SPY", "price": 591.0},
            "contracts": [
                {
                    "ticker": "O:SPY260520C00590000",
                    "contract_type": "call",
                    "strike_price": 590.0,
                    "expiration_ms": option_expiry_anchor_ms(date(2026, 5, 20)),
                    "last_quote": "n/a",
                }
            ],
        }

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(
                rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
            ),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is not None and row.error.startswith("replication_failed")
        assert store.read_series("SPY") == [row]

    def test_the_dominance_gate_event_carries_only_what_the_replicator_reports(self, caplog):
        import logging

        from app.services.rate_dividend_service import RateAndDividend
        from app.volatility.iv_provenance import IvProvenance

        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = _bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        gated = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 1.0},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.9,
            max_single_strike_share=0.41,
            single_strike_dropped=2,
            single_strike_hard_failed=False,
        )

        with (
            patch(
                "app.services.iv_recorder.get_rate_and_dividend",
                return_value=RateAndDividend(
                    rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
                ),
            ),
            patch("app.services.iv_recorder.vix_style_iv30_with_provenance", return_value=(0.20, gated)),
            caplog.at_level(logging.WARNING),
        ):
            record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        (event,) = [r for r in caplog.records if getattr(r, "event", None) == "iv_dominance_gate"]
        assert (event.max_share_after, event.iterations, event.hard_failed) == (0.41, 2, False)
        # The replicator reports no pre-drop share and no strike count, so the event has none.
        assert not hasattr(event, "max_share_before")
        assert not hasattr(event, "strikes_remaining")


class TestDefaultStorePath:
    def test_the_default_store_writes_under_the_configured_directory(self, tmp_path: Path):
        """``IV_RECORDER_STORE_PATH`` is read when the process starts, so this runs one."""
        import os
        import subprocess
        import sys

        code = (
            "from app.services.iv_recorder import RecordedIvSnapshot, get_iv_store\n"
            "get_iv_store().write(RecordedIvSnapshot(ticker='SPY', snapshot_ts_ms=1, slot='16:00', spot=0.0, "
            "rate=0.0, dividend_yield=0.0, rate_source='x', dividend_source='x', iv30_vix_style=None, "
            "iv30_parametric=None, iv_provenance={}, raw_chain=[]))\n"
        )
        env = {**os.environ, "IV_RECORDER_STORE_PATH": str(tmp_path), "POLYGON_API_KEY": "test"}

        subprocess.run(
            [sys.executable, "-W", "ignore", "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            check=True,
            capture_output=True,
            timeout=180,
        )

        assert (tmp_path / "SPY.jsonl").exists()


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

    async def test_snapshot_route_files_a_lower_case_ticker_under_the_upper_case_one(self, client, in_memory_store):
        from app.routers import iv_recorder as iv_recorder_router

        with patch.object(
            iv_recorder_router.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 0.0}, "contracts": []},
        ):
            resp = await client.post("/api/iv-recorder/snapshot", json={"ticker": " spy", "slot": "09:35"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["snapshot"]["ticker"] == "SPY"
        assert len(in_memory_store.read_series("SPY")) == 1

    @pytest.mark.parametrize("ticker", ["../SPY", "SPY/QQQ", "A" * 13])
    async def test_snapshot_route_refuses_a_ticker_that_is_not_one(self, client, in_memory_store, ticker):
        resp = await client.post("/api/iv-recorder/snapshot", json={"ticker": ticker, "slot": "09:35"})

        assert resp.status_code == 422
        assert in_memory_store.read_series(ticker) == []

    async def test_snapshot_route_records_a_slot(self, client, in_memory_store):
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
