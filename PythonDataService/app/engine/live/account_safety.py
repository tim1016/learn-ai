"""Durable account suspension for attributable-but-unmanaged custody.

The account Clerk journal remains the source of order, execution, and exposure
facts.  This module owns only the account-level *permission* projection that
is needed when a retired bot still has nonterminal custody.  A suspension is
therefore neither foreign exposure nor a terminal state for healthy siblings:
it blocks new account entries until the Clerk records a fresh clean/adopted
epoch reconciliation after the retired custody is gone.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.account_artifacts import account_artifact_file_path, read_account_clerk_generation
from app.engine.live.account_clerk_journal import is_economic_terminal_broker_event
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_epoch import AccountEpoch, AccountEpochState
from app.engine.live.account_safety_admission_claim import (
    AccountSafetyAdmissionClaim,
    AccountSafetyAdmissionError,
    account_safety_admission_claim,
    open_write_transaction,
    persist_protected_payload_on_connection,
    read_protected_payload,
    validate_claim_on_connection,
)
from app.engine.live.live_state_sidecar import _fsync_parent_dir
from app.schemas.account_truth import AccountTruthResponse

ACCOUNT_SAFETY_FILENAME = "account_safety.json"
_ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S = 10.0
_ACCOUNT_SAFETY_ADMISSION_POLL_S = 0.01
logger = logging.getLogger(__name__)


class AccountSafetyVerdict(StrEnum):
    """Closed account-level permission vocabulary from ADR 0033."""

    CLEAN = "CLEAN"
    RECONCILING = "RECONCILING"
    SUSPENDED = "SUSPENDED"
    CONTAMINATED = "CONTAMINATED"


class RetiredOwnerCustody(BaseModel):
    """Immutable Clerk identity retained when an originator is retired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    order_ref: str = Field(min_length=1, max_length=512)
    # Clerk evidence has a durable order/intention lifecycle; broker evidence
    # is an independently observed fact that only a later fresh Account Truth
    # observation may clear.
    evidence_source: Literal["clerk", "broker"] = "clerk"


class AccountSafetySuspension(BaseModel):
    """The bounded evidence that keeps account entry permission suspended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suspension_id: str = Field(min_length=1, max_length=256)
    reason_code: str = "RETIRED_OWNER_LIVE_EXPOSURE"
    custody: tuple[RetiredOwnerCustody, ...] = ()
    detected_at_ms: int = Field(ge=0)
    reconciliation_epoch: AccountEpoch | None = None
    reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    requires_broker_clearance: bool = False


class AccountSafetyState(BaseModel):
    """Durable permission state, never a second order/exposure ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    verdict: AccountSafetyVerdict = AccountSafetyVerdict.CLEAN
    suspension: AccountSafetySuspension | None = None
    last_reconciliation_id: str | None = Field(default=None, min_length=1, max_length=128)
    last_broker_observed_at_ms: int | None = Field(default=None, ge=0)
    updated_at_ms: int = Field(ge=0)


class AccountSafetyCorruptError(RuntimeError):
    """The durable safety projection cannot be read and must fail closed."""


class AccountSafetyEntryBlockedError(RuntimeError):
    """A suspended account rejected a new entry before A0 or A1."""

    def __init__(self, state: AccountSafetyState) -> None:
        super().__init__(
            state.suspension.reason_code
            if state.suspension is not None
            else "ACCOUNT_SAFETY_ENTRY_BLOCKED"
        )
        self.state = state


