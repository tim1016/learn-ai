"""The shadow world's ports: real live reads, synthesized fills, no submission.

ADR 0059 D2: a Shadow Account Authority is the real live read port bound to a
trade port that never submits. Three objects live here:

* ``ShadowOrderBook`` — the synthesized orders and their settlement. A
  regular-session leg fills at its decision bar's close (``decision_bar_close``,
  the same ``immediate_fill_price`` the sim world uses). A regular-session
  sale whose traded instrument had no bar for the decision's minute fills at
  the close of the newest earlier one, stamped when it was decided
  (``older_bar_close``, #2912). An extended-session
  limit leg rests and settles on read under ``limit_touch`` (D5.5) against
  the bars its own instance retained after the decision bar, and it
  cancels at the declared window's close for the decision's trading day — the
  instant the vendor would have cancelled a DAY extended order.
* ``NoSubmitAlpacaTradePort`` — ``BrokerTradePort`` over the book. It holds no
  Alpaca client; nothing in this module can reach the vendor's write API.
* ``ShadowAccountReadPort`` — ``BrokerReadPort``: account, clock, activities,
  assets, portfolio history and capabilities from the live read port;
  positions and orders from the book, so the reconciliation sweep reconciles
  the world this Clerk custodies (ruling R3).

An option order of one leg is filled as a Dry Run's simulation fills one
(ADR 0076 decision 25, ``simulated_option_orders``): at once, from the quote
the Clerk bound to it, into this same book. It is never priced from a bar,
and it never rests: a limit the quote does not reach ends the order on the
spot. The quotes are the ones kept for this shadow account, in its own
folder beside the book: an entry's as its order is sent, an exit
decision's, and the marks the Clerk's keeping states of every contract the
account's bots hold. They are the account's, not one bot's: one contract
has one market. An order of several legs is refused by name.

Settlement is driven by reads, not by a task: the sweep's periodic order and
position reads are the clock. A resting order cancels only once one bucket
(the decision bar's own length) has elapsed past the declared close (ruling
R5), so the closing bucket has been retained before the book concludes the
order never touched.

Three consequences of settling on read, written down so the next reader does
not rediscover them in production:

* A read is a durable write — a locked WAL append — **only while a resting
  order exists**. With nothing to settle it is a plain WAL read that takes no
  lock and appends nothing, so a request-path gate read costs a file read.
* The sweep's ``gather(list_orders, list_positions)`` therefore runs two
  independent settlement passes with two clock reads. That is safe in either
  argument order: settlement only ever moves an order forward, so whichever
  pass runs second observes a superset of what the first did.
* Once the slack window has elapsed, a cancel is **final**. A bar retained
  afterwards that would have touched the limit does not revive the order, so
  an evidence-retention stall lasting longer than one bucket past the declared
  close converts a would-be fill into a cancel. That determinism is the point
  of R5, and it is the residual to watch — it is the shape of the #1921
  feed-stall family.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.account_authority import (
    is_shadow_evidence_account_id,
    shadow_account_id_for_live_account,
    shadow_evidence_account_id_for_strategy,
)
from app.broker.alpaca.clerk.fill_models import limit_touch_fill
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.simulated_option_expiry import SimulatedOptionExpiry
from app.broker.alpaca.clerk.simulated_option_orders import SimulatedOptionOrders, contract_multipliers
from app.broker.alpaca.clerk.simulated_option_settlement import OwnersWord, SimulatedOptionSettlements
from app.broker.alpaca.clerk.sqlite.contract_terms import is_option_contract_symbol
from app.broker.alpaca.clerk.sqlite.reconcile import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.writes import account_paths, confined_account_file
from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    BoundBar,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOptionFill,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
    recovery_bar_in_send_session,
)
from app.broker.alpaca.clerk.synthetic_broker import (
    UnsupportedOptionOrderRefusal,
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
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    MultiLegOptionOrderRequest,
    OptionOrderRequest,
)
from app.broker.contract.ports import BrokerReadPort
from app.engine.live.order_identity import (
    NAMESPACE_ROOT,
    NAMESPACE_SEP,
    OrderRefParseError,
    build_bot_order_namespace,
    parse_order_ref,
)
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote
from app.services.session_authority import (
    declared_session_bounds,
    scheduled_extended_session_bounds,
)
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
    """Per-call read-only handles on the evidence ledgers the shadow world settles against.

    Every ledger here belongs to *another* authority — the instance that
    retained the bars — and is opened read-only for the duration of one read
    and then closed. Holding one would make this book a second, schema-creating
    writer on a live ledger and leave it attached when that instance shuts
    down, where a truncating checkpoint fails closed on a lingering reader.
    """

    def __init__(self, artifacts_root: Path) -> None:
        self._root = artifacts_root

    @contextmanager
    def open(self, account_id: str) -> Iterator[SourceBarLedger]:
        """Open one instance's evidence ledger read-only for exactly one read."""
        if not is_shadow_evidence_account_id(account_id):
            raise SynthesizedBarBindingError(
                "Shadow fills settle only against a shadow-evidence: ledger."
            )
        ledger = SourceBarLedger(
            artifacts_root=self._root, account_id=account_id, read_only=True
        )
        try:
            yield ledger
        finally:
            ledger.close(checkpoint=False)

    def verify(self, retained_bar: RetainedSourceBar) -> RetainedSourceBar:
        with self.open(retained_bar.account_id) as ledger:
            persisted = ledger.by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SynthesizedBarBindingError(
                "Shadow bar binding is not the exact retained source-bar observation."
            )
        return persisted

    def bars_after(
        self, account_id: str, *, provider: str, symbol: str, start_ms: int
    ) -> list[RetainedSourceBar]:
        with self.open(account_id) as ledger:
            return ledger.bars_after(provider=provider, symbol=symbol, start_ms=start_ms)

    def latest_for_symbol(self, account_id: str, *, symbol: str) -> RetainedSourceBar | None:
        """Return the newest durable bar from one instance's evidence namespace."""
        with self.open(account_id) as ledger:
            return ledger.latest_for_symbol(symbol)


