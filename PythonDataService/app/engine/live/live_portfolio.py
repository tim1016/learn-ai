"""Live portfolio adapter for paper trading.

The strategy-facing methods intentionally mirror
``app.engine.execution.portfolio.Portfolio``: strategies can call
``set_holdings`` and ``liquidate`` synchronously inside bar handlers. The
live engine drains the resulting pending orders and submits them through the
existing IBKR paper-order boundary asynchronously.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.engine.live.intent_wal import IntentWal

from app.broker.ibkr.account import fetch_account_summary, fetch_positions
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderEvent, IbkrOrderSpec
from app.broker.ibkr.orders import (
    cancel_paper_order,
    list_open_orders,
    place_paper_order,
    stream_order_events,
)
from app.engine.execution.order import Direction, Order, OrderEvent, OrderType
from app.engine.execution.order_sizer import (
    FixedNotional,
    FixedShares,
    OrderSizer,
    SetHoldings,
    StrategyExplicit,
)
from app.engine.execution.portfolio import Position
from app.engine.execution.sizing import SimpleFloorSizing, SizingModel
from app.engine.live.account_owner_fence import (
    require_account_clerk_write_grant,
)
from app.engine.live.intent_events import DropReason
from app.schemas.broker_capability import SessionKind
from app.schemas.live_runs import GateResult
from app.services.session_authority import (
    SessionAuthorityState,
    evaluate_session_submit,
)


def _try_int(value: object) -> int | None:
    """Convert a possibly-stringly-typed broker id to ``int``; ``None`` if absent."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _describe_policy_value(policy: object) -> str:
    """Stringify the policy's principal value (decimal-safe) for the audit row."""
    if isinstance(policy, FixedShares):
        return str(policy.value)
    if isinstance(policy, SetHoldings):
        return str(policy.fraction)
    if isinstance(policy, FixedNotional):
        return str(policy.value)
    if isinstance(policy, StrategyExplicit):
        return ""
    return ""


logger = logging.getLogger(__name__)
ACCOUNT_OBSERVATION_LEASE_SHADOW_DIVERGENCES: Counter[str] = Counter()


class LiveBrokerEventStreamError(RuntimeError):
    """Raised when the IBKR order-event stream has terminated unexpectedly.

    Once the background stream task dies, fills stop arriving at the
    engine. Continuing to submit orders while the broker side is silent
    would silently desync the portfolio from broker reality. The engine
    surfaces this as a failed run rather than a degraded one.
    """


def _append_sizing_skip_line(path: Path, payload: dict) -> None:
    """Best-effort append to the SIZING_SKIP audit log. A write failure
    is logged but does not break the bar handler — the in-memory
    ``sizing_resolutions`` list is the loss-tolerant secondary surface."""
    import json as _json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(payload) + "\n")
            fh.flush()
    except OSError:
        logger.exception("sizing_skip.jsonl append failed; in-memory audit preserved")


class ControlledLiveHaltError(RuntimeError):
    """Base class for deliberate live-runtime halts that must not recovery-flatten."""


class BrokerSafetyVerdictBlockError(ControlledLiveHaltError):
    """Phase 7B / VCR-0010 — broker safety verdict is not ``paper-only``.

    Raised by ``submit_pending_orders`` BEFORE any broker call when the
    injected ``verdict_provider`` returns a verdict other than
    ``paper-only`` (i.e. ``unsafe`` or ``unknown``). Per PRD §7B the
    engine refuses to submit orders while the verdict is unsafe and
    refuses to start while the verdict is unknown outside the named
    diagnostic path. Mid-session transitions from ``paper-only`` to a
    non-``paper-only`` verdict trigger this exception on the next pending
    submit; the engine catches and writes ``halt.flag`` + durable
    ``desired_state=PAUSED`` per the PRD's "Mid-session transition"
    contract (event ``BROKER_SAFETY_VERDICT_TRANSITION_HALT``).

    ``verdict`` is the literal ``final_verdict`` value the provider
    returned (``unsafe``, ``unknown``, etc.); ``detail`` carries any
    additional context the provider chose to surface.
    """

    def __init__(self, *, verdict: str, detail: str | None = None) -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"BrokerSafetyVerdictBlockError(verdict={verdict!r}){suffix}")
        self.verdict = verdict
        self.detail = detail


class SubmitGateBlockError(ControlledLiveHaltError):
    """Base class for controlled pre-submit gate refusals."""


class AccountFreezeBlockError(SubmitGateBlockError):
    """Raised when account-level freeze evidence blocks order submission."""

    def __init__(self, *, evidence: object) -> None:
        reason = getattr(evidence, "reason", None)
        super().__init__(f"AccountFreezeBlockError(reason={reason!r})")
        self.evidence = evidence


class AccountRegistryBlockError(SubmitGateBlockError):
    """Raised when the account instance registry blocks order submission."""

    def __init__(self, *, gate_result: object) -> None:
        reason = getattr(gate_result, "operator_reason", None)
        super().__init__(f"AccountRegistryBlockError(reason={reason!r})")
        self.gate_result = gate_result


class AccountTruthBlockError(SubmitGateBlockError):
    """Raised when cached Account Truth blocks order submission."""

    def __init__(self, *, gate_result: object) -> None:
        reason = getattr(gate_result, "operator_reason", None)
        super().__init__(f"AccountTruthBlockError(reason={reason!r})")
        self.gate_result = gate_result


class SessionPolicyBlockError(SubmitGateBlockError):
    """Raised when the current session is outside the strategy submit policy."""

    def __init__(
        self,
        *,
        session_state: SessionAuthorityState,
        allowed_sessions: tuple[SessionKind, ...],
        order_mechanism_sessions: tuple[SessionKind, ...],
        reason: str,
    ) -> None:
        super().__init__(
            "SessionPolicyBlockError("
            f"phase={session_state.phase!r}, "
            f"allowed_sessions={allowed_sessions!r}, "
            f"order_mechanism_sessions={order_mechanism_sessions!r}, "
            f"reason={reason!r})"
        )
        self.session_state = session_state
        self.allowed_sessions = allowed_sessions
        self.order_mechanism_sessions = order_mechanism_sessions
        self.reason = reason


class GateResultProvider(Protocol):
    """Provider returning a canonical submit gate result."""

    def __call__(self) -> GateResult | None: ...


class SessionGateProvider(Protocol):
    """Provider returning the current centralized session-authority state."""

    def __call__(self) -> SessionAuthorityState | None: ...


class SubmitUncertainHaltError(RuntimeError):
    """Phase 5D / VCR-0002 — submit state machine reached HALT.

    Raised by ``submit_pending_orders`` after appending
    ``SUBMIT_UNCERTAIN_HALTED`` to the WAL. The engine catches this and
    writes ``halt.flag`` + durable ``desired_state=PAUSED`` per PRD §5D
    HALT semantics. The bar loop must exit; no new orders may submit
    on this run until operator reconciliation clears the three Resume
    guards (broker safety verdict, cold-start reconciler verdict,
    no unresolved uncertain intent in WAL).
    """

    def __init__(
        self,
        *,
        intent_id: str,
        order_ref: str,
        probe_result: str,
        retry_count: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"SubmitUncertainHaltError(intent_id={intent_id!r} order_ref={order_ref!r} "
            f"probe={probe_result} retry_count={retry_count} reason={reason!r})"
        )
        self.intent_id = intent_id
        self.order_ref = order_ref
        self.probe_result = probe_result
        self.retry_count = retry_count
        self.reason = reason


