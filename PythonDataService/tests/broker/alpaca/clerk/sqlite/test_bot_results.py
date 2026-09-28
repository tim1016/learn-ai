"""Home's bot facts from the one money authority (PRD #2560 D7).

``bots_holding_money`` must agree with the money bar's stopped slices -- the
same "position cost or still-claimed money above zero" -- without a cash
observation, and a Finished bot's result must be exactly what its budget's
balance gained over its commitment.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_money import money_bar
from app.broker.alpaca.clerk.sqlite.budget_projection import (
    BudgetUnavailable,
    project_bot_results,
    read_bot_results,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _record_sale
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice


def _enter(repo: ClerkSqliteRepository, sid: str, *, quantity: int) -> EnterSubmission:
    return accept_enter(
        repo, account_id=repo.account_id, strategy_instance_id=sid, decision_id=f"enter-{sid}",
        lifecycle_run_id=f"run-{sid}", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity),
        reference_price=100, envelope=_gate(),
    )


def _stop(repo: ClerkSqliteRepository, sid: str) -> None:
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", clock=repo.clock)


@pytest.fixture
def account(tmp_path: Path):
    """Four budgeted bots, one of each kind Home tells apart.

    ``a`` stopped holding 2 SPY; ``b`` stopped flat with its entry order still
    working; ``c`` stopped after buying at 100 and selling at 110; ``d``
    still running with 1 SPY.
    """
    repo = _new_budget_repo(tmp_path)
    for sid in ("c", "d"):
        repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS)
    for sid, cents in (("a", 25_000), ("b", 15_000), ("c", 15_000), ("d", 15_000)):
        _deploy(repo, sid, cents)
    # Every entry is admitted against the one cash observation before any fills.
    held, _working, sold, running = (_enter(repo, sid, quantity=2 if sid == "a" else 1) for sid in "abcd")
    _append_slice(repo, held, execution_id="a-buy", quantity=2, source_event_at_ms=NOON - 1, fee=0)
    _append_slice(repo, sold, execution_id="c-buy", quantity=1, source_event_at_ms=NOON - 2, fee=0)
    _record_sale(repo, sold, key="c-sell", price=110, at_ms=NOON - 1)
    _append_slice(repo, running, execution_id="d-buy", quantity=1, source_event_at_ms=NOON - 1, fee=0)
    for sid in ("a", "b", "c"):
        _stop(repo, sid)
    yield repo
    repo.close()


def test_bots_holding_money_are_exactly_the_money_bars_stopped_slices(account: ClerkSqliteRepository) -> None:
    holding = account.bots_holding_money()

    # A running bot holds money too; Home tells it apart by its run.
    assert holding == {"a", "b", "d"}
    bar = money_bar(account.account_money(cash=10_000, seen_before_ms=NOON + 1))
    stopped = {segment.strategy_instance_id for segment in bar.segments if segment.kind == "stopped"}
    # ``b`` is flat, but its working entry still claims cash: holding, not finished.
    assert stopped == {"a", "b"} == holding - {"d"}


def test_a_finished_bots_result_is_what_its_balance_gained_over_its_budget(account: ClerkSqliteRepository) -> None:
    with account._write_lock:
        results = project_bot_results(
            account._conn, fees=account.fee_attribution(now_ms=NOON), strategy_instance_ids=["c"],
        )

    own = next(
        item for item in account.account_budget(cash=10_000, seen_before_ms=NOON + 1).deployments
        if item.strategy_instance_id == "c"
    )
    assert results["c"].result == own.balance - Decimal(own.committed_cents) / 100
    assert results["c"].result == Decimal(10) - own.fees
    assert results["c"].trade_count == 2


def test_results_read_on_their_own_snapshot_match_the_writers(account: ClerkSqliteRepository) -> None:
    with account._write_lock:
        written = project_bot_results(
            account._conn, fees=account.fee_attribution(now_ms=NOON), strategy_instance_ids=["c", "a"],
        )

    assert account.bot_results(["c", "a"]) == written


def test_results_the_fee_evidence_cannot_vouch_for_are_refused_never_zero(account: ClerkSqliteRepository) -> None:
    with pytest.raises(BudgetUnavailable, match="Fee evidence"):
        # No producer has checked the fee evidence in this process.
        read_bot_results(account.db_path, now_ms=NOON, fee_evidence_checked_at_ms=None, strategy_instance_ids=["c"])
