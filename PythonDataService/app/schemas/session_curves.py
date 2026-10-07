"""A run's daily curve as a read sends it (``app/research/session_curves.py``), for every page that draws runs."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: Every session close any curve of a read scored, in order, sent once.
SessionsMs = list[Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]]


class SessionCurve(BaseModel):
    """One run's daily curve against the read's ``sessions_ms``: its values from ``first_session`` to its last session."""

    model_config = ConfigDict(extra="forbid")

    first_session: int = Field(ge=0, description="The index in sessions_ms of the run's first session close.")
    cumulative_return: list[float | None] = Field(
        description="Equity over the run's starting capital, less 1, at each session close from first_session on; null at a session the run did not score."
    )
    drawdown: list[float | None] | None = Field(
        description="Equity over its running peak, less 1, against the same sessions; null as a whole when the peak never rose above 0."
    )
    deepest_drawdown: float | None = Field(
        description="The lowest value of drawdown, at a session close; the engine's bar-by-bar worst fall can be deeper. Null without a drawdown."
    )
