"""Tests for the Layer B ``BarSeriesJoiner``.

Joins a live-decision bar series against canonical Polygon bars on
``bar_close_ms``. Asserts on the joined output — matched bars, coverage
gaps on each side, and that NO silent forward-fill or interpolation
happens (per ``.claude/rules/numerical-rigor.md``).
"""

from __future__ import annotations

from app.engine.live.artifacts import DecisionRow
from app.engine.live.divergence.bar_series_joiner import (
    CanonicalBar,
    join_bar_series,
)


def _live(bar_close_ms: int, close: float = 100.0) -> DecisionRow:
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal="HOLD",
        intended_price=close,
        bar_source="ibkr_paper_delayed",
        bar_open=close,
        bar_high=close,
        bar_low=close,
        bar_close=close,
        bar_volume=1000.0,
    )


def _canonical(bar_close_ms: int, close: float = 100.0) -> CanonicalBar:
    return CanonicalBar(
        bar_close_ms=bar_close_ms,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
    )


def test_fully_aligned_series_has_no_coverage_gaps() -> None:
    live = [_live(1000), _live(2000)]
    canonical = [_canonical(1000), _canonical(2000)]

    joined = join_bar_series(live, canonical)

    assert [j.bar_close_ms for j in joined] == [1000, 2000]
    assert all(j.live is not None and j.canonical is not None for j in joined)
    assert all(j.gap_side is None for j in joined)


def test_live_missing_a_bar_is_a_coverage_gap_on_the_live_side() -> None:
    live = [_live(1000)]
    canonical = [_canonical(1000), _canonical(2000)]

    joined = join_bar_series(live, canonical)

    gap = next(j for j in joined if j.bar_close_ms == 2000)
    assert gap.live is None
    assert gap.canonical is not None
    assert gap.gap_side == "live"


def test_canonical_missing_a_bar_is_a_coverage_gap_on_the_canonical_side() -> None:
    live = [_live(1000), _live(2000)]
    canonical = [_canonical(1000)]

    joined = join_bar_series(live, canonical)

    gap = next(j for j in joined if j.bar_close_ms == 2000)
    assert gap.canonical is None
    assert gap.live is not None
    assert gap.gap_side == "canonical"


def test_same_bar_different_ohlcv_carries_both_sides() -> None:
    live = [_live(1000, close=100.00)]
    canonical = [_canonical(1000, close=100.05)]

    joined = join_bar_series(live, canonical)

    assert len(joined) == 1
    j = joined[0]
    assert j.gap_side is None
    # No forward-fill / reconciliation — both raw values survive for the
    # classifier to compare.
    assert j.live.bar_close == 100.00
    assert j.canonical.close == 100.05


def test_no_forward_fill_for_missing_bars() -> None:
    # A gap on either side must never be filled with the neighbouring value.
    live = [_live(1000, close=100.0), _live(3000, close=102.0)]
    canonical = [_canonical(1000), _canonical(2000), _canonical(3000)]

    joined = join_bar_series(live, canonical)

    gap = next(j for j in joined if j.bar_close_ms == 2000)
    assert gap.live is None  # not back/forward-filled from 1000 or 3000
