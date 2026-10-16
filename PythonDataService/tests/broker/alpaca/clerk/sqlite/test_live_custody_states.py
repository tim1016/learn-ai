"""An order the broker may still fill is live custody, whatever state it is in (#3216, owner ruling 2026-10-14).

A retired bot whose only live thing is an order in a rare working state -- held
for an auction, suspended, pending a replace, calculated, stopped -- must not
read as inert, or its custody projection, its watched symbol and its quote
lines go quiet while the order can still fill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import (
    submit_retire_strategy_instance,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

SID = "retired-bot"

#: The states the broker may still act on an order in, as the Clerk has always read them.
USUAL = ("new", "accepted", "pending_new", "partially_filled", "pending_cancel")
#: Less common states in which an order can still fill: held for an auction,
#: suspended, pending a replace, calculated, or stopped.
RARE = ("accepted_for_bidding", "pending_replace", "suspended", "calculated", "stopped")
ENDED = ("filled", "canceled", "expired", "rejected")


def _retired_bot_with_one_order(tmp_path: Path, broker_state: str) -> ClerkSqliteRepository:
    """A bot whose run stopped and whose effect ended, holding nothing but one order the broker shows in ``broker_state``."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-LIVE-CUSTODY", artifacts_root=tmp_path, clock=_TestClock(NOON))
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="seal")
    submit_start_run(repo, account_id=repo.account_id, strategy_instance_id=SID, lifecycle_run_id="run-1", clock=repo.clock)
    accepted = accept_enter(
        repo, account_id=repo.account_id, strategy_instance_id=SID, lifecycle_run_id="run-1", decision_id="enter-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=SID, lifecycle_run_id="run-1", clock=repo.clock)
    order = repo.order(accepted.order_ref)
    assert order is not None
    with repo.write_fence() as conn:
        # The books are set to the one state this read is about: the effect has ended (a terminal state) and the
        # broker still shows its order in a state it can fill from.
        conn.execute("UPDATE effect_operations SET state = 'failed' WHERE effect_operation_id = ?", (order.effect_operation_id,))
        conn.execute("UPDATE orders SET broker_state = ? WHERE order_ref = ?", (broker_state, accepted.order_ref))
        conn.commit()
    submit_retire_strategy_instance(
        repo, account_id=repo.account_id, strategy_instance_id=SID, retired_at_ms=NOON, clock=repo.clock,
    )
    return repo


@pytest.mark.parametrize("broker_state", [*USUAL, *RARE])
def test_a_retired_bot_whose_only_live_thing_is_an_order_the_broker_may_fill_is_live_custody(
    tmp_path: Path, broker_state: str,
) -> None:
    repo = _retired_bot_with_one_order(tmp_path, broker_state)
    try:
        assert repo.strategy_instances_with_live_custody() == {SID}
    finally:
        repo.close()


@pytest.mark.parametrize("broker_state", ENDED)
def test_a_retired_bot_whose_only_order_has_ended_reads_inert(tmp_path: Path, broker_state: str) -> None:
    repo = _retired_bot_with_one_order(tmp_path, broker_state)
    try:
        assert repo.strategy_instances_with_live_custody() == set()
    finally:
        repo.close()


@pytest.mark.parametrize("broker_state", RARE)
def test_the_symbol_of_an_order_the_broker_may_still_fill_stays_watched(tmp_path: Path, broker_state: str) -> None:
    repo = _retired_bot_with_one_order(tmp_path, broker_state)
    try:
        assert repo.market_data_symbols() == ("SPY",)
    finally:
        repo.close()
