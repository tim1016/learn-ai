"""Atomic repository operations for non-bot broker-order observations.

External orders deliberately sit outside the bot command/fill model.  This
small mixin keeps their two critical check-and-append operations under the
same repository write coordinator as the custody hash chain, without growing
the repository spine with another product-specific concern.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource, TransitionInput

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class ExternalOrderNotFoundError(ValueError):
    """An operator acknowledgement named no durable external observation."""


class ClerkSqliteRepositoryExternalOrderApi:
    """Focused atomic mutations mixed into ``ClerkSqliteRepository``."""

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
        and existing.price == expected.price
        and existing.evidence_refs == expected.evidence_refs
    )
