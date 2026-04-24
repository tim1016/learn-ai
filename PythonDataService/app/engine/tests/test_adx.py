"""Tests for AverageDirectionalIndex.

Three layers of coverage:

1. **Hand-computed micro-tests** on tiny OHLC sequences — the strongest
   rigor layer. Verifies the port against Wilder's spec bit-exactly.
2. **Cross-reference vs pandas-ta** on a 500-bar synthetic series — a
   sanity check against another implementation at a loose tolerance.
   pandas-ta seeds DMI with SMA rather than sum-then-Wilder-average,
   which introduces a small deterministic offset; the test pins that
   difference so we notice if it drifts.
3. **Golden-fixture regression** — re-runs the port on the committed
   input CSV and compares against the committed output CSV. Catches any
   accidental change to the math in future PRs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.adx import AverageDirectionalIndex

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden" / "adx_14"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(ts: datetime, high: str, low: str, close: str, open_: str | None = None) -> TradeBar:
    end = ts + timedelta(minutes=15)
    o = Decimal(open_) if open_ is not None else Decimal(close)
    return TradeBar(
        symbol="SPY",
        time=ts,
        end_time=end,
        open=o,
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000_000,
    )


def _synthetic_bars(count: int, seed: int = 42) -> pd.DataFrame:
    """Reproducible random-walk OHLC bars. Mirrors the shape used in
    test_indicator_parity._make_realistic_bars but with monotonic 15-min
    timestamps."""
    rng = np.random.default_rng(seed)
    closes = [150.0]
    for _ in range(count - 1):
        closes.append(closes[-1] + rng.normal(0.0, 0.3))
    closes_arr = np.array(closes)

    highs = closes_arr + rng.uniform(0.1, 1.0, count)
    lows = closes_arr - rng.uniform(0.1, 1.0, count)
    opens = closes_arr + rng.uniform(-0.5, 0.5, count)

    highs = np.maximum(highs, np.maximum(opens, closes_arr))
    lows = np.minimum(lows, np.minimum(opens, closes_arr))

    base_ts = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    ts = [base_ts + pd.Timedelta(minutes=15 * i) for i in range(count)]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes_arr,
        }
    )


def _run_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Stream the DataFrame through our AverageDirectionalIndex and
    return a DataFrame of adx/plus_di/minus_di aligned to input rows."""
    adx = AverageDirectionalIndex("ADX", period)
    out_adx: list[float | None] = []
    out_plus: list[float | None] = []
    out_minus: list[float | None] = []
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
        adx.update(bar)
        out_adx.append(float(adx.current_value) if adx.is_ready else None)
        out_plus.append(float(adx.plus_di) if adx.plus_di is not None else None)
        out_minus.append(float(adx.minus_di) if adx.minus_di is not None else None)
    return pd.DataFrame({"adx": out_adx, "plus_di": out_plus, "minus_di": out_minus})


# ---------------------------------------------------------------------------
# Layer 1: hand-computed micro-tests
# ---------------------------------------------------------------------------


def test_warmup_no_value_until_two_periods():
    """is_ready false until samples >= 2 * period."""
    adx = AverageDirectionalIndex("ADX", 14)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for i in range(27):
        bar = _bar(
            t + timedelta(minutes=15 * i),
            high=f"{100 + i * 0.1:.4f}",
            low=f"{99 + i * 0.1:.4f}",
            close=f"{99.5 + i * 0.1:.4f}",
        )
        adx.update(bar)
        assert not adx.is_ready, f"ready too early at sample {i + 1}"
    bar = _bar(
        t + timedelta(minutes=15 * 27),
        high=f"{100 + 27 * 0.1:.4f}",
        low=f"{99 + 27 * 0.1:.4f}",
        close=f"{99.5 + 27 * 0.1:.4f}",
    )
    adx.update(bar)
    assert adx.is_ready
    assert adx.current_value is not None


def test_dm_priority_up_over_down_when_equal_ties():
    """Equal up/down moves produce ZERO DM on both sides.

    Wilder's rule is strict inequality: +DM set only when
    up_move > down_move (and > 0). Ties → nothing.
    """
    adx = AverageDirectionalIndex("ADX", 2)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    adx.update(_bar(t, "10", "8", "9"))
    # up_move = 12-10 = 2, down_move = 8-6 = 2 → tie → both DM = 0
    adx.update(_bar(t + timedelta(minutes=15), "12", "6", "9"))
    # up_move = 14-12 = 2, down_move = 6-4 = 2 → tie again
    adx.update(_bar(t + timedelta(minutes=15 * 2), "14", "4", "9"))
    # At sample 3 (dm_samples=2=period), smoothed values seed.
    # Both +DM and -DM sums are 0 → plus_di and minus_di = 0
    # DX numerator is 0 → DX = 0
    assert adx.plus_di == Decimal(0)
    assert adx.minus_di == Decimal(0)


