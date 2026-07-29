"""Orchestration service for synchronized offline bot-trading sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.broker.ibkr.models import IbkrMinuteBar
from app.config import settings
from app.engine.execution.signal_intent_executor import SignalSymbolExecutor
from app.engine.live.artifacts import DECISION_COLUMNS
from app.engine.live.config import LiveConfig
from app.engine.live.live_artifact_io import parquet_row_count
from app.engine.live.live_engine import LiveEngine, LiveRunResult
from app.engine.live.replay_layer import ReplaySimBroker
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.artifact_io import (
    atomic_write_pydantic_artifact,
    read_pydantic_artifact,
)
from app.schemas.offline_replay import (
    OfflineReplayBotResult,
    OfflineReplayBotStatus,
    OfflineReplayCatalogResponse,
    OfflineReplayCommandRequest,
    OfflineReplayCreateRequest,
    OfflineReplaySessionListResponse,
    OfflineReplaySessionResponse,
    OfflineReplaySpeed,
    OfflineReplayStatus,
    OfflineReplaySymbol,
)
from app.services.offline_replay_clock import (
    OfflineReplayClock,
    ReplayTickPermission,
)
from app.services.offline_replay_data import (
    OfflineReplayDataService,
    PreparedOfflineReplay,
)
from app.utils.timestamps import now_ms_utc

_TERMINAL_STATUSES: frozenset[OfflineReplayStatus] = frozenset(
    {"completed", "stopped", "failed", "interrupted"}
)
_EMA_REPLAY_WARMUP_MINUTES = 225
_BOT_PROCESS_TIMEOUT_S = 30.0


class OfflineReplayNotFoundError(LookupError):
    """No replay session exists for the requested identifier."""


class OfflineReplayConflictError(RuntimeError):
    """A command is invalid for the session's current lifecycle state."""


@dataclass(slots=True)
class _FeedItem:
    bar: IbkrMinuteBar
    consumed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _BotRecord:
    symbol: OfflineReplaySymbol
    run_id: str
    initial_cash: Decimal
    data_sha256: str = ""
    status: OfflineReplayBotStatus = "pending"
    bars_processed: int = 0
    decisions_emitted: int = 0
    orders_submitted: int = 0
    fills_observed: int = 0
    open_position_quantity: int = 0
    final_equity: Decimal = Decimal("0")
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(slots=True)
class _SessionRecord:
    session_id: str
    session_date_ms: int
    session_open_ms: int
    session_close_ms: int
    warmup_end_ms: int
    playback_end_ms: int
    playback_minutes: int
    speed: OfflineReplaySpeed
    created_at_ms: int
    symbols: tuple[OfflineReplaySymbol, ...]
    bots: dict[OfflineReplaySymbol, _BotRecord]
    status: OfflineReplayStatus = "preparing"
    playhead_ms: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    clock: OfflineReplayClock | None = None
    task: asyncio.Task[None] | None = None


