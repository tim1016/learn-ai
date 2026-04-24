"""Indicator base class mirroring LEAN's Indicators/IndicatorBase.cs.

The key properties for strategy logic are:
  * ``is_ready``: True once enough samples have been received. The strategy
    should guard on this before using the indicator value.
  * ``current_value``: The latest computed value. Accessed as a property to
    match LEAN's ``Current.Value``.
  * ``samples``: Number of distinct timestamps the indicator has seen.
  * ``update(time, value)``: Push a new data point.

LEAN's IndicatorBase deduplicates updates with identical timestamps. We do
the same: a repeated timestamp does not increment ``samples`` and does not
recompute the value.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal


class Indicator(ABC):
    """Base class for streaming indicators.

    Subclasses implement ``_compute_next_value`` which receives the current
    (time, price) pair and returns either the new indicator value or None if
    not yet computable.
    """

    def __init__(self, name: str, period: int) -> None:
        self.name = name
        self.period = period
        self.samples: int = 0
        self._current_value: Decimal | None = None
        self._current_time: datetime | None = None
        self._previous_value: Decimal | None = None
        self._previous_time: datetime | None = None

    @property
    def current_value(self) -> Decimal | None:
        return self._current_value

    @property
    def current_time(self) -> datetime | None:
        return self._current_time

    @property
    def previous_value(self) -> Decimal | None:
        return self._previous_value

    @property
    def is_ready(self) -> bool:
        return self.samples >= self.period

    def update(self, time: datetime, value: Decimal) -> bool:
        """Push a new data point. Returns True if the value was consumed.

        Duplicate timestamps (same instant as the last update) are silently
        dropped — this matches LEAN's deduplication in IndicatorBase.
        """
        if not isinstance(value, Decimal):
            # Coerce to Decimal to avoid float drift inside recursive formulas.
            value = Decimal(str(value))
        if self._current_time is not None and time <= self._current_time:
            # Stale or duplicate — skip.
            return False
        self.samples += 1
        new_value = self._compute_next_value(time, value)
        if new_value is not None:
            self._previous_value = self._current_value
            self._previous_time = self._current_time
            self._current_value = new_value
            self._current_time = time
        return True

    def reset(self) -> None:
        self.samples = 0
        self._current_value = None
        self._current_time = None
        self._previous_value = None
        self._previous_time = None
        self._reset_state()

    @abstractmethod
    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        """Compute and return the new indicator value, or None."""
        raise NotImplementedError

    def _reset_state(self) -> None:
        """Override to clear subclass-specific state."""


class BarIndicator(ABC):
    """Base class for streaming indicators that consume full OHLCV bars.

    Mirrors LEAN's ``BarIndicator<IBaseDataBar>``. Same contract as
    ``Indicator`` (samples, is_ready, current_value) but ``update`` takes
    a TradeBar instead of a single close price. Subclasses implement
    ``_compute_next_value`` which receives the bar and returns the new
    indicator value or None if not yet computable.
    """

    def __init__(self, name: str, period: int) -> None:
        self.name = name
        self.period = period
        self.samples: int = 0
        self._current_value: Decimal | None = None
        self._current_time: datetime | None = None
        self._previous_value: Decimal | None = None
        self._previous_time: datetime | None = None

    @property
    def current_value(self) -> Decimal | None:
        return self._current_value

    @property
    def current_time(self) -> datetime | None:
        return self._current_time

    @property
    def previous_value(self) -> Decimal | None:
        return self._previous_value

    @property
    def is_ready(self) -> bool:
        return self.samples >= self.period

    def update(self, bar: object) -> bool:
        """Push a new bar. Returns True if consumed.

        Duplicate or out-of-order bars (same or earlier ``end_time``) are
        silently dropped, matching the ``Indicator`` base behavior.
        """
        end_time = bar.end_time  # type: ignore[attr-defined]
        if self._current_time is not None and end_time <= self._current_time:
            return False
        self.samples += 1
        new_value = self._compute_next_value(bar)
        if new_value is not None:
            self._previous_value = self._current_value
            self._previous_time = self._current_time
            self._current_value = new_value
            self._current_time = end_time
        return True

    def reset(self) -> None:
        self.samples = 0
        self._current_value = None
        self._current_time = None
        self._previous_value = None
        self._previous_time = None
        self._reset_state()

    @abstractmethod
    def _compute_next_value(self, bar: object) -> Decimal | None:
        """Compute and return the new indicator value, or None."""
        raise NotImplementedError

    def _reset_state(self) -> None:
        """Override to clear subclass-specific state."""
