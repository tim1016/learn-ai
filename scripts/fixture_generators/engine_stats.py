"""Generators for ENG-001 (Sharpe) and ENG-001b (Sortino) golden fixtures.

Oracle: hand_computed — numpy path independent from canonical pure-Python loops.
  Sharpe: numpy mean + std(ddof=1), annualized by sqrt(periods_per_year)
  Sortino: numpy mean + downside variance with all-N denominator

Our canonical:
  app/engine/results/statistics.py::_sharpe(returns, periods_per_year)
  app/engine/results/statistics.py::_sortino(returns, periods_per_year)

Sharpe formula:
  mean(r) / std(r, ddof=1) * sqrt(periods_per_year)

Sortino formula (canonical convention):
  mean(r) / sqrt(sum(d^2 for d in downside) / N) * sqrt(periods_per_year)
  N = len(returns) — all N, NOT len(downside). This matches statistics.py._sortino.

Input grid (3 cases, 5 returns each, periods_per_year=252):
  Case 1: [0.01, 0.02, -0.01, 0.03, 0.01]
    Sharpe: mean=0.012, var(ddof=1)=0.00022
  Case 2: [0.005, 0.015, -0.005, 0.010, 0.000]
    Sharpe: mean=0.005, var(ddof=1)=0.0000625
  Case 3: [-0.02, 0.04, -0.01, 0.02, -0.005]
    Sharpe: mean=0.005, var(ddof=1)=0.0006
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes
from golden_support.io import write_arrow

def _generation_date() -> str:
    return date.today().isoformat()
ORACLE_DESCRIPTION = "hand_computed — numpy mean/std(ddof=1) independent of canonical pure-Python loops"

# Fixed 5-element return cases, hand-verifiable
CASES: list[list[float]] = [
    [0.01, 0.02, -0.01, 0.03, 0.01],
    [0.005, 0.015, -0.005, 0.010, 0.000],
    [-0.02, 0.04, -0.01, 0.02, -0.005],
]
PERIODS_PER_YEAR = 252


def _oracle_sharpe(returns: list[float], periods_per_year: int) -> float:
    r = np.array(returns, dtype=np.float64)
    mean = r.mean()
    std = r.std(ddof=1)
    return float((mean / std) * np.sqrt(periods_per_year))


def _oracle_sortino(returns: list[float], periods_per_year: int) -> float:
    r = np.array(returns, dtype=np.float64)
    mean = r.mean()
    downside = r[r < 0]
    # All-N denominator — matches canonical _sortino in statistics.py
    downside_var = float(np.sum(downside**2) / len(r))
    downside_std = float(np.sqrt(downside_var))
    return float((mean / downside_std) * np.sqrt(periods_per_year))


def _build_input_table() -> pa.Table:
    n = len(CASES)
    return pa.table(
        {
            "r0": pa.array([c[0] for c in CASES], type=pa.float64()),
            "r1": pa.array([c[1] for c in CASES], type=pa.float64()),
            "r2": pa.array([c[2] for c in CASES], type=pa.float64()),
            "r3": pa.array([c[3] for c in CASES], type=pa.float64()),
            "r4": pa.array([c[4] for c in CASES], type=pa.float64()),
            "periods_per_year": pa.array([PERIODS_PER_YEAR] * n, type=pa.int64()),
        }
    )


def _write_attribution_eng001(path: Path, justification: str) -> None:
    path.write_text(
        f"""# ENG-001 — Sharpe Ratio (Daily Returns, Annualized)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-element
daily return series spanning typical win/loss patterns. Hand-designed for
exact verifiability. See `engine_stats.py::CASES`.

**Layer 2 — Methodology provenance:** Sharpe (1994), "The Sharpe Ratio",
Journal of Portfolio Management 21(1) §IV.
Formula: mean(r) / std(r, ddof=1) · √periods_per_year
Canonical: `PythonDataService/app/engine/results/statistics.py::_sharpe`.

**Layer 3 — Independent numerical oracle:** {ORACLE_DESCRIPTION}.
`numpy.mean` + `numpy.std(ddof=1)`, annualized by `numpy.sqrt({PERIODS_PER_YEAR})`.

## Formula

Sharpe = mean(r) / std(r, ddof=1) · √periods_per_year

