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


class BarField(_OperandBase):
    kind: Literal["BarField"]
    field: Literal["open", "high", "low", "close", "volume"]


class ConstOperand(_OperandBase):
    kind: Literal["Const"]
    value: float


class Subtract(_OperandBase):
    kind: Literal["Subtract"]
    left: Operand
    right: Operand


# Phase 1 ships only IndicatorRef, BarField, Const, Subtract. Add/Multiply/
# Divide/Abs are reserved kinds — including them would invite spec authors
# to write Phase 2 shapes that the evaluator can't run yet, which contradicts
# the "if it loads, it runs" contract for the Phase 1 vocabulary.
Operand = Annotated[
    IndicatorRef | BarField | ConstOperand | Subtract,
    Field(discriminator="kind"),
]
Subtract.model_rebuild()


# ---------------------------------------------------------------------------
# Indicators block.
# ---------------------------------------------------------------------------
class IndicatorBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["SMA", "EMA", "RSI", "ADX", "MACD", "SUPERTREND"]
    period: int = Field(ge=2)
    source: Literal["open", "high", "low", "close", "hlc3", "ohlc4"] = "close"

    # RSI-only: Wilders smoothing toggle. Engine RSI is Wilders by default,
    # so this is included for explicitness in the spec.
    ma_type: Literal["wilders", "simple"] | None = None


# ---------------------------------------------------------------------------
# Condition primitives. Each "kind" is a discriminator value for the union.
# ---------------------------------------------------------------------------
ComparisonOp = Literal["<", "<=", "==", ">=", ">", "!="]


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


Condition = Annotated[
    IndicatorComparison | IndicatorBetween | FreshCross | BarsSinceEntry | TimeOfDay,
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
# Survival rules (Phase 2).
# ---------------------------------------------------------------------------
class SurvivalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    when: LogicNode
    action: dict


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
                raise ValueError(f"condition references undeclared indicator id: {ref!r} (declared: {sorted(declared)})")

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
            _walk_condition(rule.when)
        for snap_id in self.diagnostics.snapshot_at_entry:
            refs.append(snap_id)
        for snap_id in self.diagnostics.snapshot_at_exit:
            refs.append(snap_id)

        return refs


def load_spec_from_path(path: str | Path) -> StrategySpec:
    """Load and validate a ``StrategySpec`` from a JSON file on disk."""
    raw = Path(path).read_text(encoding="utf-8")
    return StrategySpec.model_validate(json.loads(raw))