@runtime_checkable
class BrokerAdapter(Protocol):
    """Async broker surface LivePortfolio + LiveEngine consume — the typed
    ``IBrokerAdapter`` contract of ADR 0002. Both the executing
    ``IbkrBrokerAdapter`` and the shadow ``NoSubmitBrokerAdapter`` implement
    it, so the engine depends on the protocol, never a concrete adapter.

    ADR 0008 / Phase 5B — concrete implementations declare a class variable
    ``requires_durable_submit: ClassVar[bool]``. ``True`` means ``place_order``
    reaches a real broker; ``LivePortfolio`` refuses construction without an
    ``IntentWal`` + ``bot_order_namespace``. ``False`` is shadow / fake / replay.
    The invariant is read at runtime via ``getattr(broker,
    'requires_durable_submit', False)`` — a missing marker defaults to ``False``
    (safe: skips the WAL enforcement). The marker is intentionally NOT a
    Protocol member, because ``@runtime_checkable`` Protocols verify data
    attributes by name on Python 3.12+, and declaring it here would force
    every fake/test broker (which uses ``isinstance(broker, ReplayBrokerAdapter)``
    and friends elsewhere in the engine) to declare it too. A new real-broker
    adapter MUST set this to ``True`` explicitly or it silently bypasses ADR 0008.
    """

    async def fetch_account_summary(self): ...

    async def fetch_positions(self): ...

    async def place_order(self, spec: IbkrOrderSpec, *, perm_id_wait_s: float = 0.0) -> IbkrOrderAck: ...

    async def cancel_open_orders(self) -> list[int]:
        """Cancel every order this runner still has open at the broker.

        Real adapters scope to the runner's own orders so that running
        the live engine never touches an unrelated open order on the
        same paper account. The method returns only after every targeted
        order is terminal and, when event streaming is active, any terminal
        fills have reached the adapter's event buffer. Returns the list of
        cancelled ``order_id`` values.
        """
        ...

    # Phase 5D / VCR-0002 — ``probe_intent_status`` is NOT declared on the
    # Protocol so that the runtime ``isinstance(broker, BrokerAdapter)``
    # check still succeeds for legacy / replay / no-submit adapters that
    # never need a probe. The submit loop in ``submit_pending_orders``
    # duck-types it via ``getattr(broker, "probe_intent_status", None)``
    # and falls back to ``BrokerProbe.NOT_PROVABLE`` when the method is
    # absent — the safe halt-default. A real-broker adapter MUST add the
    # method to enable RETRY_ONCE / RECOVER_ADOPT (Phase 5C ownership
    # query subclass).


