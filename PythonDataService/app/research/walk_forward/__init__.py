"""Walk-forward analysis over ``StrategySpec`` runs.

Phase C of the build-alpha-style research pipeline (architecture spec at
``docs/architecture/build-alpha-style-features-1-8-research-spec.md`` §
Feature 4). Splits a date window into train/test folds, optionally selects a
fully-materialized candidate spec on each train window, freezes it for test,
and aggregates fold-level metrics into a combined OOS curve. Fixed-spec 4A
and candidate-grid 4B share the same canonical runner.

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
    AnchoredSplitPolicySpec,
    ChronologicalSplitPolicySpec,
    FoldResult,
    ParameterSearchConfig,
    RollingSplitPolicySpec,
    SelectionFailureResult,
    SplitPolicySpec,
    TrainingCandidateResult,
    WalkForwardConfig,
    WalkForwardResult,
    parse_split_policy_spec,
)
from app.research.walk_forward.runner import WalkForwardRequest, run_walk_forward
from app.research.walk_forward.selection import (
    ParameterCandidate,
    ParameterSearchRequest,
    ParameterSelectionError,
)
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
    "AnchoredSplitPolicySpec",
    "ChronologicalSplitPolicy",
    "ChronologicalSplitPolicySpec",
    "FoldResult",
    "FoldWindow",
    "ParameterCandidate",
    "ParameterSearchConfig",
    "ParameterSearchRequest",
    "ParameterSelectionError",
    "RollingSplitPolicy",
    "RollingSplitPolicySpec",
    "SelectionFailureResult",
    "SplitPolicy",
    "SplitPolicySpec",
    "TrainingCandidateResult",
    "WalkForwardAlreadyExistsError",
    "WalkForwardConfig",
    "WalkForwardCorruptError",
    "WalkForwardNotFoundError",
    "WalkForwardRequest",
    "WalkForwardResult",
    "build_split_policy",
    "list_walk_forwards",
    "load_walk_forward",
    "parse_split_policy_spec",
    "run_walk_forward",
    "save_walk_forward",
]
