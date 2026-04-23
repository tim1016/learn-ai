"""Tests for app.services.trade_comparison.

NOTE: trade_comparison uses string timestamps and datetime.strptime, which
violates the int64 ms UTC timestamp policy in .claude/rules/numerical-rigor.md.
This is a production-code violation, not a test gap. These tests cover the
current behavior; a follow-up PR should rewrite the module to consume
int64 ms UTC directly. See the research plan §6.7.
"""

from __future__ import annotations

import pytest

from app.services.trade_comparison import (
    MatchStats,
    TradeComparison,
    match_trades,
)

ENTRY_T1 = "2024-01-01T14:30:00Z"
EXIT_T1 = "2024-01-01T14:45:00Z"
ENTRY_T2 = "2024-01-01T15:00:00Z"
EXIT_T2 = "2024-01-01T15:15:00Z"


def _our(entry: str, exit_: str, entry_price: float = 100.0, exit_price: float = 101.0) -> dict:
    return {
        "entry_timestamp": entry,
        "exit_timestamp": exit_,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": exit_price - entry_price,
        "pnl_pct": (exit_price - entry_price) / entry_price,
    }


def _ref(entry: str, exit_: str, entry_price: float = 100.0, exit_price: float = 101.0) -> dict:
    return {
        "entry_time": entry,
        "exit_time": exit_,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": exit_price - entry_price,
        "pnl_pct": (exit_price - entry_price) / entry_price,
    }


def test_match_trades_exact_match_produces_zero_deltas():
    our_trades = [_our(ENTRY_T1, EXIT_T1)]
    ref_trades = [_ref(ENTRY_T1, EXIT_T1)]

    comparisons, stats = match_trades(our_trades, ref_trades)

    assert len(comparisons) == 1
    c = comparisons[0]
    assert c.matched is True
    assert c.source == "matched"
    assert c.entry_price_delta == pytest.approx(0.0, abs=1e-9, rel=0)
    assert c.pnl_delta == pytest.approx(0.0, abs=1e-9, rel=0)
    assert c.timestamp_delta_s == pytest.approx(0.0, abs=1e-9, rel=0)
    assert stats.match_rate == pytest.approx(1.0, abs=1e-9, rel=0)
    assert stats.matched_count == 1


def test_match_trades_extra_ref_marked_unmatched():
    ref_trades = [_ref(ENTRY_T1, EXIT_T1), _ref(ENTRY_T2, EXIT_T2)]
    our_trades = [_our(ENTRY_T1, EXIT_T1)]

    comparisons, stats = match_trades(our_trades, ref_trades)

    unmatched = [c for c in comparisons if c.source == "extra_ref"]
    assert len(unmatched) == 1
    assert unmatched[0].ref_entry_time == ENTRY_T2
    assert unmatched[0].our_entry_time is None
    assert unmatched[0].matched is False
    assert stats.match_rate == pytest.approx(0.5, abs=1e-9, rel=0)
    assert stats.extra_ref == 1


def test_match_trades_extra_ours_listed_after_ref():
    ref_trades = [_ref(ENTRY_T1, EXIT_T1)]
    our_trades = [_our(ENTRY_T1, EXIT_T1), _our(ENTRY_T2, EXIT_T2)]

    comparisons, stats = match_trades(our_trades, ref_trades)

    extras = [c for c in comparisons if c.source == "extra_ours"]
    assert len(extras) == 1
    assert extras[0].our_entry_time == ENTRY_T2
    assert extras[0].ref_entry_time is None
    assert stats.extra_ours == 1


def test_match_trades_delta_within_tolerance_matches():
    # 900s (15 min) is the default max_delta_s — boundary still matches.
    our_trades = [_our("2024-01-01T14:45:00Z", "2024-01-01T15:00:00Z")]
    ref_trades = [_ref(ENTRY_T1, EXIT_T1)]  # 900s earlier

    comparisons, stats = match_trades(our_trades, ref_trades)

    assert len(comparisons) == 1
    assert comparisons[0].matched is True
    assert comparisons[0].timestamp_delta_s == pytest.approx(900.0, abs=1e-9, rel=0)
    assert stats.avg_ts_delta_s == pytest.approx(900.0, abs=1e-9, rel=0)


def test_match_trades_delta_beyond_tolerance_does_not_match():
    # 30 min apart, default tolerance 15 min → no match.
    our_trades = [_our("2024-01-01T15:00:00Z", "2024-01-01T15:15:00Z")]
    ref_trades = [_ref(ENTRY_T1, EXIT_T1)]

    comparisons, _ = match_trades(our_trades, ref_trades)

    # Exactly one ref unmatched and one our unmatched.
    sources = sorted(c.source for c in comparisons)
    assert sources == ["extra_ours", "extra_ref"]


def test_match_trades_empty_inputs_produce_zero_match_rate():
    comparisons, stats = match_trades(our_trades=[], ref_trades=[])

    assert comparisons == []
    assert isinstance(stats, MatchStats)
    assert stats.match_rate == pytest.approx(0.0, abs=1e-9, rel=0)
    assert stats.matched_count == 0
    assert stats.total_ref == 0
    assert stats.total_ours == 0


def test_trade_comparison_dataclass_instantiation():
    c = TradeComparison(
        trade_num=1,
        ref_entry_time=ENTRY_T1,
        our_entry_time=ENTRY_T1,
        ref_exit_time=EXIT_T1,
        our_exit_time=EXIT_T1,
        ref_entry_price=100.0,
        our_entry_price=100.0,
        ref_exit_price=101.0,
        our_exit_price=101.0,
        ref_pnl=1.0,
        our_pnl=1.0,
        ref_pnl_pct=0.01,
        our_pnl_pct=0.01,
        entry_price_delta=0.0,
        exit_price_delta=0.0,
        pnl_delta=0.0,
        pnl_pct_delta=0.0,
        timestamp_delta_s=0.0,
        matched=True,
        source="matched",
    )

    assert c.trade_num == 1
    assert c.source == "matched"
