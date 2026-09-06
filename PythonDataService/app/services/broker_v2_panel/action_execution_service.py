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

The dispatch wires Resume, Pause, Continue, Stop, flatten-and-stop,
reconciliation, clear-hold, and guarded inventory recovery. Unsupported
closed-set actions such as Retire and Cancel order are not presented and raise
``ActionNotAvailableError`` if called directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost, RepositoryPoisoned
from app.broker.v2panel.vocabulary import ActionId
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class ActionExecutionError(Exception):
    """Base typed action-execution error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.reason_code = reason_code


class StaleRevisionError(ActionExecutionError):
    """The POST's action-scoped concurrency token is no longer current (409)."""

    http_status = 409


class ActionNotAvailableError(ActionExecutionError):
    """The action is not executable for this bot in its current state (409)."""

    http_status = 409


class UnknownActionError(ActionExecutionError):
    """The action id is outside the closed set (422). Should not occur post-validation."""

    http_status = 422


class ActionOutcomeUnknownError(ActionExecutionError):
    """A performer began but did not return a terminal command receipt (500)."""

    http_status = 500


#: Reason code for a lost/expired account execution lease (T7c, #1794).
EXECUTION_AUTHORITY_LOST_REASON_CODE = "EXECUTION_LEASE_LOST"

#: Reason code for a lease the ADR 0050 revival just restored, distinct from
#: the terminal ``EXECUTION_AUTHORITY_LOST_REASON_CODE``: the account's
#: authority is fine again, this one request simply landed on the outage and
#: applied nothing. Kept apart so a frontend (or an operator reading logs)
#: never confuses "restart the data plane" with "click the action again".
EXECUTION_LEASE_REVIVED_REASON_CODE = "EXECUTION_LEASE_REVIVED"

#: Reason code for an authority fenced by an unconfirmed mirror finalize.
AUTHORITY_POISONED_REASON_CODE = "AUTHORITY_MIRROR_UNCONFIRMED"


class ExecutionAuthorityLostError(ActionExecutionError):
    """This account's execution lease is lost even after a revival attempt (503).

    Fail-closed is correct and stays; this type exists so the surface says so
    in authored copy instead of leaking a raw 500 (T7c). The clerk router has
    translated the same condition since the SQLite cutover -- the panel
    router simply had no handler for it.

    ADR 0050: the write path (``panel_data_source.run_action``) always tries
    exactly one supervised revival via ``ReconciliationSweep.revive_now()`` --
    the same entry point the lease heartbeat uses -- before this error is
    raised. A lease that merely expired under a frozen-then-thawed process,
    with nobody else ever touching the account, self-cures there and this
    error is never seen for that case. By the time this is raised, a control
    on this panel *did* just try to re-acquire the lease; the copy must say
    what the store actually refused, never the pre-ADR-0050 claim that no
    control here could have tried.
    """

    http_status = 503

    def __init__(
        self,
        *,
        revival_outcome: str,
        attempted: bool = True,
        remedy: str | None = None,
    ) -> None:
        # Account-scoped problem, account-scoped cure -- the Two-Tap rule's own
        # shape. The internal handle message ("this handle can no longer
        # write") is diagnostic, not operator copy, and must not reach here.
        #
        # ``attempted`` distinguishes an outcome the store actually proved
        # (a revival ran and was refused, or errored) from one where no
        # revival could even be tried (the authority or its sweep was gone) --
        # claiming a "store-verified" attempt on the latter would be false.
        # ``remedy`` lets a genuinely transient outcome close on "retry",
        # never both "retry" and "restart the data plane" in the same
        # message -- one blocker, one remedy (#1955 final review).
        lede = (
            "a supervised, store-verified revival attempt did not restore it"
            if attempted
            else "no supervised revival could even be attempted"
        )
        super().__init__(
            "This account's execution authority can no longer be written to.",
            detail=(
                f"The data plane's execution lease for this account was lost, and {lede}: "
                f"{revival_outcome}. Refusing writes is deliberate: a holder that cannot "
                "prove it still owns the account must not act on stale authority. "
                + (
                    remedy
                    or "Restart the data plane to acquire a fresh lease and reconcile custody on boot."
                )
            ),
            reason_code=EXECUTION_AUTHORITY_LOST_REASON_CODE,
        )


