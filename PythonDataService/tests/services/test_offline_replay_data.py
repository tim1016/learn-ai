from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.services.offline_replay_data import (
    OfflineReplayDataError,
    OfflineReplayDataService,
)


def _trade_bars(symbol: str, count: int) -> list[TradeBar]:
    start = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
    return [
        TradeBar(
            symbol=symbol,
            time=start + timedelta(minutes=index),
            end_time=start + timedelta(minutes=index + 1),
            open=Decimal("100") + index,
            high=Decimal("101") + index,
            low=Decimal("99") + index,
            close=Decimal("100.5") + index,
            volume=1_000 + index,
        )
        for index in range(count)
    ]


def _native_bars(symbol: str, count: int) -> tuple[IbkrMinuteBar, ...]:
    return tuple(
        OfflineReplayDataService._to_native_bar(bar, fetched_at_ms=42)
        for bar in _trade_bars(symbol, count)
    )


def test_source_validation_rejects_a_missing_market_minute() -> None:
    bars = _trade_bars("SPY", 4)
    bars.pop(2)
    session = session_window_for_date(datetime(2026, 7, 28).date())

    with pytest.raises(OfflineReplayDataError, match="missing source minute"):
        OfflineReplayDataService._validate_source_bars(
            symbol="SPY",
            session=session,
            bars=bars,
            required_minutes=3,
        )


def test_source_validation_ignores_a_gap_after_the_requested_window() -> None:
    bars = _trade_bars("SPY", 5)
    bars[-1] = replace(
        bars[-1],
        time=bars[-1].time + timedelta(minutes=1),
        end_time=bars[-1].end_time + timedelta(minutes=1),
    )
    session = session_window_for_date(datetime(2026, 7, 28).date())

    OfflineReplayDataService._validate_source_bars(
        symbol="SPY",
        session=session,
        bars=bars,
        required_minutes=4,
    )


def test_symbol_alignment_requires_identical_exchange_timestamps() -> None:
    spy = _native_bars("SPY", 3)
    tsla = list(_native_bars("TSLA", 3))
    tsla[1] = tsla[1].model_copy(
        update={
            "start_ms": tsla[1].start_ms + 60_000,
            "end_ms": tsla[1].end_ms + 60_000,
        }
    )

    with pytest.raises(OfflineReplayDataError, match="do not align exactly"):
        OfflineReplayDataService._validate_symbol_alignment(
            {"SPY": spy, "TSLA": tuple(tsla)}
        )


def test_data_digest_is_reproducible_and_price_sensitive() -> None:
    bars = _native_bars("SPY", 3)
    changed = list(bars)
    changed[2] = changed[2].model_copy(update={"close": Decimal("999")})

    assert OfflineReplayDataService._bar_digest(bars) == (
        OfflineReplayDataService._bar_digest(bars)
    )
    assert OfflineReplayDataService._bar_digest(bars) != (
        OfflineReplayDataService._bar_digest(tuple(changed))
    )
