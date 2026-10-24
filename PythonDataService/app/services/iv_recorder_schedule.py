"""The IV30 recorder's clock: one snapshot per trading day, near the close (#3525).

The Quartz cron that called the recorder retired with the C# backend (ADR 0071 decision 6, ADR 0081). This is the
Python clock that takes its place for the closing snapshot. It is a service-lifetime loop started from the data plane's
lifespan, the shape the option archive's scheduler has: a ``tick`` every 30 seconds asks a pure ``due`` question of the
clock, ``start`` and ``stop`` own the task, and the clock and the sleep are injected so a test drives it without
waiting. Only the data-plane core runs it (``_ROLE_RUNS_DATA_PLANE_CORE``), so one process owns the fire.

When. Each trading day, ``close + vendor delay`` after the canonical calendar's close of that day
(:func:`app.data_lake.capture_finality.capture_final_ms`, ``regular``): the plan is 15-minute delayed, so a snapshot
asked for at the close shows the quotes of a quarter hour earlier, and one asked for a quarter hour later shows the close.
An early close moves the instant with it. Non-trading days have no instant, and no time of day is written here.

Catch-up. A process that starts after the instant but within :data:`CATCH_UP_MS` of it still records the day. Later than
that the chain's quotes have stopped at the close while the underlying keeps printing until 20:00 ET, so a snapshot would
pair two different moments; the day is not recorded.

Once a day. A symbol is recorded when it has no close-slot row for the day in the store, so a restart in the window never
asks the vendor twice. An attempt that fails is logged with a structured ``action`` and is not retried that day:
the recorder itself files an error-tagged row for a vendor or solver failure (which counts as the day's row), and a
failure that files nothing is remembered by this process for the day. The next trading day tries again.

Each row carries the quote time the recorder always stamped (``snapshot_ts_ms``, ``int64 ms UTC``); this loop passes it
the clock's reading, so it is the moment of the snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime

from app.config import settings
from app.data_lake.capture_finality import capture_final_ms
from app.lean_sidecar.trading_calendar import is_trading_day
from app.schemas.broker_bots import normalized_symbol
from app.services.iv_recorder import IvSnapshotStore, RecordedIvSnapshot, get_iv_store, record_iv_snapshot
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The slot a closing snapshot is filed under (ADR 0071 decision 7: "16:00 captures the print"). It names the close
#: whatever time the calendar puts it at, so an early close files under it too.
CLOSE_SLOT = "16:00"

#: How long after its instant a day can still be recorded (see the module docstring).
CATCH_UP_MS = 2 * 3_600_000

#: Seconds between looks at the clock.
TICK_SECONDS = 30

#: Records one symbol's snapshot at the instant given; run on a worker thread because the recorder blocks on the vendor.
SnapshotRecorder = Callable[[str, datetime], RecordedIvSnapshot]


def parse_symbols(raw: str) -> tuple[str, ...]:
    """``IV_RECORDER_SYMBOLS`` as symbols: comma separated, upper-cased, repeats dropped; empty is none (schedule off)."""
    symbols: list[str] = []
    for token in raw.split(","):
        if not token.strip():
            continue
        try:
            symbol = normalized_symbol(token)
        except ValueError as exc:
            raise ValueError(f"IV_RECORDER_SYMBOLS names {token.strip()!r}: {exc}") from exc
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def snapshot_due_ms(day: date) -> int:
    """The instant ``day``'s closing snapshot is taken: the calendar's close plus the vendor delay."""
    return capture_final_ms(day, "regular")


def snapshot_day_at(now_ms: int) -> date | None:
    """The trading day whose closing snapshot is wanted at ``now_ms``, or ``None`` outside every day's window."""
    day = et_date_at_ms(now_ms)
    if not is_trading_day(day):
        return None
    due = snapshot_due_ms(day)
    return day if due <= now_ms < min(due + CATCH_UP_MS, et_day_end_ms(day)) else None


def _has_close_row(store: IvSnapshotStore, symbol: str, day: date) -> bool:
    rows = store.read_series(symbol, start_ms=snapshot_due_ms(day), end_ms=et_day_end_ms(day))
    return any(row.slot == CLOSE_SLOT for row in rows)


class IvRecorderSchedule:
    """Records each configured symbol's IV30 once per trading day, near the close."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        store: IvSnapshotStore,
        record: SnapshotRecorder,
        clock: Callable[[], int] = now_ms_utc,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.symbols, self.store, self._record, self.clock, self._sleep = tuple(symbols), store, record, clock, sleep
        self._task: asyncio.Task[None] | None = None
        #: ``(symbol, day)`` pairs this process has settled: recorded, found recorded, or failed once.
        self._settled: set[tuple[str, date]] = set()

    @classmethod
    def from_settings(cls) -> IvRecorderSchedule | None:
        """The schedule the data plane runs, or ``None`` when ``IV_RECORDER_SYMBOLS`` names no symbol."""
        symbols = parse_symbols(settings.IV_RECORDER_SYMBOLS)
        if not symbols:
            return None
        from app.services.polygon_client import PolygonClientService

        store, polygon = get_iv_store(), PolygonClientService()

        def record(ticker: str, asof: datetime) -> RecordedIvSnapshot:
            return record_iv_snapshot(ticker=ticker, slot=CLOSE_SLOT, store=store, polygon=polygon, asof=asof)

        return cls(symbols=symbols, store=store, record=record)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="iv-recorder-schedule")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def tick(self) -> None:
        """Record every symbol whose closing snapshot is due now and not yet settled. A failure of one never stops the rest."""
        now = self.clock()
        day = snapshot_day_at(now)
        if day is None:
            return
        for symbol in self.symbols:
            if (symbol, day) in self._settled:
                continue
            # Settled before the vendor is asked, so a slow or failing attempt is never begun again today.
            self._settled.add((symbol, day))
            try:
                if await asyncio.to_thread(_has_close_row, self.store, symbol, day):
                    continue
                row = await asyncio.to_thread(self._record, symbol, datetime.fromtimestamp(now / 1000, tz=UTC))
            except Exception:
                logger.exception(
                    "IV recorder could not take a closing snapshot; the next trading day tries again",
                    extra={"action": "iv_recorder_snapshot_failed", "symbol": symbol, "day": day.isoformat()},
                )
                continue
            if row.error is not None:
                logger.warning(
                    "IV recorder filed an error row for a closing snapshot",
                    extra={"action": "iv_recorder_snapshot_error_row", "symbol": symbol, "day": day.isoformat(), "error": row.error},
                )
            else:
                logger.info(
                    "IV recorder recorded a closing snapshot",
                    extra={"action": "iv_recorder_snapshot_recorded", "symbol": symbol, "day": day.isoformat(),
                           "snapshot_ts_ms": row.snapshot_ts_ms},
                )

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("IV recorder schedule tick failed; it looks again at the next tick",
                                 extra={"action": "iv_recorder_tick_failed"})
            await self._sleep(TICK_SECONDS)
