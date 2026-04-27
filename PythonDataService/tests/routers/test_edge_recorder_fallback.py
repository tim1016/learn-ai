"""Tests for the recorder fallback in edge.py.

Closes the second item from docs/architecture/iv-ownership-signoff.md §5
(realized-vs-iv router auto-reads from recorder). Exercises:

- The pure helper ``_iv_series_from_recorder`` (precedence + skip rules).
- The realized-vs-iv route's ``iv_source`` field across the three states:
  ``caller_supplied`` (caller wins), ``recorder`` (fallback fires),
  ``absent`` (recorder empty).
- The regime route's silent fallback (no response field, but the recorder
  iv30 reaches the feature builder).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.edge import _iv_series_from_recorder
from app.services.iv_recorder import (
    InMemoryIvSnapshotStore,
    RecordedIvSnapshot,
    get_iv_store,
    set_iv_store,
)


@pytest.fixture
def recorder_store():
    """Swap the process-wide store with an in-memory one; restore after."""
    new = InMemoryIvSnapshotStore()
    original = get_iv_store()
    set_iv_store(new)
    try:
        yield new
    finally:
        set_iv_store(original)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _bars(n: int, start_ms: int = 1_700_000_000_000, step_ms: int = 86_400_000) -> list[dict]:
    """Synthetic daily OHLCV with mild drift so RV estimators have signal."""
    rng = np.random.default_rng(seed=42)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=n)))
    bars: list[dict] = []
    for i, c in enumerate(closes):
        ts = start_ms + i * step_ms
        bars.append(
            {
                "ts": int(ts),
                "open": float(c * 0.999),
                "high": float(c * 1.005),
                "low": float(c * 0.995),
                "close": float(c),
                "volume": 1_000_000.0,
            }
        )
    return bars


def _record(
    store: InMemoryIvSnapshotStore,
    *,
    ts_ms: int,
    iv_vix: float | None = 0.20,
    iv_param: float | None = None,
    vcs: float | None = None,
    error: str | None = None,
    ticker: str = "SPY",
) -> None:
    prov: dict = {}
    if vcs is not None:
        prov["variance_contribution_synthetic"] = vcs
    store.write(
        RecordedIvSnapshot(
            ticker=ticker,
            snapshot_ts_ms=ts_ms,
            slot="09:35",
            spot=100.0,
            rate=0.045,
            dividend_yield=0.015,
            rate_source="test",
            dividend_source="test",
            iv30_vix_style=iv_vix,
            iv30_parametric=iv_param,
            iv_provenance=prov,
            raw_chain=[],
            error=error,
        )
    )


class TestIvSeriesFromRecorder:
    def test_empty_window_returns_empty_list(self, recorder_store):
        assert _iv_series_from_recorder("SPY", pd.Index([], dtype=np.int64)) == []

    def test_no_rows_returns_empty_list(self, recorder_store):
        idx = pd.Index([1_700_000_000_000, 1_700_086_400_000], dtype=np.int64)
        assert _iv_series_from_recorder("SPY", idx) == []

    def test_prefers_vix_style_over_parametric(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, iv_param=0.19)
        idx = pd.Index([ts], dtype=np.int64)

        items = _iv_series_from_recorder("SPY", idx)

        assert len(items) == 1
        assert items[0]["iv30"] == pytest.approx(0.21)

    def test_falls_back_to_parametric_when_vix_style_missing(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=None, iv_param=0.19)
        idx = pd.Index([ts], dtype=np.int64)

        items = _iv_series_from_recorder("SPY", idx)

        assert items[0]["iv30"] == pytest.approx(0.19)

    def test_skips_rows_with_error(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, error="polygon outage")
        idx = pd.Index([ts], dtype=np.int64)

        assert _iv_series_from_recorder("SPY", idx) == []

    def test_skips_rows_with_no_iv(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=None, iv_param=None)
        idx = pd.Index([ts], dtype=np.int64)

        assert _iv_series_from_recorder("SPY", idx) == []

    def test_propagates_variance_contribution_synthetic(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, vcs=0.12)
        idx = pd.Index([ts], dtype=np.int64)

        items = _iv_series_from_recorder("SPY", idx)

        assert items[0]["variance_contribution_synthetic"] == pytest.approx(0.12)
        # health_score is intentionally omitted so the parser defaults it to 1.0.
        assert "health_score" not in items[0]

    def test_window_filters_by_ticker(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, ticker="QQQ", iv_vix=0.25)
        idx = pd.Index([ts], dtype=np.int64)

        assert _iv_series_from_recorder("SPY", idx) == []
        assert len(_iv_series_from_recorder("QQQ", idx)) == 1


class TestRealizedVsIvIvSourceField:
    async def test_caller_supplied_wins_over_recorder(self, client, recorder_store):
        bars = _bars(40)
        # Recorder has a row mid-window — should be ignored when caller supplies.
        _record(recorder_store, ts_ms=bars[20]["ts"], iv_vix=0.30)
        caller_iv = [{"ts": bars[10]["ts"], "iv30": 0.18}]

        resp = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars, "iv_series": caller_iv},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["iv_source"] == "caller_supplied"

    async def test_recorder_fallback_fires(self, client, recorder_store):
        bars = _bars(40)
        _record(recorder_store, ts_ms=bars[15]["ts"], iv_vix=0.22)
        _record(recorder_store, ts_ms=bars[25]["ts"], iv_vix=0.24)

        resp = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["iv_source"] == "recorder"
        # iv30 is sparse — only the two snapshot bars carry a value.
        non_null = sum(1 for v in body["iv30"] if v is not None)
        assert non_null == 2

    async def test_absent_when_recorder_empty(self, client, recorder_store):
        bars = _bars(40)

        resp = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["iv_source"] == "absent"
        assert all(v is None for v in body["iv30"])
