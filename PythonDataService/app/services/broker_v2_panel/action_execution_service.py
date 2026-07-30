"""Presented-action execution (spec §11).

``execute_action`` enforces the three execution invariants and dispatches to the
backend capability that performs the action:

1. **Revision guard.** The POST carries the panel-state ``revision`` it was
   presented against. If the current revision differs, the action is stale →
   ``StaleRevisionError`` (the router maps it to ``409`` + a refresh).
2. **Idempotency.** An ``idempotency_key`` seen before is a no-op — the same
   double-click or retry does not re-fire the action (``applied=False``).
3. **Identity from the channel.** The operator identity is the configured
   ``PANEL_OPERATOR_IDENTITY`` (§14), never a request field.

The dispatch wires the actions the backend can perform today: ``stop``,
``reconcile_now``, ``clear_hold``. Actions whose lifecycle backend lands in a
later slice (``start``, ``retire``, ``flatten_stop``, ``deploy``,
``cancel_order`` — the last needs an order id chosen from the working-orders
list) raise ``ActionNotAvailableError`` rather than presenting a fake success,
per the closed-set/no-fake-buttons rule (§11).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from app.broker.v2panel.vocabulary import ActionId
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult


class ActionExecutionError(Exception):
    """Base typed action-execution error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class StaleRevisionError(ActionExecutionError):
    """The POST bound to a panel revision that is no longer current (409)."""

    http_status = 409


class ActionNotAvailableError(ActionExecutionError):
    """The action is not executable for this bot in its current state (409)."""

    http_status = 409


class UnknownActionError(ActionExecutionError):
    """The action id is outside the closed set (422). Should not occur post-validation."""

    http_status = 422


# A performer runs one action and returns a human-readable outcome message.
ActionPerformer = Callable[[str], Awaitable[str]]

_IN_FLIGHT_WAIT_SECONDS = 5.0


@dataclass
class IdempotencyRecord:
    state: Literal["in_flight", "succeeded", "failed"]
    result: PanelActionResult | None = None
    error_detail: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event)


class IdempotencyStore:
    """In-memory idempotency ledger keyed by ``(sid, action_id, key)``.

    Three states — ``in_flight``, ``succeeded``, ``failed`` — with an asyncio
    event that the first caller sets when execution resolves. Concurrent POSTs
    with the same key wait up to 5 s for the first execution to finish rather
    than racing to double-fire the action (§11).

    The store is process-local — durable idempotency across restarts is a
    later-slice concern; within one process it makes double-clicks and retries
    safe (§11).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}

    async def reserve_or_get(
        self, sid: str, action_id: str, key: str
    ) -> IdempotencyRecord | None:
        """Reserve key for new execution, returning None; or return existing record.

        If None: key is new, caller should execute and then call complete() or fail().
        If record with state in_flight: waits (up to 5 s) for the first
            execution to finish, then returns the resolved record.
        If record with state succeeded/failed: returns immediately.
        """
        compound = (sid, action_id, key)
        async with self._lock:
            existing = self._records.get(compound)
            if existing is None:
                record = IdempotencyRecord(state="in_flight")
                self._records[compound] = record
                return None
            if existing.state != "in_flight":
                return existing
            # in_flight — capture the event to wait on outside the lock
            event = existing._event

        # Wait outside the lock so the first caller can complete/fail
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=_IN_FLIGHT_WAIT_SECONDS)

        async with self._lock:
            return self._records.get(compound)

    async def complete(
        self, sid: str, action_id: str, key: str, result: PanelActionResult
    ) -> None:
        compound = (sid, action_id, key)
        async with self._lock:
            record = self._records.get(compound)
            if record is not None:
                record.state = "succeeded"
                record.result = result
                record._event.set()

    async def fail(self, sid: str, action_id: str, key: str, error: str) -> None:
        compound = (sid, action_id, key)
        async with self._lock:
            record = self._records.get(compound)
            if record is not None:
                record.state = "failed"
                record.error_detail = error
                record._event.set()

    async def release(self, sid: str, action_id: str, key: str) -> None:
        """Drop a reservation whose action provably never ran (pre-exec reject).

        A stale-revision or not-available rejection happens *before* the
        performer runs, so nothing was applied. Removing the record frees the
        key for a corrected retry — otherwise it would sit ``in_flight`` forever
        (a leak) and force the retry to wait out the in-flight timeout. Any
        waiters are woken so they re-evaluate instead of blocking.
        """
        compound = (sid, action_id, key)
        async with self._lock:
            record = self._records.pop(compound, None)
            if record is not None:
                record._event.set()


_IDEMPOTENCY_STORE = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    """Return the process-level idempotency store (installed for the app)."""
    return _IDEMPOTENCY_STORE


def reset_idempotency_store_for_testing() -> None:
    """Clear the process-level idempotency ledger (test isolation)."""
    _IDEMPOTENCY_STORE._records.clear()


async def execute_action(
    request: PanelActionRequest,
    *,
    sid: str,
    current_revision: int,
    performers: dict[ActionId, ActionPerformer],
    operator_identity: str,
    store: IdempotencyStore | None = None,
) -> PanelActionResult:
    """Execute one presented action under the three invariants (§11).

    ``performers`` maps each executable action id to a coroutine that performs
    it and returns an outcome message. An action id absent from the map raises
    ``ActionNotAvailableError`` (the honest "not yet wired" path).
    """
    ledger = store if store is not None else get_idempotency_store()

    # 2. Idempotency — check BEFORE the revision guard so a genuine retry of an
    # already-applied action stays a safe no-op even if the panel has since
    # advanced (the action already took effect; re-posting must not 409).
    record = await ledger.reserve_or_get(sid, request.action_id, request.idempotency_key)
    reserved_fresh = record is None
    if record is not None:
        if record.state == "succeeded" and record.result is not None:
            return PanelActionResult(
                action_id=request.action_id,
                applied=False,
                revision=current_revision,
                message=record.result.message,
            )
        if record.state == "failed":
            raise ActionNotAvailableError(
                "This action previously failed; the idempotency key cannot be reused.",
                detail=record.error_detail,
            )
        # state is still in_flight after timeout — treat as a new execution
        # by falling through (the first caller may have crashed). We do not own
        # this reservation, so we must not release/complete it below.

    try:
        # 1. Revision guard.
        if request.revision != current_revision:
            raise StaleRevisionError(
                "The panel changed since this action was presented.",
                detail=(
                    f"Presented revision {request.revision} no longer matches the "
                    f"current revision {current_revision}. Refresh and retry."
                ),
            )

        performer = performers.get(request.action_id)
        if performer is None:
            raise ActionNotAvailableError(
                f"The '{request.action_id}' action cannot be executed right now.",
                detail="This action's backend is not available for this bot in its current state.",
            )

        # 3. Identity from the channel — the performer receives the configured
        # operator identity, never anything from the request body.
        message = await performer(operator_identity)
    except ActionExecutionError:
        # Pre-execution rejection (stale revision / not available): the action
        # never ran, so free a fresh reservation for a corrected retry.
        if reserved_fresh:
            await ledger.release(sid, request.action_id, request.idempotency_key)
        raise
    except Exception as err:
        # The performer ran and failed: burn the key so a blind retry does not
        # re-fire an action that may have partially applied.
        if reserved_fresh:
            await ledger.fail(sid, request.action_id, request.idempotency_key, str(err))
        raise

    result = PanelActionResult(
        action_id=request.action_id,
        applied=True,
        revision=current_revision,
        message=message,
    )
    if reserved_fresh:
        await ledger.complete(sid, request.action_id, request.idempotency_key, result)
    return result