class _Settlable(NamedTuple):
    """One resting order's settlement inputs, narrowed once for the whole pass."""

    leg: BrokerOrderLeg
    anchor: SynthesizedAnchor
    cancel_at_ms: int


def _settlable(record: SynthesizedOrderRecord) -> _Settlable | None:
    """The settlement inputs of a resting order the evidence can still decide.

    ``None`` for anything a settlement pass would leave alone — a terminal
    order, an immediate-fill anchor, or a record written before legs and
    anchors were durable. One answer serves both callers: whether to take the
    transaction at all, and whether to act on this record inside it.
    """
    anchor, leg = record.anchor, record.leg
    if (
        record.order.status != _RESTING
        or anchor is None
        or leg is None
        or anchor.fill_model != "limit_touch"
        or anchor.cancel_at_ms is None
    ):
        return None
    return _Settlable(leg=leg, anchor=anchor, cancel_at_ms=anchor.cancel_at_ms)


def _fill_event(client_order_id: str, *, at_ms: int, price: float, quantity: float) -> BrokerOrderEvent:
    return BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=at_ms,
        price=price,
        quantity=quantity,
        execution_id=f"shadow-execution:{client_order_id}",
    )


class ShadowOrderBook:
    """Synthesized orders for one shadow authority, settled on read while any rests."""

    def __init__(
        self,
        *,
        ledger: SynthesizedOrderLedger,
        evidence: EvidenceLedgers,
        window: ExtendedHoursWindow | None,
        option_orders: SimulatedOptionOrders,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._ledger = ledger
        self._evidence = evidence
        self._window = window
        self._clock = clock
        # The book's option orders of one leg: filled from a quote into this same ledger (ADR 0076).
        self.option_orders = option_orders

    def bind_evaluated_bar(
        self, client_order_id: str, retained_bar: RetainedSourceBar, *, fill_at_ms: int | None = None
    ) -> None:
        self._require_own_evidence(client_order_id, retained_bar)
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar, fill_at_ms=fill_at_ms)

    def bind_latest_recovery_bar(self, client_order_id: str, *, symbol: str, side: str | None = None) -> bool:
        """Bind a recovery EXIT to the latest bar its own instance retained.

        Strategy decisions always bind their exact deciding bar before submit.
        Recovery EXITs have no strategy decision bar, so their distinct durable
        decision identity explicitly selects the newest retained observation
        from the order namespace that owns the exposure. ``False`` keeps the
        recovery effect uncertain when no such evidence exists. A bar prices
        either side alike, so ``side`` is not consulted.
        """
        account_id = self._evidence_namespace_for(client_order_id)
        retained_bar = self._evidence.latest_for_symbol(account_id, symbol=symbol)
        if not recovery_bar_in_send_session(retained_bar, now_ms=self._clock(), extended_window=self._window):
            return False
        self.bind_evaluated_bar(client_order_id, retained_bar)
        return True

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
            bound = self._ledger.consume_binding(client_order_id)
            if bound is None:
                raise ShadowFillBindingError(
                    "Shadow execution requires the exact retained decision bar bound to this order; "
                    "nothing is ever priced from a later bar."
                )
            bar = self._ledger.verified_retained_bar(bound.bar, symbol=leg.symbol)
            # Belt and braces over the bind-time check: the durable anchor can
            # never name another instance's evidence ledger.
            self._require_own_evidence(client_order_id, bar)
            if leg.extended_hours and bound.older_fill_at_ms is not None:
                # The Clerk refuses this before it reaches a port
                # (``_PricingBarBoundTradePort``); a caller that got here is
                # outside it.
                raise ShadowFillBindingError(
                    "A resting shadow order is priced from its own decision bar, never an older one."
                )
            order, anchor = (
                self._resting_order(leg, client_order_id=client_order_id, bar=bar)
                if leg.extended_hours
                else self._immediate_order(leg, client_order_id=client_order_id, priced_by=bound._replace(bar=bar))
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
                anchor=self._canceled_anchor(record, now),
                option=record.option,
            )

    def _canceled_anchor(self, record: SynthesizedOrderRecord, at_ms: int) -> SynthesizedAnchor | None:
        anchor = record.anchor
        if anchor is None or anchor.fill_model != "limit_touch":
            return anchor
        bars = self._evidence.bars_after(
            anchor.evidence_account_id, provider=anchor.provider, symbol=record.order.symbol,
            start_ms=anchor.decision_bar_end_ms,
        )
        end_ms = min(at_ms, anchor.cancel_at_ms) if anchor.cancel_at_ms is not None else at_ms
        witnessed = _execution_window_witnessed(bars, start_ms=anchor.decision_bar_end_ms, end_ms=end_ms)
        return anchor.model_copy(update={"unfilled_reason": "untouched" if witnessed else "no_evidence"})

    def orders(self) -> list[BrokerOrder]:
        return [record.order for record in self._settled_records().values()]

    def order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next((order for order in self.orders() if order.client_order_id == client_order_id), None)

    def order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        return next((order for order in self.orders() if order.order_id == order_id), None)

    def fill_anchors(self) -> Mapping[str, SynthesizedAnchor]:
        """How each filled order was priced, read as the book was left: nothing is settled (#2912)."""
        return self._ledger.fill_anchors()

    def record(self, client_order_id: str) -> SynthesizedOrderRecord | None:
        """The durable record — order, leg and fill anchor — for one client order id."""
        return next(
            (
                record
                for record in self._settled_records().values()
                if record.order.client_order_id == client_order_id
            ),
            None,
        )

    def _settled_records(self) -> dict[str, SynthesizedOrderRecord]:
        """The latest record per order, settled first when anything can settle.

        The read is a plain WAL read until a resting order exists; only then
        does it take the transaction — the process lock plus the cross-process
        advisory lock — and possibly append. A gate or a sweep reading a book
        that holds nothing but terminal orders pays neither.
        """
        records = self._ledger.latest_records()
        if not any(_settlable(record) is not None for record in records.values()):
            return records
        with self._ledger.transaction() as rows:
            self._settle_locked(rows)
            return self._ledger.latest_records_from(rows)

    def positions(self) -> list[BrokerPosition]:
        # A position in an option contract is stated in dollars by the multiplier its opening order kept.
        records = list(self._settled_records().values())
        return synthesized_positions(
            SHADOW_BROKER_ID, [record.order for record in records], contract_multipliers=contract_multipliers(records)
        )

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
        self, leg: BrokerOrderLeg, *, client_order_id: str, priced_by: BoundBar
    ) -> tuple[BrokerOrder, SynthesizedAnchor]:
        bar = priced_by.bar
        order = shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=bar,
            broker_id=SHADOW_BROKER_ID,
            id_prefix="shadow",
            observed_at_ms=self._clock(),
            fill_at_ms=priced_by.older_fill_at_ms,
        )
        return order, SynthesizedAnchor(
            fill_model=priced_by.fill_model,
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
        if not bounds.open_ms < bar.end_ms < bounds.close_ms:
            # Outside the window the record is incoherent: an order submitted at
            # or after the instant it is recorded as cancelled, which no later
            # bar can ever fill.
            raise ShadowFillBindingError(
                f"The decision bar closes at {bar.end_ms} ms, outside the declared window "
                f"{bounds.open_ms}-{bounds.close_ms} ms; no resting order can be synthesized from it."
            )
        scheduled = scheduled_extended_session_bounds(et_date_at_ms(bar.end_ms))
        cancel_at_ms = min(bounds.close_ms, scheduled.close_ms) if scheduled is not None else bounds.close_ms
        if bar.end_ms >= cancel_at_ms:
            raise ShadowFillBindingError("The decision bar closes after the day's after-hours session ended.")
        at_ms = bar.end_ms
        # Shares ``shape_immediate_order``'s ``BrokerOrder`` field list; only the
        # outcome half (status, fills, events) differs. A field added to the
        # contract model needs both. Deliberately not extracted: a shaper taking
        # every differing field as a keyword is longer than the two call sites.
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
            cancel_at_ms=cancel_at_ms,
        )

    def _settle_locked(self, records: list[SynthesizedOrderRecord]) -> None:
        """Resolve every resting order the retained evidence can now decide (D5.5)."""
        now_ms = self._clock()
        for record in list(self._ledger.latest_records_from(records).values()):
            settlable = _settlable(record)
            if settlable is None:
                continue
            leg, anchor, cancel_at_ms = settlable
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
                cancel_at_ms=cancel_at_ms,
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
            if now_ms >= cancel_at_ms + bucket_ms:
                self._ledger.append_locked(
                    records,
                    order=record.order.model_copy(
                        update={
                            "status": "canceled",
                            "canceled_at_ms": cancel_at_ms,
                            "updated_at_ms": cancel_at_ms,
                            "observed_at_ms": now_ms,
                        }
                    ),
                    leg=leg,
                    anchor=anchor.model_copy(update={"unfilled_reason": (
                        "untouched" if _execution_window_witnessed(
                            bars, start_ms=anchor.decision_bar_end_ms, end_ms=cancel_at_ms,
                        ) else "no_evidence"
                    )}),
                )


