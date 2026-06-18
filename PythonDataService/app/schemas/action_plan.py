"""Action-plan schema — PRD #593 Slice 1A (issue #594).

Foundational baseline. Slice 1A ships only the empty-plan shape:
``ActionPlan(on_enter=[], on_exit=[])``. Stock entry legs land in Slice 1B
(#595); option legs and selectors in Slice 1C (#596).

Per ADR 0012 the plan declares operator-intended instruments at deploy
time. The engine does NOT consume the plan in Slices 1–3 (PRD #593);
consumption is Slice 4 (follow-up PRD + ADR 0013).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ActionPlan(BaseModel):
    """Operator-declared instrument plan, hashed into ``run_id``.

    Empty in Slice 1A — leg shapes are added incrementally in #595 and
    #596. The two ordered lists exist now so the deploy boundary, ledger
    persistence, ``run_id`` hashing, and cockpit display all have a
    stable container to round-trip through before the leg variants land.
    """

    model_config = ConfigDict(extra="forbid")

    on_enter: list = Field(default_factory=list)
    on_exit: list = Field(default_factory=list)
