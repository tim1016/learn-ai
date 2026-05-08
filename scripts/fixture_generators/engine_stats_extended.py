"""Generators for ENG-002 through ENG-005 golden fixtures.

Oracle: hand_computed — numpy vectorized path independent from canonical pure-Python loops.

ENG-002 — Max Drawdown
  Oracle: numpy accumulate-max then vectorized fraction
  Canonical: statistics.py::_max_drawdown(curve)

ENG-003 — Trade Statistics
  Oracle: explicit Python formula matching compute_trade_statistics arithmetic
  Canonical: statistics.py::compute_trade_statistics(trades)

ENG-004 — CAGR (Compound Annual Growth Rate)
  Oracle: (final/initial)^(1/years) - 1 using numpy power
  Canonical: statistics.py::compute_portfolio_statistics — ann_return formula

ENG-005 — Calmar Ratio
  Oracle: CAGR / max_drawdown using same numpy paths as ENG-002/ENG-004
  Canonical: statistics.py::compute_portfolio_statistics — calmar formula

Input cases (3 per fixture):

ENG-002 equity curves (5 points each):
  Case 1: [100, 110, 95, 105, 100]   → MDD = 15/110 ≈ 0.136364
  Case 2: [100,  90, 80, 95, 100]    → MDD = 20/100 = 0.20
  Case 3: [100, 120, 130, 110, 125]  → MDD = 20/130 ≈ 0.153846

ENG-003 trade pnl_pct sets (4 trades each):
  Case 1: [+0.05, -0.02, +0.03, -0.01]
  Case 2: [+0.10, +0.05, -0.08, -0.02]
  Case 3: [-0.05, -0.03, +0.04, -0.01]

ENG-004 / ENG-005 inputs (initial_cash, final_equity, trading_days, curve[5]):
  Case 1: initial=1000, final=1100, days=252,  curve=[1000,1050,1100,1020,1100]
  Case 2: initial=1000, final=1200, days=252,  curve=[1000, 900,1200,1100,1200]
  Case 3: initial=1000, final=1150, days=252,  curve=[1000,1000, 850, 900,1150]
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.io import write_arrow  # noqa: E402

def _generation_date() -> str:
    return date.today().isoformat()
TRADING_DAYS_PER_YEAR = 252

# ── ENG-002: Max Drawdown ─────────────────────────────────────────────────────

MDD_CURVES: list[list[float]] = [
    [100.0, 110.0, 95.0, 105.0, 100.0],
    [100.0, 90.0, 80.0, 95.0, 100.0],
    [100.0, 120.0, 130.0, 110.0, 125.0],
]


def _oracle_mdd(curve: list[float]) -> float:
    a = np.array(curve, dtype=np.float64)
    running_max = np.maximum.accumulate(a)
    drawdowns = np.where(running_max > 0, (running_max - a) / running_max, 0.0)
    return float(np.max(drawdowns))


# ── ENG-003: Trade Statistics ─────────────────────────────────────────────────

TRADE_CASES: list[list[float]] = [
    [0.05, -0.02, 0.03, -0.01],
    [0.10, 0.05, -0.08, -0.02],
    [-0.05, -0.03, 0.04, -0.01],
]


def _oracle_trade_stats(pnl_pcts: list[float]) -> dict[str, Any]:
    total = len(pnl_pcts)
    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -(gross_loss / len(losses)) if losses else 0.0
    avg_trade = sum(pnl_pcts) / total

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    if losses:
        payoff_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)
    elif wins:
        payoff_ratio = float("inf")
    else:
        payoff_ratio = 0.0

    return {
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / total,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "avg_trade_pct": avg_trade,
        "largest_win_pct": max(pnl_pcts),
        "largest_loss_pct": min(pnl_pcts),
        "profit_factor": profit_factor,
        "expectancy_pct": avg_trade,
        "payoff_ratio": payoff_ratio,
    }


# ── ENG-004/ENG-005: CAGR and Calmar ─────────────────────────────────────────

CALMAR_CASES: list[tuple[float, float, int, list[float]]] = [
    (1000.0, 1100.0, 252, [1000.0, 1050.0, 1100.0, 1020.0, 1100.0]),
    (1000.0, 1200.0, 252, [1000.0, 900.0, 1200.0, 1100.0, 1200.0]),
    (1000.0, 1150.0, 252, [1000.0, 1000.0, 850.0, 900.0, 1150.0]),
]


def _oracle_cagr(initial_cash: float, final_equity: float, trading_days: int) -> float:
    years = trading_days / TRADING_DAYS_PER_YEAR
    return float((final_equity / initial_cash) ** (1.0 / years) - 1.0)


def _oracle_calmar(initial_cash: float, final_equity: float, trading_days: int, curve: list[float]) -> float:
    cagr = _oracle_cagr(initial_cash, final_equity, trading_days)
    mdd = _oracle_mdd(curve)
    return float(cagr / mdd)


# ── Output helpers ────────────────────────────────────────────────────────────


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


# ── ENG-002 ───────────────────────────────────────────────────────────────────


def _write_attribution_eng002(path: Path, justification: str) -> None:
    path.write_text(
        f"""# ENG-002 — Maximum Drawdown

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 5-point equity
curves spanning typical drawdown patterns. Hand-designed for exact verifiability.
See `engine_stats_extended.py::MDD_CURVES`.

