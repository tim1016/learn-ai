"""The one strict wire contract for the coordinator-owned history batch.

Issue #2204 gate F2. Before this module the contract existed in three
hand-maintained shapes and neither side enforced it: the
``CompleteHistoryBatch`` dataclass, a hand-built response dict literal in
``app.routers.internal_fleet``, and a private, field-defaulting
``_HistoryBatchWireResponse`` on the Clerk
(``app.services.broker_v2_panel.history_batch_client``) -- and the request
itself was a dict literal on the Clerk but a ``BaseModel`` on the
coordinator. A missing ``bars`` key produced a healthy-looking empty batch
instead of a refusal; unsorted or duplicate bars, a non-``polygon`` bar
source, a string/float/bool ``as_of_ms``, and a mismatched
``effective_as_of_ms`` all passed silently.

``HistoryBatchQuery`` and ``HistoryBatchRequest`` are ``strict=True`` and
``extra="forbid"`` with no defaults on any field, so a malformed request
fails at the boundary (422 on the coordinator; ``coordinator_unavailable`` on
the Clerk, per ``RemoteHistoryBatchClient.fetch_batch``) instead of coercing
a string/float/bool into an int or silently dropping an unrecognized field.
``HistoryBatchQuery`` carries only what a batch provider needs to answer one
request (issue #2204 gate F2's "no swappable positional ints" -- one object,
not a four-positional-argument tuple two of whose members are bare ints);
``HistoryBatchRequest`` extends it with the ``clerk_id`` the wire boundary
additionally authenticates with, and is the exact model both
``app.routers.internal_fleet``'s route parameter and
``RemoteHistoryBatchClient.fetch_batch``'s outbound POST body use -- one
model, not the dict/BaseModel mismatch above.

``HistoryBatchResponse`` replaces ``CompleteHistoryBatch``. Its
``model_validator`` enforces every invariant the old response silently
accepted a violation of: bars sorted strictly increasing by ``start_ms``,
every bar's ``end_ms`` exceeding its ``start_ms``, and every bar tagged
``source="polygon"`` (the sole source this operation ever returns). The
batch-level ``source`` field is the response's own provenance declaration
(PRD FR-007), distinct from each bar's own ``source`` tag.

Boundary bound on ``as_of_ms`` / ``effective_as_of_ms``: the backward walk
(``app.lean_sidecar.trading_calendar.session_start_for_bar_count``) crashes
outside a specific range rather than degrading gracefully --
``as_of_ms=0`` raises ``ValueError`` (the UTC-to-ET conversion pushes the
resolved date before the walk's own Unix-epoch floor) and a value near
``MAX_TIMESTAMP_MS`` raises ``OverflowError`` (pandas' nanosecond
``Timestamp`` cannot represent an instant past ~2262-04-11). The chosen
range, ``[2000-01-01T00:00:00Z, 2260-01-01T00:00:00Z]``, is comfortably
inside both edges: any ``required_bar_count`` this operation can ever
declare (``MAX_HISTORY_REQUIRED_BAR_COUNT``) resolves against thousands of
NYSE sessions behind 2000-01-01, and 2260-01-01 leaves the walk's own
internal date arithmetic (chunk expansion, session-close lookups) over two
years of margin before pandas' ceiling. Everything outside it is a 422 at
the coordinator, never a 500.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.broker_bots import normalized_symbol
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryTimeframe, ChartOverlayNoticeView

#: The as_of_ms/effective_as_of_ms domain this operation accepts -- see the
#: module docstring for why these exact instants.
MIN_HISTORY_BATCH_AS_OF_MS = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp() * 1000)
MAX_HISTORY_BATCH_AS_OF_MS = int(datetime(2260, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _bounded_required_bar_count(value: int) -> int:
    """Reject a count the display+warmup policy could never produce.

    A deferred import: ``app.services.broker_v2_panel.chart_projection_service``
    is a service module that itself imports this schema module for
    ``HistoryBatchQuery``/``HistoryBatchResponse`` (issue #2204 gate F5's
    relocated coordinator walk does too) -- importing it back at this
    module's top level would be circular. By the time a request is actually
    validated, both modules have finished importing, so the deferred lookup
    always resolves.
    """
    from app.services.broker_v2_panel.chart_projection_service import (
        MAX_HISTORY_REQUIRED_BAR_COUNT,
    )

    if value > MAX_HISTORY_REQUIRED_BAR_COUNT:
        raise ValueError(
            f"required_bar_count must not exceed {MAX_HISTORY_REQUIRED_BAR_COUNT} "
            "(the largest value the display+warmup policy can produce)"
        )
    return value


class HistoryBatchQuery(BaseModel):
    """What a Clerk-side batch provider needs to answer one history request.

    The ``HistoryBatchProvider`` callable (``chart_projection_service``)
    takes exactly this one object -- issue #2204 gate F2's "no swappable
    positional ints" -- rather than the
    ``(symbol, timeframe, required_bar_count, as_of_ms)`` tuple every
    implementation used to accept, two members of which are bare ints a
    caller could transpose without either side noticing.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=12)
    timeframe: ChartHistoryTimeframe
    required_bar_count: int = Field(gt=0)
    as_of_ms: int = Field(ge=MIN_HISTORY_BATCH_AS_OF_MS, le=MAX_HISTORY_BATCH_AS_OF_MS)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return normalized_symbol(value)

    @field_validator("required_bar_count")
    @classmethod
    def _bound_required_bar_count(cls, value: int) -> int:
        return _bounded_required_bar_count(value)


