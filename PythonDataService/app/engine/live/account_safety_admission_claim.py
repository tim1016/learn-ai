"""ADR 0048 Decision 4f — the fenced single-writer admission claim.

Collapses the four account-safety admission marker classes (``gate``,
``writer``, ``readers/*``, ``participants/*``) into one liveness-bound,
generation-fenced claim record per account: an ``owner`` token, an
``acquired_at_ms``/``expires_at_ms`` liveness window, and a monotonic
fencing generation.

A bare renewable lease is explicitly rejected by the ADR: expiry makes an
orphan breakable, but expiry is not exclusion. A writer paused by a
stopped-world GC, a SIGSTOP, or a suspended VM does not know its claim
expired; if a new owner breaks the claim and starts writing while the old
one is still frozen, the old writer must not be able to resume and complete
a mutation it no longer has any right to. T7
(``docs/audits/bot-fleet-stress-2026-08-26.md``) is exactly this scenario
happening to the SQLite execution lease for real.

So breaking an expired claim durably increments the fencing generation
(:func:`try_acquire_or_break_claim`), and every protected mutation
**validates and persists in one SQLite transaction**
(:func:`open_write_transaction` + :func:`validate_claim_on_connection` +
:func:`persist_protected_payload_on_connection`) against this module's own
store. This is not two operations in sequence — even two operations both
against SQLite, if the second is a separate connection/transaction, leave a
real window between them for a paused writer to be overtaken and resume
into. ``BEGIN IMMEDIATE`` (``open_write_transaction``) takes the file's
write lock for the whole span, so a concurrent break attempt on another
connection must wait for this transaction to resolve — there is no instant
at which the claim can be observed "validated, but not yet written." This
mirrors, and shares the file-locking primitive with,
``ClerkSqliteRepository``'s own write-transaction discipline
(``app/broker/alpaca/clerk/sqlite/repository.py``), which the ADR names as
the reference for what "atomic against the durable state" means.
There is deliberately no "just validate my claim" entry point that manages
its own connection. Such a convenience reads as the safe thing and is not:
validating on one connection and then writing on another is a check-then-act
race even against this same store, which is the defect 4f.3 exists to
forbid. Callers validate on the connection they are about to write on.

Engine-local by design (ADR 0048 4a/4f): this is a private SQLite file per
IBKR account, under that account's own artifact directory. It is unrelated
to the Alpaca SQLite clerk (``app/broker/alpaca/clerk/sqlite/``), which
serves a disjoint, non-overlapping account namespace. The claim and the
payload it protects are colocated in this same file/table set deliberately
— atomicity across two independent stores (this claim DB and, say, a JSON
file on the filesystem) is not achievable without a distributed-transaction
protocol, which is out of proportion for an engine-local claim. A protected
mutation that cannot be brought into this store has no atomic option here
and must instead be routed through a single owner or a queue (ADR 0048
4f.3's escape hatch).
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.account_artifacts import account_artifact_file_path

ACCOUNT_SAFETY_ADMISSION_CLAIM_FILENAME = "account_safety_admission_claim.db"
DEFAULT_ADMISSION_CLAIM_TTL_MS = 30_000
_ACQUIRE_TIMEOUT_S = 10.0
_ACQUIRE_POLL_S = 0.01

logger = logging.getLogger(__name__)


class AccountSafetyAdmissionError(RuntimeError):
    """The fenced admission claim could not be acquired before the deadline."""


class AccountSafetyAdmissionClaimLost(RuntimeError):
    """A protected mutation's held generation is no longer current at the store.

    Raised by the store's own compare-and-swap check, never inferred from a
    writer's in-memory belief that it still holds the claim — that check is
    exactly the one a paused writer always passes (ADR 0048 4f).
    """


def account_safety_admission_claim_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the durable engine-local claim store for one account."""

    return account_artifact_file_path(artifacts_root, account_id, ACCOUNT_SAFETY_ADMISSION_CLAIM_FILENAME)


def default_admission_claim_owner() -> str:
    """A boot-unique token, not a bare PID.

    Same pattern ``default_lease_owner`` already uses
    (``app/broker/alpaca/clerk/sqlite/writes.py:49-54``): the OS can recycle
    a PID onto an unrelated later process, which a bare ``pid:N`` comparison
    would mistake for the same live owner.
    """

    return f"boot:{secrets.token_hex(8)}:pid:{os.getpid()}"


