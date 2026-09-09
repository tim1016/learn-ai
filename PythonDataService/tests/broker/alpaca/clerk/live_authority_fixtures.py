"""One activated live account, one live-sealed binding, one recording trade port (ADR 0059 slice 7).

Not a conftest: imported by name from ``tests/broker/alpaca/clerk/``,
``tests/services/`` and ``tests/broker/v2panel/``, which share no conftest —
the ``live_arming_fixtures`` precedent. Extends those fixtures rather than
restating them, so every slice-7 test describes the same live account.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, select_active_clerk_runtime
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.test_active_authority import _ActivationStore

LIVE_SID = "ema-live-1"


def live_activation(
    *,
    account_id: str = LIVE_ACCT,
    authority_generation: int = 1,
    db_identity_token: str = "live-db",
) -> ActivationRecord:
    """The cutover's activation record for the live account — the second leg of Decision 1."""
    return ActivationRecord.create(
        account_id=account_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        broker_proof_reference="proof.json",
        broker_proof_sha256="0" * 64,
        legacy_quarantine_manifest="quarantine.json",
        legacy_quarantine_manifest_sha256="1" * 64,
        activated_at_ms=1,
    )


class _RecordingLiveBroker(_LiveBroker):
    """The live account whose trade port *is* reached: it records every submit and answers accepted."""

    def __init__(self, *, now_ms: int, **kwargs: Any) -> None:
        super().__init__(now_ms=now_ms, **kwargs)
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def submit(self, *args: Any, **kwargs: Any) -> BrokerOrder:
        self.submissions.append((args, kwargs))
        client_order_id = kwargs.get("client_order_id") or next(
            (value for value in args if isinstance(value, str)), "unknown"
        )
        leg = kwargs.get("leg") or next((value for value in args if hasattr(value, "quantity")), None)
        return BrokerOrder(
            broker="alpaca",
            order_id=f"live-order-{len(self.submissions)}",
            client_order_id=client_order_id,
            symbol=getattr(leg, "symbol", "SPY"),
            asset_class="us_equity",
            side=str(getattr(leg, "side", "buy")),
            order_type="market",
            time_in_force="day",
            quantity=float(getattr(leg, "quantity", 1)),
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=self.now_ms,
            created_at_ms=self.now_ms,
            updated_at_ms=self.now_ms,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=self.now_ms,
        )


def pinned_repository(now_ms: int) -> Callable[[str, Path], ClerkSqliteRepository]:
    """A repository opener pinned to one instant, so nothing here reads a wall clock."""

    def _open(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
        return ClerkSqliteRepository.open(
            account_id=account_id, artifacts_root=artifacts_root, clock=lambda: now_ms
        )

    return _open


async def compose_live(
    tmp_path: Path,
    broker: _LiveBroker,
    *,
    now_ms: int,
    live_state_root: Path,
    control_unauthenticated: bool = False,
    live_envelope_values: LiveEnvelopeValues | None = TEST_ENVELOPE_VALUES,
) -> ActiveClerkRuntime:
    """Initialize the live custody database, activate it, and run the real selector."""
    repository = ClerkSqliteRepository.initialize(
        account_id=LIVE_ACCT, artifacts_root=tmp_path, clock=lambda: now_ms
    )
    meta = repository.control_meta_snapshot()
    repository.close()
    activation = live_activation(
        authority_generation=meta.authority_generation, db_identity_token=meta.db_identity_token
    )
    return await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(activation),
        repository_opener=pinned_repository(now_ms),
        live_envelope_values=live_envelope_values,
        live_state_root=lambda: live_state_root,
        control_unauthenticated=control_unauthenticated,
    )


__all__ = ["LIVE_SID", "_RecordingLiveBroker", "compose_live", "live_activation", "pinned_repository"]
