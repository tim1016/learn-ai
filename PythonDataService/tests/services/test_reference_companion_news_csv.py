"""`build_news_csv` flattening — the exporter owns its own shape.

`PolygonClientService.list_news` returns structured rows (list-valued
`tickers`/`keywords`, a nested `publisher`) so the news page keeps full
fidelity. The CSV writer stringifies whatever it is handed, so this
exporter must flatten at its own boundary — otherwise a column reads
`['SPY', 'DIA']` instead of `SPY,DIA`.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.services.reference_companion_service import build_news_csv


class _FakePolygon:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    def list_news(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self._rows


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "a1",
        "title": "T",
        "author": "A",
        "published_utc_ms": 1788528600000,
        "article_url": "https://example.test/a",
        "description": "D",
        "tickers": ["SPY", "DIA"],
        "keywords": ["markets", "etf"],
        "publisher": {"name": "Example Wire", "homepage_url": "https://example.test"},
    }
    base.update(overrides)
    return base


def _parse(csv_bytes: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))


def test_build_news_csv_flattens_lists_and_publisher() -> None:
    out = build_news_csv(_FakePolygon([_row()]), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None
    [record] = _parse(out)
    assert record["tickers"] == "SPY,DIA"
    assert record["keywords"] == "markets,etf"
    assert record["publisher"] == "Example Wire"
    # temporal-rigor: the durable artifact carries canonical ms, not the vendor string.
    assert record["published_utc_ms"] == "1788528600000"


def test_build_news_csv_orders_by_canonical_ms_not_vendor_string() -> None:
    older = _row(id="old", published_utc_ms=1788528600000)
    newer = _row(id="new", published_utc_ms=1788615000000)

    out = build_news_csv(_FakePolygon([newer, older]), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None
    assert [r["id"] for r in _parse(out)] == ["old", "new"]


def test_build_news_csv_keeps_rows_with_null_ms_last() -> None:
    """An unparseable vendor timestamp must not drop the article."""
    dated = _row(id="dated")
    undated = _row(id="undated", published_utc_ms=None)

    out = build_news_csv(_FakePolygon([undated, dated]), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None
    assert [r["id"] for r in _parse(out)] == ["dated", "undated"]


def test_build_news_csv_returns_none_when_no_articles() -> None:
    assert build_news_csv(_FakePolygon([]), "SPY", "2026-09-01", "2026-09-30") is None


def test_build_news_csv_tolerates_missing_publisher() -> None:
    out = build_news_csv(_FakePolygon([_row(publisher=None)]), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None
    [record] = _parse(out)
    assert record["publisher"] == ""
