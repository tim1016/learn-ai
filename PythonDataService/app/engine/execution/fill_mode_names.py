"""The one parser from a fill mode's request name to :class:`FillMode` (#2599).

Every surface that takes a fill mode by name -- the engine backtest, research
runs, the legacy walk-forward route and spec strategies -- parses it here, so
one spelling means one mode everywhere and an unknown one is refused with the
modes that surface really runs. A surface passes the modes it offers as
``allowed``; the canonical name of a parsed mode is its ``FillMode.value``,
and that is the name a surface passes on, stores and hashes.

It also names two rules that must stay apart: the one research defaults to,
and the one a record saved before it named a fill mode ran under. A fallback
for such a record never follows the default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Literal, get_args

from app.engine.execution.order import FillMode

# What the evidence grade and the walk-forward study fill at when the question
# is "will this survive live?": the earliest price a live order could get.
RESEARCH_DEFAULT_FILL_MODE: Literal["decision_minute_open"] = "decision_minute_open"
# What a run or sweep saved before it recorded its fill mode ran under.
UNRECORDED_FILL_MODE: Literal["signal_bar_close"] = "signal_bar_close"

# What a research request may ask for, as a schema types it and as the parser
# checks it: one list. ``NEXT_SESSION_OPEN`` is the daily-prediction parity
# path: only the research-run runner offers it.
FillModeName = Literal["signal_bar_close", "next_bar_open", "decision_minute_open"]
REQUEST_FILL_MODES: tuple[FillMode, ...] = tuple(FillMode(name) for name in get_args(FillModeName))

# The short names the engine backtest took before the parser was shared. Only
# that surface, and Recency, which runs on it, passes them: with three
# "...open" modes, "open" names nothing on its own.
ENGINE_BACKTEST_ALIASES: Mapping[str, FillMode] = MappingProxyType(
    {
        "signalbarclose": FillMode.SIGNAL_BAR_CLOSE,
        "close": FillMode.SIGNAL_BAR_CLOSE,
        "nextbaropen": FillMode.NEXT_BAR_OPEN,
        "open": FillMode.NEXT_BAR_OPEN,
    }
)
NO_ALIASES: Mapping[str, FillMode] = MappingProxyType({})
_BY_NAME: dict[str, FillMode] = {mode.value: mode for mode in FillMode}


class UnknownFillModeError(ValueError):
    """The name is not a fill mode this surface runs."""

    def __init__(self, raw: str, allowed: Sequence[FillMode]) -> None:
        self.raw = raw
        self.allowed = tuple(allowed)
        super().__init__(f"unknown fill_mode {raw!r} — expected {self.expected}")

    @property
    def expected(self) -> str:
        """The allowed names as prose: ``a, b or c``."""
        *head, last = (mode.value for mode in self.allowed)
        return f"{', '.join(head)} or {last}" if head else last


def parse_fill_mode(
    raw: str,
    *,
    allowed: Sequence[FillMode] = tuple(FillMode),
    aliases: Mapping[str, FillMode] = NO_ALIASES,
) -> FillMode:
    """Return the mode ``raw`` names; case, surrounding space and hyphens are not part of the name.

    ``aliases`` are the extra names a surface takes on top of the modes' own.

    Raises:
        UnknownFillModeError: ``raw`` names no mode, or one outside ``allowed``.
    """
    name = raw.strip().lower().replace("-", "_")
    mode = _BY_NAME.get(name, aliases.get(name))
    if mode is None or mode not in allowed:
        raise UnknownFillModeError(raw, allowed)
    return mode
