"""Boot-time selection and process registry for the SQLite Alpaca Clerk.

The broker account is resolved before any writer is constructed. A valid
activation record selects SQLite; every other activation state selects no
custody authority.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    AccountAuthorityKind,
    AccountBoundBrokerPorts,
    bind_real_alpaca_ports,
    bind_shadow_ports,
    bind_synthetic_ports,
    require_synthetic_account_id,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
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
    SyntheticActivationInvalid,
    SyntheticActivationRecord,
    SyntheticActivationStore,
)
from app.broker.alpaca.clerk.trade_evidence import (
    NullTradeUpdateEvidenceSink,
    SqliteTradeUpdateEvidenceSink,
    TradeUpdateEvidenceSink,
)
from app.broker.alpaca.symbol_validity import SymbolValidityProbe, SymbolValidityStore
from app.broker.contract.errors import BrokerAccountModeDisagreement, BrokerError
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class _ComposedAuthority:
    repository: ClerkSqliteRepository
    facade: SqliteAlpacaClerkFacade
    sweep: ReconciliationSweep
    hold_sync: StreamHealthHoldSync


async def _compose_repository_runtime(
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
        return await _select_shadow_clerk_runtime(
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


async def _select_shadow_clerk_runtime(
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
) -> ActiveClerkRuntime:
    """Compose the Shadow Account Authority for a live account (ADR 0059 D2).

    The live trade port is never bound: the shadow world's trade port is
    ``NoSubmitAlpacaTradePort``. Real-money custody stays unconstructible
    until slice 7 admits an armed instance.
    """
    try:
        await verify_shadow_namespace_empty(read)
    except (ShadowNamespacePoisoned, ShadowNamespaceUnproven) as exc:
        logger.warning(
            "live account refused for shadow: order namespace not proven empty",
            extra={"action": "shadow_namespace_refused", "reason_code": exc.reason_code},
        )
        return _unavailable(exc.reason_code, account_id=account.account_id, recovery=str(exc))
    except BrokerError as exc:
        return _unavailable(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=account.account_id,
            recovery=f"Restore the live account's order-history read: {exc}",
        )
    shadow = compose_shadow_ports(
        live_read=read,
        live_account_id=account.account_id,
        artifacts_root=artifacts_root,
    )
    ports = bind_shadow_ports(account_id=shadow.account_id, read=shadow.read, trade=shadow.trade)
    store = ShadowActivationStore(artifacts_root)
    try:
        activation = store.latest(shadow.account_id)
    except ShadowActivationInvalid as exc:
        return _unavailable(
            "SHADOW_ACTIVATION_RECORD_INVALID",
            account_id=shadow.account_id,
            recovery=str(exc),
            activation_detected=True,
        )
    if activation is None:
        return _unavailable(
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
        composed = await _compose_repository_runtime(
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
        )
    except Exception as exc:
        logger.warning(
            "Shadow Alpaca Clerk failed startup; no authority installed",
            extra={"action": "shadow_active_clerk_startup_failed", "account_id": shadow.account_id},
            exc_info=True,
        )
        return _unavailable(
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
        evidence_sink=NullTradeUpdateEvidenceSink(),
        _sqlite_repository=composed.repository,
        account_id=shadow.account_id,
        account_authority_kind="shadow",
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


async def _activate_isolated_authority(
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
    record = await _activate_isolated_authority(
        account_id=shadow_account_id_for_live_account(live_account_id),
        artifacts_root=artifacts_root,
        store=activation_store or ShadowActivationStore(artifacts_root),
    )
    assert isinstance(record, ShadowActivationRecord)
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
