"""`/api/news` contract — full field fidelity, canonical time, honest provenance.

The endpoint exists to carry *every* documented upstream field through. The
tests that matter are therefore about what must **not** be dropped: the
vendor-asserted ``insights`` array, the nested publisher, the list-valued
fields, and the provenance labels that stop a consumer mistaking asserted
sentiment for one of our derived features.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.routers import news as news_router
from app.services.polygon_client import PolygonClientService

# 2026-09-04T13:30:00Z, pinned rather than recomputed so a broken conversion
# fails here instead of agreeing with itself.
PUBLISHED_MS = 1788528600000


def _article(**overrides: Any) -> dict[str, Any]:
    """One raw upstream article, shaped exactly as the JSON payload delivers it.

    Dicts, not SDK model objects: polygon-api-client 1.12.5's ``TickerNews``
    has no ``insights`` field, so the client reads the raw payload instead.
    """
    base: dict[str, Any] = {
        "id": "art-1",
        "title": "SPY climbs",
        "description": "A description.",
        "author": "A. Reporter",
        "article_url": "https://example.test/a",
        "amp_url": "https://example.test/a/amp",
        "image_url": "https://example.test/a.png",
        "published_utc": "2026-09-04T13:30:00Z",
        "tickers": ["SPY", "DIA"],
        "keywords": ["markets", "etf"],
        "publisher": {
            "name": "Example Wire",
            "homepage_url": "https://example.test",
            "logo_url": "https://example.test/logo.png",
            "favicon_url": "https://example.test/fav.ico",
        },
        "insights": [
            {"ticker": "SPY", "sentiment": "positive", "sentiment_reasoning": "Because reasons."},
        ],
    }
    base.update(overrides)
    return base


def test_serialize_news_article_reads_insights_the_sdk_model_would_drop() -> None:
    """Regression: the SDK's TickerNews dataclass has no ``insights`` field.

    Asserting on a third-party internal is deliberate here, against
    ``testing.md``'s general guidance: this is a canary telling us when the
    raw-payload workaround can be retired, not a test of library behaviour
    we depend on.

    Deserializing through the typed iterator silently discards vendor
    sentiment — the one field this surface exists to carry. The client reads
    the raw payload for exactly this reason; this test fails if it stops.
    """
    from polygon.rest.models import TickerNews

    assert "insights" not in TickerNews.__dataclass_fields__, (
        "The SDK now models `insights` — the raw-payload workaround in "
        "PolygonClientService.list_news can be reconsidered."
    )
    assert PolygonClientService._serialize_news_article(_article())["insights"] != []


def test_serialize_news_article_preserves_every_documented_field() -> None:
    row = PolygonClientService._serialize_news_article(_article())

    assert row["id"] == "art-1"
    assert row["amp_url"] == "https://example.test/a/amp"
    assert row["image_url"] == "https://example.test/a.png"
    # Lists stay lists — the CSV exporter flattens at its own boundary.
    assert row["tickers"] == ["SPY", "DIA"]
    assert row["keywords"] == ["markets", "etf"]
    assert row["publisher"]["favicon_url"] == "https://example.test/fav.ico"
    # The field the previous implementation dropped entirely.
    assert row["insights"] == [
        {"ticker": "SPY", "sentiment": "positive", "sentiment_reasoning": "Because reasons."}
    ]


def test_serialize_news_article_converts_published_utc_to_canonical_ms() -> None:
    row = PolygonClientService._serialize_news_article(_article())

    assert row["published_utc_ms"] == PUBLISHED_MS
    # temporal-rigor: the vendor's RFC3339 string is snapped to ms and not kept.
    assert "published_utc" not in row


def test_serialize_news_article_survives_unparseable_published_utc() -> None:
    """One bad vendor timestamp nulls its own ms — it must not discard the row."""
    row = PolygonClientService._serialize_news_article(_article(published_utc="not-a-date"))

    assert row["published_utc_ms"] is None
    assert row["title"] == "SPY climbs"


def test_serialize_news_article_tolerates_absent_publisher_and_insights() -> None:
    row = PolygonClientService._serialize_news_article(_article(publisher=None, insights=None))

    assert row["publisher"] is None
    assert row["insights"] == []



# ── The vendor seam ──────────────────────────────────────────────────────
# Everything above tests `_serialize_news_article`, a pure static. These drive
# `list_news` itself, which is where the risky assumptions live: that
# `raw=True` yields an object with `.data`, that the payload parses, that the
# throttle is acquired, and that every parameter is forwarded.


class _FakeSdkClient:
    """Stands in for ``polygon.RESTClient``, recording what it was asked for."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def list_ticker_news(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(data=json.dumps(self._payload))


class _RecordingThrottle:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def acquire(self, *, label: str, on_event: Any = None) -> None:
        self.labels.append(label)


def _client_with(payload: dict[str, Any]) -> tuple[PolygonClientService, _FakeSdkClient, _RecordingThrottle]:
    """Build the service without running ``__init__`` (which needs a live API key)."""
    service = PolygonClientService.__new__(PolygonClientService)
    sdk = _FakeSdkClient(payload)
    throttle = _RecordingThrottle()
    service.client = sdk
    service._throttle = throttle
    return service, sdk, throttle


