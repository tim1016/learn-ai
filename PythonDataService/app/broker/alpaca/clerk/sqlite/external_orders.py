"""Durable containment of broker orders outside registered bot custody.

This module is intentionally the only path that turns a foreign Alpaca order
into an ``external_orders`` row.  It does not create an ``orders`` row, a
``fills`` row, or a position update, so external account activity can never
be misattributed to a bot's economics.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    ExternalOrderAcknowledgedFacts,
    ExternalOrderObservedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import TransitionProvenance, raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
    UnfoldableBrokerOrder,
    UnfoldableBrokerOrderCause,
)
from app.broker.contract.models import BrokerOrder


class ExternalOrderObservationError(ValueError):
    """The broker supplied insufficient evidence for a truthful observation."""


class InvalidExternalOrderCursor(ValueError):
    """A page cursor is malformed or belongs to another SQLite authority."""


ExternalOrderLifecycleState = Literal["review_required", "reviewed"]


@dataclass(frozen=True)
class ExternalOrderPage:
    """One bounded external-order page from a single SQLite read snapshot."""

    orders: tuple[ExternalOrderResource, ...]
    next_cursor: str | None
    authority_generation: int
    control_revision: int


def observe_external_order(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    proof_reference: str | None = None,
) -> ExternalOrderResource:
    """Record one foreign order and atomically fence new bot exposure.

    The resulting transition has no bot identity.  Its fold owns the
    external-order row plus the specific ``UNEXPLAINED_ORDER`` hold cause.
    Repeated identical broker snapshots return the existing row without
    growing either the hash chain or the hold evidence.
    """
    expected = _observation_from_broker_order(order)
    facts = ExternalOrderObservedFacts(
        external_order_id=expected.external_order_id,
        broker_order_id=expected.broker_order_id,
        client_order_id=expected.client_order_id,
        symbol=expected.symbol,
        side=expected.side,
        qty=expected.qty,
        order_type=expected.order_type,
        limit_price=expected.limit_price,
        stop_price=expected.stop_price,
        filled_avg_price=expected.filled_avg_price,
        observed_at_ms=expected.observed_at_ms,
        evidence_refs=list(expected.evidence_refs),
    )
    return repo.append_external_order_observation_if_changed(
        expected=expected,
        build_transition=lambda: TransitionInput(
            transition_kind="EXTERNAL_ORDER_OBSERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            broker_order_id=expected.broker_order_id,
            proof_reference=proof_reference or expected.broker_order_id,
            source_event_at_ms=expected.observed_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXTERNAL_ORDER_OBSERVED",
            facts_json=facts.to_facts_json(),
        ),
    )


def record_unfoldable_broker_order(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    reason: str,
    proof_reference: str | None = None,
) -> str:
    """Durably name one foreign order :func:`observe_external_order` refused (#2363).

    A broker order this fold cannot state truthfully (a multi-leg parent with
    a null ``side``) must neither be written as a guessed external-order row
    nor be silently dropped. It joins the one account-scoped
    ``UNFOLDABLE_BROKER_ORDER`` episode, keyed by broker order id, so the
    operator sees every such order and its reason. A replay of the same order
    re-states an identical cause and appends nothing. The episode's policy
    gates nothing (see ``uncertainty_policies``); the caller keeps folding
    every other order. Returns :func:`raise_uncertainty`'s outcome.
    """
    entry = UnfoldableBrokerOrder(
        broker_order_id=order.order_id.strip() or f"unidentified:{proof_reference}",
        client_order_id=order.client_order_id or None,
        reason=reason,
    )
    known = {entry.broker_order_id: entry}
    active = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        strategy_instance_id=None,
    )
    if active is not None:
        prior = UnfoldableBrokerOrderCause.from_mapping(
            UncertaintyRaisedFacts.from_facts_json(active["facts_json"]).cause_facts
        )
        known = {prior_order.broker_order_id: prior_order for prior_order in prior.orders} | known
    cause = UnfoldableBrokerOrderCause(
        orders=tuple(known[broker_order_id] for broker_order_id in sorted(known))
    )
    named = "; ".join(f"{unfoldable.broker_order_id}: {unfoldable.reason}" for unfoldable in cause.orders)
    return raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        headline="A broker order could not be recorded",
        explanation=(
            f"The Clerk could not record {len(cause.orders)} broker order(s) from the "
            f"trade-update stream ({named}). Each was set aside so every other order "
            "keeps folding."
        ),
        operator_impact=(
            "Nothing is paused by this record. Any position change these orders made is "
            "still checked per symbol by reconciliation."
        ),
        next_step="Inspect each named order at Alpaca and confirm no bot position depends on it.",
        evidence_refs=tuple(
            sorted(
                {
                    ref
                    for unfoldable in cause.orders
                    for ref in (unfoldable.broker_order_id, unfoldable.client_order_id)
                    if ref is not None
                }
            )
        ),
        cause_facts=cause.to_mapping(),
        severity="error",
        provenance=TransitionProvenance(
            broker_order_id=entry.broker_order_id,
            proof_reference=proof_reference or entry.broker_order_id,
            source_event_at_ms=order.observed_at_ms,
        ),
    )


def acknowledge_external_order(
    repo: ClerkSqliteRepository,
    *,
    external_order_id: str,
    operator: str,
) -> ExternalOrderResource:
    """Mark exactly one external order reviewed and scope its hold release."""
    if not external_order_id:
        raise ValueError("external_order_id must be non-empty")
    if not operator or len(operator) > 64:
        raise ValueError("operator must be between 1 and 64 characters")
    facts = ExternalOrderAcknowledgedFacts(
        external_order_id=external_order_id,
        ack_operator=operator,
    )
    return repo.acknowledge_external_order_if_unreviewed(
        external_order_id=external_order_id,
        build_transition=lambda existing: TransitionInput(
            transition_kind="EXTERNAL_ORDER_ACKNOWLEDGED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            broker_order_id=existing.broker_order_id,
            proof_reference=existing.external_order_id,
            source_event_at_ms=existing.observed_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXTERNAL_ORDER_ACKNOWLEDGED",
            facts_json=facts.to_facts_json(),
        ),
    )


class SqliteExternalOrderReader:
    """Read-only, identity-bound access to the external-order fold table."""

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._authority_generation = authority_generation
        self._db_identity_token = db_identity_token
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only = ON")
        with self._read_transaction():
            self._verify_identity()

    @classmethod
    def from_repository(
        cls,
        repository: ClerkSqliteRepository,
    ) -> SqliteExternalOrderReader:
        meta = repository.control_meta_snapshot()
        return cls(
            db_path=repository.db_path,
            account_id=meta.account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
        )

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def external_orders(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 50,
        lifecycle_state: ExternalOrderLifecycleState | None = None,
    ) -> ExternalOrderPage:
        if not 1 <= page_size <= 100:
            raise ValueError("external-order page_size must be between 1 and 100")
        anchor = (
            self._decode_cursor(cursor, lifecycle_state=lifecycle_state)
            if cursor is not None
            else None
        )
        with self._read_transaction():
            meta = self._verify_identity()
            rows = reads.external_order_page(
                self._conn,
                observation_sequence_before=(anchor[0] if anchor is not None else None),
                external_order_id_before=(anchor[1] if anchor is not None else None),
                lifecycle_state=lifecycle_state,
                limit=page_size + 1,
            )
        visible = tuple(rows[:page_size])
        next_cursor = (
            self._encode_cursor(visible[-1], lifecycle_state=lifecycle_state)
            if len(rows) > page_size and visible
            else None
        )
        return ExternalOrderPage(
            orders=visible,
            next_cursor=next_cursor,
            authority_generation=self._authority_generation,
            control_revision=meta["control_revision"],
        )

    def _verify_identity(self) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT account_id, authority_generation, db_identity_token, control_revision "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
        if row is None or (
            row["account_id"] != self._account_id
            or row["authority_generation"] != self._authority_generation
            or row["db_identity_token"] != self._db_identity_token
        ):
            raise InvalidExternalOrderCursor("external-order reader authority identity changed")
        return row

    def _encode_cursor(
        self,
        order: ExternalOrderResource,
        *,
        lifecycle_state: ExternalOrderLifecycleState | None,
    ) -> str:
        payload = {
            "account_id": self._account_id,
            "authority_generation": self._authority_generation,
            "db_identity_token": self._db_identity_token,
            "external_order_id": order.external_order_id,
            "lifecycle_state": lifecycle_state,
            "observation_sequence": order.observation_sequence,
        }
        return base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    def _decode_cursor(
        self,
        cursor: str,
        *,
        lifecycle_state: ExternalOrderLifecycleState | None,
    ) -> tuple[int, str]:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        except (binascii.Error, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidExternalOrderCursor("external-order cursor is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("account_id") != self._account_id
            or payload.get("authority_generation") != self._authority_generation
            or payload.get("db_identity_token") != self._db_identity_token
            or payload.get("lifecycle_state") != lifecycle_state
            or not isinstance(payload.get("observation_sequence"), int)
            or not isinstance(payload.get("external_order_id"), str)
            or not payload["external_order_id"]
        ):
            raise InvalidExternalOrderCursor(
                "external-order cursor has a different authority or filter scope"
            )
        return payload["observation_sequence"], payload["external_order_id"]


def _observation_from_broker_order(order: BrokerOrder) -> ExternalOrderResource:
    broker_order_id = order.order_id.strip()
    symbol = order.symbol.strip().upper()
    side = order.side.upper()
    qty = order.quantity if order.quantity is not None else order.filled_quantity
    order_type = order.order_type.strip().lower()
    if not broker_order_id:
        raise ExternalOrderObservationError("broker order id must be non-empty")
    if not symbol:
        raise ExternalOrderObservationError("external order symbol must be non-empty")
    if side not in {"BUY", "SELL"}:
        raise ExternalOrderObservationError("external order side must be buy or sell")
    if not math.isfinite(qty) or qty < 0:
        raise ExternalOrderObservationError("external order quantity must be finite and non-negative")
    if not order_type:
        raise ExternalOrderObservationError("external order type must be non-empty")
    for price_name, price in (
        ("limit", order.limit_price),
        ("stop", order.stop_price),
        ("filled average", order.filled_avg_price),
    ):
        if price is not None and not math.isfinite(price):
            raise ExternalOrderObservationError(
                f"external order {price_name} price must be finite when supplied"
            )
    return ExternalOrderResource(
        # Broker order identity is stable and unique in the SQLite fold;
        # reusing it makes the row directly auditable against broker evidence.
        external_order_id=broker_order_id,
        broker_order_id=broker_order_id,
        client_order_id=(
            order.client_order_id
            if order.client_order_id is not None
            else f"missing-client-order-id:{broker_order_id}"
        ),
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        filled_avg_price=order.filled_avg_price,
        observed_at_ms=order.observed_at_ms,
        acknowledged_at_ms=None,
        ack_operator=None,
        evidence_refs=(broker_order_id,),
    )


__all__ = [
    "ExternalOrderLifecycleState",
    "ExternalOrderObservationError",
    "ExternalOrderPage",
    "InvalidExternalOrderCursor",
    "SqliteExternalOrderReader",
    "acknowledge_external_order",
    "observe_external_order",
    "record_unfoldable_broker_order",
]
