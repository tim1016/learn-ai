"""Generate BS-008, the Strategy Builder's browser-vs-canonical Black-Scholes parity fixture.

The Strategy Builder re-prices a strategy in the browser on every leg edit
(ADR 0068 exception 1: ``Frontend/src/app/utils/black-scholes.ts`` and
``strategy-payoff.ts``). The oracle is the canonical Python: ``bs_greeks.py``
for one option's price and Greeks, ``strategy_engine.py`` for a strategy's
expiry payoff, current-time P&L (with its what-if shifts) and aggregate
Greeks. This script imports them and carries no formula of its own.

Run from ``PythonDataService/``::

    DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.fixture_generators.bs_browser_parity

It writes ``input.json``, ``output.json`` and ``attribution.md`` under
``tests/fixtures/golden/options-pricing/BS-008/v1/``, records their hashes in
``tests/fixtures/golden/manifest.json``, and copies ``output.json`` byte for
byte to ``Frontend/src/testing/bs-parity/grid.json``: the Angular unit-test
builder resolves a spec's imports under ``Frontend/src`` only.

Regenerate only when the canonical or the case lists change, and say which in
the commit. Never hand-edit any of the four files.
"""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any, Final

from app.models.strategy import StrategyLeg
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.services.strategy_engine import compute_current_curve, compute_greek_curves, compute_payoff_curve
from tests.fixtures.golden_support.hashing import compute_hashes, file_sha256

FIXTURE_ID: Final = "BS-008"
GENERATED: Final = "2026-10-14"
ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURE_DIR: Final = ROOT / "tests" / "fixtures" / "golden" / "options-pricing" / FIXTURE_ID / "v1"
MANIFEST_PATH: Final = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"
FRONTEND_COPY: Final = ROOT.parent / "Frontend" / "src" / "testing" / "bs-parity" / "grid.json"
REGENERATE_COMMAND: Final = (
    'cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.fixture_generators.bs_browser_parity'
)

# One option: the 360-case grid the price kernel has been pinned on since #2747.
GRID: Final[dict[str, list[Any]]] = {
    "spot": [80.0, 100.0, 120.0],
    "strike": [100.0],
    "ttm_days": [7, 30, 90, 180, 365],
    "volatility": [0.10, 0.20, 0.40],
    "rate": [0.0, 0.05],
    "dividend": [0.0, 0.02],
    "option_type": ["call", "put"],
}


def _leg(strike: float, option_type: str, position: str, premium: float, iv: float, quantity: int) -> dict[str, Any]:
    return {
        "strike": strike,
        "option_type": option_type,
        "position": position,
        "premium": premium,
        "iv": iv,
        "quantity": quantity,
    }


# A strategy. Its prices are the server's curve grid: ``spot * (1 +- price_range_pct)`` over
# ``curve_points`` points.
STRATEGIES: Final[list[dict[str, Any]]] = [
    {
        "name": "bull_call_spread_95_105",
        "spot": 100.0,
        "rate": 0.05,
        "ttm_days": 30,
        "price_range_pct": 0.10,
        "curve_points": 5,
        "legs": [_leg(95.0, "call", "long", 7.2, 0.26, 1), _leg(105.0, "call", "short", 2.6, 0.23, 1)],
    },
    # A leg the chain gave no IV (#3221): the server values it at intrinsic, -6 / -1 / +4 at 95 / 100 / 105.
    {
        "name": "long_call_95_without_iv",
        "spot": 100.0,
        "rate": 0.05,
        "ttm_days": 30,
        "price_range_pct": 0.10,
        "curve_points": 5,
        "legs": [_leg(95.0, "call", "long", 6.0, 0.0, 1)],
    },
    {
        "name": "iron_condor_two_contracts",
        "spot": 100.0,
        "rate": 0.043,
        "ttm_days": 45,
        "price_range_pct": 0.20,
        "curve_points": 9,
        "legs": [
            _leg(90.0, "put", "long", 0.45, 0.31, 2),
            _leg(95.0, "put", "short", 1.3, 0.28, 2),
            _leg(105.0, "call", "short", 1.15, 0.2, 2),
            _leg(110.0, "call", "long", 0.4, 0.22, 2),
        ],
    },
    {
        "name": "ratio_call_spread_short_leg_without_iv",
        "spot": 100.0,
        "rate": 0.05,
        "ttm_days": 30,
        "price_range_pct": 0.10,
        "curve_points": 5,
        "legs": [_leg(100.0, "call", "long", 5.1, 0.24, 1), _leg(105.0, "call", "short", 2.4, 0.0, 2)],
    },
    # Both legs sit under 10% vol, so the IV-10% what-if floors them at zero (#3221).
    {
        "name": "long_straddle_100_low_iv",
        "spot": 100.0,
        "rate": 0.05,
        "ttm_days": 30,
        "price_range_pct": 0.10,
        "curve_points": 5,
        "legs": [_leg(100.0, "call", "long", 1.0, 0.05, 1), _leg(100.0, "put", "long", 0.9, 0.04, 1)],
    },
]

