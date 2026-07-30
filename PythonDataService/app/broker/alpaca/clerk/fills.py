"""Fill attribution for the broker-v2 panel (S0).

``normalize_fill_event`` is the CANONICAL normalized-fill fold gate for the
broker-v2 panel.  All consumers that need fills from the Alpaca order journal
route through this single function.  Attribution, exposure, trades, P&L, and
the rollup cache all use ``FillRecord`` objects produced by this gate.

``project_instance_fills(sid, entries)`` projects a bot's fills from the
Alpaca order journal filtered by the bot's canonical namespace
``learn-ai/{sid}/v1``.  It never accounts-nets — only fills whose
``order_ref`` belongs to exactly ``learn-ai/{sid}/v1`` are returned.

The function is the single authority that feeds:

- The FIFO P&L engine (``fifo_pnl.py``).
- The trades list on the trader-lens panel.
- The fill markers on the chart pane.
- The rollup cache (``rollup_cache.py``).

Dedup rule: each ``(account_id, event_key)`` is counted exactly once; a
re-delivery with the same event_key is absorbed idempotently (temporal-rigor
live-subscription relaxation).

Canonical normalized-fill fold gate (``normalize_fill_event``):
    Formula: fill attribution = {owned ORDER_EVENT entries where event_type in
        {"fill", "partial_fill"} AND event_key non-empty AND leg/order_ref
        non-None}, deduplicated on (account_id, event_key).
    Reference: Alpaca trade_updates stream event taxonomy; temporal-rigor.md
        §"Finite ingestion vs. live subscriptions" (idempotent redelivery).
    Canonical implementation: this file (normalize_fill_event).
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_fills.py and
        PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py (golden
        fixture scenarios "duplicate_delivery" and "partial_fill_sequence").

Partial-fill semantics: ``"partial_fill"`` events are real broker executions
that MUST be credited to P&L and the trades list.  The ``(account_id,
event_key)`` dedup handles the case where a ``partial_fill`` and its
completing ``"fill"`` share the same ``event_key`` — the second arrival is
dropped.  A ``partial_fill`` whose ``event_key`` never appears again IS a
real, standalone execution.

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


def normalize_fill_event(
    entry: OrderJournalEntry,
    seen: set[tuple[str, str]],
    target_namespace: str,
) -> FillRecord | None:
    """Canonical normalized-fill fold gate.

    Returns a ``FillRecord`` when ``entry`` is a new, owned, fill-type event
    in ``target_namespace``.  Returns ``None`` otherwise (not a fill, wrong
    namespace, already seen, or missing required fields).

    ``seen`` is mutated in-place to record the ``(account_id, event_key)``
    dedup key when a FillRecord is returned.  The caller is responsible for
    maintaining ``seen`` across the full event stream.

    Accepted event types: ``"fill"`` and ``"partial_fill"``.
    - ``"partial_fill"``: a real broker execution of part of an order; must
      be counted in P&L and the trades list.
    - The ``(account_id, event_key)`` dedup handles the case where a
      ``partial_fill`` and its completing ``"fill"`` share the same
      ``event_key`` — the second arrival is silently dropped.

    The returned ``FillRecord`` has ``sid=""``; ``project_instance_fills``
    stamps the actual sid after filtering by namespace.  Callers that use
    ``normalize_fill_event`` directly (e.g., a multi-namespace scanner) must
    stamp the sid themselves.

    This is the SINGLE authority for whether a journal entry produces a
    FillRecord.  Do not replicate this logic elsewhere.
    """
    if entry.kind is not ClerkEntryKind.ORDER_EVENT or entry.owned is not True:
        return None
    event = entry.event
    if event is None or event.event_type not in ("fill", "partial_fill"):
        return None
    if event.quantity is None or event.price is None:
        return None
    if not entry.event_key:
        return None
    leg = entry.leg
    if leg is None or not entry.order_ref:
        return None
    try:
        entry_namespace, intent_id = parse_order_ref(entry.order_ref)
    except ValueError:
        return None
    if entry_namespace != target_namespace:
        return None
    dedup_key = (entry.account_id, entry.event_key)
    if dedup_key in seen:
        return None
    seen.add(dedup_key)
    return FillRecord(
        account_id=entry.account_id,
        sid="",  # stamped by the caller after namespace filtering
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

    Routes through ``normalize_fill_event`` — the canonical fold gate.
    """
    if not sid:
        raise ValueError("sid must be non-empty")
    target_namespace = build_bot_order_namespace(sid)
    seen: set[tuple[str, str]] = set()
    out: list[FillRecord] = []

    for entry in entries:
        record = normalize_fill_event(entry, seen, target_namespace)
        if record is None:
            continue
        # Stamp the actual sid (normalize_fill_event leaves sid="" because it
        # is namespace-scoped, not sid-scoped — the namespace encodes the sid
        # but extract round-trip is wasteful when the caller already knows it).
        out.append(
            FillRecord(
                account_id=record.account_id,
                sid=sid,
                intent_id=record.intent_id,
                order_ref=record.order_ref,
                event_key=record.event_key,
                symbol=record.symbol,
                side=record.side,
                quantity=record.quantity,
                fill_price=record.fill_price,
                filled_at_ms=record.filled_at_ms,
                fee=record.fee,
            )
        )

    out.sort(key=lambda r: r.filled_at_ms)
    return tuple(out)
