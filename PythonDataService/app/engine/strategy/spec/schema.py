"""Pydantic v2 models for the declarative ``StrategySpec`` schema.

The schema is the single source of truth for the spec format. Pydantic
generates the JSON Schema via ``StrategySpec.model_json_schema()``; the
evaluator consumes validated models, so any spec that loads is guaranteed
to be structurally well-formed before a single bar is processed.

Phase 1 hard boundaries (validator-enforced, not runtime-enforced):
  * single-symbol — ``len(symbols) == 1``
  * no survival actions — ``survival == []``
  * equity-only position — ``position.kind == "EQUITY_LONG"``

The schema admits richer Phase 2 shapes (multi-leg options, survival
actions, nested AND/OR groups). A spec authored against Phase 2 features
will pass model_validate today and fail at the Phase 1 evaluator boundary
with a descriptive ``NotImplementedError`` — this is intentional, so spec
authors can write forward-compatible JSON without round-tripping it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Operand AST — typed expression tree for the left/right side of comparisons.
#
# No ``"expr": "ema5 - ema10"`` strings anywhere. Storage is the AST; UIs may
# render the AST as text, but the serialized form is structural.
# ---------------------------------------------------------------------------
class _OperandBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndicatorRef(_OperandBase):
    kind: Literal["IndicatorRef"]
    indicator: str  # must match an id in the indicators block


class ConstOperand(_OperandBase):
    kind: Literal["Const"]
    value: float


class Subtract(_OperandBase):
    kind: Literal["Subtract"]
    left: Operand
    right: Operand


# Phase 1 ships only IndicatorRef, Const, Subtract — every operand kind
# the evaluator can actually run. Add/Multiply/Divide/Abs and BarField
# (raw OHLCV references) are reserved for Phase 2; they are deliberately
# absent from the union so a spec that loads is also a spec that runs,
# rather than a spec that schema-validates and then crashes mid-backtest.
Operand = Annotated[
    IndicatorRef | ConstOperand | Subtract,
    Field(discriminator="kind"),
]
Subtract.model_rebuild()


# ---------------------------------------------------------------------------
# Indicators block.
# ---------------------------------------------------------------------------
class IndicatorBlock(BaseModel):
    """Declarative indicator description.

    The ``period`` field is the primary period for every supported kind:
      * SMA / EMA: window length
      * RSI: window length (Wilders smoothing)
      * ADX: Wilder DI / DX period (warmup = 2 × period)
      * MACD: ``slow_period``; ``fast_period`` and ``signal_period``
        default to 12 and 9 if omitted
      * SUPERTREND: ATR period; ``multiplier`` defaults to 3.0

    For SMA/EMA/RSI/MACD (single-price indicators) ``source`` selects which
    bar field feeds the indicator. ADX and SUPERTREND consume full OHLC
    bars and ignore ``source``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["SMA", "EMA", "RSI", "ADX", "MACD", "SUPERTREND"]
    period: int = Field(ge=2)
    source: Literal["open", "high", "low", "close", "hlc3", "ohlc4"] = "close"

    # RSI-only: Wilders smoothing toggle. Engine RSI is Wilders by default,
    # so this is included for explicitness in the spec.
    ma_type: Literal["wilders", "simple"] | None = None

    # MACD-only: classical MACD has three knobs. ``period`` carries
    # ``slow_period``; the other two are optional with defaults matching
    # the LEAN / Pine convention (12 / 26 / 9).
    fast_period: int | None = Field(default=None, ge=2)
    signal_period: int | None = Field(default=None, ge=2)

    # SUPERTREND-only: ATR-band multiplier. Defaults to 3.0 if omitted.
    multiplier: float | None = Field(default=None, gt=0.0)


# ---------------------------------------------------------------------------
# Condition primitives. Each "kind" is a discriminator value for the union.
# ---------------------------------------------------------------------------
ComparisonOp = Literal["<", "<=", "==", ">=", ">", "!="]
PredictionLookup = Literal["exact_bar_close", "next_after_bar_close"]


class _ConditionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndicatorComparison(_ConditionBase):
    kind: Literal["IndicatorComparison"]
    left: Operand
    op: ComparisonOp
    right: Operand


