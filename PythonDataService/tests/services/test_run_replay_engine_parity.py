"""Engine-parity leg: BacktestEngine vs runner seam over one run's bars."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.sqlite import qualification_shadow_trace
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import run_shadow_trace_evaluation
from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import MarketDataBar
from app.services.run_replay_proof import engine_parity_over_bars, to_trade_bar
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms
from tests._helpers.bot_runner.ema_parity import (
    _ema_parity_bars_through_first_exit,
    _ema_signal_evaluation_id,
    lean_cell_bars,
)

#: The first session after every registered Signal Program's built-in backtest
#: window: EMA, SMA, RSI and the SPY strategies end theirs on 2026-03-27,
#: Deployment Validation on 2026-04-15. Every live run since then is dated here.
_AFTER_EVERY_BUILTIN_WINDOW = date(2026, 4, 16)

_SIGNAL_PROGRAM_KEYS = sorted(
    key for key, registration in _STRATEGY_REGISTRY.items() if registration.signal_program_factory is not None
)

#: The session whose final minute only the live seam sees with a raised close.
_PERTURBED_SESSION = date(2026, 4, 22)


def _with_raised_close(bar: TradeBar) -> TradeBar:
    close = bar.close + Decimal(5)
    return replace(bar, close=close, high=max(bar.high, close))


class _FeedWithOneRaisedClose(qualification_shadow_trace._QualifiedBarFeed):
    """The live seam's bar feed, with the perturbed session's final close raised."""

    def __init__(self, symbol: str, bars: Sequence[TradeBar]) -> None:
        raised_ms = session_close_ms_utc(_PERTURBED_SESSION)
        super().__init__(symbol, [_with_raised_close(bar) if bar.end_ms == raised_ms else bar for bar in bars])


def _trade_bars(market_bars: Sequence[MarketDataBar]) -> list[TradeBar]:
    return [
        to_trade_bar(
            RetainedSourceBar.from_market_bar(seq=index + 1, account_id="paper:t", bar=bar)
        )
        for index, bar in enumerate(market_bars)
    ]


def _fixture_trade_bars() -> list[TradeBar]:
    return _trade_bars(_ema_parity_bars_through_first_exit())


def _bars_after_every_builtin_window() -> list[TradeBar]:
    """SPY's retained LEAN minutes from 2026-04-16 through 2026-04-30."""
    market_bars = lean_cell_bars(
        "SPY_W3mo_2026-02-02_to_2026-04-30", symbol="SPY", stop_after_ms=MAX_TIMESTAMP_MS
    )
    return _trade_bars(
        [bar for bar in market_bars if et_date_at_ms(bar.start_ms) >= _AFTER_EVERY_BUILTIN_WINDOW]
    )


def test_engine_parity_over_bars_proves_the_shared_seam_on_real_bars() -> None:
    bars = _fixture_trade_bars()

    result = engine_parity_over_bars("ema_crossover_signal", "SPY", None, bars)

    assert result.divergence is None
    assert result.error is None
    assert result.trace_root is not None and len(result.trace_root) == 64
    assert result.compared_count > 0


@pytest.mark.parametrize("strategy_key", _SIGNAL_PROGRAM_KEYS)
def test_engine_parity_over_bars_compares_every_trace_of_a_run_after_the_builtin_window(
    strategy_key: str,
) -> None:
    """#2608: the reference backtest used to read only its strategy's built-in
    window, so a run dated after it was compared with an empty reference and
    diverged at index 0 ("reference sequence exhausted")."""
    bars = _bars_after_every_builtin_window()

    result = engine_parity_over_bars(strategy_key, "SPY", None, bars)

    assert result.divergence is None
    assert result.error is None
    assert result.compared_count > 0


def test_engine_parity_over_bars_reports_a_live_side_divergence_at_its_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bar the live seam saw differently is reported at that bar's own trace.

    Only the live side's feed is perturbed: perturbing the shared bar list
    would change both seams alike and hide the divergence.
    """
    bars = _bars_after_every_builtin_window()
    perturbed_close_ms = session_close_ms_utc(_PERTURBED_SESSION)
    baseline = asyncio.run(run_shadow_trace_evaluation("ema_crossover_signal", "SPY", None, bars))
    expected_index = next(
        index for index, trace in enumerate(baseline.traces) if trace.bar_close_ms == perturbed_close_ms
    )
    monkeypatch.setattr(qualification_shadow_trace, "_QualifiedBarFeed", _FeedWithOneRaisedClose)

    result = engine_parity_over_bars("ema_crossover_signal", "SPY", None, bars)

    assert result.divergence is not None
    assert result.divergence.index == expected_index
    assert result.divergence.evaluation_id == _ema_signal_evaluation_id(perturbed_close_ms)
    assert result.compared_count == expected_index


def test_engine_parity_over_bars_reports_an_unsupported_program_as_error() -> None:
    result = engine_parity_over_bars("no-such-strategy", "SPY", None, [])

    assert result.trace_root is None
    assert result.divergence is None
    assert result.error is not None and "no-such-strategy" in result.error
