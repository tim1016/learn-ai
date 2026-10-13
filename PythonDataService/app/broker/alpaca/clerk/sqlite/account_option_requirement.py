"""The account's option requirement over what the books attribute to its bots (ADR 0076 decisions 26 and 28).

Alpaca values the whole account together, so two spreads that need $1,000
apart can need nothing together, and a bot's own commitment cannot say what
an entry added to the account. The comparison of an option entry with the
broker's option buying power therefore records the account's requirement
twice: from the books' holdings as the first reading began, and as the second
began once the entry's outcome was proven.

The figures are evidence. They change no result and hold nothing. Holdings
whose requirement cannot be worked out are recorded as that, ``None``, and
logged; the comparison goes on without them.

Formula: held(c) = Σ over bots of the signed contracts of option contract c,
  bought above zero; for each multiplier m, ``incremental_account_requirement``
  over the contracts recorded with m; the account's requirement is the sum
  over multipliers, as ``option_position_money`` sums its (underlying,
  multiplier) groups.
Reference: Alpaca, "Options Level 3 Trading"
  (https://docs.alpaca.markets/docs/options-level-3-trading, read 2026-10-08);
  ADR 0076 decision 28.
Canonical implementation: ``app/options/requirement.py``
  (``incremental_account_requirement``); this file for which contracts the
  books hold and the multiplier each was recorded with.
Validated against: tests/broker/alpaca/clerk/sqlite/test_account_option_requirement.py.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence

from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.reads import HeldOptionContract
from app.options.contract import OptionContract
from app.options.requirement import EntrySize, incremental_account_requirement
from app.options.structure import ResolvedLeg

logger = logging.getLogger(__name__)


def _legs_by_multiplier(
    conn: sqlite3.Connection, holdings: Sequence[HeldOptionContract]
) -> dict[int, tuple[ResolvedLeg, ...]]:
    """The account's net held contracts as legs, grouped by the multiplier each was recorded with.

    A contract held opposite by two bots is one position of the account, so
    the bots' signed quantities are summed first. Raises ``ValueError`` for
    a quantity that is not whole contracts or a contract the books hold no
    terms for: the books are then wrong, and nothing is rounded or guessed.
    """
    net: dict[str, int] = defaultdict(int)
    for holding in holdings:
        if not float(holding.quantity).is_integer():
            raise ValueError(f"{holding.symbol} is held as {holding.quantity!r}: option contracts are held whole")
        net[holding.symbol] += int(holding.quantity)
    by_multiplier: dict[int, list[ResolvedLeg]] = defaultdict(list)
    for symbol, contracts in sorted(net.items()):
        if contracts == 0:
            continue
        terms = contract_terms(conn, symbol)
        if terms is None:
            raise ValueError(f"the books hold {symbol} with no recorded terms")
        by_multiplier[terms.multiplier].append(
            ResolvedLeg(
                leg_id=symbol,
                contract=OptionContract.from_occ_symbol(symbol),
                position="long" if contracts > 0 else "short",
                ratio=abs(contracts),
            )
        )
    return {multiplier: tuple(legs) for multiplier, legs in by_multiplier.items()}


def account_requirement_cents(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    before: Sequence[HeldOptionContract],
    after: Sequence[HeldOptionContract],
) -> tuple[int, int] | None:
    """The account's option requirement for the holdings ``before`` and ``after``, in whole cents.

    ``None`` when either cannot be worked out (a loss with no limit, a
    fraction of a contract, a contract with no recorded terms). It is
    logged with its reason, so a figure that is missing says why.
    """
    try:
        held_before = _legs_by_multiplier(conn, before)
        held_after = _legs_by_multiplier(conn, after)
        figures = [
            incremental_account_requirement(
                held_before.get(multiplier, ()), held_after.get(multiplier, ()),
                EntrySize(multiplier=multiplier, units=1),
            )
            for multiplier in sorted(held_before.keys() | held_after.keys())
        ]
    except ValueError as exc:
        # ``StructureMoneyRefused`` is one: the holdings' loss cannot be bounded, or an amount is not whole cents.
        logger.warning(
            "the account's option requirement could not be worked out; the comparison goes on without it",
            extra={"action": "option_account_requirement_unavailable", "account_id": account_id, "error": str(exc)},
        )
        return None
    return sum(figure[0] for figure in figures), sum(figure[1] for figure in figures)


__all__ = ["account_requirement_cents"]
