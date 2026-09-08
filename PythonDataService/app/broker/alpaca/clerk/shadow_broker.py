"""The shadow world's ports: real live reads, synthesized fills, no submission.

ADR 0059 D2: a Shadow Account Authority is the real live read port bound to a
trade port that never submits. Three objects live here:

* ``ShadowOrderBook`` — the synthesized orders and their settlement. A
  regular-session leg fills at its decision bar's close (``decision_bar_close``,
  the same ``immediate_fill_price`` the sim world uses). An extended-session
  limit leg rests and settles on every read under ``limit_touch`` (D5.5)
  against the bars its own instance retained after the decision bar, and it
  cancels at the declared window's close for the decision's trading day — the
  instant the vendor would have cancelled a DAY extended order.
* ``NoSubmitAlpacaTradePort`` — ``BrokerTradePort`` over the book. It holds no
  Alpaca client; nothing in this module can reach the vendor's write API.
* ``ShadowAccountReadPort`` — ``BrokerReadPort``: account, clock, activities,
  assets, portfolio history and capabilities from the live read port;
  positions and orders from the book, so the reconciliation sweep reconciles
  the world this Clerk custodies (ruling R3).

Settlement is driven by reads, not by a task: the sweep's periodic order and
position reads are the clock. A resting order cancels only once one bucket
(the decision bar's own length) has elapsed past the declared close (ruling
R5), so the closing bucket has been retained before the book concludes the
order never touched.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    is_shadow_evidence_account_id,
    shadow_account_id_for_live_account,
    shadow_evidence_account_id_for_strategy,
)
from app.broker.alpaca.clerk.fill_models import limit_touch_fill
from app.broker.alpaca.clerk.sqlite.reconcile import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.writes import account_paths, confined_account_file
from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
)
from app.broker.alpaca.clerk.synthetic_broker import (
    filter_synthesized_orders,
    shape_immediate_order,
    synthesized_positions,
)
from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.ports import BrokerReadPort
from app.engine.live.order_identity import (
    NAMESPACE_ROOT,
    NAMESPACE_SEP,
    OrderRefParseError,
    build_bot_order_namespace,
    parse_order_ref,
)
from app.services.session_authority import declared_session_bounds
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

SHADOW_BROKER_ID = "shadow"
_RESTING = "new"
_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})


class ShadowFillBindingError(RuntimeError):
    """A shadow order cannot be synthesized: no bound decision bar, or no declared window."""


class ShadowNamespacePoisoned(RuntimeError):
    """The live account already holds orders the Clerk minted (ADR 0002 invariant 1)."""

    reason_code = "SHADOW_NAMESPACE_POISONED"

    def __init__(self, order_ids: Sequence[str]) -> None:
        self.order_ids = tuple(order_ids)
        super().__init__(
            f"the live account already holds {len(self.order_ids)} order(s) in the "
            f"Clerk's namespace: {', '.join(self.order_ids)}"
        )


class ShadowNamespaceUnproven(RuntimeError):
    """The order history read is at its page boundary; emptiness cannot be proven."""

    reason_code = "SHADOW_NAMESPACE_UNPROVEN"


class EvidenceLedgers:
    """Read handles on the per-instance evidence ledgers the shadow world settles against."""

    def __init__(self, artifacts_root: Path) -> None:
        self._root = artifacts_root
        self._ledgers: dict[str, SourceBarLedger] = {}
        self._lock = threading.Lock()

    def ledger(self, account_id: str) -> SourceBarLedger:
        if not is_shadow_evidence_account_id(account_id):
            raise SynthesizedBarBindingError(
                "Shadow fills settle only against a shadow-evidence: ledger."
            )
        with self._lock:
            ledger = self._ledgers.get(account_id)
            if ledger is None:
                ledger = SourceBarLedger(artifacts_root=self._root, account_id=account_id)
                self._ledgers[account_id] = ledger
            return ledger

    def verify(self, retained_bar: RetainedSourceBar) -> RetainedSourceBar:
        persisted = self.ledger(retained_bar.account_id).by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SynthesizedBarBindingError(
                "Shadow bar binding is not the exact retained source-bar observation."
            )
        return persisted

    def bars_after(
        self, account_id: str, *, provider: str, symbol: str, start_ms: int
    ) -> list[RetainedSourceBar]:
        return self.ledger(account_id).bars_after(provider=provider, symbol=symbol, start_ms=start_ms)

    def close(self) -> None:
        with self._lock:
            for ledger in self._ledgers.values():
                ledger.close(checkpoint=False)
            self._ledgers.clear()


def _fill_event(client_order_id: str, *, at_ms: int, price: float, quantity: float) -> BrokerOrderEvent:
    return BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=at_ms,
        price=price,
        quantity=quantity,
        execution_id=f"shadow-execution:{client_order_id}",
    )


class ShadowOrderBook:
    """Synthesized orders for one shadow authority, settled on every read."""

    def __init__(
        self,
        *,
        ledger: SynthesizedOrderLedger,
        evidence: EvidenceLedgers,
        window: ExtendedHoursWindow | None,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._ledger = ledger
        self._evidence = evidence
        self._window = window
        self._clock = clock

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        self._require_own_evidence(client_order_id, retained_bar)
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar)

    def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        # One book serves every instance on this authority, so the order's own
        # namespace is the only thing that says whose evidence may price it.
        self._evidence_namespace_for(client_order_id)
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            existing = self._ledger.find_order(records, client_order_id)
            if existing is not None:
                self._ledger.consume_bound_bar(client_order_id)
                return existing
            bound = self._ledger.consume_bound_bar(client_order_id)
            if bound is None:
                raise ShadowFillBindingError(
                    "Shadow execution requires the exact retained decision bar bound to this order; "
                    "nothing is ever priced from a later bar."
                )
            bar = self._ledger.verified_retained_bar(bound, symbol=leg.symbol)
            # Belt and braces over the bind-time check: the durable anchor can
            # never name another instance's evidence ledger.
            self._require_own_evidence(client_order_id, bar)
            order, anchor = (
                self._resting_order(leg, client_order_id=client_order_id, bar=bar)
                if leg.extended_hours
                else self._immediate_order(leg, client_order_id=client_order_id, bar=bar)
            )
            self._ledger.append_locked(records, order=order, leg=leg, anchor=anchor)
            return order

    def cancel(self, order_id: str) -> None:
        """Mark a resting synthesized order cancelled; a terminal one is left alone. Never a vendor call."""
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            record = next(
                (
                    row
                    for row in self._ledger.latest_records_from(records).values()
                    if row.order.order_id == order_id
                ),
                None,
            )
            if record is None or record.order.status in _TERMINAL:
                return
            now = self._clock()
            self._ledger.append_locked(
                records,
                order=record.order.model_copy(
                    update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                ),
                leg=record.leg,
                anchor=record.anchor,
            )

    def settle(self) -> None:
        with self._ledger.transaction() as records:
            self._settle_locked(records)

    def orders(self) -> list[BrokerOrder]:
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            return self._ledger.latest_orders_from_records(records)

    def order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next((order for order in self.orders() if order.client_order_id == client_order_id), None)

    def record(self, client_order_id: str) -> SynthesizedOrderRecord | None:
        """The durable record — order, leg and fill anchor — for one client order id."""
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            return self._ledger.find_record(records, client_order_id)

    def positions(self) -> list[BrokerPosition]:
        return synthesized_positions(SHADOW_BROKER_ID, self.orders())

    def close(self) -> None:
        self._evidence.close()

    @staticmethod
    def _evidence_namespace_for(client_order_id: str) -> str:
        """The evidence ledger the instance that minted this order retains into.

        A ``client_order_id`` on this authority is a program order ref —
        ``learn-ai/<instance>/v1:<intent>`` — and that instance name is the only
        statement of whose retained bars may price the order. A manual or
        emergency namespace has no instance evidence ledger and so cannot
        synthesize a fill here at all.
        """
        try:
            namespace, _intent_id = parse_order_ref(client_order_id)
            root, _, remainder = namespace.partition(NAMESPACE_SEP)
            strategy_instance_id = remainder.rpartition(NAMESPACE_SEP)[0]
            if root != NAMESPACE_ROOT or build_bot_order_namespace(strategy_instance_id) != namespace:
                raise OrderRefParseError(client_order_id, "not a program order namespace")
            return shadow_evidence_account_id_for_strategy(strategy_instance_id)
        except ValueError as error:
            raise ShadowFillBindingError(
                "only a program order synthesizes a fill on the shadow authority; "
                f"{client_order_id!r} is not one"
            ) from error

    @classmethod
    def _require_own_evidence(cls, client_order_id: str, bar: RetainedSourceBar) -> None:
        """Refuse a decision bar retained by any instance but this order's own."""
        expected = cls._evidence_namespace_for(client_order_id)
        if bar.account_id != expected:
            raise ShadowFillBindingError(
                f"the decision bar was retained by {bar.account_id!r}, not this order's evidence "
                f"namespace {expected!r}; a shadow fill is never priced from another "
                "instance's evidence"
            )

    def _immediate_order(
        self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar
    ) -> tuple[BrokerOrder, SynthesizedAnchor]:
        order = shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=bar,
            broker_id=SHADOW_BROKER_ID,
            id_prefix="shadow",
            observed_at_ms=self._clock(),
        )
        return order, SynthesizedAnchor(
            fill_model="decision_bar_close",
            evidence_account_id=bar.account_id,
            provider=bar.provider,
            bar_identity=bar.bar_identity,
            bar_ref=bar.bar_ref,
            decision_bar_start_ms=bar.start_ms,
            decision_bar_end_ms=bar.end_ms,
            fill_bar_ref=bar.bar_ref if order.status == "filled" else None,
        )

    def _resting_order(
        self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar
    ) -> tuple[BrokerOrder, SynthesizedAnchor]:
        if self._window is None:
            raise ShadowFillBindingError(
                "An extended-hours leg needs the broker's declared window to know when the vendor would cancel it."
            )
        bounds = declared_session_bounds(et_date_at_ms(bar.end_ms), self._window)
        if bounds is None:
            raise ShadowFillBindingError("The decision bar does not fall on a trading day.")
        at_ms = bar.end_ms
        order = BrokerOrder(
            broker=SHADOW_BROKER_ID,
            order_id=f"shadow-order:{client_order_id}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side,
            order_type=str(leg.order_type),
            time_in_force=str(leg.time_in_force),
            quantity=leg.quantity,
            filled_quantity=0.0,
            limit_price=leg.limit_price,
            stop_price=None,
            extended_hours=True,
            filled_avg_price=None,
            status=_RESTING,
            submitted_at_ms=at_ms,
            created_at_ms=at_ms,
            updated_at_ms=at_ms,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=self._clock(),
        )
        return order, SynthesizedAnchor(
            fill_model="limit_touch",
            evidence_account_id=bar.account_id,
            provider=bar.provider,
            bar_identity=bar.bar_identity,
            bar_ref=bar.bar_ref,
            decision_bar_start_ms=bar.start_ms,
            decision_bar_end_ms=bar.end_ms,
            cancel_at_ms=bounds.close_ms,
        )

    def _settle_locked(self, records: list[SynthesizedOrderRecord]) -> None:
        """Resolve every resting order the retained evidence can now decide (D5.5)."""
        now_ms = self._clock()
        for record in list(self._ledger.latest_records_from(records).values()):
            anchor, leg = record.anchor, record.leg
            if (
                record.order.status != _RESTING
                or anchor is None
                or leg is None
                or anchor.fill_model != "limit_touch"
                or anchor.cancel_at_ms is None
            ):
                continue
            client_order_id = record.order.client_order_id or record.order.order_id
            bars = self._evidence.bars_after(
                anchor.evidence_account_id,
                provider=anchor.provider,
                symbol=record.order.symbol,
                start_ms=anchor.decision_bar_end_ms,
            )
            fill = limit_touch_fill(
                leg,
                decision_bar_end_ms=anchor.decision_bar_end_ms,
                bars=bars,
                cancel_at_ms=anchor.cancel_at_ms,
            )
            if fill is not None:
                self._ledger.append_locked(
                    records,
                    order=record.order.model_copy(
                        update={
                            "status": "filled",
                            "filled_quantity": leg.quantity,
                            "filled_avg_price": float(fill.price),
                            "filled_at_ms": fill.filled_at_ms,
                            "updated_at_ms": fill.filled_at_ms,
                            "observed_at_ms": now_ms,
                            "events": [
                                _fill_event(
                                    client_order_id,
                                    at_ms=fill.filled_at_ms,
                                    price=float(fill.price),
                                    quantity=leg.quantity,
                                )
                            ],
                        }
                    ),
                    leg=leg,
                    anchor=anchor.model_copy(update={"fill_bar_ref": fill.bar_ref}),
                )
                continue
            bucket_ms = anchor.decision_bar_end_ms - anchor.decision_bar_start_ms
            if now_ms >= anchor.cancel_at_ms + bucket_ms:
                self._ledger.append_locked(
                    records,
                    order=record.order.model_copy(
                        update={
                            "status": "canceled",
                            "canceled_at_ms": anchor.cancel_at_ms,
                            "updated_at_ms": anchor.cancel_at_ms,
                            "observed_at_ms": now_ms,
                        }
                    ),
                    leg=leg,
                    anchor=anchor,
                )