class IndicatorBetween(_ConditionBase):
    kind: Literal["IndicatorBetween"]
    indicator: str
    lo: float
    hi: float
    inclusive: bool = True

    @model_validator(mode="after")
    def _check_bounds(self) -> IndicatorBetween:
        if self.lo >= self.hi:
            raise ValueError(f"IndicatorBetween: require lo < hi (got lo={self.lo}, hi={self.hi})")
        return self


class FreshCross(_ConditionBase):
    kind: Literal["FreshCross"]
    left: str  # indicator id
    right: str  # indicator id
    direction: Literal["up", "down"]


class BarsSinceEntry(_ConditionBase):
    kind: Literal["BarsSinceEntry"]
    op: ComparisonOp
    value: int = Field(ge=0)


class TimeOfDay(_ConditionBase):
    kind: Literal["TimeOfDay"]
    after: str | None = None  # "HH:MM"
    before: str | None = None  # "HH:MM"
    tz: str = "America/New_York"


class PnLPercent(_ConditionBase):
    """Compares the open trade's unrealized PnL (as fraction of entry price)
    against a threshold. Read as ``(current_close - entry_price) / entry_price``.
    Values are unitless fractions, *not* percent — use ``-0.01`` for "-1%".

    Returns False when no position is open. Survival rules using this
    primitive are the standard form of stop-loss / profit-target in the
    spec layer.
    """

    kind: Literal["PnLPercent"]
    op: ComparisonOp
    value: float


class PnLPoints(_ConditionBase):
    """Compares the open trade's unrealized PnL in price points
    (``current_close - entry_price``) against a threshold.

    Returns False when no position is open. Use ``PnLPercent`` for
    risk-adjusted thresholds; use ``PnLPoints`` when the threshold has
    natural absolute units (e.g., a fixed-dollar stop on a known asset).
    """

    kind: Literal["PnLPoints"]
    op: ComparisonOp
    value: float


class DrawdownFromPeak(_ConditionBase):
    """Trailing-stop primitive — fires when the current close has
    retraced from the peak-since-entry by at least ``value``.

    The peak is tracked from the entry fill onwards (resets on exit).
    ``value`` is a non-negative fraction: ``0.005`` means "fired when
    we've given back 0.5% from the high since entry".

    Returns False when no position is open or before the entry fills
    (peak is undefined until then). Stateful primitive — internal peak
    state is reset by ``observe_bar`` when the position is flat.
    """

    kind: Literal["DrawdownFromPeak"]
    value: float = Field(ge=0.0)


class BarProperty(_ConditionBase):
    """Compares a bar-derived property to a threshold. Stateless.

    Properties:
      * ``range`` — high - low (price points)
      * ``body`` — abs(close - open) (price points)
      * ``range_pct`` — (high - low) / close (unitless fraction)
      * ``body_pct`` — abs(close - open) / close (unitless fraction)
    """

    kind: Literal["BarProperty"]
    property: Literal["range", "body", "range_pct", "body_pct"]
    op: ComparisonOp
    value: float


class PredictionRef(BaseModel):
    """Spec-local handle bound to one column of a prediction set artifact.

    ``id`` is referenced by ``PredictionComparison.prediction``. ``field``
    is the column name in the artifact rows (default ``"prediction"`` for
    the v0.5 single-scalar contract; reserved for future multi-column
    artifacts).

    ``lookup`` selects the evaluator's row-selection policy at decision
    time. ``"exact_bar_close"`` (default) reads the row keyed at the
    consolidated bar's ``end_time_ms``. ``"next_after_bar_close"`` reads
    the row with the smallest timestamp strictly greater than the bar's
    ``end_time_ms`` — used for "consume tomorrow's prediction at today's
    close" strategies like QC's precomputed-predictions tutorial.
    Coverage validation (``app.research.ml.coverage``) is lookup-aware
    and fails at run-pipeline boundary if any fired bar lacks the
    required successor row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    prediction_set_id: str
    field: str = "prediction"
    lookup: PredictionLookup = "exact_bar_close"


class PredictionComparison(_ConditionBase):
    """Compare a per-bar prediction value against a constant threshold."""

    kind: Literal["PredictionComparison"]
    prediction: str  # PredictionRef.id
    op: ComparisonOp
    value: float


Condition = Annotated[
    IndicatorComparison
    | IndicatorBetween
    | FreshCross
    | BarsSinceEntry
    | TimeOfDay
    | PnLPercent
    | PnLPoints
    | DrawdownFromPeak
    | BarProperty
    | PredictionComparison,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Logic node — a tree of AND/OR groups whose leaves are conditions or other
# logic nodes. The schema permits arbitrary depth; the Phase 1 UI will only
# render the flat top-level form.
# ---------------------------------------------------------------------------
class LogicNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logic: Literal["AND", "OR"]
    conditions: list[Condition | LogicNode]


LogicNode.model_rebuild()


# ---------------------------------------------------------------------------
# Position sizing.
# ---------------------------------------------------------------------------
class SetHoldings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["SetHoldings"]
    fraction: float = Field(gt=0.0, le=1.0)


class FixedContracts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["FixedContracts"]
    value: int = Field(ge=1)


SizeRule = Annotated[SetHoldings | FixedContracts, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Position structure.
# ---------------------------------------------------------------------------
class EquityLongPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["EQUITY_LONG"]


class OptionTemplatePosition(BaseModel):
    """Phase 2 placeholder. Schema admits the shape so authors can write
    forward-compatible specs; the Phase 1 evaluator raises
    ``NotImplementedError`` if it sees one."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["OPTION_TEMPLATE"]
    template: str
    expiration: dict | None = None
    legs: list[dict] = Field(default_factory=list)
    filters: dict | None = None


