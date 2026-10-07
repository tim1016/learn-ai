"""Golden fixture generator for SB-001: arch's stationary bootstrap of a paired, serially dependent series.

The reference is ``arch.bootstrap.StationaryBootstrap``. This script never
imports the canonical draw (``app/research/monte_carlo/stationary.py``): it
needs only arch, numpy and pyarrow, and computes Sharpe with its own formula.

arch is generation-only. It is in no requirements file and nothing under
``app/`` or ``tests/`` imports it, so this script runs in a throwaway
environment and no test imports this module.

Regeneration (must be justified in the commit message, ADR 0069), from the
repo root:
    python3.12 -m venv "$TMPDIR/sb-001"
    "$TMPDIR/sb-001/bin/pip" install arch==8.0.0 numpy==2.2.6 pyarrow==19.0.1
    "$TMPDIR/sb-001/bin/python" PythonDataService/scripts/fixture_generators/stationary_bootstrap.py
then record the new file and content hashes in
``tests/fixtures/golden/manifest.json`` (``golden_support.hashing.compute_hashes``).
The script refuses to write from any other arch or numpy: a seed fixes the
draws only under the versions that drew them.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import arch
import numpy as np
import pyarrow as pa
from arch.bootstrap import StationaryBootstrap
from pyarrow import ipc

ARCH_VERSION = "8.0.0"
NUMPY_VERSION = "2.2.6"
OUT_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden" / "resampling" / "SB-001" / "v1"
SEED = 20261006
N = 250
MEAN_BLOCK = 5
REPLICATIONS = 200_000
LEVELS = (0.05, 0.25, 0.50, 0.75, 0.95)
STATISTICS = ("mean", "sharpe", "sharpe_difference")
logger = logging.getLogger(__name__)


def _sharpe(returns: np.ndarray) -> float:
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))


def _statistics(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.array([a.mean(), _sharpe(a), _sharpe(a) - _sharpe(b)])


def _paired_series() -> tuple[np.ndarray, np.ndarray]:
    """``a`` is an AR(1) return series; ``b`` follows it with its own noise, so the two are dependent in time and on each other."""
    rng = np.random.default_rng(SEED)
    noise = rng.normal(0.0004, 0.01, N)
    a = np.empty(N)
    a[0] = noise[0]
    for t in range(1, N):
        a[t] = 0.3 * a[t - 1] + noise[t]
    b = 0.6 * a + rng.normal(0.0002, 0.008, N)
    return a, b


def _write(table: pa.Table, name: str) -> None:
    with ipc.new_file(str(OUT_DIR / name), table.schema) as writer:
        writer.write_table(table)


def main() -> None:
    if (arch.__version__, np.__version__) != (ARCH_VERSION, NUMPY_VERSION):
        raise SystemExit(
            f"SB-001 is pinned to arch {ARCH_VERSION} and numpy {NUMPY_VERSION}; this is arch {arch.__version__} and numpy {np.__version__}"
        )
    a, b = _paired_series()
    # Both series are resampled with the same indices, so each draw keeps them paired.
    draws = StationaryBootstrap(MEAN_BLOCK, a, b, seed=SEED).apply(_statistics, REPLICATIONS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _write(pa.table({"a": a, "b": b}), "input.arrow")
    quantiles = {name: np.quantile(draws[:, column], LEVELS) for column, name in enumerate(STATISTICS)}
    _write(pa.table({"level": list(LEVELS), **quantiles}), "output.arrow")
    logger.info("SB-001 written to %s", OUT_DIR)


if __name__ == "__main__":
    main()
