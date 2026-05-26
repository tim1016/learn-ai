"""Walk-forward analysis over ``StrategySpec`` runs.

Phase C of the build-alpha-style research pipeline (architecture spec at
``docs/architecture/build-alpha-style-features-1-8-research-spec.md`` §
Feature 4). Splits a date window into train/test folds, runs the same
spec over each fold's test window through the canonical engine, and
aggregates fold-level metrics into a combined OOS curve.

**Milestone 4A only**: fixed spec across folds. The train window is
*declared* but not *executed* — there's nothing being fitted on train,
so the train side is informational only and reserved for Phase 4B
(train-side parameter selection, which lives behind Feature 8 /
sensitivity sweeps).

Each fold's test run is a normal ``RunLedger`` + ``BacktestRunResult``
persisted under ``artifacts/runs/<fold_run_id>/`` with
``parent_run_id = walk_forward_id`` so ``list_runs?parent_run_id=…``
finds them. The walk-forward result itself is persisted under
``artifacts/walk-forward/<walk_forward_id>/{config,result}.json``.

See ``docs/references/walk-forward.md`` for the split-policy choices
and the compounded-vs-rebased combined-curve decision.
"""

from __future__ import annotations

from app.research.walk_forward.descriptor import WALK_FORWARD_ARTIFACT
from app.research.walk_forward.errors import (
    WalkForwardAlreadyExistsError,
    WalkForwardCorruptError,
    WalkForwardNotFoundError,
)
from app.research.walk_forward.result import (
    FoldResult,
    SplitPolicySpec,
    WalkForwardConfig,
    WalkForwardResult,
)
from app.research.walk_forward.runner import WalkForwardRequest, run_walk_forward
from app.research.walk_forward.splits import (
    AnchoredSplitPolicy,
    ChronologicalSplitPolicy,
    FoldWindow,
    RollingSplitPolicy,
    SplitPolicy,
    build_split_policy,
)
from app.research.walk_forward.storage import (
    list_walk_forwards,
    load_walk_forward,
    save_walk_forward,
)

__all__ = [
    "WALK_FORWARD_ARTIFACT",
    "AnchoredSplitPolicy",
    "ChronologicalSplitPolicy",
    "FoldResult",
    "FoldWindow",
    "RollingSplitPolicy",
    "SplitPolicy",
    "SplitPolicySpec",
    "WalkForwardAlreadyExistsError",
    "WalkForwardConfig",
    "WalkForwardCorruptError",
    "WalkForwardNotFoundError",
    "WalkForwardRequest",
    "WalkForwardResult",
    "build_split_policy",
    "list_walk_forwards",
    "load_walk_forward",
    "run_walk_forward",
    "save_walk_forward",
]