def _execution_window_witnessed(bars: list[RetainedSourceBar], *, start_ms: int, end_ms: int) -> bool:
    """Untouched is proven only by continuous retained bars through cancellation."""
    covered_until = start_ms
    for bar in sorted(bars, key=lambda item: item.start_ms):
        if bar.end_ms <= covered_until:
            continue
        if bar.start_ms > covered_until or bar.end_ms > end_ms:
            break
        covered_until = bar.end_ms
    return covered_until >= end_ms and end_ms > start_ms


class _ShadowSettlementBars:
    """Market bars retained by the bots whose option orders belong to this shadow book."""

    def __init__(self, book: ShadowOrderBook) -> None:
        self._book = book

    def latest_for_symbol(self, symbol: str, *, market_only: bool, at_or_before_ms: int) -> RetainedSourceBar | None:
        records = self._book._ledger.latest_records().values()
        accounts = {
            self._book._evidence_namespace_for(record.order.client_order_id)
            for record in records
            if record.option is not None and record.option.request.contract.root == symbol
            and record.order.client_order_id is not None
        }
        bars = []
        for account_id in sorted(accounts):
            try:
                with self._book._evidence.open(account_id) as ledger:
                    bar = ledger.latest_for_symbol(symbol, market_only=market_only, at_or_before_ms=at_or_before_ms)
            except FileNotFoundError:
                logging.getLogger(__name__).warning(
                    "shadow settlement has no retained market ledger",
                    extra={"action": "shadow_settlement_ledger_missing", "account_id": account_id, "symbol": symbol},
                )
                continue
            if bar is not None:
                bars.append(bar)
        if not bars:
            return None
        latest_ms = max(bar.end_ms for bar in bars)
        latest = [bar for bar in bars if bar.end_ms == latest_ms]
        if len({bar.close for bar in latest}) != 1:
            raise ShadowBookUnreadable(f"Shadow settlement has conflicting closing prices for {symbol}.")
        return min(latest, key=lambda bar: bar.bar_ref)


