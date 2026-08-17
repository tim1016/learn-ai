"""Recency Chart statistics — Python-authored, per AGENTS.md #5.

These are the numbers the Angular swimlane renders but never computes:
per-trade dollar PnL, holding-session count (calendar-derived), a combo's
windowed total PnL (the hero-selection metric), and a combo's Sharpe (the
opacity-encoding metric). Every formula is defined here and documented in
docs/math-sources-of-truth.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.research.recency.stats import (
    HeroCandidate,
    HeroTrade,
    TradeForStats,
    holding_sessions,
    select_window_heroes,
    sharpe,
    total_pnl,
    trade_dollar_pnl,
)


def _ms(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp() * 1000)


def _trade(entry_ms: int, exit_ms: int, pnl_pts: float, pnl_pct: float, quantity: int = 10) -> TradeForStats:
    return TradeForStats(entry_ms=entry_ms, exit_ms=exit_ms, pnl_pts=pnl_pts, pnl_pct=pnl_pct, quantity=quantity)


class TestHoldingSessions:
    def test_same_day_entry_and_exit_is_one_session(self) -> None:
        entry = _ms(2026, 6, 10, 14, 30)  # Wed, during RTH in UTC terms
        exit_ = _ms(2026, 6, 10, 19, 0)
        assert holding_sessions(entry, exit_) == 1

    def test_monday_to_wednesday_spans_three_sessions(self) -> None:
        # 2026-06-08 Mon, 06-09 Tue, 06-10 Wed — no holiday in between.
        entry = _ms(2026, 6, 8, 14, 30)
        exit_ = _ms(2026, 6, 10, 19, 0)
        assert holding_sessions(entry, exit_) == 3

    def test_spans_a_weekend_without_counting_weekend_days(self) -> None:
        # Fri 2026-06-05 -> Mon 2026-06-08: only Fri + Mon are sessions.
        entry = _ms(2026, 6, 5, 14, 30)
        exit_ = _ms(2026, 6, 8, 19, 0)
        assert holding_sessions(entry, exit_) == 2

    def test_spans_a_holiday_without_counting_it(self) -> None:
        # 2026-07-03 (Fri, observed Independence Day holiday) is not a
        # session; Thu 07-02 and Mon 07-06 are the two sessions.
        entry = _ms(2026, 7, 2, 14, 30)
        exit_ = _ms(2026, 7, 6, 19, 0)
        assert holding_sessions(entry, exit_) == 2


class TestTradeDollarPnl:
    def test_multiplies_points_by_quantity(self) -> None:
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        assert trade_dollar_pnl(trade) == 25.0

    def test_negative_points_produce_negative_dollar_pnl(self) -> None:
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=-1.5, pnl_pct=-0.015, quantity=4)
        assert trade_dollar_pnl(trade) == -6.0

    def test_subtracts_two_flat_commissions_for_the_round_trip(self) -> None:
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        assert trade_dollar_pnl(trade, commission_per_order=1.5) == pytest.approx(25.0 - 3.0, abs=1e-9, rel=0)

    def test_zero_commission_matches_the_gross_default(self) -> None:
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        assert trade_dollar_pnl(trade, commission_per_order=0.0) == trade_dollar_pnl(trade)

    def test_matches_the_canonical_engine_flat_fee_formula(self) -> None:
        """Parity with app.routers.engine._persisted_trade_net_pnl's flat-fee
        branch (compatibility_profile=None) — the codebase's one canonical
        formula for round-trip net PnL under a flat per-order commission
        (CLAUDE.md guiding philosophy #5)."""
        from decimal import Decimal

        from app.routers.engine import EngineTradeResponse, _persisted_trade_net_pnl

        commission_per_order = 1.5
        engine_trade = EngineTradeResponse(
            trade_number=1,
            entry_time=_ms(2026, 6, 1),
            entry_price=100.0,
            exit_time=_ms(2026, 6, 2),
            exit_price=102.5,
            quantity=10,
            pnl_pts=2.5,
            pnl_pct=0.025,
            result="win",
        )
        expected = _persisted_trade_net_pnl(
            trade=engine_trade, commission_per_order=commission_per_order, compatibility_profile=None
        )
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        actual = trade_dollar_pnl(trade, commission_per_order=commission_per_order)
        assert Decimal(str(actual)) == Decimal(str(expected))


class TestTotalPnl:
    def test_sums_dollar_pnl_for_trades_with_entry_in_window(self) -> None:
        window_start = _ms(2026, 6, 1)
        window_end = _ms(2026, 6, 30)
        trades = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),  # +20
            _trade(_ms(2026, 6, 10), _ms(2026, 6, 11), pnl_pts=-1.0, pnl_pct=-0.01, quantity=10),  # -10
        ]
        assert total_pnl(trades, window_start, window_end) == 10.0

    def test_excludes_trades_entering_outside_the_window(self) -> None:
        window_start = _ms(2026, 6, 1)
        window_end = _ms(2026, 6, 30)
        trades = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),  # in window: +20
            _trade(_ms(2026, 5, 20), _ms(2026, 5, 21), pnl_pts=100.0, pnl_pct=1.0, quantity=10),  # entry before window
        ]
        assert total_pnl(trades, window_start, window_end) == 20.0

    def test_is_order_independent(self) -> None:
        window_start = _ms(2026, 6, 1)
        window_end = _ms(2026, 6, 30)
        trades = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),
            _trade(_ms(2026, 6, 10), _ms(2026, 6, 11), pnl_pts=-1.0, pnl_pct=-0.01, quantity=10),
            _trade(_ms(2026, 6, 15), _ms(2026, 6, 16), pnl_pts=3.0, pnl_pct=0.03, quantity=10),
        ]
        forward = total_pnl(trades, window_start, window_end)
        reversed_order = total_pnl(list(reversed(trades)), window_start, window_end)
        assert forward == reversed_order

    def test_empty_trades_return_zero(self) -> None:
        assert total_pnl([], _ms(2026, 6, 1), _ms(2026, 6, 30)) == 0.0

    def test_subtracts_commission_per_trade_in_window(self) -> None:
        window_start = _ms(2026, 6, 1)
        window_end = _ms(2026, 6, 30)
        trades = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),  # gross +20
            _trade(_ms(2026, 6, 10), _ms(2026, 6, 11), pnl_pts=-1.0, pnl_pct=-0.01, quantity=10),  # gross -10
        ]
        # gross 20 - 10 = 10, minus 2 trades x 2 commissions x $1 = 4 -> 6
        assert total_pnl(trades, window_start, window_end, commission_per_order=1.0) == pytest.approx(
            6.0, abs=1e-9, rel=0
        )


