"""Alpaca Clerk (Broker System v2, phase 2).

The Clerk is the **in-process** single-writer that owns order submission for
Alpaca. Unlike the IBKR clerk (a separate process arbitrating one shared
gateway session with a lease + generation fencing), Alpaca is stateless REST —
there is no single gateway session to arbitrate — so the Clerk is a plain async
service running inside the data-plane container.

**Single-writer / single-worker constraint.** Serial submission per account is
guaranteed by (a) an ``asyncio.Lock`` intake lock and (b) the deployment
running a single uvicorn worker. Two workers would each hold their own lock and
break serialization; the append-only journal's ``fsync`` durability holds
regardless, but the "one order in flight at a time" invariant does not. Do not
scale this service to multiple workers without moving the intake lock to a
cross-process primitive.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.clerk import (
    AlpacaClerk,
    ClerkAdmissionSnapshotChangedError,
    InventoryBaselineRefusedError,
    get_alpaca_clerk,
    reset_alpaca_clerk_for_testing,
    set_alpaca_clerk,
)
from app.broker.alpaca.clerk.custody_resolution_store import (
    CustodyResolutionOutcomeUnknownError,
)
from app.broker.alpaca.clerk.diagnosis import (
    CustodyConflictResponse,
    CustodyDiagnosis,
    CustodyDivergence,
    CustodyPositionDelta,
    CustodyResolutionReceipt,
    CustodyResolutionRequest,
    CustodyResolutionStep,
    CustodyResolutionStepResult,
    CustodySnapshotChangedError,
)
from app.broker.alpaca.clerk.exposure import (
    FlattenRefusedError,
    InstanceExposure,
    project_instance_exposure,
    project_instance_timeline,
    strategy_instance_id_for_namespace,
    verify_flatten,
)
from app.broker.alpaca.clerk.journal import get_clerk_settings
from app.broker.alpaca.clerk.models import (
    STREAM_HEALTH_HOLD_CODE,
    AccountFreezeState,
    ChannelHealth,
    ClerkEntryKind,
    ClerkStatus,
    HoldState,
    InstanceCustodyProof,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegError,
    OrderLegResult,
    OrderSubmitResult,
    ReconciliationSummary,
)
from app.broker.alpaca.clerk.stream_health import (
    StreamHealthGate,
    build_default_stream_health_gate,
    execution_channel_health,
    market_data_channel_health,
    stream_health_refusal,
)
from app.broker.alpaca.clerk.sweep import (
    ReconciliationSweep,
    get_reconciliation_sweep,
    reset_reconciliation_sweep_for_testing,
    set_reconciliation_sweep,
)

__all__ = [
    "STREAM_HEALTH_HOLD_CODE",
    "AccountFreezeState",
    "AlpacaClerk",
    "ChannelHealth",
    "ClerkAdmissionSnapshotChangedError",
    "ClerkEntryKind",
    "ClerkStatus",
    "CustodyConflictResponse",
    "CustodyDiagnosis",
    "CustodyDivergence",
    "CustodyPositionDelta",
    "CustodyResolutionOutcomeUnknownError",
    "CustodyResolutionReceipt",
    "CustodyResolutionRequest",
    "CustodyResolutionStep",
    "CustodyResolutionStepResult",
    "CustodySnapshotChangedError",
    "FlattenRefusedError",
    "HoldState",
    "InstanceCustodyProof",
    "InstanceExposure",
    "InventoryBaselineRefusedError",
    "OrderCancelResult",
    "OrderJournalEntry",
    "OrderLegError",
    "OrderLegResult",
    "OrderSubmitResult",
    "ReconciliationSummary",
    "ReconciliationSweep",
    "StreamHealthGate",
    "build_default_stream_health_gate",
    "execution_channel_health",
    "get_alpaca_clerk",
    "get_clerk_settings",
    "get_reconciliation_sweep",
    "market_data_channel_health",
    "project_instance_exposure",
    "project_instance_timeline",
    "reset_alpaca_clerk_for_testing",
    "reset_reconciliation_sweep_for_testing",
    "set_alpaca_clerk",
    "set_reconciliation_sweep",
    "strategy_instance_id_for_namespace",
    "stream_health_refusal",
    "verify_flatten",
]
