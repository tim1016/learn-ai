"""Ephemeral bot projection for Clerk-owned asynchronous custody.

The Account Clerk journal is the sole durable source of custody and broker
truth.  This component holds only the metadata a running bot needs to join an
A0 admission to a later Clerk-relayed callback; after a restart it reconstructs
the minimum safe fill metadata from an exact namespace-matching callback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.execution.order import Order
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    order_ref_namespace_matches,
    parse_order_ref,
)


class CustodyEntryPendingError(RuntimeError):
    """Non-terminal refusal while this strategy already has an A0 entry."""

    def __init__(self, *, intent_id: str, order_ref: str, reason: str) -> None:
        super().__init__(
            f"CustodyEntryPendingError(intent_id={intent_id!r} order_ref={order_ref!r} "
            f"reason={reason!r})"
        )
        self.intent_id = intent_id
        self.order_ref = order_ref
        self.reason = reason


@dataclass(frozen=True)
class CustodyPendingOrder:
    """Bot-local A0 admission; intentionally not a second account ledger."""

    order: Order
    intent_id: str
    order_ref: str
    journal_seq: int
    recorded_at_ms: int


@dataclass(frozen=True)
class CustodyCallbackMetadata:
    """Strategy metadata needed to project a later broker callback once."""

    symbol: str
    tag: str
    signed_qty: int
    submitted_at_ms: int
    evaluation_id: str | None
    intent_id: str | None
    order_ref: str
    perm_id: int | None = None


class CustodyProjection:
    """Join one bot's transient A0 state to its Clerk callback stream."""

    def __init__(self) -> None:
        self._admissions_by_order_id: dict[int, CustodyPendingOrder] = {}
        self._terminal_order_ids: set[int] = set()
        self._metadata_by_order_ref: dict[str, CustodyCallbackMetadata] = {}

    @property
    def pending_orders(self) -> tuple[CustodyPendingOrder, ...]:
        return tuple(
            pending
            for order_id, pending in sorted(self._admissions_by_order_id.items())
            if order_id not in self._terminal_order_ids
        )

    def record_a0_admission(
        self,
        *,
        order: Order,
        intent_id: str,
        order_ref: str,
        journal_seq: int,
        recorded_at_ms: int,
        current_lifecycle_is_terminal: bool,
    ) -> CustodyPendingOrder:
        pending = CustodyPendingOrder(
            order=order,
            intent_id=intent_id,
            order_ref=order_ref,
            journal_seq=journal_seq,
            recorded_at_ms=recorded_at_ms,
        )
        self._admissions_by_order_id[order.order_id] = pending
        if current_lifecycle_is_terminal:
            self._terminal_order_ids.add(order.order_id)
        return pending

    def capture_submission_metadata(
        self,
        *,
        order_id: int,
        evaluation_id: str | None,
    ) -> CustodyCallbackMetadata | None:
        """Attach strategy metadata after A0 without manufacturing broker facts."""

        pending = self._admissions_by_order_id.get(order_id)
        if pending is None:
            return None
        metadata = CustodyCallbackMetadata(
            symbol=pending.order.symbol,
            tag=pending.order.tag,
            signed_qty=int(pending.order.quantity),
            submitted_at_ms=pending.recorded_at_ms,
            evaluation_id=evaluation_id,
            intent_id=pending.intent_id,
            order_ref=pending.order_ref,
        )
        self._metadata_by_order_ref[pending.order_ref] = metadata
        return metadata

    def adopt_callback(
        self,
        event: IbkrOrderEvent,
        *,
        strategy_instance_id: str,
    ) -> CustodyCallbackMetadata | None:
        """Return trusted callback metadata, reconstructing only a owned fill."""

        if event.order_ref is None:
            return None
        self._resolve_terminal_callback(event)
        metadata = self._metadata_by_order_ref.get(event.order_ref)
        if metadata is None:
            metadata = self._reconstruct_after_restart(event, strategy_instance_id=strategy_instance_id)
            if metadata is None:
                return None
            self._metadata_by_order_ref[event.order_ref] = metadata
        if event.perm_id is not None and metadata.perm_id != event.perm_id:
            metadata = replace(metadata, perm_id=event.perm_id)
            self._metadata_by_order_ref[event.order_ref] = metadata
        return metadata

    def _resolve_terminal_callback(self, event: IbkrOrderEvent) -> None:
        assert event.order_ref is not None
        pending = next(
            (
                value
                for value in self._admissions_by_order_id.values()
                if value.order_ref == event.order_ref
            ),
            None,
        )
        if pending is None or not _is_terminal_callback(event):
            return
        self._terminal_order_ids.add(pending.order.order_id)

    @staticmethod
    def _reconstruct_after_restart(
        event: IbkrOrderEvent,
        *,
        strategy_instance_id: str,
    ) -> CustodyCallbackMetadata | None:
        if (
            event.event_type != "fill"
            or event.symbol is None
            or event.side is None
            or event.fill_quantity is None
            or not order_ref_namespace_matches(
                event.order_ref,
                {build_bot_order_namespace(strategy_instance_id)},
            )
        ):
            return None
        try:
            magnitude = abs(int(event.fill_quantity))
            _namespace, intent_id = parse_order_ref(event.order_ref)
        except (TypeError, ValueError):
            return None
        if magnitude == 0:
            return None
        return CustodyCallbackMetadata(
            symbol=event.symbol,
            tag="AccountClerkCustody:recovered",
            signed_qty=magnitude if event.side == "BUY" else -magnitude,
            submitted_at_ms=0,
            evaluation_id=None,
            intent_id=intent_id,
            order_ref=event.order_ref,
            perm_id=event.perm_id,
        )


def _is_terminal_callback(event: IbkrOrderEvent) -> bool:
    status = (event.status or "").lower()
    return event.event_type == "cancel" or status in {
        "filled",
        "cancelled",
        "apicancelled",
        "inactive",
        "rejected",
    }


__all__ = [
    "CustodyCallbackMetadata",
    "CustodyEntryPendingError",
    "CustodyPendingOrder",
    "CustodyProjection",
]
