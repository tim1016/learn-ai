"""The catalog walk's vendor-call contract: what we actually ask Polygon for.

The route tests stub ``list_catalog_tickers`` wholesale, so only these pin
the request itself — that the delisted half of the universe is *fetched*
(explicit ``active=True`` and ``active=False`` walks), not merely projected
once it arrives.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.polygon_client import PolygonClientService


class _FakeTicker:
    def __init__(self, ticker: str, active: bool) -> None:
        self.ticker = ticker
        self.name = f"{ticker} Inc."
        self.primary_exchange = "NASDAQ"
        self.active = active


class _FakeSdk:
    """Captures ``list_tickers`` kwargs and replays one page per call."""

    def __init__(self) -> None:
        self.pages: dict[bool, list[list[_FakeTicker]]] = {True: [], False: []}
        self.calls: list[dict[str, Any]] = []

    def list_tickers(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # The SDK's iterator is flat across pages — pagination is its job,
        # not the caller's.
        return iter(t for page in self.pages[kwargs["active"]] for t in page)


def _service_with(sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch) -> PolygonClientService:
    service = PolygonClientService()
    monkeypatch.setattr(service, "client", sdk)
    return service


def test_catalog_walks_both_listing_states(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _FakeSdk()
    sdk.pages[True] = [[_FakeTicker("MSFT", True)]]
    sdk.pages[False] = [[_FakeTicker("OLD", False)]]
    service = _service_with(sdk, monkeypatch)

    entries = service.list_catalog_tickers()

    # The delisted walk must be a real fetch with active=False: an omitted
    # `active` filter reads as active-only on the wire, which silently
    # emptied the picker's delisted universe.
    assert sdk.calls == [
        {"market": "stocks", "active": True, "limit": 1000},
        {"market": "stocks", "active": False, "limit": 1000},
    ]
    assert [(row["symbol"], row["status"]) for row in entries] == [
        ("MSFT", "active"),
        ("OLD", "inactive"),
    ]


def test_catalog_walk_dedupes_a_symbol_asked_for_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeSdk()
    # A symbol that flips listing state between the two walks would arrive
    # from both; the first (active) walk wins and it ships once.
    sdk.pages[True] = [[_FakeTicker("MSFT", True)]]
    sdk.pages[False] = [[_FakeTicker("MSFT", False)]]
    service = _service_with(sdk, monkeypatch)

    entries = service.list_catalog_tickers()

    assert [row["symbol"] for row in entries] == ["MSFT"]
    assert entries[0]["status"] == "active"