class NoSubmitAlpacaTradePort(UnsupportedOptionOrderRefusal):
    """``BrokerTradePort`` that synthesizes every order and never reaches Alpaca (ADR 0059 D2).

    It takes an option order of one leg, so it answers that and the two
    option lookups itself, from the book's own. An order of several legs it
    refuses in the words every simulated port refuses option orders in
    (``UnsupportedOptionOrderRefusal``).
    """

    broker_id = SHADOW_BROKER_ID
    _named_in_option_refusals = "The shadow order book"

    def __init__(self, book: ShadowOrderBook) -> None:
        self._book = book
        self._options = book.option_orders
        self._expiry = SimulatedOptionExpiry(
            account_id=book._ledger.account_id, broker_id=self.broker_id, ledger=book._ledger,
            source_bars=_ShadowSettlementBars(book), clock=book._clock,
        )

    def settle_expired_options(
        self, *, after_ms: int, at_the_owners_word: Mapping[str, OwnersWord] | None = None,
    ) -> SimulatedOptionSettlements:
        return self._expiry.settle_expired_options(after_ms=after_ms, at_the_owners_word=at_the_owners_word)

    @property
    def submission_response_is_authoritative_evidence(self) -> bool:
        return True

    def bind_evaluated_bar(
        self, client_order_id: str, retained_bar: RetainedSourceBar, *, fill_at_ms: int | None = None
    ) -> None:
        self._book.bind_evaluated_bar(client_order_id, retained_bar, fill_at_ms=fill_at_ms)

    def bind_latest_recovery_bar(self, client_order_id: str, *, symbol: str, side: str | None = None) -> bool:
        return self._book.bind_latest_recovery_bar(client_order_id, symbol=symbol, side=side)

    def synthesized_fill_anchors(self) -> Mapping[str, SynthesizedAnchor]:
        return self._book.fill_anchors()

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return self._book.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        self._book.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._book.order_by_client_order_id(client_order_id)

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        return self._book.order_by_broker_order_id(order_id)

    # ── option orders of one leg (ADR 0076 decision 25) ─────────────────

    def bind_option_entry_quote(
        self, client_order_id: str, *, symbol: str, quote: HeldQuote, multiplier: int
    ) -> None:
        """Bind an opening order to the one quote its limit price was worked out from, and keep that quote."""
        self._options.bind_entry_quote(client_order_id, symbol=symbol, quote=quote, multiplier=multiplier)

    def bind_option_close_quotes(
        self,
        client_order_id: str,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        quotes: Sequence[HeldQuote],
        usable_generation: int | None,
    ) -> bool:
        """Bind a closing order to the newest quote kept for its contract; ``False`` leaves the close unsent."""
        return self._options.bind_close_quotes(
            client_order_id, symbol=symbol, side=side, quotes=quotes, usable_generation=usable_generation
        )

    def recovery_price_available(self, symbol: str, *, side: Literal["buy", "sell"] = "sell") -> bool:
        """Whether a recovery EXIT for ``symbol`` sent now has a price to fill at.

        For a position in an option contract: only when the newest quote
        kept for it has something on the side the close fills at, so an
        owner is refused a Flatten up front that the book could not price.
        For shares it always answers yes, as it did when it was never asked:
        the book binds the newest bar the bot's own instance retained when
        the reduction is sent, and says then when it has none.
        """
        contract_symbol = symbol.upper()
        if is_option_contract_symbol(contract_symbol):
            return self._options.prices_a_close(contract_symbol, side=side)
        return True

    def synthesized_option_fills(self) -> Mapping[str, SynthesizedOptionFill]:
        """How the book priced each option order it answered, by client order id."""
        return self._options.fills()

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder:
        """Fill an option order of one leg from the quote bound to it, or end it unfilled. Never a vendor call."""
        if isinstance(order, MultiLegOptionOrderRequest):
            raise self._option_orders_not_taken("takes no option orders of several legs")
        return self._options.submit(order, client_order_id=client_order_id)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        return self._options.order_where(lambda record: record.order.client_order_id == client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        return self._options.order_where(lambda record: record.order.order_id == order_id)


class ShadowAccountReadPort:
    """``BrokerReadPort``: live account truth, synthesized custody (ruling R3)."""

    broker_id = SHADOW_BROKER_ID

    def __init__(self, *, live: BrokerReadPort, book: ShadowOrderBook) -> None:
        self._live = live
        self._book = book

    def capabilities(self) -> BrokerCapabilities:
        # The live broker's own, less what the shadow order book does not do:
        # it fills an option order of one leg and takes none of several (#2913).
        return self._live.capabilities().revised(supports_multi_leg_option_orders=False)

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
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(
            self._book.orders(), status=status, limit=limit, after_ms=after_ms,
            symbols=symbols, until_ms=until_ms,
        )

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]:
        activity_filter = (
            {} if activity_type is None else {"activity_type": activity_type}
        )
        return await self._live.list_activities(
            after_ms=after_ms, limit=limit, **activity_filter
        )

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        """None, and that is all of them: positions here are the shadow book's, and no broker ends them.

        The live account's own assignments, exercises and expiries end
        positions the live account holds. They are not passed through as
        events of a book that never held those contracts. A contract the
        book itself holds past its expiry is settled by the trade port's
        separate simulated-expiry step, as on a Dry Run (ADR 0076 D22).
        """
        del after_ms, resume
        return BrokerOptionEventEvidence(events=(), share_trades=(), history_complete=True)

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        return await self._live.list_assets(status=status, limit=limit)

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        return await self._live.get_asset(symbol)

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        return await self._live.get_option_contract(contract)

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        return await self._live.get_option_account()

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
    ledger = _order_ledger(artifacts_root, account_id, evidence)
    book = ShadowOrderBook(
        ledger=ledger,
        evidence=evidence,
        window=live_read.capabilities().extended_hours_window,
        option_orders=SimulatedOptionOrders(
            account_id=account_id,
            ledger=ledger,
            # The shadow account's own, beside its order book: what its Clerk's keeping writes and
            # its simulated account reading marks from.
            kept=KeptOptionQuotes.of_account(artifacts_root=artifacts_root, account_id=account_id),
            clock=clock,
            broker_id=SHADOW_BROKER_ID,
            id_prefix="shadow",
        ),
        clock=clock,
    )
    return ShadowPorts(
        read=ShadowAccountReadPort(live=live_read, book=book),
        trade=NoSubmitAlpacaTradePort(book),
        book=book,
        account_id=account_id,
    )


