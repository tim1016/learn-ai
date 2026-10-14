"""``POST /api/options/expirations``: a range the route can walk, a complete list, a loud outage (#3221).

The vendor call is ``requests``, which ``respx`` cannot intercept, so the vendor
is mocked with ``responses`` (the repo's requests-level mock, as in
``test_options_expirations_key_not_echoed.py``). The key below is made up.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses
from httpx import ASGITransport, AsyncClient
from responses import matchers

from app.config import settings
from app.main import app
from app.options.expiry import option_expiry_anchor_ms
from app.services.polygon_client import MAX_EXPIRATIONS_WORKERS, PolygonClientService
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

FAKE_KEY = "fake-polygon-key-3221-not-a-real-credential"
VENDOR_CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"


@pytest.fixture
def fake_polygon_key(monkeypatch):
    monkeypatch.setattr(settings, "POLYGON_API_KEY", FAKE_KEY)


@pytest.fixture
def no_backoff(monkeypatch):
    """The client backs off 2 + 4 + 8 s between attempts; a test does not wait for it."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _post(client: AsyncClient, **bounds: date | None):
    body: dict[str, object] = {"underlying_ticker": "SPY", "contract_type": "call"}
    if bounds.get("gte") is not None:
        body["expiration_ms_gte"] = option_expiry_anchor_ms(bounds["gte"])
    if bounds.get("lte") is not None:
        body["expiration_ms_lte"] = option_expiry_anchor_ms(bounds["lte"])
    return await client.post("/api/options/expirations", json=body)


def _vendor_window_start(request: requests.PreparedRequest) -> str:
    return parse_qs(urlparse(request.url).query)["expiration_date.gte"][0]


@pytest.mark.parametrize(
    ("gte", "lte"),
    [
        pytest.param(date(2030, 2, 15), date(2030, 1, 18), id="reversed"),
        pytest.param(date(2030, 1, 18), date(2040, 1, 18), id="ten-years-wide"),
        pytest.param(None, date(9999, 12, 31), id="open-start-to-year-9999"),
    ],
)
@responses.activate
async def test_a_range_the_route_cannot_walk_is_a_422_before_any_vendor_call(client, gte, lte):
    resp = await _post(client, gte=gte, lte=lte)

    assert resp.status_code == 422
    assert len(responses.calls) == 0


@responses.activate
async def test_an_open_ended_range_past_the_default_horizon_searches_from_its_own_start(client, fake_polygon_key):
    # No upper bound and a start past today + 180 days used to build zero windows -> a 500.
    start = et_date_at_ms(now_ms_utc()) + timedelta(days=400)
    responses.add(responses.GET, VENDOR_CONTRACTS_URL, json={"results": [{"expiration_date": start.isoformat()}]})

    resp = await _post(client, gte=start)

    assert resp.status_code == 200
    assert resp.json()["expirations"] == [option_expiry_anchor_ms(start)]
    window_starts = sorted(_vendor_window_start(c.request) for c in responses.calls)
    assert window_starts[0] == start.isoformat()
    window_ends = [parse_qs(urlparse(c.request.url).query)["expiration_date.lte"][0] for c in responses.calls]
    assert max(window_ends) == (start + timedelta(days=180)).isoformat()


@responses.activate
async def test_a_window_that_spans_vendor_pages_returns_every_expiry_on_every_page(client, fake_polygon_key, caplog):
    # 2030-01-02..2030-01-31 is exactly one 30-day window, so the vendor is asked for one list.
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        json={
            "results": [{"expiration_date": "2030-01-04"}, {"expiration_date": "2030-01-11"}],
            "next_url": f"{VENDOR_CONTRACTS_URL}?cursor=page-two",
        },
    )
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        json={"results": [{"expiration_date": "2030-01-18"}, {"expiration_date": "2030-01-25"}]},
    )

    with caplog.at_level(logging.DEBUG):
        resp = await _post(client, gte=date(2030, 1, 2), lte=date(2030, 1, 31))

    assert resp.status_code == 200
    body = resp.json()
    assert body["expirations"] == [option_expiry_anchor_ms(date(2030, 1, d)) for d in (4, 11, 18, 25)]
    assert body["count"] == 4
    # The vendor's own next_url is followed, and the key is added to it the way it is to the first request.
    second = responses.calls[1].request.url
    assert "cursor=page-two" in second
    assert FAKE_KEY in second
    # ... and it never leaves the process.
    assert FAKE_KEY not in resp.text
    assert FAKE_KEY not in caplog.text


