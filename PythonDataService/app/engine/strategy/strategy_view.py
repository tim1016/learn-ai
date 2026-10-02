"""A strategy's view: what the owner sees of its decisions, declared once (#2639).

Each registered strategy declares its view on its registration: the values it
records on every decision bar (and where each is drawn), the rules it checks,
and its default Dark Bright Gate. The view is content, not math. Which values
and checks exist is the strategy's code (``decision_explanation``); this
module only names them, places them and says how to word them, so colours and
panes can change without touching a sealed source.

Value labels, variables and gate labels are ``str.format`` templates over the
bot's deployed settings (``{fast_period}``). A check's operator and threshold
are never worded here: they come from what the decision recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ChartParamRef:
    """Reference to one validated strategy parameter in a chart recipe."""

    field: str


@dataclass(frozen=True, slots=True)
class StrategyChartIndicator:
    """Declarative catalogue-indicator recipe owned by the registered strategy."""

    name: str
    params: dict[str, int | float | ChartParamRef]


@dataclass(frozen=True, slots=True)
class ViewValue:
    """One indicator value the strategy records on every decision bar.

    ``pane`` is ``"price"`` to overlay the candles, any other id for a pane of
    its own (values sharing an id share the pane), or ``None`` to list the
    value without drawing it. ``catalogue`` names the catalogue indicator
    this value equals, so a chart-computed twin is never drawn beside the
    bot's own. ``band`` draws reference lines on the value's pane.
    """

    key: str
    label: str
    variable: str
    pane: str | None = "price"
    catalogue: StrategyChartIndicator | None = None
    band: tuple[float | ChartParamRef, float | ChartParamRef] | None = None
    decimals: int = 2


@dataclass(frozen=True, slots=True)
class ViewCheck:
    """How one recorded rule is worded.

    The operator and threshold are never written here: they come from the
    decision's own record (``ExplainedCheck.comparison``/``threshold``), so the
    words cannot claim a comparison the code did not make. This only names
    the rule, sets its precision and, for a ``STATE`` rule, words its states
    and what passing requires.
    """

    check_id: str
    label: str
    chip: str
    decimals: int = 2
    signed: bool = False
    unit: str = ""
    states: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    needs: str = ""


@dataclass(frozen=True, slots=True)
class ViewGate:
    """The strategy's default Dark Bright Gate: bright exactly where one recorded rule passed.

    Reading the rule's own pass/fail is what keeps the shading's edges the
    strategy's: a bar sitting exactly on a threshold is shaded the way the
    bot judged it, with no second comparison anywhere.
    """

    gate_id: str
    label: str
    expression: str
    check_id: str


@dataclass(frozen=True, slots=True)
class StrategyView:
    """Everything the strategy view draws for one strategy, declared once."""

    values: tuple[ViewValue, ...]
    checks: tuple[ViewCheck, ...]
    default_gate: ViewGate

    def __post_init__(self) -> None:
        check_ids = [check.check_id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"a strategy view names a check twice: {check_ids}")
        value_keys = [value.key for value in self.values]
        if len(set(value_keys)) != len(value_keys):
            raise ValueError(f"a strategy view names a value twice: {value_keys}")
        if self.default_gate.check_id not in check_ids:
            raise ValueError(f"default gate reads an undeclared check: {self.default_gate.check_id}")

    def check(self, check_id: str) -> ViewCheck | None:
        """The wording for ``check_id``, or ``None`` when this view does not declare it."""
        return next((check for check in self.checks if check.check_id == check_id), None)

    @property
    def chart_indicators(self) -> tuple[StrategyChartIndicator, ...]:
        """The catalogue twins of the drawn values, once each, in declaration order."""
        twins: list[StrategyChartIndicator] = []
        for value in self.values:
            if value.pane is not None and value.catalogue is not None and value.catalogue not in twins:
                twins.append(value.catalogue)
        return tuple(twins)


__all__ = [
    "ChartParamRef",
    "StrategyChartIndicator",
    "StrategyView",
    "ViewCheck",
    "ViewGate",
    "ViewValue",
]
