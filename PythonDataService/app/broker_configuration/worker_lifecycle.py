"""Installation ownership and atomic selection handover for the one worker."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from app.broker.alpaca.active_binding import UnboundBroker, refuse_active_alpaca_binding
from app.broker.alpaca.clerk.account_authority import (
    is_shadow_account_id,
    live_account_id_for_shadow_account,
)
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime, unavailable_runtime
from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker_configuration.arming_policy import install_configuration_arming_fence
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.runtime import get_broker_configuration_service, resolve_clerk_dir
from app.broker_configuration.worker_binding import (
    BoundWorker,
    ServiceFactory,
    acknowledge_worker_binding,
)
from app.utils.advisory_lock import try_advisory_file_lock


async def close_failed_startup() -> None:
    """Close resources installed before a later startup step failed."""
    from app.broker.alpaca.clerk.active_authority import (
        close_synthetic_clerk_runtimes,
        get_active_clerk_runtime,
        set_active_clerk_runtime,
    )
    from app.broker.alpaca.market_liveness import (
        get_market_liveness_consumer,
        set_market_liveness_consumer,
    )
    from app.broker.alpaca.trade_updates import (
        get_trade_updates_consumer,
        set_trade_updates_consumer,
    )

    updates = get_trade_updates_consumer()
    if updates is not None:
        await updates.stop()
        set_trade_updates_consumer(None)
    liveness = get_market_liveness_consumer()
    if liveness is not None:
        await liveness.stop()
        set_market_liveness_consumer(None)
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
    """Confirm the runtime before exposing its writer or starting any taps."""
    if runtime.clerk is None:
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
    try:
        accepted = acknowledge_worker_binding(
            bound=bound, account_id=account_id, service_factory=service_factory
        )
        if accepted:
            install_configuration_arming_fence(
                bound=bound,
                gate=(runtime.clerk.live_arming if isinstance(runtime.clerk, SqliteAlpacaClerkFacade) else None),
            )
            return runtime
    except LiveArmingInvalid:
        await runtime.close()
        refuse_active_alpaca_binding(UnboundBroker(
            reason="broker_unconfigured",
            message="The applied broker configuration's arming evidence could not be read.",
            next_step="Restore the configuration arming evidence, then restart.",
        ))
        return unavailable_runtime(
            "LIVE_ARMING_LEDGER_INVALID",
            account_id=account_id,
            recovery="The configuration arming evidence could not be read; restore it and restart.",
        )
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
