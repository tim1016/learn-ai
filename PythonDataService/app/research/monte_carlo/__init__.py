"""Monte Carlo risk analysis over a parent run's trade list.

Phase D of the build-alpha-style research pipeline (architecture spec at
``docs/architecture/build-alpha-style-features-1-8-research-spec.md`` §
Feature 5). Takes the trade list from an existing ``RunLedger`` and
simulates N alternate paths via two methods:

  * **Reshuffle** — same trades, different order. Tests path
    dependence: if the strategy's edge is real, the order shouldn't
    matter (much).
  * **Resample** — sample trades with replacement. Tests sensitivity
    to the specific draw of trades you happened to get; produces
    forward-looking projections of "what could the next N trades
    look like if they're drawn from the same distribution?".

Outputs equity bands (5/50/95 percentile per trade index), drawdown
quantiles, terminal-PnL quantiles, max-losing-streak quantiles, and
breach probabilities for client-supplied drawdown thresholds. All
random draws use ``numpy.random.default_rng(seed)`` so the same
``random_seed`` produces identical simulations across machines.

Persisted under ``<root>/monte-carlo/<mc_id>/{config,result}.json`` —
sibling layout to ``walk-forward/``. Each MC links back to its parent
run via ``parent_run_id``; the parent's ledger is the authoritative
source of the trade list (this module never re-runs the engine).

Feature 6 (OHLC noise / shifted-bar / synthetic-data tests) is a
separate concern that needs synthetic-data generators preserving OHLC
invariants — deferred to a future ``app/research/robustness/`` module.
"""

from __future__ import annotations

from app.research.monte_carlo.methods import (
    resample_trades,
    reshuffle_trades,
)
from app.research.monte_carlo.result import (
    EquityBandPoint,
    MonteCarloConfig,
    MonteCarloResult,
)
from app.research.monte_carlo.runner import (
    MonteCarloRequest,
    run_monte_carlo,
)
from app.research.monte_carlo.storage import (
    MonteCarloAlreadyExistsError,
    MonteCarloCorruptError,
    MonteCarloNotFoundError,
    list_monte_carlos,
    load_monte_carlo,
    save_monte_carlo,
)

__all__ = [
    "EquityBandPoint",
    "MonteCarloAlreadyExistsError",
    "MonteCarloConfig",
    "MonteCarloCorruptError",
    "MonteCarloNotFoundError",
    "MonteCarloRequest",
    "MonteCarloResult",
    "list_monte_carlos",
    "load_monte_carlo",
    "resample_trades",
    "reshuffle_trades",
    "run_monte_carlo",
    "save_monte_carlo",
]