class AccountSafetyAdmissionTransition(BaseModel):
    """The O_EXCL mutex owner for a durable safety-state transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    clerk_generation: int = Field(ge=0)
    acquired_at_ms: int = Field(ge=0)


def account_safety_path(artifacts_root: Path, account_id: str) -> Path:
    """Return a stable per-account path anchor.

    Durable state now lives in the ADR 0048 4f admission-claim SQLite store
    (`account_safety_admission_claim.py`), not at this path going forward —
    but this path is also the pre-migration location, and any state
    recorded there before this change must still be seen: see
    `_read_legacy_json_compat`. `_account_safety_state_transition_path`
    also derives its sibling marker name from this path; that inner
    transition lock is unrelated to 4f's four admission-marker classes and
    is intentionally left untouched here.
    """

    return account_artifact_file_path(artifacts_root, account_id, ACCOUNT_SAFETY_FILENAME)


def account_safety_state_exists(artifacts_root: Path, account_id: str) -> bool:
    """Return whether durable safety state has ever been recorded.

    `AccountSafetyAuthority.read()` always synthesizes a CLEAN default when
    absent, so callers that must distinguish "never observed" from "an
    explicit CLEAN was recorded" (e.g. the account-truth snapshot's
    ``ACCOUNT_SAFETY_NOT_AVAILABLE`` reason) use this instead. Must agree
    with `_read_locked`'s legacy-JSON compat fallback: an account whose
    only recorded state is still the pre-migration JSON file has recorded
    state, not none.
    """

    if read_protected_payload(artifacts_root, account_id) is not None:
        return True
    return account_safety_path(artifacts_root, account_id).exists()


def _account_safety_state_transition_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the cross-runtime mutex for durable safety-state transitions."""

    target = account_safety_path(artifacts_root, account_id)
    return target.with_name(f".{target.name}.transition")


