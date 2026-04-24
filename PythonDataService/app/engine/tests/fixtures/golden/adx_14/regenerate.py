"""Regenerate the ADX(14) golden fixture.

Runs the committed AverageDirectionalIndex implementation on a
reproducible 500-bar synthetic random-walk series and writes
``input.csv`` (OHLC) plus ``output.csv`` (adx, plus_di, minus_di).

Run from the repo root inside the python-service container:

    podman exec polygon-data-service python \\
        /app/app/engine/tests/fixtures/golden/adx_14/regenerate.py

Regenerating this fixture invalidates the regression test. Only do so
with a justification documented in docs/references/adx.md.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.adx import AverageDirectionalIndex

SEED = 42
COUNT = 500
PERIOD = 14

OUT_DIR = Path(__file__).parent


def build_bars() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    closes = [150.0]
    for _ in range(COUNT - 1):
        closes.append(closes[-1] + rng.normal(0.0, 0.3))
    closes_arr = np.array(closes)

    highs = closes_arr + rng.uniform(0.1, 1.0, COUNT)
    lows = closes_arr - rng.uniform(0.1, 1.0, COUNT)
    opens = closes_arr + rng.uniform(-0.5, 0.5, COUNT)

    highs = np.maximum(highs, np.maximum(opens, closes_arr))
    lows = np.minimum(lows, np.minimum(opens, closes_arr))

    base = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    ts = [base + pd.Timedelta(minutes=15 * i) for i in range(COUNT)]
    return pd.DataFrame(
        {"timestamp": ts, "open": opens, "high": highs, "low": lows, "close": closes_arr}
    )


def compute(df: pd.DataFrame) -> pd.DataFrame:
    adx = AverageDirectionalIndex("ADX", PERIOD)
    adx_vals: list[float] = []
    plus_vals: list[float] = []
    minus_vals: list[float] = []
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
        adx_vals.append(float(adx.current_value) if adx.is_ready else float("nan"))
        plus_vals.append(float(adx.plus_di) if adx.plus_di is not None else float("nan"))
        minus_vals.append(float(adx.minus_di) if adx.minus_di is not None else float("nan"))
    return pd.DataFrame({"adx": adx_vals, "plus_di": plus_vals, "minus_di": minus_vals})


def main() -> None:
    bars = build_bars()
    out = compute(bars)

    bars.to_csv(OUT_DIR / "input.csv", index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    out.to_csv(OUT_DIR / "output.csv", index=False, float_format="%.17g")
    print(f"Wrote {OUT_DIR / 'input.csv'}")
    print(f"Wrote {OUT_DIR / 'output.csv'}")


if __name__ == "__main__":
    main()
