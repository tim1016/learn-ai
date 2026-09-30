"""Installation ownership and atomic selection handover for the one worker."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.active_binding import (
    UnboundBroker,
    account_background_work_refused,
    refuse_active_alpaca_binding,
)
from app.broker.alpaca.clerk.account_authority import (
    is_shadow_account_id,
    live_account_id_for_shadow_account,
)
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime, unavailable_runtime
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.runtime import get_broker_configuration_service, resolve_clerk_dir
from app.broker_configuration.worker_binding import (
    BoundWorker,
    ServiceFactory,
    acknowledge_worker_binding,
    record_worker_startup_refusal,
)
from app.utils.advisory_lock import try_advisory_file_lock

if TYPE_CHECKING:
    from app.broker.alpaca.market_liveness import AlpacaMarketLivenessConsumer
    from app.services.sovereign_equity_snapshots import DailySovereignEquitySnapshotScheduler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountBackgroundWork:
    """The bound account's background work, which runs only while its binding stands (#2669).

    The IBKR market-status source on Alpaca's execution clock, and the daily
    sovereign equity snapshot scheduler. A boot that met a binding refusal
    starts neither. A boot whose Alpaca did not answer starts both -- the pin
    was never checked, so nothing was refused -- and a reconnect whose
    acknowledgement then refuses the binding stops them, so the lane ends as
    a boot that met the refusal directly. The start, that stop and the
    shutdown's all go through this module, reading the one decision
    ``account_background_work_refused``.
    """

    market_liveness: AlpacaMarketLivenessConsumer
    equity_snapshots: DailySovereignEquitySnapshotScheduler


_running_account_work: AccountBackgroundWork | None = None


def start_account_background_work(work: AccountBackgroundWork) -> None:
    """Start the account's background work unless the binding is refused."""
    global _running_account_work
    if account_background_work_refused():
        logger.info(
            "The Alpaca binding is refused; the IBKR market-status source and the daily "
            "sovereign equity snapshot scheduler are not started.",
            extra={"action": "account_background_work_not_started"},
        )
        return
    from app.broker.alpaca.market_liveness import set_market_liveness_consumer

    work.market_liveness.start()
    set_market_liveness_consumer(work.market_liveness)
    work.equity_snapshots.start()
    _running_account_work = work
    logger.info(
        "IBKR market-status source, Alpaca execution clock and daily sovereign equity "
        "snapshot scheduler started.",
        extra={"action": "account_background_work_started"},
    )


async def stop_account_background_work() -> bool:
    """Stop and clear the running account background work; whether any was running.

    Idempotent. The snapshot scheduler stops first -- it is the worker that
    can write for an account the configuration did not approve -- and a raise
    from it still stops the market-status source. Only this lane's own
    market-status source closes; the shared IBKR client and bar feed are not
    this work's to touch.
    """
    global _running_account_work
    work = _running_account_work
    if work is None:
        return False
    from app.broker.alpaca.market_liveness import set_market_liveness_consumer

    try:
        await work.equity_snapshots.stop()
    finally:
        set_market_liveness_consumer(None)
        await work.market_liveness.stop()
    _running_account_work = None
    return True


async def close_failed_startup() -> None:
    """Close resources installed before a later startup step failed."""
    from app.broker.alpaca.clerk.active_authority import (
        close_synthetic_clerk_runtimes,
        get_active_clerk_runtime,
        set_active_clerk_runtime,
    )
    from app.broker.alpaca.trade_updates import (
        get_trade_updates_consumer,
        set_trade_updates_consumer,
    )
    from app.services.bot_runner import get_bot_task_registry

    # Boot's Dry Run restoration starts before later startup steps (#2582),
    # and a restoration still inside its lease wait registers its runtime only
    # after the synthetic close below -- leaving its account's lease held in a
    # process that never serves. Cancelled first, an interrupted opening
    # releases the lease itself (#2668).
    registry = get_bot_task_registry()
    if registry is not None:
        await registry.stop_dry_run_restoration()
    updates = get_trade_updates_consumer()
    if updates is not None:
        await updates.stop()
        set_trade_updates_consumer(None)
    await stop_account_background_work()
    await close_synthetic_clerk_runtimes()
    runtime = get_active_clerk_runtime()
    set_active_clerk_runtime(None)
    if runtime is not None:
        await runtime.close()