def _create_exclusive_marker(path: Path, *, deadline: float, payload: str | None = None) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AccountSafetyAdmissionError(
                    f"account safety admission marker is already held: {path}"
                ) from None
            time.sleep(_ACCOUNT_SAFETY_ADMISSION_POLL_S)
            continue
        except OSError as exc:
            raise AccountSafetyAdmissionError(
                f"account safety admission marker could not be acquired: {path}: {exc}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload or f"{{\"created_at_ms\":{time.time_ns() // 1_000_000}}}")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_parent_dir(path)
        return


def _remove_marker(path: Path) -> None:
    try:
        path.unlink()
        _fsync_parent_dir(path)
    except FileNotFoundError:
        logger.error("account safety admission marker disappeared while held", extra={"path": str(path)})


@contextmanager
def _account_safety_state_transition_lock(
    artifacts_root: Path,
    account_id: str,
) -> Iterator[None]:
    """Serialize durable safety projection mutations with a host/VM O_EXCL lock."""

    with _account_safety_generation_transition_lock(
        _account_safety_state_transition_path(artifacts_root, account_id),
        artifacts_root=artifacts_root,
        account_id=account_id,
    ):
        yield


@contextmanager
def _account_safety_generation_transition_lock(
    transition_path: Path,
    *,
    artifacts_root: Path,
    account_id: str,
) -> Iterator[None]:
    """Acquire one generation-stamped O_EXCL transition marker."""

    transition_path.parent.mkdir(parents=True, exist_ok=True)
    transition = AccountSafetyAdmissionTransition(
        account_id=account_id,
        clerk_generation=_current_clerk_generation(artifacts_root, account_id),
        acquired_at_ms=time.time_ns() // 1_000_000,
    )
    _create_exclusive_marker(
        transition_path,
        deadline=time.monotonic() + _ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S,
        payload=transition.model_dump_json(),
    )
    try:
        yield
    finally:
        _remove_marker(transition_path)


def _current_clerk_generation(artifacts_root: Path, account_id: str) -> int:
    generation = read_account_clerk_generation(artifacts_root, account_id)
    return generation.generation if generation is not None else 0


@contextmanager
def account_safety_admission_lock(
    artifacts_root: Path, account_id: str
) -> Iterator[AccountSafetyAdmissionClaim]:
    """Hold the fenced single-writer admission claim for an account.

    ADR 0048 Decision 4f: collapses the four O_EXCL marker classes this used
    to coordinate (``gate``, ``writer``, ``readers/*``, ``participants/*``)
    into one liveness-bound, generation-fenced claim
    (:mod:`app.engine.live.account_safety_admission_claim`). Readers are not
    part of the protocol any more — the shared entry permit
    (``account_safety_entry_admission_lock``) and the writer turnstile it
    drained are retired with it, per the provenance in 4f: the reader half's
    only caller was the legacy ``AccountClerk`` control path removed by
    #1679, so ``readers/`` was never populated in production.

    Yields the held :class:`AccountSafetyAdmissionClaim` so a caller
    performing a protected mutation can validate it inside the same
    transaction as that mutation (see :func:`open_write_transaction`).
    """

    with account_safety_admission_claim(artifacts_root, account_id) as claim:
        yield claim


def retired_owner_nonterminal_custody(
    entries: Iterable[AccountClerkJournalEntry],
    *,
    strategy_instance_id: str,
) -> tuple[RetiredOwnerCustody, ...]:
    """Return the exact retired-owner intents that still need a manager.

    A broker acknowledgement is deliberately nonterminal: it can still carry
    an open order or live position.  Only an economic terminal callback,
    explicit A0 expiry, or an adopted/halt reconciliation resolves custody.
    """

    grouped: dict[str, list[AccountClerkJournalEntry]] = {}
    for entry in entries:
        if entry.intent is None or entry.intent.strategy_instance_id != strategy_instance_id:
            continue
        grouped.setdefault(entry.intent.intent_id, []).append(entry)

    retained: list[RetiredOwnerCustody] = []
    for intent_id, intent_entries in grouped.items():
        recorded = next((entry for entry in intent_entries if entry.entry_kind == "recorded"), None)
        if recorded is None or recorded.intent is None or _custody_is_terminal(intent_entries):
            continue
        retained.append(
            RetiredOwnerCustody(
                account_id=recorded.intent.account_id,
                strategy_instance_id=strategy_instance_id,
                intent_id=intent_id,
                order_ref=recorded.intent.order_ref,
            )
        )
    return tuple(
        sorted(
            retained,
            key=lambda item: (
                item.account_id,
                item.order_ref,
                item.intent_id,
                item.evidence_source,
            ),
        )
    )


class AccountSafetyAuthority:
    """Single-writer durable suspension authority for one account.

    The Clerk calls :meth:`bind_reconciliation` only after it invalidates the
    account epoch.  The authority itself cannot create a clean state: only
    :meth:`lift_if_proven` can do that, and it requires the exact successor
    reconciliation receipt plus zero retained retired-owner custody.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        now_ms: Callable[[], int],
        admission_claim: AccountSafetyAdmissionClaim | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._now_ms = now_ms
        # Held only by a caller inside `account_safety_admission_lock`'s
        # critical section (ADR 0048 4f). When set, every durable write
        # validates it and persists in the SAME SQLite transaction (ADR
        # 0048 4f.3), so a writer paused between the check and the write
        # cannot complete a stale mutation after the claim was broken.
        self._admission_claim = admission_claim

    def read(self) -> AccountSafetyState:
        """Read the durable state, synthesizing clean only when absent."""

        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked()
        return state or AccountSafetyState(
            account_id=self._account_id,
            updated_at_ms=self._now_ms(),
        )

    def suspend_retired_owner_custody(
        self,
        custody: Iterable[RetiredOwnerCustody],
    ) -> AccountSafetyState:
        """Durably close entry permission while retaining exact owner evidence."""

        retained = tuple(
            sorted(
                set(custody),
                key=lambda item: (
                    item.account_id,
                    item.order_ref,
                    item.intent_id,
                    item.evidence_source,
                ),
            )
        )
        if any(item.account_id != self._account_id for item in retained):
            raise ValueError("ACCOUNT_SAFETY_CUSTODY_ACCOUNT_MISMATCH")
        if not retained:
            return self.read()
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            existing = self._read_locked()
            existing_custody = existing.suspension.custody if existing and existing.suspension else ()
            combined = tuple(
                sorted(
                    {*existing_custody, *retained},
                    key=lambda item: (
                        item.account_id,
                        item.order_ref,
                        item.intent_id,
                        item.evidence_source,
                    ),
                )
            )
            if (
                existing is not None
                and existing.verdict is AccountSafetyVerdict.SUSPENDED
                and existing.suspension is not None
                and existing.suspension.custody == combined
            ):
                return existing
            suspension_id = _suspension_id(self._account_id, combined)
            state = AccountSafetyState(
                account_id=self._account_id,
                verdict=AccountSafetyVerdict.SUSPENDED,
                suspension=AccountSafetySuspension(
                    suspension_id=suspension_id,
                    custody=combined,
                    detected_at_ms=self._now_ms(),
                ),
                last_reconciliation_id=(existing.last_reconciliation_id if existing is not None else None),
                last_broker_observed_at_ms=(
                    existing.last_broker_observed_at_ms if existing is not None else None
                ),
                updated_at_ms=self._now_ms(),
            )
            self._write_locked(state)
            return state

    def mark_broker_clearance_required(self, *, detected_at_ms: int) -> AccountSafetyState:
        """Require a later fresh Account Truth observation before release.

        A callback can terminally fill an order while leaving a live position.
        The Clerk journal alone cannot attribute a same-symbol position shared
        with a healthy sibling, so it must not release this suspension until
        Account Truth has observed the retired broker exposure gone.
        """

        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked()
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            suspension = state.suspension
            assert suspension is not None
            updated = state.model_copy(
                update={
                    "suspension": suspension.model_copy(
                        update={
                            "requires_broker_clearance": True,
                            "reconciliation_epoch": None,
                            "reconciliation_id": None,
                        }
                    ),
                    # Do not move the observation watermark backwards.  A
                    # late callback still requires a *newer* full Truth
                    # sweep; an already-recorded snapshot cannot prove that
                    # the callback's resulting position was cleared.
                    "last_broker_observed_at_ms": max(
                        detected_at_ms,
                        state.last_broker_observed_at_ms or 0,
                    ),
                    "updated_at_ms": self._now_ms(),
                }
            )
            self._write_locked(updated)
            return updated

    def observe_broker_retired_owner_custody(
        self,
        custody: Iterable[RetiredOwnerCustody],
        *,
        observed_at_ms: int,
    ) -> AccountSafetyState:
        """Replace broker-derived retired custody from one ordered Truth sweep.

        A fresh clean observation only removes the *broker* evidence. It never
        clears the suspension itself; the Clerk still needs its exact successor
        epoch reconciliation before :meth:`lift_if_proven` can reopen entries.
        """

        observed = tuple(
            sorted(
                set(custody),
                key=lambda item: (
                    item.account_id,
                    item.order_ref,
                    item.intent_id,
                    item.evidence_source,
                ),
            )
        )
        if any(item.account_id != self._account_id for item in observed):
            raise ValueError("ACCOUNT_SAFETY_CUSTODY_ACCOUNT_MISMATCH")
        if any(item.evidence_source != "broker" for item in observed):
            raise ValueError("ACCOUNT_SAFETY_BROKER_EVIDENCE_SOURCE_REQUIRED")
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            existing = self._read_locked()
            if (
                existing is not None
                and existing.last_broker_observed_at_ms is not None
                and observed_at_ms <= existing.last_broker_observed_at_ms
            ):
                return existing
            prior = existing.suspension.custody if existing and existing.suspension else ()
            clerk_custody = tuple(item for item in prior if item.evidence_source == "clerk")
            combined = tuple(
                sorted(
                    {*clerk_custody, *observed},
                    key=lambda item: (
                        item.account_id,
                        item.order_ref,
                        item.intent_id,
                        item.evidence_source,
                    ),
                )
            )
            prior_suspension = existing.suspension if existing is not None else None
            prior_broker_custody = tuple(
                item
                for item in (prior_suspension.custody if prior_suspension is not None else ())
                if item.evidence_source == "broker"
            )
            broker_evidence_changed = prior_broker_custody != observed
            if not combined and existing is None:
                state = AccountSafetyState(
                    account_id=self._account_id,
                    last_broker_observed_at_ms=observed_at_ms,
                    updated_at_ms=self._now_ms(),
                )
            elif not combined:
                # A clean Truth sweep must not manufacture a suspension for
                # an account that was already clean.  If it cleared the final
                # broker-owned evidence from a prior suspension, retain that
                # suspension until the Clerk supplies a new epoch proof.
                assert existing is not None
                if existing.verdict is not AccountSafetyVerdict.SUSPENDED:
                    state = existing.model_copy(
                        update={
                            "last_broker_observed_at_ms": observed_at_ms,
                            "updated_at_ms": self._now_ms(),
                        }
                    )
                else:
                    assert prior_suspension is not None
                    state = existing.model_copy(
                        update={
                            "suspension": prior_suspension.model_copy(
                                update={
                                    "custody": (),
                                    "suspension_id": _suspension_id(self._account_id, ()),
                                    "reconciliation_epoch": None,
                                    "reconciliation_id": None,
                                    "requires_broker_clearance": False,
                                }
                            ),
                            "last_broker_observed_at_ms": observed_at_ms,
                            "updated_at_ms": self._now_ms(),
                        }
                    )
            else:
                # Do not invalidate an already bound Clerk proof merely
                # because a routine clean account sweep repeated while the
                # suspension is supported only by Clerk custody.  Any changed
                # broker fact, including its explicit clean successor, does
                # invalidate that proof.
                reuse_reconciliation = (
                    prior_suspension is not None
                    and not broker_evidence_changed
                    and not prior_suspension.requires_broker_clearance
                )
                state = AccountSafetyState(
                    account_id=self._account_id,
                    verdict=AccountSafetyVerdict.SUSPENDED,
                    suspension=AccountSafetySuspension(
                        suspension_id=_suspension_id(self._account_id, combined),
                        custody=combined,
                        detected_at_ms=(
                            prior_suspension.detected_at_ms
                            if prior_suspension is not None
                            else observed_at_ms
                        ),
                        reconciliation_epoch=(
                            prior_suspension.reconciliation_epoch
                            if reuse_reconciliation
                            else None
                        ),
                        reconciliation_id=(
                            prior_suspension.reconciliation_id if reuse_reconciliation else None
                        ),
                        # A broker fact must be followed by its clean Truth
                        # successor and then a Clerk epoch proof.
                        requires_broker_clearance=bool(observed),
                    ),
                    last_reconciliation_id=(
                        existing.last_reconciliation_id if existing is not None else None
                    ),
                    last_broker_observed_at_ms=observed_at_ms,
                    updated_at_ms=self._now_ms(),
                )
            self._write_locked(state)
            return state

    def bind_reconciliation(self, epoch: AccountEpochState) -> AccountSafetyState:
        """Bind a suspension to the invalid epoch that must prove its cure."""

        if epoch.status != "INVALID" or epoch.reconciliation_id is None:
            raise ValueError("ACCOUNT_SAFETY_RECONCILIATION_EPOCH_REQUIRED")
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked()
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            assert state.suspension is not None
            if (
                state.suspension.reconciliation_epoch == epoch.current_epoch
                and state.suspension.reconciliation_id == epoch.reconciliation_id
            ):
                return state
            updated = state.model_copy(
                update={
                    "suspension": state.suspension.model_copy(
                        update={
                            "reconciliation_epoch": epoch.current_epoch,
                            "reconciliation_id": epoch.reconciliation_id,
                        }
                    ),
                    "updated_at_ms": self._now_ms(),
                }
            )
            self._write_locked(updated)
            return updated

    def require_entry_admission(self) -> AccountSafetyState:
        """Fail before a new account entry can create custody or broker risk."""

        state = self.read()
        if state.verdict is not AccountSafetyVerdict.CLEAN:
            raise AccountSafetyEntryBlockedError(state)
        return state

    def lift_if_proven(
        self,
        *,
        epoch: AccountEpochState,
        retained_custody: Iterable[RetiredOwnerCustody],
    ) -> AccountSafetyState:
        """Lift only from the exact successor of a suspension's epoch proof."""

        remaining = tuple(retained_custody)
        with _account_safety_state_transition_lock(self._artifacts_root, self._account_id):
            state = self._read_locked()
            if state is None or state.verdict is not AccountSafetyVerdict.SUSPENDED:
                return state or AccountSafetyState(
                    account_id=self._account_id,
                    updated_at_ms=self._now_ms(),
                )
            suspension = state.suspension
            assert suspension is not None
            if not _may_lift(suspension, epoch=epoch, retained_custody=remaining):
                return state
            updated = AccountSafetyState(
                account_id=self._account_id,
                verdict=AccountSafetyVerdict.CLEAN,
                last_reconciliation_id=epoch.last_reconciliation_id,
                updated_at_ms=self._now_ms(),
            )
            self._write_locked(updated)
            return updated

    def _read_locked(self) -> AccountSafetyState | None:
        payload_json = read_protected_payload(self._artifacts_root, self._account_id)
        if payload_json is None:
            payload_json = self._read_legacy_json_compat()
            if payload_json is None:
                return None
        try:
            state = AccountSafetyState.model_validate_json(payload_json)
        except (ValidationError, ValueError) as exc:
            raise AccountSafetyCorruptError(
                f"account safety state for {self._account_id!r} is unreadable: {exc}"
            ) from exc
        if state.account_id != self._account_id:
            raise AccountSafetyCorruptError("account safety state account id does not match its store")
        return state

    def _read_legacy_json_compat(self) -> str | None:
        """ADR 0048 4f migration compat: the pre-SQLite JSON projection this
        account's state lived in before this change (Decision 2's "a
        storage move of durable state is a migration, not a refactor, and
        the data has to survive the version boundary" applies here too,
        even though this move is far smaller than the holds->uncertainties
        migration it names).

        Self-retiring: once *anything* protected writes this account's
        state again, `_write_locked` persists into SQLite and every
        subsequent read finds it there, before this fallback is ever
        consulted -- no migration ceremony, no operator action. Never
        deletes or rewrites the legacy file: this repo's convention for
        authority files is to relocate on retirement, never delete, and
        that is an operator action outside this module's job.
        """

        path = account_safety_path(self._artifacts_root, self._account_id)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AccountSafetyCorruptError(f"legacy account safety state at {path} is unreadable: {exc}") from exc

    def _write_locked(self, state: AccountSafetyState) -> None:
        # ADR 0048 4f.3: validate the held admission claim's generation and
        # persist the payload it protects in ONE SQLite transaction, not a
        # check followed by a separate durable write. A writer paused
        # (stopped-world GC, SIGSTOP, a suspended VM) between the two would
        # otherwise resume and complete a stale mutation after the claim
        # was broken out from under it — `open_write_transaction`'s
        # `BEGIN IMMEDIATE` holds the file's write lock across both steps,
        # so a concurrent break attempt cannot land in between.
        updated_at_ms = self._now_ms()
        conn = open_write_transaction(self._artifacts_root, self._account_id)
        try:
            if self._admission_claim is not None:
                validate_claim_on_connection(conn, self._admission_claim, now_ms=updated_at_ms)
            persist_protected_payload_on_connection(
                conn, payload_json=state.model_dump_json(), updated_at_ms=updated_at_ms
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _custody_is_terminal(entries: Iterable[AccountClerkJournalEntry]) -> bool:
    return any(
        entry.entry_kind == "custody_expired_before_submit"
        # A confirmed broker cancel means the order was never filled and cannot
        # create future exposure — the cancel handshake closed the Clerk's book
        # on this intent before any position was opened.
        or entry.entry_kind == "cancel_confirmed"
        or (
            entry.entry_kind == "reconciliation"
            and entry.reconciliation_verdict in {"RECOVER_ADOPT", "HALT"}
        )
        or (
            entry.entry_kind == "broker_event"
            and is_economic_terminal_broker_event(entry.broker_event)
        )
        for entry in entries
    )


def _may_lift(
    suspension: AccountSafetySuspension,
    *,
    epoch: AccountEpochState,
    retained_custody: tuple[RetiredOwnerCustody, ...],
) -> bool:
    return (
        not retained_custody
        and not suspension.requires_broker_clearance
        and not any(item.evidence_source == "broker" for item in suspension.custody)
        and suspension.reconciliation_epoch is not None
        and suspension.reconciliation_id is not None
        and epoch.status == "CLEAN"
        and epoch.reconciliation_verdict in {"CLEAN", "ADOPTED"}
        and epoch.last_reconciliation_id == suspension.reconciliation_id
    )


def _suspension_id(account_id: str, custody: tuple[RetiredOwnerCustody, ...]) -> str:
    # Order refs can be hundreds of characters long.  Keep this durable ID
    # bounded while retaining a deterministic receipt identity for the exact
    # custody set; the full evidence stays in ``AccountSafetySuspension``.
    identity = "\0".join(
        "\0".join(
            (
                item.account_id,
                item.strategy_instance_id,
                item.intent_id,
                item.order_ref,
                item.evidence_source,
            )
        )
        for item in custody
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"account-suspension:{account_id}:{digest}"


def retired_owner_broker_custody_from_account_truth(
    account_truth: AccountTruthResponse,
) -> tuple[RetiredOwnerCustody, ...]:
    """Project only exact retired-owner broker facts from an Account Truth sweep."""

    account_id = account_truth.account_id
    if account_id is None:
        return ()
    custody: set[RetiredOwnerCustody] = set()
    for order in account_truth.orders:
        if (
            order.fact_kind == "open_order"
            and _is_retired_truth_owner(order.owner.owner_class, order.owner.owner_binding_state)
        ):
            custody.add(
                RetiredOwnerCustody(
                    account_id=account_id,
                    strategy_instance_id=order.owner.owner_key,
                    intent_id=_broker_evidence_id("order", order.lifecycle_id),
                    order_ref=order.order_ref or _broker_evidence_ref("order", order.lifecycle_id),
                    evidence_source="broker",
                )
            )
    for position in account_truth.positions:
        if _is_retired_truth_owner(position.owner.owner_class, position.owner.owner_binding_state):
            identity = f"{position.con_id}:{position.symbol.upper()}"
            custody.add(
                RetiredOwnerCustody(
                    account_id=account_id,
                    strategy_instance_id=position.owner.owner_key,
                    intent_id=_broker_evidence_id("position", identity),
                    order_ref=_broker_evidence_ref("position", identity),
                    evidence_source="broker",
                )
            )
    return tuple(
        sorted(
            custody,
            key=lambda item: (
                item.account_id,
                item.order_ref,
                item.intent_id,
                item.evidence_source,
            ),
        )
    )


def _is_retired_truth_owner(owner_class: str, binding_state: str) -> bool:
    return owner_class in {"bot", "mixed_known"} and binding_state == "RETIRED"


def _broker_evidence_id(kind: str, identity: str) -> str:
    return f"broker-{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _broker_evidence_ref(kind: str, identity: str) -> str:
    return f"broker-{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def account_safety_blocks_current_bot(
    safety: AccountSafetyState,
    strategy_instance_id: str,
) -> bool:
    """Return True iff account safety should block this specific bot.

    A suspension whose custody list names only other bots' strategy_instance_ids
    must not cascade to bots that have no retired-owner exposure of their own.
    When custody is empty or strategy_instance_id is unknown the check is
    conservative (True) so no unexamined gap silently permits entry.
    """
    if safety.verdict is not AccountSafetyVerdict.SUSPENDED:
        return False
    suspension = safety.suspension
    if suspension is None or not suspension.custody:
        return True
    if not strategy_instance_id:
        return True
    return any(c.strategy_instance_id == strategy_instance_id for c in suspension.custody)


__all__ = [
    "ACCOUNT_SAFETY_FILENAME",
    "AccountSafetyAdmissionError",
    "AccountSafetyAuthority",
    "AccountSafetyCorruptError",
    "AccountSafetyEntryBlockedError",
    "AccountSafetyState",
    "AccountSafetySuspension",
    "AccountSafetyVerdict",
    "RetiredOwnerCustody",
    "account_safety_admission_lock",
    "account_safety_blocks_current_bot",
    "account_safety_path",
    "account_safety_state_exists",
    "retired_owner_broker_custody_from_account_truth",
    "retired_owner_nonterminal_custody",
]
