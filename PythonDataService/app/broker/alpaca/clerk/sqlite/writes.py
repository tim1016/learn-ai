"""Write-side SQL primitives and path helpers shared by the live-append
commit path and mirror-rebuild replay.

Split out of ``repository.py`` (corrective foundation slice, Scope E) to
keep that module under the file-size ceiling — these are low-level,
call-order-sensitive helpers with no lock or fold-dispatch behavior of their
own; ``ClerkSqliteRepository`` is still the only caller.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path

from app.broker.alpaca.paths import resolve_contained_path, safe_path_component

TRANSITION_COLUMNS: tuple[str, ...] = (
    "sequence",
    "prev_hash",
    "row_hash",
    "authority_generation",
    "strategy_instance_id",
    "run_id",
    "command_id",
    "effect_operation_id",
    "order_ref",
    "broker_order_id",
    "transition_kind",
    "custody_owner",
    "execution_authority",
    "operation_state",
    "broker_state",
    "proof_reference",
    "source_event_at_ms",
    "clerk_observed_at_ms",
    "recorded_at_ms",
    "summary_code",
    "facts_schema_version",
    "facts_json",
)

_NON_KEY_TRANSITION_COLUMNS = tuple(
    c for c in TRANSITION_COLUMNS if c not in ("sequence", "prev_hash", "row_hash")
)


def default_lease_owner() -> str:
    """A unique-per-process token, not a bare PID — the OS can recycle a PID
    onto an unrelated later process, which a bare ``pid:N`` comparison would
    mistake for the same live owner (open-pr-review-2026-08-05.md P1 "Lease
    is never renewed", which this pairs with)."""
    return f"boot:{secrets.token_hex(8)}:pid:{os.getpid()}"


def account_paths(artifacts_root: Path, account_id: str) -> tuple[Path, Path]:
    """Return ``(accounts_root, account_dir)``, both containment-checked."""
    safe_account_id = safe_path_component(account_id, "account_id")
    accounts_root = resolve_contained_path(artifacts_root, "accounts", "alpaca")
    account_dir = resolve_contained_path(artifacts_root, "accounts", "alpaca", safe_account_id)
    return accounts_root, account_dir


def confined_account_file(artifacts_root: Path, account_id: str, filename: str) -> Path:
    """Confine the exact file path, not merely its containing account
    directory — a legitimate account directory can still contain a symlink
    named ``filename`` escaping ``artifacts_root`` (open-pr-review-2026-08-05.md
    P2, "`clerk.db` is not itself confined" / "mirror file is not itself
    confined")."""
    safe_account_id = safe_path_component(account_id, "account_id")
    return resolve_contained_path(artifacts_root, "accounts", "alpaca", safe_account_id, filename)


def row_to_payload(row: sqlite3.Row) -> dict:
    return {column: row[column] for column in TRANSITION_COLUMNS}


def insert_custody_transition_row(
    conn: sqlite3.Connection, *, sequence: int, prev_hash: str, row_hash: str, payload: dict
) -> None:
    """The one INSERT that writes a ``custody_transitions`` row.

    Shared by the live-append commit path and mirror-rebuild replay so the
    column list and value order are defined exactly once.
    """
    conn.execute(
        f"INSERT INTO custody_transitions (sequence, prev_hash, row_hash, "
        f"{', '.join(_NON_KEY_TRANSITION_COLUMNS)}) "
        f"VALUES (?, ?, ?, {', '.join('?' for _ in _NON_KEY_TRANSITION_COLUMNS)})",
        (sequence, prev_hash, row_hash, *(payload[c] for c in _NON_KEY_TRANSITION_COLUMNS)),
    )


def advance_control_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE control_meta SET control_revision = control_revision + 1 WHERE id = 1")
    return conn.execute("SELECT control_revision FROM control_meta WHERE id = 1").fetchone()[
        "control_revision"
    ]


def insert_mirror_fence_prepare_row(
    conn: sqlite3.Connection,
    *,
    sequence: int,
    row_hash: str,
    authority_generation: int,
    recorded_at_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO mirror_fence (sequence, phase, row_hash, authority_generation, recorded_at_ms) "
        "VALUES (?, 'PREPARE', ?, ?, ?)",
        (sequence, row_hash, authority_generation, recorded_at_ms),
    )
