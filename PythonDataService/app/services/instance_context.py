"""PRD #619-A §A4 — shared instance-context assembler.

The mutation endpoints
(``POST /api/live-instances/{sid}/desired-state``,
``POST /api/live-instances/{sid}/flatten-and-pause``,
``POST /api/live-instances/{sid}/commands``) historically each
re-assembled the same set of facts before evaluating the pre-write
capability gate: daemon process, live binding, runs index, broker
view, desired state, last exit + poisoned, resume guard state. Three
copies of the same block drifted independently — one already returned
slightly different values from the others, which is exactly the kind
of stale-snapshot-driving-a-mutation defect PRD #616 was meant to
close.

This module hosts the shared loader. It takes its dependencies
explicitly (no imports from ``app.routers.*``) so the contract is
testable in isolation and routing concerns stay one-way: router
imports service, service never imports router.

PRD #619-A keeps the contract minimal — the fields are the same facts
the existing endpoints already compute. ``daemon_boot_id`` is reserved
for 619-B (the daemon ownership / lease layer) and is ``None`` today;
the field exists so 619-B can fill it in without churning callers.
Mutation endpoints still re-fetch the daemon binding *after* the
durable write (post-write daemon revalidation) — the loader handles
only the pre-write composition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.resume_guard_state import ResumeGuardState

if TYPE_CHECKING:
    from app.schemas.live_runs import (
        InstanceBrokerView,
        InstanceLastExit,
        InstanceProcessView,
        LiveBinding,
    )


@dataclass(frozen=True)
class InstanceContext:
    """Assembled pre-mutation facts for an instance.

    All facts are read at the same instant for one mutation request,
    so the gate operates on a consistent snapshot. Post-write
    revalidation re-fetches the daemon binding separately (the
    durable write may have moved the daemon-side state).

    Attributes
    ----------
    strategy_instance_id:
        The validated instance id (post ``_validate_instance_id``).
    observation_at_ms:
        ``int64`` ms UTC when the daemon binding was observed. Carried
        forward so 619-B's freshness checks have a stable timestamp.
    daemon_boot_id:
        The daemon's boot identity at observation time. ``None`` in
        619-A (the daemon does not publish one yet); 619-B wires this.
    process:
        ``InstanceProcessView`` interpreted from the daemon response.
    live_binding:
        ``LiveBinding`` interpreted from the daemon response, if any.
    runs:
        The run-index entries for the instance, most-recent first.
    desired_state:
        Durable desired state (``DesiredStateView``).
    last_exit:
        Latest run's exit summary, if any.
    poisoned:
        True iff the last exit carries a halt trigger.
    broker:
        Account-attribution view (owned positions / explained map).
    owned_positions_empty:
        True iff the broker has no non-zero owned positions.
    guard_state:
        Composed ``ResumeGuardState`` over the latest evidence run.
    """

    strategy_instance_id: str
    observation_at_ms: int
    daemon_boot_id: str | None
    process: InstanceProcessView
    live_binding: LiveBinding | None
    runs: list[dict]
    desired_state: object  # DesiredStateView — kept loose to avoid import cycles
    last_exit: InstanceLastExit | None
    poisoned: bool
    broker: InstanceBrokerView | None
    owned_positions_empty: bool
    guard_state: ResumeGuardState


async def load_instance_context(
    strategy_instance_id: str,
    *,
    now_ms: Callable[[], int],
    fetch_daemon_process: Callable[[str], Awaitable[dict | None]],
    interpret_daemon_process: Callable[
        [dict | None], tuple[InstanceProcessView, LiveBinding | None]
    ],
    scan_runs_for_instance: Callable[[str], list[dict]],
    resolve_desired_state: Callable[[str], object],
    instance_last_exit: Callable[[list[dict]], InstanceLastExit | None],
    instance_broker: Callable[[str], InstanceBrokerView | None],
    resolve_guard_state_for: Callable[
        [LiveBinding | None, list[dict]], ResumeGuardState
    ],
) -> InstanceContext:
    """Compose a single ``InstanceContext`` from explicit dependencies.

    All file-system, daemon-transport, and schema helpers are passed as
    callables so this module never imports ``app.routers.*``. The
    router wires up the closures over its own helpers and the live
    settings.

    ``observation_at_ms`` is stamped *after* the daemon fetch returns
    so it always describes the actual reading we're acting on.
    """
    daemon = await fetch_daemon_process(strategy_instance_id)
    observation_at_ms = now_ms()
    process, live_binding = interpret_daemon_process(daemon)
    runs = scan_runs_for_instance(strategy_instance_id)
    desired = resolve_desired_state(strategy_instance_id)
    last_exit = instance_last_exit(runs)
    broker = instance_broker(strategy_instance_id)
    owned_positions_empty = broker is None or not any(
        qty != 0 for qty in broker.owned_positions.values()
    )
    poisoned = bool(last_exit and last_exit.halt_trigger is not None)
    guard_state = resolve_guard_state_for(live_binding, runs)
    daemon_boot_id = None  # PRD #619-B fills this in.
    return InstanceContext(
        strategy_instance_id=strategy_instance_id,
        observation_at_ms=observation_at_ms,
        daemon_boot_id=daemon_boot_id,
        process=process,
        live_binding=live_binding,
        runs=runs,
        desired_state=desired,
        last_exit=last_exit,
        poisoned=poisoned,
        broker=broker,
        owned_positions_empty=owned_positions_empty,
        guard_state=guard_state,
    )