class NoSubmitAlpacaTradePort:
    """``BrokerTradePort`` that synthesizes every order and never reaches Alpaca (ADR 0059 D2)."""

    broker_id = SHADOW_BROKER_ID

    def __init__(self, book: ShadowOrderBook) -> None:
        self._book = book

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        self._book.bind_evaluated_bar(client_order_id, retained_bar)

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return self._book.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        self._book.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._book.order_by_client_order_id(client_order_id)


class ShadowAccountReadPort:
    """``BrokerReadPort``: live account truth, synthesized custody (ruling R3)."""

    broker_id = SHADOW_BROKER_ID

    def __init__(self, *, live: BrokerReadPort, book: ShadowOrderBook) -> None:
        self._live = live
        self._book = book

    def capabilities(self) -> BrokerCapabilities:
        return self._live.capabilities()

    async def get_account(self) -> BrokerAccountSnapshot:
        return await self._live.get_account()

    async def list_positions(self) -> list[BrokerPosition]:
        return self._book.positions()

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(self._book.orders(), status=status, limit=limit, after_ms=after_ms)

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        return await self._live.list_activities(after_ms=after_ms, limit=limit)

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        return await self._live.list_assets(status=status, limit=limit)

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        return await self._live.get_asset(symbol)

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return await self._live.get_clock_evidence()

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        return await self._live.get_portfolio_history(history_range)