class IbkrBrokerAdapter(BrokerAdapter):
    """Production adapter over the existing broker module.

    Tracks the set of order IDs this adapter has placed so that
    ``cancel_open_orders`` only touches the live runner's own orders.
    Any pre-existing or unrelated order on the paper account is left
    alone, even if it shares the connected client. Buffers IBKR order
    events so the live engine can drain real fills per bar.
    """

    # ADR 0008 / Phase 5B — this adapter calls ``IB.placeOrder`` for real,
    # so any ``LivePortfolio`` constructed around it MUST carry an IntentWal
    # and a non-empty bot_order_namespace. Enforced in ``LivePortfolio.__post_init__``.
    requires_durable_submit: ClassVar[bool] = True

    def __init__(
        self,
        client: IbkrClient,
        *,
        require_account_owner_write_fence: bool = False,
        owner_generation_provider: Callable[[], int] | None = None,
    ) -> None:
        self._client = client
        self._owned_order_ids: set[int] = set()
        self._event_buffer: list[IbkrOrderEvent] = []
        self._event_task: asyncio.Task[None] | None = None
        self._stream_failure: BaseException | None = None
        self._broker_callback_sink: Callable[[IbkrOrderEvent], None] | None = None
        self._observed_fill_count_by_order_id: dict[int, int] = {}
        self._event_buffer_changed = asyncio.Event()
        self._require_account_owner_write_fence = require_account_owner_write_fence
        self._owner_generation_provider = owner_generation_provider

    @property
    def owned_order_ids(self) -> set[int]:
        return set(self._owned_order_ids)

    @property
    def stream_failure(self) -> BaseException | None:
        """The exception that terminated the order-event stream, if any.

        ``None`` while the stream is healthy (or hasn't been started).
        Set once if the streaming task exits via an unhandled exception
        — the engine reads this each iteration and fails the run, since
        a dead stream means broker fills are no longer being ingested.
        """
        return self._stream_failure

    def set_broker_callback_sink(self, sink: Callable[[IbkrOrderEvent], None] | None) -> None:
        """Install a synchronous receipt-time hook for durable raw callbacks."""
        self._broker_callback_sink = sink

    def require_account_owner_write_fence(
        self,
        owner_generation_provider: Callable[[], int],
    ) -> None:
        """Require AccountOwner context before any broker-write method."""
        self._require_account_owner_write_fence = True
        self._owner_generation_provider = owner_generation_provider

    async def fetch_account_summary(self):
        return await fetch_account_summary(self._client)

    async def fetch_positions(self):
        return await fetch_positions(self._client)

    async def place_order(self, spec: IbkrOrderSpec, *, perm_id_wait_s: float = 0.0) -> IbkrOrderAck:
        self._enforce_account_owner_write_fence("broker.place_order")
        ack = await place_paper_order(self._client, spec, perm_id_wait_s=perm_id_wait_s)
        self._owned_order_ids.add(int(ack.order_id))
        return ack

    async def cancel_open_orders(self) -> list[int]:
        self._enforce_account_owner_write_fence("broker.cancel_open_orders")
        open_orders = await list_open_orders(self._client)
        targeted = [
            int(order.order_id)
            for order in open_orders
            if int(order.order_id) in self._owned_order_ids
        ]
        if not targeted:
            return []

        for order in open_orders:
            if int(order.order_id) not in self._owned_order_ids:
                # Foreign order on this paper account — never the
                # runner's to cancel. Leaving it alone is the whole
                # point of the ownership filter.
                continue
            await cancel_paper_order(self._client, order.order_id)

        targeted_set = set(targeted)
        while True:
            remaining = {
                int(order.order_id)
                for order in await list_open_orders(self._client)
                if int(order.order_id) in targeted_set
            }
            if not remaining:
                break
            await asyncio.sleep(0.05)

        if self._event_task is not None:
            await self._wait_for_terminal_fills(targeted_set)
        return targeted

    def _enforce_account_owner_write_fence(self, boundary: str) -> None:
        if not self._require_account_owner_write_fence:
            return
        account_id = getattr(self._client, "connected_account", None)
        require_account_clerk_write_grant(
            account_id=account_id,
            boundary=boundary,
            clerk_generation_provider=self._owner_generation_provider,
        )

    async def _wait_for_terminal_fills(self, targeted_order_ids: set[int]) -> None:
        """Wait until the event buffer contains every fill on terminal orders.

        The engine sizes an instance-scoped liquidation from its owned fill
        ledger, never from account-net positions. Terminal cancellation is
        therefore not complete for that caller until the polling event stream
        has observed every fill now present on the cached terminal trades.
        The caller wraps ``cancel_open_orders`` in its managed timeout, so an
        unprovable cache or failed stream fails closed instead of hanging.
        """

        while True:
            self._event_buffer_changed.clear()
            if self._stream_failure is not None:
                raise BrokerError(
                    "IBKR order-event stream failed before cancel confirmation"
                ) from self._stream_failure

            trades_by_order_id = {
                int(trade.order.orderId): trade
                for trade in self._client.ib.trades()
                if int(trade.order.orderId) in targeted_order_ids
            }
            missing_trades = targeted_order_ids - trades_by_order_id.keys()
            if missing_trades:
                raise BrokerError(
                    "IBKR terminal order cache missing owned order(s): "
                    f"{sorted(missing_trades)}"
                )

            fills_complete = all(
                self._observed_fill_count_by_order_id.get(order_id, 0)
                >= len(getattr(trade, "fills", []) or [])
                for order_id, trade in trades_by_order_id.items()
            )
            if fills_complete:
                return

            await self._event_buffer_changed.wait()

    async def start_event_stream(self) -> None:
        """Begin draining IBKR order events into the local buffer."""
        if self._event_task is not None:
            return
        self._stream_failure = None
        self._event_task = asyncio.create_task(self._run_event_stream())

    async def stop_event_stream(self) -> None:
        if self._event_task is None:
            return
        self._event_task.cancel()
        try:
            await self._event_task
        except asyncio.CancelledError:
            pass
        finally:
            self._event_task = None

    async def _run_event_stream(self) -> None:
        try:
            async for event in stream_order_events(self._client):
                # Per spec § 7: persist all received executions to
                # executions.parquet whether or not Python originated
                # them, regardless of clientId. The previous
                # ``order_id in owned`` filter dropped foreign fills
                # entirely — defeating the outside-mutation halt
                # check that needs to see foreign executions to fire.
                # Downstream ownership filtering for the engine's
                # portfolio-update path lives in
                # ``LiveEngine._convert_ibkr_fill`` (which checks
                # ``_order_meta`` per fill); it correctly drops
                # foreigns from the portfolio side. The halt-detection
                # consumer (Phase C-2c-b2-ii) reads the unfiltered
                # buffer instead.
                #
                # ADR 0014 / issue #684 PR3 — buffer every callback type so the
                # host runner can persist the raw broker-callback WAL. The
                # engine's portfolio path still no-ops non-fill events in
                # ``LiveEngine._convert_ibkr_fill``.
                if self._broker_callback_sink is not None:
                    self._broker_callback_sink(event)
                if event.event_type == "fill":
                    order_id = int(event.order_id)
                    self._observed_fill_count_by_order_id[order_id] = (
                        self._observed_fill_count_by_order_id.get(order_id, 0) + 1
                    )
                self._event_buffer.append(event)
                self._event_buffer_changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Record the cause so the engine can fail the run on the
            # next iteration — silently retiring would leave the engine
            # submitting orders while no fills arrive, desyncing the
            # portfolio from broker reality. Logging here keeps the
            # original traceback in the operator log.
            logger.exception("IBKR order-event stream terminated unexpectedly")
            self._stream_failure = exc
            self._event_buffer_changed.set()

    def drain_broker_events(self) -> list[IbkrOrderEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events


@dataclass
class LivePortfolio:
    """Portfolio-shaped live state with broker-backed account snapshots."""

    broker: BrokerAdapter
    cash: Decimal = Decimal(0)
    net_liquidation: Decimal = Decimal(0)
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[Order] = field(default_factory=list)
    reference_price: dict[str, Decimal] = field(default_factory=dict)
    total_fees: Decimal = Decimal(0)
    _next_order_id: int = 0
    # Sizing policy — shared with the simulated Portfolio (see
    # app.engine.execution.sizing). Defaults to plain-floor; a live run
    # mirroring LEAN sets LeanSetHoldingsSizing + order_fee.
    sizing_model: SizingModel = field(default_factory=SimpleFloorSizing)
    order_fee: Decimal = Decimal(0)
    # ADR 0009 — when set, ``set_holdings`` consults the policy-application
    # adapter instead of the percent path. ``None`` ⇒ legacy ``SimpleFloorSizing``
    # path (pre-policy era). Wired only for the ``set_holdings`` surface;
    # ``market_order`` and ``liquidate`` are unaffected (explicit strategy
    # sizing wins; liquidate is a flatten command, not a sizing surface).
    order_sizer: OrderSizer | None = None
    # ADR 0009 § 11 — captured per-trade audit list. Each row is the sizing
    # decision the policy made for one set_holdings call: policy_kind,
    # policy_value (decimal-stringified), intended_qty, reference_price
    # (decimal-stringified), sized_via. The engine flushes this into the
    # live_state sidecar at each bar so the cockpit can read it.
    sizing_resolutions: list[dict] = field(default_factory=list)
    # ADR 0009 § 6 — registered sizing surface. ``None`` = unknown (legacy
    # callers / tests). When ``"explicit"`` is registered AND the strategy
    # invokes ``set_holdings`` (the policy surface), the first such call is
    # a fail-fast registration bug — the order will fire with a misleading
    # ledger otherwise.
    registered_sizing_surface: str | None = None
    # ADR 0008 / Phase 5A — intent identity foundation. When ``intent_wal``
    # and ``bot_order_namespace`` are both set, ``set_holdings`` mints an
    # ``intent_id`` (only when ``delta != 0``; a skip never mints), the
    # submit path stamps ``order_ref = {namespace}:{intent_id}`` on the
    # ``IbkrOrderSpec``, and ``PENDING_INTENT`` / ``SUBMITTED`` /
    # ``ACK_FAILED_UNCERTAIN`` are appended around ``broker.place_order``.
    # Legacy / replay callers leave these unset and keep their pre-Phase-5A
    # behaviour. Phase 5D wires the full submit-retry state machine; Phase 8
    # promotes the in-memory ``sizing_resolutions`` list to a WAL fold.
    intent_wal: IntentWal | None = None
    bot_order_namespace: str = ""
    # Phase 7B / VCR-0010 — broker safety verdict provider. Callable returning
    # the current verdict's ``final_verdict`` value (``paper-only``,
    # ``unsafe``, ``unknown``) or ``None`` when no verdict is available yet
    # (e.g. broker disconnected, probe in progress). ``submit_pending_orders``
    # consults this BEFORE each broker call and raises
    # ``BrokerSafetyVerdictBlockError`` if the verdict is anything other than
    # ``paper-only`` or ``None``. ``None`` keeps the prior pre-Phase-7B
    # behavior (no verdict enforcement) for replay / shadow / legacy paths.
    verdict_provider: object = None  # Callable[[], str | None] | None
    # Account-scoped lifecycle freeze provider. When it returns active
    # freeze evidence, submit is refused before any broker call.
    account_freeze_provider: object = None
    # Account-scoped instance registry provider. When it returns any gate result
    # other than pass, submit is refused before any broker call.
    account_registry_gate_provider: GateResultProvider | None = None
    # Account Truth provider. When it returns any gate result other than pass,
    # submit is refused before any broker call. The provider must read cached
    # Account Truth only; it must not sweep IBKR from the submit path.
    account_truth_gate_provider: GateResultProvider | None = None
    # Shadow-only durable Account Observation Lease comparison. This provider
    # never changes the submit decision until sequence parity has been proven;
    # its outcome is logged and counted against the live Account Truth gate.
    account_observation_lease_gate_provider: GateResultProvider | None = None
    # Durable account-event writer owned by the engine. It records every
    # paired shadow comparison at an actual submit boundary so parity evidence
    # survives child-process restarts without introducing a second scheduler.
    account_observation_lease_shadow_comparison_observer: (
        Callable[[GateResult, GateResult], object] | None
    ) = None
    # PRD #1005 Slice 2 — centralized session-authority submit gate. When
    # wired, pending orders are refused before any broker call unless the
    # current phase is both strategy-declared and supported by the active order
    # mechanism. Until Slice 3 lands extended-hours placement, the mechanism
    # side stays RTH-only even if the strategy declares PRE/POST/OVERNIGHT.
    session_gate_provider: SessionGateProvider | None = None
    allowed_sessions: tuple[SessionKind, ...] = ("RTH",)
    order_mechanism_sessions: tuple[SessionKind, ...] = ("RTH",)
    # AccountOwner mode: when set, runner code emits an AccountOwnerSubmitIntent
    # to this callable instead of placing the order through its broker adapter.
    account_owner_submitter: object = None
    account_id: str = ""
    strategy_instance_id: str = ""
    run_id: str = ""
    owner_generation_provider: object = None
    trace_id_provider: object = None
    # Phase 8 / VCR-0003 — SIZING_SKIP audit log path. When set, every
    # set_holdings call that resolves to ``delta == 0`` (target == current
    # → no order to submit) appends a JSON line to this file capturing
    # the full skip context (symbol, policy_kind, policy_value,
    # target_qty, current_qty, reference_price, reason, ts_ms_utc). The
    # PRD §8 contract: skips do NOT mint an intent_id and so don't fit
    # the IntentEvent invariant (order_ref == namespace:intent_id). A
    # separate JSONL keeps the durable audit honest without forcing a
    # data-model relaxation that ripples through the fold and the
    # ColdStartReconciler. ``None`` keeps the prior pre-Phase-8-skip
    # behavior — the in-memory ``sizing_resolutions`` list is the only
    # surface a Sizing card can read.
    sizing_skip_log_path: Path | None = None
    # Internal: ``order_id → intent_id`` so the submit step can recover the
    # identity minted in ``set_holdings`` without changing the ``Order``
    # value type (existing tests construct ``Order`` directly).
    _intent_by_order_id: dict[int, str] = field(default_factory=dict)
    _last_minted_intent_id: str | None = None

    def __post_init__(self) -> None:
        """ADR 0008 / Stage 6 — enforce the durable AccountOwner invariant.

        A ``LivePortfolio`` whose broker adapter declares
        ``requires_durable_submit = True`` cannot be constructed without an
        AccountOwner submitter and a non-empty ``bot_order_namespace``. This
        closes the legacy WAL-direct-submit lane: the protocol exists, the
        engine cannot bypass AccountOwner.

        Shadow / fake adapters (``requires_durable_submit = False`` or unset)
        retain the pre-Phase-5B opt-in behaviour for backwards compatibility
        with the replay/test fixtures.
        """
        if self.account_owner_submitter is not None and self.intent_wal is not None:
            raise ValueError("AccountOwner mode and intent_wal are mutually exclusive durability lanes")
        if self.account_owner_submitter is not None:
            if not self.bot_order_namespace:
                raise ValueError("AccountOwner mode requires a non-empty bot_order_namespace")
            return
        if not getattr(self.broker, "requires_durable_submit", False):
            return
        raise ValueError(
            "ADR 0028 / Stage 6: LivePortfolio with a real-broker adapter "
            f"({type(self.broker).__name__}) cannot submit through the legacy "
            "IntentWal lane. Pass account_owner_submitter plus a non-empty "
            "bot_order_namespace so AccountOwner remains the sole writer."
        )

    def drop_pending_before_submit(self, *, drop_reason: DropReason, ts_ms: int) -> None:
        """Append drop events for WAL-identified pending orders, then clear memory."""

        if self.intent_wal is not None and self.bot_order_namespace:
            from app.engine.live.intent_events import IntentEventType
            from app.engine.live.order_identity import build_order_ref

            for order in self.pending_orders:
                intent_id = self._intent_by_order_id.get(order.order_id)
                if intent_id is None:
                    continue
                self.intent_wal.append(
                    event_type=IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
                    intent_id=intent_id,
                    bot_order_namespace=self.bot_order_namespace,
                    order_ref=build_order_ref(self.bot_order_namespace, intent_id),
                    drop_reason=drop_reason,
                    ts_ms=ts_ms,
                )
        self.pending_orders.clear()
        self._intent_by_order_id.clear()

    async def refresh_from_broker(self) -> None:
        """Refresh cash, net liquidation, and positions from the broker."""
        account = await self.broker.fetch_account_summary()
        self.cash = Decimal(str(account.cash_balance or 0))
        self.net_liquidation = Decimal(str(account.net_liquidation or account.cash_balance or 0))

        snapshot = await self.broker.fetch_positions()
        refreshed: dict[str, Position] = {}
        for pos in snapshot.positions:
            refreshed[pos.symbol.upper()] = Position(
                symbol=pos.symbol.upper(),
                quantity=int(pos.quantity),
                average_price=Decimal(str(pos.avg_cost)),
            )
        self.positions = refreshed

    def update_reference_price(self, symbol: str, price: Decimal) -> None:
        self.reference_price[symbol.upper()] = price

    def get_position(self, symbol: str) -> Position:
        sym = symbol.upper()
        if sym not in self.positions:
            self.positions[sym] = Position(symbol=sym)
        return self.positions[sym]

    def total_value(self) -> Decimal:
        has_open_positions = any(pos.quantity != 0 for pos in self.positions.values())
        if self.net_liquidation and (not has_open_positions or not self.reference_price):
            return self.net_liquidation
        value = self.cash
        for sym, pos in self.positions.items():
            price = self.reference_price.get(sym, pos.average_price)
            value += pos.market_value(price)
        return value

    def _next_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id

    def submit_market_order(
        self,
        symbol: str,
        quantity: int,
        time: datetime,
        tag: str = "",
        *,
        explicit_call: bool = False,
    ) -> Order:
        if quantity == 0:
            raise ValueError("cannot submit a zero-quantity market order")
        # ADR 0009 § 6 / VCR-P3-F — order-surface reverse fail-fast. A
        # strategy registered as ``policy`` invoking ``market_order``
        # (the explicit surface) is the mirror of the forward case
        # caught in ``set_holdings``. ``explicit_call=True`` flags the
        # invocation as originating from ``ctx.market_order`` /
        # ``strategy.market_order`` (the public explicit surfaces);
        # ``set_holdings``, ``liquidate``, and engine-internal flatten
        # paths continue to call without the flag so they remain
        # unaffected.
        if explicit_call and self.registered_sizing_surface == "policy":
            raise RuntimeError(
                f"Order-surface mismatch (ADR 0009 § 6 / VCR-P3-F): strategy "
                f"registered with sizing_surface='policy' invoked market_order "
                f"on {symbol.upper()}. Re-register as sizing_surface='explicit' "
                "or change the strategy to use set_holdings; halting before "
                "the misleading entry."
            )
        order = Order(
            order_id=self._next_id(),
            symbol=symbol.upper(),
            quantity=quantity,
            order_type=OrderType.MARKET,
            time=time,
            direction=Direction.LONG if quantity > 0 else Direction.SHORT,
            tag=tag,
        )
        self.pending_orders.append(order)
        return order

    def set_holdings(
        self,
        symbol: str,
        target_fraction: Decimal | float,
        time: datetime,
        tag: str = "",
    ) -> Order | None:
        """Resolve a target position via the sizing policy (ADR 0009).

        When ``order_sizer`` is set, ``set_holdings`` consults the policy
        adapter — ``FixedShares`` returns the integer share count directly,
        ``SetHoldings`` delegates to ``LeanSetHoldingsSizing`` (wired in PR2),
        ``FixedNotional`` floors notional / price (wired in PR4). When unset,
        falls back to the legacy ``sizing_model`` path so existing replay and
        test setups (which never configured a policy) keep their prior behavior
        until they are migrated.

        A resolved target that matches the current position is a no-op (returns
        ``None``); the engine logs a *sizing skip* upstream when the policy
        resolves to a zero target while flat (ADR 0009 § 4).
        """
        sym = symbol.upper()
        target_fraction = Decimal(str(target_fraction))
        price = self.reference_price.get(sym)
        fixed_shares_without_price = (
            price is None
            and self.order_sizer is not None
            and isinstance(self.order_sizer.policy, FixedShares)
        )
        if price is None and not fixed_shares_without_price:
            raise RuntimeError(f"Cannot set_holdings on {sym}: no reference price.")
        current_pos = self.get_position(sym)
        # ADR 0009 § 6 — order-surface fail-fast. A strategy registered as
        # ``explicit`` invoking ``set_holdings`` (a policy surface) is a
        # registration bug; halting here prevents a misleading ledger entry.
        # ``liquidate`` is exempt (it's a flatten command, not a sizing
        # surface), so we only enforce this on the entry path.
        if self.registered_sizing_surface == "explicit":
            raise RuntimeError(
                f"Order-surface mismatch (ADR 0009 § 6): strategy registered "
                f"with sizing_surface='explicit' invoked set_holdings on {sym}. "
                "Re-register as sizing_surface='policy' or change the strategy "
                "to use market_order; halting before the misleading entry."
            )
        if self.order_sizer is not None:
            target_quantity = self.order_sizer.resolve_set_holdings_quantity(
                target_fraction=target_fraction,
                reference_price=price,
                order_fee=self.order_fee,
            )
            # ADR 0009 § 11 — record the resolution before computing the delta
            # so the audit log captures BOTH skip cases (delta == 0) and
            # submitted orders. The cockpit later joins by intent_id when the
            # WAL is fully wired; until then, the row is timestampable by the
            # bar's ``time`` and surfaced verbatim.
            policy = self.order_sizer.policy
            self.sizing_resolutions.append(
                {
                    "ts_ms": int(time.timestamp() * 1000),
                    "symbol": sym,
                    "policy_kind": policy.kind,
                    "policy_value": _describe_policy_value(policy),
                    "intended_qty": int(target_quantity),
                    "reference_price": str(price) if price is not None else None,
                    "sized_via": "policy_set_holdings",
                }
            )
        else:
            target_quantity = self.sizing_model.target_quantity(
                portfolio_value=self.total_value(),
                price=price,
                target_fraction=target_fraction,
                order_fee=self.order_fee,
            )
        delta = target_quantity - current_pos.quantity
        if delta == 0:
            # PRD §5A — a skip is not an intent. ``intent_id`` is minted
            # only AFTER this check, so a no-op never reserves an identity.
            # Phase 8 / VCR-0003 — durable SIZING_SKIP audit. The skip
            # joins the in-memory ``sizing_resolutions`` list (which the
            # Sizing card already reads). When ``sizing_skip_log_path``
            # is set, also append a JSON line so the audit survives a
            # restart even though the skip never minted an intent_id.
            self._append_sizing_skip(
                ts_ms=int(time.timestamp() * 1000),
                symbol=sym,
                policy_kind=(self.order_sizer.policy.kind if self.order_sizer is not None else "sizing_model"),
                policy_value=(_describe_policy_value(self.order_sizer.policy) if self.order_sizer is not None else ""),
                target_qty=int(target_quantity),
                current_qty=int(current_pos.quantity),
                reference_price=str(price) if price is not None else None,
                reason=(
                    "target_equals_current"
                    if target_quantity == current_pos.quantity != 0
                    else "zero_shares_while_flat"
                ),
            )
            return None
        order = self.submit_market_order(sym, delta, time, tag=tag or "SetHoldings")
        # Phase 5A — mint the intent_id only when the WAL surface is wired
        # (production path). Replay / legacy tests run without the WAL and
        # keep the prior behaviour: no minting, no WAL writes.
        if self.intent_wal is not None and self.bot_order_namespace:
            from app.engine.live.intent_events import IntentEventType, IntentKind
            from app.engine.live.order_identity import build_order_ref, mint_intent_id

            intent_id = mint_intent_id()
            self._intent_by_order_id[order.order_id] = intent_id
            self._last_minted_intent_id = intent_id

            # Phase 8 (ADR 0009 § 11) — append SIZING_RESOLVED to the WAL
            # immediately after the intent_id is minted and BEFORE the
            # PENDING_INTENT / SUBMITTED record that follows in the submit
            # path. Reuses the same intent_id so the per-trade Sizing card
            # can join SIZING_RESOLVED → submit → fill on intent_id alone.
            # SIZING_SKIP (no intent_id) is deferred — the IntentEvent model
            # currently requires non-empty intent_id + order_ref; emitting
            # the skip side would need a data-model relaxation that ripples
            # through ColdStartReconciler and the fold, scope-creep for the
            # WAL-emit half. Tracked as VCR-0003 follow-up.
            if self.order_sizer is not None:
                from app.engine.execution.order_sizer import (
                    default_sizing_provenance,
                )

                policy = self.order_sizer.policy
                self.intent_wal.append(
                    event_type=IntentEventType.SIZING_RESOLVED,
                    intent_id=intent_id,
                    bot_order_namespace=self.bot_order_namespace,
                    order_ref=build_order_ref(self.bot_order_namespace, intent_id),
                    intent_kind=IntentKind.STRATEGY,
                    order_id=order.order_id,
                    policy_kind=policy.kind,
                    policy_value=_describe_policy_value(policy),
                    intended_qty=int(target_quantity),
                    reference_price=str(price) if price is not None else None,
                    sizing_provenance_at_resolve_time=default_sizing_provenance(policy),
                    sized_via="policy_set_holdings",
                    symbol=sym,
                    ts_ms=int(time.timestamp() * 1000),
                )
        return order

    def liquidate(self, symbol: str, time: datetime) -> Order | None:
        pos = self.get_position(symbol)
        if pos.quantity == 0:
            return None
        return self.submit_market_order(symbol, -pos.quantity, time, tag="Liquidate")

    def drain_pending(self) -> Iterable[Order]:
        orders = list(self.pending_orders)
        self.pending_orders.clear()
        return orders

    def record_broker_fill(self, event: OrderEvent) -> None:
        """Update the local cache from a broker-reported fill event."""
        pos = self.get_position(event.symbol)
        new_qty = pos.quantity + event.fill_quantity
        if pos.quantity == 0 or (pos.quantity > 0) == (event.fill_quantity > 0):
            if new_qty != 0:
                pos.average_price = (
                    pos.average_price * Decimal(pos.quantity) + event.fill_price * Decimal(event.fill_quantity)
                ) / Decimal(new_qty)
        elif new_qty != 0 and (pos.quantity > 0) != (new_qty > 0):
            pos.average_price = event.fill_price
        pos.quantity = new_qty
        if pos.quantity == 0:
            pos.average_price = Decimal(0)
        self.cash -= Decimal(event.fill_quantity) * event.fill_price
        self.cash -= event.fee
        self.net_liquidation = Decimal(0)
        self.total_fees += event.fee

    async def submit_pending_orders(self) -> list[IbkrOrderAck]:
        """Submit all locally queued orders through the paper-order boundary.

        Phase 5B (ADR 0008 / VCR-0002) — on a *real-broker* adapter (one whose
        ``requires_durable_submit`` is ``True``), the WAL writes and the
        deterministic ``order_ref`` stamp are MANDATORY:

        * ``__post_init__`` has already guaranteed ``intent_wal`` and
          ``bot_order_namespace`` are set; no nullable guards needed here.
        * An order arriving without a minted ``intent_id`` is minted at this
          boundary so every broker order has an identity (covers strategies
          that called ``market_order`` / ``liquidate`` directly rather than
          ``set_holdings``). A future PR may tighten this to fail-fast.
        * A namespace-match assertion runs immediately before
          ``broker.place_order`` — belt-and-suspenders against stale
          ``order_ref``s from adoption / replay / future tooling.
        * The WAL surface is unconditional: ``PENDING_INTENT`` is fsynced
          BEFORE ``broker.place_order``, ``SUBMITTED`` after a clean ack,
          ``ACK_FAILED_UNCERTAIN`` if the call raises (the only honest event
          when a placement may or may not have landed).

        Shadow / no-submit adapters (``requires_durable_submit`` False or
        unset) retain the pre-Phase-5B opt-in behaviour: WAL writes happen
        iff an ``intent_wal`` was passed and an ``intent_id`` was minted
        upstream. Replay / explicit-surface tests are unaffected.
        """
        from app.engine.live.intent_events import IntentEventType
        from app.engine.live.order_identity import build_order_ref, mint_intent_id
        from app.engine.live.submit_state_machine import (
            RETRY_CAP,
            AckOutcome,
            BrokerProbe,
            SubmitVerdict,
            next_action,
        )
        from app.utils.timestamps import now_ms_utc

        requires_durable = bool(getattr(self.broker, "requires_durable_submit", False))

        if self.account_freeze_provider is not None:
            freeze_evidence = self.account_freeze_provider()  # type: ignore[operator]
            if freeze_evidence is not None and not self._pending_orders_reduce_exposure_only():
                self.drop_pending_before_submit(
                    drop_reason="account_freeze_block",
                    ts_ms=now_ms_utc(),
                )
                raise AccountFreezeBlockError(evidence=freeze_evidence)

        if self.account_registry_gate_provider is not None:
            registry_gate = self.account_registry_gate_provider()
            if registry_gate is not None and getattr(registry_gate, "status", None) != "pass":
                self.drop_pending_before_submit(
                    drop_reason="account_registry_block",
                    ts_ms=now_ms_utc(),
                )
                raise AccountRegistryBlockError(gate_result=registry_gate)

        account_truth_gate = None
        if self.account_truth_gate_provider is not None:
            account_truth_gate = self.account_truth_gate_provider()
        if self.account_observation_lease_gate_provider is not None:
            try:
                lease_gate = self.account_observation_lease_gate_provider()
            except Exception:
                lease_gate = None
                logger.exception("account observation lease shadow gate read failed")
            if (
                account_truth_gate is not None
                and lease_gate is not None
                and self.pending_orders
                and self.account_observation_lease_shadow_comparison_observer is not None
            ):
                try:
                    self.account_observation_lease_shadow_comparison_observer(
                        account_truth_gate,
                        lease_gate,
                    )
                except Exception:
                    logger.exception("account observation lease shadow comparison write failed")
            if (
                account_truth_gate is not None
                and lease_gate is not None
                and getattr(account_truth_gate, "status", None) != getattr(lease_gate, "status", None)
            ):
                shadow_key = (
                    f"truth={getattr(account_truth_gate, 'status', None)}:"
                    f"lease={getattr(lease_gate, 'status', None)}"
                )
                ACCOUNT_OBSERVATION_LEASE_SHADOW_DIVERGENCES[shadow_key] += 1
                logger.warning(
                    "account observation lease shadow divergence",
                    extra={
                        "truth_status": getattr(account_truth_gate, "status", None),
                        "truth_reason": getattr(account_truth_gate, "operator_reason", None),
                        "lease_status": getattr(lease_gate, "status", None),
                        "lease_reason": getattr(lease_gate, "operator_reason", None),
                        "divergence_count": ACCOUNT_OBSERVATION_LEASE_SHADOW_DIVERGENCES[
                            shadow_key
                        ],
                    },
                )
        if account_truth_gate is not None and getattr(account_truth_gate, "status", None) != "pass":
            self.drop_pending_before_submit(
                drop_reason="account_truth_block",
                ts_ms=now_ms_utc(),
            )
            raise AccountTruthBlockError(gate_result=account_truth_gate)

        session_state_for_submit: SessionAuthorityState | None = None
        if self.pending_orders and self.session_gate_provider is not None:
            session_state = self.session_gate_provider()
            if session_state is not None:
                session_state_for_submit = session_state
                extended_reference_price_ok = not any(
                    self.reference_price.get(order.symbol) is None
                    or self.reference_price[order.symbol] <= 0
                    for order in self.pending_orders
                )
                block_reason = evaluate_session_submit(
                    phase=session_state.phase,
                    allowed_sessions=self.allowed_sessions,
                    order_mechanism_sessions=self.order_mechanism_sessions,
                    extended_reference_price_ok=extended_reference_price_ok,
                )
                if block_reason is not None:
                    self.drop_pending_before_submit(
                        drop_reason="session_policy_block",
                        ts_ms=now_ms_utc(),
                    )
                    raise SessionPolicyBlockError(
                        session_state=session_state,
                        allowed_sessions=self.allowed_sessions,
                        order_mechanism_sessions=self.order_mechanism_sessions,
                        reason=block_reason,
                    )

        # Phase 7B / VCR-0010 — broker safety verdict gate. Consulted once at
        # the start of the submit pass (the verdict can't flip mid-call) so
        # the cost is a single getter call per bar, not per order. When the
        # verdict is set and != ``paper-only`` (and not ``None``), refuse the
        # entire pending batch and raise — the engine catches this and writes
        # halt.flag + durable PAUSED + ``BROKER_SAFETY_VERDICT_TRANSITION_HALT``
        # to the WAL.
        if self.verdict_provider is not None:
            verdict_value = self.verdict_provider()  # type: ignore[operator]
            if verdict_value is not None and verdict_value != "paper-only":
                self.drop_pending_before_submit(
                    drop_reason="broker_safety_halt",
                    ts_ms=now_ms_utc(),
                )
                raise BrokerSafetyVerdictBlockError(
                    verdict=str(verdict_value),
                    detail=(
                        "engine refuses to submit while broker safety verdict is not paper-only (PRD §7B order-block)"
                    ),
                )

        acks: list[IbkrOrderAck] = []
        for order in self.drain_pending():
            intent_id = self._intent_by_order_id.pop(order.order_id, None)
            order_ref: str | None = None
            extended_session = (
                session_state_for_submit is not None
                and session_state_for_submit.phase in ("PRE", "POST", "OVERNIGHT")
            )
            limit_price = float(self.reference_price[order.symbol]) if extended_session else None

            if self.account_owner_submitter is not None:
                if intent_id is None:
                    intent_id = mint_intent_id()
                    self._last_minted_intent_id = intent_id
                order_ref = build_order_ref(self.bot_order_namespace, intent_id)
            elif requires_durable:
                # __post_init__ guarantees intent_wal + namespace. Mint a
                # fallback intent_id for orders that bypassed set_holdings
                # (market_order / liquidate / engine-internal flatten paths)
                # so every real-broker order carries an identity.
                if intent_id is None:
                    intent_id = mint_intent_id()
                    self._last_minted_intent_id = intent_id
                order_ref = build_order_ref(self.bot_order_namespace, intent_id)
            elif self.intent_wal is not None and intent_id is not None:
                # Shadow / fake path opting into the WAL voluntarily.
                order_ref = build_order_ref(self.bot_order_namespace, intent_id)

            spec = IbkrOrderSpec(
                symbol=order.symbol,
                sec_type="STK",
                action="BUY" if order.quantity > 0 else "SELL",
                quantity=abs(order.quantity),
                order_type="LMT" if extended_session else "MKT",
                limit_price=limit_price,
                time_in_force="DAY",
                outside_rth=extended_session,
                confirm_paper=True,
                client_order_id=f"live-{order.order_id}",
                order_ref=order_ref,
            )

            if requires_durable:
                # Defense in depth — run BEFORE the WAL write so a malformed
                # spec never reaches durable storage. An ``order_ref`` that
                # doesn't match this instance's ``bot_order_namespace`` would
                # mis-attribute a real broker placement; catches stale tokens
                # leaking from adoption / replay / future tooling before they
                # reach IBKR (or the WAL). The boundary delimiter is ``:``
                # (the canonical separator); ``startswith`` here is safe
                # because the namespace is followed by exactly ``:`` (not the
                # broker-sourced side that CONTEXT.md flags).
                assert spec.order_ref is not None  # narrow for type-checker
                expected_prefix = self.bot_order_namespace + ":"
                assert spec.order_ref.startswith(expected_prefix), (
                    f"ADR 0008 namespace mismatch: spec.order_ref="
                    f"{spec.order_ref!r} does not start with {expected_prefix!r}"
                )

            if self.account_owner_submitter is not None:
                from app.engine.live.account_owner import AccountOwnerSubmitIntent, AccountOwnerSubmitRejected
                from app.utils.timestamps import now_ms_utc

                if not self.account_id or not self.strategy_instance_id or not self.run_id:
                    raise ValueError("AccountOwner mode requires account_id, strategy_instance_id, and run_id")
                if not self.bot_order_namespace:
                    raise ValueError("AccountOwner mode requires bot_order_namespace")
                if self.owner_generation_provider is None:
                    raise ValueError("AccountOwner mode requires owner_generation_provider")
                assert intent_id is not None and order_ref is not None
                trace_id = (
                    self.trace_id_provider()  # type: ignore[operator]
                    if self.trace_id_provider is not None
                    else intent_id
                )
                owner_generation = self.owner_generation_provider()  # type: ignore[operator]
                try:
                    result = await self.account_owner_submitter(  # type: ignore[operator]
                        AccountOwnerSubmitIntent(
                            trace_id=str(trace_id),
                            account_id=self.account_id,
                            strategy_instance_id=self.strategy_instance_id,
                            run_id=self.run_id,
                            bot_order_namespace=self.bot_order_namespace,
                            intent_id=intent_id,
                            order_ref=order_ref,
                            intent_kind="STRATEGY",
                            order_spec=spec.model_dump(),
                            owner_generation=int(owner_generation),
                            created_at_ms=now_ms_utc(),
                        )
                    )
                except AccountOwnerSubmitRejected as exc:
                    raise SubmitUncertainHaltError(
                        intent_id=intent_id,
                        order_ref=order_ref,
                        probe_result="rejected",
                        retry_count=0,
                        reason=exc.reason,
                    ) from exc
                if getattr(result, "status", None) != "accepted":
                    reason = str(getattr(result, "reason", None) or "AccountOwner submit was not accepted")
                    raise SubmitUncertainHaltError(
                        intent_id=intent_id,
                        order_ref=order_ref,
                        probe_result=str(getattr(result, "status", None) or "unknown"),
                        retry_count=0,
                        reason=reason,
                    )
                acks.append(
                    IbkrOrderAck(
                        account_id=self.account_id,
                        is_paper=True,
                        order_id=int(result.order_id),
                        perm_id=_try_int(getattr(result, "perm_id", None)),
                        client_id=0,
                        con_id=0,
                        symbol=spec.symbol,
                        action=spec.action,
                        quantity=spec.quantity,
                        order_type=spec.order_type,
                        limit_price=spec.limit_price,
                        status="Submitted",
                        placed_at_ms=now_ms_utc(),
                    )
                )
                continue

            wal_active = self.intent_wal is not None and intent_id is not None and order_ref is not None

            # Phase 5D / VCR-0002 — submit retry policy driven by
            # ``submit_state_machine.next_action``. Each attempt fsync's a
            # fresh ``PENDING_INTENT`` so a crash-during-retry still leaves
            # the same recoverable WAL fingerprint as a crash-during-first.
            # ``retry_count`` runs 0..RETRY_CAP; PROVABLY_ABSENT escalates
            # the count, NOT_PROVABLE halts immediately, PRESENT adopts.
            retry_count = 0
            ack: IbkrOrderAck | None = None
            while True:
                if wal_active:
                    assert self.intent_wal is not None  # narrow for type-checker
                    self.intent_wal.append(
                        event_type=IntentEventType.PENDING_INTENT,
                        intent_id=intent_id,
                        bot_order_namespace=self.bot_order_namespace,
                        order_ref=order_ref,
                        order_spec=spec.model_dump(),
                    )

                try:
                    ack = await self.broker.place_order(spec)
                    ack_outcome = AckOutcome.CLEAN_ACK
                    ack_reason: str | None = None
                except Exception as exc:
                    ack_outcome = AckOutcome.RAISED_OR_TIMEOUT
                    ack_reason = f"broker.place_order raised: {type(exc).__name__}: {exc}"

                # State machine: ack phase.
                ack_verdict = next_action(
                    current_status=IntentEventType.PENDING_INTENT,
                    ack_outcome=ack_outcome,
                )
                if ack_verdict is SubmitVerdict.RECORD_SUBMITTED:
                    assert ack is not None  # narrow
                    if wal_active:
                        assert self.intent_wal is not None
                        self.intent_wal.append(
                            event_type=IntentEventType.SUBMITTED,
                            intent_id=intent_id,
                            bot_order_namespace=self.bot_order_namespace,
                            order_ref=order_ref,
                            order_id=_try_int(getattr(ack, "order_id", None)),
                            perm_id=_try_int(getattr(ack, "perm_id", None)),
                        )
                    acks.append(ack)
                    break  # next order

                # ack_verdict == RECORD_ACK_FAILED_UNCERTAIN.
                if wal_active:
                    assert self.intent_wal is not None
                    self.intent_wal.append(
                        event_type=IntentEventType.ACK_FAILED_UNCERTAIN,
                        intent_id=intent_id,
                        bot_order_namespace=self.bot_order_namespace,
                        order_ref=order_ref,
                        reason=ack_reason,
                    )

                # Non-WAL (replay / shadow) paths preserve prior raise-on-fail
                # behaviour: there is no durable identity to retry against, so
                # the state machine has nothing to gate. The raised exception
                # propagates to the bar loop the same way it did before
                # Phase 5D.
                if not wal_active:
                    raise RuntimeError(ack_reason or "broker.place_order raised")

                # Probe phase. ``probe_intent_status`` is the I/O boundary
                # for "is the order present at the broker." The default
                # implementation returns NOT_PROVABLE so the state machine
                # halts on the first uncertain ack — a real-broker adapter
                # MUST override this to enable the RETRY_ONCE / RECOVER_ADOPT
                # paths (Phase 5C ownership-query subclass).
                # ``hasattr`` guard keeps legacy / replay fakes that pre-date
                # Phase 5D working without forcing every test broker to declare
                # the method; absence resolves to NOT_PROVABLE, which is the
                # safe halt-default.
                assert intent_id is not None and order_ref is not None
                probe_fn = getattr(self.broker, "probe_intent_status", None)
                if probe_fn is None:
                    probe_value = BrokerProbe.NOT_PROVABLE.value
                else:
                    probe_value = await probe_fn(intent_id, order_ref)
                try:
                    probe = BrokerProbe(probe_value)
                except ValueError as exc:
                    raise RuntimeError(
                        f"broker.probe_intent_status returned an invalid value "
                        f"{probe_value!r}; expected one of {list(BrokerProbe)}"
                    ) from exc

                probe_verdict = next_action(
                    current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
                    probe=probe,
                    retry_count=retry_count,
                )

                if probe_verdict is SubmitVerdict.RECOVER_ADOPT:
                    if wal_active:
                        assert self.intent_wal is not None
                        self.intent_wal.append(
                            event_type=IntentEventType.SUBMITTED_RECOVERED,
                            intent_id=intent_id,
                            bot_order_namespace=self.bot_order_namespace,
                            order_ref=order_ref,
                            reason=f"probe=PRESENT (retry_count={retry_count})",
                        )
                    # Adopted order: the broker has it; we have no synthesized
                    # ack to return because the original raise lost the ack.
                    # Downstream fill processing keys on order_ref/perm_id so
                    # the absence of an ack object is fine.
                    break

                if probe_verdict is SubmitVerdict.RETRY_ONCE:
                    if wal_active:
                        assert self.intent_wal is not None
                        self.intent_wal.append(
                            event_type=IntentEventType.INTENT_NOT_ACCEPTED,
                            intent_id=intent_id,
                            bot_order_namespace=self.bot_order_namespace,
                            order_ref=order_ref,
                            reason=f"probe=PROVABLY_ABSENT (retry_count={retry_count}); retrying with same intent_id",
                        )
                    retry_count += 1
                    if retry_count > RETRY_CAP:
                        # Belt-and-suspenders: next_action already enforces the
                        # cap, but a defensive check here removes any window
                        # where a loop bug could double-submit.
                        raise RuntimeError("submit retry exceeded RETRY_CAP — state-machine invariant violated")
                    continue  # next attempt: fresh PENDING_INTENT, same order_ref

                # probe_verdict is HALT. The WAL records the halt-class event
                # BEFORE the exception propagates so a crash between WAL and
                # engine cleanup still leaves the recovery-readable receipt.
                assert probe_verdict is SubmitVerdict.HALT
                halt_reason = (
                    f"submit state not provable after retry_count={retry_count} (probe={probe.value}); {ack_reason}"
                )
                if wal_active:
                    assert self.intent_wal is not None
                    self.intent_wal.append(
                        event_type=IntentEventType.SUBMIT_UNCERTAIN_HALTED,
                        intent_id=intent_id,
                        bot_order_namespace=self.bot_order_namespace,
                        order_ref=order_ref,
                        reason=halt_reason,
                    )
                raise SubmitUncertainHaltError(
                    intent_id=intent_id,
                    order_ref=order_ref,
                    probe_result=probe.value,
                    retry_count=retry_count,
                    reason=halt_reason,
                )
        return acks

    def _append_sizing_skip(
        self,
        *,
        ts_ms: int,
        symbol: str,
        policy_kind: str,
        policy_value: str,
        target_qty: int,
        current_qty: int,
        reference_price: str | None,
        reason: str,
    ) -> None:
        """Phase 8 / VCR-0003 — durable SIZING_SKIP audit entry.

        Per PRD §8: a skip carries no ``intent_id`` (it's not an intent).
        Annotates the most recent ``sizing_resolutions`` row (which the
        order_sizer branch already appended pre-delta) with the skip
        marker, then — when ``sizing_skip_log_path`` is set — fsyncs a
        JSON line to the durable log.
        """
        if self.sizing_resolutions:
            self.sizing_resolutions[-1]["skipped"] = True
            self.sizing_resolutions[-1]["skip_reason"] = reason
        else:
            # Non-policy path (legacy sizing_model) hasn't appended a row;
            # synthesize one so the in-memory list stays the single source
            # of truth for the Sizing card UI.
            self.sizing_resolutions.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "policy_kind": policy_kind,
                    "policy_value": policy_value,
                    "intended_qty": target_qty,
                    "reference_price": reference_price,
                    "sized_via": "sizing_model",
                    "skipped": True,
                    "skip_reason": reason,
                }
            )
        if self.sizing_skip_log_path is None:
            return
        _append_sizing_skip_line(
            self.sizing_skip_log_path,
            {
                "event_type": "SIZING_SKIP",
                "ts_ms_utc": ts_ms,
                "symbol": symbol,
                "policy_kind": policy_kind,
                "policy_value": policy_value,
                "target_qty": target_qty,
                "current_qty": current_qty,
                "reference_price": reference_price,
                "reason": reason,
            },
        )

    def last_minted_intent_id(self) -> str | None:
        """The most recent ``intent_id`` minted on a ``set_holdings`` submit.

        Surfaces the identity foundation for tests; production callers join
        through the WAL (``intent_events.jsonl``) instead.
        """
        return self._last_minted_intent_id

    def intent_id_for_order(self, order_id: int) -> str | None:
        """The ``intent_id`` reserved for the given pending order, if any.

        Returns the value while the order is still pending; once
        ``submit_pending_orders`` runs, the mapping is consumed and only
        the WAL retains the join.
        """
        return self._intent_by_order_id.get(order_id)

    def _pending_orders_reduce_exposure_only(self) -> bool:
        if not self.pending_orders:
            return False
        for order in self.pending_orders:
            position = self.get_position(order.symbol)
            if position.quantity == 0:
                return False
            if position.quantity > 0 and order.quantity >= 0:
                return False
            if position.quantity < 0 and order.quantity <= 0:
                return False
            if abs(order.quantity) > abs(position.quantity):
                return False
        return True
