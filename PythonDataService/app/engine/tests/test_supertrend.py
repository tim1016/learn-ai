"""Tests for Supertrend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.supertrend import Supertrend

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden" / "supertrend_10_3"


def _bar(ts: datetime, high: str, low: str, close: str, open_: str | None = None) -> TradeBar:
    o = Decimal(open_) if open_ is not None else Decimal(close)
    return TradeBar(
        symbol="SPY",
        time=ts,
        end_time=ts + timedelta(minutes=15),
        open=o,
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=0,
    )


def _synthetic_bars(count: int, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = [150.0]
    for _ in range(count - 1):
        closes.append(closes[-1] + rng.normal(0.0, 0.3))
    arr = np.array(closes)
    highs = arr + rng.uniform(0.1, 1.0, count)
    lows = arr - rng.uniform(0.1, 1.0, count)
    opens = arr + rng.uniform(-0.5, 0.5, count)
    highs = np.maximum(highs, np.maximum(opens, arr))
    lows = np.minimum(lows, np.minimum(opens, arr))
    base = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    ts = [base + pd.Timedelta(minutes=15 * i) for i in range(count)]
    return pd.DataFrame({"timestamp": ts, "open": opens, "high": highs, "low": lows, "close": arr})


def _run_st(df: pd.DataFrame, atr_period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    st = Supertrend("ST", atr_period, multiplier)
    line: list[float | None] = []
    direction: list[int | None] = []
    for row in df.itertuples(index=False):
        bar = TradeBar(
            symbol="SPY",
            time=row.timestamp.to_pydatetime(),
            end_time=(row.timestamp + pd.Timedelta(minutes=15)).to_pydatetime(),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=0,
        )
        st.update(bar)
        line.append(float(st.current_value) if st.current_value is not None else None)
        direction.append(st.direction)
    return pd.DataFrame({"supertrend": line, "direction": direction})


# ---------------------------------------------------------------------------
# Layer 1: hand-computed micro-tests
# ---------------------------------------------------------------------------


def test_warmup_emits_first_value_at_atr_period():
    """First supertrend value at samples == atr_period."""
    st = Supertrend("ST", atr_period=5, multiplier=3)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for i in range(4):
        st.update(_bar(t + timedelta(minutes=15 * i), "101", "99", "100"))
        assert not st.is_ready
        assert st.current_value is None
    st.update(_bar(t + timedelta(minutes=15 * 4), "101", "99", "100"))
    assert st.is_ready
    assert st.current_value is not None
    # First bar: direction defaults to uptrend (+1).
    assert st.is_long is True


def test_direction_flips_when_close_breaks_upper_band():
    """Start flat → supertrend = lower band (uptrend). Drop close below
    the prior lower band → direction flips to downtrend."""
    st = Supertrend("ST", atr_period=3, multiplier=1)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Build up 3 flat-ish bars so ATR stabilises.
    for i in range(3):
        st.update(_bar(t + timedelta(minutes=15 * i), "101", "99", "100"))
    assert st.is_long is True
    first_lower = st.lower_band
    # Next bar: close drops well below the prior lower band.
    st.update(_bar(t + timedelta(minutes=15 * 3), "101", "90", "91"))
    # Close (91) < prev_lower, direction flips to -1.
    assert st.is_long is False
    assert st.direction == -1
    assert first_lower is not None


def test_uptrend_lower_band_never_retraces_down():
    """In an uptrend with the direction preserved, the lower band is
    clamped to its previous value whenever the new basic would be lower."""
    st = Supertrend("ST", atr_period=3, multiplier=1)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Warmup.
    for i in range(3):
        st.update(_bar(t + timedelta(minutes=15 * i), "101", "99", "100"))
    lower_bars: list[Decimal] = [st.lower_band]  # type: ignore[list-item]
    # Feed bars whose close stays between prev_lower and prev_upper.
    # Each should preserve direction and clamp the lower band
    # monotonically.
    for j, close in enumerate([100, 100, 100], start=4):
        st.update(_bar(t + timedelta(minutes=15 * j), "101", "99", str(close)))
        lower_bars.append(st.lower_band)  # type: ignore[arg-type]
    # Monotone non-decreasing in uptrend with flat bars.
    for a, b in pairwise(lower_bars):
        assert b >= a


# ---------------------------------------------------------------------------
# Layer 2: cross-reference vs pandas-ta
# ---------------------------------------------------------------------------


def test_matches_pandas_ta_supertrend_strict():
    """pandas-ta uses the same formula; should agree bit-exactly post-warmup."""
    df = _synthetic_bars(500)
    ours = _run_st(df, atr_period=10, multiplier=3)

    ref = ta.supertrend(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        length=10,
        multiplier=3.0,
    )
    ref_line = ref["SUPERT_10_3.0"].to_numpy()
    ref_dir = ref["SUPERTd_10_3.0"].to_numpy()

    ours_line = np.array([v if v is not None else np.nan for v in ours["supertrend"].tolist()])
    ours_dir = np.array(
        [v if v is not None else 0 for v in ours["direction"].tolist()],
        dtype=float,
    )

    np.testing.assert_allclose(ours_line, ref_line, atol=1e-9, rtol=0, equal_nan=True)
    # Direction comparison: pandas-ta defers the first direction by one
    # bar (first line at index `length - 1`, first direction at index
    # `length`). Compare only where both sides have a defined direction.
    both_defined = ~np.isnan(ref_dir)
    np.testing.assert_array_equal(ours_dir[both_defined].astype(int), ref_dir[both_defined].astype(int))


# ---------------------------------------------------------------------------
# Layer 3: golden fixture regression
# ---------------------------------------------------------------------------


def test_golden_fixture_regression():
    input_csv = FIXTURE_DIR / "input.csv"
    output_csv = FIXTURE_DIR / "output.csv"
    if not input_csv.exists() or not output_csv.exists():
        pytest.skip("Supertrend golden fixture not yet generated")

    df_in = pd.read_csv(input_csv, parse_dates=["timestamp"])
    if df_in["timestamp"].dt.tz is None:
        df_in["timestamp"] = df_in["timestamp"].dt.tz_localize("UTC")
    ours = _run_st(df_in, atr_period=10, multiplier=3)
    df_out = pd.read_csv(output_csv)

    np.testing.assert_allclose(
        ours["supertrend"].astype(float).to_numpy(),
        df_out["supertrend"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
    np.testing.assert_array_equal(
        np.where(pd.isna(ours["direction"]), 0, ours["direction"]).astype(int),
        np.where(pd.isna(df_out["direction"]), 0, df_out["direction"]).astype(int),
    )