# The page's what-if chips (``whatIfScenarios`` in ``strategy-builder.component.ts``) and the
# unshifted curve. ``analyze_strategy`` prices a what-if at ``max(ttm - days / 365, 0)`` years
# with ``iv_shift`` added to every leg's IV, floored at zero.
SCENARIOS: Final[list[dict[str, Any]]] = [
    {"name": "now", "iv_shift": 0.0, "time_shift_days": 0.0},
    {"name": "time_plus_5", "iv_shift": 0.0, "time_shift_days": 5.0},
    {"name": "iv_up_10", "iv_shift": 0.10, "time_shift_days": 0.0},
    {"name": "iv_down_10", "iv_shift": -0.10, "time_shift_days": 0.0},
]

# What the browser copy is held to, per quantity, with the reason. The browser's normal CDF is
# Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7); the canonical's is scipy's, exact to machine
# precision. Each atol sits between the largest difference measured over this fixture
# (2026-10-14) and the most the CDF's error can reach through that quantity. The strategy
# figures also carry the rounding of what the canonical returns: 4 dp for payoff and P&L,
# 6 dp for aggregate Greeks.
TOLERANCE: Final[dict[str, dict[str, Any]]] = {
    "price": {
        "atol": 2e-5,
        "rtol": 0.0,
        "why": "CDF error through S + K <= 220 on this grid is at most 3.3e-5; measured 1.5e-5",
    },
    "delta": {"atol": 1e-7, "rtol": 0.0, "why": "N(d1) itself: CDF error at most 1.5e-7; measured 6.9e-8"},
    "gamma": {"atol": 1e-12, "rtol": 0.0, "why": "no CDF term, float rounding only; measured 2.8e-17"},
    "theta": {
        "atol": 2e-9,
        "rtol": 0.0,
        "why": "CDF error scaled by r*K*e^-rT/365 <= 1.4e-2 is at most 2.1e-9; measured 9.5e-10",
    },
    "vega": {"atol": 1e-12, "rtol": 0.0, "why": "no CDF term, float rounding only; measured 1.1e-16"},
    "rho": {
        "atol": 1e-7,
        "rtol": 0.0,
        "why": "CDF error scaled by K*T*e^-rT/100 <= 1 is at most 1.5e-7; measured 6.7e-8",
    },
    "strategy_expiry_payoff": {
        "atol": 1e-9,
        "rtol": 0.0,
        "why": "prices and premiums carry at most 4 dp, so the canonical's 4 dp rounding removes nothing; measured 2.7e-15",
    },
    "strategy_pnl": {
        "atol": 1e-4,
        "rtol": 0.0,
        "why": "one 4 dp step: 5e-5 is the canonical's rounding, the rest the browser's CDF; measured 5.4e-5",
    },
    "strategy_delta": {
        "atol": 2e-6,
        "rtol": 0.0,
        "why": "6 dp rounding (5e-7) plus CDF error 1.5e-7 over the widest case's 8 contract-legs; measured 8.4e-7",
    },
    "strategy_gamma": {"atol": 1e-6, "rtol": 0.0, "why": "one 6 dp step, the canonical's rounding; measured 5.0e-7"},
    "strategy_theta": {"atol": 1e-6, "rtol": 0.0, "why": "one 6 dp step, the canonical's rounding; measured 4.7e-7"},
    "strategy_vega": {"atol": 1e-6, "rtol": 0.0, "why": "one 6 dp step, the canonical's rounding; measured 4.9e-7"},
}


