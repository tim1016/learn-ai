"""Atomic repository operations for non-bot broker-order observations.

External orders deliberately sit outside the bot command/fill model.  This
small mixin keeps their two critical check-and-append operations under the
same repository write coordinator as the custody hash chain, without growing
the repository spine with another product-specific concern.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource, TransitionInput

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class ExternalOrderNotFoundError(ValueError):
    """An operator acknowledgement named no durable external observation."""


class ClerkSqliteRepositoryExternalOrderApi:
    """Focused atomic mutations mixed into ``ClerkSqliteRepository``."""

    @contextmanager
    def unfoldable_order_write_serialized(self: ClerkSqliteRepository) -> Iterator[None]:
        """Hold the write coordinator across one unfoldable-order read-merge-append.

        The ``UNFOLDABLE_BROKER_ORDER`` episode is one account-wide list that
        three writers edit (the trade-update sink, the reconciliation verdict,
        and the operator acknowledgement). Each reads the active cause, merges,
        and appends; without one lock across all three steps a concurrent
        writer's order could be dropped from the fence (#2363). The lock is
        reentrant, so the appends inside reacquire it safely.
        """
        with self._write_lock:
            self._assert_not_poisoned()
            yield

    def unfoldable_broker_order_acknowledgements(
        self: ClerkSqliteRepository,
    ) -> dict[str, reads.UnfoldableBrokerOrderReview]:
        with self._write_lock:
            return reads.unfoldable_broker_order_acknowledgements(self._conn)

    def unfoldable_broker_orders_active_since(
        self: ClerkSqliteRepository, *, reason_code: str, since_ms: int
    ) -> int:
        with self._write_lock:
            return reads.unfoldable_broker_orders_active_since(
                self._conn, reason_code=reason_code, since_ms=since_ms
            )

    def append_external_order_observation_if_changed(
        self: ClerkSqliteRepository,
        *,
        expected: ExternalOrderResource,
        build_transition: Callable[[], TransitionInput],
    ) -> ExternalOrderResource:
        """Append only a new external fact; duplicate delivery is a no-op."""
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            existing = reads.external_order_by_broker_order_id(
                self._conn, expected.broker_order_id
            )
            if existing is not None and _same_observation(existing, expected):
                return existing
            self.append_transition(build_transition())
            observed = reads.external_order_by_broker_order_id(
                self._conn, expected.broker_order_id
            )
            assert observed is not None, "external-order fold did not materialize its observation"
            return observed

    def acknowledge_external_order_if_unreviewed(
        self: ClerkSqliteRepository,
        *,
        external_order_id: str,
        build_transition: Callable[[ExternalOrderResource], TransitionInput],
    ) -> ExternalOrderResource:
        """Append one acknowledgement only while this exact observation is open."""
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            existing = reads.external_order(self._conn, external_order_id)
            if existing is None:
                raise ExternalOrderNotFoundError(
                    f"external order {external_order_id!r} was not found"
                )
            if existing.acknowledged_at_ms is not None:
                return existing
            self.append_transition(build_transition(existing))
            acknowledged = reads.external_order(self._conn, external_order_id)
            assert acknowledged is not None, "external-order acknowledgement removed its audit row"
            assert acknowledged.acknowledged_at_ms is not None, (
                "external-order acknowledgement fold did not mark the order reviewed"
            )
            return acknowledged


def _same_observation(
    existing: ExternalOrderResource,
    expected: ExternalOrderResource,
) -> bool:
    return (
        existing.external_order_id == expected.external_order_id
        and existing.client_order_id == expected.client_order_id
        and existing.symbol == expected.symbol
        and existing.side == expected.side
        and existing.qty == expected.qty
        and existing.order_type == expected.order_type
        and existing.limit_price == expected.limit_price
        and existing.stop_price == expected.stop_price
        and existing.filled_avg_price == expected.filled_avg_price
        and existing.evidence_refs == expected.evidence_refs
    )
