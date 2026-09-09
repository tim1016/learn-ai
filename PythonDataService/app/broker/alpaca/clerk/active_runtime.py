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
from typing import TYPE_CHECKING, Final, Literal, Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityKind,
    AccountBoundBrokerPorts,
)
from app.broker.alpaca.clerk.active_protocol import ActiveAlpacaClerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid
from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    guard_broker_ports,
    guard_broker_read_port,
)
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
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
from app.broker.contract.ports import BrokerReadPort
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    # Type-only, and load-bearing: ``live_envelope`` and ``clerk/sqlite`` are
    # mutually dependent (``live_envelope`` reads the loss-hold reason code out
    # of ``sqlite.uncertainty_causes``; ``sqlite.repository`` reads
    # ``EnvelopeReservation`` back out of ``live_envelope``). A plain import
    # here sorts ABOVE the ``clerk.sqlite`` imports below, so it would run
    # ``sqlite/__init__`` -- and therefore ``repository`` -- while
    # ``live_envelope`` is still half-built, and ``EnvelopeReservation`` would
    # not exist yet. Verified: making it a plain import fails
    # ``import app.main`` with exactly that ImportError. The cycle, not the
    # sort order, is the thing to fix, and it is not this slice's to fix.
    # ``live_arming_ledger`` imports ``live_envelope``, so it is here for
    # exactly the same reason.
    from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
    from app.broker.alpaca.clerk.sqlite.live_envelope_sync import InstanceSeals

AuthorityKind = Literal["sqlite", "synthetic", "shadow", "unavailable"]
# The authorities whose read model is one account-scoped SQLite database.
_REPOSITORY_BACKED: frozenset[str] = frozenset({"sqlite", "synthetic", "shadow"})
SQLITE_FACADE_AUTHORITIES: Final[frozenset[str]] = frozenset({"sqlite", "shadow"})
"""Authority kinds whose clerk is a real-account ``SqliteAlpacaClerkFacade``.

The one closed set every operator surface selects the primary authority
through. The isolated ``synthetic`` world is deliberately absent: its facade
is composed per instance and selected explicitly, never found by an
account-scoped selector.
"""
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


def _ordered_taps(
    *,
    envelope_sync: BackgroundSweep | None,
    hold_sync: BackgroundSweep | None,
    sweep: BackgroundSweep | None,
) -> tuple[BackgroundSweep, ...]:
    """The background taps an authority owns, in start order, absent ones dropped.

    The order is semantic and stated here once: the envelope sync goes first
    because its own projection reader is a second handle on the repository
    every other tap and the facade write through, so it is also the first to
    be stopped. Both stop sites -- the runtime's ``close()`` and the failed
    startup cleanup in :func:`compose_repository_runtime` -- read it from
    here rather than each keeping their own branch order true.
    """
    return tuple(tap for tap in (envelope_sync, hold_sync, sweep) if tap is not None)


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
    envelope_sync: LiveEnvelopeSync | None = None
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

    def _taps(self) -> tuple[BackgroundSweep, ...]:
        return _ordered_taps(
            envelope_sync=self.envelope_sync, hold_sync=self.hold_sync, sweep=self.sweep
        )

    def start_background_taps(self) -> None:
        """Start the taps that need nothing from boot recovery.

        The reconciliation sweep is deliberately absent: main.py starts it
        after boot recovery so the periodic pass cannot race the boot
        reconciliation (both call ``reconcile_once``), and binds the ADR 0050
        revival hook before that loop runs. Separate from selection because
        one of the stream-health sync's two providers -- the
        ``trade_updates`` consumer -- is registered after the selector
        returns; see the construction site. The envelope sync needs only the
        read port and could start earlier, but sharing this seam is what
        makes "did anything start the taps?" one question main.py answers in
        one place.
        """
        for tap in _ordered_taps(envelope_sync=self.envelope_sync, hold_sync=self.hold_sync, sweep=None):
            tap.start()

    async def close(self) -> None:
        # Every tap is stopped before the repository it writes to is closed,
        # in ``_ordered_taps``' declared order. ``stop()`` is terminal -- the
        # envelope sync closes its own projection reader there -- so the
        # handles are dropped afterwards and a second ``close()`` re-stops
        # nothing, exactly as ``_sqlite_repository`` below.
        for tap in self._taps():
            await tap.stop()
        self.envelope_sync = None
        self.hold_sync = None
        self.sweep = None
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
    envelope_sync: LiveEnvelopeSync | None


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
    live_envelope: LiveEnvelopeGate | None = None,
    envelope_read: BrokerReadPort | None = None,
    arming_ledger: LiveArmingLedger | None = None,
    arming_gate: ArmingGate | None = None,
    instance_seals: InstanceSeals | None = None,
) -> _ComposedAuthority:
    """Open the account's repository and stand up its Clerk, sweep and hold sync.

    Shared by the real-paper and shadow authorities; on any failure every
    handle opened here is closed before the exception propagates, so the
    caller only maps it to a startup refusal.

    ``envelope_read`` is the port the envelope observes when it is not the
    Clerk's own read port -- the shadow authority passes the live account's
    read so cash and positions are the real account's while custody stays
    synthesized.

    ``arming_ledger`` is the account's sealed-arming evidence (ADR 0059 D3). The
    envelope sync re-reads it every tick so an arming performed by the
    out-of-process CLI reaches the running gate within one cadence.

    ``arming_gate`` and ``instance_seals`` exist only on the live authority
    (ADR 0059 D11, slice 7): the gate the facade admits ENTERs against, and
    the runner's sealed bindings the sync reads beside the ledger every tick
    — injected as a callable, the ``roster_symbols`` pattern, so the clerk
    layer never learns the runner's root.
    """
    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    hold_sync: StreamHealthHoldSync | None = None
    envelope_sync: LiveEnvelopeSync | None = None
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
            live_envelope=live_envelope,
            live_arming=arming_gate,
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
        # it via `start_background_taps()` once the provider exists.
        hold_sync = StreamHealthHoldSync(repo=repository, gate=stream_health_gate)
        # ADR 0059 D4: the envelope's own fixed cadence, for the same reason
        # the hold sync has one -- the reconcile loop's backoff reaches 300 s
        # on failure, and a losing day must not wait that long to be judged.
        # Unstarted here too: `start_background_taps()` is the one start seam.
        # The envelope judges the account the money is in. Under shadow that
        # is the live account (cash and positions), not the synthesized
        # book, whose positions never mark to market.
        envelope_sync = (
            None
            if live_envelope is None
            else LiveEnvelopeSync(
                repo=repository,
                read=(
                    guarded_read
                    if envelope_read is None
                    else guard_broker_read_port(envelope_read, intake=intake)
                ),
                envelope=live_envelope,
                arming_ledger=arming_ledger,
                arming_gate=arming_gate,
                instance_seals=instance_seals,
            )
        )
        await asyncio.wait_for(
            facade.recover(),
            timeout=startup_recovery_timeout_s,
        )
        return _ComposedAuthority(
            repository=repository,
            facade=facade,
            sweep=sweep,
            hold_sync=hold_sync,
            envelope_sync=envelope_sync,
        )
    except Exception:
        # Whatever was built before the failure, stopped in the same declared
        # order the runtime's own ``close()`` uses -- a tap left running here
        # would outlive the repository closed on the next line.
        for tap in _ordered_taps(
            envelope_sync=envelope_sync, hold_sync=hold_sync, sweep=sweep
        ):
            await tap.stop()
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