@dataclass(frozen=True)
class ShadowPorts:
    read: ShadowAccountReadPort
    trade: NoSubmitAlpacaTradePort
    book: ShadowOrderBook
    account_id: str


def compose_shadow_ports(
    *,
    live_read: BrokerReadPort,
    live_account_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
) -> ShadowPorts:
    """Bind one live read port into the shadow world for ``live_account_id``.

    The order WAL lives in the shadow custody directory
    (``accounts/alpaca/shadow:<live_account_id>/``, the same isolation the
    ``sim:`` namespace gets); settlement reads the per-instance
    ``shadow-evidence:`` ledgers.
    """
    account_id = shadow_account_id_for_live_account(live_account_id)
    _accounts_root, account_dir = account_paths(artifacts_root, account_id)
    account_dir.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceLedgers(artifacts_root)
    ledger = SynthesizedOrderLedger(
        account_id=account_id,
        path=confined_account_file(artifacts_root, account_id, SYNTHESIZED_ORDER_LEDGER_FILENAME),
        trusted_root=account_dir,
        verify_bar=evidence.verify,
        label="shadow_order",
    )
    book = ShadowOrderBook(
        ledger=ledger,
        evidence=evidence,
        window=live_read.capabilities().extended_hours_window,
        clock=clock,
    )
    return ShadowPorts(
        read=ShadowAccountReadPort(live=live_read, book=book),
        trade=NoSubmitAlpacaTradePort(book),
        book=book,
        account_id=account_id,
    )


