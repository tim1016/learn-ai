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
    compose_failure_refusal,
    compose_repository_runtime,
    developer_reset_refusal,
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
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.schemas.account_authority import world_admits_account_mode

logger = logging.getLogger(__name__)

# ADR 0059 D10 by extension (design R14): a real-money authority never
# installs behind an open data-plane control surface.
LIVE_CONTROL_UNAUTHENTICATED = "LIVE_CONTROL_UNAUTHENTICATED"


# The runner's sealed bindings on one live account: ``strategy_instance_id``
# -> that instance's current sealed-program hash. Built in the composition
# root (``main.py``, beside ``_alpaca_roster_symbols``) and injected, so the
# clerk layer never learns the runner's root and never imports the binding
# repository — the ``roster_symbols`` pattern, one argument wider because the
# composition root learns the live account's id only from this selector's own
# broker read. ``None`` means no seals, which means no instance is armed:
# fail closed, never a default.
type InstanceSealsForAccount = Callable[[str], Mapping[str, str]]


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
    instance_seals: InstanceSealsForAccount | None,
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
    # The world-to-mode rule is the closed table's, never a ``"live"`` literal
    # restated here: this is the one place a real-money authority decides mode
    # agreement, and a fourth spelling of the rule is a fourth thing to keep
    # in step with `_MODE_ADMITTED_BY_WORLD` (repo philosophy #5).
    if (
        not world_admits_account_mode("real_live", account.account_mode)
        or activation.account_id != account.account_id
    ):
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
    reset_refusal = developer_reset_refusal(
        account_id=account.account_id,
        artifacts_root=artifacts_root,
        authority_generation=activation.authority_generation,
        db_identity_token=activation.db_identity_token,
        cutover_noun="live",
    )
    if reset_refusal is not None:
        return reset_refusal

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
            # Bound to the observed account here, where its id is first known;
            # the sync calls the result with no arguments once per tick.
            instance_seals=(
                None if instance_seals is None else (lambda: instance_seals(account.account_id))
            ),
        )
    except Exception as exc:
        logger.warning(
            "Live Alpaca Clerk failed startup; no authority installed",
            extra={"action": "live_active_clerk_startup_failed", "account_id": account.account_id},
            exc_info=True,
        )
        return compose_failure_refusal(
            exc,
            account_id=account.account_id,
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


__all__ = ["LIVE_CONTROL_UNAUTHENTICATED", "InstanceSealsForAccount", "select_live_clerk_runtime"]
