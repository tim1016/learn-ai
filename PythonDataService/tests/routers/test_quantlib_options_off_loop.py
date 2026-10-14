"""QuantLib pricing runs in a worker thread, not on the event loop (#3187).

``/api/quantlib/price`` and ``/api/quantlib/compare`` priced inside their ``async def`` handlers, so one Pricing Lab
click (``/compare`` is fired on every contract pick) held the whole data-plane process for as long as QuantLib ran:
every other request, and the live market-data feed that lives in the same process, waited for it.

The pricing call is replaced here by one that stands in for a long QuantLib call: it does not finish until the
event loop has served another request. That is only possible if the loop is free while it runs. On the loop's own
thread nothing can serve that request, the call gives up after ``PRICING_PATIENCE_S``, and the test fails.

A real QuantLib C++ call holds the interpreter lock for its whole length, so moving it to a thread frees the loop
between calls and not during one. What this pins is the move itself; the numbers are the pricer's, unchanged.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

from app.options.expiry import option_expiry_anchor_ms
from app.routers import quantlib_options
from app.services.quantlib_pricer import GreeksResult

# Long enough that a loaded CI runner serves the other request inside it, short enough that a regression fails fast.
PRICING_PATIENCE_S = 3.0

PRICE_PAYLOAD: dict[str, Any] = {
    "spot": 100.0,
    "strike": 100.0,
    "volatility": 0.25,
    "expiration_ms": option_expiry_anchor_ms(date(2099, 4, 2)),
    "evaluation_date": "2099-01-02",
    "option_type": "call",
    "engine": "monte_carlo",
}
COMPARE_PAYLOAD: dict[str, Any] = {
    "spot": 100.0,
    "strike": 100.0,
    "volatility": 0.25,
    "expiration_ms": option_expiry_anchor_ms(date(2099, 4, 2)),
    "evaluation_date": "2099-01-02",
    "option_type": "call",
    "num_points": 10,
}


class SlowPricing:
    """A pricing call whose first call waits for the event loop to serve another request, and says whether it did."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.other_request_served = threading.Event()
        self.calls = 0
        self.loop_was_free: bool | None = None

    def result(self, *, spot: float) -> GreeksResult:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.loop_was_free = self.other_request_served.wait(timeout=PRICING_PATIENCE_S)
        return GreeksResult(
            engine="monte_carlo", price=spot / 10, delta=0.5, gamma=0.01, theta=-0.02, vega=0.1, rho=0.05
        )


async def _health_while(pricing: asyncio.Task[Any], slow: SlowPricing, client: AsyncClient) -> None:
    """Ask the data plane something trivial once the pricing call is running, then let the pricing go."""
    while not slow.started.is_set():
        await asyncio.sleep(0.01)
    response = await client.get("/health")
    assert response.status_code == 200
    slow.other_request_served.set()
    await pricing


async def test_price_does_not_hold_the_event_loop_while_quantlib_prices(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowPricing()
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", True)
    monkeypatch.setattr(quantlib_options, "price_option", lambda **kwargs: slow.result(spot=kwargs["spot"]))
    pricing = asyncio.create_task(client.post("/api/quantlib/price", json=PRICE_PAYLOAD))

    await _health_while(pricing, slow, client)

    assert slow.loop_was_free is True, "the event loop was held for the whole of the pricing call"
    response = pricing.result()
    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "engine": "monte_carlo",
        "price": 10.0,
        "delta": 0.5,
        "gamma": 0.01,
        "theta": -0.02,
        "vega": 0.1,
        "rho": 0.05,
        "d1": None,
        "d2": None,
        "error": None,
    }


async def test_compare_does_not_hold_the_event_loop_while_quantlib_prices(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowPricing()
    monkeypatch.setattr(quantlib_options, "_QL_AVAILABLE", True)
    monkeypatch.setattr(
        quantlib_options, "_price_option_for_compare", lambda **kwargs: slow.result(spot=kwargs["spot"])
    )
    pricing = asyncio.create_task(client.post("/api/quantlib/compare", json=COMPARE_PAYLOAD))

    await _health_while(pricing, slow, client)

    assert slow.loop_was_free is True, "the event loop was held for the whole of the pricing call"
    response = pricing.result()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert [model["model"] for model in body["models"]] == [
        "python_bs",
        "quantlib_bs",
        "quantlib_crr",
        "quantlib_jr",
        "quantlib_lr",
        "quantlib_fd",
        "quantlib_mc",
    ]
    # Six QuantLib engines, every point of each.
    assert slow.calls == 6 * COMPARE_PAYLOAD["num_points"]
