"""Presented-action execution (spec §11).

``execute_action`` enforces the three execution invariants and dispatches to the
backend capability that performs the action:

1. **Action token guard.** The POST carries the action-specific concurrency
   token it was presented against. If that action's relevant state differs, it is stale →
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
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
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
    """The POST's action-scoped concurrency token is no longer current (409)."""

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


class DurableIdempotencyStore(IdempotencyStore):
    """File-backed command receipts for one bot's panel actions.

    A process restart cannot forget a completed STOP/FLATTEN command and fire it
    again.  A persisted in-flight reservation is deliberately restored as a
    failed/unknown command: replaying it would be less safe than asking the
    operator to inspect its Clerk evidence and issue a fresh command.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        for compound, payload in raw.items():
            sid, action_id, key = compound.split("\u001f", 2)
            state = payload["state"]
            if state == "succeeded":
                result = PanelActionResult.model_validate(payload["result"])
                self._records[(sid, action_id, key)] = IdempotencyRecord(
                    state="succeeded", result=result
                )
            else:
                self._records[(sid, action_id, key)] = IdempotencyRecord(
                    state="failed",
                    error_detail=(
                        payload.get("error_detail")
                        or "The prior command did not finish with a durable receipt. "
                        "Inspect Clerk evidence before issuing a new command."
                    ),
                )

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {}
        for (sid, action_id, key), record in self._records.items():
            compound = "\u001f".join((sid, action_id, key))
            payload[compound] = {
                "state": record.state,
                "result": record.result.model_dump() if record.result is not None else None,
                "error_detail": record.error_detail,
            }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=self._path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def reserve_or_get(
        self, sid: str, action_id: str, key: str
    ) -> IdempotencyRecord | None:
        self._load()
        record = await super().reserve_or_get(sid, action_id, key)
        if record is None:
            self._persist()
        return record

    async def complete(
        self, sid: str, action_id: str, key: str, result: PanelActionResult
    ) -> None:
        await super().complete(sid, action_id, key, result)
        self._persist()

    async def fail(self, sid: str, action_id: str, key: str, error: str) -> None:
        await super().fail(sid, action_id, key, error)
        self._persist()

    async def release(self, sid: str, action_id: str, key: str) -> None:
        await super().release(sid, action_id, key)
        self._persist()


_IDEMPOTENCY_STORE = IdempotencyStore()
_DURABLE_STORES: dict[Path, DurableIdempotencyStore] = {}


def get_idempotency_store() -> IdempotencyStore:
    """Return the process-level idempotency store (installed for the app)."""
    return _IDEMPOTENCY_STORE


def reset_idempotency_store_for_testing() -> None:
    """Clear the process-level idempotency ledger (test isolation)."""
    _IDEMPOTENCY_STORE._records.clear()
    _DURABLE_STORES.clear()


def durable_idempotency_store_for(path: Path) -> DurableIdempotencyStore:
    """Return the restart-safe receipt ledger for one bot instance."""
    return _DURABLE_STORES.setdefault(path, DurableIdempotencyStore(path))


async def execute_action(
    request: PanelActionRequest,
    *,
    sid: str,
    current_revision: int,
    performers: dict[ActionId, ActionPerformer],
    operator_identity: str,
    current_concurrency_token: str,
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
                concurrency_token=current_concurrency_token,
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
        # 1. Action-specific concurrency guard.  Display revisions intentionally
        # advance for unrelated evidence; only this action's declared inputs may
        # invalidate a presented command.
        if request.concurrency_token != current_concurrency_token:
            raise StaleRevisionError(
                "This action changed since it was presented.",
                detail=(
                    "The action's current concurrency token no longer matches. "
                    "Refresh and retry."
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
        concurrency_token=current_concurrency_token,
        message=message,
    )
    if reserved_fresh:
        await ledger.complete(sid, request.action_id, request.idempotency_key, result)
    return result