def _option_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for spot, strike, ttm_days, volatility, rate, dividend, option_type in product(*GRID.values()):
        ttm_years = ttm_days / 365.0
        is_call = option_type == "call"
        case: dict[str, Any] = {
            "spot": spot,
            "strike": strike,
            "ttm_days": ttm_days,
            "ttm_years": ttm_years,
            "volatility": volatility,
            "rate": rate,
            "dividend": dividend,
            "option_type": option_type,
            "expected_price": bs_european_price(
                spot=spot,
                strike=strike,
                ttm_years=ttm_years,
                rate=rate,
                volatility=volatility,
                is_call=is_call,
                dividend=dividend,
            ),
        }
        # The browser copy takes no dividend yield (the page and the server both use q = 0),
        # so its Greeks are held where q = 0 only.
        if dividend == 0.0:
            greeks = black_scholes_greeks(
                spot=spot,
                strike=strike,
                ttm_years=ttm_years,
                volatility=volatility,
                rate=rate,
                dividend=dividend,
                is_call=is_call,
            )
            case["expected_greeks"] = {
                "delta": greeks.delta,
                "gamma": greeks.gamma,
                "theta": greeks.theta,
                "vega": greeks.vega,
                "rho": greeks.rho,
            }
        cases.append(case)
    return cases


def _strategy_case(spec: dict[str, Any]) -> dict[str, Any]:
    legs = [StrategyLeg.model_validate(leg) for leg in spec["legs"]]
    spot, rate = spec["spot"], spec["rate"]
    pct, points = spec["price_range_pct"], spec["curve_points"]
    ttm_years = spec["ttm_days"] / 365.0

    payoff = compute_payoff_curve(legs, spot, pct, points)
    pnl: dict[str, list[float]] = {}
    for scenario in SCENARIOS:
        shifted_years = max(ttm_years - scenario["time_shift_days"] / 365.0, 0.0)
        curve = compute_current_curve(legs, spot, pct, points, rate, shifted_years, iv_shift=scenario["iv_shift"])
        pnl[scenario["name"]] = [p.theoretical_pnl for p in curve]
    greek_curve = compute_greek_curves(legs, spot, pct, points, rate, ttm_years)

    return {
        "name": spec["name"],
        "spot": spot,
        "rate": rate,
        "ttm_days": spec["ttm_days"],
        "ttm_years": ttm_years,
        "legs": spec["legs"],
        "prices": [p.price for p in payoff],
        "expected": {
            "expiry_payoff": [p.pnl for p in payoff],
            "pnl": pnl,
            "greeks": {
                "delta": [p.delta for p in greek_curve],
                "gamma": [p.gamma for p in greek_curve],
                "theta": [p.theta for p in greek_curve],
                "vega": [p.vega for p in greek_curve],
            },
        },
    }


def build_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    """The fixture's ``(input, output)`` documents, deterministically ordered."""
    cases = _option_cases()
    document_input = {"grid": GRID, "scenarios": SCENARIOS, "strategies": STRATEGIES}
    document_output = {
        "schema_version": 2,
        "generated_by": "PythonDataService/scripts/fixture_generators/bs_browser_parity.py",
        "source": (
            "PythonDataService/app/services/bs_greeks.py (bs_european_price, black_scholes_greeks); "
            "PythonDataService/app/services/strategy_engine.py "
            "(compute_payoff_curve, compute_current_curve, compute_greek_curves)"
        ),
        "units": "per share; theta per calendar day, vega per 1 vol point, rho per 1 rate point",
        "tolerance": TOLERANCE,
        "scenarios": SCENARIOS,
        "n_cases": len(cases),
        "cases": cases,
        "strategies": [_strategy_case(spec) for spec in STRATEGIES],
    }
    return document_input, document_output


