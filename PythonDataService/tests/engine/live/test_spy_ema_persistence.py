"""Tests for SpyEmaCrossoverAlgorithm persistence hooks.

The strategy exposes three methods consumed by LiveContext:
  - report_state_for_persistence() -> dict | None
  - restore_state_from_persistence(payload) -> None
  - validate_state_payload(payload) -> ValidationResult

PR1 contract: report returns None unless indicators are ready AND
position is flat AND no pending orders AND no open insights.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm


def _build_warmed_strategy() -> SpyEmaCrossoverAlgorithm:
    """Construct a strategy with indicators forced past warmup, flat lifecycle."""
    strat = SpyEmaCrossoverAlgorithm()
    # Stand-alone construction (no LiveEngine.run) — drive initialize()
    # manually with a minimal fake context.
    strat.ctx = MagicMock()
    strat.ctx.add_equity.return_value = "SPY"
    strat.initialize()

    # Drive enough closes through the indicators to make them ready.
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    for i in range(20):
        bar_time = t0 + timedelta(minutes=15 * i)
        close = Decimal(400 + i)
        strat._ema5.update(bar_time, close)
        strat._ema10.update(bar_time, close)
        strat._rsi14.update(bar_time, close)
    return strat


def test_report_state_returns_none_when_indicators_not_ready() -> None:
    strat = SpyEmaCrossoverAlgorithm()
    strat.ctx = MagicMock()
    strat.ctx.add_equity.return_value = "SPY"
    strat.initialize()
    # No updates -> indicators not ready.
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_none_when_in_position() -> None:
    strat = _build_warmed_strategy()
    strat._in_position = True
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_none_when_pending_entry() -> None:
    strat = _build_warmed_strategy()
    from app.engine.strategy.algorithms.spy_ema_crossover import _PendingEntry

    strat._pending_entry = _PendingEntry(ema5=Decimal("400"), ema10=Decimal("399"), rsi=Decimal("60"))
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_none_when_open_trade() -> None:
    """The state between entry fill and exit fill — _open_trade is set."""
    from app.engine.strategy.algorithms.spy_ema_crossover import _OpenTrade

    strat = _build_warmed_strategy()
    strat._open_trade = _OpenTrade(
        entry_time=datetime(2026, 5, 18, 14, 0, tzinfo=UTC),
        entry_price=Decimal("410"),
        ema5=Decimal("410"),
        ema10=Decimal("409"),
        rsi=Decimal("62"),
    )
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_payload_when_flat_and_ready() -> None:
    strat = _build_warmed_strategy()
    payload = strat.report_state_for_persistence()
    assert payload is not None
    assert "ema5" in payload
    assert "ema10" in payload
    assert "rsi14" in payload
    assert "_prev_ema5_above_ema10" in payload
    assert "lifecycle" in payload
    assert payload["lifecycle"]["position_qty"] == 0
    assert payload["lifecycle"]["pending_orders_count"] == 0


def test_restore_state_round_trip_produces_bit_identical_next_value() -> None:
    """After hydrate, the next consolidated bar produces the same indicator
    values as if the strategy had run continuously."""
    src = _build_warmed_strategy()
    payload = src.report_state_for_persistence()
    assert payload is not None

    # Path A: continue src directly.
    next_time = datetime(2026, 5, 18, 14, 0, tzinfo=UTC) + timedelta(minutes=15 * 20)
    next_close = Decimal("420")
    src._ema5.update(next_time, next_close)
    src._ema10.update(next_time, next_close)
    src._rsi14.update(next_time, next_close)
    expected = (src._ema5.current_value, src._ema10.current_value, src._rsi14.current_value)

    # Path B: fresh strategy + restore + feed the same bar.
    dst = SpyEmaCrossoverAlgorithm()
    dst.ctx = MagicMock()
    dst.ctx.add_equity.return_value = "SPY"
    dst.initialize()
    dst.restore_state_from_persistence(payload)
    dst._ema5.update(next_time, next_close)
    dst._ema10.update(next_time, next_close)
    dst._rsi14.update(next_time, next_close)
    actual = (dst._ema5.current_value, dst._ema10.current_value, dst._rsi14.current_value)

    assert actual == expected, f"bit-identical equivalence broken: {actual} != {expected}"


def test_validate_state_payload_accepts_well_formed_payload() -> None:
    strat = _build_warmed_strategy()
    payload = strat.report_state_for_persistence()
    assert payload is not None
    result = strat.validate_state_payload(payload)
    assert result.payload_shape_ok
    assert result.failure_reason is None


def test_validate_state_payload_rejects_missing_keys() -> None:
    strat = SpyEmaCrossoverAlgorithm()
    bad = {"ema5": {}}  # missing ema10, rsi14, _prev_ema5_above_ema10, lifecycle
    result = strat.validate_state_payload(bad)
    assert result.failure_reason == "payload_mismatch"
    assert result.payload_shape_ok is False


def test_strategy_key_and_period_constants() -> None:
    assert SpyEmaCrossoverAlgorithm.STRATEGY_KEY == "spy_ema_crossover"
    assert SpyEmaCrossoverAlgorithm.CONSOLIDATOR_PERIOD_MIN == 15
