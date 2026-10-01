"""Every Python surface prices an omitted risk-free rate at the one default (#2764).

The .NET Backend and the Angular pages no longer send a rate of their own;
they omit it and let Python fill it. Each test prices the same input three
times: with the rate omitted, with ``DEFAULT_RISK_FREE_RATE`` passed
explicitly, and with a different rate. The first two must agree exactly. The
third must differ, which proves the output depends on the rate, so the
agreement is not vacuous.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.models.portfolio import ScenarioRequest
from app.models.requests import OptionsCompanionConfig
from app.research.options.contract_finder import _find_otm_call_by_delta, _find_otm_put_by_delta
from app.research.options.iv_builder import _derive_iv_for_contract
from app.routers import quantlib_options
from app.services import fred_service
from app.services.bs_greeks import bs_european_price
from app.services.options_companion_service import _compute_row_greeks
from app.services.portfolio_scenario import evaluate_scenario
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.volatility.analytics import compute_put_call_parity_forward
from app.volatility.conventions import SurfaceConventions
from app.volatility.solver import implied_volatility, solve_iv_chain
from app.volatility.surface import VolSurfaceBuilder

OTHER_RATE = 0.10
# Large enough to move a 25-delta strike pick across the dense strike grid below.
OTHER_RATE_FOR_STRIKE_PICK = 0.50


def _priced_chain(rate: float) -> list[dict[str, Any]]:
    """Two expiries of calls and puts priced at a flat 25 % vol and ``rate``."""
    records: list[dict[str, Any]] = []
    for ttm in (30 / 365, 90 / 365):
        for strike in (90.0, 95.0, 97.5, 100.0, 102.5, 105.0, 110.0):
            for is_call in (True, False):
                price = bs_european_price(100.0, strike, ttm, rate, 0.25, is_call=is_call)
                records.append({"strike": strike, "ttm": ttm, "option_price": price, "is_call": is_call})
    return records


_CHAIN = _priced_chain(0.05)
_STRIKES_EVERY_HALF_DOLLAR = [80.0 + 0.5 * i for i in range(81)]


def _contracts(contract_type: str) -> list[dict[str, Any]]:
    return [
        {"strike_price": k, "contract_type": contract_type, "ticker": f"O:TEST{k}"} for k in _STRIKES_EVERY_HALF_DOLLAR
    ]


def _scenario(**rate: float) -> dict[str, Any]:
    request = ScenarioRequest.model_validate(
        {
            "as_of_ms": 1_767_225_600_000,
            "spot_price": 615.0,
            "positions": [
                {
                    "instrument": "option",
                    "symbol": "SPY",
                    "option_type": "call",
                    "strike": 620.0,
                    "expiration_ms": 1_769_817_600_000,
                    "quantity": 1.0,
                    "multiplier": 100.0,
                    "entry_price": 4.5,
                    "current_iv": 0.22,
                }
            ],
            **rate,
        }
    )
    return evaluate_scenario(request).model_dump()


# (surface, rate keyword, other rate, call with the given rate keyword arguments)
_HELPERS: list[tuple[str, str, float, Callable[..., Any]]] = [
    (
        "implied_volatility",
        "rate",
        OTHER_RATE,
        lambda **kw: implied_volatility(option_price=4.0, spot=100.0, strike=100.0, ttm=0.25, **kw).iv,
    ),
    (
        "solve_iv_chain",
        "rate",
        OTHER_RATE,
        lambda **kw: [row["iv"] for row in solve_iv_chain(_CHAIN, spot=100.0, **kw)],
    ),
    (
        "VolSurfaceBuilder",
        "rate",
        OTHER_RATE,
        lambda **kw: VolSurfaceBuilder(spot=100.0, **kw).build(_CHAIN).volatility(strike=100.0, ttm=60 / 365),
    ),
    (
        "SurfaceConventions",
        "rate",
        OTHER_RATE,
        lambda **kw: (SurfaceConventions(**kw).forward(100.0, 0.25), SurfaceConventions(**kw).discount_factor(0.25)),
    ),
    (
        "compute_put_call_parity_forward",
        "rate",
        OTHER_RATE,
        lambda **kw: compute_put_call_parity_forward(_CHAIN, **kw),
    ),
    (
        "contract_finder._find_otm_put_by_delta",
        "rfr",
        OTHER_RATE_FOR_STRIKE_PICK,
        lambda **kw: _find_otm_put_by_delta(_contracts("put"), stock_close=100.0, dte_days=30, **kw),
    ),
    (
        "contract_finder._find_otm_call_by_delta",
        "rfr",
        OTHER_RATE_FOR_STRIKE_PICK,
        lambda **kw: _find_otm_call_by_delta(_contracts("call"), stock_close=100.0, dte_days=30, **kw),
    ),
    (
        "portfolio.ScenarioRequest",
        "risk_free_rate",
        OTHER_RATE,
        _scenario,
    ),
    (
        "iv_builder._derive_iv_for_contract",
        "risk_free_rate",
        OTHER_RATE,
        lambda **kw: _derive_iv_for_contract({"vw": 4.0}, "O:TEST260116C00100000", 100.0, 60, "call", **kw),
    ),
]


@pytest.mark.parametrize(
    ("rate_keyword", "other_rate", "call"),
    [pytest.param(keyword, other, call, id=name) for name, keyword, other, call in _HELPERS],
)
def test_helper_prices_an_omitted_rate_at_the_default(
    rate_keyword: str, other_rate: float, call: Callable[..., Any]
) -> None:
    omitted = call()

    assert omitted == call(**{rate_keyword: DEFAULT_RISK_FREE_RATE})
    assert omitted != call(**{rate_keyword: other_rate})


def test_options_companion_solves_an_omitted_rate_at_the_default() -> None:
    """The Data Lab export omits an empty rate field; the per-bar IV/Greeks solve uses the default."""
    bar_ts = 1_749_562_200_000  # 2025-06-10 09:30 ET
    expiry_close = bar_ts + 30 * 24 * 60 * 60 * 1000

    def solve(config: OptionsCompanionConfig) -> dict[str, float | None]:
        values, status = _compute_row_greeks(
            option_close=12.0,
            underlying_spot=710.0,
            strike=705.0,
            bar_ts_ms=bar_ts,
            expiry_close_ms=expiry_close,
            is_call=True,
            config=config,
        )
        assert status == "ok"
        return values

    omitted = solve(OptionsCompanionConfig.model_validate({"enabled": True, "include_rho": True}))

    assert omitted == solve(
        OptionsCompanionConfig(enabled=True, include_rho=True, risk_free_rate=DEFAULT_RISK_FREE_RATE)
    )
    assert omitted != solve(OptionsCompanionConfig(enabled=True, include_rho=True, risk_free_rate=OTHER_RATE))


def test_fred_lookup_without_data_returns_the_default() -> None:
    """A failed FRED lookup and an omitted rate price at the same number."""
    fred_service.clear_cache()
    try:
        with patch.object(fred_service, "_fetch_all_tenors", return_value={}):
            assert fred_service.get_risk_free_rate(dte_days=30, observation_date="2025-01-15") == DEFAULT_RISK_FREE_RATE
    finally:
        fred_service.clear_cache()


async def _post_three_ways(
    client: AsyncClient, path: str, payload: dict[str, Any], other_rate: float
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    responses = []
    for extra in ({}, {"risk_free_rate": DEFAULT_RISK_FREE_RATE}, {"risk_free_rate": other_rate}):
        response = await client.post(path, json={**payload, **extra})
        assert response.status_code == 200, response.text
        responses.append(response.json())
    omitted, explicit_default, other = responses
    return omitted, explicit_default, other


async def test_strategy_analyze_prices_an_omitted_rate_at_the_default(client: AsyncClient) -> None:
    expiration = (datetime.now(UTC).date() + timedelta(days=90)).isoformat()
    payload = {
        "symbol": "AAPL",
        "legs": [
            {"strike": 100, "option_type": "call", "position": "long", "premium": 5.0, "iv": 0.25, "quantity": 1},
            {"strike": 105, "option_type": "call", "position": "short", "premium": 2.0, "iv": 0.23, "quantity": 1},
        ],
        "expiration_date": expiration,
        "spot_price": 102,
        "include_current_curve": True,
    }

    omitted, explicit_default, other = await _post_three_ways(client, "/api/strategy/analyze", payload, OTHER_RATE)

    assert omitted["success"] is True
    assert omitted == explicit_default
    assert omitted["pop"] != other["pop"]


async def test_quantlib_price_prices_an_omitted_rate_at_the_default(client: AsyncClient) -> None:
    payload = {
        "spot": 100.0,
        "strike": 100.0,
        "volatility": 0.25,
        "expiration_date": "2025-04-02",
        "evaluation_date": "2025-01-02",
        "option_type": "call",
    }

    omitted, explicit_default, other = await _post_three_ways(client, "/api/quantlib/price", payload, OTHER_RATE)

    assert omitted["success"] is True
    assert omitted == explicit_default
    assert not math.isclose(omitted["price"], other["price"], rel_tol=0.0, abs_tol=1e-9)


async def test_pricing_compare_prices_and_echoes_an_omitted_rate_at_the_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The echoed rate is what Pricing Lab prices its in-browser overlay at."""
    # The Python BS curve alone proves the rate; the six QuantLib engines only add wall time.
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", False)
    payload = {
        "spot": 100.0,
        "strike": 100.0,
        "volatility": 0.25,
        "expiration_date": "2025-04-02",
        "evaluation_date": "2025-01-02",
        "option_type": "put",
        "num_points": 10,
    }

    omitted, explicit_default, other = await _post_three_ways(client, "/api/quantlib/compare", payload, OTHER_RATE)

    assert omitted["success"] is True
    assert omitted["risk_free_rate"] == DEFAULT_RISK_FREE_RATE
    assert omitted == explicit_default
    assert other["risk_free_rate"] == OTHER_RATE
    assert omitted["models"] != other["models"]
