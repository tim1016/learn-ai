"""A decision bar missing its final minute is still decided (#2909).

When the last minute of a 15-minute bucket never arrives, LEAN's
consolidator still emits the bucket — its ``EndTime`` is computed as
``Time + Period`` — and the algorithm evaluates it, priced at the last
minute that did arrive. This port's consolidator fired the same bucket but
stamped its end at the last contained minute's end, so the Signal Session
refused it as ``TIMEFRAME_MISMATCH`` and a backtest silently lost the
decision bar: the committed ``SPY_W3mo`` cell loses its 10:15 bar on
2026-03-18 when the 10:14 minute is removed (1,611 decisions instead of
1,612). The quarantine is drained and reported now, but a reported lost
decision is still a lost decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.programs.ema_previous_crossover import (
    EmaPreviousCrossoverParams,
    build_ema_previous_crossover_program,
)
from scripts.generate_signal_program_trace_corpus import _DEFAULT_CELLS_ROOT, _minute_bars
from tests.fixtures.golden_support.strategy_replay import staged_decisions

_CELL = "SPY_W3mo_2026-02-02_to_2026-04-30"

#: 2026-03-18 10:15 ET is 14:15 UTC (EDT): the end of the minute bar that
#: starts 10:14 — the one the issue removes.
_MISSING_MINUTE_END_MS = int(datetime(2026, 3, 18, 14, 15, tzinfo=UTC).timestamp() * 1000)

#: The minute before it: 10:13–10:14 ET, the last one that still arrives.
_LAST_SEEN_MINUTE_END_MS = _MISSING_MINUTE_END_MS - 60_000


def test_the_cell_replays_over_every_bucket():
    minute_bars = _minute_bars(Path(_DEFAULT_CELLS_ROOT) / _CELL, "SPY")
    program = build_ema_previous_crossover_program(EmaPreviousCrossoverParams())
    decisions = staged_decisions(program.strategy, minute_bars)
    assert len(decisions) == 1_612


def test_a_bucket_missing_its_last_minute_is_still_decided():
    minute_bars = [b for b in _minute_bars(Path(_DEFAULT_CELLS_ROOT) / _CELL, "SPY") if b.end_ms != _MISSING_MINUTE_END_MS]
    program = build_ema_previous_crossover_program(EmaPreviousCrossoverParams())
    decisions = staged_decisions(program.strategy, minute_bars)

    # The full count comes back: the 10:15 bar is evaluated, not quarantined.
    assert len(decisions) == 1_612
    assert program.take_quarantine() is None, "the short bucket was refused instead of decided"

    bar, _ = next((bar, d) for bar, d in decisions if bar.end_ms == _MISSING_MINUTE_END_MS)
    # LEAN prices the recovered bucket at the last minute that did arrive:
    # its close is the 10:13 minute's close, and its end is the bucket's own
    # boundary.
    last_seen_close = next(b.close for b in minute_bars if b.end_ms == _LAST_SEEN_MINUTE_END_MS)
    assert bar.close == last_seen_close
    assert bar.end_ms - bar.start_ms == 15 * 60_000


def test_a_missing_first_minute_fires_at_the_bucket_start():
    """A bucket missing its FIRST minute is the same rule read from the other
    end: the bar still opens at the bucket's rounded start."""
    minute_bars = _minute_bars(Path(_DEFAULT_CELLS_ROOT) / _CELL, "SPY")
    # Drop the first minute of one mid-cell bucket: 10:00–10:01 ET on 2026-03-18.
    first_minute_end = int(datetime(2026, 3, 18, 14, 1, tzinfo=UTC).timestamp() * 1000)
    kept = [b for b in minute_bars if b.end_ms != first_minute_end]
    program = build_ema_previous_crossover_program(EmaPreviousCrossoverParams())
    decisions = staged_decisions(program.strategy, kept)
    assert len(decisions) == 1_612
    bar: TradeBar | None = next((b for b, _ in decisions if b.end_ms == first_minute_end + 14 * 60_000), None)
    assert bar is not None
    assert bar.start_ms == first_minute_end - 60_000