def _order_ledger(
    artifacts_root: Path, account_id: str, evidence: EvidenceLedgers
) -> SynthesizedOrderLedger:
    """One ``shadow:`` authority's order WAL, in its own custody directory."""
    _accounts_root, account_dir = account_paths(artifacts_root, account_id)
    return SynthesizedOrderLedger(
        account_id=account_id,
        path=confined_account_file(artifacts_root, account_id, SYNTHESIZED_ORDER_LEDGER_FILENAME),
        trusted_root=account_dir,
        verify_bar=evidence.verify,
        label="shadow_order",
    )


class ShadowBookUnreadable(RuntimeError):
    """A ``shadow:`` order book cannot be read, so it is no witness to what it holds."""


def open_book_orders(
    *, artifacts_root: Path, account_id: str, strategy_instance_id: str
) -> tuple[BrokerOrder, ...]:
    """The orders one bot still has open in a ``shadow:`` book, read as the book was left (#2694).

    A plain read: nothing is settled, so a graduated account's book is never
    written again. An open order whose id names no minting bot is counted
    against every bot, since nobody can say whose it is. A book that was
    never written holds none.
    """
    ledger = _order_ledger(artifacts_root, account_id, EvidenceLedgers(artifacts_root))
    try:
        orders = ledger.latest_orders()
    except (RuntimeError, OSError, ValueError) as error:
        raise ShadowBookUnreadable(f"the order book of {account_id!r} cannot be read: {error}") from error
    own = build_bot_order_namespace(strategy_instance_id)
    return tuple(
        order
        for order in orders
        if order.status not in _TERMINAL and _minting_namespace(order.client_order_id) in (own, None)
    )


