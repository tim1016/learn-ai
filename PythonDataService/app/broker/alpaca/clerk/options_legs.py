"""Sealed options-session leg instructions and exact allocation custody.

Formula: an allocation holds its opening executions plus the reducing
  executions linked to that opening; protection is the canonical cover book.
Reference: ADR 0076 (execution custody) and ADR 0078 (session leg rules).
Canonical implementation: this allocation composition; ``options_backtest/cover_book.py``
  owns cover and ``options/requirement.py`` owns collateral.
Validated against: tests/broker/alpaca/clerk/test_options_leg_custody.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.money import normalize_money
from app.options.contract import OptionContract
from app.options_backtest.cover_book import Held, Holding
from app.options_backtest.setup import LEG_ID_PATTERN, LegSetup, OptionsBacktestSetup
from app.schemas.action_plan import CloseLegExit, OptionInstrument, OptionsSessionActionPlan

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
    from app.broker.alpaca.clerk.sqlite.option_decision_fills import OptionDecisionFill
    from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True, slots=True)
class SessionEntryLeg:
    """The resolved leg's execution identity, without Signal selectors."""

    leg_id: str
    instrument: OptionInstrument
    position: Literal["long", "short"]
    right: Literal["call", "put"]
    qty_ratio: int = 1


@dataclass(frozen=True, slots=True)
class OptionsLegInstruction:
    """A single order instruction derived from custody-sealed session rules.

    This is deliberately not an ActionPlan: a sold leg can use previously
    confirmed protection, and does not claim to be a complete structure.
    """

    session_plan: OptionsSessionActionPlan
    setup: OptionsBacktestSetup
    leg: LegSetup
    attempt_id: str

    @property
    def on_enter(self) -> tuple[SessionEntryLeg, ...]:
        return (SessionEntryLeg(
            leg_id=self.leg.leg_id,
            instrument=OptionInstrument(kind="option", underlying=self.setup.underlying),
            position="long" if self.leg.side == "buy" else "short", right=self.leg.right,
        ),)

    @property
    def on_exit(self) -> tuple[CloseLegExit, ...]:
        return (CloseLegExit(kind="close_leg", entry_leg_id=self.leg.leg_id),)


@dataclass(frozen=True, slots=True)
class OptionsLegSnapshot:
    """One attempt's actual allocation, including an unanswered opening order."""

    leg_id: str
    attempt_id: str
    entry_decision_id: str | None = None
    entry_order_ref: str | None = None
    standing: EntryStanding | None = None
    held_contract: OptionContract | None = None
    held_quantity: int = 0
    entry_fills: tuple[OptionDecisionFill, ...] = ()
    close_fills: tuple[OptionDecisionFill, ...] = ()
    close_pending: bool = False


def sealed_instruction(
    repo: ClerkSqliteRepository, strategy_instance_id: str, leg_id: str, attempt_id: str,
) -> OptionsLegInstruction:
    """Read authorization from the registered immutable bot, never from its caller."""
    import re

    if re.fullmatch(LEG_ID_PATTERN, leg_id) is None:
        raise ValueError("an options instruction names a setup leg")
    if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 128:
        raise ValueError("an options attempt has a stable non-empty identity of at most 128 characters")
    config = repo.bot_config(strategy_instance_id)
    if config is None:
        raise ValueError("the options bot is not registered with this Clerk")
    payload = json.loads(config.config_json)
    options = payload.get("options_bot")
    if not isinstance(options, dict):
        raise ValueError("this bot has no sealed options session configuration")
    from app.schemas.options_bot import OptionsBotConfiguration

    qualified = OptionsBotConfiguration.model_validate(options)
    setup = qualified.setup
    plan = OptionsSessionActionPlan.model_validate(payload["action_plan"])
    if plan.underlying != setup.underlying or plan.setup_sha256 != options["setup_sha256"]:
        raise ValueError("the session plan and its sealed setup disagree")
    leg = next((leg for leg in setup.legs if leg.leg_id == leg_id), None)
    if leg is None:
        raise ValueError(f"the sealed setup has no leg {leg_id!r}")
    return OptionsLegInstruction(plan, setup, leg, attempt_id)


def allocation_entries(
    repo: ClerkSqliteRepository, strategy_instance_id: str,
) -> tuple[tuple[str, OptionEntryFacts], ...]:
    """Every session allocation on immutable entry facts, including prior sessions."""
    from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, option_entry_of_order

    return tuple(
        (order.order_ref, entry)
        for order in repo.entry_orders_for_strategy(strategy_instance_id)
        if isinstance(entry := option_entry_of_order(repo, order.order_ref), OptionEntryFacts)
        and entry.options_attempt is not None
    )


def attempt_entry(
    repo: ClerkSqliteRepository, strategy_instance_id: str, leg_id: str, attempt_id: str,
) -> tuple[str, OptionEntryFacts] | None:
    entries = [
        (order_ref, entry) for order_ref, entry in allocation_entries(repo, strategy_instance_id)
        if entry.options_attempt is not None
        and entry.options_attempt.leg_id == leg_id and entry.options_attempt.attempt_id == attempt_id
    ]
    if len(entries) > 1:
        raise RuntimeError("one options leg attempt has more than one opening allocation")
    return entries[0] if entries else None


