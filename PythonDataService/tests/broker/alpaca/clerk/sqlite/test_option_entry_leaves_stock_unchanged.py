"""A stock bot's ENTER and EXIT are recorded exactly as they were before the Clerk took an option entry (#2913).

Every value pinned here was computed by ``origin/master`` at ``ab1804a7``,
before the first line of the option entry path was written, and compared
with this branch's own. An option entry adds a key to the ENTER's identity
payload and to its facts; a stock ENTER has neither, so its identity hash and
its facts, which are hashed into the custody chain, must not move by a byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import _enter_identity, accept_enter
from app.broker.alpaca.clerk.sqlite.exit import _exit_identity
from app.broker.alpaca.clerk.sqlite.facts import (
    EnterAcceptedFacts,
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg

_MARKET = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)
_LIMIT = BrokerOrderLeg(symbol="QQQ", side="buy", quantity=3, order_type="limit", limit_price=412.37, extended_hours=True)
_MARKET_HASH = "e4fb4588ba9c0b9874f3d7056f6d6793316a8932f59192539ee714f8ea1bfdd8"


def _identity(leg: BrokerOrderLeg) -> tuple[str, str, str, str]:
    return _enter_identity(account_id="PA-TEST", strategy_instance_id="spy-bot", decision_id="d-1", leg=leg)


def test_a_stock_enters_identity_hashes_as_it_did_on_master() -> None:
    assert _identity(_MARKET) == ("spy-bot:d-1", _MARKET_HASH, "cmd:spy-bot:d-1", "enter:spy-bot:d-1")
    assert _identity(_LIMIT)[1] == "6b5d466b9ce1102d821dcba69a4ace839622eab8aa8fa3c87998921634504cb7"


def test_a_stock_enter_is_stored_with_the_facts_it_had_on_master(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path, clock=lambda: 1_700_000_000_000)
    try:
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id="PA-TEST", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")

        accepted = accept_enter(
            repo, account_id="PA-TEST", strategy_instance_id="spy-bot", decision_id="d-1", lifecycle_run_id="run-1",
            leg=_MARKET,
        )

        stored = repo.first_order_transition(order_ref=accepted.order_ref, transition_kind="ENTER_ACCEPTED")
        assert stored["facts_json"] == (
            '{"action":"ENTER","decision_id":"d-1","effect_idempotency_key":"enter:spy-bot:d-1","effect_kind":"ENTER",'
            '"idempotency_key":"spy-bot:d-1","intended_end_state":null,"kind":"strategy_decision","leg":{"extended_hours":'
            'false,"limit_price":null,"order_type":"market","quantity":1.0,"side":"buy","symbol":"SPY","time_in_force":'
            f'"day"}},"payload_hash":"{_MARKET_HASH}"}}'
        )
        assert accepted.command.payload_hash == _MARKET_HASH
    finally:
        repo.close()


def test_a_stock_enter_with_a_reservation_writes_the_facts_it_wrote_on_master() -> None:
    facts = EnterAcceptedFacts(
        idempotency_key="spy-bot:d-1", payload_hash="p", kind="strategy_decision", action="ENTER",
        intended_end_state=None, effect_idempotency_key="enter:spy-bot:d-1", effect_kind="ENTER", decision_id="d-1",
        leg=_LIMIT.model_dump(mode="json"), cash_reference_price="412.37", fee_provision_cents=3,
    )

    assert facts.to_facts_json() == (
        '{"action":"ENTER","cash_reference_price":"412.37","decision_id":"d-1","effect_idempotency_key":'
        '"enter:spy-bot:d-1","effect_kind":"ENTER","fee_provision_cents":3,"idempotency_key":"spy-bot:d-1",'
        '"intended_end_state":null,"kind":"strategy_decision","leg":{"extended_hours":true,"limit_price":412.37,'
        '"order_type":"limit","quantity":3.0,"side":"buy","symbol":"QQQ","time_in_force":"day"},"payload_hash":"p"}'
    )
    assert EnterAcceptedFacts.from_facts_json(facts.to_facts_json()) == facts


def test_a_fee_provision_with_no_cash_reference_is_refused_not_dropped() -> None:
    facts = EnterAcceptedFacts(
        idempotency_key="spy-bot:d-1", payload_hash="p", kind="strategy_decision", action="ENTER",
        intended_end_state=None, effect_idempotency_key="enter:spy-bot:d-1", effect_kind="ENTER", decision_id="d-1",
        leg=_LIMIT.model_dump(mode="json"), cash_reference_price=None, fee_provision_cents=3,
    )

    with pytest.raises(ValueError, match="fee provision"):
        facts.to_facts_json()


def test_a_stock_exit_hashes_and_is_written_as_it_was_on_master() -> None:
    identity = _exit_identity(account_id="PA-TEST", strategy_instance_id="spy-bot", decision_id="x-1", entry_order_ref="ref-1")
    accepted = ExitAcceptedFacts(
        idempotency_key="spy-bot:x-1", payload_hash="p", kind="strategy_decision", action="EXIT", intended_end_state=None,
        effect_idempotency_key="exit:spy-bot:x-1", effect_kind="EXIT", decision_id="x-1", entry_order_ref="ref-1",
        entry_order_refs=["ref-1"],
    )

    assert identity[1] == "4854ffb4e5a7587b40bfda33473b88b5ad135195b8106d62de434680185b79ac"
    assert accepted.to_facts_json() == (
        '{"action":"EXIT","decision_id":"x-1","effect_idempotency_key":"exit:spy-bot:x-1","effect_kind":"EXIT",'
        '"entry_order_ref":"ref-1","entry_order_refs":["ref-1"],"idempotency_key":"spy-bot:x-1",'
        '"intended_end_state":null,"kind":"strategy_decision","payload_hash":"p"}'
    )
    assert ExitReducingOrderCreatedFacts(symbol="SPY", side="SELL", quantity=1.0).to_facts_json() == (
        '{"quantity":1.0,"side":"SELL","symbol":"SPY"}'
    )