class TestSelectWindowHeroes:
    def test_selects_each_symbol_strategy_winner_from_entries_inside_the_window(self) -> None:
        heroes = select_window_heroes(
            [
                HeroCandidate(
                    recency_run_id=1,
                    symbol="SPY",
                    strategy_key="ema",
                    params_hash="all-time-winner",
                    trades=(
                        HeroTrade(entry_ms=50, pnl=1_000.0),
                        HeroTrade(entry_ms=500, pnl=-10.0),
                    ),
                ),
                HeroCandidate(
                    recency_run_id=2,
                    symbol="SPY",
                    strategy_key="ema",
                    params_hash="window-winner",
                    trades=(HeroTrade(entry_ms=500, pnl=200.0),),
                ),
            ],
            window_start_ms=100,
            window_end_ms=1_000,
        )

        assert heroes[0].recency_run_id == 2
        assert heroes[0].symbol == "SPY"
        assert heroes[0].strategy_key == "ema"
        assert heroes[0].params_hash == "window-winner"
        assert heroes[0].total_pnl == 200.0

    def test_excludes_a_trade_that_only_overlaps_the_window(self) -> None:
        heroes = select_window_heroes(
            [
                HeroCandidate(
                    recency_run_id=1,
                    symbol="SPY",
                    strategy_key="ema",
                    params_hash="entered-before",
                    trades=(HeroTrade(entry_ms=99, pnl=1_000.0),),
                ),
                HeroCandidate(
                    recency_run_id=2,
                    symbol="SPY",
                    strategy_key="ema",
                    params_hash="entered-inside",
                    trades=(HeroTrade(entry_ms=100, pnl=-5.0),),
                ),
            ],
            window_start_ms=100,
            window_end_ms=1_000,
        )

        assert heroes[0].recency_run_id == 2


class TestSharpe:
    def test_known_series_matches_hand_computed_value(self) -> None:
        # returns: 0.02, 0.03, -0.01, 0.01 -> mean=0.0125, sample-std(ddof=1)=0.017078...
        trades = [
            _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2, pnl_pct=0.02),
            _trade(_ms(2026, 6, 3), _ms(2026, 6, 4), pnl_pts=3, pnl_pct=0.03),
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=-1, pnl_pct=-0.01),
            _trade(_ms(2026, 6, 7), _ms(2026, 6, 8), pnl_pts=1, pnl_pct=0.01),
        ]
        result = sharpe(trades)
        assert result is not None
        assert result == pytest.approx(0.0125 / 0.0170782512765993, rel=1e-9)

    def test_below_min_trades_returns_none(self) -> None:
        trades = [_trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2, pnl_pct=0.02)]
        assert sharpe(trades, min_trades=2) is None

    def test_zero_variance_series_returns_none(self) -> None:
        trades = [
            _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=1, pnl_pct=0.01),
            _trade(_ms(2026, 6, 3), _ms(2026, 6, 4), pnl_pts=1, pnl_pct=0.01),
        ]
        assert sharpe(trades) is None

    def test_empty_trades_returns_none(self) -> None:
        assert sharpe([]) is None
