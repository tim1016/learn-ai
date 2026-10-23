"""Independent scalar oracles for Lo HAC and Bailey/López de Prado DSR.

No application imports or NumPy/SciPy. Lo uses 60-digit Decimal influence
scores instead of the production joint-moment matrix. DSR includes numbers
transcribed from the authors' pp. 9–10 example and a measured-grid example.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, localcontext
from pathlib import Path
from statistics import NormalDist, mean, stdev

ROOT = (
    Path(__file__).resolve().parents[2]
    / "PythonDataService/tests/fixtures/golden/research-primitives"
)
NORMAL = NormalDist()


def hac(case: dict) -> dict:
    with localcontext() as context:
        context.prec = 60
        values = [Decimal(str(x)) for x in case["returns"]]
        n = len(values)
        mu = sum(values) / n
        v = sum((x - mu) ** 2 for x in values) / n
        sr = mu / v.sqrt()
        z = [(x - mu) / v.sqrt() for x in values]
        influence = [x - sr * (x * x - 1) / 2 for x in z]
        long_variance = sum(x * x for x in influence) / n
        start = 0
        lag = case["hac_lag"]
        for length in case["segment_lengths"]:
            segment = influence[start : start + length]
            for k in range(1, min(lag + 1, length)):
                weight = 1 - Decimal(k) / (lag + 1)
                long_variance += (
                    2
                    * weight
                    * sum(segment[t] * segment[t - k] for t in range(k, length))
                    / n
                )
            start += length
        scale = (Decimal(n - 1) / n * case["bars_per_year"]).sqrt()
        point = float(sr * scale)
        se = float((long_variance / n).sqrt() * scale)
        zcrit = NORMAL.inv_cdf(0.975)
        return {
            "point": point,
            "se": se,
            "ci_lower": point - zcrit * se,
            "ci_upper": point + zcrit * se,
        }


def dsr(
    sr: float, n: int, skew: float, kurt: float, spread: float, trials: float
) -> tuple[float, float]:
    gamma = 0.5772156649015329
    maximum = spread * (
        (1 - gamma) * NORMAL.inv_cdf(1 - 1 / trials)
        + gamma * NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    )
    probability = NORMAL.cdf(
        (sr - maximum) * math.sqrt((n - 1) / (1 - skew * sr + (kurt - 1) * sr * sr / 4))
    )
    return maximum, probability


def grid_oracle(rows: list[list[float]], selected: int) -> dict:
    n, m = len(rows[0]), len(rows)
    sharpes = [mean(row) / stdev(row) for row in rows]
    standardized = [[(x - mean(row)) / stdev(row) for x in row] for row in rows]
    rho = mean(
        sum(x * y for x, y in zip(standardized[i], standardized[j], strict=True))
        / (n - 1)
        for i in range(m)
        for j in range(i + 1, m)
    )
    effective = min(m, max(1, rho + (1 - rho) * m))
    row = rows[selected]
    deviations = [x - mean(row) for x in row]
    variance = mean(x * x for x in deviations)
    skew = (
        math.sqrt(n * (n - 1))
        / (n - 2)
        * mean(x**3 for x in deviations)
        / variance**1.5
    )
    kurt = (n - 1) / ((n - 2) * (n - 3)) * (
        (n + 1) * (mean(x**4 for x in deviations) / variance**2 - 3) + 6
    ) + 3
    spread = stdev(sharpes)
    maximum, probability = dsr(sharpes[selected], n, skew, kurt, spread, effective)
    return dict(
        raw_sharpe=sharpes[selected],
        trial_sharpe_std=spread,
        mean_trial_correlation=rho,
        effective_trials=effective,
        skewness=skew,
        kurtosis=kurt,
        expected_max_under_null=maximum,
        dsr_probability=probability,
    )


def write(fixture: str, inputs: dict, outputs: dict) -> None:
    directory = ROOT / fixture / "v1"
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in (("input", inputs), ("output", outputs)):
        (directory / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    values = [
        -0.02,
        -0.01,
        0.005,
        0.01,
        0.03,
        0.025,
        -0.005,
        -0.015,
        0.002,
        0.008,
        0.012,
        -0.003,
    ]
    cases = [
        dict(returns=values, hac_lag=lag, segment_lengths=segments, bars_per_year=252)
        for lag, segments in [(0, [12]), (3, [12]), (3, [6, 6])]
    ]
    write("RP-007", {"cases": cases}, {"cases": [hac(case) for case in cases]})
    published = [
        dict(
            sharpe=2.5 / math.sqrt(250),
            sample_size=1250,
            skewness=skew,
            kurtosis=kurt,
            trial_sharpe_std=math.sqrt(0.5 / 250),
            effective_trials=trials,
        )
        for trials, skew, kurt in [(100, -3, 10), (46, -3, 10), (88, 0, 3)]
    ]
    rows = [
        [0.01, -0.02, 0.03, 0.005, -0.01, 0.02, -0.005, 0.015],
        [-0.01, 0.02, 0.005, -0.03, 0.01, 0.005, 0.02, -0.015],
        [0.02, 0.01, -0.01, 0.015, -0.02, 0.005, -0.005, 0.03],
        [0.005, -0.01, 0.015, 0.02, 0.025, -0.02, 0.01, -0.005],
    ]
    write(
        "RP-008",
        {"published": published, "grid": rows, "selected_index": 0},
        {
            "published_dsr": [0.9004, 0.9505, 0.9505],
            "published_null_max_100": 0.1132,
            "grid": grid_oracle(rows, 0),
        },
    )


if __name__ == "__main__":
    main()
