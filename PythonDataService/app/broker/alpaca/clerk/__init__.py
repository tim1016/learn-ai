"""Alpaca Clerk (Broker System v2, phase 2).

The Clerk is the **in-process** single-writer that owns order submission for
Alpaca. Unlike the IBKR clerk (a separate process arbitrating one shared
gateway session with a lease + generation fencing), Alpaca is stateless REST —
there is no single gateway session to arbitrate — so the Clerk is a plain async
service running inside the data-plane container.

**Single-writer / single-worker constraint.** Serial submission per account is
guaranteed by (a) an ``asyncio.Lock`` intake lock and (b) the deployment
running a single uvicorn worker. Two workers would each hold their own lock and
break serialization; the append-only journal's ``fsync`` durability holds
regardless, but the "one order in flight at a time" invariant does not. Do not
scale this service to multiple workers without moving the intake lock to a
cross-process primitive.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.clerk import (
    AlpacaClerk,
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegResult,
    OrderSubmitResult,
)

__all__ = [
    "AlpacaClerk",
    "ClerkEntryKind",
    "OrderCancelResult",
    "OrderJournalEntry",
    "OrderLegResult",
    "OrderSubmitResult",
    "get_alpaca_clerk",
    "reset_alpaca_clerk_for_testing",
    "set_alpaca_clerk",
]
