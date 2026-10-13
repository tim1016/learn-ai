"""The cash ladder: the same run replayed at several starting-cash levels (#3108, PRD #3095).

Formula: level cash = ⌊multiple · the setup's starting cash⌋ to whole cents, for each multiple of the ladder, the
    setup's own (multiple 1) always among them and kept as it is; every level is replayed at the setup's own slippage,
    carrying its own cash from session to session, on the data the run already loaded.
Reference: the owner's rules in #3095 and #3108: participation changes with cash, so the run is replayed and not
  re-priced, as the slippage ladder is (ADR 0078 decision 9); and no level is called safe.
Canonical implementation: this file for which levels there are and what a level reports; ``run.py`` replays them.
Validated against: tests/options_backtest/test_run_capital.py, whose expectations are worked by hand.

A level reports its starting cash, the entries it filled, the entries it had refused for cash, its net profit and
its capital evidence (``capital_evidence.py``). The level whose cash is the setup's own is the run itself: it is not
replayed again. A level is a fact about one replay and no more: none is a minimum, and none is called safe, since
what a smaller cash refuses and a larger one fills is the whole point of the ladder and not a verdict on it.

Cents. Every price, fee and cash in a replay is in whole cents, so a level's cash is too: half of 100.01 is 50.005,
which is no balance an account can hold, and a replay would then decide cash admission on it and serve it in a form
the page does not read as dollars. A level's cash is the multiple of the setup's cash rounded DOWN to the cent, exactly
(``fills.EXACT``, no float). Down is the conservative direction for a ladder that says how far the cash provably
carried: a level never has a cent its multiple did not give. The setup's own level is not rounded, since the setup
already holds whole cents and that level is the run. A multiple that comes to less than a cent is refused, and two
multiples that round to one cash are one level, the smaller multiple's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.options_backtest import fills
from app.options_backtest.capital_evidence import CapitalEvidence

# Half the setup's cash, the same, double and four times.
DEFAULT_CASH_LADDER: tuple[Decimal, ...] = (Decimal("0.5"), Decimal(1), Decimal(2), Decimal(4))

_SETUPS_OWN = Decimal(1)
_ONE_CENT = Decimal("0.01")
_CENTS_PER_DOLLAR = Decimal(100)


@dataclass(frozen=True)
class CashLevel:
    """The whole run replayed at one starting cash."""

    # The level as a multiple of the setup's starting cash, and the cash it started with.
    multiple: Decimal
    starting_cash_usd: Decimal
    # The level whose cash is the setup's own: this level is the run's own result.
    is_setups_own: bool
    # Entries that filled, re-entries included: each is one leg trade.
    entries_filled: int
    entries_refused_for_cash: int
    net_profit_usd: Decimal
    capital_evidence: CapitalEvidence


def _down_to_the_cent(amount: Decimal) -> Decimal:
    """``amount``, which is above zero, rounded down to whole cents. Exact: the cents are an integer, never a float."""
    with localcontext(fills.EXACT):
        return Decimal(int(amount * _CENTS_PER_DOLLAR)).scaleb(-2)


def ladder_levels(starting_cash_usd: Decimal, multiples: Sequence[Decimal]) -> dict[Decimal, Decimal]:
    """Each level's cash and the multiple it is, lowest cash first, the setup's own among them and each cash once.

    A level's cash is the multiple of the setup's rounded down to the cent; the setup's own is kept as it is.
    ``ValueError`` for a multiple that is not a number above zero, or that comes to less than one cent: a level with
    no cash is not a run.
    """
    wanted = {*multiples, _SETUPS_OWN}
    for multiple in wanted:
        if not (multiple.is_finite() and multiple > 0):
            raise ValueError(f"a cash ladder level is a multiple of the starting cash above zero; got {multiple}")
    levels: dict[Decimal, Decimal] = {}
    with localcontext(fills.EXACT):
        for multiple in sorted(wanted):
            cash = starting_cash_usd if multiple == _SETUPS_OWN else _down_to_the_cent(starting_cash_usd * multiple)
            if cash < _ONE_CENT:
                raise ValueError(
                    f"a cash ladder level of {multiple} times a starting cash of {starting_cash_usd} is under one cent"
                )
            levels.setdefault(cash, multiple)
    return levels
