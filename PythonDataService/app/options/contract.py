"""One option contract, and the OCC symbol that names it at the broker.

A contract is its root symbol, its expiry, call or put, and its strike. The
symbol is those four written out, the way Alpaca writes them:
``AAPL240119C00100000`` is the AAPL call struck at 100 expiring 2024-01-19 —
the root, the expiry as ``YYMMDD``, ``C`` or ``P``, and the strike in
thousandths of a dollar as eight digits.

Reference: Alpaca, "Options Trading" (https://docs.alpaca.markets/docs/options-trading)
and the option-contract reference (https://docs.alpaca.markets/reference/get-options-contracts),
whose example symbols the tests pin.

The strike is held in whole thousandths of a dollar, the symbol's own unit, so
a contract has exactly one spelling and no float decides which contract an
order names.

What a symbol cannot say is not checked here: that the contract delivers 100
shares of the stock the root suggests, and that it is American. Those are
facts of the listing. The one thing the spelling does show is an adjusted
contract, whose root carries a digit (``AAPL1``); such a root is refused.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.session_anchors import MAX_TIMESTAMP_MS, option_expiry_anchor_ms, option_expiry_date

OptionRight = Literal["call", "put"]

_ROOT_PATTERN = r"^[A-Z]{1,6}$"
_OCC_SYMBOL = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yymmdd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")
_RIGHT_LETTER: dict[OptionRight, str] = {"call": "C", "put": "P"}
_RIGHT_OF_LETTER: dict[str, OptionRight] = {letter: right for right, letter in _RIGHT_LETTER.items()}

# The symbol writes the year in two digits and the strike in eight.
_FIRST_SYMBOL_YEAR = 2000
_LAST_SYMBOL_YEAR = 2099
_MAX_STRIKE_THOUSANDTHS = 99_999_999
_THOUSANDTHS_PER_DOLLAR = 1000


def strike_thousandths(strike: Decimal | int | float | str) -> int:
    """A strike in dollars as whole thousandths of a dollar; refuses one finer than a thousandth.

    A float is read as the shortest decimal that names it, so the ``452.5``
    a listing reports is exactly 452.5 and the residue of float arithmetic
    (``0.30000000000000004``) is refused rather than rounded.
    """
    if isinstance(strike, bool):
        raise ValueError("a strike must be a number of dollars")
    try:
        dollars = strike if isinstance(strike, Decimal) else Decimal(str(strike))
    except InvalidOperation as exc:
        raise ValueError(f"a strike must be a number of dollars, got {strike!r}") from exc
    if not dollars.is_finite():
        raise ValueError(f"a strike must be a finite number of dollars, got {strike!r}")
    # The exact ratio, so no decimal context rounds a too-fine strike into an acceptable one.
    numerator, denominator = dollars.as_integer_ratio()
    thousandths, remainder = divmod(numerator * _THOUSANDTHS_PER_DOLLAR, denominator)
    if remainder:
        raise ValueError(f"a strike is listed to a thousandth of a dollar at the finest, got {strike!r}")
    return thousandths


class OptionContract(BaseModel):
    """One listed option contract: what an order names and a position holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The OCC root: the stock's ticker as the symbol writes it, letters only.
    root: Annotated[str, Field(pattern=_ROOT_PATTERN)]
    # The expiry date's 16:00 New York anchor (ADR 0022 (a)).
    expiry_ms: Annotated[int, Field(gt=0, le=MAX_TIMESTAMP_MS)]
    right: OptionRight
    strike_thousandths: Annotated[int, Field(gt=0, le=_MAX_STRIKE_THOUSANDTHS)]

    @field_validator("expiry_ms")
    @classmethod
    def _an_expiry_the_symbol_can_write(cls, expiry_ms: int) -> int:
        expiry_date = option_expiry_date(expiry_ms)
        if not _FIRST_SYMBOL_YEAR <= expiry_date.year <= _LAST_SYMBOL_YEAR:
            raise ValueError(
                f"an option symbol writes its expiry year in two digits; {expiry_date.isoformat()} is outside "
                f"{_FIRST_SYMBOL_YEAR}-{_LAST_SYMBOL_YEAR}"
            )
        return expiry_ms

    @classmethod
    def of(
        cls,
        *,
        root: str,
        expiry_date: date,
        right: OptionRight,
        strike: Decimal | int | float | str,
    ) -> OptionContract:
        """The contract for a root, an expiry date, call or put, and a strike in dollars."""
        return cls(
            root=root,
            expiry_ms=option_expiry_anchor_ms(expiry_date),
            right=right,
            strike_thousandths=strike_thousandths(strike),
        )

    @classmethod
    def from_occ_symbol(cls, symbol: str) -> OptionContract:
        """The contract an OCC symbol names; refuses anything that is not one."""
        match = _OCC_SYMBOL.fullmatch(symbol)
        if match is None:
            raise ValueError(
                f"{symbol!r} is not an option contract symbol: expected a root of one to six letters, "
                "the expiry as YYMMDD, C or P, and the strike as eight digits"
            )
        yymmdd = match["yymmdd"]
        try:
            expiry_date = date(_FIRST_SYMBOL_YEAR + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:]))
        except ValueError as exc:
            raise ValueError(f"{symbol!r} is not an option contract symbol: {yymmdd} is not a date") from exc
        return cls(
            root=match["root"],
            expiry_ms=option_expiry_anchor_ms(expiry_date),
            right=_RIGHT_OF_LETTER[match["right"]],
            strike_thousandths=int(match["strike"]),
        )

    @property
    def expiry_date(self) -> date:
        return option_expiry_date(self.expiry_ms)

    @property
    def strike(self) -> Decimal:
        """The strike in dollars, exact."""
        return Decimal(self.strike_thousandths) / _THOUSANDTHS_PER_DOLLAR

    @property
    def occ_symbol(self) -> str:
        return f"{self.root}{self.expiry_date:%y%m%d}{_RIGHT_LETTER[self.right]}{self.strike_thousandths:08d}"
