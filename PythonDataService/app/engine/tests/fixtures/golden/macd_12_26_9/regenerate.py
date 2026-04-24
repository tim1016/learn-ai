"""Regenerate the MACD(12,26,9) golden fixture.

Writes ``input.csv`` (close series) and ``output.csv`` (macd / signal /
histogram per row).

Run from the repo root with the python-service container up:

    podman exec -w /app polygon-data-service python -m \\
        app.engine.tests.fixtures.golden.macd_12_26_9.regenerate
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from app.engine.indicators.macd import MovingAverageConvergenceDivergence

SEED = 42
COUNT = 500
FAST = 12
SLOW = 26
SIGNAL = 9

OUT_DIR = Path(__file__).parent


def build_closes() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    vals = [150.0]
    for _ in range(COUNT - 1):
        vals.append(vals[-1] + rng.normal(0.0, 0.3))
    base = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    ts = [base + pd.Timedelta(minutes=15 * i) for i in range(COUNT)]
    return pd.DataFrame({"timestamp": ts, "close": vals})


def compute(df: pd.DataFrame) -> pd.DataFrame:
    macd = MovingAverageConvergenceDivergence("MACD", FAST, SLOW, SIGNAL)
    m: list[float] = []
    s: list[float] = []
    h: list[float] = []
    for row in df.itertuples(index=False):
        macd.update(row.timestamp.to_pydatetime(), Decimal(str(row.close)))
        m.append(float(macd.macd) if macd.macd is not None else float("nan"))
        s.append(float(macd.signal) if macd.signal is not None else float("nan"))
        h.append(float(macd.histogram) if macd.histogram is not None else float("nan"))
    return pd.DataFrame({"macd": m, "signal": s, "histogram": h})


def main() -> None:
    df_in = build_closes()
    df_out = compute(df_in)
    df_in.to_csv(OUT_DIR / "input.csv", index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    df_out.to_csv(OUT_DIR / "output.csv", index=False, float_format="%.17g")
    print(f"Wrote {OUT_DIR / 'input.csv'}")
    print(f"Wrote {OUT_DIR / 'output.csv'}")


if __name__ == "__main__":
    main()
