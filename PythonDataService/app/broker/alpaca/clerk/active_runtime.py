"""Composition layer for one activated Alpaca custody authority.

Selection-free on purpose: this module knows how to open an account's
repository and stand up its Clerk, sweep and hold sync, but never which
account or authority a boot should choose. That keeps it importable by
every selector -- real paper, shadow and synthetic -- with no import cycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityKind,
    AccountBoundBrokerPorts,
)
from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import (
    ReconciliationListener,
    ReconciliationSweep,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    DEFAULT_LEASE_TTL_MS,
    AlreadyInitialized,
    ClerkSqliteRepository,
    ExecutionLeaseHeld,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.stream_health_sync import StreamHealthHoldSync
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationRecord,
    IsolatedActivationStore,
)
from app.broker.alpaca.clerk.trade_evidence import TradeUpdateEvidenceSink
from app.broker.alpaca.symbol_validity import SymbolValidityProbe, SymbolValidityStore
from app.utils.timestamps import now_ms_utc

AuthorityKind = Literal["sqlite", "synthetic", "shadow", "unavailable"]
# The authorities whose read model is one account-scoped SQLite database.
_REPOSITORY_BACKED: frozenset[str] = frozenset({"sqlite", "synthetic", "shadow"})
_ACCOUNT_KIND_BY_AUTHORITY: dict[str, AccountAuthorityKind] = {
    "sqlite": "real_paper",
    "synthetic": "synthetic",
    "shadow": "shadow",
}
DEFAULT_STARTUP_RECOVERY_TIMEOUT_S = 60.0
DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S = DEFAULT_LEASE_TTL_MS / 1000 + 5.0
DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S = DEFAULT_LEASE_TTL_MS / 1000


class BackgroundSweep(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


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
    hold_sync: StreamHealthHoldSync | None = None
    evidence_sink: TradeUpdateEvidenceSink | None = None
    startup_failure: ClerkStartupFailure | None = None
    _sqlite_repository: ClerkSqliteRepository | None = None
    account_id: str | None = None
    account_authority_kind: AccountAuthorityKind | None = None

    @property
    def sqlite_repository(self) -> ClerkSqliteRepository | None:
        """Return the active SQLite read authority, never a latent database."""
        if self.authority_kind not in _REPOSITORY_BACKED:
            return None
        return self._sqlite_repository

    def start_hold_sync(self) -> None:
        """Begin sampling stream health, once both providers are installed.

        Separate from selection because the ``trade_updates`` consumer is
        registered after the selector returns; see the construction site.
        """
        if self.hold_sync is not None:
            self.hold_sync.start()

    async def close(self) -> None:
        if self.hold_sync is not None:
            await self.hold_sync.stop()
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
        return _ACCOUNT_KIND_BY_AUTHORITY.get(self.authority_kind)


def open_repository(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
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


@dataclass(frozen=True)
class _ComposedAuthority:
    repository: ClerkSqliteRepository
    facade: SqliteAlpacaClerkFacade
    sweep: ReconciliationSweep
    hold_sync: StreamHealthHoldSync


async def compose_repository_runtime(
    *,
    ports: AccountBoundBrokerPorts,
    authority_kind: Literal["sqlite", "shadow"],
    account_mode: Literal["paper", "live"],
    artifacts_root: Path,
    verify_activation: Callable[[ControlMetaSnapshot], None],
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
    sweep_listener: ReconciliationListener | None = None,
) -> _ComposedAuthority:
    """Open the account's repository and stand up its Clerk, sweep and hold sync.

    Shared by the real-paper and shadow authorities; on any failure every
    handle opened here is closed before the exception propagates, so the
    caller only maps it to a startup refusal.
    """
    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    hold_sync: StreamHealthHoldSync | None = None
    try:
        repository = await _open_repository_after_lease_expiry(
            repository_opener,
            account_id=ports.account_id,
            artifacts_root=artifacts_root,
            wait_timeout_s=execution_lease_wait_timeout_s,
            retry_interval_s=execution_lease_retry_interval_s,
        )
        verify_activation(repository.control_meta_snapshot())
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
            authority_kind=authority_kind,
            # Proven above from the broker's own account read, not inferred.
            account_mode=account_mode,
            program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
        )
        publish = facade.publish_sweep_reconciliation
        on_result: ReconciliationListener = (
            publish if sweep_listener is None else (lambda result: sweep_listener(publish(result)))
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
            on_result=on_result,
            # Custody first, evidence second: the symbol-validity probe runs
            # only after a succeeded pass, through the same guarded read port,
            # and records durably what the read path may then consume (#1795).
            after_pass=(
                SymbolValidityProbe(
                    store=SymbolValidityStore(artifacts_root),
                    read=guarded_read,
                    roster_symbols=roster_symbols,
                ).run_due
                if roster_symbols is not None
                else None
            ),
        )
        sweep.start_lease_heartbeat()
        # #1777 WP4: the stream-health hold runs on its own fixed cadence,
        # never the reconcile loop's -- a backoff that reaches 300 s on
        # failure, exactly when a channel is down, must not delay a hold
        # raise or release.
        #
        # Constructed here, but deliberately *not* started: one of its two
        # providers (the trade_updates consumer) is registered by main.py
        # only after this function returns, and this function then awaits
        # startup recovery. Sampling before then reads "consumer is not
        # running" -- indistinguishable from a real outage -- and would
        # persist a false account-wide hold on every boot. main.py starts
        # it via `start_hold_sync()` once the provider exists.
        hold_sync = StreamHealthHoldSync(repo=repository, gate=stream_health_gate)
        await asyncio.wait_for(
            facade.recover(),
            timeout=startup_recovery_timeout_s,
        )
        return _ComposedAuthority(
            repository=repository,
            facade=facade,
            sweep=sweep,
            hold_sync=hold_sync,
        )
    except Exception:
        if hold_sync is not None:
            await hold_sync.stop()
        if sweep is not None:
            await sweep.stop()
        if repository is not None:
            repository.close()
        raise


def unavailable_runtime(
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


async def activate_isolated_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    store: IsolatedActivationStore,
) -> IsolatedActivationRecord:
    """Initialize (or reopen) one isolated repository and durably activate it exactly once."""
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
            raise store.record_type.invalid_error(
                f"{store.record_type.label} does not match repository identity"
            )
        record = store.record_type.create(
            account_id=account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            activated_at_ms=now_ms_utc(),
        )
        store.append(record)
        return record
    finally:
        repository.close()


__all__ = [
    "DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S",
    "DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S",
    "DEFAULT_STARTUP_RECOVERY_TIMEOUT_S",
    "ActiveClerkRuntime",
    "AuthorityKind",
    "BackgroundSweep",
    "ClerkStartupFailure",
    "activate_isolated_authority",
    "compose_repository_runtime",
    "open_repository",
    "unavailable_runtime",
]
