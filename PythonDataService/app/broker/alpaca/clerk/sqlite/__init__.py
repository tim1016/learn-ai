"""Event-sourced SQLite authority for the Alpaca Account Clerk (#1375+).

Public surface: :class:`ClerkSqliteRepository` and its typed inputs/outputs.
Everything else in this package (``schema``, ``hashchain``, ``mirror``,
``registry``, ``folds``) is implementation detail behind that repository
boundary — PRD §9.2: "SQL stays behind one repository boundary and never
spreads through routers, strategy, or presentation code."
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.repository import (
    AlreadyInitialized,
    ClerkSqliteError,
    ClerkSqliteRepository,
    CommandResource,
    CommittedTransition,
    ControlMetaSnapshot,
    DatabaseIdentityMismatch,
    DatabaseMissingAfterEstablishment,
    ExecutionLeaseHeld,
    HashChainBroken,
    IntegrityCheckFailed,
    ReservationConflict,
    ReservedExisting,
    ReservedNew,
    RunResource,
    SchemaVersionMismatch,
    TransitionInput,
)

__all__ = [
    "AlreadyInitialized",
    "ClerkSqliteError",
    "ClerkSqliteRepository",
    "CommandResource",
    "CommittedTransition",
    "ControlMetaSnapshot",
    "DatabaseIdentityMismatch",
    "DatabaseMissingAfterEstablishment",
    "ExecutionLeaseHeld",
    "HashChainBroken",
    "IntegrityCheckFailed",
    "ReservationConflict",
    "ReservedExisting",
    "ReservedNew",
    "RunResource",
    "SchemaVersionMismatch",
    "TransitionInput",
]
