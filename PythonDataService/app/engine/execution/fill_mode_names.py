"""The one parser from a fill mode's request name to :class:`FillMode` (#2599).

Every surface that takes a fill mode by name -- the engine backtest, research
runs, the legacy walk-forward route and spec strategies -- parses it here, so
one spelling means one mode everywhere and an unknown one is refused with the
modes that surface really runs. A surface passes the modes it offers as
``allowed``; the canonical name of a parsed mode is its ``FillMode.value``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.engine.execution.order import FillMode

# What a research request may ask for. ``NEXT_SESSION_OPEN`` is the
# daily-prediction parity path: only the research-run runner offers it.
REQUEST_FILL_MODES: tuple[FillMode, ...] = (
    FillMode.SIGNAL_BAR_CLOSE,
    FillMode.NEXT_BAR_OPEN,
    FillMode.DECISION_MINUTE_OPEN,
)

_ALIASES: dict[str, FillMode] = {
    "signalbarclose": FillMode.SIGNAL_BAR_CLOSE,
    "close": FillMode.SIGNAL_BAR_CLOSE,
    "nextbaropen": FillMode.NEXT_BAR_OPEN,
    "open": FillMode.NEXT_BAR_OPEN,
}
_BY_NAME: dict[str, FillMode] = {**{mode.value: mode for mode in FillMode}, **_ALIASES}


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


def parse_fill_mode(raw: str, *, allowed: Sequence[FillMode] = tuple(FillMode)) -> FillMode:
    """Return the mode ``raw`` names; case, surrounding space and hyphens are not part of the name.

    Raises:
        UnknownFillModeError: ``raw`` names no mode, or one outside ``allowed``.
    """
    mode = _BY_NAME.get(raw.strip().lower().replace("-", "_"))
    if mode is None or mode not in allowed:
        raise UnknownFillModeError(raw, allowed)
    return mode