**Layer 2 — Methodology provenance:** Bacon, *Practical Portfolio Performance
Measurement and Attribution* (2e), §8.2. Max drawdown = max over all t of
(peak_t - equity_t) / peak_t.

**Layer 3 — Independent numerical oracle:** numpy `maximum.accumulate` (running
max) + vectorized `(running_max - equity) / running_max` — different path from
canonical pure-Python loop with early exit.

## Formula

max_drawdown = max_t[(peak_t - equity_t) / peak_t]  where peak_t > 0
Returns a positive fraction (0.20 = 20% drawdown)

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::_max_drawdown`
Signature: `_max_drawdown(curve: Sequence[float]) -> float`
Uses pure-Python loop with running peak.

## Hand-Verification

Case 1: curve=[100, 110, 95, 105, 100]
  Running max: [100, 110, 110, 110, 110]
  Max DD at index 2: (110-95)/110 = 15/110 ≈ 0.136364

Case 2: curve=[100, 90, 80, 95, 100]
  Running max: [100, 100, 100, 100, 100]
  Max DD at index 2: (100-80)/100 = 0.20

Case 3: curve=[100, 120, 130, 110, 125]
  Running max: [100, 120, 130, 130, 130]
  Max DD at index 3: (130-110)/130 = 20/130 ≈ 0.153846

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless fraction (0.20 = 20% max drawdown)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-002 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: numpy accumulate-max + vectorized fraction
Script: scripts/fixture_generators/engine_stats_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_eng002(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-002: Max Drawdown fixture."""
    n = len(MDD_CURVES)
    input_table = pa.table(
        {
            "e0": pa.array([c[0] for c in MDD_CURVES], type=pa.float64()),
            "e1": pa.array([c[1] for c in MDD_CURVES], type=pa.float64()),
            "e2": pa.array([c[2] for c in MDD_CURVES], type=pa.float64()),
            "e3": pa.array([c[3] for c in MDD_CURVES], type=pa.float64()),
            "e4": pa.array([c[4] for c in MDD_CURVES], type=pa.float64()),
        }
    )
    output_table = pa.table(
        {
            "oracle_mdd": pa.array(
                [_oracle_mdd(c) for c in MDD_CURVES],
                type=pa.float64(),
            )
        }
    )
    _ = n
    _write_and_report(version_dir, "ENG-002", input_table, output_table, _write_attribution_eng002, justification)


# ── ENG-003 ───────────────────────────────────────────────────────────────────


def _write_attribution_eng003(path: Path, justification: str) -> None:
    path.write_text(
        f"""# ENG-003 — Trade Statistics

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 4-trade pnl_pct
sets spanning win/loss mixes. Hand-designed for exact verifiability.
See `engine_stats_extended.py::TRADE_CASES`.

**Layer 2 — Methodology provenance:** Standard portfolio statistics. Profit factor
= gross_win / gross_loss; payoff_ratio = avg_win / |avg_loss|; expectancy = avg_trade.

**Layer 3 — Independent numerical oracle:** Explicit Python formula replicating
the same arithmetic as the canonical, but written from first principles without
calling `compute_trade_statistics`.

## Columns (output)

- total_trades, winning_trades, losing_trades
- win_rate = winning / total
- avg_win_pct = mean of positive pnl_pcts
- avg_loss_pct = mean of negative pnl_pcts (signed: negative number)
- avg_trade_pct = mean of all pnl_pcts
- largest_win_pct = max(pnl_pcts)
- largest_loss_pct = min(pnl_pcts)
- profit_factor = gross_win / gross_loss
- expectancy_pct = avg_trade_pct (expectation of single trade)
- payoff_ratio = avg_win / |avg_loss|

## Tolerance

Integer columns: exact. Float columns: atol=1e-12, rtol=0.0.

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_trade_statistics`
Accepts Sequence of _TradeLike (pnl_pcts extracted as float).

