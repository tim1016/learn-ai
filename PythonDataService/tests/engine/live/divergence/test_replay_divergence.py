"""Tests for the Layer B ``ReplayDivergenceClassifier``.

Pure-function classification over a joined-bar + replayed-decision pair:
the right category at the right magnitude with the right tolerance and
severity. In-tolerance / boundary / out-of-tolerance per category, mirroring
the Layer A classifier pattern.
"""

from __future__ import annotations

from app.engine.live.artifacts import DecisionRow
from app.engine.live.divergence.bar_series_joiner import CanonicalBar, JoinedBar
from app.engine.live.divergence.common import Severity
from app.engine.live.divergence.replay_divergence import (
    ReplayDivergenceCategory,
    ReplayTolerances,
    classify_replay_divergences,
    classify_trade_graph_drift,
)


def _live(
    *,
    bar_close_ms: int = 1000,
    close: float = 100.0,
    signal: str = "HOLD",
    indicators: dict | None = None,
) -> DecisionRow:
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal=signal,
        intended_price=close,
        bar_source="ibkr_paper_delayed",
        bar_open=close,
        bar_high=close,
        bar_low=close,
        bar_close=close,
        bar_volume=1000.0,
        indicator_values=indicators or {},
    )


def _canonical(*, bar_close_ms: int = 1000, close: float = 100.0) -> CanonicalBar:
    return CanonicalBar(
        bar_close_ms=bar_close_ms,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


def _replayed(
    *, bar_close_ms: int = 1000, signal: str = "HOLD", indicators: dict | None = None
) -> DecisionRow:
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal=signal,
        intended_price=100.0,
        indicator_values=indicators or {},
    )


def test_close_price_drift_out_of_tolerance_emits_data_drift_c() -> None:
    # Live close 100.00, canonical close 100.05 → $0.05 drift > $0.01 tolerance.
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(close=100.00),
        canonical=_canonical(close=100.05),
        gap_side=None,
    )

    divergences = classify_replay_divergences(joined, _replayed(), ReplayTolerances())

    drift = [
        d for d in divergences if d.category is ReplayDivergenceCategory.DATA_DRIFT_C
    ]
    assert len(drift) == 1
    assert abs(drift[0].magnitude - 0.05) < 1e-9
    assert drift[0].applied_tolerance == ReplayTolerances().bar_value_atol
    # Delayed paper data has a baseline price floor — non-gating.
    assert drift[0].severity is Severity.NON_GATING
    assert drift[0].bar_close_ms == 1000


def test_open_high_low_drift_each_emit_their_own_category() -> None:
    live = DecisionRow(
        bar_close_ms=1000,
        signal="HOLD",
        intended_price=100.0,
        bar_source="ibkr_paper_delayed",
        bar_open=100.0,
        bar_high=101.0,
        bar_low=99.0,
        bar_close=100.0,
        bar_volume=1000.0,
    )
    canonical = CanonicalBar(
        bar_close_ms=1000,
        open=100.10,  # +0.10 drift
        high=101.20,  # +0.20 drift
        low=98.70,  # -0.30 drift
        close=100.0,
        volume=1000.0,
    )
    joined = JoinedBar(bar_close_ms=1000, live=live, canonical=canonical, gap_side=None)

    cats = {
        d.category for d in classify_replay_divergences(joined, _replayed(), ReplayTolerances())
    }
    assert ReplayDivergenceCategory.DATA_DRIFT_O in cats
    assert ReplayDivergenceCategory.DATA_DRIFT_H in cats
    assert ReplayDivergenceCategory.DATA_DRIFT_L in cats
    assert ReplayDivergenceCategory.DATA_DRIFT_C not in cats  # close matches


def test_volume_drift_beyond_share_tolerance_emits_data_drift_v() -> None:
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(close=100.0),  # bar_volume=1000.0
        canonical=CanonicalBar(
            bar_close_ms=1000, open=100.0, high=100.0, low=100.0, close=100.0, volume=1005.0
        ),
        gap_side=None,
    )

    divergences = classify_replay_divergences(joined, _replayed(), ReplayTolerances())

    vol = [d for d in divergences if d.category is ReplayDivergenceCategory.DATA_DRIFT_V]
    assert len(vol) == 1
    assert vol[0].magnitude == 5.0
    assert vol[0].applied_tolerance == ReplayTolerances().volume_atol