def _minting_namespace(client_order_id: str | None) -> str | None:
    """The order namespace that minted ``client_order_id``; ``None`` when it names none."""
    if not client_order_id:
        return None
    try:
        return parse_order_ref(client_order_id)[0]
    except OrderRefParseError:
        return None


def _clerk_minted(client_order_id: str | None) -> bool:
    return _minting_namespace(client_order_id) is not None


async def verify_shadow_namespace_empty(read: BrokerReadPort) -> None:
    """ADR 0002 invariant 1, transferred: the live account holds no Clerk-minted order, ever.

    Bounded by what the read port can see — the newest page of the whole
    history plus the newest page of open orders, both bounded by
    ``MAX_OPEN_ORDER_SNAPSHOT``. A full *history* page proves nothing and
    refuses (``SHADOW_NAMESPACE_UNPROVEN``), the same posture the sweep takes
    at its own 500-row boundary; any Clerk-minted ``client_order_id`` in
    either read is poisoned state (``SHADOW_NAMESPACE_POISONED``).

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
    "ShadowBookUnreadable",
    "ShadowFillBindingError",
    "ShadowNamespacePoisoned",
    "ShadowNamespaceUnproven",
    "ShadowOrderBook",
    "ShadowPorts",
    "compose_shadow_ports",
    "open_book_orders",
    "verify_shadow_namespace_empty",
]
