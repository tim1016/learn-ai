"""Fill attribution for the broker-v2 panel (S0).

``project_instance_fills(sid, entries)`` projects a bot's fills from the
Alpaca order journal filtered by the bot's canonical namespace
``learn-ai/{sid}/v1``.  It never accounts-nets — only fills whose
``order_ref`` belongs to exactly ``learn-ai/{sid}/v1`` are returned.

The function is the single authority that feeds:

- The FIFO P&L engine (``fifo_pnl.py``).
- The trades list on the trader-lens panel.
- The fill markers on the chart pane.

Dedup rule mirrors ``exposure.py``: each ``(account_id, event_key)`` is
counted exactly once; a redelivery with the same event_key is idempotent
(the broker may redeliver from the ``trade_updates`` stream).

``FillRecord`` carries an optional ``fee`` field.  When the journal entry
carries no commission information the field is ``None`` — callers must
render that as "Fees not reported", never ``$0.00``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import OrderSide
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    parse_order_ref,
)


@dataclass(frozen=True)
class FillRecord:
    """One attributed fill for a specific bot namespace.

    ``fee`` is ``None`` when the journal carries no commission data for this
    fill — callers must render that as "Fees not reported", never ``$0.00``.
    All timestamps are ``int64 ms UTC``.
    """

    account_id: str
    sid: str
    intent_id: str
    order_ref: str
    event_key: str
    symbol: str
    side: OrderSide
    quantity: float  # positive share count
    fill_price: float
    filled_at_ms: int  # int64 ms UTC
    fee: float | None  # None = "not reported"


def project_instance_fills(
    sid: str,
    entries: Iterable[OrderJournalEntry],
) -> tuple[FillRecord, ...]:
    """Project the fills owned by bot ``sid`` from the Alpaca order journal.

    Only entries whose ``order_ref`` resolves to namespace
    ``learn-ai/{sid}/v1`` (exact structural match per ADR 0008 §1) are
    included.  Each ``(account_id, event_key)`` is deduped — a re-delivered
    fill event is absorbed idempotently (temporal-rigor live-subscription
    relaxation).

    ``entries`` is consumed once; pass ``journal.read_all()`` for the full
    history or a pre-filtered slice for bounded reads.

    Returns fills sorted ascending by ``filled_at_ms`` so callers can build
    the trades list and the FIFO lot stack in one pass.
    """
    if not sid:
        raise ValueError("sid must be non-empty")
    target_namespace = build_bot_order_namespace(sid)
    seen: set[tuple[str, str]] = set()
    out: list[FillRecord] = []

    for entry in entries:
        if entry.kind is not ClerkEntryKind.ORDER_EVENT or entry.owned is not True:
            continue
        event = entry.event
        # "partial_fill" is intentionally included here (differs from exposure.py
        # which only accepts "fill").  Partial fills are real executions that
        # must be credited to P&L and the trades list; exposure.py omits them
        # because partial fills may be followed by a completing "fill" event
        # carrying the same event_key — the dedup below handles both.
        if event is None or event.event_type not in ("fill", "partial_fill"):
            continue
        if event.quantity is None or event.price is None:
            continue
        if not entry.event_key:
            continue
        leg = entry.leg
        if leg is None or not entry.order_ref:
            continue
        try:
            entry_namespace, intent_id = parse_order_ref(entry.order_ref)
        except ValueError:
            continue
        if entry_namespace != target_namespace:
            continue
        dedup_key = (entry.account_id, entry.event_key)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(
            FillRecord(
                account_id=entry.account_id,
                sid=sid,
                intent_id=intent_id,
                order_ref=entry.order_ref,
                event_key=entry.event_key,
                symbol=leg.symbol,
                side=leg.side,
                quantity=event.quantity,
                fill_price=event.price,
                filled_at_ms=event.occurred_at_ms,
                fee=None,  # Alpaca does not report per-fill commission in trade_updates
            )
        )

    out.sort(key=lambda r: r.filled_at_ms)
    return tuple(out)