where ddof=1 (sample standard deviation, N−1 denominator)

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_sharpe`
Signature: `_sharpe(returns, periods_per_year) -> float | None`
Uses pure-Python `sum(...)` loops — different path, same formula.

## Hand-Verification (exact arithmetic)

Case 1: r=[0.01, 0.02, -0.01, 0.03, 0.01]
  mean=0.012, deviations=[-0.002, 0.008, -0.022, 0.018, -0.002]
  Σ(dev²)=0.00088, var(ddof=1)=0.00022
  Sharpe = (0.012/√0.00022) · √252

Case 2: r=[0.005, 0.015, -0.005, 0.010, 0.000]
  mean=0.005, deviations=[0, 0.01, -0.01, 0.005, -0.005]
  Σ(dev²)=0.00025, var(ddof=1)=0.0000625
  Sharpe = (0.005/√0.0000625) · √252

Case 3: r=[-0.02, 0.04, -0.01, 0.02, -0.005]
  mean=0.005, deviations=[-0.025, 0.035, -0.015, 0.015, -0.010]
  Σ(dev²)=0.0024, var(ddof=1)=0.0006
  Sharpe = (0.005/√0.0006) · √252

## Tolerance

atol=1e-9, rtol=0.0

Rationale: numpy vs pure-Python float64 on the same formula.
Observed max abs error: < 1e-15.

## Units

dimensionless (annualized Sharpe ratio)

## Known Limitations

- 5-element series. Short-series Sharpe is statistically unreliable in practice.
- Tests the formula kernel, not the full equity-curve pipeline.
- Zero-std and single-element edge cases are covered in unit tests, not here.

## Regeneration

  python scripts/generate_fixtures.py --id ENG-001 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: {ORACLE_DESCRIPTION}
Script: scripts/fixture_generators/engine_stats.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def _write_attribution_eng001b(path: Path, justification: str) -> None:
    path.write_text(
        f"""# ENG-001b — Sortino Ratio (Daily Returns, Annualized)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-element
daily return series. Same cases as ENG-001. See `engine_stats.py::CASES`.

**Layer 2 — Methodology provenance:** Bacon, *Practical Portfolio Performance
Measurement and Attribution* (2e), §8.3 (Sortino Ratio).
Canonical: `PythonDataService/app/engine/results/statistics.py::_sortino`.

**Layer 3 — Independent numerical oracle:** {ORACLE_DESCRIPTION}.
numpy mean + downside variance with all-N denominator.

## Formula

Sortino = mean(r) / √(Σd² / N) · √periods_per_year

where:
  d = [r for r in returns if r < 0]  (downside returns, strict negative)
  N = len(returns)                    (ALL returns, NOT len(d))
  periods_per_year = {PERIODS_PER_YEAR}

## Denominator Convention (IMPORTANT)

The canonical `_sortino` uses `len(returns)` (all N) in the downside-variance
denominator, not `len(downside)`. This is pinned here. Any change to this
convention requires regenerating this fixture with explicit justification.

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_sortino`
Signature: `_sortino(returns, periods_per_year) -> float | None`
Key line: `downside_var = sum(r * r for r in downside) / len(returns)`

## Hand-Verification (exact arithmetic)

Case 1: r=[0.01, 0.02, -0.01, 0.03, 0.01]
  mean=0.012, downside=[-0.01]
  downside_var=(-0.01)²/5=0.0001/5=0.00002
  Sortino = (0.012/√0.00002) · √252

Case 2: r=[0.005, 0.015, -0.005, 0.010, 0.000]
  mean=0.005, downside=[-0.005]
  downside_var=(-0.005)²/5=0.000025/5=0.000005
  Sortino = (0.005/√0.000005) · √252

Case 3: r=[-0.02, 0.04, -0.01, 0.02, -0.005]
  mean=0.005, downside=[-0.02,-0.01,-0.005]
  Σd²=0.0004+0.0001+0.000025=0.000525
  downside_var=0.000525/5=0.000105
  Sortino = (0.005/√0.000105) · √252

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless (annualized Sortino ratio)

## Known Limitations

- 5-element series. Production Sortino requires a full equity curve.
- Denominator convention (all-N) differs from some textbook formulations.
- Edge cases (no downside, single return) covered in unit tests, not here.

## Regeneration

  python scripts/generate_fixtures.py --id ENG-001b --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: {ORACLE_DESCRIPTION}
Script: scripts/fixture_generators/engine_stats.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def _write_and_report(
    version_dir: Path,
    fixture_id: str,
    input_table: pa.Table,
    output_table: pa.Table,
    write_attribution_fn,
    justification: str,
) -> None:
    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    write_attribution_fn(attribution_path, justification)

    content_hashes, file_hashes = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    print(f"  {fixture_id}: {len(input_table)} cases")
    print(f"  content_sha256[input.arrow]:  {content_hashes['input.arrow']}")
    print(f"  content_sha256[output.arrow]: {content_hashes['output.arrow']}")
    print(f"  file_sha256[input.arrow]:     {file_hashes['input.arrow']}")
    print(f"  file_sha256[output.arrow]:    {file_hashes['output.arrow']}")
    print()
    print("  Paste into manifest.json versions entry:")
    print(
        f"""  {{
    "input": "input.arrow",
    "output": "output.arrow",
    "attribution": "attribution.md",
    "content_sha256": {{
      "input.arrow": "{content_hashes['input.arrow']}",
      "output.arrow": "{content_hashes['output.arrow']}"
    }},
    "file_sha256": {{
      "input.arrow": "{file_hashes['input.arrow']}",
      "output.arrow": "{file_hashes['output.arrow']}"
    }}
  }}"""
    )


def generate_eng001(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-001: Sharpe Ratio fixture."""
    input_table = _build_input_table()
    output_table = pa.table(
        {
            "oracle_sharpe": pa.array(
                [_oracle_sharpe(c, PERIODS_PER_YEAR) for c in CASES],
                type=pa.float64(),
            )
        }
    )
    _write_and_report(
        version_dir,
        "ENG-001",
        input_table,
        output_table,
        _write_attribution_eng001,
        justification,
    )


def generate_eng001b(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-001b: Sortino Ratio fixture."""
    input_table = _build_input_table()
    output_table = pa.table(
        {
            "oracle_sortino": pa.array(
                [_oracle_sortino(c, PERIODS_PER_YEAR) for c in CASES],
                type=pa.float64(),
            )
        }
    )
    _write_and_report(
        version_dir,
        "ENG-001b",
        input_table,
        output_table,
        _write_attribution_eng001b,
        justification,
    )