@dataclass(frozen=True)
class AccountSafetyAdmissionClaim:
    """A held claim: its identity, its fencing generation, and its store."""

    account_id: str
    owner: str
    generation: int
    acquired_at_ms: int
    expires_at_ms: int
    claim_path: Path
    ttl_ms: int


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_ACQUIRE_TIMEOUT_S, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS admission_claim ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "owner TEXT, "
        "acquired_at_ms INTEGER, "
        "expires_at_ms INTEGER NOT NULL DEFAULT 0, "
        "generation INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO admission_claim "
        "(id, owner, acquired_at_ms, expires_at_ms, generation) VALUES (1, NULL, NULL, 0, 0)"
    )
    # Colocated with the claim in the SAME database file/table set (not a
    # second store) so a protected write can validate the claim's
    # generation and persist the payload it guards in ONE SQLite
    # transaction -- see `open_write_transaction`. A protected mutation
    # that validated in this store but persisted somewhere else (a JSON
    # file via `os.replace`, say) is a check-then-act race, not an atomic
    # validation (ADR 0048 4f.3).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS protected_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "payload_json TEXT, "
        "updated_at_ms INTEGER)"
    )
    return conn


def open_write_transaction(artifacts_root: Path, account_id: str) -> sqlite3.Connection:
    """Open one SQLite write transaction against this account's claim store.

    ``BEGIN IMMEDIATE`` takes the database's write lock immediately, before
    any statement runs on the connection. That is what makes a
    validate-then-persist sequence performed on the returned connection
    genuinely atomic: a concurrent claim-break attempt on a *different*
    connection must wait for this transaction to commit or roll back, so
    nothing can ever observe this claim as "validated, but broken before
    the payload it guarded landed." The caller commits or rolls back and
    closes the connection.
    """

    path = account_safety_admission_claim_path(artifacts_root, account_id)
    conn = _connect(path)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def validate_claim_on_connection(
    conn: sqlite3.Connection, claim: AccountSafetyAdmissionClaim, *, now_ms: int
) -> None:
    """The CAS half of a protected write, on a caller-managed connection.

    Runs on a connection the caller controls, so it shares one transaction
    with the durable write that follows (see `open_write_transaction`).
    There is deliberately no variant that opens and commits its own
    connection: validating on one connection and writing on another is a
    check-then-act race even against the same store, and a convenience that
    made it easy would be the whole defect ADR 0048 4f.3 forbids, one call
    away.
    """

    cursor = conn.execute(
        "UPDATE admission_claim SET expires_at_ms = ? "
        "WHERE id = 1 AND owner = ? AND generation = ? AND expires_at_ms >= ?",
        (now_ms + claim.ttl_ms, claim.owner, claim.generation, now_ms),
    )
    if cursor.rowcount == 0:
        logger.error(
            "account safety admission claim generation superseded; rejecting protected mutation",
            extra={"account_id": claim.account_id, "owner": claim.owner, "generation": claim.generation},
        )
        raise AccountSafetyAdmissionClaimLost(
            f"account {claim.account_id!r} admission claim generation {claim.generation} "
            "is no longer current at the store; this handle can no longer complete its mutation"
        )


def persist_protected_payload_on_connection(
    conn: sqlite3.Connection, *, payload_json: str, updated_at_ms: int
) -> None:
    """The durable-write half of a protected write, on the SAME connection.

    Must be called only after :func:`validate_claim_on_connection` on a
    connection opened by :func:`open_write_transaction`, so the two share
    one transaction and neither can be observed to have happened without
    the other.
    """

    conn.execute(
        "INSERT INTO protected_state (id, payload_json, updated_at_ms) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json, updated_at_ms = excluded.updated_at_ms",
        (payload_json, updated_at_ms),
    )


def read_protected_payload(artifacts_root: Path, account_id: str) -> str | None:
    """Return the current protected payload, or ``None`` if never written.

    A pure read: unlike every other function here, this must not create the
    claim database (or its parent account directory) as a side effect of
    merely checking whether anything has ever been recorded — a read-only
    caller synthesizing a default for an account it has never observed
    (e.g. the account-truth snapshot) must not manufacture an artifact.
    """

    path = account_safety_admission_claim_path(artifacts_root, account_id)
    if not path.exists():
        return None
    conn = _connect(path)
    try:
        row = conn.execute("SELECT payload_json FROM protected_state WHERE id = 1").fetchone()
    finally:
        conn.close()
    return None if row is None else row["payload_json"]


