"""Action-plan schema — PRD #593 Slices 1A (#594), 1B (#595), 1C (#596).

Slice 1A shipped the empty-plan envelope.
Slice 1B added the stock ``ActionEntity`` entry leg + ``close_leg`` exit.
Slice 1C adds the option ``ActionEntity`` and the strike / expiry
selector discriminated unions.

ADR 0012 fixes the invariants this schema encodes:
* every entry leg carries a stable ``leg_id`` (regex
  ``^[a-z0-9_]{1,32}$``) so the future resolver can persist a
  ``leg_id -> conId`` map (§3);
* every leg carries an explicit ``instrument.underlying`` — no implicit
  fallback from ``live_config.symbol`` (§5);
* ``qty_ratio`` is a declarative positive integer; composition against
  ``live_config.sizing`` is deferred to Slice 4 (§4);
* exit entries are lifecycle actions — Slice 1 ships only ``close_leg``,
  which references an entry by ``entry_leg_id``. Exits NEVER redeclare
  selectors (§3);
* the ``delta`` strike selector is deliberately omitted from the
  deployable schema until Slice 6 ships its resolver, so an operator
  cannot deploy a plan the engine cannot run;
* the ``absolute`` expiry selector carries ``expiration_ms: int64`` ms
  UTC per the repo's timestamp policy; display conversion to
  ``America/New_York`` is a UI-layer concern.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

# ``leg_id`` regex from ADR 0012 §3 — lowercase ASCII + digits + underscore,
# bounded to 32 chars so it embeds cleanly in logs, parquet column names,
# and future broker-side ``orderRef`` tags without escaping.
_LEG_ID_PATTERN = r"^[a-z0-9_]{1,32}$"


class StockInstrument(BaseModel):
    kind: Literal["stock"]
    underlying: Annotated[str, Field(min_length=1)]


class OptionInstrument(BaseModel):
    kind: Literal["option"]
    underlying: Annotated[str, Field(min_length=1)]


# ---- Strike selectors (Slice 1C) ------------------------------------------


class AtmStrike(BaseModel):
    selector: Literal["atm"]


class AtmOffsetStrike(BaseModel):
    selector: Literal["atm_offset"]
    offset: int


class AbsoluteStrike(BaseModel):
    """Broker-derived strike — Slice 1F (#605).

    Emitted by the cockpit's option-leg-picker after the operator drills
    down through ``broker.expirations`` → ``broker.strikes`` → call/put
    and ``broker.searchOptionContracts`` qualifies the choice. The
    strike is the concrete number IBKR listed; ``run_id`` hashes the
    operator's exact pick, not a relative selector that might resolve
    differently at consumption time.
    """

    selector: Literal["absolute"]
    strike: float = Field(gt=0)


# Slice 6 will land a ``DeltaStrike`` variant once the chain-lookup
# resolver is in place. Deliberately absent until then so an operator
# cannot deploy a plan the engine cannot run (ADR 0012 §"Anti-patterns").
StrikeSelector = Annotated[
    AtmStrike | AtmOffsetStrike | AbsoluteStrike,
    Field(discriminator="selector"),
]


# ---- Expiry selectors (Slice 1C) ------------------------------------------


class MinDteExpiry(BaseModel):
    selector: Literal["min_dte"]
    days: Annotated[int, Field(ge=1)]


class NearestWeeklyExpiry(BaseModel):
    selector: Literal["nearest_weekly"]


class AbsoluteExpiry(BaseModel):
    selector: Literal["absolute"]
    # ``int64`` ms UTC per the repo timestamp policy. Display conversion
    # to ``America/New_York`` lives at the UI boundary, not here.
    expiration_ms: int


ExpirySelector = Annotated[
    MinDteExpiry | NearestWeeklyExpiry | AbsoluteExpiry,
    Field(discriminator="selector"),
]


# ---- Entry-leg variants ---------------------------------------------------


class StockEntryLeg(BaseModel):
    """Stock ``ActionEntity`` — Slice 1B."""

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    instrument: StockInstrument
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1)]


class OptionEntryLeg(BaseModel):
    """Option ``ActionEntity`` — Slice 1C.

    Adds ``right``, ``strike``, ``expiry`` on top of the common entry-leg
    fields. ``leg_id`` is the same stable identity stock legs carry —
    each option leg of a multi-leg structure resolves to its own
    ``conId`` at consumption time, even when several legs share the same
    expiry-selector inputs.
    """

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    instrument: OptionInstrument
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1)]
    right: Literal["call", "put"]
    strike: StrikeSelector
    expiry: ExpirySelector


def _entry_leg_kind(v: object) -> str | None:
    """Pull the nested ``instrument.kind`` discriminator for the
    ``ActionEntity`` union. Pydantic returns a clear "unknown
    discriminator value" error when this returns an unexpected string,
    and a "missing discriminator field" error when it returns ``None``."""

    if isinstance(v, dict):
        instrument = v.get("instrument")
        if isinstance(instrument, dict):
            return instrument.get("kind")
        return None
    instrument = getattr(v, "instrument", None)
    return getattr(instrument, "kind", None)


# Discriminated on ``instrument.kind`` so a malformed option leg cannot
# silently fall back to the stock variant. Slice 1B = stock only;
# Slice 1C adds the option variant.
ActionEntity = Annotated[
    Annotated[StockEntryLeg, Tag("stock")] | Annotated[OptionEntryLeg, Tag("option")],
    Discriminator(_entry_leg_kind),
]


class CloseLegExit(BaseModel):
    """``close_leg`` ``ExitEntity`` — Slice 1B."""

    kind: Literal["close_leg"]
    entry_leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]


class ParityWarning(BaseModel):
    """Wire shape for a single parity warning — Slice 1D (#597).

    Produced by ``app.engine.action_plan.parity.parity_diagnostics`` and
    exposed via ``POST /api/live-instances/preview-action-plan``. Codes
    extend as new diagnostic kinds land (e.g. asymmetric position
    direction). The picker keys its inline-error renderer off ``code``.
    """

    code: Literal["orphan_entry"]
    message: str
    leg_id: str | None = None


class ActionPlanPreviewResponse(BaseModel):
    """Response envelope for the preview endpoint. Stable shape: the
    response is always ``{warnings: [...]}`` so adding warning kinds
    never requires the client to re-discriminate on top-level keys."""

    warnings: list[ParityWarning]


class ActionPlan(BaseModel):
    """Operator-declared instrument plan, hashed into ``run_id``.

    Slice 1A shipped the empty-plan envelope; Slice 1B added stock entry
    legs + ``close_leg`` exits; Slice 1C adds option entry legs and the
    strike / expiry selector unions. Hard schema rejections live here.
    Parity *warnings* (asymmetric structures, etc.) are computed
    separately by ``parity_diagnostics`` in Slice 1D (#597).
    """

    model_config = ConfigDict(extra="forbid")

    on_enter: list[ActionEntity] = Field(default_factory=list)
    on_exit: list[CloseLegExit] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_referential_integrity(self) -> ActionPlan:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for leg in self.on_enter:
            if leg.leg_id in seen:
                duplicates.add(leg.leg_id)
            seen.add(leg.leg_id)
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
