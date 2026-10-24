"""The Realized vs IV series composition and where its IV comes from (ADR 0071 decisions 9 and 11; #3525).

These were the route-level tests of ``POST /api/edge/realized-vs-iv/series``, which #3525 retired when the study began
reading admitted lake bars. They keep the same assertions at the seams that exist now:

* ``recorded_iv_points`` and ``load_iv_input`` (``app.services.realized_vs_iv_study_service``) read the recorder;
* ``compute_realized_vs_iv_series`` (``app.engine.edge.realized_vs_iv_series``) composes the series.

The lake-sourced study around them is tested in ``tests/research/test_realized_vs_iv_study.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.realized_vs_iv_series import _parse_iv_series, compute_realized_vs_iv_series
from app.services.iv_recorder import InMemoryIvSnapshotStore, RecordedIvSnapshot, get_iv_store, set_iv_store
from app.services.realized_vs_iv_study_service import load_iv_input, recorded_iv_points
from app.utils.session_anchors import et_date_at_ms

START_MS = 1_700_000_000_000
DAY_MS = 86_400_000


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


def _bars(n: int, start_ms: int = START_MS, step_ms: int = DAY_MS) -> pd.DataFrame:
    """Synthetic daily OHLCV with mild drift so RV estimators have signal."""
    rng = np.random.default_rng(seed=42)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=n)))
    index = pd.Index([start_ms + i * step_ms for i in range(n)], name="ts", dtype=np.int64)
    return pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def _series(bars: pd.DataFrame, iv_points: list[dict] | None, *, iv_source: str = "caller_supplied", **overrides) -> dict:
    settings = {
        "symbol": "SPY",
        "bar_size": "1d",
        "tenor_days": 30,
        "hf_session": "ETH",
        "estimators": ["yz"],
        "windows": [5, 10, 30],
        "confidence_floor": 0.1,
    }
    return compute_realized_vs_iv_series(bars, iv_points=iv_points, iv_source=iv_source, **{**settings, **overrides})  # type: ignore[arg-type]


def _window_of(bars: pd.DataFrame):
    return et_date_at_ms(int(bars.index.min())), et_date_at_ms(int(bars.index.max()))


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


class TestRecordedIvPoints:
    @pytest.mark.parametrize(
        ("records", "window_ms"),
        [
            pytest.param((), (), id="empty_window"),
            pytest.param((), (1_700_000_000_000, 1_700_086_400_000), id="no_rows"),
            pytest.param(
                ({"iv_vix": 0.21, "error": "polygon outage"},), (1_700_000_000_000,), id="skips_rows_with_error"
            ),
            pytest.param(
                ({"iv_vix": None, "iv_param": None},), (1_700_000_000_000,), id="skips_rows_with_no_iv"
            ),
        ],
    )
    def test_returns_empty_list_when_no_row_is_usable(self, recorder_store, records, window_ms):
        for kwargs in records:
            _record(recorder_store, ts_ms=1_700_000_000_000, **kwargs)
        start, end = (window_ms[0], window_ms[-1]) if window_ms else (1_700_000_000_000, 1_700_000_000_000)

        assert recorded_iv_points("SPY", start_ms=start, end_ms=end) == []

    @pytest.mark.parametrize(
        ("iv_vix", "iv_param", "expected_iv30"),
        [
            pytest.param(0.21, 0.19, 0.21, id="prefers_vix_style_over_parametric"),
            pytest.param(None, 0.19, 0.19, id="falls_back_to_parametric_when_vix_style_missing"),
        ],
    )
    def test_iv30_comes_from_the_preferred_available_source(self, recorder_store, iv_vix, iv_param, expected_iv30):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=iv_vix, iv_param=iv_param)

        items = recorded_iv_points("SPY", start_ms=ts, end_ms=ts)

        assert len(items) == 1, "the one recorded row yields one item"
        assert items[0]["iv30"] == pytest.approx(expected_iv30), "iv30 comes from the preferred available source"

    def test_propagates_variance_contribution_synthetic(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, iv_vix=0.21, vcs=0.12)

        items = recorded_iv_points("SPY", start_ms=ts, end_ms=ts)

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

        items = recorded_iv_points("SPY", start_ms=ts, end_ms=ts)

        assert items[0]["health_score"] == pytest.approx(0.85)
        assert items[0]["variance_contribution_synthetic"] == pytest.approx(0.12)

    def test_window_filters_by_ticker(self, recorder_store):
        ts = 1_700_000_000_000
        _record(recorder_store, ts_ms=ts, ticker="QQQ", iv_vix=0.25)

        assert recorded_iv_points("SPY", start_ms=ts, end_ms=ts) == []
        assert len(recorded_iv_points("QQQ", start_ms=ts, end_ms=ts)) == 1


class TestParseIvSeriesNullCoalescing:
    """Caller-supplied iv_series may carry ``health_score: null`` on the
    wire (a JSON-explicit "no value"), not just an absent key. The
    parser must handle explicit null without crashing on ``float(None)``,
    and both shapes (missing-key, explicit-null) must take the same
    imputed-evidence branch. CodeRabbit P1 on PR 47, refined per
    ADR 0071 decision 11 — imputed bars now drop the health
    factor entirely (confidence = 1 - vcs) rather than apply a 0.5 prior.
    """

    def test_parse_iv_series_handles_explicit_null_health(self):
        idx = pd.Index([1_000, 2_000], dtype=np.int64)
        # First bar: missing key. Second bar: explicit None on the wire.
        # Both must take the drop-health-factor branch and flag the bar
        # as imputed, not raise.
        iv_series = [
            {"ts": 1_000, "iv30": 0.20, "variance_contribution_synthetic": 0.05},
            {"ts": 2_000, "iv30": 0.21, "health_score": None, "variance_contribution_synthetic": 0.07},
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


class TestIvSourceField:
    def test_caller_supplied_wins_over_recorder(self, recorder_store):
        bars = _bars(40)
        # Recorder has a row mid-window — should be ignored when caller supplies.
        _record(recorder_store, ts_ms=int(bars.index[20]), iv_vix=0.30)
        caller_iv = [{"ts": int(bars.index[10]), "iv30": 0.18}]

        loaded = load_iv_input("SPY", _window_of(bars), caller_iv)

        assert loaded.source == "caller_supplied"
        assert [p["iv30"] for p in loaded.points] == [0.18]

    def test_recorder_fallback_fires(self, recorder_store):
        bars = _bars(40)
        _record(recorder_store, ts_ms=int(bars.index[15]), iv_vix=0.22)
        _record(recorder_store, ts_ms=int(bars.index[25]), iv_vix=0.24)

        loaded = load_iv_input("SPY", _window_of(bars), None)
        body = _series(bars, list(loaded.points), iv_source=loaded.source)

        assert loaded.source == "recorder"
        assert body["iv_source"] == "recorder"
        # iv30 is sparse — only the two snapshot bars carry a value.
        non_null = sum(1 for v in body["iv30"] if v is not None)
        assert non_null == 2

    def test_absent_when_recorder_empty(self, recorder_store):
        bars = _bars(40)

        loaded = load_iv_input("SPY", _window_of(bars), None)
        body = _series(bars, None, iv_source=loaded.source)

        assert loaded.source == "absent" and loaded.points == ()
        assert body["iv_source"] == "absent"
        assert all(v is None for v in body["iv30"])

    def test_an_empty_caller_list_reads_the_recorder_as_it_always_did(self, recorder_store):
        bars = _bars(40)
        _record(recorder_store, ts_ms=int(bars.index[5]), iv_vix=0.22)

        assert load_iv_input("SPY", _window_of(bars), []).source == "recorder"

    def test_the_points_are_part_of_the_identity_by_digest(self, recorder_store):
        bars = _bars(40)
        _record(recorder_store, ts_ms=int(bars.index[5]), iv_vix=0.22)
        before = load_iv_input("SPY", _window_of(bars), None)
        _record(recorder_store, ts_ms=int(bars.index[6]), iv_vix=0.23)
        after = load_iv_input("SPY", _window_of(bars), None)

        assert before.digest != after.digest, "a new snapshot is a new input"
        assert after.digest == load_iv_input("SPY", _window_of(bars), None).digest, "and the same snapshots are the same one"


class TestHealthScoreImputedPrior:
    """Pins the drop-health-factor policy: when ``health_score`` is omitted
    from iv_series items but ``variance_contribution_synthetic`` is present
    (the typical recorder-fallback shape), confidence collapses to
    ``(1 - vcs)`` rather than applying a synthetic prior multiplier. The
    imputed-ness is surfaced via ``explanation.health_imputed_now`` so the
    UI can flag the bar even though the numeric confidence may match the
    explicit full-health case.

    Why: the
    earlier 0.5 imputed-prior policy was replaced because halving every
    no-evidence confidence is itself a real signal-attenuation choice
    with no evidence to support the cut.
    """

    def test_explanation_flags_health_imputed_when_missing(self):
        bars = _bars(40)
        # Recorder snapshot carries vcs but no health_score — the typical
        # recorder-fallback shape.
        points = [{"ts": int(bars.index[30]), "iv30": 0.22, "variance_contribution_synthetic": 0.10}]

        body = _series(bars, points, iv_source="recorder")

        explanation = body["explanation"]
        assert explanation is not None
        assert explanation["health_imputed_now"] is True

    def test_explanation_does_not_flag_when_health_supplied(self):
        bars = _bars(40)
        # Caller supplies both vcs *and* health_score — no imputation.
        points = [
            {
                "ts": int(bars.index[30]),
                "iv30": 0.22,
                "variance_contribution_synthetic": 0.10,
                "health_score": 0.95,
            }
        ]

        body = _series(bars, points)

        explanation = body["explanation"]
        assert explanation is not None
        assert explanation["health_imputed_now"] is False

    def test_imputed_drops_health_factor_matches_explicit_full_health(self):
        bars = _bars(40)
        # Two runs, identical except one supplies health=1.0 explicitly,
        # the other omits it. Under the drop-health-factor branch, the
        # imputed bar produces confidence = (1 - vcs); the explicit
        # full-health bar produces confidence = 1.0 * (1 - vcs). With
        # vcs = 0 in both cases the two values must be identical — both
        # collapse to 1.0. The imputed-ness is still surfaced via the
        # ``health_imputed_now`` flag so the UI can mark the bar.
        ts = int(bars.index[30])
        body_imputed = _series(bars, [{"ts": ts, "iv30": 0.22, "variance_contribution_synthetic": 0.0}], iv_source="recorder")
        body_explicit = _series(
            bars, [{"ts": ts, "iv30": 0.22, "variance_contribution_synthetic": 0.0, "health_score": 1.0}]
        )

        c_imputed = body_imputed["explanation"]["latest_confidence"]
        c_explicit = body_explicit["explanation"]["latest_confidence"]
        # Both confidences collapse to 1.0 (vcs=0 on both), but reach it
        # via different branches.
        assert c_imputed == pytest.approx(c_explicit, rel=1e-6)
        # Imputed-ness flag still fires regardless of the numeric match.
        assert body_imputed["explanation"]["health_imputed_now"] is True
        assert body_explicit["explanation"]["health_imputed_now"] is False


class TestTheUnrealizedForwardTailIsNotASignal:
    """The last window's forward RV is unrealized; a z-score built over a filled tail read as SHORT/LONG VOL."""

    def test_vrp_z_is_null_wherever_the_forward_rv_is(self):
        bars = _bars(400)
        iv_series = [{"ts": int(ts), "iv30": 0.20 + 0.01 * ((i * 7) % 5)} for i, ts in enumerate(bars.index)]

        body = _series(bars, iv_series, estimators=["ctc"], windows=[5])

        blind = [i for i, rv in enumerate(body["rv_hf_forward"]) if rv is None]
        assert blind, "the forward RV has an unrealized tail"
        assert blind[-1] == len(bars) - 1
        assert all(body["vrp_z"][i] is None for i in blind)
        assert any(z is not None for z in body["vrp_z"]), "earlier bars keep their z-score"
        assert body["coverage"]["forward_nan_bars"] == len(blind)