def try_acquire_or_break_claim(
    artifacts_root: Path,
    account_id: str,
    *,
    owner: str,
    now_ms: int,
    ttl_ms: int = DEFAULT_ADMISSION_CLAIM_TTL_MS,
) -> AccountSafetyAdmissionClaim | None:
    """Atomically install ``owner`` iff the claim is free or expired.

    Breaking an expired claim (an orphaned owner whose ``expires_at_ms`` has
    passed) increments ``generation`` in the same statement — no separate
    repair step and no operator action, per ADR 0048 4d. Returns ``None`` on
    contention (a live claim held by someone else).
    """

    path = account_safety_admission_claim_path(artifacts_root, account_id)
    conn = _connect(path)
    try:
        previous = conn.execute("SELECT owner, expires_at_ms FROM admission_claim WHERE id = 1").fetchone()
        cursor = conn.execute(
            "UPDATE admission_claim SET owner = ?, acquired_at_ms = ?, expires_at_ms = ?, "
            "generation = generation + 1 "
            "WHERE id = 1 AND (owner IS NULL OR expires_at_ms < ?)",
            (owner, now_ms, now_ms + ttl_ms, now_ms),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM admission_claim WHERE id = 1").fetchone()
    finally:
        conn.close()
    if previous is not None and previous["owner"] is not None:
        # The class that caused the real S4 outage (ADR 0048 4e): an orphan
        # left behind by a dead or paused writer, broken here with no
        # operator action and no repair ceremony (4d) -- surfaced, not
        # silenced, since a breaking-this-often account is worth knowing
        # about even though it is now self-healing.
        logger.warning(
            "account safety admission claim broke an orphaned owner",
            extra={
                "account_id": account_id,
                "previous_owner": previous["owner"],
                "previous_expires_at_ms": previous["expires_at_ms"],
                "new_owner": owner,
                "generation": row["generation"],
            },
        )
    return AccountSafetyAdmissionClaim(
        account_id=account_id,
        owner=row["owner"],
        generation=row["generation"],
        acquired_at_ms=row["acquired_at_ms"],
        expires_at_ms=row["expires_at_ms"],
        claim_path=path,
        ttl_ms=ttl_ms,
    )


def _release(path: Path, *, owner: str, generation: int) -> None:
    """Best-effort clean release; a no-op if this claim was already broken."""

    conn = _connect(path)
    try:
        conn.execute(
            "UPDATE admission_claim SET owner = NULL, expires_at_ms = 0 WHERE id = 1 AND owner = ? AND generation = ?",
            (owner, generation),
        )
    finally:
        conn.close()


@contextmanager
def account_safety_admission_claim(
    artifacts_root: Path,
    account_id: str,
    *,
    owner: str | None = None,
    ttl_ms: int = DEFAULT_ADMISSION_CLAIM_TTL_MS,
    now_ms: int | None = None,
    acquire_timeout_s: float = _ACQUIRE_TIMEOUT_S,
) -> Iterator[AccountSafetyAdmissionClaim]:
    """Hold the one fenced single-writer claim for an account (ADR 0048 4f).

    Replaces the ``gate``/``writer``/``readers/*``/``participants/*`` O_EXCL
    marker turnstile: readers are not part of the protocol any more (4f item
    1), and an orphaned claim is broken by ordinary acquisition, not by an
    operator or a repair ceremony (4d).
    """

    resolved_owner = owner or default_admission_claim_owner()
    deadline = time.monotonic() + acquire_timeout_s
    while True:
        current_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        claim = try_acquire_or_break_claim(
            artifacts_root, account_id, owner=resolved_owner, now_ms=current_ms, ttl_ms=ttl_ms
        )
        if claim is not None:
            break
        if time.monotonic() >= deadline:
            raise AccountSafetyAdmissionError(
                f"account {account_id!r} admission claim is already held: "
                f"{account_safety_admission_claim_path(artifacts_root, account_id)}"
            )
        time.sleep(_ACQUIRE_POLL_S)
    try:
        yield claim
    finally:
        _release(claim.claim_path, owner=claim.owner, generation=claim.generation)


__all__ = [
    "ACCOUNT_SAFETY_ADMISSION_CLAIM_FILENAME",
    "DEFAULT_ADMISSION_CLAIM_TTL_MS",
    "AccountSafetyAdmissionClaim",
    "AccountSafetyAdmissionClaimLost",
    "AccountSafetyAdmissionError",
    "account_safety_admission_claim",
    "account_safety_admission_claim_path",
    "default_admission_claim_owner",
    "open_write_transaction",
    "persist_protected_payload_on_connection",
    "read_protected_payload",
    "try_acquire_or_break_claim",
    "validate_claim_on_connection",
]
