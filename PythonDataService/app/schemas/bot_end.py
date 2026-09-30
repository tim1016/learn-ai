"""A bot's owner-set end: when it stops, and whether it sells or keeps its shares then (#2607).

The end is the owner's schedule for one deployed bot, not one of its sealed
terms: it lives in the bot's desired state, may be changed while the bot
runs, and never enters a binding, a configuration hash, a program seal or a
Deploy fingerprint. ``end_at_ms`` is ``int64 ms UTC``; ``None`` means "no
end: run until I stop it".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: What the Clerk does with the bot's shares at its end: sell them at market,
#: or keep them.
BotEndAction = Literal["SELL", "KEEP"]

#: Where a bot's end stands. ``ending``: its time has come and the Clerk is
#: carrying it out; ``ended``: the Clerk has carried it out.
BotEndStatus = Literal["no_end", "scheduled", "ending", "ended"]


class BotEnd(BaseModel):
    """One scheduled end, validated against the canonical calendar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction = "SELL"


class BotEndInput(BaseModel):
    """The owner's choice of end, as Deploy and the bot panel send it.

    ``end_at_ms`` is required and may be ``null``: ``null`` is the explicit
    "no end" choice. A Deploy that sends no end at all gets the default end.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction = "SELL"


class BotEndPreviewRequest(BaseModel):
    """Check an end on the Deploy form before the bot exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_mode: Literal["paper", "dry_run", "shadow", "live"]
    # ``None`` previews the default end.
    end: BotEndInput | None = None


class BotEndView(BaseModel):
    """A bot's end in the owner's words, authored by the backend.

    ``headline`` is the one line the panel shows ("Ends today 15:59 ET ·
    sells"); ``explanation`` says what will happen, or what happened.
    ``notice`` is set only when the chosen time was moved, e.g. to one minute
    before an early close. ``editable`` says whether the panel may offer to
    change it now.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction
    status: BotEndStatus
    headline: str
    explanation: str
    notice: str | None = None
    editable: bool


__all__ = [
    "BotEnd",
    "BotEndAction",
    "BotEndInput",
    "BotEndPreviewRequest",
    "BotEndStatus",
    "BotEndView",
]
