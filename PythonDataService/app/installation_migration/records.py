"""The strict record base every migration fact and manifest field shares (#2268).

Its own module so both :mod:`facts` (volume identity) and :mod:`topology`
(host facts) can build on it: ``contents`` imports ``topology``, and ``facts``
imports ``contents``, so the base cannot live in either without a cycle.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: An instant in the domain's admissible range (temporal-rigor.md).
InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class StrictRecord(BaseModel):
    """A frozen, closed, strictly typed fact: no coercion, no unknown field.

    Strict so a manifest that says ``1`` where a flag belongs, or ``true``
    where a generation belongs, is refused rather than read as the other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


__all__ = ["InstantMs", "StrictRecord"]
