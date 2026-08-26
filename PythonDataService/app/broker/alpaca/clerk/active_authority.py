"""Boot-time selection and process registry for the SQLite Alpaca Clerk.

The broker account is resolved before any writer is constructed. A valid
activation record selects SQLite; every other activation state selects no
custody authority.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    AccountAuthorityKind,
    bind_real_alpaca_ports,
    bind_synthetic_ports,
    require_synthetic_account_id,
)
from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
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
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import (
    DEFAULT_LEASE_TTL_MS,
    AlreadyInitialized,
    ClerkSqliteRepository,
    ExecutionLeaseHeld,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.synthetic_activation import (
    SyntheticActivationInvalid,
    SyntheticActivationRecord,
    SyntheticActivationStore,
)
from app.broker.alpaca.clerk.trade_evidence import (
    SqliteTradeUpdateEvidenceSink,
    TradeUpdateEvidenceSink,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

AuthorityKind = Literal["sqlite", "synthetic", "unavailable"]
DEFAULT_STARTUP_RECOVERY_TIMEOUT_S = 60.0
DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S = DEFAULT_LEASE_TTL_MS / 1000 + 5.0
DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S = DEFAULT_LEASE_TTL_MS / 1000


class BackgroundSweep(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


class ActivationResolver(Protocol):
    def latest(self, account_id: str) -> ActivationRecord | None: ...

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> ActivationRecord | None: ...


@dataclass(frozen=True)
class ClerkStartupFailure:
    """Fail-closed account impact exposed when no mutating Clerk is installed."""

    reason_code: str
    account_id: str | None
    scope: Literal["ACCOUNT_CLERK"]
    impact: str
    recovery: str
    observed_at_ms: int
    activation_detected: bool = False
    authority_generation: int | None = None
    db_identity_token: str | None = None


@dataclass
class ActiveClerkRuntime:
    """Everything main.py needs after one authority selection."""

    authority_kind: AuthorityKind
    clerk: ActiveAlpacaClerk | None = None
    sweep: BackgroundSweep | None = None
    evidence_sink: TradeUpdateEvidenceSink | None = None
    startup_failure: ClerkStartupFailure | None = None
    _sqlite_repository: ClerkSqliteRepository | None = None
    account_id: str | None = None
    account_authority_kind: AccountAuthorityKind | None = None

    @property
    def sqlite_repository(self) -> ClerkSqliteRepository | None:
        """Return the active SQLite read authority, never a latent database."""
        if self.authority_kind not in {"sqlite", "synthetic"}:
            return None
        return self._sqlite_repository

    async def close(self) -> None:
        if self.sweep is not None:
            await self.sweep.stop()
        if isinstance(self.clerk, SqliteAlpacaClerkFacade):
            await self.clerk.drain_effects()
        if self._sqlite_repository is not None:
            self._sqlite_repository.close()
            self._sqlite_repository = None

    @property
    def selected_account_id(self) -> str | None:
        """Return the explicit composition key, never a global default."""
        if self.account_id is not None:
            return self.account_id
        clerk_account_id = getattr(self.clerk, "account_id", None)
        return clerk_account_id if isinstance(clerk_account_id, str) else None

    @property
    def selected_account_authority_kind(self) -> AccountAuthorityKind | None:
        """Return the closed authority kind used by read-model contracts."""
        if self.account_authority_kind is not None:
            return self.account_authority_kind
        return "synthetic" if self.authority_kind == "synthetic" else (
            "real_paper" if self.authority_kind == "sqlite" else None
        )


def _open_repository(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.open(
        account_id=account_id,
        artifacts_root=artifacts_root,
    )


async def _open_repository_after_lease_expiry(
    opener: Callable[[str, Path], ClerkSqliteRepository],
    *,
    account_id: str,
    artifacts_root: Path,
    wait_timeout_s: float,
    retry_interval_s: float,
) -> ClerkSqliteRepository:
    """Retry only the expected crashed-process lease handoff condition."""
    if wait_timeout_s < 0 or retry_interval_s <= 0:
        raise ValueError("execution lease wait must be non-negative with a positive retry interval")
    deadline = asyncio.get_running_loop().time() + wait_timeout_s
    while True:
        try:
            return opener(account_id, artifacts_root)
        except ExecutionLeaseHeld:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(retry_interval_s, remaining))


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
) -> ActiveClerkRuntime:
    """Resolve the account, validate activation, and construct one authority."""
    try:
        account = await read.get_account()
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
    if account.account_mode != "paper":
        return _unavailable(
            "LIVE_ACCOUNT_REFUSED",
            account_id=account.account_id,
            recovery="Use the separately supervised paper-account cutover workflow.",
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

    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    try:
        repository = await _open_repository_after_lease_expiry(
            repository_opener,
            account_id=account.account_id,
            artifacts_root=artifacts_root,
            wait_timeout_s=execution_lease_wait_timeout_s,
            retry_interval_s=execution_lease_retry_interval_s,
        )
        meta = repository.control_meta_snapshot()
        resolved = store.resolve(
            account.account_id,
            meta.authority_generation,
            meta.db_identity_token,
            artifacts_root,
        )
        if resolved is None:
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")
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
            stream_health=stream_health_gate,
            intake=intake,
        )
        # Keep the execution lease alive across the (possibly slow) startup
        # recovery passes. The reconcile loop still starts after boot recovery
        # in main.py, but the lease heartbeat must begin now so a clean-account
        # boot whose recovery only reads from the broker cannot let the lease
        # expire before the sweep is running.
        sweep = ReconciliationSweep(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            intake=intake,
            # The sweep is the sole automatic reconciler; publishing its
            # verdict is what lets pure panel reads project real custody
            # instead of answering `stale` forever (#1776 WP2).
            on_result=facade.publish_reconciliation,
        )
        sweep.start_lease_heartbeat()
        await asyncio.wait_for(
            facade.recover(),
            timeout=startup_recovery_timeout_s,
        )
    except Exception as exc:
        if sweep is not None:
            await sweep.stop()
        if repository is not None:
            repository.close()
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
        clerk=facade,
        sweep=sweep,
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=repository,
            intake=facade.intake,
            reconciler=facade,
        ),
        _sqlite_repository=repository,
        account_id=account.account_id,
        account_authority_kind="real_paper",
    )


def _unavailable(
    reason_code: str,
    *,
    account_id: str | None,
    recovery: str,
    activation_detected: bool = False,
    authority_generation: int | None = None,
    db_identity_token: str | None = None,
) -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="unavailable",
        startup_failure=ClerkStartupFailure(
            reason_code=reason_code,
            account_id=account_id,
            scope="ACCOUNT_CLERK",
            impact="Broker-mutating Alpaca Clerk capability is not installed.",
            recovery=recovery,
            observed_at_ms=now_ms_utc(),
            activation_detected=activation_detected,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
        ),
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
    try:
        repository = ClerkSqliteRepository.initialize(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )
    except AlreadyInitialized:
        # A process can crash after durable repository initialization but before
        # activation-record append. A later explicit activation must complete
        # that same repository fence rather than silently selecting it at boot.
        repository = ClerkSqliteRepository.open(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )
    try:
        meta = repository.control_meta_snapshot()
        store = activation_store or SyntheticActivationStore(artifacts_root)
        prior = store.latest(account_id)
        if prior is not None:
            if (
                prior.authority_generation == meta.authority_generation
                and prior.db_identity_token == meta.db_identity_token
            ):
                # A process can restart after the activation proof was fsync'd
                # but before the in-memory authority registry was restored.
                # Reusing this exact proof is safe; appending it again would
                # violate the activation ledger's monotonic generation fence.
                return prior
            raise SyntheticActivationInvalid(
                "synthetic activation does not match repository identity"
            )
        record = SyntheticActivationRecord.create(
            account_id=account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            activated_at_ms=now_ms_utc(),
        )
        store.append(record)
        return record
    finally:
        repository.close()


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
        )
        sweep = ReconciliationSweep(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            intake=intake,
            # The sweep is the sole automatic reconciler; publishing its
            # verdict is what lets pure panel reads project real custody
            # instead of answering `stale` forever (#1776 WP2).
            on_result=facade.publish_reconciliation,
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
    """Return only the real-paper compatibility authority, if installed.

    New callers that possess an account identity must use
    :func:`get_clerk_runtime`; this legacy helper must never return a synthetic
    Clerk to a real-paper caller by accident.
    """
    if _runtime is None or _runtime.authority_kind != "sqlite":
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
    "activate_synthetic_clerk_authority",
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
