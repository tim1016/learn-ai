"""Golden fixture validation for engine-statistics fixtures (ENG-001, ENG-001b).

Loads each fixture via the registry, calls the canonical function on every
input case, and asserts numerical agreement with the oracle output at the
tolerance pinned in manifest.json.

Run in isolation (no FastAPI app needed):
  python -m pytest tests/fixtures/test_engine_stats_fixtures.py -v --noconftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.engine.results.statistics import _sharpe, _sortino  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(fixture_id: str) -> tuple[pa.Table, pa.Table, float, float]:
    files = registry.active_files(fixture_id)
    fixture_dir = registry.fixture_dir(fixture_id)
    manifest_fixture = registry._manifest.by_id(fixture_id)

    inp = pa.ipc.open_file(fixture_dir / files.input).read_all()
    out = pa.ipc.open_file(fixture_dir / files.output).read_all()
    atol = manifest_fixture.tolerance.atol
    rtol = manifest_fixture.tolerance.rtol
    return inp, out, atol, rtol


def _rows_to_returns(inp: pa.Table) -> list[tuple[list[float], int]]:
    """Reconstruct (returns, periods_per_year) from each fixture row."""
    result = []
    for i in range(len(inp)):
        returns = [
            inp["r0"][i].as_py(),
            inp["r1"][i].as_py(),
            inp["r2"][i].as_py(),
            inp["r3"][i].as_py(),
            inp["r4"][i].as_py(),
        ]
        ppy = int(inp["periods_per_year"][i].as_py())
        result.append((returns, ppy))
    return result


# ---------------------------------------------------------------------------
# ENG-001: Sharpe Ratio
# ---------------------------------------------------------------------------

class TestENG001Sharpe:
    def test_case_count(self) -> None:
        inp, _, _, _ = _load("ENG-001")
        assert len(inp) == 3, f"Expected 3 cases, got {len(inp)}"

    def test_canonical_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("ENG-001")
        cases = _rows_to_returns(inp)
        oracle_sharpes = out["oracle_sharpe"].to_pylist()

        canonical_sharpes = [_sharpe(returns, ppy) for returns, ppy in cases]

        for i, (canonical, oracle) in enumerate(zip(canonical_sharpes, oracle_sharpes, strict=True)):
            assert canonical is not None, f"Case {i}: canonical _sharpe returned None unexpectedly"
            err = abs(canonical - oracle)
            assert err <= atol + rtol * abs(oracle), (
                f"Case {i}: canonical={canonical:.10f}, oracle={oracle:.10f}, err={err:.3e}, atol={atol:.3e}"
            )

    def test_sharpe_is_positive_for_positive_mean_cases(self) -> None:
        """All 3 fixture cases have positive mean returns — Sharpe should be positive."""
        _inp, out, _, _ = _load("ENG-001")
        oracle_sharpes = out["oracle_sharpe"].to_pylist()
        for i, s in enumerate(oracle_sharpes):
            assert s > 0, f"Case {i}: expected positive Sharpe for positive-mean series, got {s:.6f}"

    def test_sharpe_none_for_zero_std(self) -> None:
        """Canonical _sharpe returns None when all returns are equal (zero std)."""
        result = _sharpe([0.01, 0.01, 0.01, 0.01, 0.01], 252)
        assert result is None, f"Expected None for zero-std series, got {result}"

    def test_sharpe_none_for_single_return(self) -> None:
        """Canonical _sharpe returns None when len < 2."""
        result = _sharpe([0.01], 252)
        assert result is None


# ---------------------------------------------------------------------------
# ENG-001b: Sortino Ratio
# ---------------------------------------------------------------------------

class TestENG001bSortino:
    def test_case_count(self) -> None:
        inp, _, _, _ = _load("ENG-001b")
        assert len(inp) == 3, f"Expected 3 cases, got {len(inp)}"

    def test_canonical_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("ENG-001b")
        cases = _rows_to_returns(inp)
        oracle_sortinos = out["oracle_sortino"].to_pylist()

        canonical_sortinos = [_sortino(returns, ppy) for returns, ppy in cases]

        for i, (canonical, oracle) in enumerate(zip(canonical_sortinos, oracle_sortinos, strict=True)):
            assert canonical is not None, f"Case {i}: canonical _sortino returned None unexpectedly"
            err = abs(canonical - oracle)
            assert err <= atol + rtol * abs(oracle), (
                f"Case {i}: canonical={canonical:.10f}, oracle={oracle:.10f}, err={err:.3e}, atol={atol:.3e}"
            )

    def test_sortino_none_when_no_downside(self) -> None:
        """Canonical _sortino returns None when no negative returns exist."""
        result = _sortino([0.01, 0.02, 0.005, 0.03, 0.01], 252)
        assert result is None, f"Expected None for all-positive returns, got {result}"

    def test_sortino_none_for_single_return(self) -> None:
        """Canonical _sortino returns None when len < 2."""
        result = _sortino([-0.01], 252)
        assert result is None

    def test_sortino_greater_than_sharpe_for_low_downside(self) -> None:
        """When downside is rare, Sortino > Sharpe (smaller penalty denominator)."""
        # Case 1: [0.01, 0.02, -0.01, 0.03, 0.01] — single small downside
        _inp, out, _, _ = _load("ENG-001b")
        _inp_001, out_001, _, _ = _load("ENG-001")
        oracle_sortino_c1 = out["oracle_sortino"][0].as_py()
        oracle_sharpe_c1 = out_001["oracle_sharpe"][0].as_py()
        assert oracle_sortino_c1 > oracle_sharpe_c1, (
            f"Expected Sortino > Sharpe for low-downside series; "
            f"Sortino={oracle_sortino_c1:.4f}, Sharpe={oracle_sharpe_c1:.4f}"
        )
