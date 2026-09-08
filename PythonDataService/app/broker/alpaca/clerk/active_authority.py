"""Boot-time selection and process registry for the SQLite Alpaca Clerk.

The broker account is resolved before any writer is constructed. A paper
account with a valid activation record selects SQLite; a live account selects
the Shadow Account Authority behind its own activation fence (ADR 0059 D2),
which reads the live account but submits nothing. Every other activation state
selects no custody authority.

The composition each selection ends in lives in ``active_runtime``, and the
shadow world's own boot story in ``shadow_authority``; both are re-exported
here so this module stays the single import surface callers already use.
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
    ActiveClerkRuntime,
    AuthorityKind,
    ClerkStartupFailure,
    _activate_isolated_authority,
    _compose_repository_runtime,
    _open_repository,
    _unavailable,
)
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
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
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


async def select_active_clerk_runtime(
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    activation_store: ActivationResolver | None = None,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository] = _open_repository,
    startup_recovery_timeout_s: float = DEFAULT_STARTUP_RECOVERY_TIMEOUT_S,
    execution_lease_wait_timeout_s: float = DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S,
    execution_lease_retry_interval_s: float = DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S,
    stream_health_gate: StreamHealthGate | None = None,
    roster_symbols: Callable[[], Sequence[str]] | None = None,
) -> ActiveClerkRuntime:
    """Resolve the account, validate activation, and construct one authority.

    ``roster_symbols`` opts the sweep into the post-pass symbol-validity probe
    (#1795): a callable returning the fleet's bound symbols, injected here so
    the clerk layer never imports the bot-registration services. ``None`` (the
    default, and every test/synthetic path) constructs no probe.
    """
    try:
        account = await read.get_account()
    except BrokerAccountModeDisagreement as exc:
        logger.warning(
            "Alpaca configured mode and observed account disagree; Clerk unavailable",
            extra={"action": "active_clerk_mode_disagreement", "detail": exc.detail},
        )
        return _unavailable(
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
        return _unavailable(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=None,
            recovery=f"Restore the Alpaca account identity probe: {exc}",
        )
    if account.account_mode == "live":
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
        )
    try:
        ports = bind_real_alpaca_ports(
            account_id=account.account_id,
            read=read,
            trade=trade,
        )
    except AccountAuthorityIdentityError as exc:
        return _unavailable(
            "REAL_PORT_REJECTED_SYNTHETIC_ACCOUNT",
            account_id=account.account_id,
            recovery=str(exc),
        )

    store = activation_store or ActivationStore(artifacts_root / "accounts" / "alpaca")
    try:
        activation = store.latest(account.account_id)
    except ActivationRecordInvalid as exc:
        return _unavailable(
            "ACTIVATION_RECORD_INVALID",
            account_id=account.account_id,
            recovery=str(exc),
            activation_detected=True,
        )

    if activation is not None and DeveloperCleanSlateResetRegistry(
        artifacts_root / "accounts" / "alpaca"
    ).authorizes_reinitialize(
        account_id=account.account_id,
        prior_authority_generation=activation.authority_generation,
        artifacts_root=artifacts_root,
    ):
        return _unavailable(
            "DEVELOPER_RESET_REACTIVATION_REQUIRED",
            account_id=account.account_id,
            recovery=(
                "This activated authority was moved aside by a developer clean-slate "
                "reset. Regenerate it, then complete a new paper cutover before startup."
            ),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    if activation is None:
        return _unavailable(
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
        composed = await _compose_repository_runtime(
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
        return _unavailable(
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
    record = await _activate_isolated_authority(
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
    repository_opener: Callable[[str, Path], ClerkSqliteRepository] = _open_repository,
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
        return _unavailable(
            "SYNTHETIC_PORT_ACCOUNT_MISMATCH",
            account_id=account_id,
            recovery=str(exc),
        )

    store = activation_store or SyntheticActivationStore(artifacts_root)
    try:
        activation = store.latest(account_id)
    except SyntheticActivationInvalid as exc:
        return _unavailable(
            "SYNTHETIC_ACTIVATION_RECORD_INVALID",
            account_id=account_id,
            recovery=str(exc),
            activation_detected=True,
        )
    if activation is None:
        return _unavailable(
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
        return _unavailable(
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
    if _runtime is None or _runtime.authority_kind not in {"sqlite", "shadow"}:
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
    "get_active_clerk_runtime",
    "get_alpaca_clerk",
    "get_clerk_runtime",
    "register_clerk_runtime",
    "reset_alpaca_clerk_for_testing",
    "select_active_clerk_runtime",
    "select_synthetic_clerk_runtime",
    "set_active_clerk_runtime",
    "set_alpaca_clerk",
    "unregister_clerk_runtime",
]
