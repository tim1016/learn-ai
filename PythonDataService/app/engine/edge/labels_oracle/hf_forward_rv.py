"""Forward (oracle) HF realized vol — ex-post VRP analysis only.

Formula: Forward-window two-component HF realized variance: RV²_forward[d] = HF realized vol of bars [d+1, d+W]; last W trading days are NaN
Reference: Andersen, T.G., Bollerslev, T., Diebold, F.X., Labys, P. (2003) "Modeling and Forecasting Realized Volatility" Econometrica 71(2); Barndorff-Nielsen, O.E. & Shephard, N. (2002) two-component estimator
Canonical implementation: app/engine/edge/labels_oracle/hf_forward_rv.py
Validated against: NONE — pending

This module is in ``labels_oracle/``. It is NEVER imported by
``features_realtime/`` or by any feature/regime pipeline (CI grep guard
enforces this).

For each trading day ``d``, the forward RV is the HF realized vol of bars
``[d+1, d+W]`` — i.e., the variance the option market priced at d.
The last W trading days of the output are NaN.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.edge.features_realtime.hf_realized_vol import (
    Session,
    _et_trading_date,
    daily_two_component_rv_sq,
)


def hf_forward_rv_trd252(
    bars: pd.DataFrame,
    *,
    window_trading_days: int = 21,
    session: Session = "ETH",
) -> pd.Series:
    """Forward HF realized vol on TRD/252 basis, indexed by bar timestamp.

    ``RV[t] = vol of bars [t+1, t+W_trading_days]``. The terminal ``W`` days
    are NaN — the forward window has not yet been realized.
    """
    if window_trading_days <= 0:
        raise ValueError(f"window_trading_days must be positive: {window_trading_days}")

    daily_rv_sq = daily_two_component_rv_sq(bars, session=session)
    if daily_rv_sq.empty:
        return pd.Series(np.nan, index=bars.index, dtype=float)

    # Trailing window first, then shift left so the value at d uses [d+1, d+W].
    trailing = daily_rv_sq.rolling(
        window=window_trading_days, min_periods=window_trading_days
    ).sum()
    forward = trailing.shift(-window_trading_days)
    forward_vol = np.sqrt(forward * 252.0 / window_trading_days)

    bar_dates = pd.Series(_et_trading_date(bars.index), index=bars.index, name="trading_date")
    out = forward_vol.reindex(bar_dates.values).to_numpy()
    return pd.Series(
        out, index=bars.index, dtype=float, name=f"rv_hf_fwd_{window_trading_days}d_{session.lower()}"
    )
