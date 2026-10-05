"""The sessions a default plan is laid over: the lake's latest unbroken run, read from the catalog."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.data_lake import catalog_client
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search import service
from app.research.golden_search.planning import StudyRange, fit_range, registry_incumbent
from app.research.golden_search.service import LakeSessions
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms


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


async def test_a_range_that_ends_before_a_hole_is_laid_out_from_the_run_it_ends_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor of a picked range is the run that reaches its last day, not the lake's latest run (#2886)."""
    before = expected_sessions(date(2024, 12, 2), date(2025, 4, 30))
    after = expected_sessions(date(2025, 5, 5), date(2026, 10, 2))
    _catalog_holding(monkeypatch, [*before, *after])

    sessions = await service.lake_sessions("SPY")

    assert sessions is not None and sessions.first == date(2025, 5, 5)
    # A range ending on 2025-04-30 reads nothing across the hole of 05-01 and 05-02.
    assert sessions.run_start(date(2025, 4, 30)) == date(2024, 12, 2)
    fit = fit_range(
        StudyRange(date(2025, 1, 1), date(2025, 5, 1)), final_months=1, training_months=1, test_months=1, earliest_session=sessions.run_start(date(2025, 4, 30))
    )
    assert (fit.development_start, fit.final_start) == (date(2025, 1, 1), date(2025, 4, 1))
    # A range reaching past the hole, or past the lake's last day, is held to the later run.
    assert sessions.run_start(date(2026, 10, 2)) == sessions.run_start(date(2026, 11, 30)) == date(2025, 5, 5)
    # No run reaches a day inside the hole: the plan check names the sessions the lake lacks.
    assert sessions.run_start(date(2025, 5, 2)) is None


async def test_the_default_plan_for_a_range_before_a_hole_keeps_the_run_up_month_of_the_run_it_ends_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """`defaults` hands the range layout the run that reaches the range's last day (#2886)."""
    held = frozenset([*expected_sessions(date(2024, 12, 2), date(2025, 4, 30)), *expected_sessions(date(2025, 5, 5), date(2026, 10, 2))])

    async def _coverage(symbol: str) -> LakeSessions:
        return LakeSessions(first=date(2025, 5, 5), last=date(2026, 10, 2), held=held)

    async def _incumbent(strategy_key: str, symbol: str, **_: object) -> service.IncumbentChoice:
        return service.IncumbentChoice(ref=registry_incumbent(strategy_key, symbol), label="Registry validated settings")

    async def _exposure(symbol: str, start_ms: int, end_ms: int, **_: object) -> dict[str, object]:
        return {"state": "not_opened", "ledger_overlaps": 0, "outside_activity_overlaps": 0, "explanation": ""}

    monkeypatch.setattr(service, "resolve_incumbent", _incumbent)
    monkeypatch.setattr(service, "exposure_view", _exposure)

    plan = await service.defaults(
        "ema_crossover_signal",
        "SPY",
        final_months=1,
        training_months=1,
        test_months=1,
        range_start_ms=et_midnight_ms(date(2024, 11, 15)),
        range_end_ms=et_midnight_ms(date(2025, 5, 1)),
        lake_coverage=_coverage,
    )

    # The run before the hole starts on 2024-12-02; its first month is the run-up, so development starts in January.
    assert et_date_at_ms(plan["development_start_ms"]) == date(2025, 1, 1)
    assert "the lake's unbroken history starts on 2024-12-02" in plan["range_note"]

