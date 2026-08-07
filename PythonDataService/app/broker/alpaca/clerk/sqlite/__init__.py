"""Event-sourced SQLite authority for the Alpaca Account Clerk (#1375+).

Public surface: :class:`ClerkSqliteRepository` and its typed inputs/outputs.
Everything else in this package (``schema``, ``hashchain``, ``mirror``,
``registry``, ``folds``, ``reads``, ``writes``, ``models``) is implementation
detail behind that repository boundary — PRD §9.2: "SQL stays behind one
repository boundary and never spreads through routers, strategy, or
presentation code."
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    CommittedTransition,
    ControlMetaSnapshot,
    EffectOperationResource,
    OperationClaim,
    OrderResource,
    RunResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    AlreadyInitialized,
    ClerkSqliteError,
    ClerkSqliteRepository,
    DatabaseIdentityMismatch,
    DatabaseMissingAfterEstablishment,
    ExecutionLeaseHeld,
    ExecutionLeaseLost,
    HashChainBroken,
    IntegrityCheckFailed,
    OperationClaimError,
    RecoveryInProgress,
    RepositoryPoisoned,
    SchemaVersionMismatch,
)

__all__ = [
    "AlreadyInitialized",
    "ClerkSqliteError",
    "ClerkSqliteRepository",
    "CommandCreated",
    "CommandExistingConflict",
    "CommandExistingSame",
    "CommandResource",
    "CommittedTransition",
    "ControlMetaSnapshot",
    "DatabaseIdentityMismatch",
    "DatabaseMissingAfterEstablishment",
    "EffectOperationResource",
    "ExecutionLeaseHeld",
    "ExecutionLeaseLost",
    "HashChainBroken",
    "IntegrityCheckFailed",
    "OperationClaim",
    "OperationClaimError",
    "OrderResource",
    "RecoveryInProgress",
    "RepositoryPoisoned",
    "RunResource",
    "SchemaVersionMismatch",
    "TransitionInput",
]
