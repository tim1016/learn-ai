"""Builders for the scoped-health tests: each source's reading, healthy unless told otherwise."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any
from uuid import uuid4

from app.broker.ibkr.health import synthetic_disconnected_health
from app.broker.ibkr.models import BrokerConnectionState, IbkrConnectionHealth
from app.data_lake.types import RunReadinessResponse, SymbolRunReadiness
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.schemas.option_archive import (
    ArchiveGap,
    ArchiveJobHealth,
    ArchiveSeverity,
    OptionArchiveHealth,
    UnderlyingArchiveHealth,
)
from app.services.scoped_health_service import HealthReaders

#: Fixed instants: 2026-10-23 19:00 UTC, and two session opens before it.
NOW_MS = 1_792_782_000_000
SESSION_OPEN_MS = session_open_ms_utc(date(2026, 10, 20))
EARLIER_SESSION_OPEN_MS = session_open_ms_utc(date(2026, 10, 16))


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


def symbol_row(symbol: str, *, unreadable: int = 0, missing: int = 0, held: int = 100) -> SymbolRunReadiness:
    return SymbolRunReadiness(
        symbol=symbol,
        first_trading_date_ms=EARLIER_SESSION_OPEN_MS,
        last_trading_date_ms=SESSION_OPEN_MS,
        session_count=held,
        run_ready_count=held - unreadable,
        unrebuildable_count=0,
        rebuild_first_trading_date_ms=EARLIER_SESSION_OPEN_MS if unreadable else None,
        rebuild_last_trading_date_ms=SESSION_OPEN_MS if unreadable else None,
        gap_session_count=missing,
        gap_count=1 if missing else 0,
        fill_first_trading_date_ms=SESSION_OPEN_MS if missing else None,
        fill_last_trading_date_ms=SESSION_OPEN_MS if missing else None,
    )


def lake_readiness(*rows: SymbolRunReadiness) -> RunReadinessResponse:
    return RunReadinessResponse(
        market="usa",
        price_adjustment_mode="polygon_split_adjusted",
        data_type="trade",
        data_root_id=uuid4(),
        symbols=list(rows) or [symbol_row("SPY"), symbol_row("QQQ")],
    )


def underlying(
    symbol: str, severity: ArchiveSeverity = "healthy", reason: str = "Every required session has been archived.", *, gaps: int = 0
) -> UnderlyingArchiveHealth:
    return UnderlyingArchiveHealth(
        underlying=symbol,
        severity=severity,
        reason=reason,
        pending_sessions=gaps,
        partial_sessions=0,
        lost_sessions=0,
        gaps=[
            ArchiveGap(session_ms=SESSION_OPEN_MS, permanent=False, days_left=30, reason="Not archived.", recorded_ms=NOW_MS)
            for _ in range(gaps)
        ],
        phase="complete",
    )


def job(severity: ArchiveSeverity = "healthy", reason: str = "The latest backup passed verification.") -> ArchiveJobHealth:
    return ArchiveJobHealth(severity=severity, reason=reason, last_success_ms=NOW_MS)


def archive_health(
    *,
    underlyings: list[UnderlyingArchiveHealth] | None = None,
    backup: ArchiveJobHealth | None = None,
    restore: ArchiveJobHealth | None = None,
) -> OptionArchiveHealth:
    return OptionArchiveHealth(
        enabled=True,
        generated_ms=NOW_MS,
        attention=False,
        underlyings=underlyings or [underlying("SPY"), underlying("QQQ"), underlying("IWM")],
        backup=backup or job(),
        test_restore=restore or job(reason="The latest test restore passed verification."),
    )


def ibkr_health(state: BrokerConnectionState = "connected") -> IbkrConnectionHealth:
    return synthetic_disconnected_health(state=state, disabled=state == "disabled")


def serving[T](value: T) -> Callable[[], Awaitable[T]]:
    async def read() -> T:
        return value

    return read


def failing(error: Exception | None = None) -> Callable[[], Awaitable[Any]]:
    async def read() -> Any:
        raise error or RuntimeError("the source is down")

    return read


def readers(**overrides: Callable[[], Awaitable[Any]]) -> HealthReaders:
    """Every source healthy, except the ones named."""
    chosen: dict[str, Callable[[], Awaitable[Any]]] = {
        "lake": serving(lake_readiness()),
        "archive": serving(archive_health()),
        "ibkr": serving(ibkr_health()),
    }
    chosen.update(overrides)
    return HealthReaders(**chosen)  # type: ignore[arg-type]
