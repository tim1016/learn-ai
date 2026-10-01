"""Event-sourced SQLite authority for the Alpaca Account Clerk (#1375+).

Public surface: :class:`ClerkSqliteRepository` and its typed inputs/outputs.
Everything else in this package (``schema``, ``hashchain``, ``mirror``,
``registry``, ``folds``, ``reads``, ``writes``, ``models``) is implementation
detail behind that repository boundary: SQL stays behind it and never spreads
through routers, strategy, or presentation code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
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



def __getattr__(name: str) -> object:
    """Resolve repository exports only when a caller requests the repository.

    Pure inputs such as uncertainty causes are imported by live_envelope;
    eagerly loading the repository here would import that envelope again
    before its reservation type exists. The public export identities remain
    owned by repository.py, while pure submodules can finish independently.
    """
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.broker.alpaca.clerk.sqlite import repository

    return getattr(repository, name)
