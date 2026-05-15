"""Async live-engine driver.

Live paper fills are broker-timed, so this driver does not maintain the
backtest engine's deferred fill list. The fake broker used by the replay
gate can still emit deterministic next-minute-open fills, letting the
same strategy class prove live/backtest lifecycle parity without touching
IBKR.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.engine.live.indicator_state import HydratePolicy

from app.broker.ibkr.bars import stream_minute_bars
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import PAPER_PORTS
from app.broker.ibkr.models import IbkrMinuteBar, IbkrOrderEvent
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import EquitySnapshot
from app.engine.execution.order import Direction, OrderEvent
from app.engine.framework.insight_scorer import DefaultInsightScoreFunction
from app.engine.live.artifacts import (
    DecisionRow,
    ExecutionRow,
    LiveArtifactWriters,
    TradeRow,
)
from app.engine.live.bar_adapter import trade_bars_from_ibkr
from app.engine.live.config import LiveConfig
from app.engine.live.halt import (
    FatalHaltError,
    check_lost_fill,
    check_outside_mutation,
    now_ms_utc,
    write_poisoned_flag,
)
from app.engine.live.live_context import LiveContext
from app.engine.live.live_portfolio import (
    BrokerAdapter,
    IbkrBrokerAdapter,
    LiveBrokerEventStreamError,
    LivePortfolio,
)
from app.engine.strategy.base import LoggedTrade, Strategy

logger = logging.getLogger(__name__)


_ENGINE_TZ = ZoneInfo("America/New_York")


async def _next_bar_or_shutdown(
    source_iter,
    shutdown_event: asyncio.Event | None,
) -> tuple[TradeBar | None, bool]:
    """Race the next bar from ``source_iter`` against ``shutdown_event``.

    Returns ``(bar, shutdown_won)``:
      * ``(TradeBar, False)`` — a bar arrived before shutdown
      * ``(None, True)`` — shutdown fired first (or was already set)
      * ``(None, False)`` — source exhausted (``StopAsyncIteration``)

    When ``shutdown_event`` is ``None``, behaves like a normal
    ``__anext__`` and returns ``(bar, False)`` or ``(None, False)``
    on exhaustion.

    Why this exists: the Phase 8 graceful-shutdown check used to live
    inside ``async for minute_bar in source:``. When ``source`` is
    wedged on its own ``__anext__`` (no bars arriving — IBKR error
    420 from a same-IP-binding rejection, a Gateway daily restart,
    a market-halt period), the loop body never runs and the
    shutdown check is never reached. SIGINT can't unwedge the
    engine, so SIGKILL is required after the timeout grace
    (observed 2026-05-13 container run, exit 137 after 30 s).
    Racing ``source.__anext__()`` against ``shutdown_event.wait()``
    means shutdown fires within bounded time even when no bar
    arrives.

    When shutdown wins, the in-flight ``source_iter.__anext__()`` is
    cancelled. Async-generator-style sources (``stream_minute_bars``)
    treat ``CancelledError`` as a normal exit through their
    ``finally`` block, which cancels the IBKR real-time bar
    subscription cleanly.
    """
    if shutdown_event is None:
        try:
            return (await source_iter.__anext__(), False)
        except StopAsyncIteration:
            return (None, False)

    next_task = asyncio.ensure_future(source_iter.__anext__())
    shutdown_task = asyncio.ensure_future(shutdown_event.wait())
    try:
        await asyncio.wait(
            {next_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Cancel and silently drain whichever task lost the race.
        # CancelledError from cancellation is expected; StopAsyncIteration
        # may surface if the source was on its very last item.
        for task in (next_task, shutdown_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await task

    # Surface a real source exception even if shutdown also fired —
    # operators want to see broker stream errors, not have them masked
    # by the graceful-exit path. (Reviewer feedback on PR #231:
    # silent exception swallow when shutdown is concurrent.)
    if next_task.done() and not next_task.cancelled():
        exc = next_task.exception()
        if exc is not None and not isinstance(exc, StopAsyncIteration):
            raise exc

    if shutdown_event.is_set():
        return (None, True)
    if next_task.done() and not next_task.cancelled():
        try:
            return (next_task.result(), False)
        except StopAsyncIteration:
            return (None, False)
    # Defensive: shutdown not set, next_task wasn't completable. Treat
    # as exhaustion to break the caller's loop rather than spinning.
    return (None, False)


# How long to wait for broker.cancel_open_orders during a fatal halt
# before giving up. The whole point of _fatal_halt is to land
# poisoned.flag on disk, and the cancel is best-effort cleanup —
# operator reconciliation handles any straggling orders. Without a
# cap, a hung broker (which is the contamination scenario this path
# exists for) blocks the cancel await indefinitely and the flag
# never gets written. (CodeRabbit P1 from #194.)
FATAL_HALT_CANCEL_TIMEOUT_S = 5.0


class MaxOrdersPerDayExceeded(RuntimeError):
    """Raised when the per-day order cap (§ 9) is exceeded mid-session.

    The cap exists so a buggy strategy or flapping connection can't
    drain the account through repeated submissions in a single day.
    Default cap for the SPY 15-min run is 4 (1 entry + 1 exit + 1
    retry + 1 force-flat). Crossing the cap halts the run; resuming
    requires investigation and a new ``run_id``.
    """


@dataclass(frozen=True)
class _OrderMeta:
    """Per-order context the engine needs to convert IBKR fills.

    ``IbkrOrderEvent`` carries ``order_id`` and the magnitude/price of
    the fill but no symbol, no signed quantity, and no strategy-side
    tag. The engine stamps that context in when it submits, so a fill
    on ``order_id=42`` can be expanded back to a full
    ``OrderEvent(symbol=..., fill_quantity=signed, tag=...)``.

    ``submitted_at_ms`` is the int64 ms UTC of order submission;
    used by halt.check_lost_fill to decide when an unfilled order
    has aged past its expected fill window (§ 7.1 trigger B).
    """

    symbol: str
    tag: str
    signed_qty: int
    submitted_at_ms: int = 0


@runtime_checkable
class ReplayBrokerAdapter(BrokerAdapter, Protocol):
    """Optional fake-broker hooks used by deterministic replay tests."""

    async def advance_bar(self, bar: TradeBar) -> None: ...

    def drain_order_events(self) -> list[OrderEvent]: ...


@runtime_checkable
class IbkrEventAdapter(BrokerAdapter, Protocol):
    """Optional broker hooks for live IBKR fill streaming."""

    async def start_event_stream(self) -> None: ...

    async def stop_event_stream(self) -> None: ...

    def drain_broker_events(self) -> list[IbkrOrderEvent]: ...

    @property
    def stream_failure(self) -> BaseException | None: ...


@dataclass
class LiveRunResult:
    """Captured result of a finite live-engine run."""

    initial_cash: Decimal
    final_equity: Decimal
    total_fees: Decimal
    order_events: list[OrderEvent] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    bars: list[TradeBar] = field(default_factory=list)
    equity_curve: list[EquitySnapshot] = field(default_factory=list)
    insights: list = field(default_factory=list)
    insight_summary: dict = field(default_factory=dict)
    submitted_order_ids: list[int] = field(default_factory=list)
    open_positions: dict[str, int] = field(default_factory=dict)
    pending_orders: int = 0


class LiveEngine:
    """Async runtime for Strategy subclasses against a broker boundary."""

    def __init__(
        self,
        client: IbkrClient | None,
        config: LiveConfig | None = None,
        *,
        broker: BrokerAdapter | None = None,
        output_dir: Path | None = None,
        account_id: str = "",
        readonly: bool = False,
        max_orders_per_day: int | None = None,
        fill_window_ms: int | None = None,
        # NEW: indicator-state persistence.
        artifacts_root: Path | None = None,
        hydrate_policy: HydratePolicy | None = None,
        session_start_ms: int | None = None,
        code_sha: str = "",
        strategy_spec_sha: str = "",
    ) -> None:
        self._client = client
        self._config = config or LiveConfig()
        if broker is not None:
            self._broker = broker
        elif client is not None:
            self._broker = IbkrBrokerAdapter(client)
        else:
            raise ValueError("LiveEngine requires either an IbkrClient or a broker adapter.")
        # Per-order metadata captured at submit time; used to expand
        # IBKR fill events back into engine OrderEvents.
        self._order_meta: dict[int, _OrderMeta] = {}
        # Optional artifact-writer integration. When ``output_dir`` is
        # set, the run() loop opens a LiveArtifactWriters bundle, feeds
        # decisions / executions / trades per Phase B's reconcile schemas,
        # and closes the bundle in finally. Tests and replay paths that
        # don't pass an output_dir see exactly the prior behavior.
        # ``account_id`` populates the executions row's account_id
        # column; defaults to empty string when no broker connection
        # exists (replay tests).
        self._output_dir = output_dir
        self._account_id = account_id
        # ``readonly``: drain the strategy's pending orders without
        # actually calling broker.place_order. The strategy still runs
        # and publishes decision snapshots; the executions parquet
        # stays empty (correct — no fills happened). This is what
        # powers Phase D's dry-run mode.
        self._readonly = readonly
        # ``max_orders_per_day``: § 9 operational safety. None disables
        # the cap (replay tests). Counter resets on the date boundary
        # of the most-recent bar; an attempt past the cap raises
        # MaxOrdersPerDayExceeded mid-run, surfacing as a halt.
        self._max_orders_per_day = max_orders_per_day
        # ``fill_window_ms``: how long to wait before declaring a
        # Python-owned order's fill lost (§ 7.1 trigger B). Default
        # is one consolidator period + 60s slack. ``None`` disables
        # both the lost-fill and outside-mutation halt detection
        # (replay tests). When set, the engine writes poisoned.flag
        # and raises FatalHaltError on either trigger.
        if fill_window_ms is None:
            self._fill_window_ms = self._config.consolidator_period_min * 60 * 1000 + 60_000
        else:
            self._fill_window_ms = fill_window_ms
        self._halt_enabled = output_dir is not None  # need run_dir to write poisoned.flag
        self._artifacts_root = artifacts_root
        self._hydrate_policy = hydrate_policy
        self._session_start_ms = session_start_ms
        self._code_sha = code_sha
        self._strategy_spec_sha = strategy_spec_sha

    def _validate_paper_client(self) -> None:
        if self._client is None:
            return
        settings = self._client.settings
        if settings.mode != "paper":
            raise RuntimeError(f"LiveEngine paper runtime requires IBKR_MODE=paper, got {settings.mode!r}.")
        if settings.port not in PAPER_PORTS:
            raise RuntimeError(f"LiveEngine paper runtime requires paper port, got {settings.port}.")
        account_id = self._client.connected_account
        if account_id is None or not account_id.upper().startswith("DU"):
            raise RuntimeError(f"LiveEngine paper runtime requires a DU paper account, got {account_id!r}.")

    async def run(
        self,
        strategy: Strategy,
        bars: AsyncIterable[TradeBar] | None = None,
        *,
        ibkr_bars: AsyncIterable[IbkrMinuteBar] | None = None,
        shutdown_event: asyncio.Event | None = None,
    ) -> LiveRunResult:
        """Run a strategy against supplied bars or the real IBKR bar stream.

        ``bars`` accepts engine ``TradeBar`` instances (used by replay
        tests). ``ibkr_bars`` accepts wire-typed ``IbkrMinuteBar``
        instances and is wrapped through the bar adapter — this is the
        production path's shape, exposed for tests that drive the
        engine without a live IBKR connection.

        ``shutdown_event`` is the graceful-shutdown hook for SIGINT /
        SIGTERM (wired in ``run.py`` cmd_start). When set, the bar
        loop runs ``_shutdown_flatten`` (cancel open broker orders +
        liquidate positions + submit the liquidations) and exits
        cleanly through the existing finally block (artifact writers
        close, event stream stops). Responsiveness: the event is
        checked once per minute-bar tick, so SIGINT honors at most one
        minute late under real IBKR. Set to ``None`` (default) to
        disable — replay and FakeBroker tests that don't need
        graceful-shutdown semantics keep the prior behavior.
        """
        if bars is not None and ibkr_bars is not None:
            raise ValueError("supply at most one of bars or ibkr_bars")

        self._validate_paper_client()
        portfolio = LivePortfolio(self._broker)
        await portfolio.refresh_from_broker()
        initial_cash = portfolio.cash
        ctx = LiveContext(
            portfolio=portfolio,
            hydrate_policy=self._hydrate_policy,
            run_dir=self._output_dir,
            artifacts_root=self._artifacts_root,
            session_start_ms=self._session_start_ms,
        )
        strategy.ctx = ctx
        strategy.initialize()

        # NEW: hydrate call site (after initialize, before bar loop).
        if self._hydrate_policy is not None:
            ctx.hydrate_indicator_state(strategy)

        if len(ctx.symbols) != 1:
            raise NotImplementedError("LiveEngine v1 supports a single symbol only")
        symbol = ctx.symbols[0]
        if bars is not None:
            source = bars
        elif ibkr_bars is not None:
            source = trade_bars_from_ibkr(ibkr_bars)
        else:
            source = trade_bars_from_ibkr(stream_minute_bars(self._client, symbol))

        order_events: list[OrderEvent] = []
        retained_bars: list[TradeBar] = []
        equity_curve: list[EquitySnapshot] = []
        submitted_order_ids: list[int] = []
        previous_bar: TradeBar | None = None
        last_force_flat_date: date | None = None
        force_flat_at = self._config.force_flat_at

        # § 9 max-orders-per-day enforcement. Counter resets on the
        # date boundary of each new bar; crossing the cap raises
        # MaxOrdersPerDayExceeded which surfaces as a halt to the
        # caller (run.py turns this into an exit code).
        orders_submitted_today: int = 0
        current_session_date: date | None = None

        # § 7 fatal-halt state. ``seen_executions`` accumulates dict
        # rows (one per broker fill event observed) so check_lost_fill
        # can match owned orders against actual fills, and so
        # check_outside_mutation runs against the cumulative set
        # rather than just the per-bar drain. Both are no-ops when
        # halt detection isn't enabled (replay tests).
        seen_executions: list[dict] = []

        # Artifact-writer integration. Bundle is None when no
        # output_dir is configured — keeps replay tests (FakeBroker
        # paths) free of file IO. Per-bar dedupe state for the
        # decision writer: we only write when last_decision_snapshot
        # carries a bar_close_ms we haven't seen.
        writers: LiveArtifactWriters | None = None
        last_written_decision_ms: int | None = None
        last_written_trade_count = 0
        if self._output_dir is not None:
            writers = LiveArtifactWriters.open(self._output_dir)

        started_event_stream = False
        if isinstance(self._broker, IbkrEventAdapter):
            await self._broker.start_event_stream()
            started_event_stream = True

        source_iter = source.__aiter__()
        last_bar: TradeBar | None = None
        try:
            while True:
                minute_bar, shutdown_won = await _next_bar_or_shutdown(source_iter, shutdown_event)
                if minute_bar is None:
                    if shutdown_won:
                        # Graceful shutdown via SIGINT/SIGTERM. cancel_open_orders +
                        # liquidate + submit happen via _shutdown_flatten; the
                        # existing finally block (post-break) flushes writers and
                        # stops the broker event stream. ``last_bar.end_time``
                        # gives the historical "use the last bar's time" behavior
                        # when bars were flowing; the wall-clock fallback covers
                        # the wedged-source case (issue surfaced 2026-05-13)
                        # where no bar ever arrived.
                        fallback_time = last_bar.end_time if last_bar is not None else datetime.now(_ENGINE_TZ)
                        ctx.log(f"[SHUTDOWN] {fallback_time}: shutdown_event set; flattening and exiting")
                        flat_acks = await self._shutdown_flatten(portfolio, ctx, bar_time=fallback_time)
                        submitted_order_ids.extend(ack.order_id for ack in flat_acks)
                    # source exhausted (shutdown_won=False) OR shutdown finished —
                    # either way the bar loop is done.
                    break
                last_bar = minute_bar
                self._raise_if_event_stream_failed()
                # Reset per-day order counter on session-date boundary.
                bar_date = minute_bar.time.date()
                if current_session_date is None or bar_date != current_session_date:
                    orders_submitted_today = 0
                    current_session_date = bar_date
                await self._process_replay_broker_bar(minute_bar)
                for event in self._drain_replay_order_events():
                    portfolio.record_broker_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
                    if writers is not None:
                        self._write_execution(writers, event)
                        last_written_trade_count = self._flush_new_trades(writers, strategy, last_written_trade_count)

                # Drain real-broker events ONCE; halt-check the raw
                # IbkrOrderEvent list (which may contain foreign fills
                # — adapter no longer filters by ownership per § 7),
                # then convert to engine OrderEvents for the existing
                # portfolio + strategy + writer flow. Drain-once is
                # important: drain_broker_events clears the buffer, so
                # a second drain in _drain_real_broker_order_events
                # would see nothing.
                raw_real_events = self._drain_raw_real_broker_events()
                if self._halt_enabled and raw_real_events:
                    last_clean_ms = (
                        int(previous_bar.end_time.timestamp() * 1000)
                        if previous_bar is not None
                        else int(minute_bar.time.timestamp() * 1000)
                    )
                    self._extend_seen_executions(seen_executions, raw_real_events)
                    await self._check_halt_outside_mutation(
                        seen_executions,
                        last_clean_bar_close_ms=last_clean_ms,
                        portfolio=portfolio,
                        writers=writers,
                    )
                for raw_event in raw_real_events:
                    engine_event = self._convert_ibkr_fill(raw_event)
                    if engine_event is None:
                        continue
                    portfolio.record_broker_fill(engine_event)
                    order_events.append(engine_event)
                    strategy.on_order_event(engine_event)
                    if writers is not None:
                        self._write_execution(writers, engine_event)
                        last_written_trade_count = self._flush_new_trades(writers, strategy, last_written_trade_count)

                # Force-flat barrier: at most once per session date, when the
                # bar's wall-clock time crosses ``force_flat_at``, cancel the
                # runner's in-flight orders and submit a market liquidation
                # for every open position. Real-broker fills land on the
                # next print after submission; under FakeBroker they fill on
                # the next bar's open. Mirrors BacktestEngine's session-close
                # barrier in spirit; the backtest synthesizes fills at the
                # current bar's close so that path is not parity-equivalent
                # in the strict sense (documented in the Phase 5 audit), but
                # the operator-visible outcome — no position survives the
                # session — is.
                if (
                    force_flat_at is not None
                    and minute_bar.time.time() >= force_flat_at
                    and minute_bar.time.date() != last_force_flat_date
                ):
                    # Clear any orders the strategy queued earlier in this bar
                    # that have not been submitted; they would otherwise compete
                    # with the liquidation about to be sent.
                    portfolio.pending_orders.clear()
                    cancelled = await self._broker.cancel_open_orders()
                    if cancelled:
                        ctx.log(
                            f"[FORCE-FLAT] {minute_bar.time}: cancelled "
                            f"{len(cancelled)} open broker order(s) {cancelled!r}"
                        )
                    liquidations = 0
                    for sym, pos in list(portfolio.positions.items()):
                        if pos.quantity == 0:
                            continue
                        portfolio.liquidate(sym, minute_bar.end_time)
                        liquidations += 1
                    if liquidations:
                        flat_acks = await self._submit_pending_with_meta(portfolio)
                        submitted_order_ids.extend(ack.order_id for ack in flat_acks)
                        # Force-flat orders count toward the per-day cap
                        # too — § 9 doesn't carve out an exemption.
                        # Without this, a session with cap=4 + 3 normal
                        # orders + a force-flat liquidation would silently
                        # land at 4 actual broker orders without crossing
                        # the counter check that protects against runaway
                        # submissions. (CodeRabbit P1 from #186.)
                        orders_submitted_today += len(flat_acks)
                        if self._max_orders_per_day is not None and orders_submitted_today > self._max_orders_per_day:
                            raise MaxOrdersPerDayExceeded(
                                f"force-flat pushed total to {orders_submitted_today} on "
                                f"{current_session_date} (cap={self._max_orders_per_day})"
                            )
                        ctx.log(f"[FORCE-FLAT] {minute_bar.time}: submitted {liquidations} liquidation order(s)")
                    strategy.on_force_flat()
                    last_force_flat_date = minute_bar.time.date()
                    # NEW: indicator-state checkpoint at force-flat.
                    ctx.maybe_write_indicator_state(
                        strategy,
                        reason="force_flat",
                        code_sha=self._code_sha,
                        strategy_spec_sha=self._strategy_spec_sha,
                        last_consolidated_bar_end_ms=int(minute_bar.end_time.timestamp() * 1000),
                    )

                portfolio.update_reference_price(symbol, minute_bar.close)
                consolidated_count_before = len(ctx.consolidated_bars)
                for consolidator in ctx.get_consolidators(symbol):
                    consolidator.update(minute_bar)
                consolidated_emitted = len(ctx.consolidated_bars) - consolidated_count_before

                # Operator-facing heartbeat — operators tail live.log to
                # confirm bars are flowing during the strategy's indicator
                # warmup window (≥3 h 45 m for SpyEmaCrossoverAlgorithm —
                # RSI(14) is_ready at samples >= period + 1, so 15 × 15-min
                # bars). Without this, warmup is silent and an "engine
                # running, strategy in warmup" run is indistinguishable
                # from "engine hung." See issue #228 (and the #227
                # misdiagnosis it prevents recurring).
                logger.info(
                    "[BAR] %s consolidator_emitted=%d snapshot=%s",
                    minute_bar.time.isoformat(),
                    consolidated_emitted,
                    "set" if strategy.last_decision_snapshot is not None else "None",
                )

                # Snapshot publication runs inside the strategy bar
                # handler; capture it here, deduped by bar_close_ms so
                # we never write the same consolidated bar twice (a
                # consolidator may be silent on most minute bars).
                if writers is not None:
                    last_written_decision_ms = self._maybe_write_decision(writers, strategy, last_written_decision_ms)

                # § 7.1 trigger B runs BEFORE _submit_pending_with_meta
                # so any order overdue from a prior bar gates new
                # submissions on this bar — without this ordering, an
                # overdue prior order would let new orders through and
                # only halt afterwards, contaminating the broker state
                # we're already failing to reconcile. Just-submitted
                # orders aren't overdue (age ≈ 0 ≪ fill_window) so
                # being invisible to the check this iteration is fine
                # — they age normally and get caught next bar.
                # (CodeRabbit P1 from #194 reversed the prior #194
                # change that moved this AFTER the submit.)
                if self._halt_enabled:
                    last_clean_ms = int(minute_bar.end_time.timestamp() * 1000)
                    await self._check_halt_lost_fill(
                        seen_executions,
                        last_clean_bar_close_ms=last_clean_ms,
                        portfolio=portfolio,
                        writers=writers,
                    )

                submitted = await self._submit_pending_with_meta(portfolio)
                submitted_order_ids.extend(ack.order_id for ack in submitted)
                orders_submitted_today += len(submitted)
                if self._max_orders_per_day is not None and orders_submitted_today > self._max_orders_per_day:
                    raise MaxOrdersPerDayExceeded(
                        f"submitted {orders_submitted_today} orders on {current_session_date} "
                        f"(cap={self._max_orders_per_day})"
                    )

                current_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) for sym in ctx.symbols}
                ctx.insight_manager.step(minute_bar.end_time, current_prices)
                equity_curve.append(
                    EquitySnapshot(
                        timestamp=minute_bar.end_time,
                        equity=portfolio.total_value(),
                        cash=portfolio.cash,
                        holdings_value=portfolio.total_value() - portfolio.cash,
                    )
                )
                retained_bars.append(minute_bar)
                previous_bar = minute_bar

            # Source exhausted. Stop the stream first so the task flushes
            # any in-flight event into the buffer, then drain. Without
            # this final pass, fills that arrive between the last per-bar
            # drain and shutdown stay buffered and never reach the
            # portfolio or strategy — common on finite test/replay
            # streams and on real-broker shutdown.
            if started_event_stream and isinstance(self._broker, IbkrEventAdapter):
                await self._broker.stop_event_stream()
                started_event_stream = False
                self._raise_if_event_stream_failed()
                final_raw_events = self._drain_raw_real_broker_events()
                if self._halt_enabled and final_raw_events:
                    last_clean_ms = int(previous_bar.end_time.timestamp() * 1000) if previous_bar is not None else 0
                    self._extend_seen_executions(seen_executions, final_raw_events)
                    await self._check_halt_outside_mutation(
                        seen_executions,
                        last_clean_bar_close_ms=last_clean_ms,
                        portfolio=portfolio,
                        writers=writers,
                    )
                for raw_event in final_raw_events:
                    engine_event = self._convert_ibkr_fill(raw_event)
                    if engine_event is None:
                        continue
                    portfolio.record_broker_fill(engine_event)
                    order_events.append(engine_event)
                    strategy.on_order_event(engine_event)
                    if writers is not None:
                        self._write_execution(writers, engine_event)
                        last_written_trade_count = self._flush_new_trades(writers, strategy, last_written_trade_count)
        finally:
            # NEW: indicator-state checkpoint at graceful shutdown.
            if last_bar is not None:
                try:
                    ctx.maybe_write_indicator_state(
                        strategy,
                        reason="shutdown",
                        code_sha=self._code_sha,
                        strategy_spec_sha=self._strategy_spec_sha,
                        last_consolidated_bar_end_ms=int(last_bar.end_time.timestamp() * 1000),
                    )
                except Exception:
                    logger.exception("indicator-state shutdown checkpoint failed; continuing finally cleanup")
            if started_event_stream and isinstance(self._broker, IbkrEventAdapter):
                await self._broker.stop_event_stream()
            if writers is not None:
                writers.close_all()

        strategy.on_end_of_algorithm()
        if previous_bar is not None:
            final_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) for sym in ctx.symbols}
            for insight in ctx.insight_manager.get_active_insights(previous_bar.end_time):
                if not insight.score.is_final_score:
                    insight.reference_value_final = final_prices.get(insight.symbol, Decimal(0))
                    DefaultInsightScoreFunction().score(insight)
                    insight.score.finalize(previous_bar.end_time)

        open_positions = {sym: pos.quantity for sym, pos in portfolio.positions.items() if pos.quantity != 0}
        return LiveRunResult(
            initial_cash=initial_cash,
            final_equity=portfolio.total_value(),
            total_fees=portfolio.total_fees,
            order_events=order_events,
            log_lines=list(ctx.log_lines),
            bars=retained_bars,
            equity_curve=equity_curve,
            insights=ctx.insight_manager.all_insights,
            insight_summary=ctx.insight_manager.get_summary().to_dict(),
            submitted_order_ids=submitted_order_ids,
            open_positions=open_positions,
            pending_orders=len(portfolio.pending_orders),
        )

    # ──────────────────── Artifact-writer helpers ────────────────────

    @staticmethod
    def _maybe_write_decision(
        writers: LiveArtifactWriters,
        strategy: Strategy,
        last_written_ms: int | None,
    ) -> int | None:
        """Append ``strategy.last_decision_snapshot`` if it's new.

        Returns the bar_close_ms now considered "written" (the input
        ``last_written_ms`` if no new snapshot exists, or the snapshot's
        ms if a new one was just appended). The dedupe is necessary
        because the consolidator only fires on 15-min boundaries — most
        minute bars leave ``last_decision_snapshot`` unchanged from the
        prior iteration.
        """
        snap = strategy.last_decision_snapshot
        if snap is None or snap.bar_close_ms == last_written_ms:
            return last_written_ms
        writers.decisions.append_row(
            DecisionRow(
                bar_close_ms=snap.bar_close_ms,
                ema5=snap.ema5,
                ema10=snap.ema10,
                rsi=snap.rsi,
                signal=snap.signal,
                intended_price=snap.intended_price,
            )
        )
        return snap.bar_close_ms

    def _write_execution(self, writers: LiveArtifactWriters, event: OrderEvent) -> None:
        """Append one execution row for an engine-converted broker fill.

        Notes on the synthetic fields: ``exec_id`` and ``perm_id`` are
        not on the engine ``OrderEvent`` shape (only on the wire
        ``IbkrOrderEvent``). Until Phase C-2c surfaces those through a
        dedicated execution channel, we synthesize stable identifiers
        from the order_id + tag so the row is still well-formed and the
        reconciler's hash sidecar covers a real file. § 7's intra-day
        fatal halt that genuinely needs broker-primary-key indexing is
        a separate code path on a separate writer — this PR only
        produces the receipt artifact.
        """
        writers.executions.append_row(
            ExecutionRow(
                ts_ms=int(event.time.timestamp() * 1000),
                exec_id=f"engine-{event.order_id}",
                perm_id=int(event.order_id),
                client_order_id=f"live-{event.order_id}",
                account_id=self._account_id,
                symbol=event.symbol,
                fill_quantity=int(event.fill_quantity),
                fill_price=float(event.fill_price),
                fee=float(event.fee),
            )
        )

    @staticmethod
    def _flush_new_trades(
        writers: LiveArtifactWriters,
        strategy: Strategy,
        last_written_count: int,
    ) -> int:
        """Append any trades the strategy added since the last call.

        Strategies that don't carry a trade_log attribute (the base
        Strategy class doesn't) skip this entirely. SpyEmaCrossover
        appends to its trade_log on every closed round-trip in
        on_order_event; we observe the delta here.
        """
        trade_log: list[LoggedTrade] | None = getattr(strategy, "trade_log", None)
        if trade_log is None:
            return last_written_count
        new_trades = trade_log[last_written_count:]
        for trade in new_trades:
            writers.trades.append_row(
                TradeRow(
                    entry_time_ms=int(trade.entry_time.timestamp() * 1000),
                    exit_time_ms=int(trade.exit_time.timestamp() * 1000),
                    entry_price=float(trade.entry_price),
                    exit_price=float(trade.exit_price),
                    pnl_points=float(trade.pnl_pts),
                )
            )
        return len(trade_log)

    async def _submit_pending_with_meta(self, portfolio: LivePortfolio):
        """Submit queued orders and remember their per-id metadata.

        ``LivePortfolio.submit_pending_orders`` drains pending orders in
        FIFO order and returns acks in the same order. We snapshot the
        pending list first so we can pair each ack back to the originating
        ``Order`` for ``_OrderMeta`` bookkeeping. That pairing is what
        lets ``_drain_real_broker_order_events`` rebuild a full engine
        ``OrderEvent`` from a wire ``IbkrOrderEvent``.

        ``submitted_at_ms`` (captured via ``now_ms_utc()`` after the
        broker ack returns) feeds the § 7 lost-fill check in
        halt.check_lost_fill — orders aged past the fill window
        without a matching execution trip a fatal halt.

        Read-only mode: drain the strategy's pending orders so the
        portfolio doesn't keep them queued forever, but never call
        broker.place_order. Returns an empty ack list — no fills will
        come back, which is correct for the dry run (executions stay
        empty; the strategy's own _in_position / trade_log evolves on
        its internal countdown).
        """
        pending_snapshot = list(portfolio.pending_orders)
        if self._readonly:
            portfolio.pending_orders.clear()
            return []
        acks = await portfolio.submit_pending_orders()
        submitted_at_ms = now_ms_utc()
        for order, ack in zip(pending_snapshot, acks, strict=True):
            self._order_meta[int(ack.order_id)] = _OrderMeta(
                symbol=order.symbol,
                tag=order.tag,
                signed_qty=int(order.quantity),
                submitted_at_ms=submitted_at_ms,
            )
        return acks

    # ──────────────────── Graceful shutdown helper ───────────────────

    async def _shutdown_flatten(
        self,
        portfolio: LivePortfolio,
        ctx: LiveContext,
        *,
        bar_time,
    ) -> list:
        """Cancel + flatten + submit liquidations for a graceful shutdown.

        Same broker effects as the force-flat barrier (cancel open
        orders, liquidate every open position, submit) minus the
        ``strategy.on_force_flat()`` callback — which is specifically
        for session-close, not external SIGINT. Also unlike the
        barrier, this does **not** raise ``MaxOrdersPerDayExceeded``
        on cap overage: the operator chose to exit, and leaving a
        position open to honor the cap defeats the point.

        Returns the list of submitted order acks so the caller can
        record their IDs in ``submitted_order_ids``.
        """
        portfolio.pending_orders.clear()
        try:
            cancelled = await self._broker.cancel_open_orders()
        except Exception:
            # Mirror _fatal_halt's tolerance: best-effort cancel, log
            # and continue so the flatten still runs.
            logger.exception("broker.cancel_open_orders failed during shutdown_flatten")
            cancelled = []
        if cancelled:
            ctx.log(f"[SHUTDOWN] {bar_time}: cancelled {len(cancelled)} open broker order(s) {cancelled!r}")
        liquidations = 0
        for sym, pos in list(portfolio.positions.items()):
            if pos.quantity == 0:
                continue
            portfolio.liquidate(sym, bar_time)
            liquidations += 1
        if liquidations == 0:
            return []
        flat_acks = await self._submit_pending_with_meta(portfolio)
        ctx.log(f"[SHUTDOWN] {bar_time}: submitted {liquidations} liquidation order(s)")
        return flat_acks

    # ──────────────────── § 7 fatal-halt helpers ─────────────────────

    def _drain_raw_real_broker_events(self) -> list[IbkrOrderEvent]:
        """Drain the broker adapter's raw event buffer (unfiltered).

        Caller is responsible for both halt-checking the result and
        running it through ``_convert_ibkr_fill`` for the engine's
        portfolio + strategy + writer flow. Drain-once: the adapter
        clears its buffer on read, so callers must not invoke
        ``self._broker.drain_broker_events`` again in the same loop
        iteration.
        """
        if not isinstance(self._broker, IbkrEventAdapter):
            return []
        return self._broker.drain_broker_events()

    def _extend_seen_executions(self, seen_executions: list[dict], raw_events: list[IbkrOrderEvent]) -> None:
        """Append fill events from a raw drain to the cumulative executions list.

        Each entry is the dict shape ``halt.check_outside_mutation``
        and ``halt.check_lost_fill`` consume. ``client_order_id`` is
        derived from ``LivePortfolio``'s naming convention
        (``f"live-{order_id}"``); foreign fills whose ``order_id`` is
        not in ``self._order_meta`` get ``client_order_id=None`` and
        are detected as foreign by the outside-mutation check.
        """
        for event in raw_events:
            if event.event_type != "fill":
                continue
            owned = int(event.order_id) in self._order_meta
            seen_executions.append(
                {
                    "client_order_id": f"live-{event.order_id}" if owned else None,
                    "exec_id": event.exec_id,
                    "perm_id": event.perm_id,
                    "account_id": event.account_id,
                    "client_id": event.client_id,
                    # ``remaining`` is the order's leftover quantity AFTER
                    # this execution. ``check_lost_fill`` treats an order
                    # as complete iff ``remaining == 0`` for some
                    # execution — without this, a 1-share partial on a
                    # 200-share order would suppress the lost-fill halt.
                    "remaining": event.remaining,
                }
            )

    async def _check_halt_outside_mutation(
        self,
        seen_executions: list[dict],
        *,
        last_clean_bar_close_ms: int,
        portfolio: LivePortfolio,
        writers: LiveArtifactWriters | None,
    ) -> None:
        """Run § 7.1 trigger A. On halt, perform fatal-halt cleanup and raise."""
        owned_client_order_ids = {f"live-{oid}" for oid in self._order_meta}
        reason = check_outside_mutation(
            seen_executions,
            owned_client_order_ids,
            halted_at_ms=now_ms_utc(),
            last_clean_bar_close_ms=last_clean_bar_close_ms,
        )
        if reason is None:
            return
        await self._fatal_halt(reason, portfolio=portfolio, writers=writers)

    async def _check_halt_lost_fill(
        self,
        seen_executions: list[dict],
        *,
        last_clean_bar_close_ms: int,
        portfolio: LivePortfolio,
        writers: LiveArtifactWriters | None,
    ) -> None:
        """Run § 7.1 trigger B. On halt, perform fatal-halt cleanup and raise."""
        owned_orders = [
            {"client_order_id": f"live-{oid}", "submitted_at_ms": meta.submitted_at_ms}
            for oid, meta in self._order_meta.items()
        ]
        reason = check_lost_fill(
            owned_orders,
            seen_executions,
            fill_window_ms=self._fill_window_ms,
            current_time_ms=now_ms_utc(),
            last_clean_bar_close_ms=last_clean_bar_close_ms,
        )
        if reason is None:
            return
        await self._fatal_halt(reason, portfolio=portfolio, writers=writers)

    async def _fatal_halt(
        self,
        reason,
        *,
        portfolio: LivePortfolio,
        writers: LiveArtifactWriters | None,
    ) -> None:
        """Cleanup + write poisoned.flag + raise FatalHaltError.

        Order:
          1. Clear strategy's still-queued pending orders so a
             finally-block consumer doesn't try to submit them after
             the halt fires.
          2. Cancel any already-submitted Python-owned broker orders
             (§ 7.2 #2). Best-effort — the broker may not respond
             cleanly during a contaminated session, and we'd rather
             raise than block on it.
          3. Flush writers so partial parquets are on-disk before the
             flag.
          4. Write poisoned.flag (atomic via open('x') from #190;
             first-halt-wins enforced at the OS level).
          5. Raise FatalHaltError carrying the reason.

        (CodeRabbit P1 from #193 added step 2 — the prior version
        only cleared local pending_orders and missed the in-flight
        broker orders.)
        """
        portfolio.pending_orders.clear()
        try:
            cancelled = await asyncio.wait_for(
                self._broker.cancel_open_orders(),
                timeout=FATAL_HALT_CANCEL_TIMEOUT_S,
            )
            if cancelled:
                logger.info(
                    "fatal halt cancelled %d Python-owned broker order(s): %r",
                    len(cancelled),
                    cancelled,
                )
        except TimeoutError:
            # CodeRabbit P1 from #194: an unresponsive broker
            # (which is exactly the contamination scenario this
            # path exists for) used to block the cancel await
            # indefinitely, swallowing poisoned.flag entirely.
            # Cap it — the flag matters more than the cancellation.
            logger.exception(
                "broker.cancel_open_orders timed out after %ss during fatal halt; "
                "operator must reconcile any open orders manually",
                FATAL_HALT_CANCEL_TIMEOUT_S,
            )
        except Exception:
            # The broker is presumed unhealthy at this point — don't
            # block the halt waiting for cancellation. The poisoned
            # flag still gets written; operator reconciliation
            # cleans up any straggling orders manually.
            logger.exception("broker.cancel_open_orders failed during fatal halt")
        if writers is not None:
            try:
                writers.flush_all()
            except Exception:
                logger.exception("writers.flush_all failed during fatal halt")
        if self._output_dir is not None:
            try:
                write_poisoned_flag(self._output_dir, reason)
            except FileExistsError:
                # First halt already wrote it; preserve the original
                # cause per spec § 7 first-halt-wins invariant.
                logger.info("poisoned.flag already exists; preserving original halt cause")
            except Exception:
                logger.exception("write_poisoned_flag failed during fatal halt")
        raise FatalHaltError(reason)

    def _raise_if_event_stream_failed(self) -> None:
        """Fail the run if the broker event-stream task died.

        Continuing to submit orders against a broker we can no longer
        receive fills from would silently desync portfolio and strategy
        state from broker reality.
        """
        if not isinstance(self._broker, IbkrEventAdapter):
            return
        failure = self._broker.stream_failure
        if failure is None:
            return
        raise LiveBrokerEventStreamError("IBKR order-event stream terminated unexpectedly; aborting run") from failure

    async def _process_replay_broker_bar(self, bar: TradeBar) -> None:
        if isinstance(self._broker, ReplayBrokerAdapter):
            await self._broker.advance_bar(bar)

    def _drain_replay_order_events(self) -> list[OrderEvent]:
        if isinstance(self._broker, ReplayBrokerAdapter):
            return self._broker.drain_order_events()
        return []

    def _drain_real_broker_order_events(self) -> list[OrderEvent]:
        """Convert any pending IBKR fill events into engine OrderEvents."""
        if not isinstance(self._broker, IbkrEventAdapter):
            return []
        out: list[OrderEvent] = []
        for fill in self._broker.drain_broker_events():
            engine_event = self._convert_ibkr_fill(fill)
            if engine_event is not None:
                out.append(engine_event)
        return out

    def _convert_ibkr_fill(self, fill: IbkrOrderEvent) -> OrderEvent | None:
        """Translate one ``IbkrOrderEvent`` to an engine ``OrderEvent``.

        Returns ``None`` for non-fill events (status/cancel/error) and
        for events whose ``order_id`` was not placed by this runner —
        the latter means the foreign order leaked through the adapter
        ownership filter and is treated as a no-op so we never apply a
        stranger's fill to our portfolio.
        """
        if fill.event_type != "fill":
            return None
        meta = self._order_meta.get(int(fill.order_id))
        if meta is None:
            logger.warning(
                "Dropping IBKR fill for unknown order_id=%s (not placed by this runner)",
                fill.order_id,
            )
            return None
        magnitude = int(fill.fill_quantity or 0)
        if magnitude == 0:
            return None
        signed_fill_qty = magnitude if meta.signed_qty > 0 else -magnitude
        price_source = fill.last_fill_price if fill.last_fill_price is not None else fill.avg_fill_price
        if price_source is None:
            logger.warning(
                "Dropping IBKR fill for order_id=%s with no fill price",
                fill.order_id,
            )
            return None
        fill_price = Decimal(str(price_source))
        direction = Direction.LONG if signed_fill_qty > 0 else Direction.SHORT
        fill_time = datetime.fromtimestamp(fill.ts_ms / 1000, tz=UTC).astimezone(_ENGINE_TZ)
        # Commission is reported separately by IBKR (commissionReport
        # callback, not order-status events). Fee-tolerance reconciliation
        # is the Phase-9 paper-vs-broker step; keep this fee zero here so
        # the live receipt does not silently invent a commission number.
        return OrderEvent(
            order_id=int(fill.order_id),
            symbol=meta.symbol,
            time=fill_time,
            fill_price=fill_price,
            fill_quantity=signed_fill_qty,
            direction=direction,
            fee=Decimal("0"),
            tag=meta.tag,
        )
