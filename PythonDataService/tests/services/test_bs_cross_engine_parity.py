"""Cross-engine Black-Scholes parity test (Phase 1.4 of migration plan).

Pins equivalence between the two in-repo BSM implementations on a 360-case
input grid:

- ``app/services/bs_greeks.py::bs_european_price`` — closed-form, continuous-time
- ``app/services/quantlib_pricer.py::price_option`` with ``engine=ANALYTIC_BS``

Both compute the same closed-form Black-Scholes-Merton price; they must agree
to numerical roundoff. If they don't, one of them has drifted and the test
identifies the bad case.

Fixture: ``tests/fixtures/golden/bs-price-cross-engine/`` (input grid + tolerance).
Documentation: ``docs/architecture/numerical-authority-migration-plan.md`` Phase 1.4.

Tolerance: ``atol=1e-10, rtol=0``. Both paths evaluate the same formula in
single-evaluation closed-form math with no recursive accumulation; agreement
should be at last-bit machine precision.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from itertools import product
from pathlib import Path

import pytest

from app.services.bs_greeks import bs_european_price
from app.services.quantlib_pricer import (
    PricingEngine,
    price_option,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "golden" / "bs-price-cross-engine" / "cases.json"


def _load_grid() -> dict:
    with FIXTURE.open() as f:
        return json.load(f)


def _grid_cases() -> list[tuple]:
    """Expand the fixture grid into individual cases."""
    grid = _load_grid()["grid"]
    return list(
        product(
            grid["spot"],
            grid["strike"],
            grid["ttm_days"],
            grid["volatility"],
            grid["rate"],
            grid["dividend"],
            grid["option_type"],
        )
    )


def _id(case: tuple) -> str:
    spot, strike, ttm_days, vol, rate, q, opt = case
    return f"S{spot}_K{strike}_T{ttm_days}d_v{vol}_r{rate}_q{q}_{opt}"


@pytest.fixture(scope="module")
def tolerance() -> dict:
    return _load_grid()["tolerance"]


# Skip the whole module if QuantLib isn't installed in this environment.
ql = pytest.importorskip(
    "QuantLib",
    reason="QuantLib not installed; cross-engine parity test requires it. "
    "Install with `pip install QuantLib`.",
)


@pytest.mark.parametrize("case", _grid_cases(), ids=_id)
def test_bs_price_cross_engine_parity(case: tuple, tolerance: dict) -> None:
    """For every grid case, both engines must produce the same price."""
    spot, strike, ttm_days, vol, rate, q, opt = case
    is_call = opt == "call"

    # Closed-form: takes ttm_years directly.
    ttm_years = ttm_days / 365.0
    closed_form = bs_european_price(
        spot=spot,
        strike=strike,
        ttm_years=ttm_years,
        rate=rate,
        volatility=vol,
        is_call=is_call,
        dividend=q,
    )

    # QuantLib: takes calendar dates. We use Actual/365Fixed day count
    # (set in quantlib_pricer._build_process), so an evaluation date of
    # 2026-01-01 plus ttm_days = expiration_date produces ttm_years that
    # exactly matches the closed-form input.
    eval_date = date(2026, 1, 1)
    exp_date = eval_date + timedelta(days=ttm_days)
    ql_result = price_option(
        spot=spot,
        strike=strike,
        risk_free_rate=rate,
        volatility=vol,
        expiration_date=exp_date,
        option_type=opt,
        evaluation_date=eval_date,
        dividend_yield=q,
        engine=PricingEngine.ANALYTIC_BS,
    )

    diff = abs(closed_form - ql_result.price)
    assert diff <= tolerance["atol"], (
        f"BS price disagreement: closed_form={closed_form!r} vs "
        f"quantlib={ql_result.price!r}, diff={diff:.2e}, "
        f"atol={tolerance['atol']:.2e}, case={_id(case)}"
    )


def test_fixture_grid_size_is_pinned() -> None:
    """Sanity check: if someone expands the grid, the test count should
    change deliberately. This pins the count at 360 cases so an accidental
    grid change shows up in the test count diff."""
    cases = _grid_cases()
    assert len(cases) == 360, (
        f"Expected 360 grid cases (3×1×5×3×2×2×2). Got {len(cases)}. "
        f"If this is intentional, update the assertion and the attribution.md."
    )
