"""Recency Chart statistics — Python-authored, per ADR 0068.

These are the numbers the Angular swimlane renders but never computes:
per-trade dollar PnL, holding-session count (calendar-derived), a combo's
windowed total PnL (the hero-selection metric), and a combo's Sharpe (the
opacity-encoding metric). Every formula is defined here.
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
    @pytest.mark.parametrize(
        ("entry", "exit_", "sessions"),
        [
            # Wed, during RTH in UTC terms
            ((2026, 6, 10, 14, 30), (2026, 6, 10, 19, 0), 1),
            # 2026-06-08 Mon, 06-09 Tue, 06-10 Wed — no holiday in between.
            ((2026, 6, 8, 14, 30), (2026, 6, 10, 19, 0), 3),
            # Fri 2026-06-05 -> Mon 2026-06-08: only Fri + Mon are sessions.
            ((2026, 6, 5, 14, 30), (2026, 6, 8, 19, 0), 2),
            # 2026-07-03 (Fri, observed Independence Day holiday) is not a
            # session; Thu 07-02 and Mon 07-06 are the two sessions.
            ((2026, 7, 2, 14, 30), (2026, 7, 6, 19, 0), 2),
        ],
        ids=[
            "same_day_entry_and_exit_is_one_session",
            "monday_to_wednesday_spans_three_sessions",
            "spans_a_weekend_without_counting_weekend_days",
            "spans_a_holiday_without_counting_it",
        ],
    )
    def test_holding_sessions_counts_calendar_sessions(
        self, entry: tuple[int, ...], exit_: tuple[int, ...], sessions: int
    ) -> None:
        assert holding_sessions(_ms(*entry), _ms(*exit_)) == sessions


class TestTradeDollarPnl:
    def test_trade_dollar_pnl_is_points_times_quantity_less_round_trip_commission(self) -> None:
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        losing = _trade(_ms(2026, 6, 1), _ms(2026, 6, 1), pnl_pts=-1.5, pnl_pct=-0.015, quantity=4)
        assert trade_dollar_pnl(trade) == 25.0, "multiplies points by quantity"
        assert trade_dollar_pnl(losing) == -6.0, "negative points produce negative dollar pnl"
        assert trade_dollar_pnl(trade, commission_per_order=1.5) == pytest.approx(
            25.0 - 3.0, abs=1e-9, rel=0
        ), "subtracts two flat commissions for the round trip"

    def test_matches_the_canonical_engine_flat_fee_formula(self) -> None:
        """Parity with the engine payload's ``persisted_trade_net_pnl`` flat-fee
        branch (compatibility_profile=None) — the codebase's one canonical
        formula for round-trip net PnL under a flat per-order commission
        (ADR 0068 Decision 4)."""
        from decimal import Decimal

        from app.research.backtest_runs.engine_payload import persisted_trade_net_pnl
        from app.schemas.engine_backtest import EngineTradeResponse

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
        expected = persisted_trade_net_pnl(
            trade=engine_trade, commission_per_order=commission_per_order, compatibility_profile=None
        )
        trade = _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2.5, pnl_pct=0.025, quantity=10)
        actual = trade_dollar_pnl(trade, commission_per_order=commission_per_order)
        assert Decimal(str(actual)) == Decimal(str(expected))


class TestTotalPnl:
    def test_total_pnl_over_the_window(self) -> None:
        window_start = _ms(2026, 6, 1)
        window_end = _ms(2026, 6, 30)
        trades = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),  # +20
            _trade(_ms(2026, 6, 10), _ms(2026, 6, 11), pnl_pts=-1.0, pnl_pct=-0.01, quantity=10),  # -10
        ]
        assert total_pnl(trades, window_start, window_end) == 10.0, "sums dollar pnl for trades with entry in window"
        # gross 20 - 10 = 10, minus 2 trades x 2 commissions x $1 = 4 -> 6
        assert total_pnl(trades, window_start, window_end, commission_per_order=1.0) == pytest.approx(
            6.0, abs=1e-9, rel=0
        ), "subtracts commission per trade in window"

        straddling = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),  # in window: +20
            _trade(_ms(2026, 5, 20), _ms(2026, 5, 21), pnl_pts=100.0, pnl_pct=1.0, quantity=10),  # entry before window
        ]
        assert total_pnl(straddling, window_start, window_end) == 20.0, "excludes trades entering outside the window"

        three = [
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=2.0, pnl_pct=0.02, quantity=10),
            _trade(_ms(2026, 6, 10), _ms(2026, 6, 11), pnl_pts=-1.0, pnl_pct=-0.01, quantity=10),
            _trade(_ms(2026, 6, 15), _ms(2026, 6, 16), pnl_pts=3.0, pnl_pct=0.03, quantity=10),
        ]
        forward = total_pnl(three, window_start, window_end)
        reversed_order = total_pnl(list(reversed(three)), window_start, window_end)
        assert forward == reversed_order, "is order independent"

        assert total_pnl([], window_start, window_end) == 0.0, "empty trades return zero"


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
    def test_sharpe_value_and_when_it_is_undefined(self) -> None:
        # returns: 0.02, 0.03, -0.01, 0.01 -> mean=0.0125, sample-std(ddof=1)=0.017078...
        trades = [
            _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2, pnl_pct=0.02),
            _trade(_ms(2026, 6, 3), _ms(2026, 6, 4), pnl_pts=3, pnl_pct=0.03),
            _trade(_ms(2026, 6, 5), _ms(2026, 6, 6), pnl_pts=-1, pnl_pct=-0.01),
            _trade(_ms(2026, 6, 7), _ms(2026, 6, 8), pnl_pts=1, pnl_pct=0.01),
        ]
        result = sharpe(trades)
        assert result is not None, "known series has a sharpe"
        assert result == pytest.approx(0.0125 / 0.0170782512765993, rel=1e-9), "known series matches hand computed value"

        single = [_trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=2, pnl_pct=0.02)]
        assert sharpe(single, min_trades=2) is None, "below min trades returns none"

        flat = [
            _trade(_ms(2026, 6, 1), _ms(2026, 6, 2), pnl_pts=1, pnl_pct=0.01),
            _trade(_ms(2026, 6, 3), _ms(2026, 6, 4), pnl_pts=1, pnl_pct=0.01),
        ]
        assert sharpe(flat) is None, "zero variance series returns none"

        assert sharpe([]) is None, "empty trades returns none"
