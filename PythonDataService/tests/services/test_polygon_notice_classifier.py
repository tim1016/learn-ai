"""Tests for the canonical Polygon-failure -> notice-code classifier (issue #2203).

This is the single mapping the LIVE overlay (``live_chart_window``) and the
bounded HISTORY resolver (``chart_projection_service``) both call, so a
parity test here pins the vocabulary both callers are parameterized against.
"""

from __future__ import annotations

import pytest

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
)
from app.services.polygon_notice_classifier import (
    POLYGON_API_KEY_MISSING_CODE,
    classify_polygon_exception,
    missing_polygon_api_key_notice,
)


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (PolygonAuthError("Polygon 401 for SPY: bad key", 401), "polygon_auth_error"),
        (PolygonEntitlementError("Polygon 403 for SPY: no plan", 403), "polygon_entitlement_error"),
        (PolygonRateLimitedError("Polygon 429 for SPY", 429), "polygon_rate_limited"),
        (PolygonUnknownSymbolError("Polygon 404 for SPY", 404), "polygon_unknown_symbol"),
        (PolygonFetchError("Polygon 500 for SPY: boom", 500), "polygon_fetch_error"),
    ],
)
def test_classify_polygon_exception_maps_each_subclass_to_its_stable_code(
    exc: PolygonFetchError, expected_code: str
) -> None:
    notice = classify_polygon_exception(exc)

    assert notice.code == expected_code
    assert notice.message == str(exc)


def test_missing_polygon_api_key_notice_is_distinct_from_auth_error() -> None:
    """An unconfigured key is never mistaken for a key Polygon itself rejected."""
    notice = missing_polygon_api_key_notice()

    assert notice.code == POLYGON_API_KEY_MISSING_CODE
    assert notice.code != classify_polygon_exception(PolygonAuthError("401", 401)).code
    assert "POLYGON_API_KEY" in notice.message