def _text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _attribution() -> str:
    return (
        f"# {FIXTURE_ID} — the Strategy Builder's browser Black-Scholes agrees with the Python canonical\n\n"
        "## Source\n\n"
        "Reference: `PythonDataService/app/services/bs_greeks.py` (closed-form Black-Scholes-Merton price and "
        "Greeks, Hull 10e §15.8 and §19) and `PythonDataService/app/services/strategy_engine.py` "
        "(`compute_payoff_curve`, `compute_current_curve`, `compute_greek_curves`: a strategy's expiry payoff, "
        "current-time P&L with its what-if shifts, and aggregate Greeks). Both are the canonical implementations "
        "(ADR 0068). The generator imports them; it carries no formula of its own.\n\n"
        "Subject under test: `Frontend/src/app/utils/black-scholes.ts` and `strategy-payoff.ts`, the browser copy "
        "ADR 0068 names as exception 1 (latency: the builder re-prices a 1,200-point grid on every leg edit).\n\n"
        "## Evidence layers\n\n"
        "1. Market input: synthetic. A 360-case single-option grid and five hand-picked strategies.\n"
        "2. Methodology: the closed-form formulas above. The canonical's own equivalence to independent "
        "implementations is pinned by BS-001..BS-007 (py_vollib) and "
        "`tests/services/test_bs_cross_engine_parity.py` (QuantLib, `atol=1e-10`).\n"
        "3. Oracle: `reference_kind=cross_engine`. The browser copy is checked against the Python canonical. "
        "`tests/fixtures/test_bs_browser_parity_fixture.py` re-evaluates the canonical on every row and "
        "requires this file to equal it, so the expected values cannot drift from the canonical.\n\n"
        "## Cases\n\n"
        "- `cases`: spot {80, 100, 120} x strike 100 x ttm {7, 30, 90, 180, 365} days x vol {0.10, 0.20, 0.40} x "
        "rate {0, 0.05} x dividend {0, 0.02} x call/put = 360. Each has the price; the 180 with dividend 0 also "
        "carry delta, gamma, theta, vega and rho (the browser copy has no dividend input; the page and the "
        "server both use q = 0).\n"
        "- `strategies`: a 95/105 bull call spread, a 95 call with no IV (priced at intrinsic by the server, "
        "#3221), a two-contract iron condor, a ratio call spread whose short leg has no IV, and a straddle "
        "under 10% vol (the IV-10% what-if floors its IVs at zero, #3221). Each is evaluated at the "
        "server's curve grid for the expiry payoff, the current-time P&L unshifted and under the page's three "
        "what-ifs (T+5d, IV+10%, IV-10%), and the aggregate delta, gamma, theta and vega.\n"
        "- Breakevens, max profit and max loss are pinned by "
        "`Frontend/src/testing/payoff-parity/cases.json` (#3186), not repeated here.\n\n"
        "## Tolerance\n\n"
        "Held per quantity, in `output.json`'s `tolerance` block, with the reason for each. The browser copy's "
        "error floor is its Abramowitz & Stegun 7.1.26 normal CDF (`|error| < 1.5e-7`); the canonical's "
        "`scipy.stats.norm.cdf` is exact to machine precision. Each `atol` sits between the largest difference "
        "measured over this fixture and the most the CDF's error can reach through that quantity: price "
        "`2e-5`, delta and rho `1e-7`, theta `2e-9`, gamma and vega `1e-12` (no CDF term). The strategy "
        "figures also carry the rounding of what the canonical returns (4 dp for payoff and P&L, 6 dp for "
        "aggregate Greeks), so P&L is held to one 4 dp step and strategy gamma, theta and vega to one 6 dp "
        "step.\n\n"
        "## Timestamps\n\n"
        "None. Time left is `ttm_days / 365` years, as `time_to_expiry_years` measures it.\n\n"
        "## Regeneration\n\n"
        f"Generated {GENERATED}.\n\n"
        f"`{REGENERATE_COMMAND}`\n\n"
        "The Frontend copy `Frontend/src/testing/bs-parity/grid.json` is `output.json` byte for byte.\n"
    )