@responses.activate
async def test_a_chain_that_never_ends_fails_instead_of_returning_a_truncated_list(client, fake_polygon_key):
    def _always_another_page(_request):
        body = {"results": [{"expiration_date": "2030-01-18"}], "next_url": f"{VENDOR_CONTRACTS_URL}?cursor=again"}
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, VENDOR_CONTRACTS_URL, callback=_always_another_page)

    resp = await _post(client, gte=date(2030, 1, 2), lte=date(2030, 1, 31))

    assert resp.status_code == 500
    assert len(responses.calls) > 1


@responses.activate
async def test_a_next_url_off_the_vendor_host_is_refused_and_never_sent_the_key(client, fake_polygon_key):
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        json={"results": [], "next_url": "https://elsewhere.example/v3/reference/options/contracts?cursor=x"},
    )

    resp = await _post(client, gte=date(2030, 1, 2), lte=date(2030, 1, 31))

    assert resp.status_code == 500
    assert len(responses.calls) == 1


@pytest.mark.parametrize("outage", ["read_timeout", "connection_error", "http_503"])
@responses.activate
async def test_a_window_the_vendor_never_answers_fails_the_request_and_names_no_key(
    client, fake_polygon_key, no_backoff, caplog, outage
):
    # Two windows: the first answers, the second never does. It used to be skipped, and
    # the first window's expirations came back as a successful, complete-looking list.
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        json={"results": [{"expiration_date": "2030-01-18"}]},
        match=[matchers.query_param_matcher({"expiration_date.gte": "2030-01-02"}, strict_match=False)],
    )
    leaking_url = f"/v3/reference/options/contracts?expiration_date.gte=2030-02-01&apiKey={FAKE_KEY}"
    never_answers = {
        "read_timeout": {"body": requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)")},
        # What urllib3 really writes: the request path, apiKey included.
        "connection_error": {
            "body": requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool(host='api.polygon.io', port=443): Max retries exceeded with url: {leaking_url}"
            )
        },
        "http_503": {"status": 503, "json": {"status": "ERROR"}},
    }[outage]
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        match=[matchers.query_param_matcher({"expiration_date.gte": "2030-02-01"}, strict_match=False)],
        **never_answers,
    )

    with caplog.at_level(logging.DEBUG):
        resp = await _post(client, gte=date(2030, 1, 2), lte=date(2030, 3, 2))

    assert resp.status_code == 500
    assert "expirations" not in resp.json()
    for surface in (resp.text, caplog.text):
        assert FAKE_KEY not in surface
        assert "apiKey=" not in surface
    # Retried the three times it promises before giving up.
    second_window_calls = [c for c in responses.calls if _vendor_window_start(c.request) == "2030-02-01"]
    assert len(second_window_calls) == 3


@responses.activate
async def test_a_transport_error_nobody_anticipated_still_names_no_key(client, fake_polygon_key, caplog):
    # Not a timeout or a connection error, so it is not retried -- and it names the request URL.
    responses.add(
        responses.GET,
        VENDOR_CONTRACTS_URL,
        body=requests.exceptions.ChunkedEncodingError(
            f"Connection broken for {VENDOR_CONTRACTS_URL}?apiKey={FAKE_KEY}&limit=1000"
        ),
    )

    with caplog.at_level(logging.DEBUG):
        resp = await _post(client, gte=date(2030, 1, 2), lte=date(2030, 1, 31))

    assert resp.status_code == 500
    assert "Connection broken" in resp.json()["detail"]
    for surface in (resp.text, caplog.text):
        assert FAKE_KEY not in surface


@responses.activate
def test_a_wide_range_is_walked_completely_by_a_bounded_pool(fake_polygon_key):
    in_flight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def _answer(request):
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        threading.Event().wait(0.02)  # hold the slot long enough for requests to overlap
        with lock:
            in_flight["now"] -= 1
        body = {"results": [{"expiration_date": _vendor_window_start(request)}]}
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, VENDOR_CONTRACTS_URL, callback=_answer)
    start = date(2030, 1, 2)
    end = start + timedelta(days=30 * 35 - 1)  # thirty-five 30-day windows

    expirations = PolygonClientService().list_options_expirations(
        underlying_ticker="SPY",
        contract_type="call",
        expiration_ms_gte=option_expiry_anchor_ms(start),
        expiration_ms_lte=option_expiry_anchor_ms(end),
    )

    assert expirations == [option_expiry_anchor_ms(start + timedelta(days=30 * i)) for i in range(35)]
    assert in_flight["peak"] <= MAX_EXPIRATIONS_WORKERS
