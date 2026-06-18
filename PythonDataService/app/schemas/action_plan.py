"""Action-plan schema — PRD #593 Slices 1A (#594) and 1B (#595).

Slice 1A shipped the empty-plan envelope.
Slice 1B adds the first concrete leg shape: stock ``ActionEntity`` entry
legs and the ``close_leg`` ``ExitEntity`` reference. Option legs and
their strike / expiry selectors land in Slice 1C (#596).

ADR 0012 fixes the invariants this schema encodes:
* every entry leg carries a stable ``leg_id`` (regex ``^[a-z0-9_]{1,32}$``)
  so the future resolver can persist a ``leg_id -> conId`` map and exits
  target the exact contract entered (§3);
* every leg carries an explicit ``instrument.underlying`` — no implicit
  fallback from ``live_config.symbol`` (§5);
* ``qty_ratio`` is a declarative positive integer; composition against
  ``live_config.sizing`` is deferred to Slice 4 (§4);
* exit entries are lifecycle actions — Slice 1 ships only ``close_leg``,
  which references an entry by ``entry_leg_id``. Exits NEVER redeclare
  selectors (§3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ``leg_id`` regex from ADR 0012 §3 — lowercase ASCII + digits + underscore,
# bounded to 32 chars so it embeds cleanly in logs, parquet column names,
# and future broker-side ``orderRef`` tags without escaping.
_LEG_ID_PATTERN = r"^[a-z0-9_]{1,32}$"


class StockInstrument(BaseModel):
    """Stock leg instrument — Slice 1B. Option instruments land in #596."""

    kind: Literal["stock"]
    underlying: Annotated[str, Field(min_length=1)]


class StockEntryLeg(BaseModel):
    """Stock ``ActionEntity`` — Slice 1B (#595).

    Operator-declared instrument the bot opens on entry. The ``leg_id``
    becomes the leg's stable identity for the run lifetime; exits
    reference it via ``close_leg.entry_leg_id``.
    """

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    instrument: StockInstrument
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1)]


class CloseLegExit(BaseModel):
    """``close_leg`` ``ExitEntity`` — Slice 1B.

    Lifecycle action that closes a previously-opened entry leg by its
    stable ``leg_id``. The plan-level validator enforces referential
    integrity (the referenced leg must exist on ``on_enter``).
    """

    kind: Literal["close_leg"]
    entry_leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]


class ActionPlan(BaseModel):
    """Operator-declared instrument plan, hashed into ``run_id``.

    Slice 1A shipped the empty-plan envelope. Slice 1B adds stock entry
    legs and ``close_leg`` exits; Slice 1C will add option legs and
    selectors. Hard schema rejections (orphan close_leg, duplicate
    leg_id, missing underlying, qty_ratio < 1, malformed leg_id) live
    here. Parity *warnings* (asymmetric structures, etc.) are computed
    separately by ``parity_diagnostics`` in Slice 1D (#597).
    """

    model_config = ConfigDict(extra="forbid")

    on_enter: list[StockEntryLeg] = Field(default_factory=list)
    on_exit: list[CloseLegExit] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_referential_integrity(self) -> ActionPlan:
        leg_ids: list[str] = [leg.leg_id for leg in self.on_enter]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for lid in leg_ids:
            if lid in seen:
                duplicates.add(lid)
            seen.add(lid)
        if duplicates:
            raise ValueError(
                f"duplicate leg_id within action plan: {sorted(duplicates)}"
            )
        for exit_entry in self.on_exit:
            if exit_entry.entry_leg_id not in seen:
                raise ValueError(
                    f"close_leg references unknown entry_leg_id "
                    f"{exit_entry.entry_leg_id!r}; on_enter declares "
                    f"{sorted(seen)}"
                )
        return self
