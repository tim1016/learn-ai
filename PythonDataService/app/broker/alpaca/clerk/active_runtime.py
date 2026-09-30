"""Composition layer for one activated Alpaca custody authority.

Selection-free on purpose: this module knows how to open an account's
repository and stand up its Clerk, sweep and hold sync, but never which
account or authority a boot should choose. That keeps it importable by
every selector -- real paper, shadow, real live and synthetic -- with no
import cycle.

It also hosts the two refusal shapes those selectors share --
``compose_failure_refusal`` and ``developer_reset_refusal`` -- for that same
reason: they are used by ``active_authority`` and by ``live_authority``, and
``live_authority`` is imported *by* ``active_authority``, so either selector
owning them would put the shared shape downstream of one of its two callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
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
from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
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
from app.broker.alpaca.clerk.sqlite.runtime import (
    SqliteAlpacaClerkFacade,
    StartupBrokerTruthUnavailable,
)
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection
from app.broker.alpaca.clerk.sqlite.stream_health_sync import StreamHealthHoldSync
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationRecord,
    IsolatedActivationStore,
)
from app.broker.alpaca.clerk.trade_evidence import TradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerError, BrokerRateLimited, BrokerUnreachable
from app.broker.contract.ports import BrokerReadPort
from app.utils.timestamps import Clock, now_ms_utc

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
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate

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
BROKER_UNREACHABLE_RECONNECTING: Final = "BROKER_UNREACHABLE_RECONNECTING"
"""The one startup failure that is not terminal (#2582).

Alpaca did not answer while the authority was selected or booted -- a network
failure, a timeout, its own server error or a rate limit
(:func:`transient_startup_failure`). Nothing about the account is wrong; the
composition root re-selects on a bounded backoff until Alpaca answers
(``authority_reconnect``). Every other startup failure stays terminal.
"""
_AWAITING_ACTIVATION_REASON_CODES: Final[frozenset[str]] = frozenset({
    "ACTIVATION_REQUIRED",
    "SHADOW_ACTIVATION_REQUIRED",
    "SYNTHETIC_ACTIVATION_REQUIRED",
    # ``live_envelope.LIVE_ENVELOPE_MISSING``; importing it here is the cycle
    # described at the top of this module.
    "LIVE_ENVELOPE_MISSING",
    "DEVELOPER_RESET_REACTIVATION_REQUIRED",
})
"""Refusals that are the owner's step still to take, not a failure (#2620).

The account was never activated, or its activation needs a value set or a
new cutover. Nothing broke, and the account's Settings is where the step is
taken; every other refusal means an authority that should serve does not.
"""


class StartupRecoveryTimedOut(TimeoutError):
    """Startup recovery did not finish inside its deadline (#2582).

    Recovery is paced by Alpaca's answers to the account's orders and
    positions, so running out of time is Alpaca answering too slowly -- the
    same transient failure as a single read timing out, and retried the same
    way.
    """

    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            f"Startup recovery did not finish within {timeout_s:g} seconds while reading Alpaca."
        )


class BackgroundSweep(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


def _ordered_taps(
    *,
    envelope_sync: BackgroundSweep | None,
    hold_sync: BackgroundSweep | None,
    sweep: BackgroundSweep | None,
    fee_sync: BackgroundSweep | None = None,
) -> tuple[BackgroundSweep, ...]:
    """The background taps an authority owns, in start order, absent ones dropped.

    The order is semantic and stated here once: the envelope sync goes first
    because its own projection reader is a second handle on the repository
    every other tap and the facade write through, so it is also the first to
    be stopped. Both stop sites -- the runtime's ``close()`` and the failed
    startup cleanup in :func:`compose_repository_runtime` -- read it from
    here rather than each keeping their own branch order true.
    """
    return tuple(tap for tap in (fee_sync, envelope_sync, hold_sync, sweep) if tap is not None)


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
    # How long Alpaca asked this Clerk to wait, when it rate-limited the
    # selection (HTTP 429 Retry-After); the reconnect waits at least this.
    retry_after_ms: int | None = None

    @property
    def reconnecting(self) -> bool:
        """Whether this failure is only Alpaca not answering yet, which startup retries."""
        return self.reason_code == BROKER_UNREACHABLE_RECONNECTING

    @property
    def awaiting_activation(self) -> bool:
        """Whether this is the owner's activation step still to take, not a failure."""
        return self.reason_code in _AWAITING_ACTIVATION_REASON_CODES


@dataclass
class ActiveClerkRuntime:
    """Everything main.py needs after one authority selection."""

    authority_kind: AuthorityKind
    clerk: ActiveAlpacaClerk | None = None
    sweep: BackgroundSweep | None = None
    hold_sync: StreamHealthHoldSync | None = None
    envelope_sync: LiveEnvelopeSync | None = None
    fee_sync: FeeEvidenceSync | None = None
    evidence_sink: TradeUpdateEvidenceSink | None = None
    startup_failure: ClerkStartupFailure | None = None
    _sqlite_repository: ClerkSqliteRepository | None = None
    account_id: str | None = None
    account_authority_kind: AccountAuthorityKind | None = None

    @property
    def reconnecting(self) -> bool:
        """No authority yet, only because Alpaca was unreachable at selection."""
        return self.clerk is None and self.startup_failure is not None and self.startup_failure.reconnecting

    @property
    def sqlite_repository(self) -> ClerkSqliteRepository | None:
        """Return the active SQLite read authority, never a latent database."""
        if self.authority_kind not in _REPOSITORY_BACKED:
            return None
        return self._sqlite_repository

    def _taps(self) -> tuple[BackgroundSweep, ...]:
        return _ordered_taps(
            envelope_sync=self.envelope_sync, hold_sync=self.hold_sync, sweep=self.sweep, fee_sync=self.fee_sync
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
        for tap in _ordered_taps(envelope_sync=self.envelope_sync, hold_sync=self.hold_sync, sweep=None, fee_sync=self.fee_sync):
            tap.start()

    async def close(self) -> None:
        # Every tap is stopped before the repository it writes to is closed,
        # in ``_ordered_taps``' declared order. ``stop()`` is terminal -- the
        # envelope sync closes its own projection reader there -- so the
        # handles are dropped afterwards and a second ``close()`` re-stops
        # nothing, exactly as ``_sqlite_repository`` below.
        for tap in self._taps():
            await tap.stop()
        self.fee_sync = None
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


async def open_repository_after_lease_expiry(
    opener: Callable[[str, Path], ClerkSqliteRepository],
    *,
    account_id: str,
    artifacts_root: Path,
    wait_timeout_s: float,
    retry_interval_s: float,
) -> ClerkSqliteRepository:
    """Retry only the expected crashed-process lease handoff condition.

    A restarted process meets its dead predecessor's lease on every account it
    reopens: that process renewed it and then died without releasing it, so it
    lapses within one lease lifetime. ``wait_timeout_s=0`` is a single attempt.
    """
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
    fee_sync: FeeEvidenceSync | None


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
    sweep_listener: ReconciliationListener | None = None,
    live_envelope: LiveEnvelopeGate | None = None,
    envelope_read: BrokerReadPort | None = None,
    arming_ledger: LiveArmingLedger | None = None,
    simulation_initial_cash: Decimal | None = None,
    initialize_reviewed_policy: Callable[[ClerkSqliteRepository], None] | None = None,
) -> _ComposedAuthority:
    """Open the account's repository and stand up its Clerk, sweep and hold sync.

    Shared by the real-paper and shadow authorities; on any failure every
    handle opened here is closed before the exception propagates, so the
    caller only maps it to a startup refusal.

    ``envelope_read`` is the port the envelope observes when it is not the
    Clerk's own read port -- the shadow authority passes the live account's
    read as the reference cash source. Simulated positions, fees and risk
    come exclusively from its own custody and retained market-data evidence.

    ``arming_ledger`` is the live account's historical arming evidence (ADR
    0059 D3), read once by the exit-terms upgrade to price a bot armed before
    exit terms existed. It grants nothing: no arming gate or per-tick arming
    refresh is composed any more (#2629).
    """
    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    hold_sync: StreamHealthHoldSync | None = None
    envelope_sync: LiveEnvelopeSync | None = None
    fee_sync: FeeEvidenceSync | None = None
    try:
        repository = await open_repository_after_lease_expiry(
            repository_opener,
            account_id=ports.account_id,
            artifacts_root=artifacts_root,
            wait_timeout_s=execution_lease_wait_timeout_s,
            retry_interval_s=execution_lease_retry_interval_s,
        )
        verify_activation(repository.control_meta_snapshot())
        if initialize_reviewed_policy is not None:
            initialize_reviewed_policy(repository)
        intake = ReentrantAsyncLock()
        guarded_read, guarded_trade = guard_broker_ports(
            read=ports.read,
            trade=ports.trade,
            intake=intake,
        )
        # ADR 0059 D4: the envelope's own fixed cadence, for the same reason
        # the stream-health hold sync below has one -- the reconcile loop's
        # backoff reaches 300 s on failure, and a losing day must not wait
        # that long to be judged. Unstarted here, like that sync:
        # `start_background_taps()` is the one start seam.
        # Shadow takes only reference cash from the real account. The shared
        # simulated projection values its own custody with retained marks.
        # Built before the facade, which asks it for a reading when an ENTER
        # waits on executions newer than the last one (#2623).
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
                custody_read=guarded_read,
                simulation=(SimulatedAccountProjection(repo=repository, artifacts_root=artifacts_root, initial_cash=simulation_initial_cash)
                    if repository.account_id.startswith(("sim:", "shadow:")) else None),
            )
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
            entry_reading=None if envelope_sync is None else envelope_sync.read_for_entry,
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
            # Retires a run whose in-process runner is gone (#2369).
            run_ownership=facade.run_ownership,
            # The facade's one pricing seam (#2440): what the sweep's
            # stuck-EXIT watchdog prices an extended-hours re-drive limit
            # from, and what an EXIT the sweep creates a reduction for is
            # re-priced from — the facade's sealed policy (re-resolved per
            # pass so a re-arm is picked up) and its live top-of-book quote
            # (#2229).
            pricing=facade.recovery_pricing,
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
        if not repository.account_id.startswith(("sim:", "shadow:")):
            fee_sync = FeeEvidenceSync(repo=repository, read=guarded_read)
        await asyncio.to_thread(facade.upgrade_legacy_exit_terms, arming_ledger)
        try:
            await asyncio.wait_for(
                facade.recover(),
                timeout=startup_recovery_timeout_s,
            )
        except TimeoutError as exc:
            raise StartupRecoveryTimedOut(startup_recovery_timeout_s) from exc
        return _ComposedAuthority(
            repository=repository,
            facade=facade,
            sweep=sweep,
            hold_sync=hold_sync,
            envelope_sync=envelope_sync,
            fee_sync=fee_sync,
        )
    except BaseException:
        # Whatever was built before the failure, stopped in the same declared
        # order the runtime's own ``close()`` uses -- a tap left running here
        # would outlive the repository closed on the next line. A cancelled
        # composition -- a reconnect interrupted by shutdown (#2582) --
        # releases its execution lease the same way.
        for tap in _ordered_taps(
            envelope_sync=envelope_sync, hold_sync=hold_sync, sweep=sweep, fee_sync=fee_sync
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
    retry_after_ms: int | None = None,
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
            retry_after_ms=retry_after_ms,
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


TransientStartupCause = BrokerUnreachable | BrokerRateLimited | StartupRecoveryTimedOut


def transient_startup_failure(exc: BaseException) -> TransientStartupCause | None:
    """The cause behind a failed startup, when Alpaca not answering yet is all it was.

    Transient -- retried by the reconnect: Alpaca unreachable, timing out or
    failing on its own side (``BrokerUnreachable``, which the Alpaca client
    raises for a network failure, a timeout and a 5xx), rate-limiting us
    (``BrokerRateLimited``), or startup recovery running out of time
    (``StartupRecoveryTimedOut``). Everything else is terminal: a refused
    credential, an answer no mapping recognized (the ``BrokerUnavailable``
    catch-all for an unexpected 404 or 409), evidence that cannot support a
    verdict, a broken repository.

    The failure may arrive wrapped -- startup recovery's own error, or boot
    recovery's preparation error raised from it -- so the explicit cause chain
    is followed to the first broker error, and that one decides.
    """
    link: BaseException | None = exc
    while link is not None:
        if isinstance(link, StartupRecoveryTimedOut):
            return link
        if isinstance(link, StartupBrokerTruthUnavailable):
            link = link.broker_error
            continue
        if isinstance(link, BrokerError):
            return link if isinstance(link, BrokerUnreachable | BrokerRateLimited) else None
        link = link.__cause__
    return None


def reconnecting_refusal(
    cause: TransientStartupCause,
    *,
    account_id: str | None,
    activation_detected: bool = False,
    authority_generation: int | None = None,
    db_identity_token: str | None = None,
) -> ActiveClerkRuntime:
    """The refusal a startup Alpaca did not answer installs while it reconnects."""
    return unavailable_runtime(
        BROKER_UNREACHABLE_RECONNECTING,
        account_id=account_id,
        recovery=(
            f"This Clerk could not read its account from Alpaca when it started: "
            f"{_sentence(cause)} It is reconnecting and will take over this account on its own "
            "once Alpaca answers; no restart is needed."
        ),
        activation_detected=activation_detected,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        retry_after_ms=cause.retry_after_ms if isinstance(cause, BrokerRateLimited) else None,
    )


def compose_failure_refusal(
    exc: BaseException,
    *,
    account_id: str,
    authority_generation: int | None,
    db_identity_token: str | None,
) -> ActiveClerkRuntime:
    """The one refusal for a composition that raised, on either side of the live fork.

    Alpaca not answering yet reconnects (``reconnecting_refusal``). An
    ``ActivationRecordInvalid`` is the cutover record's fault and names
    itself; every other failure is the startup's, and its copy says it is
    final and what ends it. Both sides carried the same six-keyword call with
    the same ternary, which is how the two sentences would have drifted.

    ``authority_generation`` and ``db_identity_token`` are ``None`` only when
    the control-meta read itself is what raised (#2620): the refusal then
    carries the account and says what happened, without inventing an
    identity it could not read.
    """
    transient = transient_startup_failure(exc)
    if transient is not None:
        return reconnecting_refusal(
            transient,
            account_id=account_id,
            activation_detected=True,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
        )
    return unavailable_runtime(
        (
            "ACTIVATION_RECORD_INVALID"
            if isinstance(exc, ActivationRecordInvalid)
            else "SQLITE_CLERK_STARTUP_FAILED"
        ),
        account_id=account_id,
        recovery=terminal_startup_recovery(exc),
        activation_detected=True,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
    )


def terminal_startup_recovery(cause: object) -> str:
    """The copy of a startup failure nothing will retry: its cause, and that a restart ends it."""
    return (
        f"This Clerk did not start: {_sentence(cause)} It will not retry on its own; "
        "restart the Clerk once that is fixed."
    )


def _sentence(cause: object) -> str:
    """A cause's own text as one sentence of the copy around it."""
    return f"{str(cause).rstrip('.')}."


async def activate_isolated_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    store: IsolatedActivationStore,
    clock: Clock = now_ms_utc,
    execution_lease_wait_timeout_s: float = 0.0,
    execution_lease_retry_interval_s: float = DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S,
) -> IsolatedActivationRecord:
    """Initialize (or reopen) one isolated repository and durably activate it exactly once.

    The lease wait applies to the reopen, as it does to every authority's
    opening (:func:`open_repository_after_lease_expiry`).
    """
    try:
        repository = ClerkSqliteRepository.initialize(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
    except AlreadyInitialized:
        # A process can crash after durable repository initialization but before
        # activation-record append. A later explicit activation must complete
        # that same repository fence rather than silently selecting it at boot.
        repository = await open_repository_after_lease_expiry(
            lambda reopened_id, root: ClerkSqliteRepository.open(
                account_id=reopened_id, artifacts_root=root, clock=clock
            ),
            account_id=account_id,
            artifacts_root=artifacts_root,
            wait_timeout_s=execution_lease_wait_timeout_s,
            retry_interval_s=execution_lease_retry_interval_s,
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
            activated_at_ms=clock(),
        )
        store.append(record)
        return record
    finally:
        repository.close()


__all__ = [
    "BROKER_UNREACHABLE_RECONNECTING",
    "DEFAULT_EXECUTION_LEASE_RETRY_INTERVAL_S",
    "DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S",
    "DEFAULT_STARTUP_RECOVERY_TIMEOUT_S",
    "SQLITE_FACADE_AUTHORITIES",
    "ActiveClerkRuntime",
    "AuthorityKind",
    "BackgroundSweep",
    "ClerkStartupFailure",
    "StartupRecoveryTimedOut",
    "TransientStartupCause",
    "activate_isolated_authority",
    "compose_failure_refusal",
    "compose_repository_runtime",
    "developer_reset_refusal",
    "open_repository",
    "open_repository_after_lease_expiry",
    "reconnecting_refusal",
    "terminal_startup_recovery",
    "transient_startup_failure",
    "unavailable_runtime",
]