def developer_reset_refusal(
    *,
    account_id: str,
    artifacts_root: Path,
    authority_generation: int,
    db_identity_token: str,
    cutover_noun: Literal["paper", "live"],
) -> ActiveClerkRuntime | None:
    """Refuse an activation a developer clean-slate reset moved aside, or admit it.

    ``None`` means the registry authorizes nothing against this generation and
    the boot may continue. Both sides of the live fork ask the same question of
    the same registry and answer with the same sentence; only the cutover the
    operator must redo differs, so ``cutover_noun`` is the whole difference and
    the guard is written once (ADR 0059 D10).
    """
    authorized = DeveloperCleanSlateResetRegistry(
        artifacts_root / "accounts" / "alpaca"
    ).authorizes_reinitialize(
        account_id=account_id,
        prior_authority_generation=authority_generation,
        artifacts_root=artifacts_root,
    )
    if not authorized:
        return None
    return unavailable_runtime(
        "DEVELOPER_RESET_REACTIVATION_REQUIRED",
        account_id=account_id,
        recovery=(
            "This activated authority was moved aside by a developer clean-slate reset. "
            f"Regenerate it, then complete a new {cutover_noun} cutover before startup."
        ),
        activation_detected=True,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
    )


def compose_failure_refusal(
    exc: BaseException,
    *,
    account_id: str,
    authority_generation: int,
    db_identity_token: str,
) -> ActiveClerkRuntime:
    """The one refusal for a composition that raised, on either side of the live fork.

    An ``ActivationRecordInvalid`` is the cutover record's fault and names
    itself; every other failure is the startup's. Both sides carried the same
    six-keyword call with the same ternary, which is how the two sentences
    would have drifted.
    """
    return unavailable_runtime(
        (
            "ACTIVATION_RECORD_INVALID"
            if isinstance(exc, ActivationRecordInvalid)
            else "SQLITE_CLERK_STARTUP_FAILED"
        ),
        account_id=account_id,
        recovery=str(exc),
        activation_detected=True,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
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
    "SQLITE_FACADE_AUTHORITIES",
    "ActiveClerkRuntime",
    "AuthorityKind",
    "BackgroundSweep",
    "ClerkStartupFailure",
    "activate_isolated_authority",
    "compose_repository_runtime",
    "open_repository",
    "unavailable_runtime",
]
