"""Dual-field bar-timestamp regression test (Slice 7).

Pins the timestamp contract documented in
``docs/audits/bar-timestamp-rigor-2026-06-12.md`` for both 1-min and
5-second live bars:

1. ``start_ms`` and ``end_ms`` are int64 ms UTC.
2. ``end_ms - start_ms == window_ms`` for the resolution.
3. ``start_ms`` is aligned to the window.
4. Both fields survive a ``BarPersistence`` round-trip unchanged.

These are the invariants the chart card depends on for the candle
mapping ``time: (b.start_ms / 1000) as UTCTimestamp`` and the partial-
bar guard in ``LiveBarAggregator._pump``. A future drift here (e.g. a
new producer that writes ``end_ms`` in seconds, or a Pydantic v3
coercion that silently widens the int to a float) breaks the chart
without raising — this test makes the drift loud.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.services.bar_persistence import BarPersistence

RESOLUTIONS = [("1m", 60_000), ("5s", 5_000)]


def _bar(start_ms: int, window_ms: int, close: str = "100.00") -> IbkrMinuteBar:
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + window_ms,
        open=Decimal("100.00"),
        high=Decimal("100.50"),
        low=Decimal("99.50"),
        close=Decimal(close),
        volume=10,
        fetched_at_ms=start_ms + window_ms,
    )


@pytest.mark.parametrize("resolution,window_ms", RESOLUTIONS)
def test_dual_field_window_is_exact_for_resolution(resolution: str, window_ms: int) -> None:
    """Invariant (2): ``end_ms - start_ms`` equals the resolution's window."""
    start_ms = 1_775_001_600_000  # 2026-04-01 00:00:00 UTC, aligned to both 1m and 5s
    bar = _bar(start_ms, window_ms)
    assert bar.end_ms - bar.start_ms == window_ms
    # Invariant (1): both are real ints, not silently float-coerced.
    assert isinstance(bar.start_ms, int)
    assert isinstance(bar.end_ms, int)


@pytest.mark.parametrize("resolution,window_ms", RESOLUTIONS)
def test_start_ms_is_aligned_to_window(resolution: str, window_ms: int) -> None:
    """Invariant (3): ``start_ms`` lands on a window boundary. A producer
    that emitted a misaligned start would be caught here, not silently
    rendered as a candle that shifts the visible time axis."""
    start_ms = 1_775_001_600_000
    bar = _bar(start_ms, window_ms)
    assert bar.start_ms % window_ms == 0


@pytest.mark.parametrize("resolution,window_ms", RESOLUTIONS)
def test_dual_fields_survive_persistence_round_trip(
    tmp_path, resolution: str, window_ms: int
) -> None:
    """Invariant (4): persistence round-trip preserves both ms fields exactly.

    A regression where Pydantic serialized ``end_ms`` as a string (the
    ``Decimal``-on-OHLC pattern), or where the JSONL reader rounded ms
    through ``float``, would surface as ``end_ms - start_ms != window_ms``
    after a replay. That would corrupt the partial-bar guard in
    ``LiveBarAggregator._pump`` — the guard reads the window from the
    persisted bar.
    """
    start_ms = 1_775_001_600_000
    bar = _bar(start_ms, window_ms)

    store = BarPersistence(root=tmp_path)
    store.append("SPY", resolution, bar)
    replayed = store.replay("SPY", resolution, date(2026, 4, 1))

    assert len(replayed) == 1
    rt = replayed[0]
    assert rt.start_ms == bar.start_ms
    assert rt.end_ms == bar.end_ms
    assert rt.end_ms - rt.start_ms == window_ms
    assert isinstance(rt.start_ms, int)
    assert isinstance(rt.end_ms, int)


@pytest.mark.parametrize("resolution,window_ms", RESOLUTIONS)
def test_monotonic_sequence_preserves_window_for_every_bar(
    tmp_path, resolution: str, window_ms: int
) -> None:
    """A run of N consecutive bars round-trips through persistence with each
    bar's dual-field invariant intact — guards against an interleaving bug
    where one bar's end_ms was silently borrowed from the next."""
    start_ms = 1_775_001_600_000
    bars = [_bar(start_ms + i * window_ms, window_ms, close=f"100.{i:02d}") for i in range(5)]

    store = BarPersistence(root=tmp_path)
    for b in bars:
        store.append("SPY", resolution, b)
    replayed = store.replay("SPY", resolution, date(2026, 4, 1))

    assert len(replayed) == len(bars)
    for original, rt in zip(bars, replayed, strict=True):
        assert rt.start_ms == original.start_ms
        assert rt.end_ms == original.end_ms
        assert rt.end_ms - rt.start_ms == window_ms
        assert rt.start_ms % window_ms == 0
