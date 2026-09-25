"""Synthetic, service-free evidence for independent review ticket 2416.

Run through control/guarded_run.py from the isolated PythonDataService directory.
No application fixes, vendor data, database, broker state, or environment files.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.engine.results.statistics import EquityPoint, summarize
from app.research.walk_forward_study.verdict import FoldEvidence, compute_verdict


def timestamp(day: int, hour: int = 20) -> int:
    return int(datetime(2025, 1, day, hour, tzinfo=UTC).timestamp() * 1000)


def first_session_omission() -> dict[str, object]:
    """The first session's loss disappears even when its opening value exists."""
    returns = [-0.20, 0.01, 0.02, -0.01, 0.01]
    equity = 100_000.0
    points = [EquityPoint(timestamp(6, 15), equity)]
    trades = []
    for day, daily_return in zip(range(6, 11), returns, strict=True):
        equity *= 1 + daily_return
        points.append(EquityPoint(timestamp(day), equity))
        trades.append(SimpleNamespace(pnl_pts=Decimal(str(daily_return)),
                                      pnl_pct=Decimal(str(daily_return)),
                                      result='WIN' if daily_return > 0 else 'LOSS',
                                      entry_time_ms=timestamp(day, 15), exit_time_ms=timestamp(day),
                                      is_synthetic_exit=False))
    result = summarize(100_000.0, equity, trades, trading_days=5, equity_curve=points)
    correct_sharpe = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
    actual_sharpe = result['sharpe_ratio']
    assert actual_sharpe is not None and actual_sharpe > 0 > correct_sharpe
    # Trace the exact scalar and trade count consumed by the Walk-Forward verdict.
    verdict = compute_verdict([FoldEvidence(0, 'completed', 1.0, actual_sharpe, result['total_trades'])], min_trades=5)
    corrected = compute_verdict([FoldEvidence(0, 'completed', 1.0, correct_sharpe, result['total_trades'])], min_trades=5)
    assert verdict.label == 'still worked' and corrected.label == 'stopped working'
    return {'net_profit_pct': result['net_profit_pct'], 'reported_sharpe': actual_sharpe,
            'all_sessions_sharpe': correct_sharpe, 'verdict': verdict.label,
            'all_sessions_verdict': corrected.label}


def compatibility_drawdown() -> dict[str, object]:
    """Same closed trades/terminal value; an intra-trade drawdown is discarded."""
    trade_returns = [0.01, -0.01, 0.02]
    trades = [SimpleNamespace(pnl_pts=Decimal(str(r)), pnl_pct=Decimal(str(r)),
                             result='WIN' if r > 0 else 'LOSS',
                             entry_time_ms=timestamp(6 + i, 15), exit_time_ms=timestamp(6 + i),
                             is_synthetic_exit=False)
              for i, r in enumerate(trade_returns)]
    final_equity = 100_000.0 * 1.01 * 0.99 * 1.02
    points = [EquityPoint(timestamp(6, 14), 100_000), EquityPoint(timestamp(6, 15), 50_000),
              EquityPoint(timestamp(6), 101_000), EquityPoint(timestamp(7), 99_990),
              EquityPoint(timestamp(8), final_equity)]
    full = summarize(100_000, final_equity, trades, trading_days=3, equity_curve=points)
    paired = summarize(100_000, final_equity, trades, trading_days=3, equity_curve=None)
    assert math.isclose(full['max_drawdown_pct'], 0.5, abs_tol=1e-12, rel_tol=0)
    assert math.isclose(paired['max_drawdown_pct'], 0.01, abs_tol=1e-12, rel_tol=0)
    return {'python_only_max_drawdown': full['max_drawdown_pct'],
            'paired_profile_max_drawdown': paired['max_drawdown_pct'],
            'same_final_equity': final_equity}


def spec_empty_source() -> dict[str, object]:
    """Exercise the real spec route and its default reader against an empty root."""
    from app.engine.strategy.spec import StrategySpec
    from app.routers.spec_strategy import SpecBacktestRequest, _default_data_source_factory, run_spec_backtest

    root = Path(__file__).resolve().parents[1]
    fixture = root / 'PythonDataService/app/engine/strategy/spec/fixtures/sma_crossover.spec.json'
    spec = StrategySpec.model_validate_json(fixture.read_text())
    with tempfile.TemporaryDirectory(prefix='spec-empty-', dir=root / '.review-tmp') as directory:
        os.environ['LEAN_DATA_ROOT'] = directory
        os.environ.pop('LEAN_DATA_CACHE', None)
        request = SpecBacktestRequest(spec=spec, start_date='2025-01-06', end_date='2025-01-10')
        result = run_spec_backtest(request, data_source_factory=_default_data_source_factory)
        assert result.success and result.total_trades == 0 and result.error is None
        assert result.final_equity == request.initial_cash
        return {'success': result.success, 'error': result.error, 'total_trades': result.total_trades,
                'final_equity': result.final_equity, 'log_lines': result.log_lines}


if __name__ == '__main__':
    evidence = {'first_session_omission': first_session_omission(),
                'compatibility_drawdown': compatibility_drawdown(),
                'spec_empty_source': spec_empty_source()}
    sys.stdout.write(json.dumps(evidence, indent=2) + '\n')
