"""Wire models of the Realized vs IV study's window route (#3525)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS


class RealizedVsIvWindow(BaseModel):
    """The window a Realized vs IV run reads when its request names none.

    ``window_start_ms`` and ``window_end_ms`` are ET midnight of the first and last session, ``int64 ms UTC``: the values the
    job request takes, so a page can send back exactly the window it showed. The window holds the fewest sessions whose
    bars let a VRP z-score exist (``resolve_window``), and ends on the last session the study can read or capture now.
    """

    bar_size: Literal["15m", "1d"]
    #: The session the run reads, spelled as every request spells it.
    session: Literal["rth", "extended"]
    window_start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, description="ET midnight of the first session, int64 ms UTC")
    window_end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, description="ET midnight of the last session, int64 ms UTC")
    #: Scheduled sessions from the first to the last, both included.
    sessions: int = Field(ge=1)
