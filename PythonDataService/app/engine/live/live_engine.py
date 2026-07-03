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
import json
import logging
import time
from collections.abc import AsyncIterable, Callable
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
from app.engine.execution.order_sizer import OrderSizer, WholeAccountPortfolioValueProvider
from app.engine.framework.insight_scorer import DefaultInsightScoreFunction
from app.engine.live.artifacts import (
    CORE_DECISION_COLUMNS,
    DECISION_COLUMNS,
    DecisionRow,
    ExecutionRow,
    LiveArtifactWriters,
    TradeRow,
)
from app.engine.live.bar_adapter import trade_bars_from_ibkr
from app.engine.live.broker_callbacks import (
    BrokerCallbackWal,
)
from app.engine.live.broker_callbacks import (
    broker_callbacks_wal_path as default_broker_callbacks_wal_path,
)
from app.engine.live.command_channel import (
    Command,
    CommandChannel,
    CommandChannelCorruptError,
    CommandVerb,
)
from app.engine.live.config import LiveConfig
from app.engine.live.desired_state import DesiredState
from app.engine.live.halt import (
    FatalHaltError,
    PoisonedHaltReason,
    PoisonedHaltTrigger,
    check_lost_fill,
    check_outside_mutation,
    write_poisoned_flag,
)
from app.engine.live.live_context import LiveContext
from app.engine.live.live_portfolio import (
    BrokerAdapter,
    ControlledLiveHaltError,
    IbkrBrokerAdapter,
    LiveBrokerEventStreamError,
    LivePortfolio,
)
from app.engine.live.order_identity import mint_intent_id
from app.engine.live.readiness import build_live_readiness
from app.engine.live.readiness_sidecar import write_readiness
from app.engine.live.runtime_producer import (
    build_bar_loop_block,
    build_broker_block,
    build_command_loop_block,
    build_control_plane_block_from_lease,
)
from app.engine.strategy.base import LoggedTrade, Strategy
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


_ENGINE_TZ = ZoneInfo("America/New_York")


def broker_callbacks_wal_path_from_output(output_dir: Path | None) -> Path | None:
    return default_broker_callbacks_wal_path(output_dir) if output_dir is not None else None


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

# Phase 5C / VCR-0002 — managed cancel/flatten paths await per-order cancel
# confirms with this timeout. On timeout the engine writes ``halt.flag``,
# sets durable ``desired_state = PAUSED``, and refuses to liquidate (PRD §5C).
# The emergency-flatten force path may proceed past the timeout but emits
# an ``EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS`` audit row.
CANCEL_CONFIRM_TIMEOUT_S = 5.0


class MaxOrdersPerDayExceeded(RuntimeError):
    """Raised when the per-day order cap (§ 9) is exceeded mid-session.

    The cap exists so a buggy strategy or flapping connection can't
    drain the account through repeated submissions in a single day.
    Default cap for the SPY 15-min run is 4 (1 entry + 1 exit + 1
    retry + 1 force-flat). Crossing the cap halts the run; resuming
    requires investigation and a new ``run_id``.
    """


class CancelConfirmTimeoutHaltError(RuntimeError):
    """Phase 5C / VCR-0002 — managed cancel-then-liquidate path stalled.

    Raised by ``LiveEngine._flatten`` when ``broker.cancel_open_orders``
    does not return within ``cancel_confirm_timeout_s``. PRD §5C
    "Cancel-confirm timeout": the engine writes ``halt.flag`` carrying
    ``CANCEL_CONFIRM_TIMEOUT_HALT`` + durable PAUSED and REFUSES to
    liquidate — submitting market liquidations against a broker we
    can't confirm cancel state with would race the just-issued cancels.

    The emergency-flatten force path may proceed past this timeout
    (operator-confirmed last-resort behavior); it emits the distinct
    ``EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS`` audit row instead of
    raising.
    """

    def __init__(self, *, timeout_s: float) -> None:
        super().__init__(
            f"CancelConfirmTimeoutHaltError(timeout_s={timeout_s}): "
            "broker.cancel_open_orders did not return within the cancel-"
            "confirm window; refused to liquidate to avoid racing "
            "just-issued cancels (PRD §5C)"
        )
        self.timeout_s = timeout_s


class ReconnectAccountMismatchHaltError(RuntimeError):
    """Phase 3 reconnect re-validation / VCR-0006.

    Raised when an IBKR reconnect lands on a different account from the
    ledger's ``account_id``. PRD §3 "Re-check on every reconnect": the
    engine writes ``halt.flag`` before raising; the bar loop exits, no
    new orders submit. Distinct from
    ``BrokerSafetyVerdictTransitionHaltError`` per the PRD's "Distinct
    event class" note — account-identity drift is a different failure
    mode from safety-verdict degradation, and the cockpit failure list
    surfaces them separately.
    """

    def __init__(
        self,
        *,
        ledger_account_id: str,
        connected_account: str,
        connection_epoch: int,
    ) -> None:
        super().__init__(
            f"ReconnectAccountMismatchHaltError("
            f"ledger_account_id={ledger_account_id!r} "
            f"connected_account={connected_account!r} "
            f"connection_epoch={connection_epoch}): "
            "broker reconnected to a different account from the run ledger — "
            "PRD §3 reconnect re-validation halt"
        )
        self.ledger_account_id = ledger_account_id
        self.connected_account = connected_account
        self.connection_epoch = connection_epoch


class BrokerSafetyVerdictTransitionHaltError(ControlledLiveHaltError):
    """Phase 7B / VCR-0010 — broker safety verdict left ``paper-only``.

    Raised by ``LiveEngine._check_verdict_transition_halt`` at the top of
    each bar iteration when the injected ``verdict_provider`` returns a
    verdict other than ``paper-only``. PRD §7B "Mid-session transition":
    the run halts BEFORE any submission, ``halt.flag`` is written to the
    output dir, and pending orders are cleared from the portfolio. The
    operator must reconcile the verdict (e.g. confirm the right port,
    re-enable readonly) and then resume — Phase 5D Resume guard #3
    (PR #535) already refuses ``cmd_resume`` when the WAL carries an
    orphaned uncertain ack.

    The matching durable ``BROKER_SAFETY_VERDICT_TRANSITION_HALT`` WAL
    event is gated on the broker-lifecycle WAL-location decision and is
    omitted here; ``halt.flag`` plus the exception carry the same
    forensic payload for the failure list / runbook.
    """

    def __init__(self, *, verdict: str) -> None:
        super().__init__(
            f"BrokerSafetyVerdictTransitionHaltError(verdict={verdict!r}): "
            "mid-session transition out of paper-only — run halted before "
            "next submit per PRD §7B"
        )
        self.verdict = verdict


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


def _emit_drop_events_for_pending(
    portfolio: LivePortfolio,
    drop_reason: str,
    ts_ms: int,
) -> None:
    """Compatibility wrapper for the portfolio-owned append-and-clear boundary."""
    portfolio.drop_pending_before_submit(
        drop_reason=drop_reason,  # type: ignore[arg-type]
        ts_ms=ts_ms,
    )


# ---------------------------------------------------------------------------
# Watchdog flatten helper — extracted for testability
# ---------------------------------------------------------------------------


class _WatchdogEngineRef(Protocol):
    """Minimal engine surface the watchdog adapter's flatten_now needs."""

    _flatten_now_requested: bool

    def _has_open_positions(self) -> bool: ...