def test_indicator_state_drift_is_gating() -> None:
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(indicators={"ema5": 100.0, "ema10": 99.0}),
        canonical=_canonical(),
        gap_side=None,
    )
    replayed = _replayed(indicators={"ema5": 100.5, "ema10": 99.0})  # ema5 drifts

    divergences = classify_replay_divergences(joined, replayed, ReplayTolerances())

    drift = [
        d
        for d in divergences
        if d.category is ReplayDivergenceCategory.INDICATOR_STATE_DRIFT
    ]
    assert len(drift) == 1
    assert drift[0].severity is Severity.GATING
    assert abs(drift[0].magnitude - 0.5) < 1e-9


def test_identical_indicator_state_emits_no_drift() -> None:
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(indicators={"ema5": 100.0}),
        canonical=_canonical(),
        gap_side=None,
    )
    replayed = _replayed(indicators={"ema5": 100.0})

    divergences = classify_replay_divergences(joined, replayed, ReplayTolerances())

    assert not [
        d
        for d in divergences
        if d.category is ReplayDivergenceCategory.INDICATOR_STATE_DRIFT
    ]


def test_decision_drift_with_matching_indicators_is_gating() -> None:
    # Same indicator state, different signal → should be impossible if the math
    # is deterministic; a real bug.
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(signal="ENTER", indicators={"ema5": 100.0}),
        canonical=_canonical(),
        gap_side=None,
    )
    replayed = _replayed(signal="HOLD", indicators={"ema5": 100.0})

    divergences = classify_replay_divergences(joined, replayed, ReplayTolerances())

    drift = [
        d for d in divergences if d.category is ReplayDivergenceCategory.DECISION_DRIFT
    ]
    assert len(drift) == 1
    assert drift[0].severity is Severity.GATING


def test_decision_drift_not_flagged_when_indicator_drift_explains_it() -> None:
    # Indicators differ, so the signal difference is an expected downstream
    # consequence — flagged as INDICATOR_STATE_DRIFT, not DECISION_DRIFT.
    joined = JoinedBar(
        bar_close_ms=1000,
        live=_live(signal="ENTER", indicators={"ema5": 100.0}),
        canonical=_canonical(),
        gap_side=None,
    )
    replayed = _replayed(signal="HOLD", indicators={"ema5": 105.0})

    divergences = classify_replay_divergences(joined, replayed, ReplayTolerances())

    assert any(
        d.category is ReplayDivergenceCategory.INDICATOR_STATE_DRIFT for d in divergences
    )
    assert not [
        d for d in divergences if d.category is ReplayDivergenceCategory.DECISION_DRIFT
    ]


def test_coverage_gap_is_non_gating() -> None:
    joined = JoinedBar(
        bar_close_ms=2000, live=None, canonical=_canonical(bar_close_ms=2000), gap_side="live"
    )

    divergences = classify_replay_divergences(joined, None, ReplayTolerances())

    gap = [d for d in divergences if d.category is ReplayDivergenceCategory.COVERAGE_GAP]
    assert len(gap) == 1
    assert gap[0].severity is Severity.NON_GATING
    assert gap[0].bar_close_ms == 2000


def test_trade_graph_drift_from_diverging_exit_bar() -> None:
    # Both enter at 1000 but exit on different bars → the trade graphs diverge
    # by 2 nodes (EXIT@3000 vs EXIT@4000), past the 1-node tolerance.
    live = [_live(bar_close_ms=1000, signal="ENTER"), _live(bar_close_ms=3000, signal="EXIT")]
    replayed = [
        _replayed(bar_close_ms=1000, signal="ENTER"),
        _replayed(bar_close_ms=4000, signal="EXIT"),
    ]

    divergences = classify_trade_graph_drift(live, replayed, ReplayTolerances())

    assert len(divergences) == 1
    d = divergences[0]
    assert d.category is ReplayDivergenceCategory.TRADE_GRAPH_DRIFT
    assert d.severity is Severity.GATING
    assert d.magnitude == 2.0


def test_identical_trade_graphs_emit_no_drift() -> None:
    live = [_live(bar_close_ms=1000, signal="ENTER"), _live(bar_close_ms=3000, signal="EXIT")]
    replayed = [
        _replayed(bar_close_ms=1000, signal="ENTER"),
        _replayed(bar_close_ms=3000, signal="EXIT"),
    ]

    assert classify_trade_graph_drift(live, replayed, ReplayTolerances()) == []
