"""The Realized vs IV series composition and where its IV comes from (ADR 0071 decisions 9 and 11; #3525).

These were the route-level tests of ``POST /api/edge/realized-vs-iv/series``, which #3525 retired when the study began
reading admitted lake bars. They keep the same assertions at the seams that exist now:

* ``recorded_iv_points`` and ``load_iv_input`` (``app.services.realized_vs_iv_study_service``) read the recorder;
* ``compute_realized_vs_iv_series`` (``app.engine.edge.realized_vs_iv_series``) composes the series.

The lake-sourced study around them is tested in ``tests/research/test_realized_vs_iv_study.py``.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.features_realtime.realized_vol import yang_zhang
from app.engine.edge.labels_oracle.forward_rv import forward_rv
from app.engine.edge.realized_vs_iv_series import _parse_iv_series, compute_realized_vs_iv_series
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.services.iv_recorder import InMemoryIvSnapshotStore, RecordedIvSnapshot, get_iv_store, set_iv_store
from app.services.realized_vs_iv_study_service import BAR_MINUTES, load_iv_input, recorded_iv_points, resolve_window
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
        "bars_per_session": 1,
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


class TestMissingIvStaysAbsentInTheZScore:
    """No IV is carried from a bar that has some to a bar that has none, not even into the VRP z-score (R25; ADR 0071 decision 9).

    The z-score is a rolling statistic over ``coverage.vrp_lookback`` bars that needs a VRP value on every one of them, so it
    exists only where recorded IV covers the whole window. Until #3525's review (M1) the z-score's IV input was
    forward-filled, which put a dense z-score and long/short-vol signals on bars that had no IV, built on a snapshot
    taken hundreds of bars earlier.
    """

    def test_iv_recorded_on_the_first_40_of_500_bars_gives_no_z_score_or_signal_on_the_bars_without_iv(self):
        bars = _bars(500)
        iv_series = [{"ts": int(ts), "iv30": 0.20 + 0.01 * ((i * 7) % 5)} for i, ts in enumerate(bars.index[:40])]

        body = _series(bars, iv_series, estimators=["ctc"], windows=[5])

        without_iv = [i for i, iv in enumerate(body["iv30"]) if iv is None]
        assert without_iv == list(range(40, 500)), "IV is on the first 40 bars and nowhere else"
        assert all(body["vrp_z"][i] is None for i in without_iv), "a z-score on a bar with no IV is a carried IV"
        # What the page turns a z-score into: past 1 sigma either way is a trade signal.
        assert not [i for i in without_iv if body["vrp_z"][i] is not None and abs(body["vrp_z"][i]) > 1.0]
        assert body["coverage"]["vrp_lookback"] == 252

    def test_the_lookback_the_series_reports_is_the_window_the_z_score_needs(self):
        assert _series(_bars(500), None)["coverage"]["vrp_lookback"] == 252
        assert _series(_bars(60), None)["coverage"]["vrp_lookback"] == 59, "a short window shrinks it to its own length less one"

    def test_a_gap_inside_dense_iv_blanks_every_window_it_touches_instead_of_being_filled(self):
        bars = _bars(500)
        gap = {300, 301, 302}
        iv_series = [
            {"ts": int(ts), "iv30": 0.20 + 0.01 * ((i * 7) % 5)} for i, ts in enumerate(bars.index) if i not in gap
        ]

        body = _series(bars, iv_series, estimators=["ctc"], windows=[5])

        lookback = body["coverage"]["vrp_lookback"]
        assert [i for i, iv in enumerate(body["iv30"]) if iv is None] == sorted(gap)
        before = [i for i, z in enumerate(body["vrp_z"]) if z is not None and i < min(gap)]
        assert before, "bars whose whole window has IV keep their z-score"
        assert all(i >= lookback - 1 for i in before), "and only once the window is full"
        touching = [i for i in range(min(gap), min(gap) + lookback + len(gap)) if i < len(bars)]
        assert all(body["vrp_z"][i] is None for i in touching), "every window that contains a bar without IV has no z-score"



def _session_bars(days: list[date], *, per_session: int, head_ms: int, seed: int = 7) -> pd.DataFrame:
    """OHLCV on the 15-minute grid of each session: ``per_session`` bars from ``head_ms`` before the open."""
    rng = np.random.default_rng(seed)
    starts = [session_open_ms_utc(day) - head_ms + k * 900_000 for day in days for k in range(per_session)]
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, size=len(starts))))
    return pd.DataFrame(
        {"open": closes * 0.9995, "high": closes * 1.002, "low": closes * 0.998, "close": closes, "volume": 1_000.0},
        index=pd.Index(starts, name="ts", dtype=np.int64),
    )


EXTENDED_HEAD_MS = 330 * 60_000  # the extended session opens 04:00 ET, 330 minutes before the open
SESSIONS_30 = expected_sessions(date(2024, 5, 1), date(2024, 6, 20))[:30]


class TestAnnualizationFollowsTheSessionTheBarsWereFoldedFrom:
    """A year is 252 sessions of the bars a session holds (26 for regular hours, 64 for extended), not 26 whatever the session (#3525)."""

    def test_an_extended_run_is_annualized_at_64_bars_a_session_and_a_regular_run_at_26(self):
        extended = _session_bars(SESSIONS_30, per_session=64, head_ms=EXTENDED_HEAD_MS)
        regular = _session_bars(SESSIONS_30, per_session=26, head_ms=0)

        for bars, per_session in ((extended, 64), (regular, 26)):
            body = _series(bars, None, bar_size="15m", bars_per_session=per_session, estimators=["yz"], windows=[5, 10])
            for window in (5, 10):
                expected_trailing = yang_zhang(bars, window=window, annualize=True, bars_per_year=252 * per_session)
                expected_forward = forward_rv(bars, estimator="yz", window=window, annualize=True, bars_per_year=252 * per_session)
                for key, expected in ((body["rv_trailing"], expected_trailing), (body["rv_forward"], expected_forward)):
                    ours = np.array([np.nan if v is None else v for v in key[f"yz_{window}"]])
                    assert np.isnan(ours).tolist() == expected.isna().tolist()
                    np.testing.assert_allclose(ours, expected.to_numpy(), atol=1e-9, rtol=0, equal_nan=True)

    def test_counting_an_extended_run_at_26_bars_a_session_understates_it_by_the_square_root_of_26_over_64(self):
        """The bug this replaces: the same extended bars annualized at 26 read 0.64 times the right figure."""
        extended = _session_bars(SESSIONS_30, per_session=64, head_ms=EXTENDED_HEAD_MS)

        right = _series(extended, None, bar_size="15m", bars_per_session=64, estimators=["yz"], windows=[5])["rv_trailing"]["yz_5"]
        wrong = _series(extended, None, bar_size="15m", bars_per_session=26, estimators=["yz"], windows=[5])["rv_trailing"]["yz_5"]

        pairs = [(a, b) for a, b in zip(right, wrong, strict=True) if a is not None and b is not None]
        assert pairs
        for a, b in pairs:
            np.testing.assert_allclose(b / a, np.sqrt(26 / 64), atol=1e-12, rtol=0)
        assert np.sqrt(26 / 64) == pytest.approx(0.637, abs=5e-4)

    def test_the_hf_estimator_does_not_depend_on_the_count_it_counts_the_sessions_itself(self):
        extended = _session_bars(SESSIONS_30, per_session=64, head_ms=EXTENDED_HEAD_MS)

        a = _series(extended, None, bar_size="15m", bars_per_session=64)
        b = _series(extended, None, bar_size="15m", bars_per_session=26)

        assert a["rv_hf_trailing"] == b["rv_hf_trailing"] and a["rv_hf_forward"] == b["rv_hf_forward"]

    @pytest.mark.parametrize(("bar_size", "bars_per_session"), [("1d", 26), ("1d", 0), ("15m", 0), ("15m", -3)])
    def test_a_count_that_cannot_describe_the_bars_is_refused(self, bar_size, bars_per_session):
        with pytest.raises(ValueError, match=r"a session, got"):
            _series(_bars(40), None, bar_size=bar_size, bars_per_session=bars_per_session)


class TestTheDefaultWindowCanShowAZScore:
    """The default window holds the fewest bars a z-score needs; one session fewer holds too few (#3525).

    The z-score exists only with IV on every bar of its 252-bar window, so these runs give every bar an IV.
    """

    NOW = 1_735_689_600_000  # 2025-01-01

    @staticmethod
    def _iv_on_every_bar(bars: pd.DataFrame) -> list[dict]:
        return [{"ts": int(ts), "iv30": 0.20 + 0.01 * ((i * 7) % 5)} for i, ts in enumerate(bars.index)]

    def test_the_daily_default_window_has_a_z_score_on_its_last_bar_that_has_a_forward_value(self):
        start, end = resolve_window("1d", "regular", self.NOW)
        sessions = expected_sessions(start, end)
        bars = _bars(len(sessions))

        body = _series(bars, self._iv_on_every_bar(bars), estimators=["ctc"], windows=[5])
        shorter = _bars(len(sessions) - 1)
        shorter_body = _series(shorter, self._iv_on_every_bar(shorter), estimators=["ctc"], windows=[5])

        assert len(sessions) == 273
        assert [i for i, z in enumerate(body["vrp_z"]) if z is not None] == [251], "exactly the bar 252 valid bars end on"
        assert all(z is None for z in shorter_body["vrp_z"]), "one session fewer shows none"

    @pytest.mark.parametrize(
        ("policy", "per_session", "head_ms", "hf_session"),
        [("regular", 26, 0, "RTH"), ("extended", 64, EXTENDED_HEAD_MS, "ETH")],
    )
    def test_the_fifteen_minute_default_window_has_a_z_score_when_every_bar_has_an_iv(self, policy, per_session, head_ms, hf_session):
        start, end = resolve_window("15m", policy, self.NOW)  # type: ignore[arg-type]
        sessions = expected_sessions(start, end)
        # The calendar's own bar count for these sessions must equal the grid built here (no early close in the window).
        assert BAR_MINUTES["15m"] == 15
        bars = _session_bars(sessions, per_session=per_session, head_ms=head_ms)
        shorter = _session_bars(sessions[1:], per_session=per_session, head_ms=head_ms)

        body = _series(bars, self._iv_on_every_bar(bars), bar_size="15m", bars_per_session=per_session, hf_session=hf_session, estimators=["ctc"], windows=[5])
        shorter_body = _series(shorter, self._iv_on_every_bar(shorter), bar_size="15m", bars_per_session=per_session, hf_session=hf_session, estimators=["ctc"], windows=[5])

        assert any(z is not None for z in body["vrp_z"])
        assert all(z is None for z in shorter_body["vrp_z"])
