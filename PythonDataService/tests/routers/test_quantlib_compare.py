"""``/api/quantlib/compare`` and ``/price``: request bounds, the python_bs curve, the New York date, lattice Greeks (#3221).

Five things Pricing Lab used to get wrong without saying so:

* a spot range of zero, a negative bound or a descending range answered 200 (a descending curve, or two surviving
  models after QuantLib raised on the rest), and a rate of 5 (meaning 5%) priced at 500%;
* the ``python_bs`` curve ignored the dividend yield, so against ``quantlib_bs`` it disagreed by the dividend alone;
* "today" was the container's UTC date, so after 20:00 ET an option expiring the next New York date read as expired;
* the binomial and finite-difference engines reported ``vega`` and ``rho`` as a flat 0.0;
* an engine that raised, or QuantLib being absent, dropped models from the answer with only a log line, and Monte
  Carlo's ``theta``, ``vega`` and ``rho`` read as a flat 0.0 where the engine provides none.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from app.options.expiry import option_expiry_anchor_ms
from app.routers import quantlib_options
from app.services import quantlib_pricer
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.services.quantlib_pricer import PricingEngine, implied_volatility, price_option
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.timestamps import to_ms_utc

EVALUATION_DATE = date(2026, 1, 2)
EXPIRY_DATE = date(2026, 4, 2)
# /compare counts whole days over 365, whatever the engine's own day count.
TTM_YEARS = (EXPIRY_DATE - EVALUATION_DATE).days / 365.0
STRIKE = 100.0
VOLATILITY = 0.25

COMPARE_PAYLOAD: dict[str, Any] = {
    "spot": 100.0,
    "strike": STRIKE,
    "volatility": VOLATILITY,
    "expiration_ms": option_expiry_anchor_ms(EXPIRY_DATE),
    "evaluation_date": EVALUATION_DATE.isoformat(),
    "option_type": "call",
    "num_points": 10,
    "spot_min": 80.0,
    "spot_max": 120.0,
}


async def _compare(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/quantlib/compare", json={**COMPARE_PAYLOAD, **overrides})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    return body


def _curve(body: dict[str, Any], model: str) -> list[dict[str, float]]:
    return next(m["points"] for m in body["models"] if m["model"] == model)


# ── The request is refused when it cannot describe a curve ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"spot_min": 0.0, "spot_max": 120.0}, "zero start"),
        ({"spot_min": -50.0, "spot_max": 50.0}, "negative start"),
        ({"spot_min": 120.0, "spot_max": 80.0}, "descending"),
        ({"spot_min": 100.0, "spot_max": 100.0}, "zero width, what an empty Range % sends"),
        ({"spot_min": 150.0, "spot_max": None}, "start above the default end (spot x 1.20)"),
        ({"spot_min": None, "spot_max": 70.0}, "end below the default start (spot x 0.80)"),
        ({"spot_min": 0.00001, "spot_max": 120.0}, "start that rounds to a spot of 0 at 4 decimals"),
    ],
)
async def test_compare_refuses_a_spot_range_that_is_not_an_upward_range(
    client: AsyncClient, overrides: dict[str, Any], reason: str
) -> None:
    payload = {**COMPARE_PAYLOAD, **overrides}
    payload = {k: v for k, v in payload.items() if v is not None}

    response = await client.post("/api/quantlib/compare", json=payload)

    assert response.status_code == 422, f"{reason}: {response.text}"


@pytest.mark.parametrize("path", ["/api/quantlib/compare", "/api/quantlib/price"])
@pytest.mark.parametrize("rate", [-0.01, 0.51, 5.0])
async def test_a_risk_free_rate_outside_zero_to_half_is_refused(client: AsyncClient, path: str, rate: float) -> None:
    response = await client.post(path, json={**COMPARE_PAYLOAD, "risk_free_rate": rate})

    assert response.status_code == 422, response.text


async def test_compare_keeps_an_upward_range_ending_where_the_request_says(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", False)

    body = await _compare(client, spot_min=90.0, spot_max=110.0)

    spots = [p["spot"] for p in _curve(body, "python_bs")]
    assert spots[0] == 90.0
    assert spots[-1] == 110.0
    assert spots == sorted(set(spots))


# ── python_bs is the canonical closed form, dividend included ───────────────────────────────────────────────


@pytest.mark.parametrize("option_type", ["call", "put"])
async def test_python_bs_curve_is_the_canonical_closed_form_and_moves_with_the_dividend(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, option_type: str
) -> None:
    # The closed-form curve alone; the QuantLib engines add wall time, not information.
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", False)

    with_dividend = await _compare(client, option_type=option_type, dividend_yield=0.03)
    without_dividend = await _compare(client, option_type=option_type, dividend_yield=0.0)

    for point in _curve(with_dividend, "python_bs"):
        is_call = option_type == "call"
        greeks = black_scholes_greeks(
            spot=point["spot"],
            strike=STRIKE,
            ttm_years=TTM_YEARS,
            volatility=VOLATILITY,
            rate=DEFAULT_RISK_FREE_RATE,
            dividend=0.03,
            is_call=is_call,
        )
        price = bs_european_price(
            point["spot"], STRIKE, TTM_YEARS, DEFAULT_RISK_FREE_RATE, VOLATILITY, is_call=is_call, dividend=0.03
        )
        assert point == {
            "spot": point["spot"],
            "price": round(price, 6),
            "delta": round(greeks.delta, 6),
            "gamma": round(greeks.gamma, 6),
            "theta": round(greeks.theta, 6),
            "vega": round(greeks.vega, 6),
            "rho": round(greeks.rho, 6),
        }
    assert _curve(with_dividend, "python_bs") != _curve(without_dividend, "python_bs")


async def test_python_bs_agrees_with_quantlib_bs_at_a_dividend_yield(client: AsyncClient) -> None:
    pytest.importorskip("QuantLib")

    body = await _compare(client, dividend_yield=0.03)

    # Same closed form to 1e-10; each value is rounded to 6 decimals by the response, so two roundings.
    for python_point, quantlib_point in zip(_curve(body, "python_bs"), _curve(body, "quantlib_bs"), strict=True):
        assert python_point["spot"] == quantlib_point["spot"]
        for field in ("price", "delta", "gamma", "theta", "vega", "rho"):
            assert python_point[field] == pytest.approx(quantlib_point[field], abs=2e-6, rel=0), field


# ── The binomial and finite-difference engines report vega and rho ─────────────────────────────────────────

# Each bound is the worst |engine - closed form| measured over the 360-case parity grid (spot 80/100/120, 7-365 days,
# vol 10-40%, rate 0-5%, dividend 0-2%, call and put) on the 201-step tree / 201x200 grid /compare prices at, rounded
# up with 2x-12x headroom (measured worst: CRR vega 7.95e-3 rho 8.1e-4; JR vega 1.29e-2 rho 2.55e-2; LR vega 6.1e-4
# rho 8.3e-7; FD vega 6.8e-4 rho 2.0e-4). The trees' own discretisation error is what is left: Jarrow-Rudd's is the
# largest, Leisen-Reimer's the smallest. A bump of the engine's own price is that engine's sensitivity, so the error
# is the engine's, not the bump's. Any plausible bump bug (scale, sign, a missing factor) misses by 0.1 or more.
LATTICE_GREEK_ATOL = {
    "quantlib_crr": {"vega": 2e-2, "rho": 2e-3},
    "quantlib_jr": {"vega": 3e-2, "rho": 5e-2},
    "quantlib_lr": {"vega": 2e-3, "rho": 1e-5},
    "quantlib_fd": {"vega": 2e-3, "rho": 1e-3},
}


@pytest.mark.parametrize("option_type", ["call", "put"])
async def test_lattice_engines_report_vega_and_rho_near_closed_form(client: AsyncClient, option_type: str) -> None:
    pytest.importorskip("QuantLib")

    body = await _compare(client, option_type=option_type, dividend_yield=0.02, risk_free_rate=0.05)

    reference = _curve(body, "python_bs")
    for model, atol in LATTICE_GREEK_ATOL.items():
        points = _curve(body, model)
        assert len(points) == len(reference)
        for greek, tolerance in atol.items():
            values = [p[greek] for p in points]
            assert any(v != 0.0 for v in values), f"{model} {greek} is a flat zero"
            for point, closed_form in zip(points, reference, strict=True):
                assert point[greek] == pytest.approx(closed_form[greek], abs=tolerance, rel=0), (model, greek)


# ── Which engines ran, and which Greeks an engine does not provide ─────────────────────────────────────────

QUANTLIB_MODELS = ["quantlib_bs", "quantlib_crr", "quantlib_jr", "quantlib_lr", "quantlib_fd", "quantlib_mc"]
# What a real engine failure can put in its text: a request URL with a key, and a traceback.
HOSTILE_ENGINE_TEXT = (
    "negative probability while pricing https://api.polygon.io/v3?apiKey=SECRET-KEY-123\n"
    'Traceback (most recent call last):\n  File "/srv/app/pricer.py", line 9, in price\n'
)


def _fake_price(*, failing: PricingEngine | None = None, **inputs: Any) -> quantlib_options._ComparePoint:
    """Picklable engine answer, including the engine's failure across the process boundary."""
    engine = inputs["engine"]
    if engine is failing:
        raise RuntimeError(HOSTILE_ENGINE_TEXT)
    provided = engine is not PricingEngine.MONTE_CARLO
    return quantlib_options._ComparePoint(
        price=1.0,
        delta=0.5,
        gamma=0.01,
        theta=-0.02 if provided else None,
        vega=0.1 if provided else None,
        rho=0.03 if provided else None,
    )


