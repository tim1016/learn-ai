"""Measured outputs at the fixed seed-decay count (ADR 0083).

RSI/ADX ratios and Supertrend's latch do not have a price-independent output
bound. Pin their measured error, including the RSI example above 0.001,
instead of pretending the state-memory bound is an output guarantee.
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Callable
from decimal import Decimal
from functools import cache
from itertools import islice
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.adx import AverageDirectionalIndex
from app.engine.indicators.base import BarIndicator, Indicator
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.indicators.sma import SimpleMovingAverage
from app.engine.indicators.supertrend import Supertrend

GOLDEN = Path(__file__).resolve().parents[2] / 'fixtures/golden'
RECEIPT = GOLDEN / 'indicator-convergence/v1/measured.json'
REAL_BARS = GOLDEN / 'cross-engine-studies/cells/SPY_W12mo_2025-05-01_to_2026-04-30/lean/observations.csv'
FACTORIES: dict[str, Callable[[], Indicator | BarIndicator]] = {
    'sma30': lambda: SimpleMovingAverage('SMA', 30),
    'ema50': lambda: ExponentialMovingAverage('EMA', 50),
    'rsi14': lambda: RelativeStrengthIndex('RSI', 14),
    'adx14': lambda: AverageDirectionalIndex('ADX', 14),
    'macd12_26_9': lambda: MovingAverageConvergenceDivergence('MACD'),
    'supertrend10_3': lambda: Supertrend('ST'),
}


@cache
def twin_bars(source: str) -> list[TradeBar]:
    if source == 'real':
        with REAL_BARS.open() as handle:
            return [TradeBar(
                symbol='SPY', start_ms=int(row['ms_utc']) - 60_000, end_ms=int(row['ms_utc']),
                open=Decimal(row['open']), high=Decimal(row['high']), low=Decimal(row['low']),
                close=Decimal(row['close']), volume=int(Decimal(row['volume'])),
            ) for row in islice(csv.DictReader(handle), 16000)]
    rng = random.Random(2857)
    price = Decimal(500)
    bars = []
    for index in range(16000):
        close = price + Decimal(str(round(rng.gauss(0, 1), 4)))
        end = 1_700_000_000_000 + (index + 1) * 60_000
        bars.append(TradeBar(symbol='SPY', start_ms=end - 60_000, end_ms=end,
                             open=price, high=max(price, close) + 1,
                             low=min(price, close) - 1, close=close, volume=100))
        price = close
    return bars


def _update(indicator: Indicator | BarIndicator, bar: TradeBar) -> None:
    if isinstance(indicator, BarIndicator):
        indicator.update(bar)
    else:
        indicator.update(bar.end_ms, bar.close)


def _values(indicator: Indicator | BarIndicator) -> tuple[Decimal, ...]:
    if isinstance(indicator, MovingAverageConvergenceDivergence):
        return (indicator.macd or Decimal(0), indicator.signal or Decimal(0), indicator.histogram or Decimal(0))
    return (indicator.current_value or Decimal(0),)


def measure_twins(source: str, key: str, *, count: int | None = None) -> dict:
    factory = FACTORIES[key]
    warm = factory()
    count = warm.bars_to_converge() if count is None else count
    values, directions = [], []
    bars = twin_bars(source)
    for bar in bars:
        _update(warm, bar)
        values.append(_values(warm))
        directions.append(warm.direction if isinstance(warm, Supertrend) else None)
    initial = [Decimal(0)] * len(values[0])
    final = initial.copy()
    mismatches = 0
    starts = range(2000, 14500, 100)
    for start in starts:
        cold = factory()
        for offset, bar in enumerate(bars[start:start + count]):
            _update(cold, bar)
            if offset + 1 == cold.ready_bars:
                initial = [total + abs(a - b) for total, a, b in zip(initial, _values(cold), values[start + offset], strict=True)]
        last = start + count - 1
        final = [total + abs(a - b) for total, a, b in zip(final, _values(cold), values[last], strict=True)]
        if isinstance(cold, Supertrend):
            mismatches += cold.direction != directions[last]
    return {
        'bars': count, 'starts': len(starts),
        'mean_initial_error': [float(value / len(starts)) for value in initial],
        'mean_final_error': [float(value / len(starts)) for value in final],
        'error_ratios': [float(b / a) if a else 0.0 for a, b in zip(initial, final, strict=True)],
        'direction_mismatches': mismatches,
    }


@pytest.mark.parametrize('source', ['real', 'synthetic'])
@pytest.mark.parametrize('key', FACTORIES)
def test_count_has_pinned_twin_output_evidence(source: str, key: str) -> None:
    expected = json.loads(RECEIPT.read_text())[source][key]
    measured = measure_twins(source, key)
    assert measured['bars'] == expected['bars']
    assert measured['starts'] == expected['starts']
    assert measured['direction_mismatches'] == expected['direction_mismatches']
    for field in ('mean_initial_error', 'mean_final_error', 'error_ratios'):
        assert measured[field] == pytest.approx(expected[field], abs=1e-12, rel=0)
    if key == 'sma30':
        assert measured['mean_final_error'] == [0]
    if key == 'ema50':
        assert measured['error_ratios'][0] <= 0.001
    if source == 'real' and key == 'rsi14':
        assert measured['error_ratios'][0] > 0.001, 'ADR 0083 records this nonlinear counterexample'


@pytest.mark.parametrize('source', ['real', 'synthetic'])
def test_supertrend_margin_keeps_the_atr_only_counterexample_visible(source: str) -> None:
    expected = json.loads(RECEIPT.read_text())['supertrend_atr_only'][source]
    measured = measure_twins(source, 'supertrend10_3', count=76)
    assert measured['direction_mismatches'] == expected['direction_mismatches']
    assert measured['error_ratios'] == pytest.approx(expected['error_ratios'], abs=1e-12, rel=0)