@contextmanager
def installation_worker(*, clerk_dir: Path | None = None) -> Iterator[UnboundBroker | None]:
    """Hold the installation's writer lock until shutdown, across account switches.

    Account execution leases remain authoritative for custody. This additional
    lock enforces v1's one process per installation even when two starts would
    select different accounts, whose custody leases cannot exclude each other.
    """
    refusal = None
    with ExitStack() as stack:
        try:
            root = resolve_clerk_dir() if clerk_dir is None else clerk_dir
            acquired = stack.enter_context(try_advisory_file_lock(root / "broker_configuration" / "worker"))
            if not acquired:
                refusal = UnboundBroker(
                    reason="broker_unconfigured",
                    message="Another worker owns this installation's broker connection.",
                    next_step="Stop the other worker before starting this installation again.",
                )
        except OSError:
            refusal = UnboundBroker(
                reason="profiles_database_unavailable",
                message="The installation's broker ownership lock could not be opened.",
                next_step="Check the Clerk volume is mounted and writable, then restart.",
            )
        yield refusal


@contextmanager
def selection_handover(
    *, service_factory: ServiceFactory = get_broker_configuration_service
) -> Iterator[UnboundBroker | None]:
    """Keep generation stable from preflight through custody and acknowledgement."""
    refusal = None
    with ExitStack() as stack:
        try:
            stack.enter_context(service_factory().selection_handover())
        except BrokerConfigurationError as exc:
            refusal = UnboundBroker(
                reason=exc.reason,
                message=exc.message,
                next_step=exc.next_step or "Wait for the other startup to finish, then retry.",
            )
        yield refusal


async def acknowledge_runtime_binding(
    *,
    bound: BoundWorker,
    runtime: ActiveClerkRuntime,
    service_factory: ServiceFactory = get_broker_configuration_service,
) -> ActiveClerkRuntime:
    """Confirm the runtime before exposing its writer or starting any taps.

    A runtime still reconnecting to Alpaca (#2582) is neither acknowledged nor
    refused: its pending Apply is decided by what the reconnect ends in, and
    that outcome comes back through here.
    """
    if runtime.reconnecting:
        return runtime
    if runtime.clerk is None:
        if runtime.startup_failure is not None:
            record_worker_startup_refusal(
                bound=bound, recovery=runtime.startup_failure.recovery,
                service_factory=service_factory,
            )
        if runtime.startup_failure is not None and runtime.startup_failure.reason_code == "ACCOUNT_PIN_MISMATCH":
            refuse_active_alpaca_binding(UnboundBroker(
                reason="account_pin_mismatch",
                message="The broker account no longer matches the approved configuration.",
                next_step=runtime.startup_failure.recovery,
            ))
        return runtime
    account_id = runtime.selected_account_id
    # The selection remembers the broker account, while a Shadow repository
    # is keyed to its separate custody world. Future switching probes the real
    # account and must never receive a synthetic custody namespace.
    if account_id is not None and is_shadow_account_id(account_id):
        account_id = live_account_id_for_shadow_account(account_id)
    if acknowledge_worker_binding(
        bound=bound, account_id=account_id, service_factory=service_factory
    ):
        return runtime
    await runtime.close()
    refuse_active_alpaca_binding(UnboundBroker(
        reason="broker_unconfigured",
        message="The broker configuration could not be acknowledged and no writer was installed.",
        next_step="Reload the selection, then restart to recover its effective revision.",
    ))
    return unavailable_runtime(
        "SELECTION_GENERATION_CONFLICT",
        account_id=account_id,
        recovery="The broker binding could not be recorded; restart to resolve the current selection.",
    )


async def acknowledge_reconnected_binding(
    *,
    bound: BoundWorker,
    runtime: ActiveClerkRuntime,
    service_factory: ServiceFactory = get_broker_configuration_service,
) -> ActiveClerkRuntime:
    """Acknowledge a reconnect's selection as the boot's own, then follow the binding (#2669).

    The boot started the account's background work only because its binding
    was not refused then: an unanswered Alpaca left the pin unchecked. When
    this acknowledgement refuses the binding -- the reconnect read an account
    the configuration did not approve -- that work stops. A reconnect that
    ends serving leaves it running.
    """
    acknowledged = await acknowledge_runtime_binding(
        bound=bound, runtime=runtime, service_factory=service_factory
    )
    if account_background_work_refused() and await stop_account_background_work():
        failure = acknowledged.startup_failure
        logger.warning(
            "The reconnect refused this worker's Alpaca binding; the IBKR market-status source "
            "and the daily sovereign equity snapshot scheduler are stopped, so no snapshot is "
            "written for an unapproved account.",
            extra={
                "action": "account_background_work_stopped",
                "reason_code": None if failure is None else failure.reason_code,
                "account_id": None if failure is None else failure.account_id,
            },
        )
    return acknowledged
