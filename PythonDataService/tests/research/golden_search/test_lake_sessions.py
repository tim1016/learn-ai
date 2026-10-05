"""The sessions a default plan is laid over: the lake's latest unbroken run, read from the catalog."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.data_lake import catalog_client
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search import service
from app.research.golden_search.service import LakeSessions


def _catalog_holding(monkeypatch: pytest.MonkeyPatch, held: list[date]) -> list[dict[str, object]]:
    """A catalog whose complete minute sessions for any symbol are ``held``; returns the reads it was asked for."""
    reads: list[dict[str, object]] = []

    async def _init_pool() -> None:
        return None

    async def _sessions(market: str, symbol: str, data_type: str, start: date | None, end: date | None, **scope: object):
        reads.append({"market": market, "symbol": symbol, "data_type": data_type, "window": (start, end), **scope})
        return [SimpleNamespace(trading_date=day) for day in held]

    monkeypatch.setattr(catalog_client, "init_pool", _init_pool)
    monkeypatch.setattr(catalog_client, "select_coverage_minute_bars", _sessions)
    return reads


async def test_a_symbol_held_in_two_stretches_starts_at_the_later_one_not_at_its_first_held_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stray = expected_sessions(date(2021, 10, 5), date(2021, 10, 15))
    recent = expected_sessions(date(2024, 9, 4), date(2026, 10, 2))
    reads = _catalog_holding(monkeypatch, [*stray, *recent])

    sessions = await service.lake_sessions("QQQ")

    assert sessions == LakeSessions(first=date(2024, 9, 4), last=date(2026, 10, 2))
    # The symbol's whole history in the tree a sweep reads: split-adjusted trade bars.
    assert reads == [
        {"market": "usa", "symbol": "QQQ", "data_type": "trade", "window": (None, None), "price_adjustment_mode": "polygon_split_adjusted"}
    ]


async def test_a_symbol_without_a_hole_keeps_its_first_session_and_an_unheld_one_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    whole = expected_sessions(date(2021, 10, 7), date(2026, 10, 2))
    _catalog_holding(monkeypatch, whole)
    assert await service.lake_sessions("SPY") == LakeSessions(first=date(2021, 10, 7), last=date(2026, 10, 2))

    _catalog_holding(monkeypatch, [])
    assert await service.lake_sessions("ZZZZ") is None
