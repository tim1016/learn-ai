"""Regression for #3188: ``POST /api/options/expirations`` must not echo the Polygon key.

The window fetch sends ``apiKey`` as a query parameter, so ``requests``' own
``HTTPError`` text -- which names the full request URL -- carried the key into
the router's 500 ``detail`` and into the log. The vendor call here is
``requests``, which ``respx`` cannot intercept, so the vendor answer is mocked
with ``responses`` (the repo's requests-level mock).

The key below is made up; the test never touches a real one.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest
import responses
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.options.expiry import option_expiry_anchor_ms

FAKE_KEY = "fake-polygon-key-3188-not-a-real-credential"
VENDOR_CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"


@pytest.fixture
def fake_polygon_key(monkeypatch):
    monkeypatch.setattr(settings, "POLYGON_API_KEY", FAKE_KEY)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.parametrize(
    ("vendor_status", "vendor_reason"),
    [(401, "Unauthorized"), (403, "Forbidden"), (429, "Too Many Requests")],
)
@responses.activate
async def test_vendor_4xx_keeps_status_and_reason_but_not_the_key(
    client, fake_polygon_key, caplog, vendor_status, vendor_reason
):
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        status=vendor_status,
        json={"status": "ERROR", "error": "plan does not include this request"},
    )
    # A one-day range is one window, so the vendor is asked exactly once.
    anchor = option_expiry_anchor_ms(date(2030, 1, 18))

    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            "/api/options/expirations",
            json={
                "underlying_ticker": "SPY",
                "contract_type": "call",
                "expiration_ms_gte": anchor,
                "expiration_ms_lte": anchor,
            },
        )

    assert len(responses.calls) == 1
    # The vendor really was sent the key (it is the request's own parameter) ...
    assert FAKE_KEY in responses.calls[0].request.url

    # ... and the answer still says what the vendor said, as a 500.
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert str(vendor_status) in detail
    assert vendor_reason in detail

    # Neither the key nor its query parameter leaves the process: not in the
    # response, not in anything the router or the client logged.
    for surface in (resp.text, caplog.text):
        assert FAKE_KEY not in surface
        assert "apiKey=" not in surface
    # The log line stays useful: it names the failed window and the vendor status.
    assert any(str(vendor_status) in r.getMessage() for r in caplog.records)
