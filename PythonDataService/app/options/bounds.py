"""What a number an owner types may be: its lowest, its highest, and the finest step it is taken in.

A bound is never written a second time. It is read off the field that
validates the number (``bounds_of``), or stated once as a ``Bounds`` by the
module that owns the number and handed to the field from there
(``Bounds.held_by``). A form is served the same ``Bounds``, so it can refuse
a number before it is sent, and a number that slips through is refused in
words that name the same range.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel

BoundsFault = Literal["below", "above", "finer"]


@dataclass(frozen=True, slots=True)
class Bounds:
    """A range a number must lie in. ``None`` on a side: no bound there."""

    lowest: Decimal | None = None
    # False: the number must be above ``lowest``, not at it.
    lowest_included: bool = True
    highest: Decimal | None = None
    highest_included: bool = True
    # The finest step the number is taken in: one, a thousandth, a millionth. None: any number.
    step: Decimal | None = None

    def __post_init__(self) -> None:
        if self.step is not None and self.step.normalize().as_tuple().digits != (1,):
            # A step is read off a number's own digits, which works for a power of ten and nothing else.
            raise ValueError(f"a step is a power of ten; got {self.step}")
        if self.lowest is not None and self.highest is not None and self.lowest > self.highest:
            raise ValueError(f"a range runs from its lowest to its highest; got {self.lowest} to {self.highest}")

    def fault(self, value: Decimal) -> BoundsFault | None:
        """Which way a finite ``value`` is outside these bounds, or ``None`` when it is inside them.

        Nothing here writes the number out or divides by it, so a number of
        any size is judged at once: ``1e999999999`` is above a hundred.
        """
        if not value.is_finite():
            raise ValueError(f"only a finite number is held to a range; got {value}")
        if self.lowest is not None and (value < self.lowest or (value == self.lowest and not self.lowest_included)):
            return "below"
        if self.highest is not None and (value > self.highest or (value == self.highest and not self.highest_included)):
            return "above"
        if self.step is not None and value and value.normalize().as_tuple().exponent < self.step.normalize().as_tuple().exponent:
            return "finer"
        return None

    def held_by(self, kind: type[int] | type[Decimal]) -> tuple[Gt | Ge | Lt | Le, ...]:
        """These bounds as a field of ``kind`` carries them. The step is the field's own to hold: a whole
        number by its type, a decimal by its places."""
        held: list[Gt | Ge | Lt | Le] = []
        if self.lowest is not None:
            held.append(Ge(kind(self.lowest)) if self.lowest_included else Gt(kind(self.lowest)))
        if self.highest is not None:
            held.append(Le(kind(self.highest)) if self.highest_included else Lt(kind(self.highest)))
        return tuple(held)


def _exact(bound: object) -> Decimal:
    return bound if isinstance(bound, Decimal) else Decimal(str(bound))


def bounds_of(model: type[BaseModel], field: str, *, step: Decimal | None = None) -> Bounds:
    """The bounds ``model`` validates ``field`` to, read off the field itself.

    A whole-number field is taken in steps of one. Any other step is the
    caller's to say, where the field's own validator holds one.
    """
    info = model.model_fields[field]
    lowest: Decimal | None = None
    highest: Decimal | None = None
    lowest_included = highest_included = True
    for held in info.metadata:
        if isinstance(held, Gt | Ge):
            lowest, lowest_included = _exact(held.gt if isinstance(held, Gt) else held.ge), isinstance(held, Ge)
        elif isinstance(held, Lt | Le):
            highest, highest_included = _exact(held.lt if isinstance(held, Lt) else held.le), isinstance(held, Le)
    if step is None and info.annotation is int:
        step = Decimal(1)
    return Bounds(lowest, lowest_included, highest, highest_included, step)
