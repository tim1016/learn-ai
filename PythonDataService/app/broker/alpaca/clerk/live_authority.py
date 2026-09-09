"""The real-money Live Account Authority's boot story (ADR 0059 D1, D11 — slice 7).

A live account gets a mutating Clerk only on three-way mode agreement: the
configured mode (which the adapter already derived the observed
``account_mode`` from), the broker-observed account, and the cutover's
activation record naming that exact account. It is the same ``sqlite`` Clerk
the paper account runs, composed with the two gates a real-money ENTER is
admitted against — the risk envelope (D4) and the per-instance arming gate
(D3/D11) — and the real trade port. Every refusal is a typed ``unavailable``
runtime; a live boot never aborts the data plane (#2014).

Cold start is the paper path's (design R18): the cutover's flat-and-order-
free evidence at graduation and ``recover()`` at every boot. The shadow
namespace scan is not applied here — after the first real order it would
refuse every boot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    bind_real_alpaca_ports,
)
from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    compose_repository_runtime,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

logger = logging.getLogger(__name__)

# ADR 0059 D10 by extension (design R14): a real-money authority never
# installs behind an open data-plane control surface.
LIVE_CONTROL_UNAUTHENTICATED = "LIVE_CONTROL_UNAUTHENTICATED"


def _instance_seals_reader(
    *, live_account_id: str, live_state_root: Callable[[], Path] | None
) -> Callable[[], Mapping[str, str]]:
    """The runner's sealed bindings on this account, read per tick by the sync.

    Injected as a callable so the clerk layer never learns the runner's root
    (the ``roster_symbols`` pattern). No root means no seals, which means no
    instance is armed — fail closed, never a default.
    """

    def _seals() -> Mapping[str, str]:
        if live_state_root is None:
            return {}
        # Local import: the ceremony imports the runner's binding repository,
        # which this boot-time module must not pull in at import.
        from app.broker.alpaca.clerk.live_arming_ceremony import instance_seal_hashes

        return {
            sid: seal.seal_hash
            for sid, seal in instance_seal_hashes(
                live_account_id=live_account_id, live_state_root=live_state_root()
            ).items()
        }

    return _seals


async def select_live_clerk_runtime(
    *,
    account: BrokerAccountSnapshot,
    activation: ActivationRecord,
    activation_store: ActivationStore,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
    live_envelope_values: LiveEnvelopeValues | None,
    live_state_root: Callable[[], Path] | None,
    control_unauthenticated: bool,
) -> ActiveClerkRuntime:
    """Compose the real-money authority for an activated live account (ADR 0059 D1/D11)."""
    if control_unauthenticated:
        return unavailable_runtime(
            LIVE_CONTROL_UNAUTHENTICATED,
            account_id=account.account_id,
            recovery=(
                "Set DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=false; a real-money authority "
                "never installs behind an open control plane (ADR 0059 D10)."
            ),
        )
    if account.account_mode != "live" or activation.account_id != account.account_id:
        return unavailable_runtime(
            LIVE_MODE_DISAGREEMENT,
            account_id=account.account_id,
            recovery=(
                "The observed account, the configured mode and the live activation record "
                "must name one live account (ADR 0059 D1)."
            ),
        )
    if live_envelope_values is None:
        return unavailable_runtime(
            LIVE_ENVELOPE_MISSING,
            account_id=account.account_id,
            recovery=(
                "Set every ALPACA_LIVE_* value; a live authority admits nothing without "
                "its envelope (ADR 0059 D4)."
            ),
        )
    try:
        ports = bind_real_alpaca_ports(
            account_id=account.account_id, read=read, trade=trade, account_mode="live"
        )
    except AccountAuthorityIdentityError as exc:
        return unavailable_runtime(
            "REAL_PORT_REJECTED_SYNTHETIC_ACCOUNT", account_id=account.account_id, recovery=str(exc)
        )
    alpaca_accounts_root = artifacts_root / "accounts" / "alpaca"
    if DeveloperCleanSlateResetRegistry(alpaca_accounts_root).authorizes_reinitialize(
        account_id=account.account_id,
        prior_authority_generation=activation.authority_generation,
        artifacts_root=artifacts_root,
    ):
        return unavailable_runtime(
            "DEVELOPER_RESET_REACTIVATION_REQUIRED",
            account_id=account.account_id,
            recovery=(
                "This activated authority was moved aside by a developer clean-slate reset. "
                "Regenerate it, then complete a new live cutover before startup."
            ),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    def _verify_live_activation(meta: ControlMetaSnapshot) -> None:
        if (
            activation_store.resolve(
                account.account_id, meta.authority_generation, meta.db_identity_token, artifacts_root
            )
            is None
        ):
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")

    try:
        composed = await compose_repository_runtime(
            ports=ports,
            authority_kind="sqlite",
            account_mode="live",
            artifacts_root=artifacts_root,
            verify_activation=_verify_live_activation,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            # Real custody: the broker's cash already reflects this Clerk's
            # own fills, so the envelope subtracts nothing (ADR 0059 D4).
            live_envelope=LiveEnvelopeGate(values=live_envelope_values, custody_is_simulated=False),
            arming_ledger=LiveArmingLedger(artifacts_root, live_account_id=account.account_id),
            arming_gate=ArmingGate(),
            instance_seals=_instance_seals_reader(
                live_account_id=account.account_id, live_state_root=live_state_root
            ),
        )
    except Exception as exc:
        logger.warning(
            "Live Alpaca Clerk failed startup; no authority installed",
            extra={"action": "live_active_clerk_startup_failed", "account_id": account.account_id},
            exc_info=True,
        )
        return unavailable_runtime(
            (
                "ACTIVATION_RECORD_INVALID"
                if isinstance(exc, ActivationRecordInvalid)
                else "SQLITE_CLERK_STARTUP_FAILED"
            ),
            account_id=account.account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )
    logger.warning(
        "REAL-MONEY Alpaca authority installed; ENTERs admit only for armed instances",
        extra={"action": "live_authority_installed", "account_id": account.account_id},
    )
    return ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=composed.facade,
        sweep=composed.sweep,
        hold_sync=composed.hold_sync,
        envelope_sync=composed.envelope_sync,
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=composed.repository,
            intake=composed.facade.intake,
            reconciler=composed.facade,
        ),
        _sqlite_repository=composed.repository,
        account_id=account.account_id,
        account_authority_kind="real_live",
    )


__all__ = ["LIVE_CONTROL_UNAUTHENTICATED", "select_live_clerk_runtime"]
