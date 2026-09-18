"""Canonical Polygon-failure -> notice-code classification (issue #2203).

Fleet Clerks boot with a present-but-empty ``POLYGON_API_KEY`` (see ADR 0062's
retained market-data provider decision: IBKR supplies live data, Alpaca
handles accounts, and a Clerk must never hold a live Polygon subscription).
Both the LIVE chart's Polygon gap overlay (``app.services.live_chart_window``)
and the bounded HISTORY chart's sole-source Polygon fetch
(``app.services.broker_v2_panel.chart_projection_service``) must degrade a
missing key or a Polygon fetch failure into one of a small set of stable
notice codes instead of letting the failure escape as an unhandled exception.

This module is the single mapping. Each caller converts the returned
``PolygonNotice`` into its own response-shaped notice type
(``live_chart_window.ChartOverlayNotice`` for LIVE,
``app.schemas.broker_v2_panel.ChartOverlayNoticeView`` for HISTORY) rather
than importing each other's private mapping, so the two panes cannot drift
onto a second vocabulary (CLAUDE.md guiding philosophy #5).

Canonical implementation: this module.
Reference: the ``PolygonFetchError`` hierarchy in
``app.data_lake.polygon_fetcher``.
Validated against: ``tests/services/test_polygon_notice_classifier.py`` and
the unchanged ``tests/services/test_live_chart_window.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
)

POLYGON_API_KEY_MISSING_CODE = "polygon_api_key_missing"


@dataclass(frozen=True)
class PolygonNotice:
    """A stable (code, message) pair describing one Polygon-facing failure."""

    code: str
    message: str


def missing_polygon_api_key_notice() -> PolygonNotice:
    """The notice for a present-but-empty ``POLYGON_API_KEY``.

    Callers check this *before* attempting a fetch — an empty key never
    reaches Polygon, so it is a precondition, not an exception mapping, and
    is kept distinct from ``polygon_auth_error`` (a key that Polygon itself
    rejected).
    """
    return PolygonNotice(
        code=POLYGON_API_KEY_MISSING_CODE,
        message="Polygon is unavailable because POLYGON_API_KEY is not configured.",
    )


def classify_polygon_exception(exc: PolygonFetchError) -> PolygonNotice:
    """Map one raised Polygon fetch exception to its stable notice code."""
    if isinstance(exc, PolygonAuthError):
        return PolygonNotice("polygon_auth_error", str(exc))
    if isinstance(exc, PolygonEntitlementError):
        return PolygonNotice("polygon_entitlement_error", str(exc))
    if isinstance(exc, PolygonRateLimitedError):
        return PolygonNotice("polygon_rate_limited", str(exc))
    if isinstance(exc, PolygonUnknownSymbolError):
        return PolygonNotice("polygon_unknown_symbol", str(exc))
    return PolygonNotice("polygon_fetch_error", str(exc))