PositionSpec = Annotated[
    EquityLongPosition | OptionTemplatePosition,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Survival actions — what a survival rule does when its `when` fires.
# Phase 2.1 ships CLOSE_ALL only. Phase 2 reserves CLOSE_FRACTION,
# ROLL_OPTION_LEG, TIGHTEN_STOP, LOG.
# ---------------------------------------------------------------------------
class CloseAllAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["CLOSE_ALL"]


SurvivalAction = Annotated[CloseAllAction, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Survival rules.
# ---------------------------------------------------------------------------
class SurvivalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    when: LogicNodeOrConditions
    action: SurvivalAction


class LogicNodeOrConditions(BaseModel):
    """A flat ``{logic, conditions}`` block — the same shape as a top-level
    entry/exit block — used by survival rules.

    Survival rules may use either a flat block or a nested ``LogicNode``;
    Phase 2.1 accepts the flat shape (simpler authoring) and treats it
    as an implicit single-level group.
    """

    model_config = ConfigDict(extra="forbid")
    logic: Literal["AND", "OR"]
    conditions: list[Condition | LogicNode]


# ---------------------------------------------------------------------------
# Lifecycle blocks.
# ---------------------------------------------------------------------------
class EntryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logic: Literal["AND", "OR"]
    conditions: list[Condition | LogicNode]
    size: SizeRule
    pyramiding: int = Field(default=1, ge=1)


class ExitBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logic: Literal["AND", "OR"]
    conditions: list[Condition | LogicNode]


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_minutes: int = Field(ge=1)


class Diagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_at_entry: list[str] = Field(default_factory=list)
    snapshot_at_exit: list[str] = Field(default_factory=list)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    name: str
    description: str | None = None
    symbols: list[str]
    resolution: Resolution
    indicators: list[IndicatorBlock] = Field(default_factory=list)
    predictions: list[PredictionRef] = Field(default_factory=list)
    entry: EntryBlock
    position: PositionSpec = Field(default_factory=lambda: EquityLongPosition(kind="EQUITY_LONG"))
    survival: list[SurvivalRule] = Field(default_factory=list)
    exit: ExitBlock
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)

    @model_validator(mode="after")
    def _check_phase1_boundaries(self) -> StrategySpec:
        # Phase 1: single-symbol only. The engine itself only supports a
        # single trading symbol per strategy today (see engine.py guards),
        # so we reject up-front with a clear message rather than letting
        # the engine fail on the first set_holdings call.
        if len(self.symbols) != 1:
            raise ValueError(f"Phase 1: single-symbol only (got {len(self.symbols)} symbols: {self.symbols})")

        # Phase 1: no survival actions. Forward-compatible specs may include
        # them; the evaluator will refuse to run them.
        # (Validation is intentionally not strict here — the schema admits
        # the shape and the evaluator does the runtime refusal — so users can
        # author forward-compatible specs without round-trip failures.)

        # Indicator id uniqueness.
        ids = [ind.id for ind in self.indicators]
        if len(ids) != len(set(ids)):
            dup = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"duplicate indicator ids: {sorted(set(dup))}")

        # Validate that every indicator-id reference inside conditions and
        # diagnostics points at a declared indicator. Catches typos at load
        # time instead of at first evaluate.
        declared = set(ids)
        for ref in self._iter_indicator_refs():
            if ref not in declared:
                raise ValueError(
                    f"condition references undeclared indicator id: {ref!r} (declared: {sorted(declared)})"
                )

        # ---- prediction validators -------------------------------------
        # Deferred import to avoid a circular import:
        # schema -> artifact -> runs.hashing -> runs.__init__ -> runs.runner -> spec.__init__ -> schema
        from app.research.ml.artifact import is_path_safe_id  # deferred: breaks circular import at module level

        pred_ids = [p.id for p in self.predictions]
        if len(pred_ids) != len(set(pred_ids)):
            dup = [i for i in pred_ids if pred_ids.count(i) > 1]
            raise ValueError(f"duplicate prediction ref ids: {sorted(set(dup))}")

        for p in self.predictions:
            if not is_path_safe_id(p.prediction_set_id):
                raise ValueError(
                    f"prediction_set_id {p.prediction_set_id!r} on ref {p.id!r} "
                    f"must be path-safe (no slashes, no traversal)"
                )

        distinct_set_ids = {p.prediction_set_id for p in self.predictions}
        if len(distinct_set_ids) > 1:
            raise ValueError(
                f"v0.5 admits at most one prediction_set_id per spec "
                f"(got {sorted(distinct_set_ids)}). v1.2 lifts this with "
                f"prediction_set_hashes: dict[str, str]."
            )

        declared_pred_ids = set(pred_ids)
        for ref_id in self._iter_prediction_refs():
            if ref_id not in declared_pred_ids:
                raise ValueError(
                    f"condition references undeclared prediction id: {ref_id!r} (declared: {sorted(declared_pred_ids)})"
                )

        return self

    # -- helpers -------------------------------------------------------------
    def _iter_indicator_refs(self) -> list[str]:
        """Walk the logic tree and collect every indicator-id reference."""
        refs: list[str] = []

        def _walk_operand(op: Operand) -> None:
            if isinstance(op, IndicatorRef):
                refs.append(op.indicator)
            elif isinstance(op, Subtract):
                _walk_operand(op.left)
                _walk_operand(op.right)
            # BarField and ConstOperand carry no indicator refs.

        def _walk_condition(cond: Condition | LogicNode) -> None:
            if isinstance(cond, LogicNode):
                for child in cond.conditions:
                    _walk_condition(child)
                return
            if isinstance(cond, IndicatorComparison):
                _walk_operand(cond.left)
                _walk_operand(cond.right)
            elif isinstance(cond, IndicatorBetween):
                refs.append(cond.indicator)
            elif isinstance(cond, FreshCross):
                refs.append(cond.left)
                refs.append(cond.right)
            # BarsSinceEntry, TimeOfDay carry no indicator refs.

        for child in self.entry.conditions:
            _walk_condition(child)
        for child in self.exit.conditions:
            _walk_condition(child)
        for rule in self.survival:
            for child in rule.when.conditions:
                _walk_condition(child)
        for snap_id in self.diagnostics.snapshot_at_entry:
            refs.append(snap_id)
        for snap_id in self.diagnostics.snapshot_at_exit:
            refs.append(snap_id)

        return refs

    def _iter_prediction_refs(self) -> list[str]:
        """Walk the logic tree and collect every PredictionComparison.prediction reference."""
        refs: list[str] = []

        def _walk(node: Condition | LogicNode) -> None:
            if isinstance(node, LogicNode):
                for child in node.conditions:
                    _walk(child)
                return
            if isinstance(node, PredictionComparison):
                refs.append(node.prediction)

        for child in self.entry.conditions:
            _walk(child)
        for child in self.exit.conditions:
            _walk(child)
        for rule in self.survival:
            for child in rule.when.conditions:
                _walk(child)
        return refs


def load_spec_from_path(path: str | Path) -> StrategySpec:
    """Load and validate a ``StrategySpec`` from a JSON file on disk."""
    raw = Path(path).read_text(encoding="utf-8")
    return StrategySpec.model_validate(json.loads(raw))