def _manifest_entry() -> dict[str, Any]:
    return {
        "id": FIXTURE_ID,
        "name": "Strategy Builder browser Black-Scholes parity",
        "category": "options-pricing",
        "canonical_module": "PythonDataService/app/services/bs_greeks.py",
        "canonical_callable": "black_scholes_greeks",
        "reference": {
            "kind": "cross_engine",
            "oracle": (
                "the Python canonical (bs_greeks.py, strategy_engine.py) evaluated at generation time, "
                "against the browser copy Frontend/src/app/utils/black-scholes.ts"
            ),
            "citation": (
                "ADR 0068 exception 1; Hull, Options Futures and Other Derivatives (10e), §15.8 and §19. "
                "The canonical itself is pinned by BS-001..BS-007 and test_bs_cross_engine_parity.py."
            ),
        },
        "market_input": {
            "source": "synthetic",
            "vendor": None,
            "generated_by": "PythonDataService/scripts/fixture_generators/bs_browser_parity.py",
        },
        "units": {"theta": "per_day", "vega": "per_vol_point", "rho": "per_rate_point"},
        "tolerance": {
            "atol": 1e-9,
            "rtol": 0.0,
            "note": (
                "The canonical re-evaluated against the stored values: strict float, 1e-9. The browser copy is "
                "held looser, per quantity, to its Abramowitz & Stegun CDF (|error| < 1.5e-7) and to the "
                "canonical's own 4 dp / 6 dp rounding of strategy figures: price 2e-5, delta and rho 1e-7, "
                "theta 2e-9, gamma and vega 1e-12, strategy P&L 1e-4. Each is in output.json with its reason."
            ),
        },
        "active_version": 1,
        "versions": {},
        "status": "active",
        "validated_by": ["tests/fixtures/test_bs_browser_parity_fixture.py"],
    }


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    document_input, document_output = build_documents()
    input_path = FIXTURE_DIR / "input.json"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    input_path.write_text(_text(document_input), encoding="utf-8")
    output_path.write_text(_text(document_output), encoding="utf-8")
    attribution_path.write_text(_attribution(), encoding="utf-8")
    FRONTEND_COPY.parent.mkdir(parents=True, exist_ok=True)
    FRONTEND_COPY.write_bytes(output_path.read_bytes())

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures: list[dict[str, Any]] = manifest["fixtures"]
    entry = _manifest_entry()
    position = next((i for i, item in enumerate(fixtures) if item["id"] == FIXTURE_ID), len(fixtures))
    fixtures[position : position + 1] = [entry]
    content_hashes, file_hashes = compute_hashes(FIXTURE_DIR, [input_path.name, output_path.name])
    file_hashes[attribution_path.name] = file_sha256(attribution_path)
    entry["versions"]["1"] = {
        "input": "input.json",
        "output": "output.json",
        "attribution": "attribution.md",
        "generated": GENERATED,
        "justification": (
            "Initial fixture. Replaces Frontend/scripts/generate-bs-parity-fixture.py, which carried its own "
            "inline Black-Scholes copy; this one imports bs_greeks.py and strategy_engine.py, and adds Greeks, "
            "strategy P&L, what-if shifts and the expiry payoff to the 360-case price grid (#3221)."
        ),
        "content_sha256": content_hashes,
        "file_sha256": file_hashes,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
