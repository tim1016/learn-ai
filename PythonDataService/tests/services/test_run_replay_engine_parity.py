"""Engine-parity leg: BacktestEngine vs runner seam over one run's bars."""

from __future__ import annotations

from app.services.run_replay_proof import engine_parity_over_bars, to_trade_bar
from app.services.source_bar_ledger import RetainedSourceBar
from tests.services.bot_runner.conftest import _ema_parity_bars_through_first_exit


def _fixture_trade_bars() -> list:
    market_bars = _ema_parity_bars_through_first_exit()
    return [
        to_trade_bar(
            RetainedSourceBar.from_market_bar(seq=index + 1, account_id="paper:t", bar=bar)
        )
        for index, bar in enumerate(market_bars)
    ]


def test_engine_parity_over_bars_proves_the_shared_seam_on_real_bars() -> None:
    bars = _fixture_trade_bars()

    result = engine_parity_over_bars("ema_crossover_signal", "SPY", None, bars)

    assert result.divergence is None
    assert result.error is None
    assert result.trace_root is not None and len(result.trace_root) == 64
    assert result.compared_count > 0


def test_engine_parity_over_bars_reports_an_unsupported_program_as_error() -> None:
    result = engine_parity_over_bars("no-such-strategy", "SPY", None, [])

    assert result.trace_root is None
    assert result.divergence is None
    assert result.error is not None and "no-such-strategy" in result.error
