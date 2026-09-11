"""Boot-time selection and process registry for the SQLite Alpaca Clerk.

The broker account is resolved before any writer is constructed. A paper
account with a valid activation record selects SQLite. A live account selects
on its own cutover record (ADR 0059 D1, slice 7): with one, the real-money
Live Account Authority; without one, the Shadow Account Authority behind its
own activation fence (ADR 0059 D2), which reads the live account but submits
nothing. Every other activation state selects no custody authority.

The composition each selection ends in lives in ``active_runtime``, the shadow
world's own boot story in ``shadow_authority`` and the live world's in
``live_authority``; all are re-exported here so this module stays the single
import surface callers already use.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    bind_real_alpaca_ports,
    bind_synthetic_ports,
    require_synthetic_account_id,
)
from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
from app.broker.alpaca.clerk.active_runtime import (
    DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S,
    DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S,
    DEFAULT_STARTUP_RECOVERY_TIMEOUT_S,
    SQLITE_FACADE_AUTHORITIES,
    ActiveClerkRuntime,
    AuthorityKind,
    ClerkStartupFailure,
    activate_isolated_authority,
    compose_failure_refusal,
    compose_repository_runtime,
    developer_reset_refusal,
    open_repository,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.live_authority import (
    InstanceSealsForAccount,
    select_live_clerk_runtime,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_authority import (
    activate_shadow_clerk_authority,
    select_shadow_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.synthetic_activation import (
    SyntheticActivationInvalid,
    SyntheticActivationRecord,
    SyntheticActivationStore,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerAccountModeDisagreement
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.schemas.account_authority import CustodyWorld

logger = logging.getLogger(__name__)


class ActivationResolver(Protocol):
    def latest(self, account_id: str) -> ActivationRecord | None: ...

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> ActivationRecord | None: ...


def _activation_record_invalid(account_id: str, exc: ActivationRecordInvalid) -> ActiveClerkRuntime:
    """The one refusal for an unreadable cutover record, on either side of the live fork."""
    return unavailable_runtime(
        "ACTIVATION_RECORD_INVALID",
        account_id=account_id,
        recovery=str(exc),
        activation_detected=True,
    )


async def select_active_clerk_runtime(
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    activation_store: ActivationResolver | None = None,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository] = open_repository,
    startup_recovery_timeout_s: float = DEFAULT_STARTUP_RECOVERY_TIMEOUT_S,
    execution_lease_wait_timeout_s: float = DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S,
    execution_lease_retry_interval_s: float = DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S,
    stream_health_gate: StreamHealthGate | None = None,
    roster_symbols: Callable[[], Sequence[str]] | None = None,
    live_envelope_values: LiveEnvelopeValues | None = None,
    instance_seals: InstanceSealsForAccount | None = None,
    control_unauthenticated: bool = False,
    expected_account_id: str | None = None,
) -> ActiveClerkRuntime:
    """Resolve the account, validate activation, and construct one authority.

    ``roster_symbols`` opts the sweep into the post-pass symbol-validity probe
    (#1795): a callable returning the fleet's bound symbols, injected here so
    the clerk layer never imports the bot-registration services. ``None`` (the
    default, and every test/synthetic path) constructs no probe.

    ``live_envelope_values`` reach only the live world's shadow authority
    (ADR 0059 D4). The paper authority below never composes an envelope, so a
    caller may offer values on any boot without changing what paper admits.

    ``instance_seals`` answers the runner's sealed bindings for one live
    account -- the live authority's arming gate reads them beside the ledger
    every tick (slice 7). Built in the composition root and injected, so the
    clerk layer never learns the runner's root. ``control_unauthenticated`` is
    the data plane's open-control flag; a live account refuses to install
    behind it (design R14). Both are inert on a paper boot.
    """
    try:
        account = await read.get_account()
    except BrokerAccountModeDisagreement as exc:
        logger.warning(
            "Alpaca configured mode and observed account disagree; Clerk unavailable",
            extra={"action": "active_clerk_mode_disagreement", "detail": exc.detail},
        )
        return unavailable_runtime(
            exc.reason_code,
            account_id=None,
            recovery=exc.detail or exc.message,
        )
    except Exception as exc:
        logger.warning(
            "Alpaca account identity could not be resolved; Clerk unavailable",
            extra={"action": "active_clerk_account_resolution_failed"},
            exc_info=True,
        )
        return unavailable_runtime(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=None,
            recovery=f"Restore the Alpaca account identity probe: {exc}",
        )
    # This read is the identity used to open custody. A separate verification
    # may have been unavailable, or observed different upstream state, so its
    # result cannot stand in for checking the revision's pin here.
    if expected_account_id is not None and account.account_id != expected_account_id:
        return unavailable_runtime(
            "ACCOUNT_PIN_MISMATCH",
            account_id=account.account_id,
            recovery=(
                "The broker account does not match the applied configuration's approved "
                "account. Restore its credentials or verify and apply a new revision."
            ),
        )
    store = activation_store or ActivationStore(artifacts_root / "accounts" / "alpaca")
    if account.account_mode == "live":
        # ADR 0059 D1 / slice 7 R1: graduation is a boot-time selection. The
        # cutover's activation record for this exact account is the second
        # leg of mode agreement; present, the real-money authority is
        # composed; absent, the account is shadowed exactly as slice 4 built.
        try:
            live_activation = store.latest(account.account_id)
        except ActivationRecordInvalid as exc:
            return _activation_record_invalid(account.account_id, exc)
        if live_activation is None:
            return await select_shadow_clerk_runtime(
                account=account,
                read=read,
                artifacts_root=artifacts_root,
                repository_opener=repository_opener,
                startup_recovery_timeout_s=startup_recovery_timeout_s,
                execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
                execution_lease_retry_interval_s=execution_lease_retry_interval_s,
                stream_health_gate=stream_health_gate,
                roster_symbols=roster_symbols,
                live_envelope_values=live_envelope_values,
            )
        return await select_live_clerk_runtime(
            account=account,
            activation=live_activation,
            activation_store=store,
            read=read,
            trade=trade,
            artifacts_root=artifacts_root,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            live_envelope_values=live_envelope_values,
            instance_seals=instance_seals,
            control_unauthenticated=control_unauthenticated,
        )
    try:
        ports = bind_real_alpaca_ports(
            account_id=account.account_id,
            read=read,
            trade=trade,
            account_mode="paper",
        )
    except AccountAuthorityIdentityError as exc:
        return unavailable_runtime(
            "REAL_PORT_REJECTED_SYNTHETIC_ACCOUNT",
            account_id=account.account_id,
            recovery=str(exc),
        )

    try:
        activation = store.latest(account.account_id)
    except ActivationRecordInvalid as exc:
        return _activation_record_invalid(account.account_id, exc)

    if activation is not None:
        reset_refusal = developer_reset_refusal(
            account_id=account.account_id,
            artifacts_root=artifacts_root,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
            cutover_noun="paper",
        )
        if reset_refusal is not None:
            return reset_refusal

    if activation is None:
        return unavailable_runtime(
            "ACTIVATION_REQUIRED",
            account_id=account.account_id,
            recovery=(
                "Complete the supervised SQLite Clerk cutover and activation "
                "before starting Alpaca custody."
            ),
        )

    def _verify_paper_activation(meta: ControlMetaSnapshot) -> None:
        if (
            store.resolve(
                account.account_id,
                meta.authority_generation,
                meta.db_identity_token,
                artifacts_root,
            )
            is None
        ):
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")

    try:
        composed = await compose_repository_runtime(
            ports=ports,
            authority_kind="sqlite",
            account_mode=account.account_mode,
            artifacts_root=artifacts_root,
            verify_activation=_verify_paper_activation,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
        )
    except Exception as exc:
        logger.warning(
            "Activated SQLite Alpaca Clerk failed startup; no writer installed",
            extra={
                "action": "sqlite_active_clerk_startup_failed",
                "account_id": account.account_id,
            },
            exc_info=True,
        )
        return compose_failure_refusal(
            exc,
            account_id=account.account_id,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    return ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=composed.facade,
        sweep=composed.sweep,
        hold_sync=composed.hold_sync,
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=composed.repository,
            intake=composed.facade.intake,
            reconciler=composed.facade,
        ),
        _sqlite_repository=composed.repository,
        account_id=account.account_id,
        account_authority_kind="real_paper",
    )


async def activate_synthetic_clerk_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    activation_store: SyntheticActivationStore | None = None,
) -> SyntheticActivationRecord:
    """Explicitly initialize and durably activate one isolated ``sim:`` account.

    No startup path calls this helper.  A synthetic account has no authority
    until a caller deliberately performs this one-time activation step.
    """
    require_synthetic_account_id(account_id)
    record = await activate_isolated_authority(
        account_id=account_id,
        artifacts_root=artifacts_root,
        store=activation_store or SyntheticActivationStore(artifacts_root),
    )
    assert isinstance(record, SyntheticActivationRecord)
    return record


async def select_synthetic_clerk_runtime(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    activation_store: SyntheticActivationStore | None = None,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository] = open_repository,
    startup_recovery_timeout_s: float = DEFAULT_STARTUP_RECOVERY_TIMEOUT_S,
) -> ActiveClerkRuntime:
    """Recover one explicit synthetic account without consulting Alpaca.

    The caller provides a synthetic read/trade pair.  Identity, activation and
    the opened repository must agree before a Clerk is returned.
    """
    try:
        require_synthetic_account_id(account_id)
        ports = bind_synthetic_ports(account_id=account_id, read=read, trade=trade)
        observed = await ports.read.get_account()
        if observed.account_id != account_id:
            raise AccountAuthorityIdentityError("synthetic account probe disagrees with authority key")
    except (AccountAuthorityIdentityError, ValueError) as exc:
        return unavailable_runtime(
            "SYNTHETIC_PORT_ACCOUNT_MISMATCH",
            account_id=account_id,
            recovery=str(exc),
        )

    store = activation_store or SyntheticActivationStore(artifacts_root)
    try:
        activation = store.latest(account_id)
    except SyntheticActivationInvalid as exc:
        return unavailable_runtime(
            "SYNTHETIC_ACTIVATION_RECORD_INVALID",
            account_id=account_id,
            recovery=str(exc),
            activation_detected=True,
        )
    if activation is None:
        return unavailable_runtime(
            "SYNTHETIC_ACTIVATION_REQUIRED",
            account_id=account_id,
            recovery="Explicitly activate this sim: account before composing its Clerk.",
        )

    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    try:
        repository = repository_opener(account_id, artifacts_root)
        meta = repository.control_meta_snapshot()
        if (
            meta.authority_generation != activation.authority_generation
            or meta.db_identity_token != activation.db_identity_token
        ):
            raise SyntheticActivationInvalid("synthetic activation does not match repository identity")
        intake = ReentrantAsyncLock()
        guarded_read, guarded_trade = guard_broker_ports(
            read=ports.read,
            trade=ports.trade,
            intake=intake,
        )
        facade = SqliteAlpacaClerkFacade(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            intake=intake,
            authority_kind="synthetic",
            # A simulator is a paper environment by construction (ADR 0054).
            account_mode="paper",
            program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
        )
        sweep = ReconciliationSweep(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            intake=intake,
            # The sweep is the sole automatic reconciler; publishing its
            # verdict is what lets pure panel reads project real custody
            # instead of answering `stale` forever (#1776 WP2).
            on_result=facade.publish_sweep_reconciliation,
            # ADR 0050: no on_lease_revived here, deliberately. Lease
            # *revival* applies to this synthetic heartbeat like any other,
            # but the post-revival recovery pass is real-paper-scoped (the
            # boot-recovery candidates come from the account authority, not
            # per-strategy synthetic repos), so a revived synthetic lease
            # relies on the boot scan for its terminal-evidence closure —
            # the same posture every authority had before ADR 0050.
        )
        sweep.start_lease_heartbeat()
        await asyncio.wait_for(facade.recover(), timeout=startup_recovery_timeout_s)
    except Exception as exc:
        if sweep is not None:
            await sweep.stop()
        if repository is not None:
            repository.close()
        return unavailable_runtime(
            "SYNTHETIC_CLERK_STARTUP_FAILED",
            account_id=account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    return ActiveClerkRuntime(
        authority_kind="synthetic",
        clerk=facade,
        sweep=sweep,
        _sqlite_repository=repository,
        account_id=account_id,
        account_authority_kind="synthetic",
    )


class ClerkAuthorityRegistry:
    """In-process registry keyed by the exact account authority identity."""

    def __init__(self) -> None:
        self._runtimes: dict[str, ActiveClerkRuntime] = {}

    def register(self, runtime: ActiveClerkRuntime) -> None:
        account_id = runtime.selected_account_id
        if runtime.clerk is None or account_id is None:
            raise ValueError("only an active account-scoped Clerk can be registered")
        existing = self._runtimes.get(account_id)
        if existing is not None and existing is not runtime:
            raise ValueError(f"account authority {account_id!r} is already registered")
        self._runtimes[account_id] = runtime

    def resolve(self, account_id: str) -> ActiveClerkRuntime | None:
        return self._runtimes.get(account_id)

    def unregister(self, account_id: str) -> ActiveClerkRuntime | None:
        """Remove one exact authority without perturbing other accounts."""
        return self._runtimes.pop(account_id, None)

    def synthetic_runtimes(self) -> tuple[ActiveClerkRuntime, ...]:
        """Return the isolated runtimes that must be closed at shutdown."""
        return tuple(
            runtime
            for runtime in self._runtimes.values()
            if runtime.authority_kind == "synthetic"
        )

    def clear(self) -> None:
        self._runtimes.clear()


_runtime: ActiveClerkRuntime | None = None
_authority_registry = ClerkAuthorityRegistry()


def get_active_clerk_runtime() -> ActiveClerkRuntime | None:
    return _runtime


def active_program_leg_policy() -> ProgramLegPolicy:
    """The active authority's leg policy; regular-only while none is active."""
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.clerk is None:
        return ProgramLegPolicy.regular_only()
    return runtime.clerk.program_leg_policy


def primary_custody_world() -> CustodyWorld | None:
    """The world the primary authority custodies in, or ``None`` while none is installed.

    Gates that relabel or relax on the shadow world read this; a caller that
    would have to *guess* where evidence goes must refuse on ``None`` instead
    (``bot_binding_authority.primary_custody_kind``).
    """
    runtime = get_active_clerk_runtime()
    kind = None if runtime is None else runtime.selected_account_authority_kind
    return kind if kind in ("real_paper", "shadow", "real_live") else None


def custody_world_or_paper(world: CustodyWorld | None) -> CustodyWorld:
    """Label a world that may be absent, falling back to the paper world.

    ``None`` from :func:`primary_custody_world` means no authority is
    installed, so there is no world to name; the label falls back to the
    paper world because no other world is constructible without one (the
    synthetic world is never primary). Takes the already-read world rather
    than re-reading it, so one surface's answer cannot change mid-request —
    and so the coercion is written here once instead of at each caller.

    Labelling only. A caller that would have to *guess where evidence goes*
    must refuse on ``None`` instead (``bot_binding_authority.primary_custody_kind``).
    """
    return world or "real_paper"


def set_active_clerk_runtime(runtime: ActiveClerkRuntime | None) -> None:
    global _runtime
    _runtime = runtime
    _authority_registry.clear()
    # The legacy real-paper compatibility seam can hold a test double before
    # account configuration has selected a concrete authority.  Such a value
    # remains readable through ``get_alpaca_clerk`` but must never become an
    # account-keyed runtime: only a concrete account identity may enter the
    # registry used by new custody paths.
    if runtime is not None and runtime.clerk is not None and runtime.selected_account_id is not None:
        _authority_registry.register(runtime)


def register_clerk_runtime(runtime: ActiveClerkRuntime) -> None:
    """Add an authority without replacing the real-paper compatibility selection."""
    _authority_registry.register(runtime)


def get_clerk_runtime(account_id: str) -> ActiveClerkRuntime | None:
    """Resolve one authority by exact account key; there is no fallback."""
    return _authority_registry.resolve(account_id)


def unregister_clerk_runtime(account_id: str) -> ActiveClerkRuntime | None:
    """Remove one exact non-primary authority after its owner releases it."""
    runtime = _authority_registry.resolve(account_id)
    if runtime is _runtime:
        raise ValueError("the primary Clerk runtime cannot be unregistered by account")
    return _authority_registry.unregister(account_id)


async def close_synthetic_clerk_runtimes() -> None:
    """Drain and close every registered synthetic runtime exactly once."""
    runtimes = _authority_registry.synthetic_runtimes()
    for runtime in runtimes:
        account_id = runtime.selected_account_id
        if account_id is not None:
            _authority_registry.unregister(account_id)
    for runtime in runtimes:
        await runtime.close()


def get_alpaca_clerk() -> ActiveAlpacaClerk | None:
    """Return the primary account authority, if installed: real paper, or the shadow of a live account.

    New callers that possess an account identity must use
    :func:`get_clerk_runtime`; this helper must never return a synthetic
    Clerk to a real-account caller by accident. Paper-only surfaces keep
    refusing on the facade's ``account_mode`` -- a shadow authority answers
    ``"live"``.
    """
    if _runtime is None or _runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES:
        return None
    return _runtime.clerk


def set_alpaca_clerk(clerk: ActiveAlpacaClerk | None) -> None:
    """Compatibility test seam backed by the sole active-runtime registry."""
    set_active_clerk_runtime(
        None if clerk is None else ActiveClerkRuntime(authority_kind="sqlite", clerk=clerk)
    )


def reset_alpaca_clerk_for_testing() -> None:
    set_active_clerk_runtime(None)


__all__ = [
    "DEFAULT_STARTUP_RECOVERY_TIMEOUT_S",
    "ActiveAlpacaClerk",
    "ActiveClerkRuntime",
    "AuthorityKind",
    "ClerkAuthorityRegistry",
    "ClerkStartupFailure",
    "activate_shadow_clerk_authority",
    "activate_synthetic_clerk_authority",
    "active_program_leg_policy",
    "close_synthetic_clerk_runtimes",
    "custody_world_or_paper",
    "get_active_clerk_runtime",
    "get_alpaca_clerk",
    "get_clerk_runtime",
    "primary_custody_world",
    "register_clerk_runtime",
    "reset_alpaca_clerk_for_testing",
    "select_active_clerk_runtime",
    "select_live_clerk_runtime",
    "select_synthetic_clerk_runtime",
    "set_active_clerk_runtime",
    "set_alpaca_clerk",
    "unregister_clerk_runtime",
]
