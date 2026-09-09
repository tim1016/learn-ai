"""HTTP contracts for the ticker-news read surface (GET /api/news).

Every documented upstream field is carried through — nothing is dropped
or flattened at this boundary.

Two rules from this repo shape the shapes below.

**Temporal.** ``published_utc_ms`` is the canonical ``int64 ms UTC`` form
required by ``.claude/rules/temporal-rigor.md``; the conversion happens at
the vendor ingestion boundary in ``PolygonClientService.list_news``. The
vendor's RFC3339 string is *not* carried forward — that rule is explicit
that a vendor time string is snapped to the closest constructible instant
and never kept.

**Provenance.** ``insights`` is *vendor-asserted* data, not derived. Polygon
produces the sentiment label with an unpublished model we cannot reimplement,
so it can never carry the golden fixture + pinned tolerance that
``.claude/rules/numerical-rigor.md`` requires of derived math. It is recorded,
never validated, and ``NewsResponse.sentiment_provenance`` states that on the
wire so a consumer cannot mistake it for one of our own computed features.

``NewsArticle`` mirrors ``PolygonClientService._serialize_news_article``'s
output key-for-key, so the router validates a client row with plain
``model_validate`` — there is no hand-written field-by-field mapper to drift.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

SENTIMENT_PROVENANCE = "vendor_asserted"
"""Lineage label for every sentiment value this surface returns.

Distinct from the ``derived`` features in
``app/research/features/registry.py``, which we compute from lake bytes and
prove against a fixture. An asserted value can only be recorded verbatim.
"""

VENDOR = "polygon"
VENDOR_ENDPOINT = "/v2/reference/news"


class NewsPublisher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    homepage_url: str | None = None
    logo_url: str | None = None
    favicon_url: str | None = None


class NewsInsight(BaseModel):
    """One vendor-asserted sentiment reading for one ticker in one article."""

    model_config = ConfigDict(extra="forbid")

    ticker: str | None = None
    sentiment: str | None = None
    sentiment_reasoning: str | None = None


class NewsArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str | None = None
    description: str | None = None
    author: str | None = None
    article_url: str | None = None
    amp_url: str | None = None
    image_url: str | None = None

    published_utc_ms: int | None = Field(
        default=None,
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="Canonical int64 ms UTC. Null when the vendor string was unparseable.",
    )

    tickers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    publisher: NewsPublisher | None = None
    insights: list[NewsInsight] = Field(default_factory=list)


class NewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    articles: list[NewsArticle]
    count: int = Field(ge=0)

    vendor: Literal["polygon"] = VENDOR
    vendor_endpoint: Literal["/v2/reference/news"] = VENDOR_ENDPOINT
    sentiment_provenance: Literal["vendor_asserted"] = SENTIMENT_PROVENANCE
    fetched_at_ms: int = Field(
        ge=0,
        le=MAX_TIMESTAMP_MS,
        description="When this platform fetched the page — not when the vendor scored the sentiment.",
    )