def _fake_engines(monkeypatch: pytest.MonkeyPatch, *, failing: PricingEngine | None = None) -> None:
    """Every QuantLib engine prices instantly, Monte Carlo without theta/vega/rho; `failing` raises instead."""

    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", True)
    monkeypatch.setattr(quantlib_options, "_price_option_for_compare", partial(_fake_price, failing=failing))


async def test_an_engine_that_raises_is_named_with_a_reason_and_the_others_still_answer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_engines(monkeypatch, failing=PricingEngine.BINOMIAL_CRR)

    body = await _compare(client)

    assert [m["model"] for m in body["models"]] == ["python_bs", *(m for m in QUANTLIB_MODELS if m != "quantlib_crr")]
    assert [u["model"] for u in body["unavailable_models"]] == ["quantlib_crr"]
    reason = body["unavailable_models"][0]["reason"]
    assert "RuntimeError" in reason
    # The engine's own text stays in the service log: neither the key nor the traceback reaches the reply.
    assert "SECRET-KEY-123" not in json.dumps(body)
    assert "Traceback" not in json.dumps(body)
    assert "\n" not in reason


async def test_with_quantlib_absent_every_quantlib_model_is_named_as_not_run(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", False)

    body = await _compare(client)

    assert [m["model"] for m in body["models"]] == ["python_bs"]
    assert [u["model"] for u in body["unavailable_models"]] == QUANTLIB_MODELS
    assert all("QuantLib is not installed" in u["reason"] for u in body["unavailable_models"])


async def test_when_every_engine_runs_nothing_is_listed_unavailable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_engines(monkeypatch)

    body = await _compare(client)

    assert [m["model"] for m in body["models"]] == ["python_bs", *QUANTLIB_MODELS]
    assert body["unavailable_models"] == []


async def test_monte_carlo_theta_vega_and_rho_are_null_not_zero_and_no_other_engine_loses_a_greek(
    client: AsyncClient,
) -> None:
    pytest.importorskip("QuantLib")

    body = await _compare(client)

    assert body["unavailable_models"] == []
    for point in _curve(body, "quantlib_mc"):
        assert point["delta"] is not None
        assert point["gamma"] is not None
        assert (point["theta"], point["vega"], point["rho"]) == (None, None, None)
    for model in (m for m in QUANTLIB_MODELS if m != "quantlib_mc"):
        for point in _curve(body, model):
            assert None not in point.values(), model


# ── "Today" is the New York date ────────────────────────────────────────────────────────────────────────────

NEW_YORK = ZoneInfo("America/New_York")
# 21:00 on 2027-06-15 in New York is 01:00 on 2027-06-16 UTC, so a UTC "today" is already the expiry date.
NINE_PM_ET_MS = to_ms_utc(datetime(2027, 6, 15, 21, 0, tzinfo=NEW_YORK))
NEXT_ET_DATE = date(2027, 6, 16)
ONE_DAY_YEARS = 1 / 365.0


async def test_compare_after_8pm_new_york_prices_an_option_expiring_the_next_new_york_date(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quantlib_options, "now_ms_utc", lambda: NINE_PM_ET_MS)
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", False)
    payload = {k: v for k, v in COMPARE_PAYLOAD.items() if k != "evaluation_date"}
    payload["expiration_ms"] = option_expiry_anchor_ms(NEXT_ET_DATE)

    response = await client.post("/api/quantlib/compare", json=payload)

    body = response.json()
    assert body["success"] is True, body
    assert body["time_to_expiry_years"] == round(ONE_DAY_YEARS, 6)


def test_price_after_8pm_new_york_defaults_the_pricing_date_to_the_new_york_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("QuantLib")
    monkeypatch.setattr(quantlib_pricer, "now_ms_utc", lambda: NINE_PM_ET_MS)

    result = price_option(
        spot=100.0,
        strike=100.0,
        risk_free_rate=0.043,
        volatility=VOLATILITY,
        expiration_date=NEXT_ET_DATE,
        option_type="call",
        engine=PricingEngine.ANALYTIC_BS,
    )

    expected = bs_european_price(100.0, 100.0, ONE_DAY_YEARS, 0.043, VOLATILITY, is_call=True)
    assert result.price == pytest.approx(expected, abs=1e-10, rel=0)
    assert result.price > 0.0


def test_implied_volatility_after_8pm_new_york_defaults_the_pricing_date_to_the_new_york_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("QuantLib")
    monkeypatch.setattr(quantlib_pricer, "now_ms_utc", lambda: NINE_PM_ET_MS)
    market_price = bs_european_price(100.0, 100.0, ONE_DAY_YEARS, 0.043, VOLATILITY, is_call=True)

    solved = implied_volatility(
        market_price=market_price,
        spot=100.0,
        strike=100.0,
        risk_free_rate=0.043,
        expiration_date=NEXT_ET_DATE,
        option_type="call",
    )

    assert solved == pytest.approx(VOLATILITY, abs=1e-4, rel=0)
