"""Synthetic selection demonstration for review #2421; no market data or I/O.

Run from PythonDataService using the review's guarded launcher. The estimator
is deliberately fed complete return vectors: this does not exercise or repeat
the first-session omission reported separately in #2416.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys

from app.engine.results.statistics import _probabilistic_sharpe_ratio
from app.schemas.run_verdict import RunVerdictInput
from app.services.run_verdict_service import _grade_psr_sub


def main() -> None:
    rng = random.Random(2421)
    trial_count = 1000
    observations = 252
    trials = [[rng.gauss(0.0, 0.01) for _ in range(observations)] for _ in range(trial_count)]
    sharpes = [statistics.mean(returns) / statistics.stdev(returns) for returns in trials]
    winner_index = max(range(trial_count), key=sharpes.__getitem__)
    winner = trials[winner_index]
    psr = _probabilistic_sharpe_ratio(winner)
    assert psr is not None and psr > 0.99
    score = _grade_psr_sub(psr)
    assert score.score == 18
    assert score.note == "Near-certain - verify sample size isn't inflated."

    # The production input cannot represent how the return vector was selected.
    assert not any("trial" in field or "selection" in field for field in RunVerdictInput.model_fields)
    # A fixed-seed construction is a demonstration, not a false-positive-rate
    # estimate, a strategy backtest, or proof of future loss for this winner.
    result = {
        "seed": 2421,
        "generating_mean": 0.0,
        "independent_trials": trial_count,
        "observations_each": observations,
        "selection_rule": "maximum observed Sharpe",
        "winner_index_zero_based": winner_index,
        "winner_annualized_sharpe": sharpes[winner_index] * math.sqrt(252),
        "production_psr": psr,
        "production_psr_score": score.score,
        "production_psr_note": score.note,
        "run_verdict_input_fields": sorted(RunVerdictInput.model_fields),
    }
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
