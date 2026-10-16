"""A test-only writer of option orders, standing in for the order path that will write them (#2913).

The two contract tables are written by fold helpers
(``contract_terms.record_contract_terms`` and ``bind_order_contract``). The
production folds that call them take one bought option on a Dry Run. Custody
is proven here on its own, for any contract and any multiplier, so these
tests register two transition kinds of their own. One does what an option order's acceptance does: create the
order as an ENTER does, record the contract's terms, and bind the order to
the contract, all in one transition. The other does the same for the order
that closes a position: an EXIT with its reducing order, as the real folds
create them. Fills then arrive through the real fill fold, carrying the
contract's symbol as the broker reports it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTerms,
    bind_order_contract,
    record_contract_terms,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts, ExecutionSliceFilledFacts, ExitAcceptedFacts
from app.broker.alpaca.clerk.sqlite.folds import (
    DEFAULT_FOLD_REGISTRY,
    FoldRegistry,
    _fold_enter_accepted,
    _fold_exit_accepted,
    _fold_exit_reducing_order_created,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import require_active_run
from app.broker.alpaca.clerk.sqlite.models import CommandCreated, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref, mint_intent_id
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _clock_at, _TestClock
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started

OPTION_ORDER_ACCEPTED = "TEST_OPTION_ORDER_ACCEPTED"
OPTION_CLOSING_ORDER_ACCEPTED = "TEST_OPTION_CLOSING_ORDER_ACCEPTED"

AAPL_CALL = ContractTerms(symbol="AAPL261016C00200000", multiplier=100)
AAPL_PUT = ContractTerms(symbol="AAPL261016P00190000", multiplier=100)
# A contract of ten shares. No listing a bot would pick has one; the books must still count it right.
SMALL_CALL = ContractTerms(symbol="SPY261016C00500000", multiplier=10)

ACCOUNT_ID = "PA-OPTION-CUSTODY"
RUN_ID = "run-options"


def _fold_option_order_accepted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = json.loads(payload["facts_json"])
    contract = facts.pop("option_contract")
    _fold_enter_accepted(conn, {**payload, "facts_json": canonicalize(facts)})
    _bind(conn, payload, contract)


def _fold_option_closing_order_accepted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """An EXIT and its reducing order, as the two real folds create them, bound to the contract."""
    facts = json.loads(payload["facts_json"])
    contract = facts.pop("option_contract")
    _fold_exit_accepted(conn, {**payload, "facts_json": canonicalize(facts)})
    _fold_exit_reducing_order_created(conn, payload)
    _bind(conn, payload, contract)


def _bind(conn: sqlite3.Connection, payload: dict[str, Any], contract: dict[str, Any]) -> None:
    terms = ContractTerms(symbol=contract["symbol"], multiplier=contract["multiplier"])
    record_contract_terms(conn, terms, recorded_at_ms=payload["recorded_at_ms"])
    bind_order_contract(conn, order_ref=payload["order_ref"], symbol=terms.symbol)


def option_fold_registry() -> FoldRegistry:
    """Every production fold, plus the two test kinds that accept an option order."""
    registry = FoldRegistry()
    for kind in sorted(DEFAULT_FOLD_REGISTRY.registered_kinds):
        registry.register(kind, DEFAULT_FOLD_REGISTRY._folds[kind])
    registry.register(OPTION_ORDER_ACCEPTED, _fold_option_order_accepted)
    registry.register(OPTION_CLOSING_ORDER_ACCEPTED, _fold_option_closing_order_accepted)
    return registry


def open_option_books(
    tmp_path: Path, *, clock: _TestClock | None = None, bots: dict[str, str] | None = None,
    account_id: str = ACCOUNT_ID,
) -> ClerkSqliteRepository:
    """A fresh authority with each bot registered under its symbol and running."""
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=clock or _clock_at(NOON),
        fold_registry=option_fold_registry(),
    )
    for strategy_instance_id, symbol in (bots or {"bot-a": "AAPL"}).items():
        repo.register_strategy_instance(
            strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="option-custody"
        )
        seed_historical_run_started(
            repo,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=RUN_ID,
            clock=repo.clock,
        )
    return repo


def accept_option_order(
    repo: ClerkSqliteRepository,
    *,
    terms: ContractTerms,
    side: str,
    contracts: int,
    decision_id: str,
    strategy_instance_id: str = "bot-a",
    reference_price: str | None = None,
    fee_provision_cents: int = 0,
) -> EnterSubmission:
    """Accept one order in ``terms``' contract for a bot: an order row, the contract, and the binding.

    With ``reference_price`` (a premium per share) the order also records the
    cash reservation an admitted ENTER records, with its fee provision.
    """
    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    leg = {"symbol": terms.symbol, "side": side, "quantity": contracts}
    payload_hash = hashlib.sha256(canonicalize({"decision": idempotency_key, "leg": leg}).encode("utf-8")).hexdigest()

    def build_transition() -> TransitionInput:
        active = require_active_run(repo, strategy_instance_id, RUN_ID)
        facts = json.loads(
            EnterAcceptedFacts(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="strategy_decision",
                action="ENTER",
                intended_end_state=None,
                effect_idempotency_key=f"enter:{idempotency_key}",
                effect_kind="ENTER",
                decision_id=decision_id,
                leg=leg,
                cash_reference_price=reference_price,
                fee_provision_cents=fee_provision_cents,
            ).to_facts_json()
        )
        facts["option_contract"] = {"symbol": terms.symbol, "multiplier": terms.multiplier}
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            effect_operation_id=f"effect:{idempotency_key}",
            order_ref=build_order_ref(build_bot_order_namespace(strategy_instance_id), mint_intent_id()),
            transition_kind=OPTION_ORDER_ACCEPTED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code=OPTION_ORDER_ACCEPTED,
            facts_json=canonicalize(facts),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    assert isinstance(outcome, CommandCreated)
    order = repo.order_for_effect_operation(outcome.command.effect_operation_id)
    assert order is not None
    return EnterSubmission(
        command=outcome.command,
        effect_operation_id=outcome.command.effect_operation_id,
        order_ref=order.order_ref,
        created=True,
    )


def accept_option_closing_order(
    repo: ClerkSqliteRepository,
    *,
    terms: ContractTerms,
    opened: EnterSubmission,
    decision_id: str,
) -> EnterSubmission:
    """Accept the order that closes what ``opened`` bought: an EXIT, its reducing order, and the binding.

    Returned in the shape ``fill`` takes; its ``order_ref`` is the reducing order's.
    """
    strategy_instance_id = opened.command.strategy_instance_id
    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    effect_operation_id = f"effect:{idempotency_key}"
    order_ref = build_order_ref(build_bot_order_namespace(strategy_instance_id), mint_intent_id())
    payload_hash = hashlib.sha256(
        canonicalize({"decision": idempotency_key, "closes": opened.order_ref}).encode("utf-8")
    ).hexdigest()

    def build_transition() -> TransitionInput:
        active = require_active_run(repo, strategy_instance_id, RUN_ID)
        facts = json.loads(
            ExitAcceptedFacts(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="strategy_decision",
                action="EXIT",
                intended_end_state=None,
                effect_idempotency_key=f"exit:{idempotency_key}",
                effect_kind="EXIT",
                decision_id=decision_id,
                entry_order_ref=opened.order_ref or "",
                entry_order_refs=[opened.order_ref or ""],
            ).to_facts_json()
        )
        facts["option_contract"] = {"symbol": terms.symbol, "multiplier": terms.multiplier}
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind=OPTION_CLOSING_ORDER_ACCEPTED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code=OPTION_CLOSING_ORDER_ACCEPTED,
            facts_json=canonicalize(facts),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    assert isinstance(outcome, CommandCreated)
    return EnterSubmission(
        command=outcome.command, effect_operation_id=effect_operation_id, order_ref=order_ref, created=True
    )


def slice_transition(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    occurred_at_ms: int,
    fee: float | None = None,
) -> TransitionInput:
    """One exact execution of ``accepted``'s order, in the symbol the broker reported it in."""
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol=symbol,
        side=side,
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity="not_reported" if fee is None else "reported",
        evidence_source="websocket",
        source_event_at_ms=occurred_at_ms,
    )
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=occurred_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_SLICE_FILLED",
        facts_json=facts.to_facts_json(),
    )


def fill(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    occurred_at_ms: int = NOON,
    fee: float | None = None,
) -> str:
    """Fold one execution through the Clerk's own append, and say what it did with it."""

    def build() -> TransitionInput:
        return slice_transition(
            repo, accepted, execution_id=execution_id, symbol=symbol, side=side,
            quantity=quantity, price=price, occurred_at_ms=occurred_at_ms, fee=fee,
        )

    def no_conflict() -> TransitionInput:
        raise AssertionError("this execution was expected to conflict with nothing")

    return repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=build,
        build_coverage_conflict=no_conflict,
    )
