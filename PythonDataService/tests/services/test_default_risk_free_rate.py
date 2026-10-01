"""Every request model and endpoint prices an omitted risk-free rate at the one default (#2764).

The .NET Backend and the Angular pages no longer send a rate of their own;
they omit it and let Python fill it. Each test prices the same input three
times: with the rate omitted, with ``DEFAULT_RISK_FREE_RATE`` passed
explicitly, and with a different rate. The first two must agree exactly. The
third must differ, which proves the output depends on the rate, so the
agreement is not vacuous.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.models.requests import OptionsCompanionConfig
from app.routers import quantlib_options
from app.services.options_companion_service import _compute_row_greeks
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE

OTHER_RATE = 0.10


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