# Operator-facing copy for each way the write path's ADR 0050 revival attempt
# (panel_data_source._revive_lease_or_raise) can end in this error -- kept
# beside the error class, not inline in the data-source module, so the
# authored copy and the reason it exists stay in one place. Every call site
# names a real store-proven outcome; there is deliberately no generic
# fallback string for an outcome nothing observed.
REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE = (
    "the account's active SQLite authority was replaced or removed before "
    "this action's revival could run"
)
REVIVAL_OUTCOME_REFUSED = (
    "another writer or an authority ceremony has held this account since "
    "its execution lease last renewed"
)
#: The account's SQLite authority is present, but there is no reconciliation
#: sweep behind it to revive through -- e.g. a synthetic authority (which
#: never binds a write-path-reachable sweep of its own kind) or a boot window
#: in which the sweep has not been wired up yet. Distinct from
#: ``REVIVAL_OUTCOME_AUTHORITY_UNAVAILABLE``, which says the authority itself
#: was replaced or removed -- here the authority is exactly the one this
#: action was presented against; it just has nothing to revive through.
REVIVAL_OUTCOME_NO_SWEEP = (
    "this account's active SQLite authority is not running a reconciliation "
    "sweep to revive through -- there is no supervised revival path available "
    "for it"
)
#: ``ReconciliationSweep.revive_now()`` raised something other than
#: ``ExecutionLeaseLost``/``RepositoryPoisoned`` -- a transient store error
#: (e.g. "database is locked") that never reached a confirmed outcome. This
#: mirrors the lease heartbeat's own transient vocabulary
#: (``_run_lease_heartbeat``'s "errored; retrying" branch): the store
#: hiccupped rather than proved the lease unrecoverable, so the honest copy
#: is "retry shortly", not "another writer took the account". The remedy
#: sentence (not this string) is what actually tells the operator to retry --
#: kept out of here so it is said exactly once (see
#: ``ExecutionAuthorityLostError``'s ``remedy`` parameter).
REVIVAL_OUTCOME_TRANSIENT_STORE_ERROR = (
    "the store returned a transient error while attempting the revival and "
    "never reached a confirmed outcome -- this is the same class of hiccup "
    "the lease heartbeat retries on its own"
)
#: The single remedy sentence for :data:`REVIVAL_OUTCOME_TRANSIENT_STORE_ERROR`.
#: Deliberately does not mention restarting the data plane: nothing here
#: proved the lease lost, so the terminal cure would be a false claim, and a
#: message ending in both "retry shortly" and "restart the data plane" gives
#: an operator two contradictory remedies for one blocker (#1955 final review).
REVIVAL_REMEDY_TRANSIENT_STORE_ERROR = (
    "Retry this action shortly -- the store hiccupped without proving the "
    "lease unrecoverable, unlike a store-refused revival."
)


class ExecutionAuthorityRevivedError(ActionExecutionError):
    """The ADR 0050 revival just succeeded; this request applied nothing (503).

    See ADR 0050's 2026-09-06 addendum for why the write path reports this as
    a retryable refusal instead of auto-retrying the mutation in-process
    (two independent reviews rejected that design in rounds 1 and 2).

    Raised only when :meth:`ReconciliationSweep.revive_now` returns
    successfully -- i.e. the store just proved this handle's lease is good
    again. Nothing about *this* request's mutation was attempted after that:
    the CAS confirming the lease is separate from, and prior to, whatever
    the performer was doing when it discovered the loss. The operator's next
    click is a genuinely fresh request: the panel client mints a new
    ``idempotency_key`` per submission (``crypto.randomUUID()`` in
    ``broker-v2-panel.service.ts``'s ``submitAction``), so a fresh key always
    reaches the broker -- and the ORIGINAL key is released, not burned
    ``failed`` (:meth:`execute_action`'s ``ExecutionLeaseLost`` handling), so
    the same-key re-POST a cohort batch derives (``{key}:{sid}``) reaches it
    too (#1955).
    """

    http_status = 503

    def __init__(self) -> None:
        super().__init__(
            "This account's execution lease had expired and has just been "
            "revived; nothing was applied.",
            detail=(
                "The data plane's execution lease for this account expired while "
                "the process was paused, and a supervised, store-verified revival "
                "just re-acquired it -- no other writer ever held the account. "
                "This request's write was not attempted under the revived lease: "
                "retry the action now that authority is restored."
            ),
            reason_code=EXECUTION_LEASE_REVIVED_REASON_CODE,
        )


class AuthorityPoisonedError(ActionExecutionError):
    """A transition committed but its mirror finalize was unconfirmed (503).

    Distinct from :class:`ExecutionAuthorityLostError` on purpose. A lost lease
    means another process owns this account and a restart re-acquires it; a
    poisoned repository means *this* authority's own durability is unproven
    until the exact fence reconciliation re-runs and finds it consistent. The
    conditions have different cures, so telling an operator to wait out a lease
    timeout here would send them somewhere the problem is not.
    """

    http_status = 503

    def __init__(self) -> None:
        super().__init__(
            "This account's authority cannot accept writes until its last "
            "transition is re-verified.",
            detail=(
                "A custody transition committed but its mirror copy was not "
                "confirmed, so the authority fenced itself rather than build on "
                "a record it cannot prove. Refusing is deliberate. Reconciling "
                "the fence -- which a data-plane restart does on open -- clears "
                "it once the record is found consistent."
            ),
            reason_code=AUTHORITY_POISONED_REASON_CODE,
        )


