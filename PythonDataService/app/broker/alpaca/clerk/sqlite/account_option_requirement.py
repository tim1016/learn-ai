"""The account's option requirement over what the books attribute to its bots (ADR 0076 decisions 26 and 28).

Alpaca values the whole account together, so two spreads that need $1,000
apart can need nothing together, and a bot's own commitment cannot say what
an entry added to the account. The comparison of an option entry with the
broker's option buying power therefore records the account's requirement
twice: from the books' holdings as the first reading began, and as the second
began once the entry's outcome was proven.

The figures are evidence. They change no result and hold nothing, so nothing
that goes wrong in working them out may reach the judgment: they come back as
``None`` and the reason is logged under an ``action`` of its own. Only a
failure of the books' own database is let through, as it is everywhere else.

Formula: held(c) = Σ over bots of the signed contracts of option contract c,
  bought above zero; the account's requirement before and after an entry is
  ``app.options.requirement.incremental_account_requirement`` over those
  holdings, each contract at the multiplier the books recorded for it.
Reference: Alpaca, "Options Level 3 Trading"
  (https://docs.alpaca.markets/docs/options-level-3-trading, read 2026-10-08);
  ADR 0076 decision 28.
Canonical implementation: ``app/options/requirement.py``
  (``incremental_account_requirement``); this file only says which contracts
  the books hold, netted across bots, and the multiplier each was recorded with.
Validated against: tests/broker/alpaca/clerk/sqlite/test_account_option_requirement.py.

The Deploy review asks the same of an entry that has not been made (#3099):
the account's holdings (``held_signed_contracts``) and those holdings with the
entry's contracts added (``with_contracts``, which lives beside the requirement it
feeds in ``app/options/requirement.py`` and is re-exported here), which
``incremental_account_requirement`` then values together.

The account's picture (#3101) reads the same holdings bot by bot
(``held_signed_contracts_by_bot``), stopped bots included, and nets them itself
with ``with_contracts``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence

from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.reads import HeldOptionContract, held_option_contracts
from app.options.requirement import ContractAtMultiplier, incremental_account_requirement, with_contracts

logger = logging.getLogger(__name__)


def _signed_contracts(
    conn: sqlite3.Connection, holdings: Sequence[HeldOptionContract]
) -> dict[ContractAtMultiplier, int]:
    """Each held contract's net whole contracts across the account's bots, at the multiplier the books recorded for it.

    One contract held opposite by two bots nets. Raises ``ValueError`` for a
    quantity that is not whole contracts, or a contract the books hold no
    terms for: the books are then wrong, and nothing is rounded or guessed.
    """
    net: dict[ContractAtMultiplier, int] = defaultdict(int)
    for holding in holdings:
        if not float(holding.quantity).is_integer():
            raise ValueError(f"{holding.symbol} is held as {holding.quantity!r}: option contracts are held whole")
        terms = contract_terms(conn, holding.symbol)
        if terms is None:
            raise ValueError(f"the books hold {holding.symbol} with no recorded terms")
        net[holding.symbol, terms.multiplier] += int(holding.quantity)
    return dict(net)


def held_signed_contracts(conn: sqlite3.Connection) -> dict[ContractAtMultiplier, int]:
    """Every option contract any bot holds, netted across the account's bots, each at its recorded multiplier.

    Raises ``ValueError`` as ``_signed_contracts`` does: the books are then
    wrong, and nothing is rounded or guessed.
    """
    return _signed_contracts(conn, held_option_contracts(conn))


def held_signed_contracts_by_bot(conn: sqlite3.Connection) -> dict[str, dict[ContractAtMultiplier, int]]:
    """Every option contract each bot holds, by bot: signed whole contracts at the multiplier the books recorded.

    A stopped bot's holdings are listed too: a Stop ends entries, not positions.
    A position the books count as no exposure (``position_quantity_is_nonzero``)
    is not held, as ``held_option_legs`` reads it. Raises ``ValueError`` as
    ``_signed_contracts`` does.
    """
    by_bot: dict[str, list[HeldOptionContract]] = defaultdict(list)
    for holding in held_option_contracts(conn):
        if position_quantity_is_nonzero(holding.quantity):
            by_bot[holding.strategy_instance_id].append(holding)
    return {sid: _signed_contracts(conn, holdings) for sid, holdings in sorted(by_bot.items())}


def account_requirement_cents(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    before: Sequence[HeldOptionContract],
    after: Sequence[HeldOptionContract],
) -> tuple[int, int] | None:
    """The account's option requirement for the holdings ``before`` and ``after``, in whole cents.

    ``None`` when either cannot be worked out (a loss with no limit, a
    fraction of a contract, a contract with no recorded terms, or anything
    unexpected): evidence is never worth the comparison. The reason is
    logged, so a figure that is missing says why.
    """
    try:
        return incremental_account_requirement(_signed_contracts(conn, before), _signed_contracts(conn, after))
    except ValueError as exc:
        # ``StructureMoneyRefused`` is one: the holdings' loss cannot be bounded, or an amount is not whole cents.
        logger.warning(
            "the account's option requirement could not be worked out; the comparison goes on without it",
            extra={"action": "option_account_requirement_unavailable", "account_id": account_id, "error": str(exc)},
        )
    except sqlite3.Error:
        raise
    except Exception:
        logger.exception(
            "the account's option requirement failed unexpectedly; the comparison goes on without it",
            extra={"action": "option_account_requirement_failed", "account_id": account_id},
        )
    return None


__all__ = ["account_requirement_cents", "held_signed_contracts", "held_signed_contracts_by_bot", "with_contracts"]
