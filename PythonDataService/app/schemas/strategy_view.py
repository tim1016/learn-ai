"""Wire shapes for the strategy view (#2639).

One read returns everything the view draws: the strategy's own decision
candles, each with the bot's recorded values and checks (already worded by
the backend) and each gate's result, plus the view declaration that says
which values are lines and where. The frontend renders no hardcoded
indicator name and does no gate arithmetic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.decision_explanation import DecisionSignal
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# How many earlier decision bars a view may carry for catalogue indicators to warm up on (#2800).
MAX_LEAD_IN_BARS = 1_000


class ExplainedValueView(BaseModel):
    """One recorded indicator value, labelled for the bot's settings."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    value: float | None
    text: str


class ExplainedCheckView(BaseModel):
    """One rule as the decision applied it, worded by the backend.

    ``applies`` says whether this rule could act on the bar: entry rules act
    while flat, exit rules while holding. A rule that did not apply is still
    shown, because a gate may shade by it.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str
    role: Literal["entry", "exit"]
    label: str
    chip: str
    observed_text: str
    needs: str
    passed: bool
    applies: bool


class DecisionExplanationView(BaseModel):
    """What one decision bar saw, ready to show."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    holding: bool
    signal: DecisionSignal
    values: list[ExplainedValueView]
    checks: list[ExplainedCheckView]


class CatalogueIndicatorRef(BaseModel):
    """A catalogue indicator with its parameters resolved for these settings."""

    model_config = ConfigDict(frozen=True)

    name: str
    params: dict[str, float]


class StrategyViewValueSpec(BaseModel):
    """Where and how one recorded value is drawn.

    ``pane`` is ``"price"`` for an overlay on the candles, another id for a
    pane of its own, or ``None`` for a value listed but not drawn.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    variable: str
    pane: str | None
    band: list[float] | None
    decimals: int
    catalogue: CatalogueIndicatorRef | None


class StrategyViewGateView(BaseModel):
    """One Dark Bright Gate the view can shade by."""

    model_config = ConfigDict(frozen=True)

    gate_id: str
    label: str
    expression: str
    source: Literal["strategy", "mine"]


class StrategyViewDeclarationView(BaseModel):
    """The strategy's view resolved against one set of deployed settings."""

    model_config = ConfigDict(frozen=True)

    values: list[StrategyViewValueSpec]
    gates: list[StrategyViewGateView]
    default_gate_id: str


class StrategyViewCandle(BaseModel):
    """One decision bar, labelled by its close.

    ``phase`` is ``before_start`` for a warmup bar the bot evaluated but
    never acted on; ``phase_text`` says so in the owner's words. ``gates``
    maps each gate id to whether it held on this bar (``None`` when the
    bar could not be judged, e.g. indicators not ready).
    """

    model_config = ConfigDict(frozen=True)

    bar_start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    bar_close_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    open: float
    high: float
    low: float
    close: float
    volume: float
    phase: Literal["before_start", "decision"]
    phase_text: str | None = None
    outcome: str | None = None
    reason_code: str | None = None
    decision_seq: int | None = None
    explanation: DecisionExplanationView
    gates: dict[str, bool | None]


class LeadInBar(BaseModel):
    """One decision bar from before the view's first candle, labelled by its close.

    A catalogue indicator is computed over these and then the candles, so it
    has warmed up by the first candle. A lead-in bar is never drawn or judged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bar_close_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    open: float = Field(allow_inf_nan=False)
    high: float = Field(allow_inf_nan=False)
    low: float = Field(allow_inf_nan=False)
    close: float = Field(allow_inf_nan=False)
    volume: float = Field(ge=0, allow_inf_nan=False)


class StrategyViewResponse(BaseModel):
    """Everything one bot's strategy view draws, in one read."""

    model_config = ConfigDict(frozen=True)

    strategy_key: str
    strategy_name: str
    symbol: str
    decision_timeframe_ms: int = Field(gt=0)
    run_id: str
    run_started_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    run_stopped_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    declaration: StrategyViewDeclarationView
    # The deployed settings by name: what a custom gate reads ``gap`` or
    # ``rsi_min`` as, echoed back when the page asks for its results.
    settings: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    candles: list[StrategyViewCandle]
    # Earlier decision bars from the same source as the candles, oldest first,
    # each closing before the first candle; empty when the source has none (#2800).
    lead_in: list[LeadInBar] = Field(default_factory=list, max_length=MAX_LEAD_IN_BARS)
    # Decisions this run took before decisions recorded their values.
    unexplained_decision_count: int = Field(default=0, ge=0)
    notices: list[str] = Field(default_factory=list)


__all__ = [
    "MAX_LEAD_IN_BARS",
    "CatalogueIndicatorRef",
    "DecisionExplanationView",
    "ExplainedCheckView",
    "ExplainedValueView",
    "LeadInBar",
    "StrategyViewCandle",
    "StrategyViewDeclarationView",
    "StrategyViewGateView",
    "StrategyViewResponse",
    "StrategyViewValueSpec",
]
