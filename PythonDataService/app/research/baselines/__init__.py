"""Null-baseline research over a parent run.

Phase E1 of the build-alpha-style research pipeline (architecture
spec at ``docs/architecture/build-alpha-style-features-1-8-research-spec.md``
§ Feature 7). Generates N alternative strategies (buy-and-hold or
random EMA window pairs), runs each through the canonical engine on
the parent's symbol / window / cost model, and aggregates target
metrics into a *null distribution* — answers "did this strategy beat
random?" empirically.

**v1 ships two baseline methods:**

  * ``buy_and_hold`` — single trade: enter on the first bar, hold
    until end-of-algorithm flush. Implemented via a tautological
    ``BarProperty: range >= 0`` entry + a never-firing exit, so no
    spec-layer change is needed.
  * ``random_ema_windows`` — sample ``(fast, slow)`` EMA period pairs
    from a bounded family (default ``fast ∈ [3, 12]`` / ``slow ∈
    [10, 30]`` with ``slow > fast``), build a SPY-EMA-style spec for
    each, run each as its own baseline. Tests "is the parent's
    EMA(5,10) choice better than a random pair from the same
    family?".

**v1 deferred:**

  * ``random_entries`` / ``random_signal_timestamps`` — would need a
    new ``BarIndex`` spec primitive (or an engine bypass) to fire on
    a pre-computed list of bar indices. Not in scope until a real
    consumer drives the spec change.
  * ``random_strategy_specs`` — random-spec generation across the
    whole primitive set is part of the Build Alpha automated-discovery
    feature, not the null-baseline feature.
  * ``cross_symbol`` — needs multi-symbol data wiring.

Each baseline run is a normal Phase A ``RunLedger`` persisted under
``artifacts/runs/<baseline_run_id>/`` with ``parent_run_id`` set to
the baselines run id, so ``list_runs(parent_run_id=baseline_id)``
enumerates them. The aggregated baseline result lives at a sibling
layout ``artifacts/baselines/<baseline_id>/{config,result}.json``.
"""

from __future__ import annotations

from app.research.baselines.generators import (
    BaselineMethod,
    buy_and_hold_spec,
    random_ema_window_specs,
)
from app.research.baselines.result import (
    BaselineConfig,
    BaselineResult,
    BaselineRunRecord,
    NullDistribution,
)
from app.research.baselines.runner import (
    BaselineRequest,
    run_baselines,
)
from app.research.baselines.storage import (
    BaselineAlreadyExistsError,
    BaselineCorruptError,
    BaselineNotFoundError,
    list_baselines,
    load_baseline,
    save_baseline,
)

__all__ = [
    "BaselineAlreadyExistsError",
    "BaselineConfig",
    "BaselineCorruptError",
    "BaselineMethod",
    "BaselineNotFoundError",
    "BaselineRequest",
    "BaselineResult",
    "BaselineRunRecord",
    "NullDistribution",
    "buy_and_hold_spec",
    "list_baselines",
    "load_baseline",
    "random_ema_window_specs",
    "run_baselines",
    "save_baseline",
]