def _clerk_minted(client_order_id: str | None) -> bool:
    if not client_order_id:
        return False
    try:
        parse_order_ref(client_order_id)
    except OrderRefParseError:
        return False
    return True


async def verify_shadow_namespace_empty(read: BrokerReadPort) -> None:
    """ADR 0002 invariant 1, transferred: the live account holds no Clerk-minted order, ever.

    Bounded by what the read port can see — the newest page of the whole
    history plus every open order. A full page proves nothing and refuses
    (``SHADOW_NAMESPACE_UNPROVEN``), the same posture the sweep takes at its
    own 500-row boundary; any Clerk-minted ``client_order_id`` is poisoned
    state (``SHADOW_NAMESPACE_POISONED``).

    The subject is the **live** account. Handed the shadow port instead, every
    category would answer from the synthesized book — empty at cold start — and
    the check would pass vacuously on a poisoned account, so it refuses.
    """
    if read.broker_id == SHADOW_BROKER_ID:
        raise ValueError("the shadow namespace check needs the live read port, not the shadow one")
    history = await read.list_orders(status="all", limit=MAX_OPEN_ORDER_SNAPSHOT)
    if len(history) >= MAX_OPEN_ORDER_SNAPSHOT:
        raise ShadowNamespaceUnproven(
            f"the live account's order history reached the {MAX_OPEN_ORDER_SNAPSHOT}-row page "
            "boundary; an empty Clerk namespace cannot be proven from one page"
        )
    open_orders = await read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT)
    owned = sorted(
        {order.order_id for order in (*history, *open_orders) if _clerk_minted(order.client_order_id)}
    )
    if owned:
        raise ShadowNamespacePoisoned(owned)


__all__ = [
    "SHADOW_BROKER_ID",
    "EvidenceLedgers",
    "NoSubmitAlpacaTradePort",
    "ShadowAccountReadPort",
    "ShadowFillBindingError",
    "ShadowNamespacePoisoned",
    "ShadowNamespaceUnproven",
    "ShadowOrderBook",
    "ShadowPorts",
    "compose_shadow_ports",
    "verify_shadow_namespace_empty",
]
