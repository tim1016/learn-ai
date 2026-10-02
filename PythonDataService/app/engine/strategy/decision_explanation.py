"""The values and checks a strategy reports alongside each decision (#2639).

A decision explanation is what a bar's decision *saw*: the indicator values it
read and every entry or exit rule it applied, each with its threshold, the
value observed and whether it passed. The strategy builds it in the same
``evaluate_signal_bar`` call that decides, from the same locals, so a check
shown to the owner can never disagree with the decision it explains.

It is deliberately not part of the Evaluation Trace. ``SignalSession`` copies
named ``SignalDecision`` fields into the trace; it never copies this one, so
the trace digest, the golden trace roots and every sealed bot stay
byte-identical (ADR 0042, ADR 0043). Labels, units and formatting live on the
strategy's registered view (``registry.StrategyView``), not here: this module
carries numbers and pass/fail only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class CheckRole(StrEnum):
    """Which side of a position a rule governs."""

    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True)
class ExplainedCheck:
    """One rule as the decision applied it on one bar.

    ``observed`` is the number the rule compared, or a short state token for
    a rule that is not a comparison (``"crossed_up"``). ``threshold`` is the
    bound the strategy used -- already resolved from its deployed settings --
    or an inclusive ``(low, high)`` band; ``None`` for a state rule.
    """

    check_id: str
    role: CheckRole
    passed: bool
    observed: Decimal | int | str | None
    threshold: Decimal | int | tuple[Decimal, Decimal] | None = None


@dataclass(frozen=True)
class DecisionExplanation:
    """Named indicator values plus the rules one decision bar was judged by.

    ``holding`` says whether the strategy held a position when the bar
    arrived: its entry rules decide only while flat and its exit rules only
    while holding. A strategy still reports the rules that did not apply on a
    bar when it computes them anyway, so a rule-based gate can shade every
    candle; ``holding`` is what tells a reader which side acted.
    """

    values: dict[str, Decimal | None]
    checks: tuple[ExplainedCheck, ...] = field(default_factory=tuple)
    holding: bool = False

    def entry_checks_pass(self) -> bool:
        """Whether every reported entry rule passed: entry rules are joined by AND."""
        entry = [check for check in self.checks if check.role is CheckRole.ENTRY]
        return bool(entry) and all(check.passed for check in entry)

    def exit_check_fires(self) -> bool:
        """Whether any reported exit rule passed: exit rules are joined by OR."""
        return any(check.passed for check in self.checks if check.role is CheckRole.EXIT)


__all__ = ["CheckRole", "DecisionExplanation", "ExplainedCheck"]
