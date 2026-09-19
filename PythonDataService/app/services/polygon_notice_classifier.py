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

Issue #2204 adds a second kind of caller: the Clerk's internal transport to
the fleet-coordinator role's history-batch operation
(``app.services.broker_v2_panel.history_batch_client``).
``coordinator_unavailable_notice`` below is not a Polygon-exception mapping --
it is the notice for the transport hop itself failing -- but it lives here
because callers still convert it into the same ``ChartOverlayNoticeView``
shape and it must not collide with, or duplicate, the ``polygon_*`` code
vocabulary above.

The shared thing is the *taxonomy* (the notice code), not the sentence: the
missing-key message names its own surface ("Polygon overlay" for LIVE,
"Polygon history" for HISTORY), so ``missing_polygon_api_key_notice`` takes
that subject as a parameter rather than hardcoding one surface's wording.
This is deliberate — genericizing the message previously changed the LIVE
chart's shipped operator-facing copy by accident (issue #2203 review).

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


def missing_polygon_api_key_notice(subject: str) -> PolygonNotice:
    """The notice for a present-but-empty ``POLYGON_API_KEY``.

    Callers check this *before* attempting a fetch — an empty key never
    reaches Polygon, so it is a precondition, not an exception mapping, and
    is kept distinct from ``polygon_auth_error`` (a key that Polygon itself
    rejected).

    ``subject`` names the failing surface in the message (e.g. ``"Polygon
    overlay"`` for the LIVE chart, ``"Polygon history"`` for the HISTORY
    chart) so each caller keeps its own correct, stable wording while still
    sharing the ``polygon_api_key_missing`` code.
    """
    return PolygonNotice(
        code=POLYGON_API_KEY_MISSING_CODE,
        message=f"{subject} is unavailable because POLYGON_API_KEY is not configured.",
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


COORDINATOR_UNAVAILABLE_CODE = "coordinator_unavailable"


def coordinator_unavailable_notice(subject: str) -> PolygonNotice:
    """The notice for a Clerk -> fleet-coordinator history-transport failure.

    Issue #2204: history retrieval moved off the Clerk (which never holds a
    usable Polygon key, per ADR 0062) and onto one authenticated internal
    request to the fleet-coordinator role. A timeout, a connection failure, a
    non-200 refusal, a malformed success payload, or an unexpected
    coordinator response all collapse into this one stable code -- the
    single history-transport extension to this module's notice vocabulary
    (FR-011).

    Deliberately **not** a ``polygon_*`` code: the vendor may be entirely
    healthy while the internal hop itself is down, and a caller that needs to
    tell the two apart (retry guidance, alerting) must not conflate them. The
    message is deliberately phrased so it stays true for every failure mode
    above (issue #2204 gate F8) -- "could not be reached" would be false for
    a coordinator that was reached and refused (a 4xx/5xx) -- and carries no
    hostname, port, token or response body; the Clerk's own transport log
    holds that diagnostic detail.
    """
    return PolygonNotice(
        code=COORDINATOR_UNAVAILABLE_CODE,
        message=f"{subject} is unavailable because the fleet coordinator "
        "did not complete the request.",
    )
