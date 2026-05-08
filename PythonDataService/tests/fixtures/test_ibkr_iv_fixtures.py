"""Golden fixture validation for OPT-IB-002: IBKR IV vs NR/Brent BSM solver.

Tests that our implied_volatility() solver recovers the same IV that IBKR
reports from real market data, within atol=1e-3 (0.1 vol).

Oracle: vendor_observed — IBKR TWS modelGreeks.impliedVol.
Canonical: PythonDataService/app/volatility/solver.py::implied_volatility

Run standalone (no FastAPI app needed):
  python -m pytest tests/fixtures/test_ibkr_iv_fixtures.py -v --noconftest

The test suite skips automatically if the fixture has not yet been generated
(status=planned in manifest, no arrow files on disk). Generate it by:
  1. python scripts/capture_ibkr_snapshot.py        (requires IBKR Gateway)
  2. python scripts/generate_fixtures.py --id OPT-IB-002 --justification '...'
  3. Update manifest.json active_version and status, commit the fixture files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow.ipc as ipc
import pytest

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.volatility.solver import SolveStatus, implied_volatility  # noqa: E402

_FIXTURE_ID = "OPT-IB-002"
_FIXTURE_AVAILABLE = registry.exists_on_disk(_FIXTURE_ID)

pytestmark = pytest.mark.skipif(
    not _FIXTURE_AVAILABLE,
    reason=f"Fixture {_FIXTURE_ID} not yet generated (status=planned). "
    "Run scripts/capture_ibkr_snapshot.py then scripts/generate_fixtures.py.",
)

_OK_STATUSES = {
    SolveStatus.NEWTON_OK,
    SolveStatus.QUANTLIB_OK,
    SolveStatus.BRENT_FALLBACK,
    SolveStatus.OK,
}


def _load() -> tuple:
    files = registry.active_files(_FIXTURE_ID)
    fixture_dir = registry.fixture_dir(_FIXTURE_ID)
    mf = registry._manifest.by_id(_FIXTURE_ID)
    inp = ipc.open_file(fixture_dir / files.input).read_all()
    out = ipc.open_file(fixture_dir / files.output).read_all()
    return inp, out, mf.tolerance.atol, mf.tolerance.rtol


class TestOPTIB002IBKRImpliedVol:
    def test_row_count_nonzero(self) -> None:
        inp, _out, _atol, _rtol = _load()
        assert len(inp) > 0, "Fixture input should have at least one contract row"

    def test_output_row_count_matches_input(self) -> None:
        inp, out, _atol, _rtol = _load()
        assert len(inp) == len(out), (
            f"input has {len(inp)} rows, output has {len(out)} rows"
        )

    def test_solver_converges_on_all_contracts(self) -> None:
        """Solver must not return CONVERGENCE_FAILURE or INPUT_ERROR for any included contract."""
        inp, _out, _atol, _rtol = _load()
        failures: list[str] = []
        for row in range(len(inp)):
            spot = float(inp["spot"][row].as_py())
            strike = float(inp["strike"][row].as_py())
            ttm = float(inp["ttm_years"][row].as_py())
            rate = float(inp["rate"][row].as_py())
            dividend = float(inp["dividend"][row].as_py())
            mid = float(inp["mid"][row].as_py())
            is_call = bool(inp["is_call"][row].as_py())
            right = inp["right"][row].as_py()
            expiry_ms = inp["expiry_ms"][row].as_py()

            result = implied_volatility(
                option_price=mid,
                spot=spot,
                strike=strike,
                ttm=ttm,
                rate=rate,
                dividend=dividend,
                is_call=is_call,
            )
            if result.status not in _OK_STATUSES:
                failures.append(
                    f"row={row} {right} K={strike:.1f} expiry_ms={expiry_ms} "
                    f"mid={mid:.4f} ttm={ttm:.4f}: status={result.status}"
                )

        assert not failures, (
            f"{len(failures)}/{len(inp)} contracts failed to converge:\n"
            + "\n".join(failures)
        )

    def test_solver_iv_matches_ibkr_within_tolerance(self) -> None:
        """Our solver IV must match IBKR-reported IV within atol=1e-3 (0.1 vol)."""
        inp, out, atol, rtol = _load()
        mismatches: list[str] = []
        skipped = 0

        for row in range(len(inp)):
            spot = float(inp["spot"][row].as_py())
            strike = float(inp["strike"][row].as_py())
            ttm = float(inp["ttm_years"][row].as_py())
            rate = float(inp["rate"][row].as_py())
            dividend = float(inp["dividend"][row].as_py())
            mid = float(inp["mid"][row].as_py())
            is_call = bool(inp["is_call"][row].as_py())
            right = inp["right"][row].as_py()
            oracle_iv = float(out["oracle_ibkr_iv"][row].as_py())

            result = implied_volatility(
                option_price=mid,
                spot=spot,
                strike=strike,
                ttm=ttm,
                rate=rate,
                dividend=dividend,
                is_call=is_call,
            )

            if result.iv is None:
                skipped += 1
                continue

            tol = atol + rtol * abs(oracle_iv)
            diff = abs(result.iv - oracle_iv)
            if diff > tol:
                mismatches.append(
                    f"row={row} {right} K={strike:.1f} ttm={ttm:.4f} "
                    f"our_iv={result.iv:.6f} ibkr_iv={oracle_iv:.6f} "
                    f"diff={diff:.2e} tol={tol:.2e} status={result.status}"
                )

        total = len(inp)
        assert not mismatches, (
            f"{len(mismatches)}/{total} contracts exceed tolerance "
            f"(atol={atol}, rtol={rtol}), {skipped} skipped (no IV):\n"
            + "\n".join(mismatches)
        )

    def test_ibkr_iv_range_plausible(self) -> None:
        """IBKR oracle IVs must all be in [0.05, 2.0] — the capture filter guarantees this."""
        _inp, out, _atol, _rtol = _load()
        ivs = out["oracle_ibkr_iv"].to_pylist()
        bad = [iv for iv in ivs if not (0.05 <= iv <= 2.0)]
        assert not bad, f"Oracle IVs outside [0.05, 2.0]: {bad}"

    def test_ttm_positive(self) -> None:
        inp, _out, _atol, _rtol = _load()
        ttms = inp["ttm_years"].to_pylist()
        bad = [t for t in ttms if t <= 0]
        assert not bad, f"Non-positive TTM values: {bad}"

    def test_mid_positive(self) -> None:
        inp, _out, _atol, _rtol = _load()
        mids = inp["mid"].to_pylist()
        bad = [m for m in mids if m <= 0]
        assert not bad, f"Non-positive mid prices: {bad}"
