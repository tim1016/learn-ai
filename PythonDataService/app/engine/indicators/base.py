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
from datetime import UTC, datetime
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
    def previous_time(self) -> datetime | None:
        return self._previous_time

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

    def to_state_dict(self) -> dict:
        """Serialize the indicator's persistable state to a JSON-safe dict.

        Common fields are produced by this base method; subclasses with
        additional state override ``_to_state_extra`` to merge their
        own keys. Decimals serialize as quoted strings; timestamps as
        int64 ms UTC.
        """
        return {
            "name": self.name,
            "period": self.period,
            "samples": self.samples,
            "current_value": _decimal_to_str(self._current_value),
            "current_time_ms": _datetime_to_ms(self._current_time),
            "previous_value": _decimal_to_str(self._previous_value),
            "previous_time_ms": _datetime_to_ms(self._previous_time),
            **self._to_state_extra(),
        }

    def restore_state(self, state: dict) -> None:
        """Restore from a dict produced by ``to_state_dict``.

        Raises ``ValueError`` on identity mismatch (different name or
        period) OR on a missing required key. Subclasses override
        ``_restore_state_extra`` to consume their own keys.
        """
        try:
            if state["name"] != self.name:
                raise ValueError(f"name mismatch: state={state['name']!r} self={self.name!r}")
            if state["period"] != self.period:
                raise ValueError(f"period mismatch: state={state['period']} self={self.period}")
            self.samples = int(state["samples"])
            self._current_value = _str_to_decimal(state["current_value"])
            self._current_time = _ms_to_datetime(state["current_time_ms"])
            self._previous_value = _str_to_decimal(state["previous_value"])
            self._previous_time = _ms_to_datetime(state["previous_time_ms"])
        except KeyError as exc:
            raise ValueError(f"restore_state: missing required key {exc} in state dict") from exc
        self._restore_state_extra(state)

    def _to_state_extra(self) -> dict:
        """Override in subclasses to add subclass-specific fields."""
        return {}

    def _restore_state_extra(self, state: dict) -> None:
        """Override in subclasses to consume subclass-specific fields."""


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
    def previous_time(self) -> datetime | None:
        return self._previous_time

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

    def to_state_dict(self) -> dict:
        """Serialize the indicator's persistable state to a JSON-safe dict.

        Common fields are produced by this base method; subclasses with
        additional state override ``_to_state_extra`` to merge their
        own keys. Decimals serialize as quoted strings; timestamps as
        int64 ms UTC.
        """
        return {
            "name": self.name,
            "period": self.period,
            "samples": self.samples,
            "current_value": _decimal_to_str(self._current_value),
            "current_time_ms": _datetime_to_ms(self._current_time),
            "previous_value": _decimal_to_str(self._previous_value),
            "previous_time_ms": _datetime_to_ms(self._previous_time),
            **self._to_state_extra(),
        }

    def restore_state(self, state: dict) -> None:
        """Restore from a dict produced by ``to_state_dict``.

        Raises ``ValueError`` on identity mismatch (different name or
        period) OR on a missing required key. Subclasses override
        ``_restore_state_extra`` to consume their own keys.
        """
        try:
            if state["name"] != self.name:
                raise ValueError(f"name mismatch: state={state['name']!r} self={self.name!r}")
            if state["period"] != self.period:
                raise ValueError(f"period mismatch: state={state['period']} self={self.period}")
            self.samples = int(state["samples"])
            self._current_value = _str_to_decimal(state["current_value"])
            self._current_time = _ms_to_datetime(state["current_time_ms"])
            self._previous_value = _str_to_decimal(state["previous_value"])
            self._previous_time = _ms_to_datetime(state["previous_time_ms"])
        except KeyError as exc:
            raise ValueError(f"restore_state: missing required key {exc} in state dict") from exc
        self._restore_state_extra(state)

    def _to_state_extra(self) -> dict:
        """Override in subclasses to add subclass-specific fields."""
        return {}

    def _restore_state_extra(self, state: dict) -> None:
        """Override in subclasses to consume subclass-specific fields."""


def _decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _str_to_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _datetime_to_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"_datetime_to_ms received a tz-naive datetime: {value!r}. All indicator timestamps must be tz-aware UTC."
        )
    return int(value.timestamp() * 1000)


def _ms_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, tz=UTC)