class OfflineReplayService:
    """Own offline replay lifecycle, virtual time, and durable projections."""

    def __init__(
        self,
        *,
        data_service: OfflineReplayDataService | None = None,
        root: Path | None = None,
        now_ms: Callable[[], int] = now_ms_utc,
        clock_factory: Callable[[OfflineReplaySpeed], OfflineReplayClock] = OfflineReplayClock,
    ) -> None:
        self._data = data_service or OfflineReplayDataService()
        self._root = root or Path(settings.OFFLINE_REPLAY_ROOT)
        self._now_ms = now_ms
        self._clock_factory = clock_factory
        self._sessions: dict[str, _SessionRecord] = {}
        self._archived: dict[str, OfflineReplaySessionResponse] = {}
        self._root.mkdir(parents=True, exist_ok=True)
        self._load_archived_sessions()

    async def catalog(self) -> OfflineReplayCatalogResponse:
        return await asyncio.to_thread(
            self._data.catalog,
            now_ms=self._now_ms(),
            warmup_minutes=_EMA_REPLAY_WARMUP_MINUTES,
            playback_minutes=60,
        )

    async def create_session(
        self,
        request: OfflineReplayCreateRequest,
    ) -> OfflineReplaySessionResponse:
        """Create a durable session record and launch preparation in background."""
        session_date = datetime.fromtimestamp(
            request.session_date_ms / 1000,
            tz=UTC,
        ).astimezone(ZoneInfo("America/New_York")).date()
        window = session_window_for_date(session_date)
        if request.session_date_ms != window.open_ms_utc:
            raise ValueError(
                "session_date_ms must equal the canonical NYSE session-open timestamp"
            )

        session_id = uuid4().hex
        created_at_ms = self._now_ms()
        warmup_end_ms = window.open_ms_utc + _EMA_REPLAY_WARMUP_MINUTES * 60_000
        playback_end_ms = warmup_end_ms + request.playback_minutes * 60_000
        if playback_end_ms > window.close_ms_utc:
            raise ValueError("selected NYSE session is too short for warm-up plus playback")

        symbols = tuple(request.symbols)
        bots = {
            symbol: _BotRecord(
                symbol=symbol,
                run_id=f"{session_id}-{symbol.lower()}",
                initial_cash=request.initial_cash_usd,
                final_equity=request.initial_cash_usd,
            )
            for symbol in symbols
        }
        record = _SessionRecord(
            session_id=session_id,
            session_date_ms=window.open_ms_utc,
            session_open_ms=window.open_ms_utc,
            session_close_ms=window.close_ms_utc,
            warmup_end_ms=warmup_end_ms,
            playback_end_ms=playback_end_ms,
            playback_minutes=request.playback_minutes,
            speed=request.speed,
            created_at_ms=created_at_ms,
            symbols=symbols,
            bots=bots,
        )
        self._sessions[session_id] = record
        self._persist(record)
        record.task = asyncio.create_task(
            self._run_session(record, request),
            name=f"offline-replay-{session_id}",
        )
        return self._project(record)

    def get_session(self, session_id: str) -> OfflineReplaySessionResponse:
        record = self._sessions.get(session_id)
        if record is not None:
            return self._project(record)
        archived = self._archived.get(session_id)
        if archived is not None:
            return archived
        raise OfflineReplayNotFoundError(session_id)

    def list_sessions(self) -> OfflineReplaySessionListResponse:
        sessions = [self._project(record) for record in self._sessions.values()]
        sessions.extend(self._archived.values())
        sessions.sort(key=lambda item: item.created_at_ms, reverse=True)
        return OfflineReplaySessionListResponse(sessions=sessions)

    async def command(
        self,
        session_id: str,
        request: OfflineReplayCommandRequest,
    ) -> OfflineReplaySessionResponse:
        record = self._sessions.get(session_id)
        if record is None:
            if session_id in self._archived:
                raise OfflineReplayConflictError("terminal replay sessions cannot accept commands")
            raise OfflineReplayNotFoundError(session_id)
        if record.status in _TERMINAL_STATUSES:
            raise OfflineReplayConflictError("terminal replay sessions cannot accept commands")
        if record.clock is None:
            if request.action == "stop":
                record.status = "stopping"
                self._persist(record)
                if record.task is not None:
                    record.task.cancel()
                record.status = "stopped"
                record.completed_at_ms = self._now_ms()
                self._persist(record)
                return self._project(record)
            raise OfflineReplayConflictError("replay controls are available after data preparation")

        if request.action == "pause":
            await record.clock.pause()
            record.status = "paused"
        elif request.action == "resume":
            await record.clock.resume()
            record.status = "running"
        elif request.action == "step":
            await record.clock.step()
            record.status = "paused"
        elif request.action == "set_speed":
            assert request.speed is not None
            await record.clock.set_speed(request.speed)
            record.speed = request.speed
        elif request.action == "stop":
            await record.clock.stop()
            record.status = "stopping"
        self._persist(record)
        return self._project(record)

    async def _run_session(
        self,
        record: _SessionRecord,
        request: OfflineReplayCreateRequest,
    ) -> None:
        try:
            prepared = await asyncio.to_thread(
                self._data.prepare,
                session_date_ms=request.session_date_ms,
                symbols=record.symbols,
                warmup_minutes=_EMA_REPLAY_WARMUP_MINUTES,
                playback_minutes=request.playback_minutes,
                auto_fetch=request.auto_fetch,
                fetched_at_ms=record.created_at_ms,
            )
            record.warmup_end_ms = prepared.warmup_end_ms
            record.playback_end_ms = prepared.playback_end_ms
            record.started_at_ms = self._now_ms()
            record.status = "warming_up"
            record.clock = self._clock_factory(request.speed)
            for symbol in record.symbols:
                record.bots[symbol].data_sha256 = prepared.data_sha256_by_symbol[symbol]
            self._persist(record)
            await self._drive_synchronized_bots(record, prepared)
        except asyncio.CancelledError:
            if record.status not in _TERMINAL_STATUSES:
                stop_requested = (
                    record.status == "stopping"
                    or (
                        record.clock is not None
                        and record.clock.stop_requested
                    )
                )
                record.status = "stopped" if stop_requested else "interrupted"
                record.completed_at_ms = self._now_ms()
                if not stop_requested:
                    record.failure_code = "REPLAY_TASK_CANCELLED"
                    record.failure_message = (
                        "replay task was cancelled before reaching a terminal state"
                    )
                self._persist(record)
            raise
        except Exception as exc:
            record.status = "failed"
            record.failure_code = type(exc).__name__
            record.failure_message = str(exc)
            record.completed_at_ms = self._now_ms()
            for bot in record.bots.values():
                if bot.status in {"pending", "running"}:
                    bot.status = "failed"
                    bot.failure_code = type(exc).__name__
                    bot.failure_message = str(exc)
            self._persist(record)

    async def _drive_synchronized_bots(
        self,
        record: _SessionRecord,
        prepared: PreparedOfflineReplay,
    ) -> None:
        assert record.clock is not None
        queues = {
            symbol: asyncio.Queue[_FeedItem | None]()
            for symbol in record.symbols
        }
        tasks = {
            symbol: asyncio.create_task(
                self._run_bot(
                    record,
                    symbol=symbol,
                    feed=self._queue_feed(queues[symbol]),
                ),
                name=f"offline-replay-{record.session_id}-{symbol.lower()}",
            )
            for symbol in record.symbols
        }
        stopped = False
        first_playback_bar = True
        try:
            total_bars = len(prepared.bars_by_symbol[record.symbols[0]])
            for index in range(total_bars):
                warmup = index < prepared.warmup_minutes
                permission = await record.clock.before_tick(warmup=warmup)
                if permission is ReplayTickPermission.STOP:
                    stopped = True
                    break
                if not warmup and not first_playback_bar:
                    await record.clock.pace(permission)
                    permission = await record.clock.before_tick(warmup=False)
                    if permission is ReplayTickPermission.STOP:
                        stopped = True
                        break
                if not warmup:
                    first_playback_bar = False
                    record.status = (
                        "paused"
                        if permission is ReplayTickPermission.STEP
                        else "running"
                    )

                items = {
                    symbol: _FeedItem(prepared.bars_by_symbol[symbol][index])
                    for symbol in record.symbols
                }
                for symbol, item in items.items():
                    await queues[symbol].put(item)
                await asyncio.wait_for(
                    asyncio.gather(*(item.consumed.wait() for item in items.values())),
                    timeout=_BOT_PROCESS_TIMEOUT_S,
                )
                record.playhead_ms = next(iter(items.values())).bar.end_ms
                for bot in record.bots.values():
                    bot.bars_processed = index + 1
                self._persist(record)
                self._raise_if_bot_failed(tasks)
        finally:
            for queue in queues.values():
                await queue.put(None)
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for symbol, result in zip(tasks, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result,
                    asyncio.CancelledError,
                ):
                    bot = record.bots[symbol]
                    bot.status = "failed"
                    bot.failure_code = type(result).__name__
                    bot.failure_message = str(result)

        # STOP can arrive while both engines are consuming the final queued
        # bar, leaving no subsequent clock check to set the local flag.
        stopped = stopped or record.clock.stop_requested
        any_failed = any(bot.status == "failed" for bot in record.bots.values())
        if any_failed:
            record.status = "failed"
            record.failure_code = "BOT_RUNTIME_FAILED"
            record.failure_message = "one or more replay bots failed"
        elif stopped:
            record.status = "stopped"
            for bot in record.bots.values():
                if bot.status == "completed":
                    bot.status = "stopped"
        else:
            record.status = "completed"
        record.completed_at_ms = self._now_ms()
        self._persist(record)

    async def _run_bot(
        self,
        record: _SessionRecord,
        *,
        symbol: OfflineReplaySymbol,
        feed: AsyncIterator[IbkrMinuteBar],
    ) -> None:
        bot = record.bots[symbol]
        bot.status = "running"
        bot_dir = self._session_dir(record.session_id) / "bots" / symbol.lower()
        bot_dir.mkdir(parents=True, exist_ok=True)
        broker = ReplaySimBroker(initial_cash=bot.initial_cash)
        strategy = EmaCrossoverSignalAlgorithm(symbol=symbol)
        engine = LiveEngine(
            None,
            LiveConfig(symbol=symbol, force_flat_at=None),
            broker=broker,
            output_dir=bot_dir,
            account_id=f"REPLAY-{symbol}-{record.session_id[:8]}",
            session_start_ms=record.session_open_ms,
            run_id=bot.run_id,
            strategy_key="ema_crossover_signal",
            strategy_instance_id=f"offline-{symbol.lower()}",
            run_mode="offline_replay",
            bar_source="polygon_historical",
            decision_columns=DECISION_COLUMNS,
            signal_intent_executor=SignalSymbolExecutor(symbol),
            account_registry_gate_enabled=False,
        )
        try:
            result = await engine.run(strategy, ibkr_bars=feed)
        except Exception as exc:
            bot.status = "failed"
            bot.failure_code = type(exc).__name__
            bot.failure_message = str(exc)
            raise
        self._apply_bot_result(bot, result, bot_dir)

    @staticmethod
    async def _queue_feed(
        queue: asyncio.Queue[_FeedItem | None],
    ) -> AsyncIterator[IbkrMinuteBar]:
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                yield item.bar
            finally:
                item.consumed.set()

    @staticmethod
    def _raise_if_bot_failed(tasks: dict[OfflineReplaySymbol, asyncio.Task[None]]) -> None:
        for symbol, task in tasks.items():
            if not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise RuntimeError(f"{symbol} replay bot failed") from exc

    @staticmethod
    def _apply_bot_result(
        bot: _BotRecord,
        result: LiveRunResult,
        bot_dir: Path,
    ) -> None:
        bot.status = "completed"
        bot.bars_processed = len(result.bars)
        bot.decisions_emitted = parquet_row_count(
            bot_dir / "decisions.parquet",
            on_error="raise",
        )
        bot.orders_submitted = len(result.submitted_order_ids)
        bot.fills_observed = len(result.order_events)
        bot.open_position_quantity = result.open_positions.get(bot.symbol, 0)
        bot.final_equity = result.final_equity

    def _project(self, record: _SessionRecord) -> OfflineReplaySessionResponse:
        return OfflineReplaySessionResponse(
            session_id=record.session_id,
            status=record.status,
            strategy_key="ema_crossover_signal",
            session_date_ms=record.session_date_ms,
            session_open_ms=record.session_open_ms,
            session_close_ms=record.session_close_ms,
            warmup_end_ms=record.warmup_end_ms,
            playback_end_ms=record.playback_end_ms,
            playhead_ms=record.playhead_ms,
            playback_minutes=record.playback_minutes,
            speed=record.speed,
            created_at_ms=record.created_at_ms,
            started_at_ms=record.started_at_ms,
            completed_at_ms=record.completed_at_ms,
            symbols=list(record.symbols),
            bots=[
                OfflineReplayBotResult(
                    symbol=bot.symbol,
                    status=bot.status,
                    run_id=bot.run_id,
                    bars_processed=bot.bars_processed,
                    decisions_emitted=bot.decisions_emitted,
                    orders_submitted=bot.orders_submitted,
                    fills_observed=bot.fills_observed,
                    open_position_quantity=bot.open_position_quantity,
                    final_equity_usd=bot.final_equity,
                    data_sha256=bot.data_sha256,
                    failure_code=bot.failure_code,
                    failure_message=bot.failure_message,
                )
                for bot in record.bots.values()
            ],
            failure_code=record.failure_code,
            failure_message=record.failure_message,
        )

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _persist(self, record: _SessionRecord) -> None:
        atomic_write_pydantic_artifact(
            self._session_dir(record.session_id) / "session.json",
            self._project(record),
        )

    def _load_archived_sessions(self) -> None:
        for path in self._root.glob("*/session.json"):
            response = read_pydantic_artifact(
                path,
                OfflineReplaySessionResponse,
            )
            if response is None:
                continue
            if response.status not in _TERMINAL_STATUSES:
                response = response.model_copy(
                    update={
                        "status": "interrupted",
                        "completed_at_ms": self._now_ms(),
                        "failure_code": "SERVICE_RESTARTED",
                        "failure_message": "data service restarted before replay completion",
                    }
                )
                atomic_write_pydantic_artifact(path, response)
            self._archived[response.session_id] = response


_offline_replay_service: OfflineReplayService | None = None


def get_offline_replay_service() -> OfflineReplayService:
    """Return the process-local replay coordinator."""
    global _offline_replay_service
    if _offline_replay_service is None:
        _offline_replay_service = OfflineReplayService()
    return _offline_replay_service