class ActivationFailedError(ActionExecutionError):
    """A performer registered real state, then failed with cleanup proven (500).

    A known, resolved failure distinct from ``ActionOutcomeUnknownError``:
    the outcome is ``failure``, not ``unknown``, because the attempted run's
    Clerk stop provably committed. Still burns the idempotency key like any
    other post-execution failure — a blind retry with the same key must not
    re-fire the action.
    """

    http_status = 500


# A performer runs one action and returns a human-readable outcome message.
# Receives the channel-derived operator identity and the request's
# operator-authored reason (may be ``None``); most performers ignore reason.
ActionPerformer = Callable[[str, str | None], Awaitable[str]]

_DEFAULT_IN_FLIGHT_WAIT_SECONDS = 5.0


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

    def __init__(self, *, wait_timeout_s: float = _DEFAULT_IN_FLIGHT_WAIT_SECONDS) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}
        self._wait_timeout_s = wait_timeout_s

    async def reserve_or_get(self, sid: str, action_id: str, key: str) -> IdempotencyRecord | None:
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
            await asyncio.wait_for(event.wait(), timeout=self._wait_timeout_s)

        async with self._lock:
            return self._records.get(compound)

    async def complete(self, sid: str, action_id: str, key: str, result: PanelActionResult) -> None:
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

    def __init__(
        self,
        path: Path,
        *,
        wait_timeout_s: float = _DEFAULT_IN_FLIGHT_WAIT_SECONDS,
    ) -> None:
        super().__init__(wait_timeout_s=wait_timeout_s)
        self._path = path
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        legacy_observed_at_ms = self._path.stat().st_mtime_ns // 1_000_000
        for compound, payload in raw.items():
            sid, action_id, key = compound.split("\u001f", 2)
            state = payload["state"]
            if state == "succeeded":
                result_payload = dict(payload["result"])
                # Receipt files written before the outcome contract carried
                # only the action result. Preserve them as successful legacy
                # receipts instead of making an upgrade re-fire a command.
                result_payload.setdefault("receipt_id", key)
                # Legacy receipts did not persist their recording time. The
                # file modification time is the earliest durable observation
                # available after upgrade; use it instead of fabricating 1970.
                result_payload.setdefault("recorded_at_ms", legacy_observed_at_ms)
                result = PanelActionResult.model_validate(result_payload)
                self._records[(sid, action_id, key)] = IdempotencyRecord(state="succeeded", result=result)
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
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def reserve_or_get(self, sid: str, action_id: str, key: str) -> IdempotencyRecord | None:
        self._load()
        record = await super().reserve_or_get(sid, action_id, key)
        if record is None:
            self._persist()
        return record

    async def complete(self, sid: str, action_id: str, key: str, result: PanelActionResult) -> None:
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
    availability_error: ActionNotAvailableError | None = None,
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
                receipt_id=record.result.receipt_id,
                recorded_at_ms=record.result.recorded_at_ms,
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
        # A timed-out waiter cannot distinguish a slow command from a crashed
        # process. Re-firing a lifecycle command is unsafe in both cases: keep
        # the reservation and require the caller to inspect the eventual Clerk
        # receipt before issuing a new key.
        raise ActionOutcomeUnknownError(
            "This command is still processing.",
            detail=(
                "No terminal receipt arrived before the idempotency wait expired. "
                "Do not retry this key; inspect Clerk evidence for the final outcome."
            ),
        )

    try:
        # Availability is checked only after idempotency recovery. A retry of
        # a completed lifecycle action may observe itself disabled precisely
        # because its first request succeeded.
        if availability_error is not None:
            raise availability_error

        # 1. Action-specific concurrency guard.  Display revisions intentionally
        # advance for unrelated evidence; only this action's declared inputs may
        # invalidate a presented command.
        if request.concurrency_token != current_concurrency_token:
            raise StaleRevisionError(
                "This action changed since it was presented.",
                detail=("The action's current concurrency token no longer matches. Refresh and retry."),
            )

        performer = performers.get(request.action_id)
        if performer is None:
            raise ActionNotAvailableError(
                f"The '{request.action_id}' action cannot be executed right now.",
                detail="This action's backend is not available for this bot in its current state.",
            )

        # 3. Identity from the channel — the performer receives the configured
        # operator identity, never anything from the request body. The
        # request's operator-authored reason is the one request field a
        # performer may consume.
        message = await performer(operator_identity, request.reason)
    except (StaleRevisionError, ActionNotAvailableError) as err:
        # Pre-execution rejection (stale revision / not available): the action
        # never ran, so free a fresh reservation for a corrected retry. Scoped
        # to exactly these two types — NOT the broader ``ActionExecutionError``
        # — because a performer can also raise ``ActionNotAvailableError``
        # (e.g. ``_resume``) after it already attempted real work; that case is
        # legitimately pre-execution too. Any OTHER ``ActionExecutionError``
        # subclass a performer raises (e.g. ``UnknownActionError``) falls
        # through to the ``except Exception`` branch below and burns the key,
        # since the performer ran and its outcome is unknown.
        # Instrument which 409-class subclass fired so a Stop-409 in the field
        # is attributable — a stale action token (running flipped) vs the
        # action being not-available — instead of an ambiguous bare 409
        # (defect #10: the documented "whole-panel revision" cause is
        # architecturally impossible for Stop, so the real trigger must be
        # disambiguated from live evidence).
        logger.info(
            "panel action rejected before execution",
            extra={
                "action": "panel_action_rejected",
                "action_id": request.action_id,
                "sid": sid,
                "error_class": type(err).__name__,
                "http_status": err.http_status,
            },
        )
        if reserved_fresh:
            await ledger.release(sid, request.action_id, request.idempotency_key)
        raise
    except ActivationFailedError as err:
        # A known, resolved failure (cleanup proven): burn the key like any
        # other post-execution failure, but report it as-is rather than
        # collapsing it into ActionOutcomeUnknownError — the outcome is
        # failure, not unknown.
        if reserved_fresh:
            await ledger.fail(sid, request.action_id, request.idempotency_key, str(err))
        raise
    except ExecutionLeaseLost:
        # Resume/Retire/Archive dispatch through this executor rather than
        # sqlite_panel_source.execute_sqlite_panel_action (that module returns
        # None for the SQLITE_PANEL_LIFECYCLE_ACTION_IDS and defers here). That
        # module lets ExecutionLeaseLost/RepositoryPoisoned propagate unwrapped
        # past its own exception handling so panel_data_source.run_action's
        # ADR 0050 revival catch can see them; re-raise unwrapped here too,
        # instead of collapsing into ActionOutcomeUnknownError's opaque 500 --
        # the revival catch needs the typed condition, not a generic failure.
        #
        # Every repository mutation renews the execution lease as its very
        # first statement under the write lock (repository.py), so the
        # mutation that raised applied nothing. A multi-leg action can still
        # have completed earlier legs (safe_flatten submits leg 1 before leg 2
        # renews); a same-key re-POST is safe there because
        # execute_safe_flatten_plan re-filters on active_exit_for_order and
        # refuses a second reduction of an already-exited entry. So this
        # releases the key like a pre-execution rejection above, not the
        # ``failed`` burn a real post-execution failure gets. A cohort leg the
        # write path later revives (panel_data_source._revive_lease_or_raise)
        # needs its ORIGINAL idempotency key free for the operator's same-key
        # re-POST -- burning it here left that leg permanently unflattenable
        # under its own key (#1955 final review).
        if reserved_fresh:
            await ledger.release(sid, request.action_id, request.idempotency_key)
        raise
    except RepositoryPoisoned as err:
        # Unlike a lost lease, a poison can be raised AFTER a transition's
        # SQLite commit already succeeded (append_transition's own
        # mirror-finalize failure, repository.py) -- so a mutation may really
        # have applied. Keep burning the key ``failed``: a blind same-key
        # retry must not re-fire a command that may have partially committed.
        if reserved_fresh:
            await ledger.fail(sid, request.action_id, request.idempotency_key, str(err))
        raise
    except Exception as err:
        # The performer ran and failed: burn the key so a blind retry does not
        # re-fire an action that may have partially applied.
        if reserved_fresh:
            await ledger.fail(sid, request.action_id, request.idempotency_key, str(err))
        raise ActionOutcomeUnknownError(
            "The command did not return a terminal receipt.",
            detail=("Its outcome is unknown. Inspect Clerk evidence before issuing another lifecycle command."),
        ) from err

    result = PanelActionResult(
        action_id=request.action_id,
        receipt_id=request.idempotency_key,
        recorded_at_ms=now_ms_utc(),
        applied=True,
        revision=current_revision,
        concurrency_token=current_concurrency_token,
        message=message,
    )
    if reserved_fresh:
        await ledger.complete(sid, request.action_id, request.idempotency_key, result)
    return result
