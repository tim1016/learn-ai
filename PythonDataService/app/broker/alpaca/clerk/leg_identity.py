"""The minted per-leg order identity and its journal-entry shaping.

Pure data shaping extracted from ``clerk.py`` (which sits at the 1k-line
ceiling): the injected clock, the broker-error adapter, and the
:class:`LegIdentity` builder every journal append site stamps. No I/O, no
locking — the clerk owns those.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderJournalEntry,
    OrderLegError,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg

# An injected clock: the current instant as ``int64`` ms UTC. Defaults to the
# ingestion-boundary wall clock; tests inject a fixed clock (mirrors the S4
# ``TradeUpdatesConsumer`` seam) so journaled timestamps are deterministic.
type Clock = Callable[[], int]


def default_clock() -> int:
    """Current instant as ``int64`` ms UTC (ingestion boundary)."""
    return int(datetime.now(UTC).timestamp() * 1000)


def leg_error(exc: BrokerError) -> OrderLegError:
    """Adapt a broker exception to the clerk's typed *what/why* leg error."""
    return OrderLegError(message=exc.message, why=exc.detail)


@dataclass(frozen=True, slots=True)
class LegIdentity:
    """The minted, durable identity for one leg, plus its journal context.

    Built once per leg before any journal write, then stamped onto every
    entry — so the six identity fields are never re-listed at each append site.
    ``client_order_id == order_ref`` is the design invariant.
    """

    account_id: str
    operator: str
    intent_id: str
    order_ref: str
    leg: BrokerOrderLeg
    clock: Clock

    def entry(
        self,
        kind: ClerkEntryKind,
        *,
        order: BrokerOrder | None = None,
        error: OrderLegError | None = None,
    ) -> OrderJournalEntry:
        """A journal entry for this identity, stamped with ``kind`` and outcome.

        ``error`` is the clerk's own typed *what/why* — a broker exception is
        adapted with :func:`leg_error` at the call site, and a resolution
        synthesises its own. Keeping the one error shape lets every terminal /
        uncertain line reuse this single builder instead of re-listing the six
        identity fields.
        """
        return OrderJournalEntry(
            kind=kind,
            account_id=self.account_id,
            operator=self.operator,
            intent_id=self.intent_id,
            order_ref=self.order_ref,
            client_order_id=self.order_ref,
            leg=self.leg,
            recorded_at_ms=self.clock(),
            order=order,
            error_message=error.message if error is not None else None,
            error_detail=error.why if error is not None else None,
        )

    @classmethod
    def from_entry(cls, entry: OrderJournalEntry, *, clock: Clock) -> LegIdentity:
        """Rebuild the identity from the owning ``intent_recorded`` line (S5).

        Resolution reuses the durable identity the submit minted — never
        fabricates one. Requires a leg: every submit-side line carries one, and
        the resolver only calls this on entries whose leg is present.
        """
        if entry.leg is None:
            raise ValueError(f"intent entry {entry.order_ref!r} has no leg to resolve")
        return cls(
            account_id=entry.account_id,
            operator=entry.operator,
            intent_id=entry.intent_id,
            order_ref=entry.order_ref,
            leg=entry.leg,
            clock=clock,
        )