## Regeneration

  python scripts/generate_fixtures.py --id ENG-003 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: hand_computed — explicit formula matching canonical arithmetic
Script: scripts/fixture_generators/engine_stats_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_eng003(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-003: Trade Statistics fixture."""
    stats = [_oracle_trade_stats(c) for c in TRADE_CASES]
    input_table = pa.table(
        {
            "t0_pnl_pct": pa.array([c[0] for c in TRADE_CASES], type=pa.float64()),
            "t1_pnl_pct": pa.array([c[1] for c in TRADE_CASES], type=pa.float64()),
            "t2_pnl_pct": pa.array([c[2] for c in TRADE_CASES], type=pa.float64()),
            "t3_pnl_pct": pa.array([c[3] for c in TRADE_CASES], type=pa.float64()),
        }
    )
    output_table = pa.table(
        {
            "total_trades": pa.array([s["total_trades"] for s in stats], type=pa.int64()),
            "winning_trades": pa.array([s["winning_trades"] for s in stats], type=pa.int64()),
            "losing_trades": pa.array([s["losing_trades"] for s in stats], type=pa.int64()),
            "win_rate": pa.array([s["win_rate"] for s in stats], type=pa.float64()),
            "avg_win_pct": pa.array([s["avg_win_pct"] for s in stats], type=pa.float64()),
            "avg_loss_pct": pa.array([s["avg_loss_pct"] for s in stats], type=pa.float64()),
            "avg_trade_pct": pa.array([s["avg_trade_pct"] for s in stats], type=pa.float64()),
            "largest_win_pct": pa.array([s["largest_win_pct"] for s in stats], type=pa.float64()),
            "largest_loss_pct": pa.array([s["largest_loss_pct"] for s in stats], type=pa.float64()),
            "profit_factor": pa.array([s["profit_factor"] for s in stats], type=pa.float64()),
            "expectancy_pct": pa.array([s["expectancy_pct"] for s in stats], type=pa.float64()),
            "payoff_ratio": pa.array([s["payoff_ratio"] for s in stats], type=pa.float64()),
        }
    )
    _write_and_report(version_dir, "ENG-003", input_table, output_table, _write_attribution_eng003, justification)


# ── ENG-004 ───────────────────────────────────────────────────────────────────


def _write_attribution_eng004(path: Path, justification: str) -> None:
    path.write_text(
        f"""# ENG-004 — CAGR (Compound Annual Growth Rate)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 equity-curve cases with
known initial/final values and trading_days. See `engine_stats_extended.py::CALMAR_CASES`.

**Layer 2 — Methodology provenance:** Standard finance definition.
CAGR = (final_equity / initial_cash)^(1/years) - 1
where years = trading_days / {TRADING_DAYS_PER_YEAR}.

**Layer 3 — Independent numerical oracle:** numpy power function — different
floating-point path from canonical `(x)**(1/y)` in pure Python.

## Formula

years = trading_days / {TRADING_DAYS_PER_YEAR}
CAGR  = (final_equity / initial_cash)^(1/years) - 1

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics`
Key line: `ann_return = (final_equity / initial_cash) ** (1 / years) - 1`

## Hand-Verification

Case 1: initial=1000, final=1100, days=252 → years=1.0 → CAGR=0.10
Case 2: initial=1000, final=1200, days=252 → years=1.0 → CAGR=0.20
Case 3: initial=1000, final=1150, days=252 → years=1.0 → CAGR=0.15

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless annualized rate (0.10 = 10% CAGR)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-004 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: numpy power (final/initial)^(1/years) - 1
Script: scripts/fixture_generators/engine_stats_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_eng004(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-004: CAGR fixture."""
    input_table = pa.table(
        {
            "initial_cash": pa.array([c[0] for c in CALMAR_CASES], type=pa.float64()),
            "final_equity": pa.array([c[1] for c in CALMAR_CASES], type=pa.float64()),
            "trading_days": pa.array([c[2] for c in CALMAR_CASES], type=pa.int64()),
        }
    )
    output_table = pa.table(
        {
            "oracle_cagr": pa.array(
                [_oracle_cagr(c[0], c[1], c[2]) for c in CALMAR_CASES],
                type=pa.float64(),
            )
        }
    )
    _write_and_report(version_dir, "ENG-004", input_table, output_table, _write_attribution_eng004, justification)


# ── ENG-005 ───────────────────────────────────────────────────────────────────


def _write_attribution_eng005(path: Path, justification: str) -> None:
    mdd_cases = [(c[0], c[1], c[2], c[3], _oracle_mdd(c[3]), _oracle_cagr(c[0], c[1], c[2])) for c in CALMAR_CASES]
    calmar_cases = [(m[4], m[5], m[5] / m[4]) for m in mdd_cases]
    path.write_text(
        f"""# ENG-005 — Calmar Ratio

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3 cases as ENG-004.
Initial cash, final equity, trading_days, and 5-point equity curve.

**Layer 2 — Methodology provenance:** Young (1991) "The Calmar Ratio: A Smoother
Tool". Calmar = CAGR / max_drawdown_pct.

**Layer 3 — Independent numerical oracle:** Composed from ENG-002 and ENG-004
numpy paths: `_oracle_cagr / _oracle_mdd`.

## Formula

years   = trading_days / {TRADING_DAYS_PER_YEAR}
CAGR    = (final_equity / initial_cash)^(1/years) - 1
max_dd  = max_t[(peak_t - equity_t) / peak_t]
Calmar  = CAGR / max_dd

## Hand-Verification

Case 1: MDD={calmar_cases[0][0]:.6f}, CAGR={calmar_cases[0][1]:.6f} → Calmar≈{calmar_cases[0][2]:.6f}
Case 2: MDD={calmar_cases[1][0]:.6f}, CAGR={calmar_cases[1][1]:.6f} → Calmar={calmar_cases[1][2]:.6f}
Case 3: MDD={calmar_cases[2][0]:.6f}, CAGR={calmar_cases[2][1]:.6f} → Calmar={calmar_cases[2][2]:.6f}

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics`
Key lines: `calmar = ann_return / max_dd` (requires max_dd > 0 and years > 0).

## Input Columns

initial_cash, final_equity, trading_days, e0..e4 (equity curve points)

## Tolerance

atol=1e-9, rtol=0.0

## Units

dimensionless (annualized CAGR per unit of max drawdown)

## Regeneration

  python scripts/generate_fixtures.py --id ENG-005 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: _oracle_cagr / _oracle_mdd (numpy paths)
Script: scripts/fixture_generators/engine_stats_extended.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_eng005(version_dir: Path, justification: str = "") -> None:
    """Generate ENG-005: Calmar Ratio fixture."""
    input_table = pa.table(
        {
            "initial_cash": pa.array([c[0] for c in CALMAR_CASES], type=pa.float64()),
            "final_equity": pa.array([c[1] for c in CALMAR_CASES], type=pa.float64()),
            "trading_days": pa.array([c[2] for c in CALMAR_CASES], type=pa.int64()),
            "e0": pa.array([c[3][0] for c in CALMAR_CASES], type=pa.float64()),
            "e1": pa.array([c[3][1] for c in CALMAR_CASES], type=pa.float64()),
            "e2": pa.array([c[3][2] for c in CALMAR_CASES], type=pa.float64()),
            "e3": pa.array([c[3][3] for c in CALMAR_CASES], type=pa.float64()),
            "e4": pa.array([c[3][4] for c in CALMAR_CASES], type=pa.float64()),
        }
    )
    output_table = pa.table(
        {
            "oracle_calmar": pa.array(
                [_oracle_calmar(c[0], c[1], c[2], c[3]) for c in CALMAR_CASES],
                type=pa.float64(),
            )
        }
    )
    _write_and_report(version_dir, "ENG-005", input_table, output_table, _write_attribution_eng005, justification)
