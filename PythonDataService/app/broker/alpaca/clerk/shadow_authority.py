"""The Shadow Account Authority's boot story (ADR 0059 D2).

A live account never gets a mutating Clerk. It gets this: the live read
port bound behind a ``shadow:`` identity whose trade port submits nothing,
composed only behind its own explicit activation fence and only while the
live order namespace stays provably empty.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    bind_shadow_ports,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    activate_isolated_authority,
    compose_repository_runtime,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.shadow_activation import (
    ShadowActivationInvalid,
    ShadowActivationRecord,
    ShadowActivationStore,
)
from app.broker.alpaca.clerk.shadow_broker import (
    ShadowNamespacePoisoned,
    ShadowNamespaceUnproven,
    compose_shadow_ports,
    verify_shadow_namespace_empty,
)
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger, ShadowSessionRecorder
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.trade_evidence import NullTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)


async def select_shadow_clerk_runtime(
    *,
    account: BrokerAccountSnapshot,
    read: BrokerReadPort,
    artifacts_root: Path,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
    live_envelope_values: LiveEnvelopeValues | None,
) -> ActiveClerkRuntime:
    """Compose the Shadow Account Authority for a live account (ADR 0059 D2).

    The live trade port is never bound: the shadow world's trade port is
    ``NoSubmitAlpacaTradePort``. Real-money custody stays unconstructible
    until slice 7 admits an armed instance.

    ``live_envelope_values`` are the configured ``ALPACA_LIVE_*`` bounds
    (ADR 0059 D4). They are not optional in practice: the shadow authority
    exists to *rehearse* the live envelope, so a boot that cannot build one
    installs no authority at all rather than a rehearsal of nothing.
    """
    if live_envelope_values is None:
        return unavailable_runtime(
            LIVE_ENVELOPE_MISSING,
            account_id=shadow_account_id_for_live_account(account.account_id),
            recovery=(
                "Set every ALPACA_LIVE_* value; the shadow authority rehearses the "
                "live envelope and refuses to run without it (ADR 0059 D4)."
            ),
        )
    try:
        await verify_shadow_namespace_empty(read)
        shadow = compose_shadow_ports(
            live_read=read,
            live_account_id=account.account_id,
            artifacts_root=artifacts_root,
        )
    except (ShadowNamespacePoisoned, ShadowNamespaceUnproven) as exc:
        logger.warning(
            "live account refused for shadow: order namespace not proven empty",
            extra={"action": "shadow_namespace_refused", "reason_code": exc.reason_code},
        )
        return unavailable_runtime(exc.reason_code, account_id=account.account_id, recovery=str(exc))
    except BrokerError as exc:
        return unavailable_runtime(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=account.account_id,
            recovery=f"Restore the live account's order-history read: {exc}",
        )
    except Exception as exc:
        # The same fail-open-to-`unavailable` posture the paper path takes for
        # its own account probe. Neither call above is exception-typed by the
        # broker contract: `list_orders` adapts each vendor payload outside
        # `AlpacaClient._call`, so a malformed one raises a raw
        # `ValidationError`, and composition touches the filesystem. Unhandled,
        # either aborts the whole data plane's startup -- and this is the
        # real-money path. (`asyncio.CancelledError` is a `BaseException` and
        # so still propagates: a cancelled boot is not a refused authority.)
        logger.warning(
            "Live account's shadow authority could not be composed; no authority installed",
            extra={
                "action": "shadow_clerk_startup_failed",
                "account_id": account.account_id,
            },
            exc_info=True,
        )
        return unavailable_runtime(
            "SHADOW_CLERK_STARTUP_FAILED",
            account_id=account.account_id,
            recovery=f"Restore the live account's shadow composition: {exc}",
        )
    ports = bind_shadow_ports(account_id=shadow.account_id, read=shadow.read, trade=shadow.trade)
    store = ShadowActivationStore(artifacts_root)
    try:
        activation = store.latest(shadow.account_id)
    except ShadowActivationInvalid as exc:
        return unavailable_runtime(
            "SHADOW_ACTIVATION_RECORD_INVALID",
            account_id=shadow.account_id,
            recovery=str(exc),
            activation_detected=True,
        )
    if activation is None:
        return unavailable_runtime(
            "SHADOW_ACTIVATION_REQUIRED",
            account_id=shadow.account_id,
            recovery=(
                "Explicitly activate the shadow authority for this live account "
                "(scripts.manage_alpaca_shadow activate) before starting shadow custody."
            ),
        )

    def _verify_shadow_activation(meta: ControlMetaSnapshot) -> None:
        if (
            meta.authority_generation != activation.authority_generation
            or meta.db_identity_token != activation.db_identity_token
        ):
            raise ShadowActivationInvalid("shadow activation does not match repository identity")

    sessions = ShadowSessionRecorder(
        ledger=ShadowSessionLedger(artifacts_root=artifacts_root, account_id=shadow.account_id),
        window=read.capabilities().extended_hours_window,
    )
    try:
        composed = await compose_repository_runtime(
            ports=ports,
            authority_kind="shadow",
            account_mode=account.account_mode,
            artifacts_root=artifacts_root,
            verify_activation=_verify_shadow_activation,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            sweep_listener=sessions.record,
            # Simulated custody: the live account's cash never moves, so the
            # envelope subtracts what this Clerk's own fills would have spent
            # (plan R2). Unsealed until an arming record exists (slice 6).
            live_envelope=LiveEnvelopeGate(
                values=live_envelope_values, custody_is_simulated=True
            ),
            # The envelope observes the live account's cash and positions
            # (plan: unrealized is the live account's).
            envelope_read=read,
        )
    except Exception as exc:
        logger.warning(
            "Shadow Alpaca Clerk failed startup; no authority installed",
            extra={"action": "shadow_active_clerk_startup_failed", "account_id": shadow.account_id},
            exc_info=True,
        )
        return unavailable_runtime(
            (
                "SHADOW_ACTIVATION_RECORD_INVALID"
                if isinstance(exc, ShadowActivationInvalid)
                else "SHADOW_CLERK_STARTUP_FAILED"
            ),
            account_id=shadow.account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )
    return ActiveClerkRuntime(
        authority_kind="shadow",
        clerk=composed.facade,
        sweep=composed.sweep,
        hold_sync=composed.hold_sync,
        envelope_sync=composed.envelope_sync,
        evidence_sink=NullTradeUpdateEvidenceSink(),
        _sqlite_repository=composed.repository,
        account_id=shadow.account_id,
        account_authority_kind="shadow",
    )


async def activate_shadow_clerk_authority(
    *,
    live_account_id: str,
    artifacts_root: Path,
    activation_store: ShadowActivationStore | None = None,
) -> ShadowActivationRecord:
    """Explicitly initialize and durably activate the shadow authority for one live account.

    No startup path calls this; the operator does, once, through
    ``scripts.manage_alpaca_shadow activate``. The custody database it creates
    is the shadow world's own -- the live account's authority is untouched.
    """
    record = await activate_isolated_authority(
        account_id=shadow_account_id_for_live_account(live_account_id),
        artifacts_root=artifacts_root,
        store=activation_store or ShadowActivationStore(artifacts_root),
    )
    assert isinstance(record, ShadowActivationRecord)
    return record


__all__ = [
    "activate_shadow_clerk_authority",
    "select_shadow_clerk_runtime",
]
