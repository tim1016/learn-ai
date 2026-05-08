"""Golden fixture validation for indicator fixtures (IND-001 through IND-003).

Tests that the canonical indicator classes produce values matching the
hand-computed oracle stored in each fixture, bar by bar.

EMA and SMA produce values at every bar (warmup is running mean).
RSI produces None (stored as NaN) for the first period bars, then floats.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_indicator_fixtures.py -v --noconftest
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.engine.indicators.ema import ExponentialMovingAverage  # noqa: E402
from app.engine.indicators.rsi import RelativeStrengthIndex  # noqa: E402
from app.engine.indicators.sma import SimpleMovingAverage  # noqa: E402

PERIOD = 3
N_BARS = 8
_BASE_TIME = datetime(2024, 1, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(fixture_id: str) -> tuple[pa.Table, pa.Table, float, float]:
    files = registry.active_files(fixture_id)
    fixture_dir = registry.fixture_dir(fixture_id)
    manifest_fixture = registry._manifest.by_id(fixture_id)
    inp = pa.ipc.open_file(fixture_dir / files.input).read_all()
    out = pa.ipc.open_file(fixture_dir / files.output).read_all()
    atol = manifest_fixture.tolerance.atol
    rtol = manifest_fixture.tolerance.rtol
    return inp, out, atol, rtol


def _prices(inp: pa.Table, row: int) -> list[float]:
    return [float(inp[f"p{i}"][row].as_py()) for i in range(N_BARS)]


def _oracle_vals(out: pa.Table, row: int) -> list[float | None]:
    vals = []
    for i in range(N_BARS):
        v = float(out[f"v{i}"][row].as_py())
        vals.append(None if math.isnan(v) else v)
    return vals


def _run_ema(prices: list[float]) -> list[float | None]:
    ind = ExponentialMovingAverage("ema", PERIOD)
    result: list[float | None] = []
    for i, p in enumerate(prices):
        t = _BASE_TIME + timedelta(seconds=i)
        ind.update(t, Decimal(str(p)))
        result.append(float(ind.current_value) if ind.current_value is not None else None)
    return result


def _run_sma(prices: list[float]) -> list[float | None]:
    ind = SimpleMovingAverage("sma", PERIOD)
    result: list[float | None] = []
    for i, p in enumerate(prices):
        t = _BASE_TIME + timedelta(seconds=i)
        ind.update(t, Decimal(str(p)))
        result.append(float(ind.current_value) if ind.current_value is not None else None)
    return result


def _run_rsi(prices: list[float]) -> list[float | None]:
    ind = RelativeStrengthIndex("rsi", PERIOD)
    result: list[float | None] = []
    for i, p in enumerate(prices):
        t = _BASE_TIME + timedelta(seconds=i)
        ind.update(t, Decimal(str(p)))
        result.append(float(ind.current_value) if ind.current_value is not None else None)
    return result


def _assert_sequence(canonical: list[float | None], oracle: list[float | None], atol: float, label: str) -> None:
    assert len(canonical) == len(oracle), f"{label}: length mismatch"
    for bar, (c, o) in enumerate(zip(canonical, oracle, strict=True)):
        if o is None:
            assert c is None, f"{label} bar {bar}: expected None, got {c}"
        else:
            assert c is not None, f"{label} bar {bar}: expected {o}, got None"
            assert abs(c - o) <= atol, (
                f"{label} bar {bar}: canonical={c} oracle={o} diff={abs(c-o):.2e} atol={atol:.2e}"
            )


# ---------------------------------------------------------------------------
# IND-001: EMA(period=3)
# ---------------------------------------------------------------------------


class TestIND001EMA:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IND-001")
        assert len(inp) == 3

    def test_ema_matches_oracle_all_cases(self) -> None:
        inp, out, atol, _rtol = _load("IND-001")
        for row in range(len(inp)):
            prices = _prices(inp, row)
            canonical = _run_ema(prices)
            oracle = _oracle_vals(out, row)
            _assert_sequence(canonical, oracle, atol, f"IND-001 row={row}")

    def test_ema_always_produces_value(self) -> None:
        """EMA produces a value at every bar (SMA warmup, not None)."""
        inp, _out, _atol, _rtol = _load("IND-001")
        for row in range(len(inp)):
            prices = _prices(inp, row)
            canonical = _run_ema(prices)
            for bar, v in enumerate(canonical):
                assert v is not None, f"Row {row} bar {bar}: EMA returned None"

    def test_ema_ready_at_period(self) -> None:
        ind = ExponentialMovingAverage("ema", PERIOD)
        for i in range(PERIOD - 1):
            ind.update(_BASE_TIME + timedelta(seconds=i), Decimal("10"))
            assert not ind.is_ready
        ind.update(_BASE_TIME + timedelta(seconds=PERIOD - 1), Decimal("10"))
        assert ind.is_ready

    def test_ema_monotone_case_exact(self) -> None:
        """For monotone prices, verify EMA seed and first post-warmup value."""
        # Case A: [10, 12, 14, 16, ...]
        ind = ExponentialMovingAverage("ema", PERIOD)
        for i, p in enumerate([10, 12, 14]):
            ind.update(_BASE_TIME + timedelta(seconds=i), Decimal(str(p)))
        # At samples=3: EMA = SMA(10,12,14) = 12
        assert abs(float(ind.current_value) - 12.0) < 1e-9

        # At samples=4: p=16, k=0.5 → EMA = 16*0.5 + 12*0.5 = 14
        ind.update(_BASE_TIME + timedelta(seconds=3), Decimal("16"))
        assert abs(float(ind.current_value) - 14.0) < 1e-9


# ---------------------------------------------------------------------------
# IND-002: SMA(period=3)
# ---------------------------------------------------------------------------


class TestIND002SMA:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IND-002")
        assert len(inp) == 3

    def test_sma_matches_oracle_all_cases(self) -> None:
        inp, out, atol, _rtol = _load("IND-002")
        for row in range(len(inp)):
            prices = _prices(inp, row)
            canonical = _run_sma(prices)
            oracle = _oracle_vals(out, row)
            _assert_sequence(canonical, oracle, atol, f"IND-002 row={row}")

    def test_sma_always_produces_value(self) -> None:
        inp, _out, _atol, _rtol = _load("IND-002")
        for row in range(len(inp)):
            prices = _prices(inp, row)
            canonical = _run_sma(prices)
            for bar, v in enumerate(canonical):
                assert v is not None, f"Row {row} bar {bar}: SMA returned None"

    def test_sma_ready_at_period(self) -> None:
        ind = SimpleMovingAverage("sma", PERIOD)
        for i in range(PERIOD - 1):
            ind.update(_BASE_TIME + timedelta(seconds=i), Decimal("10"))
            assert not ind.is_ready
        ind.update(_BASE_TIME + timedelta(seconds=PERIOD - 1), Decimal("10"))
        assert ind.is_ready

    def test_sma_rolling_window(self) -> None:
        """After warmup, SMA = mean of last 3 prices."""
        ind = SimpleMovingAverage("sma", PERIOD)
        prices = [10, 12, 14, 16]
        for i, p in enumerate(prices):
            ind.update(_BASE_TIME + timedelta(seconds=i), Decimal(str(p)))
        # At samples=4: SMA = mean([12, 14, 16]) = 14
        assert abs(float(ind.current_value) - 14.0) < 1e-9


# ---------------------------------------------------------------------------
# IND-003: RSI(period=3)
# ---------------------------------------------------------------------------


class TestIND003RSI:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IND-003")
        assert len(inp) == 3

    def test_rsi_matches_oracle_all_cases(self) -> None:
        inp, out, atol, _rtol = _load("IND-003")
        for row in range(len(inp)):
            prices = _prices(inp, row)
            canonical = _run_rsi(prices)
            oracle = _oracle_vals(out, row)
            _assert_sequence(canonical, oracle, atol, f"IND-003 row={row}")

    def test_rsi_nan_before_ready(self) -> None:
        """RSI(3) is None for bars 0..period (first period+1 bars)."""
        inp, out, _atol, _rtol = _load("IND-003")
        for row in range(len(inp)):
            oracle = _oracle_vals(out, row)
            # First bar (no delta) + first 2 accumulation bars = 3 Nones
            for bar in range(PERIOD):
                assert oracle[bar] is None, (
                    f"Row {row} bar {bar}: expected None, got {oracle[bar]}"
                )
            # Bar at index PERIOD should be non-None
            assert oracle[PERIOD] is not None, (
                f"Row {row} bar {PERIOD}: expected first RSI value, got None"
            )

    def test_rsi_ready_at_period_plus_one(self) -> None:
        ind = RelativeStrengthIndex("rsi", PERIOD)
        for i in range(PERIOD):
            ind.update(_BASE_TIME + timedelta(seconds=i), Decimal(str(10 + i)))
            assert not ind.is_ready
        ind.update(_BASE_TIME + timedelta(seconds=PERIOD), Decimal(str(10 + PERIOD)))
        assert ind.is_ready

    def test_rsi_bounded_zero_to_100(self) -> None:
        """RSI must be in [0, 100]."""
        inp, _out, _atol, _rtol = _load("IND-003")
        for row in range(len(inp)):
            canonical = _run_rsi(_prices(inp, row))
            for bar, v in enumerate(canonical):
                if v is not None:
                    assert 0.0 <= v <= 100.0, (
                        f"Row {row} bar {bar}: RSI={v} out of [0, 100]"
                    )

    def test_rsi_monotone_increasing_gives_100(self) -> None:
        """All gains → RSI = 100 (avg_loss = 0)."""
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        canonical = _run_rsi(prices)
        for bar in range(PERIOD, len(prices)):
            assert canonical[bar] == 100.0, (
                f"Bar {bar}: expected RSI=100 for all-gain series, got {canonical[bar]}"
            )