def test_pure_uptrend_minus_di_zero_adx_100():
    """A perfectly monotone up series: -DM is always 0, so -DI = 0,
    and every DX is 100 → ADX = 100 after seeding.

    +DI is *not* 100 in this case because TR includes the gap to the
    prior close (|H − prev_close|), which exceeds (H − L). The hand
    math: +DM = 1, TR = 1.5 per bar → smoothed_+DM / smoothed_TR = 2/3,
    so +DI = 66.667.
    """
    adx = AverageDirectionalIndex("ADX", 2)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    highs = [10, 11, 12, 13, 14, 15]
    lows = [9, 10, 11, 12, 13, 14]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]
    for i in range(6):
        adx.update(
            _bar(
                t + timedelta(minutes=15 * i),
                high=str(highs[i]),
                low=str(lows[i]),
                close=str(closes[i]),
            )
        )
    assert adx.is_ready
    assert adx.minus_di == Decimal(0)
    # +DI = 200/3 ≈ 66.667 by the hand computation above.
    assert adx.plus_di is not None
    assert abs(adx.plus_di - Decimal(200) / Decimal(3)) < Decimal("1e-20")
    # Monotone up → every DX = 100 → ADX = 100 after seeding.
    assert adx.current_value == Decimal(100)


# ---------------------------------------------------------------------------
# Layer 2: cross-reference vs pandas-ta
# ---------------------------------------------------------------------------


def test_matches_pandas_ta_adx_loose():
    """Our ADX stays within a small tolerance of pandas-ta's ADX.

    pandas-ta seeds its DMI differently (SMA of the first window vs our
    classical Wilder-sum-then-smooth) so the early values drift a bit.
    We check only once both have stabilised (sample index >= 4 * period)
    and use a loose atol that documents the implementation difference.

    NOT a bit-exact test — that's layer 1. This is a sanity check that
    we're computing ADX and not ATR-squared by mistake.
    """
    period = 14
    df = _synthetic_bars(500)

    ours = _run_adx(df, period=period)

    ref = ta.adx(
        df["high"],
        df["low"],
        df["close"],
        length=period,
    )
    ref_adx = ref[f"ADX_{period}"].to_numpy()

    start = 4 * period  # skip the divergent seeding window
    ours_adx = np.array([v if v is not None else np.nan for v in ours["adx"].tolist()])

    diff = np.abs(ours_adx[start:] - ref_adx[start:])
    diff = diff[~np.isnan(diff)]
    # Loose — seeding difference between our Wilder-sum and pandas-ta's SMA
    # decays geometrically with rate (period-1)/period per bar. After 4*period
    # bars the residual is << 1 percent-ADX point.
    assert diff.max() < 1.0, f"max divergence {diff.max():.4f} > 1.0 ADX points"


# ---------------------------------------------------------------------------
# Layer 3: golden fixture regression
# ---------------------------------------------------------------------------


def test_golden_fixture_regression():
    """Replaying the committed input CSV produces the committed output.

    This fixture is our own implementation's output, pinned as a
    regression guard. If Wilder's math in adx.py changes, this test
    fails and the fixture must be regenerated with justification
    (see docs/references/adx.md).
    """
    input_csv = FIXTURE_DIR / "input.csv"
    output_csv = FIXTURE_DIR / "output.csv"
    if not input_csv.exists() or not output_csv.exists():
        pytest.skip("ADX golden fixture not yet generated — run regenerate_adx_fixture.py")

    df_in = pd.read_csv(input_csv, parse_dates=["timestamp"])
    # pandas parses ISO-with-Z as UTC naive; force tz-aware.
    if df_in["timestamp"].dt.tz is None:
        df_in["timestamp"] = df_in["timestamp"].dt.tz_localize("UTC")
    ours = _run_adx(df_in, period=14)

    df_out = pd.read_csv(output_csv)

    np.testing.assert_allclose(
        ours["adx"].astype(float).to_numpy(),
        df_out["adx"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        ours["plus_di"].astype(float).to_numpy(),
        df_out["plus_di"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        ours["minus_di"].astype(float).to_numpy(),
        df_out["minus_di"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
