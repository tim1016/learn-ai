"""Pricing runs outside the service interpreter, including a C call that holds its GIL (#3212)."""

from __future__ import annotations

import asyncio
import ctypes
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.options.expiry import option_expiry_anchor_ms
from app.routers import quantlib_options
from app.services.quantlib_pricer import GreeksResult

PRICE_PAYLOAD: dict[str, Any] = {
    "spot": 100.0,
    "strike": 100.0,
    "volatility": 0.25,
    "expiration_ms": option_expiry_anchor_ms(date(2099, 4, 2)),
    "evaluation_date": "2099-01-02",
    "option_type": "call",
    "engine": "monte_carlo",
}
COMPARE_PAYLOAD: dict[str, Any] = {**PRICE_PAYLOAD, "num_points": 10}


def process_identity_price(**inputs: Any) -> GreeksResult:
    """Return the worker identity through the same picklable result as a price."""
    return GreeksResult(
        engine="monte_carlo", price=float(os.getpid()), delta=0.5, gamma=0.01, theta=-0.02, vega=0.1, rho=0.05
    )


@pytest.mark.parametrize("route", ["price", "compare"])
async def test_quantlib_runs_in_another_process(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", True)
    monkeypatch.setattr(quantlib_options, "price_option", process_identity_price)
    monkeypatch.setattr(quantlib_options, "_price_option_for_compare", process_identity_price)

    response = await client.post(
        f"/api/quantlib/{route}", json=PRICE_PAYLOAD if route == "price" else COMPARE_PAYLOAD
    )

    body = response.json()
    assert body["success"] is True, body
    prices = [body["price"]] if route == "price" else [
        point["price"] for model in body["models"] if model["model"].startswith("quantlib_") for point in model["points"]
    ]
    assert prices and all(price != os.getpid() for price in prices), "a pricing thread shares the service's interpreter"


def gil_held_price(**inputs: Any) -> GreeksResult:
    """PyDLL deliberately retains the GIL across a C call, as QuantLib does."""
    marker = Path(os.environ["QUANTLIB_TEST_WINDOW"])
    marker.write_text("pricing", encoding="utf-8")
    ctypes.PyDLL(None).usleep(2_000_000)
    marker.write_text("finished", encoding="utf-8")
    return process_identity_price(**inputs)


async def test_health_is_served_during_a_c_pricing_call_that_holds_its_interpreters_lock(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    marker = tmp_path / "pricing-window"
    monkeypatch.setenv("QUANTLIB_TEST_WINDOW", str(marker))
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", True)
    monkeypatch.setattr(quantlib_options, "price_option", gil_held_price)
    pricing = asyncio.create_task(client.post("/api/quantlib/price", json=PRICE_PAYLOAD))
    try:
        async with asyncio.timeout(15):
            while not marker.exists() and not pricing.done():
                await asyncio.sleep(0.01)
            response = await client.get("/health")
            assert response.status_code == 200
            assert marker.read_text(encoding="utf-8") == "pricing", "the service waited for the C call to finish"
            priced = await pricing
            assert priced.json()["success"] is True, priced.text
    finally:
        pricing.cancel()
        await asyncio.gather(pricing, return_exceptions=True)
