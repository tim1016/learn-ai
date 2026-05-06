"""Forward (oracle) realized vol — for ex-post VRP and analysis only.

Formula: Same estimator math as features_realtime/realized_vol.py applied over the forward window via .shift(-W); RV[t] = realized vol over [t, t+W]
Reference: Parkinson, M. (1980) "The Extreme Value Method for Estimating the Variance of the Rate of Return" J. Business 53(1); Garman, M.B. & Klass, M.J. (1980) J. Business 53(1)
Canonical implementation: app/engine/edge/labels_oracle/forward_rv.py
Validated against: NONE — pending (oracle labels, not for model use)

This module is in labels_oracle/. It is NEVER imported by features_realtime/
or by any model/feature pipeline. CI grep guard enforces this.

Identical estimator math as features_realtime/realized_vol.py, but with the
forward .shift(-W) applied so RV[t] = realized vol over [t, t+W].

The last W bars of any output series are NaN (forward window not realized).
The UI surfaces this as a greyed terminal band — see edge-feature-design.md §4.2.
"""

from __future__ import annotations

import pandas as pd

from app.engine.edge.features_realtime.realized_vol import (
    DAILY_BARS_PER_YEAR,
    close_to_close,
    garman_klass,
    parkinson,
    yang_zhang,
)

ESTIMATOR_FN = {
    "ctc": close_to_close,
    "parkinson": parkinson,
    "gk": garman_klass,
    "yz": yang_zhang,
}


def forward_rv(
    bars: pd.DataFrame,
    *,
    estimator: str,
    window: int,
    annualize: bool = True,
    bars_per_year: int = DAILY_BARS_PER_YEAR,
) -> pd.Series:
    """RV[t] computed from bars (t, t+window]. The forward shift is applied here.

    Internally we compute the *trailing* RV with the same estimator, then shift
    by -window so that each value is aligned to the start of the forward window.
    """
    if estimator not in ESTIMATOR_FN:
        raise ValueError(f"unknown estimator: {estimator!r}; choices: {sorted(ESTIMATOR_FN)}")
    fn = ESTIMATOR_FN[estimator]
    trailing = fn(bars, window=window, annualize=annualize, bars_per_year=bars_per_year)
    return trailing.shift(-window)
