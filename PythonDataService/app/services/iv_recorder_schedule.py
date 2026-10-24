"""The IV30 recorder's clock: one snapshot per trading day, five minutes before the close (#3525).

The Quartz cron that called the recorder retired with the C# backend (ADR 0071 decision 6, ADR 0081). This is the
Python clock that takes its place for the 15:55 slot. It is a service-lifetime loop started from the data plane's
lifespan, the shape the option archive's scheduler has: a ``tick`` every 30 seconds asks a pure ``due`` question of the
clock, ``start`` and ``stop`` own the task, and the clock and the sleep are injected so a test drives it without
waiting. Only the data-plane core runs it (``_ROLE_RUNS_DATA_PLANE_CORE``), so one process owns the fire.

When. The slot is :data:`SLOT_BEFORE_CLOSE_MS` before the canonical calendar's close of the day: 15:55 ET, and 12:55 on an
early close (ADR 0071's 15:55 slot, which the calendar moves with the close). The vendor's options snapshot runs
:data:`~app.services.iv_recorder.OPTIONS_SNAPSHOT_DELAY_MS` behind the market on this plan, so the vendor is asked at the
slot plus that delay (16:10 ET, 13:10 on an early close) and answers with the quotes of the slot. The row is filed at those
quotes' own time (:mod:`app.services.iv_recorder`), which sits before the close, so the regular-hours 15:45 bar, the
extended 15:45 bar and the day's bar each contain it. Non-trading days have no instant, and no time of day is written here.

Late. A process that finds the asking moment more than :data:`GRACE_MS` past does not record the day. The grace is the
slot's lead on the close: asked later than that, the vendor's quotes are of a moment after the close, and they would be
filed under the 15:55 slot beside quotes that are not. There is no catch-up. The skipped day is logged once per symbol
with ``action="iv_recorder_snapshot_skipped_late"`` and the next trading day tries again; a restart inside the grace still
records the day, and a restart after it leaves the day to whatever the store already holds.

Once a day. A symbol is recorded when the store holds no row of this slot asked for on the day, so a restart in the grace
never asks the vendor twice. An attempt that fails is logged with a structured ``action`` and is not retried that day:
the recorder itself files an error-tagged row for a vendor or solver failure (which counts as the day's row), and a
failure that files nothing is remembered by this process for the day. The next trading day tries again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime

from app.config import settings
from app.lean_sidecar.trading_calendar import is_trading_day, session_close_ms_utc
from app.services.iv_recorder import (
    OPTIONS_SNAPSHOT_DELAY_MS,
    IvSnapshotStore,
    RecordedIvSnapshot,
    get_iv_store,
    record_iv_snapshot,
)
from app.utils.session_anchors import et_date_at_ms
from app.utils.symbols import parse_symbol_list
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The slot the snapshot is filed under (ADR 0071 decision 7). It names the moment five minutes before the close whatever
#: time of day the calendar puts the close at, so an early close files under it too.
SNAPSHOT_SLOT = "15:55"

#: How long before the calendar's close the quotes wanted are those of.
SLOT_BEFORE_CLOSE_MS = 5 * 60_000

#: How long after its asking moment a day can still be recorded (see the module docstring).
GRACE_MS = SLOT_BEFORE_CLOSE_MS

#: Seconds between looks at the clock.
TICK_SECONDS = 30

#: Records one symbol's snapshot at the instant given; run on a worker thread because the recorder blocks on the vendor.
SnapshotRecorder = Callable[[str, datetime], RecordedIvSnapshot]


def slot_ms(day: date) -> int:
    """The instant whose quotes ``day``'s snapshot is wanted for: five minutes before the calendar's close."""
    return session_close_ms_utc(day) - SLOT_BEFORE_CLOSE_MS


def snapshot_due_ms(day: date) -> int:
    """The instant the vendor is asked for ``day``'s snapshot: the slot plus the options snapshot's delay."""
    return slot_ms(day) + OPTIONS_SNAPSHOT_DELAY_MS


def snapshot_day_at(now_ms: int) -> date | None:
    """The trading day whose snapshot is to be asked for at ``now_ms``, or ``None`` outside every day's grace."""
    day = et_date_at_ms(now_ms)
    if not is_trading_day(day):
        return None
    due = snapshot_due_ms(day)
    return day if due <= now_ms < due + GRACE_MS else None


def _has_snapshot_row(store: IvSnapshotStore, symbol: str, day: date) -> bool:
    """Whether the store holds this slot's row asked for on ``day``. Reads the series only, never a raw chain."""
    return any(
        row.slot == SNAPSHOT_SLOT
        and et_date_at_ms(row.asked_at_ms if row.asked_at_ms is not None else row.snapshot_ts_ms) == day
        for row in store.read_series(symbol)
    )


class IvRecorderSchedule:
    """Records each configured symbol's IV30 once per trading day, five minutes before the close."""

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
        #: ``(symbol, day)`` pairs this process has settled: recorded, found recorded, skipped late, or failed once.
        self._settled: set[tuple[str, date]] = set()

    @classmethod
    def from_settings(cls) -> IvRecorderSchedule | None:
        """The schedule the data plane runs, or ``None`` when ``IV_RECORDER_SYMBOLS`` names no symbol.

        The setting is checked when settings load, so a symbol that is not a ticker never reaches here.
        """
        symbols = parse_symbol_list(settings.IV_RECORDER_SYMBOLS, setting="IV_RECORDER_SYMBOLS")
        if not symbols:
            return None
        from app.services.polygon_client import PolygonClientService

        store, polygon = get_iv_store(), PolygonClientService()

        def record(ticker: str, asof: datetime) -> RecordedIvSnapshot:
            return record_iv_snapshot(ticker=ticker, slot=SNAPSHOT_SLOT, store=store, polygon=polygon, asof=asof)

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
        """Record every symbol whose snapshot is due now and not yet settled. A failure of one never stops the rest."""
        now = self.clock()
        day = et_date_at_ms(now)
        if not is_trading_day(day):
            return
        due = snapshot_due_ms(day)
        if now < due:
            return
        late = now >= due + GRACE_MS
        for symbol in self.symbols:
            if (symbol, day) in self._settled:
                continue
            # Settled before the vendor is asked, so a slow or failing attempt is never begun again today.
            self._settled.add((symbol, day))
            try:
                if await asyncio.to_thread(_has_snapshot_row, self.store, symbol, day):
                    continue
                if late:
                    logger.warning(
                        "IV recorder is past the grace for the day's snapshot and takes none; the next trading day tries again",
                        extra={"action": "iv_recorder_snapshot_skipped_late", "symbol": symbol, "day": day.isoformat(),
                               "late_by_ms": now - due},
                    )
                    continue
                row = await asyncio.to_thread(self._record, symbol, datetime.fromtimestamp(now / 1000, tz=UTC))
            except Exception:
                logger.exception(
                    "IV recorder could not take its snapshot; the next trading day tries again",
                    extra={"action": "iv_recorder_snapshot_failed", "symbol": symbol, "day": day.isoformat()},
                )
                continue
            if row.error is not None:
                logger.warning(
                    "IV recorder filed an error row for its snapshot",
                    extra={"action": "iv_recorder_snapshot_error_row", "symbol": symbol, "day": day.isoformat(), "error": row.error},
                )
            else:
                logger.info(
                    "IV recorder recorded its snapshot",
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
