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

import pytest

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


@pytest.mark.parametrize(
    ("row_overrides", "expected_publisher"),
    [
        pytest.param((), "Example Wire", id="flattens_lists_and_publisher"),
        pytest.param((("publisher", None),), "", id="tolerates_missing_publisher"),
    ],
)
def test_build_news_csv_flattens_lists_and_publisher(
    row_overrides: tuple[tuple[str, Any], ...], expected_publisher: str
) -> None:
    out = build_news_csv(_FakePolygon([_row(**dict(row_overrides))]), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None, "an article yields a csv"
    [record] = _parse(out)
    assert record["tickers"] == "SPY,DIA", "tickers are flattened"
    assert record["keywords"] == "markets,etf", "keywords are flattened"
    assert record["publisher"] == expected_publisher, "the publisher is flattened to its name, empty when missing"
    # The durable artifact carries canonical ms, not the vendor string.
    assert record["published_utc_ms"] == "1788528600000", "the csv carries canonical ms"


@pytest.mark.parametrize(
    ("articles", "expected_ids"),
    [
        pytest.param(
            (("new", 1788615000000), ("old", 1788528600000)),
            ["old", "new"],
            id="orders_by_canonical_ms_not_vendor_string",
        ),
        # An unparseable vendor timestamp must not drop the article.
        pytest.param(
            (("undated", None), ("dated", 1788528600000)),
            ["dated", "undated"],
            id="keeps_rows_with_null_ms_last",
        ),
    ],
)
def test_build_news_csv_orders_rows_by_canonical_ms(
    articles: tuple[tuple[str, int | None], ...], expected_ids: list[str]
) -> None:
    rows = [_row(id=article_id, published_utc_ms=published_ms) for article_id, published_ms in articles]

    out = build_news_csv(_FakePolygon(rows), "SPY", "2026-09-01", "2026-09-30")

    assert out is not None, "the articles yield a csv"
    assert [r["id"] for r in _parse(out)] == expected_ids, "rows are ordered by canonical ms, null ms last"


def test_build_news_csv_returns_none_when_no_articles() -> None:
    assert build_news_csv(_FakePolygon([]), "SPY", "2026-09-01", "2026-09-30") is None