def allocation_positions(
    repo: ClerkSqliteRepository, strategy_instance_id: str, entry_order_ref: str,
) -> dict[str, float]:
    """Actual fills of one allocation and its linked reductions, without sibling netting."""
    from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records

    with repo._write_lock:
        reducing = {
            row[0] for row in repo._conn.execute(
                "SELECT reduction.order_ref FROM operation_order_links opening "
                "JOIN effect_operations effect ON effect.effect_operation_id=opening.effect_operation_id "
                "JOIN operation_order_links reduction ON reduction.effect_operation_id=effect.effect_operation_id "
                "WHERE opening.order_ref=? AND opening.role='ENTRY' "
                "AND effect.kind='EXIT' AND reduction.role='REDUCING'", (entry_order_ref,),
            )
        }
        refs = {entry_order_ref, *reducing}
        records = effective_fill_records(
            repo._conn, account_id=repo.account_id, strategy_instance_ids=(strategy_instance_id,),
        )
        held: dict[str, Decimal] = {}
        for fill in records:
            if fill.order_ref in refs and not fill.credit_withheld:
                quantity = normalize_money(fill.quantity) * (1 if fill.side.value == "buy" else -1)
                held[fill.symbol] = held.get(fill.symbol, Decimal(0)) + quantity
        return {symbol: float(quantity) for symbol, quantity in held.items()}


def allocation_book(
    repo: ClerkSqliteRepository, strategy_instance_id: str, setup: OptionsBacktestSetup,
) -> tuple[Held, tuple[str, ...]]:
    """Cover at its conservative exposure: pending sales count, pending buys do not."""
    from app.broker.alpaca.clerk.sqlite.entry_outcome import entry_outcome

    holdings: dict[str, Holding] = {}
    closing: list[str] = []
    for order_ref, entry in allocation_entries(repo, strategy_instance_id):
        assert entry.options_attempt is not None
        leg = entry.the_leg
        outcome = entry_outcome(repo, order_ref)
        signed = allocation_positions(repo, strategy_instance_id, order_ref).get(leg.contract.occ_symbol, 0)
        if not float(signed).is_integer():
            raise ValueError("an options allocation contains a fractional contract")
        quantity = abs(int(signed))
        if leg.position == "short" and not outcome.proven:
            quantity = max(quantity, entry.order.quantity)
        if not quantity:
            continue
        if leg.leg_id in holdings:
            raise ValueError("a setup leg has overlapping attempts; reconcile custody before opening")
        holdings[leg.leg_id] = Holding(
            leg_id=leg.leg_id, contract=leg.contract, side="buy" if leg.position == "long" else "sell",
            contracts=quantity, multiplier=leg.multiplier,
        )
        if repo.active_exit_for_order(order_ref) is not None:
            closing.append(leg.leg_id)
    return Held(tuple(leg.leg_id for leg in setup.legs), tuple(holdings.values())), tuple(closing)


def leg_snapshot(
    repo: ClerkSqliteRepository, strategy_instance_id: str, leg_id: str, attempt_id: str,
) -> OptionsLegSnapshot:
    from app.broker.alpaca.clerk.sqlite.entry_outcome import entry_outcome
    from app.broker.alpaca.clerk.sqlite.option_decision_fills import option_fills_of_decision

    with repo.write_fence():
        found = attempt_entry(repo, strategy_instance_id, leg_id, attempt_id)
        if found is None:
            return OptionsLegSnapshot(leg_id, attempt_id)
        order_ref, entry = found
        order = repo.order(order_ref)
        assert order is not None
        effect = repo.effect_operation(order.effect_operation_id)
        assert effect is not None
        command = repo.get_command(effect.command_id)
        assert command is not None
        opening = option_fills_of_decision(
            repo, strategy_instance_id=strategy_instance_id, durable_decision_id=command.idempotency_key.split(":", 1)[1],
            how_simulated={},
        )
        close_decisions = repo._conn.execute(
            "SELECT command.idempotency_key FROM operation_order_links link "
            "JOIN effect_operations effect ON effect.effect_operation_id=link.effect_operation_id "
            "JOIN commands command ON command.command_id=effect.command_id "
            "WHERE link.order_ref=? AND effect.kind='EXIT' ORDER BY effect.created_at_ms", (order_ref,),
        ).fetchall()
        closed = tuple(
            fill for row in close_decisions
            for fill in option_fills_of_decision(
                repo, strategy_instance_id=strategy_instance_id,
                durable_decision_id=row[0].split(":", 1)[1], how_simulated={},
            )
        )
        positions = allocation_positions(repo, strategy_instance_id, order_ref)
        held = positions.get(entry.contract_terms.symbol, 0)
        if not float(held).is_integer():
            raise ValueError("an options allocation contains a fractional contract")
        return OptionsLegSnapshot(
            leg_id, attempt_id, command.idempotency_key.split(":", 1)[1], order_ref,
            entry_outcome(repo, order_ref).standing, entry.the_leg.contract, int(held), opening, closed,
            close_pending=repo.active_exit_for_order(order_ref) is not None,
        )


def protection_refusal(repo: ClerkSqliteRepository, strategy_instance_id: str, entry_order_ref: str) -> str | None:
    """Prevent every close path from releasing a session leg's required cover."""
    from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order
    from app.options_backtest.cover_book import may_leave

    entry = option_entry_of_order(repo, entry_order_ref)
    attribution = getattr(entry, "options_attempt", None)
    if attribution is None or entry.the_leg.position == "short":
        return None
    instruction = sealed_instruction(repo, strategy_instance_id, attribution.leg_id, attribution.attempt_id)
    book, closing = allocation_book(repo, strategy_instance_id, instruction.setup)
    if any(holding.side == "sell" for holding in book.holdings):
        if any(holding.side == "buy" and holding.leg_id in closing and holding.leg_id != attribution.leg_id for holding in book.holdings):
            return "Another protection close must settle before releasing this bought leg."
        if attribution.leg_id in {holding.leg_id for holding in book.holdings} and not may_leave(book, attribution.leg_id):
            return "A sold leg still depends on this protection; confirm its buy-back first."
    return None
