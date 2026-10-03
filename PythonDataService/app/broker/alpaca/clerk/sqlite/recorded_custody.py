"""What a bot still holds, read from its custody store's own records (#2694).

A bot that rehearsed on a live account's ``shadow:`` store stays sealed on it
after the account graduates. No broker stands behind that store any more, and
no sweep reconciles it again, so its records are the last word on what the
rehearsal left. Clear's proof that such a bot holds nothing is read here.

The rule is the installed Clear's own (ADR 0052 §1), applied to these
records: no attributed position, no working order, no order whose outcome is
unknown, no effect still owed broker evidence and no active run. Two arms are
this store's alone. An open uncertainty blocks whether it names the bot or the
whole account: on an installed account a later pass can resolve one, here
nothing ever will. And the Shadow broker's own order book is a second witness
that none of the bot's simulated orders is still open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import UnknownStrategyInstanceError
from app.broker.alpaca.clerk.sqlite.order_evidence import unresolved_order_refs, working_order_refs
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True)
class RecordedCustody:
    """One bot's holdings by its store's records, each in a field a refusal can name."""

    #: Attributed quantity per symbol, nonzero only.
    positions: Mapping[str, float]
    working_orders: int
    unknown_outcome_orders: int
    unresolved_effects: int
    open_uncertainties: int
    run_active: bool
    open_book_orders: int

    @property
    def holds_nothing(self) -> bool:
        return not self._held()

    @property
    def held_phrase(self) -> str:
        """What is held, in words: ``"10 SPY and 1 working order"``. Empty when nothing is."""
        *rest, last = self._held() or ("",)
        return f"{', '.join(rest)} and {last}" if rest else last

    def _held(self) -> tuple[str, ...]:
        """Every holding, named. The one list both the proof and its wording read."""
        counted = (
            (self.working_orders, "working order", "working orders"),
            (
                self.unknown_outcome_orders,
                "order whose outcome is unknown",
                "orders whose outcome is unknown",
            ),
            (
                self.unresolved_effects,
                "order still waiting on the broker's record",
                "orders still waiting on the broker's record",
            ),
            (self.open_uncertainties, "unresolved custody problem", "unresolved custody problems"),
            (
                self.open_book_orders,
                "simulated order still open in the Shadow order book",
                "simulated orders still open in the Shadow order book",
            ),
        )
        return (
            *(_position(symbol, quantity) for symbol, quantity in sorted(self.positions.items())),
            *(f"{count} {one if count == 1 else many}" for count, one, many in counted if count),
            *(("a run that has not ended",) if self.run_active else ()),
        )


def _position(symbol: str, quantity: float) -> str:
    return f"{abs(quantity):g} {symbol}" + (" short" if quantity < 0 else "")


def read_recorded_custody(
    repository: ClerkSqliteRepository,
    strategy_instance_id: str,
    *,
    open_book_orders: int,
) -> RecordedCustody:
    """One bot's holdings from ``repository`` alone; no broker is asked.

    ``open_book_orders`` is the store's second witness, counted by whoever
    owns its order book (``shadow_broker.open_book_orders``).

    Raises ``UnknownStrategyInstanceError`` for a bot the store never
    registered: every count below reads zero for it, and those zeros would
    prove nothing.
    """
    if repository.strategy_instance(strategy_instance_id) is None:
        raise UnknownStrategyInstanceError(strategy_instance_id)
    subject_id = bot_subject_id(strategy_instance_id)
    return RecordedCustody(
        positions={
            symbol: quantity
            for symbol, quantity in repository.attributed_positions_for_strategy(strategy_instance_id).items()
            if position_quantity_is_nonzero(quantity)
        },
        working_orders=len(working_order_refs(repository, strategy_instance_id)),
        unknown_outcome_orders=len(unresolved_order_refs(repository, strategy_instance_id)),
        unresolved_effects=len(repository.reconcilable_effect_operations(subject_id=subject_id)),
        open_uncertainties=len(repository.active_uncertainties_for_admission(subject_id=subject_id)),
        run_active=repository.active_run(strategy_instance_id) is not None,
        open_book_orders=open_book_orders,
    )


__all__ = ["RecordedCustody", "read_recorded_custody"]