async def _watchdog_flatten_now(engine_ref: _WatchdogEngineRef) -> str:
    """Implement flatten_now for the watchdog's _ControllerAdapter.

    Sets the engine's ``_flatten_now_requested`` flag and polls (100ms
    cadence) until the bar loop clears it.  The executor wraps this
    coroutine in ``asyncio.wait_for``; when the timeout expires the
    executor cancels the coroutine and records ``"timed_out"`` itself.

    Returns a ``FlattenOutcome`` literal:
    - ``not_needed``  — portfolio has no open positions.
    - ``completed``   — bar loop cleared the flag AND positions are zero.
    - ``failed``      — flag cleared but positions remain (partial fill).
    """
    if not engine_ref._has_open_positions():
        logger.info("[WATCHDOG] flatten_now: no open positions — not_needed")
        return "not_needed"

    engine_ref._flatten_now_requested = True
    logger.info("[WATCHDOG] flatten_now: set _flatten_now_requested; polling for bar-loop confirmation (100ms cadence)")

    while engine_ref._flatten_now_requested:
        await asyncio.sleep(0.1)

    if engine_ref._has_open_positions():
        logger.critical(
            "[WATCHDOG] flatten_now: bar loop cleared flag but positions remain — possible partial fill or rejection"
        )
        return "failed"

    logger.info("[WATCHDOG] flatten_now: positions confirmed zero — completed")
    return "completed"


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
        # NEW: order-idempotency sidecar (PRD-A § 16.4 Resolution 3).
        # Callable provided by run.py knows how to build the envelope
        # from current state; engine just invokes it after each flush.
        live_state_writer: Callable[[LivePortfolio, int], None] | None = None,
        # NEW: operator command channel (PRD-A § 16.4 Resolution 7 / PR-D).
        # Bot polls at 1s independent of the bar loop and dispatches
        # PAUSE/RESUME/STOP/FLATTEN/RECONCILE/MARK_POISONED to
        # in-process state. None disables the polling.
        command_channel: CommandChannel | None = None,
        # NEW: durable operator desired-state (PRD-A §16.4 Resolution 7 /
        # PR-D). ``start_paused`` boots the engine paused when the prior
        # desired_state.json says PAUSED (survives crash/reboot);
        # ``desired_state_writer`` persists intent when the command
        # channel dispatches PAUSE/RESUME/STOP so it outlives this run.
        start_paused: bool = False,
        desired_state_writer: Callable[[DesiredState, str], None] | None = None,
        # NEW: decision-row provenance (PRD-A §16.1 Resolution 5). Threaded
        # from run.py off the ledger + resolved spec. ``decision_columns``
        # is the resolved parquet schema (core + spec.decision_columns);
        # defaults to the SPY EMA schema for the no-spec replay path.
        run_id: str = "",
        strategy_key: str = "",
        strategy_instance_id: str = "",
        run_mode: str = "live_paper",
        bar_source: str = "ibkr_paper_delayed",
        decision_columns: tuple[str, ...] = DECISION_COLUMNS,
        owned_perm_ids: set[int] | None = None,
        # ADR 0009 § 6 — propagated registered sizing surface. ``None`` ⇒
        # legacy / unregistered (no fail-fast applies).
        sizing_surface: str | None = None,
        # Phase 5E / VCR-0012 — when set, ``_convert_ibkr_fill`` falls back
        # to a fold of this WAL to reconstruct a fill's ``_OrderMeta`` when
        # the in-memory dict has no entry for the fill's ``order_id``. The
        # cross-restart case: prior session minted the intent + landed the
        # broker order; this session boots into a fresh ``_order_meta`` but
        # the broker still echoes the prior session's ``perm_id`` on fills.
        # ``None`` keeps the prior pre-Phase-5E behavior (warn + drop).
        intent_wal_path: Path | None = None,
        # ADR 0014 / issue #684 PR3 — host-runner-owned raw callback WAL.
        # Defaults to ``<output_dir>/broker_callbacks.jsonl`` when a run_dir is
        # present. Tests may pass an explicit path; callers with no output_dir
        # stay artifact-free.
        broker_callbacks_wal_path: Path | None = None,
        # Phase 7B / VCR-0010 — mid-session broker safety verdict observer.
        # Called at the top of every bar iteration (BEFORE pending submits).
        # When the provider returns a non-``paper-only`` and non-``None``
        # verdict — i.e. a positive ``unsafe`` / ``unknown`` signal — the
        # bar loop writes ``halt.flag`` to ``output_dir`` and raises
        # ``BrokerSafetyVerdictTransitionHaltError`` so the run halts
        # before any submission, matching the PRD §7B "Mid-session
        # transition" contract. ``None`` (the default) keeps the
        # pre-Phase-7B behavior (no observer).
        verdict_provider: object = None,
        # PRD #619-B B3 — engine_runtime.json producer side. When wired,
        # the engine updates the aggregator on every bar iteration,
        # command-poll tick, and verdict check. The publisher that
        # consumes the aggregator is owned by the caller (cmd_start);
        # the engine only writes into the aggregator. ``None`` keeps
        # replay tests and synthetic-engine callers free of the wire.
        runtime_aggregator: object = None,
        # PRD #619-B B3 — for the control-plane block bootstrap. When
        # set, the engine reads ``<artifacts_root>/control_plane/daemon_lease.json``
        # at startup to seed ``ControlPlaneBlock.observed_daemon_boot_id``.
        # When ``watchdog_factory`` is also wired (B5 follow-up below)
        # this single read becomes a periodic producer.
        artifacts_root_for_lease: object = None,
        # PRD #619-B B5 follow-up — child watchdog factory. When set,
        # the engine constructs a ``ChildWatchdog`` from this callable
        # before the bar loop starts and stops it in the outer
        # ``finally``. The factory receives the four engine-side
        # callbacks (block_submissions, persist_paused, disconnect_broker,
        # request_engine_exit) and returns a configured watchdog. The
        # engine owns the lifecycle; the factory owns the configuration
        # (cadence, threshold, expected_daemon_boot_id from env).
        watchdog_factory: object = None,
        # AccountOwner submit mode. When wired, the portfolio emits
        # AccountOwnerSubmitIntent objects to this callable instead of calling
        # the broker adapter directly.
        account_owner_submitter: object = None,
        account_registry_gate_enabled: bool = True,
        owner_generation_provider: object = None,
        trace_id_provider: object = None,
        # Phase 5C / VCR-0002 — managed cancel/flatten paths await per-order
        # cancel confirms with this timeout. Tests pass a short value
        # (e.g. 0.05s) to exercise the timeout path without waiting 5s.
        cancel_confirm_timeout_s: float = CANCEL_CONFIRM_TIMEOUT_S,
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
        # PR 3 / operator-notice — canonical engine-start timestamp used as the
        # legacy_sizing_only_cutoff_ms fold boundary. Falls back to wall-clock
        # when the caller doesn't supply session_start_ms (live tests, replays).
        self._engine_started_at_ms: int = (
            session_start_ms if session_start_ms is not None else time.time_ns() // 1_000_000
        )
        self._code_sha = code_sha
        self._strategy_spec_sha = strategy_spec_sha
        self._live_state_writer = live_state_writer
        self._command_channel = command_channel
        self._desired_state_writer = desired_state_writer
        # PAUSE drops new orders the strategy queues each bar; RESUME
        # restores normal submission. STOP / MARK_POISONED set the
        # bar loop's shutdown_event so the existing graceful path runs.
        # ``start_paused`` seeds this from durable desired-state so a
        # bot that was PAUSED before a crash resumes paused.
        self._paused = start_paused
        # VCR-0007 / Phase 6A — FLATTEN_NOW is a pure one-shot. The command
        # dispatcher sets this flag; the bar loop polls it between bars and
        # invokes ``_flatten`` without terminating the process. The flag is
        # cleared after the flatten lands so a subsequent enqueue rearms it.
        self._flatten_now_requested = False
        # Reconciliation PR 2 / runtime RECONCILE wiring.
        # ``_submit_lock`` serialises the engine's submit critical sections
        # (bar-loop submit, _flatten liquidations) with the async reconcile
        # task. The reconcile task must wait for any in-flight submit to
        # land before probing the broker; otherwise the snapshot is racy.
        # ``_inhibit_submits`` is set the moment RECONCILE is acked so new
        # submits skip without placing orders, even if the bar loop reaches
        # the submit site while the reconcile task is still waiting on the
        # lock. ``_reconcile_task`` tracks the running task so a concurrent
        # RECONCILE returns ``already_running`` instead of starting a second.
        self._submit_lock = asyncio.Lock()
        self._inhibit_submits = False
        self._reconcile_task: asyncio.Task | None = None
        # VCR-0018-F / Phase 6C — engine-level force-flat enforcement.
        # Mirrors the local ``last_force_flat_date == minute_bar.time.date()``
        # check, surfaced as instance state so ``_submit_pending_with_meta``
        # can drop strategy-emitted orders at the engine boundary instead
        # of relying on the strategy to remember to suppress.
        self._force_flat_active = False
        # Decision-row provenance.
        self._run_id = run_id
        self._strategy_key = strategy_key
        self._strategy_instance_id = strategy_instance_id
        self._run_mode = run_mode
        self._bar_source = bar_source
        self._decision_columns = decision_columns
        self._owned_perm_ids = owned_perm_ids or set()
        # Phase 5E / VCR-0012 — fold path for the cross-restart fill classifier
        # (see ``_resolve_meta_via_intent_wal``).
        self._intent_wal_path = intent_wal_path
        callback_wal_path = (
            broker_callbacks_wal_path
            if broker_callbacks_wal_path is not None
            else broker_callbacks_wal_path_from_output(output_dir)
        )
        self._broker_callbacks_wal = BrokerCallbackWal(callback_wal_path) if callback_wal_path is not None else None
        self._broker_callbacks_wal_attached_to_stream = False
        if self._broker_callbacks_wal is not None and isinstance(self._broker, IbkrBrokerAdapter):
            self._broker.set_broker_callback_sink(self._append_raw_broker_callback)
            self._broker_callbacks_wal_attached_to_stream = True
        # Phase 7B / VCR-0010 — broker safety verdict observer.
        self._verdict_provider = verdict_provider
        # PRD #619-B B3 — engine_runtime aggregator. Producer hooks below
        # update it on bar / command-poll / verdict ticks. The publisher
        # is owned by the caller (cmd_start); a ``None`` aggregator
        # disables every producer call site.
        self._runtime_aggregator = runtime_aggregator
        self._artifacts_root_for_lease = artifacts_root_for_lease
        self._watchdog_factory = watchdog_factory
        self._account_owner_submitter = account_owner_submitter
        self._account_registry_gate_enabled = account_registry_gate_enabled
        self._owner_generation_provider = owner_generation_provider
        self._trace_id_provider = trace_id_provider
        # PRD #619-B B5 follow-up — submissions-blocked flag set by the
        # watchdog's step 1 ``block_submissions`` callback. The bar loop
        # checks this alongside ``self._paused`` and clears any pending
        # orders without sending them when set. The flag is sticky: once
        # the watchdog has decided to fail-close, the engine never
        # un-blocks itself (the operator restart starts a fresh process).
        self._submissions_blocked = False
        # Holds the watchdog instance for the duration of ``run()``; set
        # in the startup block, awaited in the outer finally.
        self._watchdog: object | None = None
        # Weak reference to the LivePortfolio created in ``run()``.  Set
        # just before the child watchdog starts so the watchdog adapter
        # can read live position state.  Cleared in the finally block.
        self._run_portfolio: object | None = None
        # Phase 5C / VCR-0002 — managed cancel-confirm timeout.
        self._cancel_confirm_timeout_s = cancel_confirm_timeout_s
        # Phase 3 / VCR-0006 — reconnect re-validation. Each bar iteration
        # snapshots IbkrClient.connectivity_lost_count; on increment + restored
        # connection the engine re-runs account_identity.verify_account_match.
        self._last_connectivity_lost_count: int = 0
        self._connection_epoch: int = 0
        self._sizing_surface = sizing_surface

        # Phase 5C / VCR-0002 — durable submit activation. When the operator
        # has flipped ``LiveConfig.durable_submit_enabled = True`` AND a
        # real IBKR client is wired, instantiate the verified
        # ``IbkrBrokerOwnershipQuery`` subclass and run the activation
        # contract (Gate #1 verified order_ref cap + Gate #2
        # subclass-allowlist). The contract raises
        # ``DurableSubmitNotActivatable`` on failure, which propagates out
        # of construction so the runner refuses to start. Default is
        # ``False`` so this is purely additive — the operator opts in
        # after the paper-side validation receipt lands.
        if self._config.durable_submit_enabled and self._client is not None:
            from app.engine.live.broker_ownership_query import (
                require_durable_submit_activation,
            )
            from app.engine.live.ibkr_broker_ownership_query import (
                VERIFIED_ORDER_REF_CAP,
                IbkrBrokerOwnershipQuery,
            )

            require_durable_submit_activation(
                enabled=True,
                verified_order_ref_cap=VERIFIED_ORDER_REF_CAP,
                ownership_query=IbkrBrokerOwnershipQuery(self._client),
            )
        # The strategy-specific decision columns = resolved minus the core.
        self._strategy_decision_columns = tuple(c for c in decision_columns if c not in CORE_DECISION_COLUMNS)

    @property
    def engine_started_at_ms(self) -> int:
        """PR 3 / operator-notice — engine-start wall-clock as int64 ms UTC.

        Used as the legacy_sizing_only_cutoff_ms fold boundary: SIZING_RESOLVED
        events before this timestamp are pre-engine-start orphans that can never
        have a following terminal event; the publisher uses them to emit
        activity.dropped_paused_intent notices without triggering for normal
        intents that simply haven't resolved yet.
        """
        return self._engine_started_at_ms

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

        # VCR-0006 / Phase 3 — strict ledger↔broker account identity. The
        # ledger hashes ``account_id`` into ``run_id``, so a deploy-time
        # identity that disagrees with the runtime-bound account would
        # silently corrupt every downstream attestation (executions row,
        # provenance card, reconciliation). Refuse to start; the operator's
        # next step is to fix IBKR_HOST/client_id or redeploy with the
        # correct account.
        from app.engine.live.account_identity import verify_account_match

        verify_account_match(
            ledger_account_id=self._account_id,
            connected_account=account_id,
        )

    def _write_session_metadata_on_start(self) -> None:
        """VCR-0006 / Phase 3 — persist the verified identity pair to
        ``session_metadata.json`` so a later audit can reconstruct who the
        run was actually placing orders for.

        Best-effort: ``_validate_paper_client`` has already enforced the
        match, so a failure to write the sidecar is a forensic gap but
        not a runtime hazard. Log loudly and continue.
        """
        if self._client is None or self._output_dir is None:
            return
        connected_account = self._client.connected_account
        if not connected_account or not self._account_id:
            return
        from app.engine.live.session_metadata import (
            SESSION_METADATA_SCHEMA_VERSION,
            SessionMetadata,
            read_session_metadata,
            write_session_metadata,
        )

        existing = read_session_metadata(self._output_dir)
        connection_epoch = (existing.connection_epoch + 1) if existing else 1
        metadata = SessionMetadata(
            schema_version=SESSION_METADATA_SCHEMA_VERSION,
            ledger_account_id=self._account_id,
            connected_account=connected_account,
            session_started_ms=self._session_start_ms,
            session_ended_ms=None,
            connection_epoch=connection_epoch,
        )
        try:
            write_session_metadata(self._output_dir, metadata)
        except OSError:
            logger.exception(
                "session_metadata.json write failed; forensic record degraded "
                "but engine will continue (identity check already passed)"
            )

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
        loop runs ``_flatten`` (cancel open broker orders +
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
        self._write_session_metadata_on_start()
        # ADR 0009 — when the resolved live_config carries a sizing policy,
        # construct the policy-application adapter and attach it to the
        # portfolio. ``set_holdings`` then routes through the adapter; legacy
        # callers (no policy in live_config) keep the prior SimpleFloorSizing
        # path. PR2 wires the SetHoldings percent path through
        # LeanSetHoldingsSizing via a callable portfolio-value provider —
        # the seam where the future capital-sleeve layer will drop in.
        # Phase 5A wiring (VCR-0002) — when a real broker requires
        # durable submit (IbkrBrokerAdapter), pass the run's IntentWal
        # and bot_order_namespace so set_holdings mints intent_id and
        # submit_pending_orders stamps order_ref on each broker call.
        # Without this, LivePortfolio.__post_init__ fail-fasts because
        # Phase 5B (ADR 0008) requires the intent-identity foundation.
        intent_wal_for_portfolio = None
        bot_order_namespace_for_portfolio = ""
        if self._intent_wal_path is not None and self._strategy_instance_id:
            from app.engine.live.account_registry import bot_order_namespace_for_instance
            from app.engine.live.intent_wal import IntentWal as _IntentWal

            bot_order_namespace_for_portfolio = bot_order_namespace_for_instance(self._strategy_instance_id)
            if self._account_owner_submitter is None:
                intent_wal_for_portfolio = _IntentWal(self._intent_wal_path)
        account_freeze_provider = None
        if self._artifacts_root is not None and self._account_id:
            from app.engine.live.account_artifacts import read_account_freeze

            def account_freeze_provider():
                return read_account_freeze(
                    self._artifacts_root,
                    self._account_id,
                )

        account_truth_gate_provider = None
        if self._account_id and getattr(self._broker, "requires_durable_submit", False):
            from app.services.account_truth_snapshot import (
                account_truth_gate_result,
                get_account_truth_snapshot_provider,
            )
            from app.utils.timestamps import now_ms_utc

            def account_truth_gate_provider():
                snapshot = get_account_truth_snapshot_provider().get(self._account_id)
                return account_truth_gate_result(snapshot, now_ms=now_ms_utc())

        portfolio = LivePortfolio(
            self._broker,
            intent_wal=intent_wal_for_portfolio,
            bot_order_namespace=bot_order_namespace_for_portfolio,
            account_freeze_provider=account_freeze_provider,
            account_registry_gate_provider=(
                self._account_registry_gate_result if self._account_registry_gate_enabled else None
            ),
            account_truth_gate_provider=account_truth_gate_provider,
            account_owner_submitter=self._account_owner_submitter,
            account_id=self._account_id,
            strategy_instance_id=self._strategy_instance_id,
            run_id=self._run_id,
            owner_generation_provider=self._owner_generation_provider,
            trace_id_provider=self._trace_id_provider,
        )
        if self._config.sizing is not None:
            portfolio.order_sizer = OrderSizer(
                self._config.sizing,
                portfolio_value_provider=WholeAccountPortfolioValueProvider(portfolio.total_value),
            )
        portfolio.registered_sizing_surface = self._sizing_surface
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
            writers = LiveArtifactWriters.open(self._output_dir, self._decision_columns)

        started_event_stream = False
        if isinstance(self._broker, IbkrEventAdapter):
            await self._broker.start_event_stream()
            started_event_stream = True

        # PRD #619-B B3 — engine_runtime aggregator startup hooks. All
        # four blocks are seeded here so the publisher's first snapshot
        # writes immediately. The earlier shape (seed only command_loop
        # + control_plane, let broker + bar_loop land on the first bar)
        # left ``engine_runtime.json`` absent through every pre-market
        # deploy, which the freshness evaluator can't distinguish from
        # a crashed engine — it always marked posture_demoted=True and
        # blocked Resume with POSTURE_DEMOTED. The session-aware
        # bar-loop evaluator already handles "no bar yet" cleanly
        # (CLOSED → NOT_APPLICABLE), so the seed just unblocks the
        # first-snapshot gate without weakening any safety signal.
        # ``_probe_and_publish_broker_block`` runs the same probe +
        # publish the ``broker_probe_task`` (spawned below) refreshes
        # every 10s; this call is its first iteration, made explicit so
        # the broker block is non-None before ``engine.run()`` proceeds.
        await self._publish_command_loop_block()
        await self._publish_initial_control_plane_block()
        await self._probe_and_publish_broker_block()
        await self._publish_initial_bar_loop_block()

        # PRD #619-B B5 follow-up — child watchdog. The factory builds
        # a configured ``ChildWatchdog`` from the engine's four side-
        # effect callbacks; the engine owns the lifecycle. When the
        # watchdog fires its 5-step contract, ``_submissions_blocked``
        # flips True (step 1), durable PAUSED + incident lands (step 2),
        # then disconnect + shutdown_event.set() drain the bar loop.
        if self._watchdog_factory is not None and self._runtime_aggregator is not None and shutdown_event is not None:
            # Expose portfolio to the watchdog adapter before the watchdog
            # starts so flatten_now can read live position state.
            self._run_portfolio = portfolio
            self._watchdog = await self._start_child_watchdog(shutdown_event)

        # Operator command-channel poll task. Spawned only when a
        # channel is configured; cancelled in the finally block. The
        # task needs a shutdown_event to honor STOP / MARK_POISONED,
        # so synthesise one if the caller didn't pass one.
        command_poll_task: asyncio.Task | None = None
        if self._command_channel is not None:
            if shutdown_event is None:
                shutdown_event = asyncio.Event()
            command_poll_task = asyncio.create_task(self._command_poll_loop(shutdown_event))

        # PRD #619-B §B — broker-probe heartbeat as its own task so its
        # 10s cadence is decoupled from the 1Hz command-poll loop and
        # from the bar-driven verdict-halt publish. Without this, the
        # broker block's ``probe_completed_at_ms`` would stagnate past
        # the 25s freshness threshold pre-market (when the bar loop
        # isn't firing) and the freshness evaluator would re-demote
        # posture seconds after a fresh start. Spawn is gated on the
        # runtime aggregator being wired so replay tests and synthetic
        # engines don't probe IBKR for no observable effect.
        broker_probe_task: asyncio.Task | None = None
        if self._runtime_aggregator is not None:
            if shutdown_event is None:
                shutdown_event = asyncio.Event()
            broker_probe_task = asyncio.create_task(
                self._broker_probe_loop(shutdown_event),
                name="broker_probe_loop",
            )

        source_iter = source.__aiter__()
        last_bar: TradeBar | None = None
        try:
            while True:
                minute_bar, shutdown_won = await _next_bar_or_shutdown(source_iter, shutdown_event)
                if minute_bar is None:
                    if shutdown_won:
                        # Graceful shutdown via SIGINT/SIGTERM. cancel_open_orders +
                        # liquidate + submit happen via _flatten; the
                        # existing finally block (post-break) flushes writers and
                        # stops the broker event stream. ``last_bar.end_time``
                        # gives the historical "use the last bar's time" behavior
                        # when bars were flowing; the wall-clock fallback covers
                        # the wedged-source case (issue surfaced 2026-05-13)
                        # where no bar ever arrived.
                        fallback_time = last_bar.end_time if last_bar is not None else datetime.now(_ENGINE_TZ)
                        ctx.log(f"[SHUTDOWN] {fallback_time}: shutdown_event set; flattening and exiting")
                        flat_acks = await self._flatten(portfolio, ctx, bar_time=fallback_time)
                        submitted_order_ids.extend(ack.order_id for ack in flat_acks)
                    # source exhausted (shutdown_won=False) OR shutdown finished —
                    # either way the bar loop is done.
                    break
                last_bar = minute_bar
                self._raise_if_event_stream_failed()
                # PRD #619-B B3 — bar-loop heartbeat. ``heartbeat_at_ms``
                # is loop scheduling (we just woke); ``latest_source_bar_ms``
                # is market-data freshness (the bar's close time).
                # Splitting them lets the backend freshness evaluator
                # tell a halted engine apart from a closed market.
                await self._publish_bar_loop_block(minute_bar)
                # Also re-emit the command-loop heartbeat on every bar
                # — bar-loop liveness implies command-loop liveness
                # without waiting for the next 1s command-poll tick.
                await self._publish_command_loop_block()
                # VCR-0007 / Phase 6A — service the FLATTEN_NOW one-shot
                # between bars BEFORE per-bar consolidation/strategy work.
                # The flatten cancels owned opens + liquidates positions
                # but does NOT terminate the engine; the bar loop keeps
                # running. The "Flatten and pause" UI endpoint writes
                # ``desired_state = PAUSED`` BEFORE enqueueing FLATTEN_NOW,
                # so the resume of the loop refuses new entries until
                # the operator explicitly resumes.
                if self._flatten_now_requested:
                    self._flatten_now_requested = False
                    ctx.log(f"[FLATTEN_NOW] {minute_bar.time}: cancelling owned opens + liquidating")
                    flat_acks = await self._flatten(portfolio, ctx, bar_time=minute_bar.time)
                    submitted_order_ids.extend(ack.order_id for ack in flat_acks)
                # Reset per-day order counter on session-date boundary.
                bar_date = minute_bar.time.date()
                if current_session_date is None or bar_date != current_session_date:
                    orders_submitted_today = 0
                    current_session_date = bar_date
                    # VCR-0018-F / Phase 6C — clear the engine-level
                    # force-flat flag on session-date boundary so a new
                    # session can trade normally until the next force-flat
                    # barrier fires.
                    self._force_flat_active = False
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
                self._append_raw_broker_callbacks(raw_real_events)
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
                    # VCR-0018-F / Phase 6C — engine-level enforcement.
                    # Once force-flat fires this session, any further order
                    # the strategy emits is dropped at the engine boundary.
                    # The flag mirrors ``last_force_flat_date`` so the
                    # session-date boundary reset clears it.
                    self._force_flat_active = True
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
                ctx.current_time = minute_bar.end_time
                strategy.on_minute_bar(minute_bar)

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

                # Phase 3 / VCR-0006 — reconnect re-validation runs BEFORE
                # the verdict observer so an account-mismatch halt surfaces
                # ahead of a safety-verdict halt (PRD §3 "Distinct event
                # class" note).
                self._check_reconnect_revalidation(portfolio)
                # Phase 7B / VCR-0010 — mid-session broker safety verdict
                # gate (PRD §7B "Mid-session transition"). Called BEFORE
                # the PAUSE drop and BEFORE submission so a verdict that
                # transitions out of paper-only halts the run proactively
                # (rather than catching the next submit's exception). The
                # halt.flag write + raise mirrors PRD §5D's SUBMIT_UNCERTAIN
                # halt semantics. PRD #619-B B3 — the check is async so it
                # can update the runtime aggregator's broker block on the
                # same path that writes verdict_snapshot.json.
                await self._check_verdict_transition_halt(portfolio)

                # PAUSE drops new orders this bar before submit. The
                # strategy still consumes the bar (indicators advance),
                # but nothing new reaches the broker. RESUME flips the
                # flag and the next bar's queue gets submitted normally.
                if self._paused or self._submissions_blocked:
                    # PR 3 / operator-notice — emit before clear so the
                    # append → fsync → clear ordering is satisfied.
                    _drop_reason = "control_plane_lease_lost" if self._submissions_blocked else "operator_paused"
                    _emit_drop_events_for_pending(
                        portfolio,
                        drop_reason=_drop_reason,
                        ts_ms=int(minute_bar.end_time.timestamp() * 1000),
                    )
                # Predictive cap check: refuse to submit the pending batch if
                # it would push the day's total past ``max_orders_per_day``,
                # rather than submitting first and raising afterwards.
                # The post-submission variant of this check raced with IBKR
                # fill delivery: the engine sent the cap-tripping order,
                # raised on the next line, started shutdown, and IBKR
                # delivered the fill milliseconds later — the engine was
                # already disconnected, so the fill never reached
                # ``executions.parquet`` and the position became orphaned
                # at the broker (fleet check then flagged "unrecognized
                # positions"). Predicting before the wire guarantees the
                # broker never sees an order the engine won't reconcile.
                pending_count = len(portfolio.pending_orders)
                if (
                    self._max_orders_per_day is not None
                    and pending_count > 0
                    and orders_submitted_today + pending_count > self._max_orders_per_day
                ):
                    # PR 3 / operator-notice — emit before clear so the
                    # append → fsync → clear ordering is satisfied.
                    _emit_drop_events_for_pending(
                        portfolio,
                        drop_reason="max_orders_per_day",
                        ts_ms=int(minute_bar.end_time.timestamp() * 1000),
                    )
                    raise MaxOrdersPerDayExceeded(
                        f"would push total to {orders_submitted_today + pending_count} on "
                        f"{current_session_date} (cap={self._max_orders_per_day}); "
                        f"dropped {pending_count} pending order(s) without submission"
                    )
                submitted = await self._submit_pending_with_meta(portfolio)
                submitted_order_ids.extend(ack.order_id for ack in submitted)
                orders_submitted_today += len(submitted)

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

                # Live-state sidecar: flush buffered artifacts, then persist
                # cursors and position snapshot once per bar so a crash between
                # bars leaves enough context for ColdStartReconciler to safely
                # resume — and so the recorded cursor never runs ahead of the
                # durable parquet rows. Cheap no-op when no writer was
                # configured.
                self._persist_live_state(portfolio, int(minute_bar.end_time.timestamp() * 1000), writers)

                # Engine-authored readiness (ADR 0005): emit the consolidated
                # "can act on the next bar?" vector from the in-loop guard values
                # each bar. The backend status endpoint transports it verbatim —
                # the operator console shows exactly what the engine enforces.
                self._emit_readiness(
                    as_of_ms=int(minute_bar.end_time.timestamp() * 1000),
                    orders_used=orders_submitted_today,
                    in_session=True,
                    force_flat_active=(last_force_flat_date == minute_bar.time.date()),
                )

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
                self._append_raw_broker_callbacks(final_raw_events)
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
            # PRD #619-B B5 follow-up — stop the child watchdog FIRST so
            # its periodic lease-reading task is settled before we touch
            # the broker session or the command poll. The watchdog's own
            # ``stop()`` is bounded; if it has already fired its 5-step
            # contract (state == EXITED) the call is a no-op.
            if self._watchdog is not None:
                try:
                    await self._watchdog.stop()
                except Exception:
                    logger.exception("child watchdog stop failed")
                self._watchdog = None
            self._run_portfolio = None
            # Stop the command-channel poller before any other cleanup
            # so a late-arriving operator command doesn't race with
            # shutdown writes.
            if command_poll_task is not None:
                command_poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await command_poll_task
            if broker_probe_task is not None:
                broker_probe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await broker_probe_task
            # Reconciliation PR 2 — drain any in-flight runtime reconcile
            # task so we don't tear down the engine while the orchestrator
            # is mid-receipt-write. The task is bounded by the broker
            # probe + classifier + a single ack file write; cancellation is
            # the fallback if a poison verdict already triggered shutdown.
            if self._reconcile_task is not None and not self._reconcile_task.done():
                self._reconcile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reconcile_task
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
                # Live-state sidecar final checkpoint — same shape as the
                # mid-run per-bar writes; helper flushes artifacts first so the
                # cursor stays consistent with on-disk rows, and handles the
                # no-op case.
                self._persist_live_state(portfolio, int(last_bar.end_time.timestamp() * 1000), writers)
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

    def _maybe_write_decision(
        self,
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

        Core provenance columns (run_id / strategy ids / mode /
        bar_source) come from the run context threaded in at
        construction; the strategy-specific indicator columns are read
        off the snapshot by the names ``spec.decision_columns`` declared
        (resolved into ``self._strategy_decision_columns``). A snapshot
        missing a declared column fails fast at the writer's column
        check rather than writing a silently-wrong schema.
        """
        snap = strategy.last_decision_snapshot
        if snap is None or snap.bar_close_ms == last_written_ms:
            return last_written_ms
        indicator_values = {name: getattr(snap, name) for name in self._strategy_decision_columns}
        writers.decisions.append_row(
            DecisionRow(
                bar_close_ms=snap.bar_close_ms,
                signal=snap.signal,
                intended_price=snap.intended_price,
                run_id=self._run_id,
                strategy_key=self._strategy_key,
                strategy_instance_id=self._strategy_instance_id,
                bar_source=self._bar_source,
                # The consolidated bar close == intended_price (share-count
                # reference); the rest of OHLCV is not on DecisionSnapshot
                # yet, so it stays NULL until the snapshot grows (Layer B).
                bar_close=snap.intended_price,
                intended_fill_model="NEXT_BAR_OPEN",
                mode=self._run_mode,
                indicator_values=indicator_values,
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
                # Shadow fills carry the simulator's ``shadow:``-prefixed ids;
                # preserve them so the broker-noncolliding invariant survives
                # into the artifact. Real fills (ids None) keep the synthesised
                # ``engine-``/``live-`` fallbacks.
                exec_id=event.exec_id if event.exec_id is not None else f"engine-{event.order_id}",
                perm_id=int(event.order_id),
                client_order_id=(
                    event.client_order_id if event.client_order_id is not None else f"live-{event.order_id}"
                ),
                account_id=self._account_id,
                symbol=event.symbol,
                fill_quantity=int(event.fill_quantity),
                fill_price=float(event.fill_price),
                # Record the broker's reported commission; NaN when not yet
                # reported, so a missing fee stays distinguishable from a
                # genuine zero downstream and is never fabricated (PRD-B).
                fee=float(event.recorded_fee) if event.recorded_fee is not None else float("nan"),
                # Provenance flows from the adapter's event: real fills are
                # ``broker_fill`` (default); the NoSubmitBrokerAdapter stamps
                # ``shadow_sim`` + source bar so the receipt is unambiguous.
                execution_source=event.execution_source,
                fill_model=event.fill_model,
                source_bar_close_ms=event.source_bar_close_ms,
                # VCR-P3-L — broker-reported execution time. Distinct from
                # ``ts_ms`` (engine wall-clock at receipt); reconciliation
                # joins and latency analysis prefer this when populated.
                exec_time_ms=event.exec_time_ms,
            )
        )

    def _emit_readiness(
        self,
        *,
        as_of_ms: int,
        orders_used: int,
        in_session: bool,
        force_flat_active: bool,
    ) -> None:
        """Write the engine-authored readiness vector sidecar (ADR 0005).

        Best-effort: a sidecar I/O hiccup must not break the bar loop.
        ``broker_connected`` is True mid-loop — a stream failure raises and
        exits before this runs, so a dead broker shows up as a stale sidecar,
        not a false "connected".
        """
        if self._output_dir is None:
            return
        poisoned = (self._output_dir / "poisoned.flag").exists()
        account_registry_gate = self._account_registry_gate_result()
        vector = build_live_readiness(
            as_of_ms=as_of_ms,
            paused=self._paused,
            broker_connected=True,
            submit_mode="readonly" if self._readonly else "live_paper",
            orders_used=orders_used,
            orders_cap=self._max_orders_per_day,
            in_session=in_session,
            force_flat_active=force_flat_active,
            poisoned=poisoned,
            bar_source=self._bar_source,
            expected_bar_source=self._bar_source,
            account_registry_gate_result=(
                account_registry_gate.model_dump(mode="json") if account_registry_gate is not None else None
            ),
        )
        try:
            write_readiness(self._output_dir, vector)
        except OSError:
            logger.warning("readiness sidecar write failed", exc_info=True)

    def _account_registry_gate_result(self):
        if (
            not self._account_registry_gate_enabled
            or self._artifacts_root is None
            or not self._account_id
            or not self._run_id
            or not self._strategy_instance_id
        ):
            return None
        from app.engine.live.account_registry import (
            bot_order_namespace_for_instance,
            evaluate_account_instance_binding,
        )

        return evaluate_account_instance_binding(
            self._artifacts_root,
            account_id=self._account_id,
            strategy_instance_id=self._strategy_instance_id,
            run_id=self._run_id,
            bot_order_namespace=bot_order_namespace_for_instance(self._strategy_instance_id),
        )

    def _persist_live_state(
        self,
        portfolio: LivePortfolio,
        bar_close_ms: int,
        writers: LiveArtifactWriters | None,
    ) -> None:
        """Flush artifacts, then invoke the optional live-state-sidecar writer.

        Ordering matters: the sidecar envelope records
        ``last_processed_bar_ms`` / ``last_artifact_flush_ms`` as
        durable cursors. The decision / execution / trade parquet rows,
        however, are only buffered in memory until ``writers.flush_all``
        (or ``close_all`` in the run finally). If we wrote the sidecar
        cursor first and the process crashed before the buffered rows
        reached disk, ColdStartReconciler would resume from a cursor
        that is *ahead* of the actual artifacts — claiming a bar/flush
        is durable when its rows were lost. So we flush the artifact
        bundle to disk before advancing the cursor; the cursor then
        only ever reflects truly durable artifacts.

        A flush failure is logged and suppresses the sidecar write for
        this bar (the cursor must not advance past artifacts we failed
        to persist), but never propagates — the sidecar is a
        crash-recovery aid, not a precondition for processing. Likewise
        the sidecar write itself is logged-and-swallowed; the
        ColdStartReconciler at next boot can detect any divergence
        between sidecar and broker if writes were missed.
        """
        if self._live_state_writer is None or bar_close_ms <= 0:
            return
        if writers is not None:
            try:
                writers.flush_all()
            except Exception:
                logger.exception(
                    "artifact flush before live-state sidecar write failed; skipping cursor advance this bar"
                )
                return
        try:
            self._live_state_writer(portfolio, bar_close_ms)
        except Exception:
            logger.exception("live-state sidecar write failed; continuing")

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

        Reconciliation PR 2: the broker call(s) happen under
        ``self._submit_lock`` and skip entirely when
        ``self._inhibit_submits`` is set (a RECONCILE has been acked and
        the reconcile task either holds the lock or is about to). The
        skip path drains pending orders to keep portfolio state honest
        and logs the suppression so the operator can correlate the
        decision row with the reconcile receipt.
        """
        async with self._submit_lock:
            return await self._submit_pending_with_meta_locked(portfolio)

    async def _submit_pending_with_meta_locked(self, portfolio: LivePortfolio):
        """Internal submit path that assumes ``self._submit_lock`` is held.

        Split from ``_submit_pending_with_meta`` so callers that already
        hold the lock (``_flatten``'s cancel→liquidate→submit critical
        section) don't re-enter ``asyncio.Lock`` — which is non-reentrant
        and would deadlock the bar loop.
        """
        pending_snapshot = list(portfolio.pending_orders)
        if self._readonly:
            portfolio.pending_orders.clear()
            return []
        # VCR-0018-F / Phase 6C — engine-level force-flat enforcement.
        # ``self._force_flat_active`` is set when the force-flat barrier
        # fires for this session date; until the next session, any order
        # the strategy emits is dropped at the engine boundary with a
        # structured log event. A strategy that "forgets" to suppress
        # cannot get an order through; the per-strategy suppression code
        # remains as defense-in-depth.
        if self._force_flat_active and pending_snapshot:
            for order in pending_snapshot:
                logger.warning(
                    "[FORCE_FLAT_DROP] strategy=%s symbol=%s qty=%s tag=%s — order "
                    "dropped at engine boundary because force-flat is active for "
                    "this session",
                    self._strategy_key,
                    order.symbol,
                    order.quantity,
                    order.tag,
                )
            portfolio.pending_orders.clear()
            return []
        if self._inhibit_submits and pending_snapshot:
            for order in pending_snapshot:
                logger.warning(
                    "[RECONCILE_INHIBIT] strategy=%s symbol=%s qty=%s tag=%s — "
                    "order dropped at engine boundary because a runtime "
                    "RECONCILE is in progress",
                    self._strategy_key,
                    order.symbol,
                    order.quantity,
                    order.tag,
                )
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

    # ──────────────────── Operator command channel ───────────────────

    async def _command_poll_loop(self, shutdown_event: asyncio.Event) -> None:
        """1s poll task that dispatches operator commands.

        Sleeps in 1s ticks until shutdown_event is set or the task is
        cancelled. On each tick, reads pending commands and dispatches
        each verb to in-process state. Always acks (even on failure)
        so the operator-side surface sees the outcome.

        STOP, MARK_POISONED set shutdown_event for the bar loop to
        observe at the next tick. PAUSE/RESUME flip self._paused —
        the bar loop drops new orders while paused. FLATTEN currently
        aliases to STOP (graceful shutdown runs the flatten path).
        RECONCILE is a runtime no-op — the ColdStartReconciler is the
        boot-time gate, not a mid-run primitive.
        """
        assert self._command_channel is not None
        while not shutdown_event.is_set():
            # PRD #619-B B3 — command-loop heartbeat on every poll tick.
            # This is the 1Hz cadence the backend freshness evaluator
            # uses to detect command-loop hangs. The broker-probe
            # cadence is owned by ``broker_probe_task`` so the two
            # heartbeats don't share a fate.
            await self._publish_command_loop_block()
            try:
                pending = self._command_channel.read_pending()
            except CommandChannelCorruptError:
                # A malformed command.*.pending.json cannot be safely
                # executed (operator typo / partial manual write). The
                # command_channel contract treats this as a hard stop:
                # halt the engine for inspection rather than log-and-retry
                # forever while the bot keeps trading against a corrupt
                # control channel. The bad file is left in place so the
                # operator can see what was wrong.
                logger.critical(
                    "command channel is corrupt; halting the engine for "
                    "operator inspection (the malformed command file is left in place)"
                )
                shutdown_event.set()
                return
            except Exception:
                logger.exception("command channel read_pending failed; sleeping then retrying")
                pending = []
            for cmd in pending:
                outcome = self._dispatch_command(cmd, shutdown_event)
                try:
                    self._command_channel.ack(cmd, outcome=outcome)
                except Exception:
                    logger.exception(
                        "command channel ack failed for seq=%s verb=%s",
                        cmd.seq,
                        cmd.verb.value,
                    )
            await asyncio.sleep(1.0)

    def _dispatch_command(self, cmd: Command, shutdown_event: asyncio.Event) -> dict:
        """Apply a single command to engine state. Returns the outcome
        payload that will be written into the ack file.

        Broad exception catch so a single bad command doesn't take
        down the poll loop; the outcome reflects the failure for
        post-mortem inspection.
        """
        try:
            if cmd.verb is CommandVerb.PAUSE:
                self._paused = True
                self._persist_desired_state(DesiredState.PAUSED, "command_channel:PAUSE")
                return {"status": "success", "effect": "paused"}
            if cmd.verb is CommandVerb.RESUME:
                self._paused = False
                self._persist_desired_state(DesiredState.RUNNING, "command_channel:RESUME")
                return {"status": "success", "effect": "resumed"}
            if cmd.verb is CommandVerb.STOP:
                self._persist_desired_state(DesiredState.STOPPED, "command_channel:STOP")
                shutdown_event.set()
                return {"status": "success", "effect": "shutdown_signalled"}
            if cmd.verb is CommandVerb.FLATTEN:
                # VCR-0007 / Phase 6A / ADR 0010 — FLATTEN_NOW is now PURE.
                # It cancels owned opens + liquidates positions but does NOT
                # mutate ``desired_state`` and does NOT terminate the
                # process. The "Flatten and pause" UI endpoint composes
                # ``desired_state = PAUSED`` BEFORE enqueueing this verb so
                # the bar loop refuses new entries after the flatten lands.
                # The actual flatten happens in the async bar loop (a flag
                # the loop polls between bars); the ack here is "accepted",
                # not "completed".
                self._flatten_now_requested = True
                return {"status": "accepted", "effect": "flatten_now_queued"}
            if cmd.verb is CommandVerb.MARK_POISONED:
                effect = "poisoned_flag_written"
                if self._output_dir is not None:
                    effect = self._write_operator_poisoned_flag(
                        self._output_dir, cmd.payload.get("reason", "operator_declared")
                    )
                shutdown_event.set()
                return {"status": "success", "effect": effect}
            if cmd.verb is CommandVerb.RECONCILE:
                # Reconciliation PR 2 — runtime RECONCILE is now wired.
                #
                # Design (from the original user spec):
                #   - RECONCILE creates a request ID and immediately inhibits
                #     new submits.
                #   - A dedicated async control task acquires the same lock
                #     used by submit/flatten operations.
                #   - It refreshes broker state, runs the same orchestrator,
                #     and writes the receipt.
                #   - Clean: release the submit barrier.
                #   - Adoption with active exposure: remain paused.
                #   - Poison: write poison evidence and halt/pause fail-closed.
                #
                # The dispatcher returns "accepted" + a request_id immediately
                # so the cockpit can render IN_PROGRESS without waiting on the
                # broker probe; the async reconcile task overwrites the ack
                # with status="completed" + verdict=... when it lands via
                # ``CommandChannel.ack_completion``.
                #
                # Concurrent RECONCILE returns ``already_running`` so the
                # cockpit + operator-surface see exactly one in-flight task
                # at a time (the receipt's ``in_progress`` sentinel reflects
                # the original task; a second probe would corrupt the
                # ordering).
                if self._reconcile_task is not None and not self._reconcile_task.done():
                    return {
                        "status": "already_running",
                        "reason": "another_reconcile_is_in_flight",
                    }
                request_id = mint_intent_id()
                accepted_at_ms = now_ms_utc()
                # Inhibit BEFORE spawning the task so a bar loop reaching the
                # submit site between this ack and the task acquiring the
                # lock still sees the barrier.
                self._inhibit_submits = True
                self._reconcile_task = asyncio.create_task(self._run_runtime_reconcile(cmd, shutdown_event, request_id))
                return {
                    "status": "accepted",
                    "request_id": request_id,
                    "accepted_at_ms": accepted_at_ms,
                }
            return {"status": "error", "effect": f"unknown_verb_{cmd.verb.value}"}
        except Exception as exc:
            logger.exception("command dispatch failed for verb=%s", cmd.verb.value)
            return {"status": "error", "effect": f"dispatch_exception: {exc!r}"}

    async def _build_runtime_broker_snapshot(self) -> object:
        """Build a fresh ``BrokerSnapshot`` for runtime reconciliation.

        Split into a method so tests can inject a fake snapshot without
        wiring a full IBKR client. Production callers (real engine + real
        broker) execute the same ``list_open_orders`` +
        ``executions_for_reconnect_recovery`` sync that ``run.py`` does
        before the cold-start orchestrator.
        """
        from app.broker.ibkr.orders import (
            executions_for_reconnect_recovery,
            list_open_orders,
        )
        from app.engine.live.run import _build_broker_snapshot_from_ibkr

        if self._client is None:
            raise RuntimeError("runtime reconcile requires a real IbkrClient to probe the broker")
        open_orders = await list_open_orders(self._client)
        executions = await executions_for_reconnect_recovery(self._client)
        return _build_broker_snapshot_from_ibkr(open_orders, executions)

    async def _run_runtime_reconcile(
        self,
        cmd: Command,
        shutdown_event: asyncio.Event,
        request_id: str,
    ) -> None:
        """Async control task for runtime RECONCILE.

        Acquires ``self._submit_lock`` (waiting for any in-flight submit or
        flatten), builds a fresh ``BrokerSnapshot`` from the broker, runs
        the cold-start orchestrator, and writes the completion outcome to
        the command ack file via ``ack_completion``. The verdict drives the
        engine's post-condition:

        - Continue → release the submit barrier (``_inhibit_submits = False``).
        - Adopt without active exposure → release the barrier; ledger now
          contains the adopted orders.
        - Adopt with active exposure → leave the barrier set + persist
          ``desired_state = PAUSED`` + flip ``self._paused`` so the next
          bar refuses to re-enter even when the operator resumes the
          submit pipeline.
        - Poison → leave the barrier set + set ``shutdown_event`` (the
          orchestrator already wrote ``poisoned.flag`` + a failed receipt).

        Any unexpected exception (broker.list_open_orders raised, the
        orchestrator blew up before writing its receipt) is logged and
        translated into a ``status="completed", verdict="error"`` ack +
        leaves the barrier set fail-closed. The next bar won't submit;
        the operator must investigate.
        """
        verdict_payload: dict = {"status": "completed", "request_id": request_id}
        try:
            from app.engine.live import reconciliation_orchestrator
            from app.engine.live.account_artifacts import compute_reconcile_namespaces
            from app.engine.live.fleet_reset_baseline import read_applicable_baseline
            from app.engine.live.live_state_sidecar import (
                LiveStateSidecarRepo,
                stable_live_state_path,
            )
            from app.engine.live.reconciliation_classifier import (
                Adopt,
                Continue,
                Poison,
            )

            if (
                self._output_dir is None
                or self._artifacts_root is None
                or not self._strategy_instance_id
                or not self._account_id
            ):
                # Replay / test paths without a real broker can't reconcile.
                # Surface this honestly rather than pretending the verb did
                # anything; leave the barrier set so the operator notices.
                verdict_payload.update(
                    {
                        "verdict": "error",
                        "detail": (
                            "runtime reconcile requires output_dir + artifacts_root + "
                            "strategy_instance_id; one or more were missing"
                        ),
                    }
                )
                return

            bot_order_namespace = f"learn-ai/{self._strategy_instance_id}/v1"
            sidecar_repo = LiveStateSidecarRepo(
                stable_live_state_path(self._artifacts_root, self._strategy_instance_id)
            )
            owned_namespaces, known_sibling_namespaces = compute_reconcile_namespaces(
                artifacts_root=self._artifacts_root,
                account_id=self._account_id,
                current_namespace=bot_order_namespace,
            )
            baseline = read_applicable_baseline(
                live_runs_root=self._output_dir.parent,
                account_id=self._account_id,
                strategy_instance_id=self._strategy_instance_id,
            )

            async with self._submit_lock:
                # Refresh broker caches under the lock so the snapshot is
                # consistent with what just-completed submits landed.
                broker_snapshot = await self._build_runtime_broker_snapshot()

                async def _probe():
                    return broker_snapshot

                result = await reconciliation_orchestrator.reconcile(
                    run_dir=self._output_dir,
                    sidecar=sidecar_repo,
                    broker_probe=_probe,
                    owned_namespaces=owned_namespaces,
                    known_sibling_namespaces=known_sibling_namespaces,
                    now_ms=now_ms_utc,
                    current_run_id=self._run_id,
                    current_strategy_instance_id=self._strategy_instance_id,
                    current_namespace=bot_order_namespace,
                    ignore_unknown_namespaces_before_ms=(
                        baseline.baseline_at_ms if baseline is not None else None
                    ),
                )
                verdict = result.verdict
                if isinstance(verdict, Continue):
                    self._inhibit_submits = False
                    verdict_payload.update({"verdict": "clean"})
                elif isinstance(verdict, Adopt) and not verdict.pause:
                    self._inhibit_submits = False
                    verdict_payload.update(
                        {
                            "verdict": "adopted",
                            "adopted_intent_ids": [o.intent_id for o in verdict.orphans],
                        }
                    )
                elif isinstance(verdict, Adopt) and verdict.pause:
                    # Ambiguous exposure: keep the barrier set AND mark the
                    # engine paused so the operator must explicitly resume
                    # before any new submission lands.
                    self._paused = True
                    self._persist_desired_state(
                        DesiredState.PAUSED,
                        "runtime_reconcile:ambiguous_exposure",
                    )
                    verdict_payload.update(
                        {
                            "verdict": "adopted_paused",
                            "adopted_intent_ids": [o.intent_id for o in verdict.orphans],
                        }
                    )
                elif isinstance(verdict, Poison):
                    # Orchestrator already wrote poisoned.flag + failed
                    # receipt; halt the engine fail-closed.
                    shutdown_event.set()
                    verdict_payload.update({"verdict": "poison", "reason": verdict.reason})
                else:
                    verdict_payload.update(
                        {
                            "verdict": "error",
                            "detail": f"unrecognised verdict type: {type(verdict).__name__}",
                        }
                    )
        except Exception as exc:
            logger.exception("runtime reconcile task failed for request_id=%s", request_id)
            verdict_payload.update({"verdict": "error", "detail": repr(exc)})
        finally:
            # Always overwrite the ack with the completion outcome so the
            # cockpit sees the transition out of IN_PROGRESS — even on the
            # error path. ``ack_completion`` is best-effort: a write
            # failure here cannot fix a broken disk, and the next /status
            # poll will re-derive state from the receipt.
            if self._command_channel is not None:
                try:
                    self._command_channel.ack_completion(cmd, outcome=verdict_payload)
                except Exception:
                    logger.exception(
                        "ack_completion failed for runtime reconcile request_id=%s",
                        request_id,
                    )

    def _persist_desired_state(self, state: DesiredState, reason: str) -> None:
        """Best-effort durable write of operator intent (PRD-A §16.4
        Resolution 7) so PAUSE / STOP survive a crash + reboot.

        Mirrors ``_persist_live_state``'s swallow-with-log contract: a
        desired-state sidecar I/O hiccup must not break command dispatch
        or take down the 1s poll loop. A ``None`` writer disables it
        (replay / test paths that don't pass one).
        """
        if self._desired_state_writer is None:
            return
        try:
            self._desired_state_writer(state, reason)
        except Exception:
            logger.exception(
                "desired-state persist failed for state=%s reason=%s",
                state.value,
                reason,
            )

    def _write_operator_poisoned_flag(self, run_dir: Path, reason: str) -> str:
        """Write a structured operator-declared poisoned.flag.

        Uses the same ``write_poisoned_flag`` / ``PoisonedHaltReason``
        schema as the automatic ``outside_mutation`` / ``lost_fill``
        triggers, under the ``operator_declared`` trigger value, so the
        boot-time ``read_poisoned_flag`` parser at run.py loads it
        cleanly and surfaces the operator's reason — rather than
        choking on a plain-text flag as a "corrupted" sentinel.

        Returns the ack-effect string. If a flag already exists (an
        automatic halt beat the operator to it), the first cause wins
        and we report that without raising.
        """
        reason_payload = PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OPERATOR_DECLARED,
            halted_at_ms=now_ms_utc(),
            last_clean_bar_close_ms=0,
            details={"source": "operator_command", "reason": reason},
        )
        try:
            write_poisoned_flag(run_dir, reason_payload)
            return "poisoned_flag_written"
        except FileExistsError:
            logger.info("poisoned.flag already exists; operator MARK_POISONED preserved prior cause")
            return "poisoned_flag_already_present"
        except OSError:
            logger.exception("could not write operator-declared poisoned.flag")
            return "poisoned_flag_write_failed"

    # ──────────────────── Graceful shutdown helper ───────────────────

    async def _flatten(
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

        Reconciliation PR 2: the whole cancel→liquidate→submit critical
        section runs under ``self._submit_lock`` so a runtime RECONCILE
        cannot probe the broker mid-flatten. The lock is non-reentrant,
        so the internal submit uses ``_submit_pending_with_meta_locked``.
        """
        async with self._submit_lock:
            portfolio.pending_orders.clear()
            # Phase 5C / VCR-0002 — managed cancel-confirm timeout. PRD §5C step
            # 4-5: every managed cancel/flatten path follows cancel → wait for
            # confirms → fetch positions → liquidate. A hung broker that can't
            # confirm cancels must NOT proceed to liquidation; the engine
            # writes halt.flag (CANCEL_CONFIRM_TIMEOUT_HALT) and raises so the
            # operator can reconcile. The emergency-flatten force path has its
            # own audit-event carve-out at the CLI layer.
            try:
                cancelled = await asyncio.wait_for(
                    self._broker.cancel_open_orders(),
                    timeout=self._cancel_confirm_timeout_s,
                )
            except TimeoutError as exc:
                if self._output_dir is not None:
                    try:
                        self._output_dir.mkdir(parents=True, exist_ok=True)
                        (self._output_dir / "halt.flag").write_text(
                            f"CANCEL_CONFIRM_TIMEOUT_HALT "
                            f"timeout_s={self._cancel_confirm_timeout_s} "
                            f"path=_flatten bar_time={bar_time}\n",
                            encoding="utf-8",
                        )
                    except OSError:
                        logger.exception("halt.flag write failed for cancel-confirm timeout")
                raise CancelConfirmTimeoutHaltError(timeout_s=self._cancel_confirm_timeout_s) from exc
            except Exception:
                # Other broker exceptions (network blip, transient) — preserve
                # the prior tolerant behavior so the operator-issued flatten
                # still acts. PRD §5C is silent on this branch; the timeout
                # path is the load-bearing safety.
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
            flat_acks = await self._submit_pending_with_meta_locked(portfolio)
            ctx.log(f"[SHUTDOWN] {bar_time}: submitted {liquidations} liquidation order(s)")
            return flat_acks

    # ──────────────────── Watchdog position helpers ──────────────────

    def _has_open_positions(self) -> bool:
        """Return True if the current run's portfolio has any non-zero position.

        Reads ``_run_portfolio`` which is set just before the child watchdog
        starts and cleared in the run() finally block.  Returns False when
        called outside of an active run (portfolio not set).
        """
        from app.engine.live.live_portfolio import LivePortfolio

        portfolio = self._run_portfolio
        if not isinstance(portfolio, LivePortfolio):
            return False
        return any(pos.quantity != 0 for pos in portfolio.positions.values())

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

    def _append_raw_broker_callbacks(self, raw_events: list[IbkrOrderEvent]) -> None:
        """Persist raw broker callbacks before projection or portfolio mutation."""
        if self._broker_callbacks_wal is None or self._broker_callbacks_wal_attached_to_stream:
            return
        for event in raw_events:
            self._append_raw_broker_callback(event)

    def _append_raw_broker_callback(self, event: IbkrOrderEvent) -> None:
        if self._broker_callbacks_wal is None:
            return
        self._broker_callbacks_wal.append_event(event)

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
                    # Broker execution time so check_outside_mutation can floor
                    # at session start (stale connect-time replay vs concurrent
                    # foreign fill). None when the broker omitted the time.
                    "exec_time_ms": event.exec_time_ms,
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
            session_start_ms=self._session_start_ms,
            owned_perm_ids=self._owned_perm_ids,
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

    def _check_reconnect_revalidation(self, portfolio: LivePortfolio) -> None:
        """Phase 3 reconnect re-validation / VCR-0006.

        Detects a broker reconnect by comparing the client's
        ``connectivity_lost_count`` to the per-engine snapshot. When the
        count has advanced AND ``connection_lost is False`` (i.e. the
        session is restored), re-runs the start-time account identity
        check against the now-restored ``connected_account``. On
        mismatch: clears pending orders, writes ``halt.flag``, raises
        ``ReconnectAccountMismatchHaltError``. On match: bumps the
        per-engine ``connection_epoch`` so a failure list can
        distinguish reconnects.
        """
        client = self._client
        if client is None or not self._account_id:
            return
        current_count = getattr(client, "connectivity_lost_count", 0)
        if current_count <= self._last_connectivity_lost_count:
            return
        # A loss happened. Only re-validate once the connection is back
        # (``connection_lost is False``); otherwise wait for the next bar.
        if getattr(client, "connection_lost", True):
            return
        # Reconnect observed. Snapshot the count + bump the epoch first so
        # repeated bar iterations don't re-validate the same restore.
        self._last_connectivity_lost_count = current_count
        self._connection_epoch += 1
        connected_account = getattr(client, "connected_account", None)
        from app.engine.live.account_identity import (
            AccountIdentityMismatchError,
            verify_account_match,
        )

        try:
            verify_account_match(
                ledger_account_id=self._account_id,
                connected_account=connected_account or "",
            )
        except AccountIdentityMismatchError:
            portfolio.pending_orders.clear()
            if self._output_dir is not None:
                try:
                    self._output_dir.mkdir(parents=True, exist_ok=True)
                    (self._output_dir / "halt.flag").write_text(
                        f"RECONNECT_ACCOUNT_MISMATCH_HALT "
                        f"ledger_account_id={self._account_id} "
                        f"connected_account={connected_account} "
                        f"connection_epoch={self._connection_epoch}\n",
                        encoding="utf-8",
                    )
                except OSError:
                    logger.exception("halt.flag write failed for reconnect-mismatch halt")
            raise ReconnectAccountMismatchHaltError(
                ledger_account_id=self._account_id,
                connected_account=connected_account or "",
                connection_epoch=self._connection_epoch,
            ) from None
        logger.info(
            "Broker reconnect re-validated identity match at epoch=%d (ledger=%s, connected=%s)",
            self._connection_epoch,
            self._account_id,
            connected_account,
        )

    async def _check_verdict_transition_halt(self, portfolio: LivePortfolio) -> None:
        """Phase 7B / VCR-0010 — broker safety verdict observer.

        Consult ``self._verdict_provider`` (if set). On every check (NOT
        only on transition) write ``verdict_snapshot.json`` to
        ``self._output_dir`` so that ``cmd_resume`` (Guard #1) can
        consult the engine's last reading even after the engine exits.
        The snapshot carries ``{verdict, observed_at_ms_utc}`` and is
        overwritten atomically per check.

        On a non-paper-only, non-None verdict, clear pending orders,
        write ``halt.flag``, and raise
        ``BrokerSafetyVerdictTransitionHaltError`` so the bar loop
        exits before any new submission. PRD §7B's "Mid-session
        transition" contract: ``halt.flag`` + durable PAUSED + WAL
        event. The WAL event is gated on the broker-lifecycle
        WAL-location decision and is omitted here; ``halt.flag`` +
        the exception carry the same forensic payload.

        PRD #619-B B3 — when a ``runtime_aggregator`` is wired, the
        verdict literal + the engine's static run-mode/readonly facts
        + the IbkrClient's health snapshot compose into
        ``engine_runtime.json``'s broker block. The composition is
        independent of the halt path so the broker block stays fresh
        on every check, halt or not.
        """
        if self._verdict_provider is None:
            return
        verdict_value = self._verdict_provider()  # type: ignore[operator]
        if self._output_dir is not None:
            self._write_verdict_snapshot(verdict_value)
        await self._publish_broker_block(verdict_value)
        if verdict_value is None or verdict_value == "paper-only":
            return
        # PR 3 / operator-notice — emit before clear (append → fsync → clear).
        _emit_drop_events_for_pending(
            portfolio,
            drop_reason="broker_safety_halt",
            ts_ms=now_ms_utc(),
        )
        if self._output_dir is not None:
            try:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                (self._output_dir / "halt.flag").write_text(
                    f"BROKER_SAFETY_VERDICT_TRANSITION_HALT verdict={verdict_value}\n",
                    encoding="utf-8",
                )
            except OSError:
                logger.exception("halt.flag write failed for verdict transition halt")
        raise BrokerSafetyVerdictTransitionHaltError(verdict=str(verdict_value))

    async def _publish_broker_block(self, verdict_value: object) -> None:
        """PRD #619-B B3 — update the engine_runtime broker block.

        No-op when the aggregator is not wired (replay tests, synthetic
        engines). When wired, composes the block from the verdict
        literal + the engine's run_mode/readonly + the IbkrClient
        health snapshot, and pushes it onto the aggregator. The
        publisher reads the aggregator on its own cadence.
        """
        if self._runtime_aggregator is None:
            return
        verdict_str = verdict_value if isinstance(verdict_value, str) else None
        now_ms = int(time.time() * 1000)
        connection_state: str = "disabled"
        connected_account: str | None = None
        port_class = "unknown"
        probe_completed_at_ms: int | None = None
        reconnect_attempt = 0
        if self._client is not None:
            try:
                health = self._client.health()
            except Exception:
                health = None
            if health is not None:
                connection_state = str(health.connection_state)
                connected_account = health.account_id
                probe_completed_at_ms = health.last_probe_ms
                # port_class is derived from settings.port
                from app.broker.safety_verdict import classify_port

                port_class = classify_port(self._client.settings.port)
                reconnect_attempt = health.reconnect_attempt or 0
        block = build_broker_block(
            verdict_value=verdict_str,
            run_mode=self._run_mode,
            readonly=self._readonly,
            connection_state=connection_state,  # type: ignore[arg-type]
            connection_epoch=self._connection_epoch,
            connected_account=connected_account,
            port_class=port_class,  # type: ignore[arg-type]
            observation_at_ms=now_ms,
            probe_completed_at_ms=probe_completed_at_ms,
            reconnect_attempt=reconnect_attempt,
        )
        await self._runtime_aggregator.update_broker(block)

    async def _publish_command_loop_block(self) -> None:
        """PRD #619-B B3 — update the command-loop block.

        Stamped on every command-poll tick AND on every bar-loop
        iteration so the backend freshness evaluator's command-loop
        check is always sourced from the freshest of the two paths.
        """
        if self._runtime_aggregator is None:
            return
        await self._runtime_aggregator.update_command_loop(
            build_command_loop_block(
                heartbeat_at_ms=int(time.time() * 1000),
                paused=self._paused,
            )
        )

    async def _publish_bar_loop_block(self, minute_bar) -> None:
        """PRD #619-B B3 — update the bar-loop block from the current bar.

        ``heartbeat_at_ms`` is wall-clock (loop scheduling). The bar's
        close time is the market-data freshness signal — a closed
        market has a fresh heartbeat but a stale latest_source_bar_ms.
        ``expected_interval_ms`` comes from the strategy spec / config
        when wired; ``None`` for replay tests.
        """
        if self._runtime_aggregator is None:
            return
        latest_source_bar_ms: int | None
        try:
            latest_source_bar_ms = int(minute_bar.end_time.timestamp() * 1000)
        except (AttributeError, TypeError):
            latest_source_bar_ms = None
        await self._runtime_aggregator.update_bar_loop(
            build_bar_loop_block(
                heartbeat_at_ms=int(time.time() * 1000),
                latest_source_bar_ms=latest_source_bar_ms,
                expected_interval_ms=None,
            )
        )

    async def _publish_initial_control_plane_block(self) -> None:
        """PRD #619-B B3 — seed the control-plane block at startup.

        Reads ``<artifacts_root>/control_plane/daemon_lease.json`` if
        an ``artifacts_root_for_lease`` was provided; otherwise emits a
        sentinel block with ``observed_daemon_boot_id=None``. 619-B B5
        will replace this single startup read with a periodic
        watchdog producer.
        """
        if self._runtime_aggregator is None:
            return
        artifacts_root = self._artifacts_root_for_lease
        path = artifacts_root if isinstance(artifacts_root, Path) else None
        await self._runtime_aggregator.update_control_plane(
            build_control_plane_block_from_lease(path, now_ms=int(time.time() * 1000))
        )

    async def _probe_and_publish_broker_block(self) -> None:
        """Refresh ``_last_probe_ms`` then republish the broker block.

        Called from two sites:

        - The engine's startup hooks (in ``run()``), where this is the
          seed call that populates ``probe_completed_at_ms`` and the
          identity axis before the runtime publisher's first snapshot
          attempt. ``IbkrClient.connect()`` does not set
          ``_last_probe_ms`` — only an explicit ``probe()`` does — so
          without this call the first snapshot would carry
          ``probe_completed_at_ms=None`` (UNKNOWN ⇒ posture_demoted).
        - The ``_broker_probe_loop`` task, every 10s (PRD §B), so the
          block stays inside the 25s freshness threshold while the bar
          loop is idle (pre-market, halted symbol, etc.).

        Persisting ``verdict_snapshot.json`` here matters even pre-bar:
        the Resume guard reads that file via
        ``read_broker_safety_verdict``, treats absence as UNKNOWN, and
        ``resolve_guard_state`` then blocks Resume on
        ``BROKER_SAFETY_UNKNOWN``. The earlier bar-driven write left
        the file absent through every pre-market window, so Resume
        wouldn't enable on a truly fresh deploy even with the broker
        block populated.

        Does NOT run the verdict-transition halt — that check stays
        bar-driven so its observable cadence is unchanged.

        No-ops when ``runtime_aggregator`` is unset: runs that
        intentionally skip the runtime publisher (replay tests,
        synthetic engines) don't pay for a real IBKR probe.
        """
        if self._runtime_aggregator is None:
            return
        if self._client is not None:
            try:
                await self._client.probe()
            except Exception:
                logger.exception("broker probe failed")
        verdict_value: object = None
        if self._verdict_provider is not None:
            try:
                verdict_value = self._verdict_provider()
            except Exception:
                logger.exception("verdict_provider failed")
        if self._output_dir is not None:
            self._write_verdict_snapshot(verdict_value)
        await self._publish_broker_block(verdict_value=verdict_value)

    async def _broker_probe_loop(self, shutdown_event: asyncio.Event) -> None:
        """PRD #619-B §B — refresh the broker block every 10s.

        Wait-first: the startup seed call in ``run()`` is the t=0
        publish, so this loop waits ``BROKER_PROBE_INTERVAL_S`` before
        each probe. ``wait_for`` on the shutdown event gives a clean
        exit without sleeping out the full quantum.
        """
        BROKER_PROBE_INTERVAL_S = 10.0
        while True:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=BROKER_PROBE_INTERVAL_S)
            except TimeoutError:
                # Timeout is the canonical signal that the interval
                # elapsed without a shutdown — the next iteration's
                # probe IS what this handler is for.
                await self._probe_and_publish_broker_block()
            else:
                return  # shutdown_event was set during the wait

    async def _publish_initial_bar_loop_block(self) -> None:
        """PRD #619-B B3 — seed the bar-loop block at startup.

        ``latest_source_bar_ms=None`` advertises "no market-data
        observation yet"; the freshness evaluator already treats
        ``None`` distinctly (no ``BAR_LOOP_LATEST_BAR_STALE`` reason
        is emitted in that case) and pre-market the session-aware
        overlay clamps the bar-loop state to ``NOT_APPLICABLE``
        regardless of heartbeat. The seed exists purely to unblock
        the aggregator's coherent-snapshot gate; once the real bar
        stream produces its first bar, ``_publish_bar_loop_block``
        overwrites this with the real ``latest_source_bar_ms``.
        """
        if self._runtime_aggregator is None:
            return
        await self._runtime_aggregator.update_bar_loop(
            build_bar_loop_block(
                heartbeat_at_ms=int(time.time() * 1000),
                latest_source_bar_ms=None,
                expected_interval_ms=None,
            )
        )

    async def _start_child_watchdog(self, shutdown_event: asyncio.Event) -> object:
        """PRD #619-B B5 follow-up — construct and start the watchdog.

        Builds the four engine-side side-effect callbacks (block
        submissions, persist durable PAUSED + incident, disconnect
        broker, request engine exit) and hands them to the caller's
        factory. The factory is the seam that lets ``cmd_start`` read
        the daemon lease path + the expected ``boot_id`` from env
        without coupling the engine to either.

        PR 2 (operator-notice) wires a ``WatchdogHaltExecutor`` as the
        handler: it orchestrates the 5 steps with per-step timeouts and
        writes a typed ``OperatorIncident`` via the ``IncidentStore``.
        The callbacks below implement the ``WatchdogShutdownController``
        protocol that the executor calls.
        """

        def _block_submissions() -> None:
            self._submissions_blocked = True

        def _persist_paused(reason: str) -> None:
            from app.engine.live.desired_state import DesiredState

            self._persist_desired_state(
                DesiredState.PAUSED,
                f"control_plane_lease_lost:{reason}",
            )

        async def _disconnect_broker() -> None:  # type: ignore[return]
            if self._client is not None:
                try:
                    await self._client.disconnect()
                    return "completed"
                except Exception:
                    logger.exception(
                        "child watchdog: broker disconnect failed",
                    )
                    return "failed"
            return "completed"

        def _request_engine_exit() -> None:
            shutdown_event.set()

        # Build the typed executor (PR 2) when the run_dir is available.
        executor = None
        if self._output_dir is not None:
            from app.engine.live.watchdog_controller import (
                WatchdogHaltExecutor,
                WatchdogTimeouts,
            )
            from app.operator.incidents.store import IncidentStore

            _engine_ref = self

            class _ControllerAdapter:
                """Adapts the engine's sync/async callbacks to the
                WatchdogShutdownController protocol."""

                async def block_submissions(self) -> None:
                    _block_submissions()

                async def persist_paused(self, reason: str) -> None:
                    _persist_paused(reason)

                async def flatten_now(self, reason: str) -> str:
                    """Delegate to the module-level ``_watchdog_flatten_now``."""
                    return await _watchdog_flatten_now(_engine_ref)

                async def disconnect_broker(self) -> str:
                    return await _disconnect_broker() or "completed"

                async def request_engine_exit(self) -> None:
                    _request_engine_exit()

            incident_store = IncidentStore(self._output_dir)
            executor = WatchdogHaltExecutor(
                _ControllerAdapter(),
                incident_store,
                timeouts=WatchdogTimeouts(),
                artifacts_root=self._artifacts_root,
                account_id=self._account_id,
            )

        watchdog = self._watchdog_factory(  # type: ignore[misc]
            block_submissions=_block_submissions,
            persist_paused=_persist_paused,
            disconnect_broker=_disconnect_broker,
            request_engine_exit=_request_engine_exit,
            aggregator=self._runtime_aggregator,
            executor=executor,
        )
        await watchdog.start()
        return watchdog

    def _write_verdict_snapshot(self, verdict_value: object) -> None:
        """Phase 7B Guard #1 — persist the latest verdict reading so
        ``cmd_resume`` can consult it before flipping desired_state to
        RUNNING. Atomic write via ``.tmp`` + rename so a partial read
        in another process can never observe a torn file.

        Snapshot shape (small + flat for cheap reads on the CLI surface):

            {"verdict": "paper-only", "observed_at_ms_utc": 1718553600123}

        ``verdict`` is the raw provider output (str, None, or any other
        truthy value coerced via ``str``). The CLI guard treats anything
        other than the literal "paper-only" as non-passing.
        """
        from datetime import UTC, datetime

        if self._output_dir is None:
            return
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "verdict": verdict_value
                if isinstance(verdict_value, str)
                else (None if verdict_value is None else str(verdict_value)),
                "observed_at_ms_utc": int(datetime.now(UTC).timestamp() * 1000),
            }
            tmp_path = self._output_dir / "verdict_snapshot.json.tmp"
            tmp_path.write_text(
                json.dumps(snapshot, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self._output_dir / "verdict_snapshot.json")
        except OSError:
            logger.exception("verdict_snapshot.json write failed")

    def _resolve_meta_via_intent_wal(self, fill: IbkrOrderEvent) -> _OrderMeta | None:
        """Phase 5E / VCR-0012 — cross-restart fill classifier.

        Folds the intent WAL once per orphan fill and looks up a
        ``SubmittedOrderView`` whose ``perm_id`` matches the fill's
        ``perm_id``. If found AND the view's ``bot_order_namespace``
        matches the namespace this engine owns AND the view's ``order_spec``
        carries symbol+action+quantity, reconstructs an ``_OrderMeta`` and
        returns it. Otherwise returns ``None`` and the fill remains dropped.

        Performance: O(N) per orphan fill where N = WAL line count. The
        WAL is small (typically <10k lines per session) and orphans are
        rare (only crosses-restart cases), so the cost is bounded. If
        orphan rates grow, the LedgerView can be cached on the engine and
        updated incrementally — kept simple here.
        """
        if self._intent_wal_path is None or fill.perm_id is None:
            return None
        from app.engine.live.intent_ledger import LedgerProjection, fold
        from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError

        try:
            events = IntentWal(self._intent_wal_path).read_tail()
        except IntentWalCorruptError:
            # A corrupt WAL must NOT silently mask the drop — surface as
            # "no meta resolved" and let the caller's warning log carry
            # the orphan signal upstream.
            return None
        view = fold(LedgerProjection(), events)
        # Reverse lookup: find the SubmittedOrderView whose perm_id matches.
        # Build only what we need rather than precomputing a Mapping.
        target = next(
            (
                v
                for v in view.submitted_orders.values()
                if v.perm_id is not None and int(v.perm_id) == int(fill.perm_id)
            ),
            None,
        )
        if target is None:
            return None
        spec = target.order_spec
        if not isinstance(spec, dict):
            return None
        symbol_obj = spec.get("symbol")
        action_obj = spec.get("action")
        quantity_obj = spec.get("quantity")
        if not isinstance(symbol_obj, str) or not isinstance(action_obj, str) or quantity_obj is None:
            return None
        try:
            magnitude = abs(int(float(quantity_obj)))
        except (TypeError, ValueError):
            return None
        if magnitude == 0:
            return None
        signed_qty = magnitude if action_obj.upper() == "BUY" else -magnitude
        return _OrderMeta(
            symbol=symbol_obj,
            tag="Phase5E:cross-restart",
            signed_qty=signed_qty,
            submitted_at_ms=0,
        )

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
            # Phase 5E / VCR-0012 — before treating this as foreign, try
            # the cross-restart classifier. The fill's order_id wasn't
            # placed in THIS process, but the fill's perm_id may be from a
            # prior session of THIS runner that landed the order pre-crash.
            recovered = self._resolve_meta_via_intent_wal(fill)
            if recovered is not None:
                logger.info(
                    "Cross-restart fill classified bot-owned by perm_id=%s (order_id=%s, symbol=%s)",
                    fill.perm_id,
                    fill.order_id,
                    recovered.symbol,
                )
                meta = recovered
            else:
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
        # Commission is reported by IBKR a beat after the execution; PRD-B reads
        # it off the polled Fill (see orders._fill_to_event). ``recorded_fee``
        # preserves the unknown (None) so the execution artifact never invents a
        # zero; the portfolio-facing ``fee`` is 0 when unknown so cash math is
        # never poisoned, and the real commission once reported.
        recorded_fee = Decimal(str(fill.fee)) if fill.fee is not None else None
        return OrderEvent(
            order_id=int(fill.order_id),
            symbol=meta.symbol,
            time=fill_time,
            fill_price=fill_price,
            fill_quantity=signed_fill_qty,
            direction=direction,
            fee=recorded_fee if recorded_fee is not None else Decimal("0"),
            recorded_fee=recorded_fee,
            tag=meta.tag,
            # VCR-P3-L — carry the broker-reported execution time through to
            # the receipt. ``IbkrOrderEvent.exec_time_ms`` is what IBKR's
            # ``Execution.time`` reported; the engine's ``fill_time`` above
            # is derived from ``ts_ms`` (wall-clock observation), which can
            # drift from the broker time under network or event-loop latency.
            exec_time_ms=fill.exec_time_ms,
        )
