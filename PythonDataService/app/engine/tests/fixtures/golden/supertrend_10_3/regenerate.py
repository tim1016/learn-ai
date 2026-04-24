"""Regenerate the Supertrend(10, 3) golden fixture."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.supertrend import Supertrend

SEED = 42
COUNT = 500
ATR_PERIOD = 10
MULTIPLIER = 3

OUT_DIR = Path(__file__).parent


def build_bars() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    closes = [150.0]
    for _ in range(COUNT - 1):
        closes.append(closes[-1] + rng.normal(0.0, 0.3))
    arr = np.array(closes)
    highs = arr + rng.uniform(0.1, 1.0, COUNT)
    lows = arr - rng.uniform(0.1, 1.0, COUNT)
    opens = arr + rng.uniform(-0.5, 0.5, COUNT)
    highs = np.maximum(highs, np.maximum(opens, arr))
    lows = np.minimum(lows, np.minimum(opens, arr))
    base = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    ts = [base + pd.Timedelta(minutes=15 * i) for i in range(COUNT)]
    return pd.DataFrame(
        {"timestamp": ts, "open": opens, "high": highs, "low": lows, "close": arr}
    )


def compute(df: pd.DataFrame) -> pd.DataFrame:
    st = Supertrend("ST", ATR_PERIOD, MULTIPLIER)
    line: list[float] = []
    direction: list[int] = []
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
        line.append(float(st.current_value) if st.current_value is not None else float("nan"))
        direction.append(st.direction if st.direction is not None else 0)
    return pd.DataFrame({"supertrend": line, "direction": direction})


def main() -> None:
    bars = build_bars()
    out = compute(bars)
    bars.to_csv(OUT_DIR / "input.csv", index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    out.to_csv(OUT_DIR / "output.csv", index=False, float_format="%.17g")
    print(f"Wrote {OUT_DIR / 'input.csv'}")
    print(f"Wrote {OUT_DIR / 'output.csv'}")


if __name__ == "__main__":
    main()
