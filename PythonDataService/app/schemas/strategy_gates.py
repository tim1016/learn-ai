"""Wire shapes for custom Dark Bright Gates (#2639 D8–D10).

A custom gate is one linear expression over the strategy view's variables,
tested > 0 or < 0. It is saved on its strategy, once for every bot and for
Strategy Lab, and it only shades candles; it never trades.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

GateSign = Literal["gt", "lt"]
# A gate's numbers are finite: an infinite coefficient would be written to the
# shared gate file as null and break every strategy's gates.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class GateTerm(BaseModel):
    """One ``coefficient · variable`` term of a gate's linear expression."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coefficient: FiniteFloat
    variable: str = Field(min_length=1)


class CustomGateInput(BaseModel):
    """What the owner types: a name, the expression and the side of zero that is bright."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    expression: str = Field(min_length=1, max_length=400)
    sign: GateSign


class CustomGate(BaseModel):
    """A saved gate: the owner's text plus its validated linear form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str = Field(pattern=r"^g-[0-9a-f]{12}$")
    strategy_key: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=60)
    expression: str = Field(min_length=1, max_length=400)
    sign: GateSign
    terms: list[GateTerm]
    constant: FiniteFloat
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    updated_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


class StrategyGateList(BaseModel):
    """Every gate saved on one strategy, oldest first."""

    model_config = ConfigDict(frozen=True)

    strategy_key: str
    gates: list[CustomGate]


class GateCandle(BaseModel):
    """One decision candle a gate is judged on: its OHLCV and the bot's values by key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bar_close_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    open: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    close: FiniteFloat
    volume: FiniteFloat = Field(ge=0)
    values: dict[str, FiniteFloat | None] = Field(default_factory=dict)


class GateEvaluationRequest(BaseModel):
    """Judge a strategy's saved gates (and an unsaved draft) on these candles.

    ``settings`` are the deployed settings the candles were decided under; a
    gate naming a setting reads it from here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    settings: dict[str, FiniteFloat | int | str | bool | None] = Field(default_factory=dict)
    candles: list[GateCandle] = Field(max_length=20_000)
    draft: CustomGateInput | None = None


class GateEvaluationResponse(BaseModel):
    """Each gate's result per candle, in candle order, plus where its numbers came from.

    ``results`` maps a gate id (``"draft"`` for the unsaved draft) to one
    entry per candle: ``True`` bright, ``False`` dark, ``None`` when a
    variable had no value on that candle. ``chart_computed`` names the
    variables the chart computed from the candles rather than read from the
    bot's records.
    """

    model_config = ConfigDict(frozen=True)

    results: dict[str, list[bool | None]]
    chart_computed: list[str]
    notices: list[str] = Field(default_factory=list)


class GateCatalogueEntry(BaseModel):
    """A catalogue indicator a gate can read, and how a gate writes it."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    # ``EMA10``: the name at its default length, or the bare name when it takes none.
    variable: str
    default_length: int | None
    min_length: int | None
    max_length: int | None


class GateCatalogue(BaseModel):
    """Every catalogue indicator a gate can read: one line, no setting or only a length."""

    model_config = ConfigDict(frozen=True)

    indicators: list[GateCatalogueEntry]


class GateRefusal(BaseModel):
    """Why a gate was not saved or judged, in the owner's words."""

    model_config = ConfigDict(frozen=True)

    code: Literal["GATE_EXPRESSION_REFUSED", "GATE_NOT_FOUND", "GATE_STORE_UNAVAILABLE", "STRATEGY_VIEW_UNAVAILABLE"]
    message: str


class GateRefusalBody(BaseModel):
    """A refused gate request's response body, as FastAPI wraps an ``HTTPException`` detail."""

    model_config = ConfigDict(frozen=True)

    detail: GateRefusal


class GateRequestInvalidBody(BaseModel):
    """A request body that failed validation, as FastAPI reports it."""

    model_config = ConfigDict(frozen=True)

    detail: list[dict[str, Any]]


__all__ = [
    "CustomGate",
    "CustomGateInput",
    "GateCandle",
    "GateCatalogue",
    "GateCatalogueEntry",
    "GateEvaluationRequest",
    "GateEvaluationResponse",
    "GateRefusal",
    "GateRefusalBody",
    "GateRequestInvalidBody",
    "GateSign",
    "GateTerm",
    "StrategyGateList",
]
