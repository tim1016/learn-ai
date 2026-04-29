"""Tests for the recorder fallback in edge.py.

Covers the realized-vs-iv router auto-reading from the recorder when
``iv_series`` is omitted, plus the imputed-prior policy on
``health_score`` (see docs/architecture/iv-ownership-research.md §4.7
and §8.1.1). Exercises:

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
    health_score: float | None = None,
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
            health_score=health_score,
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
        # When the recorder row has no health_score (legacy row, or a row
        # whose health computation failed), we omit the field so
        # _parse_iv_series takes the drop-health-factor branch and
        # confidence collapses to (1 - vcs). See _parse_iv_series
        # docstring for the rationale on the policy change from the
        # earlier 0.5 imputed prior.
        assert "health_score" not in items[0]

    def test_propagates_health_score_when_present(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, vcs=0.12, health_score=0.85)
        idx = pd.Index([ts], dtype=np.int64)

        items = _iv_series_from_recorder("SPY", idx)

        assert items[0]["health_score"] == pytest.approx(0.85)
        assert items[0]["variance_contribution_synthetic"] == pytest.approx(0.12)

    def test_omits_health_score_for_legacy_rows(self, recorder_store):
        # A row written before the health_score field existed — the
        # JsonlIvSnapshotStore reconstructs it via the default None, and
        # this fallback path should NOT inject a synthetic health number.
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, vcs=0.12, health_score=None)
        idx = pd.Index([ts], dtype=np.int64)

        items = _iv_series_from_recorder("SPY", idx)

        assert "health_score" not in items[0]

    def test_window_filters_by_ticker(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, ticker="QQQ", iv_vix=0.25)
        idx = pd.Index([ts], dtype=np.int64)

        assert _iv_series_from_recorder("SPY", idx) == []
        assert len(_iv_series_from_recorder("QQQ", idx)) == 1


class TestParseIvSeriesNullCoalescing:
    """Caller-supplied iv_series may carry ``health_score: null`` on the
    wire (a JSON-explicit "no value"), not just an absent key. Both
    parsers must handle explicit null without crashing on ``float(None)``,
    and both shapes (missing-key, explicit-null) must take the same
    imputed-evidence branch. CodeRabbit P1 on PR 47, refined per
    iv-research-chat-notes.md §5.3 — imputed bars now drop the health
    factor entirely (confidence = 1 - vcs) rather than apply a 0.5 prior.
    """

    def test_parse_iv_series_handles_explicit_null_health(self):
        from app.routers.edge import _parse_iv_series

        idx = pd.Index([1_000, 2_000], dtype=np.int64)
        # First bar: missing key. Second bar: explicit None on the wire.
        # Both must take the drop-health-factor branch and flag the bar
        # as imputed, not raise.
        iv_series = [
            {"ts": 1_000, "iv30": 0.20, "variance_contribution_synthetic": 0.05},
            {"ts": 2_000, "iv30": 0.21, "health_score": None,
             "variance_contribution_synthetic": 0.07},
        ]

        iv, confidence, health_imputed = _parse_iv_series(iv_series, idx)

        assert iv.notna().all()
        assert confidence is not None
        assert health_imputed is not None
        # Both bars are "imputed" — key-missing AND explicit-null both qualify.
        assert bool(health_imputed.loc[1_000]) is True
        assert bool(health_imputed.loc[2_000]) is True
        # Drop-health-factor branch: confidence = (1 - vcs).
        assert confidence.loc[1_000] == pytest.approx(1.0 - 0.05)
        assert confidence.loc[2_000] == pytest.approx(1.0 - 0.07)

    def test_parse_iv_series_for_regime_handles_explicit_null_health(self):
        from app.routers.edge import _parse_iv_series_for_regime

        idx = pd.Index([1_000, 2_000], dtype=np.int64)
        iv_series = [
            {"ts": 1_000, "iv30": 0.20, "health_score": None,
             "variance_contribution_synthetic": 0.0},
            {"ts": 2_000, "iv30": 0.21, "health_score": None,
             "variance_contribution_synthetic": None},
        ]

        # Must not raise — the bug was float(None) → TypeError → 500.
        iv, weight = _parse_iv_series_for_regime(iv_series, idx)

        assert iv.notna().all()
        assert weight is not None
        # h=0.5 → max(0, 2·0.5 − 1) = 0 → feature_weight = 0 regardless of vcs.
        assert weight.loc[1_000] == pytest.approx(0.0)
        assert weight.loc[2_000] == pytest.approx(0.0)

    def test_parse_iv_series_explicit_null_matches_missing_key(self):
        # Explicit null and missing key must produce identical confidence
        # for the same vcs — both are "no evidence" cases.
        from app.routers.edge import _parse_iv_series

        idx = pd.Index([1_000, 2_000], dtype=np.int64)
        iv_series = [
            {"ts": 1_000, "iv30": 0.20, "variance_contribution_synthetic": 0.10},
            {"ts": 2_000, "iv30": 0.20, "health_score": None,
             "variance_contribution_synthetic": 0.10},
        ]

        _, confidence, _ = _parse_iv_series(iv_series, idx)

        assert confidence.loc[1_000] == pytest.approx(confidence.loc[2_000])


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


class TestHealthScoreImputedPrior:
    """Pins the drop-health-factor policy: when ``health_score`` is omitted
    from iv_series items but ``variance_contribution_synthetic`` is present
    (the typical recorder-fallback shape), confidence collapses to
    ``(1 - vcs)`` rather than applying a synthetic prior multiplier. The
    imputed-ness is surfaced via ``explanation.health_imputed_now`` so the
    UI can flag the bar even though the numeric confidence may match the
    explicit full-health case.

    Refined per ``docs/architecture/iv-research-chat-notes.md`` §5.3 — the
    earlier 0.5 imputed-prior policy was replaced because halving every
    no-evidence confidence is itself a real signal-attenuation choice
    with no evidence to support the cut.
    """

    async def test_explanation_flags_health_imputed_when_missing(self, client, recorder_store):
        bars = _bars(40)
        # Recorder snapshot carries vcs but no health_score — the typical
        # recorder-fallback shape.
        _record(recorder_store, ts_ms=bars[30]["ts"], iv_vix=0.22, vcs=0.10)

        resp = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["iv_source"] == "recorder"
        explanation = body["explanation"]
        assert explanation is not None
        assert explanation["health_imputed_now"] is True

    async def test_explanation_does_not_flag_when_health_supplied(self, client, recorder_store):
        bars = _bars(40)
        # Caller supplies both vcs *and* health_score — no imputation.
        caller_iv = [
            {
                "ts": bars[30]["ts"],
                "iv30": 0.22,
                "variance_contribution_synthetic": 0.10,
                "health_score": 0.95,
            }
        ]

        resp = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars, "iv_series": caller_iv},
        )

        assert resp.status_code == 200
        body = resp.json()
        explanation = body["explanation"]
        assert explanation is not None
        assert explanation["health_imputed_now"] is False

    async def test_imputed_drops_health_factor_matches_explicit_full_health(
        self, client, recorder_store
    ):
        bars = _bars(40)
        # Two requests, identical except one supplies health=1.0 explicitly,
        # the other omits it. Under the drop-health-factor branch, the
        # imputed bar produces confidence = (1 - vcs); the explicit
        # full-health bar produces confidence = 1.0 * (1 - vcs). With
        # vcs = 0 in both cases the two values must be identical — both
        # collapse to 1.0. The imputed-ness is still surfaced via the
        # ``health_imputed_now`` flag so the UI can mark the bar.
        _record(recorder_store, ts_ms=bars[30]["ts"], iv_vix=0.22, vcs=0.0)
        resp_imputed = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars},
        )
        explicit_iv = [
            {
                "ts": bars[30]["ts"],
                "iv30": 0.22,
                "variance_contribution_synthetic": 0.0,
                "health_score": 1.0,
            }
        ]
        resp_explicit = await client.post(
            "/api/edge/realized-vs-iv/series",
            json={"symbol": "SPY", "bars": bars, "iv_series": explicit_iv},
        )

        body_imputed = resp_imputed.json()
        body_explicit = resp_explicit.json()
        c_imputed = body_imputed["explanation"]["latest_confidence"]
        c_explicit = body_explicit["explanation"]["latest_confidence"]
        # Both confidences collapse to 1.0 (vcs=0 on both), but reach it
        # via different branches.
        assert c_imputed == pytest.approx(c_explicit, rel=1e-6)
        # Imputed-ness flag still fires regardless of the numeric match.
        assert body_imputed["explanation"]["health_imputed_now"] is True
        assert body_explicit["explanation"]["health_imputed_now"] is False