def test_list_news_reads_the_raw_payload_and_keeps_insights() -> None:
    service, sdk, _ = _client_with({"results": [_article()], "next_url": "https://example.test/next"})

    rows = service.list_news(ticker="SPY", limit=5)

    assert sdk.calls[0]["raw"] is True, "raw mode is what makes insights visible at all"
    assert len(rows) == 1
    assert rows[0]["insights"][0]["sentiment"] == "positive"
    assert rows[0]["published_utc_ms"] == PUBLISHED_MS


def test_list_news_forwards_every_parameter_to_the_sdk() -> None:
    service, sdk, _ = _client_with({"results": []})

    service.list_news(
        ticker="SPY",
        ticker_gte="A",
        ticker_gt="B",
        ticker_lte="Y",
        ticker_lt="Z",
        published_utc="2026-09-04",
        published_utc_gte="2026-09-01",
        published_utc_gt="2026-08-31",
        published_utc_lte="2026-09-30",
        published_utc_lt="2026-10-01",
        sort="published_utc",
        order="desc",
        limit=7,
    )

    sent = sdk.calls[0]
    assert sent["ticker"] == "SPY"
    assert (sent["ticker_gte"], sent["ticker_gt"], sent["ticker_lte"], sent["ticker_lt"]) == ("A", "B", "Y", "Z")
    assert sent["published_utc"] == "2026-09-04"
    assert (sent["published_utc_gte"], sent["published_utc_gt"]) == ("2026-09-01", "2026-08-31")
    assert (sent["published_utc_lte"], sent["published_utc_lt"]) == ("2026-09-30", "2026-10-01")
    assert (sent["sort"], sent["order"], sent["limit"]) == ("published_utc", "desc", 7)


def test_list_news_caps_limit_at_the_upstream_page_ceiling() -> None:
    service, sdk, _ = _client_with({"results": []})

    service.list_news(ticker="SPY", limit=5000)

    assert sdk.calls[0]["limit"] == 1000


def test_list_news_paces_the_request() -> None:
    """The plan ceiling is 5/min; an unpaced news poll would burn it."""
    service, _, throttle = _client_with({"results": []})

    service.list_news(ticker="SPY")

    assert throttle.labels == ["news:SPY"]


def test_list_news_tolerates_a_payload_with_no_results_key() -> None:
    service, _, _ = _client_with({"status": "OK"})

    assert service.list_news(ticker="SPY") == []


def test_list_news_propagates_an_upstream_error() -> None:
    service, sdk, _ = _client_with({"results": []})

    def boom(**_: Any) -> SimpleNamespace:
        raise RuntimeError("polygon down")

    sdk.list_ticker_news = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="polygon down"):
        service.list_news(ticker="SPY")


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(news_router.router, prefix="/api/news")
    return app


async def _get(app: FastAPI, url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url)


async def test_endpoint_forwards_every_query_parameter(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All 13 upstream parameters must reach the client, not just the ticker."""
    seen: dict[str, Any] = {}

    def fake_list_news(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(news_router.polygon_client, "list_news", fake_list_news)

    resp = await _get(
        api,
        "/api/news?ticker=SPY&ticker_gte=A&ticker_gt=B&ticker_lte=Y&ticker_lt=Z"
        "&published_utc=2026-09-04&published_utc_gte=2026-09-01&published_utc_gt=2026-08-31"
        "&published_utc_lte=2026-09-30&published_utc_lt=2026-10-01"
        "&sort=published_utc&order=desc&limit=7",
    )

    assert resp.status_code == 200
    assert seen == {
        "ticker": "SPY",
        "ticker_gte": "A",
        "ticker_gt": "B",
        "ticker_lte": "Y",
        "ticker_lt": "Z",
        "published_utc": "2026-09-04",
        "published_utc_gte": "2026-09-01",
        "published_utc_gt": "2026-08-31",
        "published_utc_lte": "2026-09-30",
        "published_utc_lt": "2026-10-01",
        "sort": "published_utc",
        "order": "desc",
        "limit": 7,
    }


async def test_endpoint_labels_sentiment_as_vendor_asserted(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provenance label is part of the contract, not decoration.

    A consumer must be able to tell asserted vendor sentiment from a derived
    feature without reading our source.
    """
    monkeypatch.setattr(
        news_router.polygon_client,
        "list_news",
        lambda **_: [PolygonClientService._serialize_news_article(_article())],
    )

    resp = await _get(api, "/api/news?ticker=SPY")
    body = resp.json()

    assert resp.status_code == 200
    assert body["sentiment_provenance"] == "vendor_asserted"
    assert body["vendor"] == "polygon"
    assert body["vendor_endpoint"] == "/v2/reference/news"
    assert body["count"] == 1
    assert body["fetched_at_ms"] > 0

    article = body["articles"][0]
    assert article["published_utc_ms"] == PUBLISHED_MS
    assert article["insights"][0]["sentiment"] == "positive"


async def test_endpoint_returns_502_when_upstream_fails(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream outage is a gateway failure, never an unhandled 500."""

    def boom(**_: Any) -> list[dict[str, Any]]:
        raise RuntimeError("polygon down")

    monkeypatch.setattr(news_router.polygon_client, "list_news", boom)

    resp = await _get(api, "/api/news?ticker=SPY")

    assert resp.status_code == 502
    assert "polygon down" in resp.json()["detail"]


@pytest.mark.parametrize("limit", [0, 1001])
async def test_endpoint_rejects_out_of_range_limit(api: FastAPI, limit: int) -> None:
    resp = await _get(api, f"/api/news?ticker=SPY&limit={limit}")

    assert resp.status_code == 422