class HistoryBatchRequest(HistoryBatchQuery):
    """The wire request crossing the Clerk -> coordinator boundary.

    Issue #2204: the sixth ``/internal/fleet/*`` operation. Adds the
    ``clerk_id`` the coordinator authenticates the call against (checked
    against the header identity by ``app.routers.internal_fleet``'s
    ``_authorized_agent``) to ``HistoryBatchQuery``'s fields -- no ``date``
    or ISO timestamp crosses this boundary (``temporal-rigor.md``); the
    coordinator does the ms->ET date conversion with the canonical NYSE
    calendar helpers.

    ``RemoteHistoryBatchClient.fetch_batch`` sends this exact model (never a
    hand-built dict) and the router declares it as its request body type, so
    the two sides cannot drift onto different shapes.
    """

    clerk_id: str = Field(min_length=1)


class HistoryBatchResponse(BaseModel):
    """The one complete history batch a coordinator answers with.

    Replaces the three previously hand-maintained response shapes (see the
    module docstring). ``CompleteHistoryBatch`` (the pre-#2204 dataclass) is
    gone; this model is the type ``fetch_complete_history_batch`` /
    ``build_coordinator_history_batch`` return, the router declares as its
    ``response_model``, and ``RemoteHistoryBatchClient.fetch_batch``
    ``model_validate``s the coordinator's response into.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    bars: list[ChartBar]
    #: The batch's own provenance declaration (PRD FR-007) -- distinct from
    #: each bar's own ``source`` tag, which the validator below also pins to
    #: ``"polygon"``. Always ``"polygon"``: this operation has exactly one
    #: vendor.
    source: Literal["polygon"]
    overlay_notices: list[ChartOverlayNoticeView]
    effective_as_of_ms: int = Field(
        ge=MIN_HISTORY_BATCH_AS_OF_MS, le=MAX_HISTORY_BATCH_AS_OF_MS
    )

    @model_validator(mode="after")
    def _bars_are_sorted_and_all_polygon_sourced(self) -> HistoryBatchResponse:
        if self.source != "polygon":
            raise ValueError(f"batch source must be 'polygon', found {self.source!r}")
        previous_start_ms: int | None = None
        for bar in self.bars:
            if bar.source != "polygon":
                raise ValueError(
                    f"every history-batch bar must be source='polygon', found {bar.source!r}"
                )
            if bar.end_ms <= bar.start_ms:
                raise ValueError(
                    f"bar end_ms ({bar.end_ms}) must exceed its start_ms ({bar.start_ms})"
                )
            if previous_start_ms is not None and bar.start_ms <= previous_start_ms:
                raise ValueError(
                    "history-batch bars must be strictly increasing by start_ms"
                )
            previous_start_ms = bar.start_ms
        return self


__all__ = [
    "MAX_HISTORY_BATCH_AS_OF_MS",
    "MIN_HISTORY_BATCH_AS_OF_MS",
    "HistoryBatchQuery",
    "HistoryBatchRequest",
    "HistoryBatchResponse",
]
