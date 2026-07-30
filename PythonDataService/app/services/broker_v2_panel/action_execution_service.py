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

import threading
from collections.abc import Awaitable, Callable

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


class IdempotencyStore:
    """In-memory idempotency ledger keyed by ``(sid, action_id, key)``.

    A key seen before short-circuits the action to a no-op. The store is
    process-local — durable idempotency across restarts is a later-slice
    concern; within one process it makes double-clicks and retries safe (§11).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str, str], str] = {}

    def seen(self, sid: str, action_id: str, key: str) -> str | None:
        with self._lock:
            return self._seen.get((sid, action_id, key))

    def record(self, sid: str, action_id: str, key: str, message: str) -> None:
        with self._lock:
            self._seen[(sid, action_id, key)] = message


_IDEMPOTENCY_STORE = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    """Return the process-level idempotency store (installed for the app)."""
    return _IDEMPOTENCY_STORE


def reset_idempotency_store_for_testing() -> None:
    """Clear the process-level idempotency ledger (test isolation)."""
    _IDEMPOTENCY_STORE._seen.clear()


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
    prior = ledger.seen(sid, request.action_id, request.idempotency_key)
    if prior is not None:
        return PanelActionResult(
            action_id=request.action_id,
            applied=False,
            revision=current_revision,
            message=prior,
        )

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
    ledger.record(sid, request.action_id, request.idempotency_key, message)
    return PanelActionResult(
        action_id=request.action_id,
        applied=True,
        revision=current_revision,
        message=message,
    )
