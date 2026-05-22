"""Cell runner — three-gate orchestration.

Operates on already-staged outputs (LEAN pinned, Engine Lab live). LEAN
container invocation lives in the regeneration script; this module is
pure orchestration so it's exercisable in tests without LEAN running.

Gate order — short-circuit on failure:
  Gate 1: observations.csv exact equality
  Gate 2: state.csv per-bar parity within atol=1e-9
  Gate 3: trade-level cross-reconciler (8-category taxonomy)

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
           § "Tolerances and acceptance gates"
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.lean_sidecar.cross_reconciler import (
    CrossReconciliationOutput,
    CrossReconciliationTolerances,
    compare_cross_engine,
)
from app.lean_sidecar.cross_runner import CrossRunOrderEvent
from app.lean_sidecar.normalized_parser import NormalizedOrderEvent
from app.lean_sidecar.parity_matrix.observations_parity import (
    ObservationsParityResult,
    compare_observations,
)
from app.lean_sidecar.parity_matrix.state_parity import (
    StateParityResult,
    compare_state,
)


@dataclass(frozen=True)
class CellRunReport:
    """Outcome of running all three gates for one cell.

    ``observations`` is always populated (Gate 1 always runs). ``state``
    is None when Gate 1 failed and Gate 2 was skipped. ``trade`` is None
    when either Gate 1 or Gate 2 failed and Gate 3 was skipped.
    """

    overall_passed: bool
    observations: ObservationsParityResult
    state: StateParityResult | None
    trade: CrossReconciliationOutput | None


def run_cell_gates(
    *,
    pinned_lean_dir: Path,
    engine_output_dir: Path,
    engine_normalized_orders: list[CrossRunOrderEvent],
    trade_tolerances: CrossReconciliationTolerances | None = None,
    assert_fees: bool = True,
) -> CellRunReport:
    """Run the three gates in order against pinned LEAN vs live Engine output.

    ``pinned_lean_dir`` contains ``observations.csv``, ``state.csv``,
    and ``orders.json`` (the latter is a JSON array of
    ``NormalizedOrderEvent`` dicts produced by the regeneration script).
    ``engine_output_dir`` contains the same first two filenames written
    by ``SpyEmaCrossoverAlgorithm`` with ``output_dir=engine_output_dir``.
    ``engine_normalized_orders`` is the list of fills returned by the
    Engine Lab run (from ``cross_runner.run_engine_lab_on_workspace``).
    """
    obs = compare_observations(
        reference=pinned_lean_dir / "observations.csv",
        candidate=engine_output_dir / "observations.csv",
    )
    if not obs.passed:
        return CellRunReport(
            overall_passed=False,
            observations=obs,
            state=None,
            trade=None,
        )

    state = compare_state(
        reference=pinned_lean_dir / "state.csv",
        candidate=engine_output_dir / "state.csv",
    )
    if not state.passed:
        return CellRunReport(
            overall_passed=False,
            observations=obs,
            state=state,
            trade=None,
        )

    pinned_lean_events = _load_pinned_lean_orders(pinned_lean_dir / "orders.json")
    tol = trade_tolerances or CrossReconciliationTolerances.default()
    trade = compare_cross_engine(
        lean_events=pinned_lean_events,
        engine_events=engine_normalized_orders,
        tolerances=tol,
        assert_fees=assert_fees,
    )
    return CellRunReport(
        overall_passed=trade.passed,
        observations=obs,
        state=state,
        trade=trade,
    )


def _load_pinned_lean_orders(path: Path) -> list[NormalizedOrderEvent]:
    """Load orders.json as a list of NormalizedOrderEvent.

    Format: JSON array of NormalizedOrderEvent dicts. This is the format
    the regeneration script writes (Task 7/10).
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"orders.json must be a JSON array; got {type(payload).__name__}")
    return [NormalizedOrderEvent.model_validate(d) for d in payload]
