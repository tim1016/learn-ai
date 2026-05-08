"""Golden fixture validation for extended engine-statistics fixtures (ENG-002 through ENG-005).

Tests that each canonical statistics.py function matches the hand-computed oracle
stored in each fixture, at the tolerance pinned in manifest.json.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_engine_stats_extended_fixtures.py -v --noconftest
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.engine.results.statistics import (  # noqa: E402
    EquityPoint,
    _max_drawdown,
    compute_portfolio_statistics,
    compute_trade_statistics,
)

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


# ---------------------------------------------------------------------------
# ENG-002: Max Drawdown
# ---------------------------------------------------------------------------


class TestENG002MaxDrawdown:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("ENG-002")
        assert len(inp) == 3

    def test_mdd_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("ENG-002")
        oracle = [out["oracle_mdd"][i].as_py() for i in range(len(out))]
        canonical = []
        for i in range(len(inp)):
            curve = [inp[f"e{j}"][i].as_py() for j in range(5)]
            canonical.append(_max_drawdown(curve))
        np.testing.assert_allclose(canonical, oracle, atol=atol, rtol=rtol)

    def test_mdd_bounded_zero_one(self) -> None:
        inp, _out, _atol, _rtol = _load("ENG-002")
        for i in range(len(inp)):
            curve = [inp[f"e{j}"][i].as_py() for j in range(5)]
            mdd = _max_drawdown(curve)
            assert 0.0 <= mdd <= 1.0, f"Row {i}: MDD={mdd} out of [0,1]"

    def test_empty_curve_returns_zero(self) -> None:
        assert _max_drawdown([]) == 0.0

    def test_monotone_increasing_zero_mdd(self) -> None:
        assert _max_drawdown([100.0, 110.0, 120.0, 130.0]) == 0.0


# ---------------------------------------------------------------------------
# ENG-003: Trade Statistics
# ---------------------------------------------------------------------------


@dataclass
class _SimpleTrade:
    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str


class TestENG003TradeStats:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("ENG-003")
        assert len(inp) == 3

    def _make_trades(self, inp: pa.Table, row: int) -> list[_SimpleTrade]:
        trades = []
        for col in ["t0_pnl_pct", "t1_pnl_pct", "t2_pnl_pct", "t3_pnl_pct"]:
            pct = float(inp[col][row].as_py())
            result = "WIN" if pct > 0 else ("LOSS" if pct < 0 else "BREAKEVEN")
            trades.append(_SimpleTrade(
                pnl_pts=Decimal(str(pct * 100)),
                pnl_pct=Decimal(str(pct)),
                result=result,
            ))
        return trades

    def test_total_trades(self) -> None:
        inp, out, _atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert stats.total_trades == int(out["total_trades"][i].as_py())

    def test_winning_losing_trades(self) -> None:
        inp, out, _atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert stats.winning_trades == int(out["winning_trades"][i].as_py())
            assert stats.losing_trades == int(out["losing_trades"][i].as_py())

    def test_win_rate(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.win_rate - float(out["win_rate"][i].as_py())) <= atol

    def test_avg_win_loss(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.avg_win_pct - float(out["avg_win_pct"][i].as_py())) <= atol
            assert abs(stats.avg_loss_pct - float(out["avg_loss_pct"][i].as_py())) <= atol

    def test_profit_factor(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.profit_factor - float(out["profit_factor"][i].as_py())) <= atol

    def test_payoff_ratio(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.payoff_ratio - float(out["payoff_ratio"][i].as_py())) <= atol

    def test_avg_trade_matches_expectancy(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.avg_trade_pct - float(out["avg_trade_pct"][i].as_py())) <= atol
            assert abs(stats.expectancy_pct - float(out["expectancy_pct"][i].as_py())) <= atol

    def test_largest_win_loss(self) -> None:
        inp, out, atol, _rtol = _load("ENG-003")
        for i in range(len(inp)):
            trades = self._make_trades(inp, i)
            stats = compute_trade_statistics(trades)
            assert abs(stats.largest_win_pct - float(out["largest_win_pct"][i].as_py())) <= atol
            assert abs(stats.largest_loss_pct - float(out["largest_loss_pct"][i].as_py())) <= atol

    def test_empty_trades_returns_zeros(self) -> None:
        stats = compute_trade_statistics([])
        assert stats.total_trades == 0
        assert stats.win_rate == 0.0
        assert stats.profit_factor == 0.0


# ---------------------------------------------------------------------------
# ENG-004: CAGR
# ---------------------------------------------------------------------------


def _build_equity_curve(e_list: list[float]) -> list[EquityPoint]:
    """Build a minimal EquityPoint curve with monotone timestamps."""
    base = datetime(2024, 1, 1)
    return [
        EquityPoint(timestamp=base + timedelta(days=i), equity=e)
        for i, e in enumerate(e_list)
    ]


class TestENG004CAGR:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("ENG-004")
        assert len(inp) == 3

    def test_cagr_matches_oracle(self) -> None:
        inp, out, atol, _rtol = _load("ENG-004")
        for i in range(len(inp)):
            initial = float(inp["initial_cash"][i].as_py())
            final = float(inp["final_equity"][i].as_py())
            days = int(inp["trading_days"][i].as_py())
            oracle_cagr = float(out["oracle_cagr"][i].as_py())

            # canonical only computes cagr when max_dd > 0, so use a 3-point
            # curve with a dip: initial → 80% of initial → final.
            # The cagr formula uses the `final_equity` parameter, not the
            # last equity curve point, so this correctly exercises the formula.
            dip = initial * 0.8
            curve = _build_equity_curve([initial, dip, final])
            stats = compute_portfolio_statistics(
                initial_cash=initial,
                final_equity=final,
                trades=[],
                trading_days=days,
                equity_curve=curve,
            )
            assert stats.cagr is not None, f"Row {i}: cagr unexpectedly None"
            assert abs(stats.cagr - oracle_cagr) <= atol, (
                f"Row {i}: canonical CAGR={stats.cagr} oracle={oracle_cagr}"
            )

    def test_cagr_sign_matches_performance(self) -> None:
        """CAGR positive when final > initial, negative when final < initial."""
        inp, out, _atol, _rtol = _load("ENG-004")
        for i in range(len(inp)):
            initial = float(inp["initial_cash"][i].as_py())
            final = float(inp["final_equity"][i].as_py())
            oracle_cagr = float(out["oracle_cagr"][i].as_py())
            if final > initial:
                assert oracle_cagr > 0
            elif final < initial:
                assert oracle_cagr < 0
            else:
                assert oracle_cagr == 0.0


# ---------------------------------------------------------------------------
# ENG-005: Calmar Ratio
# ---------------------------------------------------------------------------


class TestENG005Calmar:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("ENG-005")
        assert len(inp) == 3

    def test_calmar_matches_oracle(self) -> None:
        inp, out, atol, _rtol = _load("ENG-005")
        for i in range(len(inp)):
            initial = float(inp["initial_cash"][i].as_py())
            final = float(inp["final_equity"][i].as_py())
            days = int(inp["trading_days"][i].as_py())
            curve_vals = [float(inp[f"e{j}"][i].as_py()) for j in range(5)]
            oracle_calmar = float(out["oracle_calmar"][i].as_py())

            equity_curve = _build_equity_curve(curve_vals)
            stats = compute_portfolio_statistics(
                initial_cash=initial,
                final_equity=final,
                trades=[],
                trading_days=days,
                equity_curve=equity_curve,
            )
            assert stats.calmar_ratio is not None, f"Row {i}: calmar_ratio unexpectedly None"
            assert abs(stats.calmar_ratio - oracle_calmar) <= atol, (
                f"Row {i}: canonical calmar={stats.calmar_ratio} oracle={oracle_calmar}"
            )

    def test_calmar_equals_cagr_over_mdd(self) -> None:
        """Calmar = CAGR / max_drawdown by definition."""
        inp, out, atol, _rtol = _load("ENG-005")
        for i in range(len(inp)):
            initial = float(inp["initial_cash"][i].as_py())
            final = float(inp["final_equity"][i].as_py())
            days = int(inp["trading_days"][i].as_py())
            curve_vals = [float(inp[f"e{j}"][i].as_py()) for j in range(5)]

            mdd = _max_drawdown(curve_vals)
            years = days / 252
            cagr = (final / initial) ** (1 / years) - 1
            expected = cagr / mdd

            oracle_calmar = float(out["oracle_calmar"][i].as_py())
            assert abs(expected - oracle_calmar) <= atol, (
                f"Row {i}: hand-computed calmar={expected} vs oracle={oracle_calmar}"
            )
