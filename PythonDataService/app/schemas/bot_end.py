"""A bot's owner-set end: when it stops, and whether it sells or keeps its shares then (#2607).

The end is the owner's schedule for one deployed bot, not one of its sealed
terms: it lives in the bot's desired state, may be changed while the bot
runs, and never enters a binding, a configuration hash, a program seal or a
consent fingerprint. ``end_at_ms`` is ``int64 ms UTC``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: What the Clerk does with the bot's shares at its end: sell them at market,
#: or keep them.
BotEndAction = Literal["SELL", "KEEP"]

#: Where a bot's end stands. ``ending``: its time has come and the Clerk has
#: still to carry it out; ``ended``: the Clerk has carried it out.
BotEndStatus = Literal["no_end", "scheduled", "ending", "ended"]

#: The one refusal for an explicit ``"end": null``: an omitted end is the
#: default end, and "no end" is named, never implied by a null.
EXPLICIT_NULL_END = (
    "end may not be null: omit it for the default end (the session close minus one minute), "
    'or send {"end_at_ms": null, "end_action": "SELL"} for no end'
)


class BotEnd(BaseModel):
    """One scheduled end, validated against the canonical calendar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction = "SELL"


class RecordedEnd(BotEnd):
    """An end as the bot's desired state records it: the end, and when the Clerk carried it out."""

    carried_out_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def scheduled(cls, end: BotEnd) -> RecordedEnd:
        """``end``, chosen now and not yet carried out."""
        return cls(end_at_ms=end.end_at_ms, end_action=end.end_action)

    def pending(self) -> BotEnd | None:
        """The end the Clerk still has to carry out; ``None`` once it has."""
        if self.carried_out_at_ms is not None:
            return None
        return BotEnd(end_at_ms=self.end_at_ms, end_action=self.end_action)


class BotEndInput(BaseModel):
    """The owner's choice of end, as Deploy and the bot panel send it.

    Both fields are required. ``end_at_ms`` ``null`` is the explicit "no
    end" choice, sent with ``end_action`` ``SELL``: a bot with no end has no
    shares to keep at one. A Deploy that sends no ``end`` at all gets the
    default end.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction


def refuse_explicit_null_end(value: object) -> object:
    """The ``mode="before"`` check of an optional ``end``: only an omitted end is the default one.

    A field's default is never validated, so this runs only for an ``end``
    the request actually sent.
    """
    if value is None:
        raise ValueError(EXPLICIT_NULL_END)
    return value


class BotEndPreviewRequest(BaseModel):
    """Check an end on the Deploy form before the bot exists.

    ``end`` omitted previews the default end; an explicit ``null`` is refused
    (:data:`EXPLICIT_NULL_END`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_mode: Literal["paper", "dry_run", "shadow", "live"]
    end: BotEndInput | SkipJsonSchema[None] = None

    @field_validator("end", mode="before")
    @classmethod
    def _end_is_never_null(cls, value: object) -> object:
        return refuse_explicit_null_end(value)


class BotEndView(BaseModel):
    """A bot's end in the owner's words, authored by the backend.

    ``headline`` is the one line the panel shows ("Ends Wed Sep 30, 15:59 ET
    · sells"); ``explanation`` says what will happen, or what happened.
    ``notice`` is set only when the chosen time was moved, e.g. to one minute
    before an early close. ``editable`` says whether the panel may offer to
    change it now. ``default_end_at_ms`` is the end the fields open on when
    the owner adds one to a bot with none -- the default end, by Deploy's
    rule -- set only while the bot has no end, its end may change now, and it
    trades in regular hours only; otherwise null.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_action: BotEndAction
    status: BotEndStatus
    headline: str
    explanation: str
    notice: str | None = None
    editable: bool
    default_end_at_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)


__all__ = [
    "EXPLICIT_NULL_END",
    "BotEnd",
    "BotEndAction",
    "BotEndInput",
    "BotEndPreviewRequest",
    "BotEndStatus",
    "BotEndView",
    "RecordedEnd",
    "refuse_explicit_null_end",
]
