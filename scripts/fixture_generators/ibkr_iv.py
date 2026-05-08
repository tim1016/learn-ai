"""Generator for OPT-IB-002: IBKR reported IV vs Newton-Raphson/Brent solver.

Reads a pre-captured IBKR market data snapshot (Arrow IPC) from
scripts/ibkr_snapshots/opt_ib_002/ and produces the golden fixture.

Oracle: vendor_observed — IBKR modelGreeks.impliedVol from TWS API.
Canonical: PythonDataService/app/volatility/solver.py::implied_volatility

The fixture validates that our NR/Brent BSM solver recovers the same IV
that IBKR reports from the same mid-price and contract parameters. Tolerance
is 1e-3 (0.1 vol) to account for IBKR's proprietary model adjustments vs
our pure BSM.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

REPO_ROOT = Path(__file__).parent.parent.parent
SNAPSHOT_DIR = REPO_ROOT / "scripts" / "ibkr_snapshots" / "opt_ib_002"

sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.io import write_arrow  # noqa: E402


def _latest_snapshot() -> Path:
    """Return the most recently modified snapshot arrow file."""
    candidates = sorted(SNAPSHOT_DIR.glob("snapshot_*.arrow"))
    if not candidates:
        raise FileNotFoundError(
            f"No snapshot files found in {SNAPSHOT_DIR}.\n"
            "Run: python scripts/capture_ibkr_snapshot.py"
        )
    return candidates[-1]


def _snapshot_date_str(path: Path) -> str:
    """Extract human-readable capture date from snapshot filename."""
    stem = path.stem  # e.g. snapshot_20260508_143022
    parts = stem.split("_")
    if len(parts) >= 3:
        try:
            dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            pass
    return stem


def generate_optib002(version_dir: Path, justification: str = "") -> dict:
    """OPT-IB-002: IBKR reported IV vs Newton-Raphson/Brent BSM solver."""
    snapshot_path = _latest_snapshot()
    snapshot_date = _snapshot_date_str(snapshot_path)

    # ── Load snapshot ─────────────────────────────────────────────────────────
    reader = ipc.open_file(snapshot_path)
    snap = reader.read_all()
    n = len(snap)

    # ── Build input.arrow — contract params + market prices ──────────────────
    # ibkr_model_price is the price IBKR's own model used when it computed
    # impliedVol (modelGreeks.optPrice). Our solver must invert this same price
    # so both oracles answer the same question.
    inp = pa.table({
        "symbol": snap["symbol"],
        "right": snap["right"],
        "strike": snap["strike"],
        "expiry_ms": snap["expiry_ms"],
        "snapshot_ms": snap["snapshot_ms"],
        "spot": snap["spot"],
        "bid": snap["bid"],
        "ask": snap["ask"],
        "mid": snap["mid"],
        "ibkr_model_price": snap["ibkr_model_price"],
        "ttm_years": snap["ttm_years"],
        "rate": snap["rate"],
        "dividend": snap["dividend"],
        "is_call": snap["is_call"],
    })

    # ── Build output.arrow — IBKR IV is the oracle ───────────────────────────
    out = pa.table({
        "oracle_ibkr_iv": snap["ibkr_iv"],
    })

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    # ── Attribution ───────────────────────────────────────────────────────────
    symbols = sorted(set(snap["symbol"].to_pylist()))
    expiries = sorted(set(snap["expiry_ms"].to_pylist()))
    ibkr_ivs = snap["ibkr_iv"].to_pylist()
    iv_min = min(ibkr_ivs)
    iv_max = max(ibkr_ivs)
    rates = snap["rate"].to_pylist()
    divs = snap["dividend"].to_pylist()
    rate_val = rates[0] if rates else 0.0
    div_val = divs[0] if divs else 0.0

    expiry_dates = []
    for e_ms in expiries:
        dt = datetime.fromtimestamp(e_ms / 1000, tz=timezone.utc)
        expiry_dates.append(dt.strftime("%Y-%m-%d"))

    (version_dir / "attribution.md").write_text(
        f"""# OPT-IB-002 — Implied Volatility: IBKR Reported vs NR/Brent BSM Solver

Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d")}
Oracle: vendor_observed — IBKR TWS API modelGreeks.impliedVol
Canonical: PythonDataService/app/volatility/solver.py::implied_volatility

## Formula

Our solver uses a three-stage cascade:
  1. Newton-Raphson with vega step (primary; quadratic convergence)
  2. QuantLib impliedVolatility (T ≥ 1 calendar day only)
  3. scipy.optimize.brentq fallback ([MIN_IV=0.005, MAX_IV=5.0])

Solves for σ such that BSM(S, K, T, r, q, σ) = ibkr_model_price.

Both the oracle and our solver invert the same price: modelGreeks.optPrice
(the option price IBKR's model used to back out impliedVol). Using mid-price
as input would compare two quantities from different prices and is incorrect.

Seed: Brenner-Subrahmanyam approximation σ₀ ≈ √(2π/T) · price/spot for ATM.

Reference: Hull §19.11 (IV); Brent (1973) §4; Brenner-Subrahmanyam (1988) FAJ.
Solver source: app/volatility/solver.py (canonical per docs/math-sources-of-truth.md).

## Input data provenance

Snapshot: {snapshot_path.name}
Captured: {snapshot_date}
Capture script: scripts/capture_ibkr_snapshot.py

Underlying: {', '.join(symbols)}
Expirations: {', '.join(expiry_dates)} ({len(expiries)} expiry/ies)
Contracts: {n} (calls + puts, strikes within ±10% of spot)
Rate: {rate_val:.4f} (continuously compounded, ~Fed Funds at capture)
Dividend: {div_val:.4f} (continuously compounded SPY trailing yield at capture)

## Oracle

IBKR's modelGreeks.impliedVol from a standard option market-data request
(reqMktData with empty genericTickList — TWS returns modelGreeks automatically
for options). modelGreeks.optPrice is the model price IBKR backed this IV from.
IBKR's methodology is proprietary; their model may use discrete dividends or
adjustments not present in our pure BSM. The tolerance floor is set accordingly.

## Oracle value range

IBKR IV range in this snapshot: [{iv_min:.4f}, {iv_max:.4f}]

## Tolerance

atol=1e-3, rtol=0.0

Both IBKR and our solver invert BSM against modelGreeks.optPrice; the 1e-3
floor accounts for IBKR's proprietary model adjustments (discrete dividends,
calibration) vs our pure continuous-dividend BSM. Contracts with bid < $0.05,
ask ≤ 0, crossed quotes, or IBKR IV outside [0.05, 2.0] excluded at capture.

## Justification

{justification or "Initial generation."}

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )

    import json as _json
    version_entry = {
        "content_sha256": content_h,
        "file_sha256": file_h,
    }
    print("\nPaste into manifest.json OPT-IB-002 versions[\"1\"]:")
    print(_json.dumps(version_entry, indent=6))

    return version_entry
