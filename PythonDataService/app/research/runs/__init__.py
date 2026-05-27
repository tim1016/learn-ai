"""Run ledger and reproducible-run infrastructure.

Wraps ``BacktestEngine.run`` for declarative ``StrategySpec`` execution
with hash-addressable identity, content-deterministic results, and a
file-backed artifact layout under ``PythonDataService/artifacts/runs/``.

Phase A scope: ledger + result Pydantic models, canonical-JSON hashing,
in-memory + file-backed storage, FastAPI endpoint. No GraphQL passthrough
in v1; that ships with Phase B's research workbench when the UI needs it.

See ``docs/architecture/build-alpha-style-features-1-8-research-spec.md``
for the surrounding research-pipeline plan and ``docs/references/run-ledger.md``
for the hashing-scheme rationale.
"""

from __future__ import annotations

from app.research.runs.descriptor import RUNS_ARTIFACT
from app.research.runs.errors import (
    RunAlreadyExistsError,
    RunCorruptError,
    RunNotFoundError,
)
from app.research.runs.hashing import (
    canonical_json,
    hash_payload,
    make_data_snapshot_id,
)
from app.research.runs.ledger import ENGINE_VERSION, RunLedger
from app.research.runs.result import (
    BacktestRunResult,
    DrawdownPoint,
    EquityCurvePoint,
    RunMetrics,
    RunTrade,
)
from app.research.runs.runner import RunRequest, run_strategy_spec
from app.research.runs.storage import (
    list_runs,
    load_run,
    save_run,
)
from app.research.runs.window import (
    ExcludedDay,
    WindowSummary,
    summarize_window,
)

__all__ = [
    "ENGINE_VERSION",
    "RUNS_ARTIFACT",
    "BacktestRunResult",
    "DrawdownPoint",
    "EquityCurvePoint",
    "ExcludedDay",
    "RunAlreadyExistsError",
    "RunCorruptError",
    "RunLedger",
    "RunMetrics",
    "RunNotFoundError",
    "RunRequest",
    "RunTrade",
    "WindowSummary",
    "canonical_json",
    "hash_payload",
    "list_runs",
    "load_run",
    "make_data_snapshot_id",
    "run_strategy_spec",
    "save_run",
    "summarize_window",
]
