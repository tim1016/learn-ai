"""Read-only verification shared by backup, restore, reset, and cutover."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.hashchain import GENESIS, verify_chain


class DatabaseVerificationFailed(Exception):
    """A database is corrupt, incomplete, or bound to another authority."""


@dataclass(frozen=True)
class DatabaseVerification:
    account_id: str
    authority_generation: int
    db_identity_token: str
    schema_version: int
    control_revision: int
    transition_count: int
    last_sequence: int
    last_row_hash: str


def verify_database(
    path: Path,
    *,
    expected_account_id: str,
    expected_generation: int | None = None,
    expected_db_identity: str | None = None,
    immutable: bool = False,
) -> DatabaseVerification:
    """Verify identity, schema, integrity, sequence continuity, and hash chain.

    The connection is URI read-only and ``query_only``.  This is safe for a
    cutover ``plan``: it neither acquires the execution lease nor updates
    ``last_open_at_ms``.
    """
    resolved = path.resolve(strict=True)
    immutable_option = "&immutable=1" if immutable else ""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro{immutable_option}",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            detail = None if integrity is None else integrity[0]
            raise DatabaseVerificationFailed(
                f"{path} integrity_check returned {detail!r}"
            )
        control = conn.execute(
            "SELECT schema_version, account_id, db_identity_token, "
            "authority_generation, control_revision FROM control_meta WHERE id = 1"
        ).fetchone()
        if control is None:
            raise DatabaseVerificationFailed(f"{path} has no control_meta row")
        _verify_control(
            control,
            path=path,
            expected_account_id=expected_account_id,
            expected_generation=expected_generation,
            expected_db_identity=expected_db_identity,
        )
        rows = conn.execute(
            f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} "
            "FROM custody_transitions ORDER BY sequence ASC"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseVerificationFailed(f"{path} cannot be verified: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()

    payloads = [writes.row_to_payload(row) for row in rows]
    for expected, row in enumerate(payloads, start=1):
        if row["sequence"] != expected:
            raise DatabaseVerificationFailed(
                f"{path} transition sequence gap: expected {expected}, found {row['sequence']}"
            )
        if row["authority_generation"] != control["authority_generation"]:
            raise DatabaseVerificationFailed(
                f"{path} transition {expected} belongs to a different generation"
            )
    bad_sequence = verify_chain(payloads)
    if bad_sequence is not None:
        raise DatabaseVerificationFailed(
            f"{path} hash-chain mismatch at sequence {bad_sequence}"
        )
    last_sequence = int(payloads[-1]["sequence"]) if payloads else 0
    last_row_hash = str(payloads[-1]["row_hash"]) if payloads else GENESIS
    return DatabaseVerification(
        account_id=str(control["account_id"]),
        authority_generation=int(control["authority_generation"]),
        db_identity_token=str(control["db_identity_token"]),
        schema_version=int(control["schema_version"]),
        control_revision=int(control["control_revision"]),
        transition_count=len(payloads),
        last_sequence=last_sequence,
        last_row_hash=last_row_hash,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_control(
    control: sqlite3.Row,
    *,
    path: Path,
    expected_account_id: str,
    expected_generation: int | None,
    expected_db_identity: str | None,
) -> None:
    if control["schema_version"] != schema.SCHEMA_VERSION:
        raise DatabaseVerificationFailed(
            f"{path} schema_version={control['schema_version']}, expected {schema.SCHEMA_VERSION}"
        )
    if control["account_id"] != expected_account_id:
        raise DatabaseVerificationFailed(
            f"{path} account_id={control['account_id']!r}, expected {expected_account_id!r}"
        )
    if expected_generation is not None and control["authority_generation"] != expected_generation:
        raise DatabaseVerificationFailed(
            f"{path} authority generation does not match expected generation"
        )
    if expected_db_identity is not None and control["db_identity_token"] != expected_db_identity:
        raise DatabaseVerificationFailed(
            f"{path} database identity does not match expected identity"
        )
