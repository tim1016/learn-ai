"""A manual chain member first seen as a foreign order is the chain's own (#2787).

Route 2 of #2686: the owner's manual order A is replaced by B, B by C, and C
fills before the Clerk sees either link. C's ``trade_updates`` frame is
consumed as a foreign order's, which leaves an ``external_orders`` row for C.
The sweep then follows the chain to C and ends the manual leg on C's
execution. Before #2787 the row kept C an outside order to every money read:
C's account activity was skipped as the Clerk's own, so fee coverage never
saw the outside order witnessed and stayed incomplete for ever, and a
budgeted account refused every bot entry. This pins that C counts once, as
the manual leg's, wherever money is counted, and that a bot ENTER filled
where only REST saw it is never stuck the same way.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.day_pnl import risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _accept_day_pnl_enter, _broker_order_fixture
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_manual_order_filled_over_rest import (
    _EXEC_1,
    _EXEC_C,
    _ActivityFeed,
    _AlpacaAccount,
    _credited,
    _fill_frame,
    _filled,
    _sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_manual_order_replaced_at_alpaca import (
    _B,
    _C,
    _buy_limit,
    _replace_at_alpaca,
    _replacement_of,
    _unexplained_hold_active,
    _Website,
)
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import OPERATOR_ID

_MANUAL_SUBJECT = manual_operator_subject_id(OPERATOR_ID)


def _reading_after_every_execution(repo: ClerkSqliteRepository) -> LiveEnvelopeGate:
    """An account reading taken after every execution the Clerk has recorded."""
    gate = _gate(cash=10_000)
    observation = gate.latest_observation()
    assert observation is not None
    gate.publish(replace(observation, observed_at_ms=repo.clock(), risk_fill_sequence=risk_fill_sequence(repo)))
    return gate


def _account_execution_coverage(repo: ClerkSqliteRepository) -> str:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        return reader.account_pnl_attribution(from_ms=0, to_ms=repo.clock()).execution_coverage
    finally:
        reader.close()


async def test_a_bot_entry_is_admitted_once_a_chain_member_first_seen_as_foreign_ends_the_leg(
    tmp_path: Path,
) -> None:
    """Route 2 on a budgeted Paper account, then the account's activity is read.

    C's execution is the manual leg's, witnessed once: fee coverage is
    complete and C's fee is the leg's alone, the money bar holds C's shares
    once, the account's P&L counts its executions complete, and the deployed
    bot's entry is admitted.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        website = _Website(repo=repo)
        manual = await _buy_limit(repo, website)
        order_ref = manual.leg.order_ref
        assert order_ref is not None
        _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
        replaced_b = replacement_b.model_copy(update={"status": "replaced", "replaced_by": _C})
        website.replacements[_B] = replaced_b
        filled_c = _filled(_replacement_of(replaced_b, repo, replacement_id=_C), repo, filled_quantity=5, avg=99.90)
        website.replacements[_C] = filled_c
        assert await _fill_frame(repo, filled_c, execution_id=_EXEC_C, quantity=5, price=99.90) == "unexplained_order"
        assert repo.external_order_by_broker_order_id(_C) is not None, "C's frame is consumed as a foreign order's"
        feed = _ActivityFeed()
        feed.fill(execution_id=_EXEC_C, order_id=_C, quantity=5, price=99.90, at_ms=repo.clock())
        for _ in range(2):
            await _sweep(repo, website, feed, spy_held=5.0)
        assert repo.order(order_ref).broker_order_id == _C
        assert _credited(repo, order_ref) == [(_EXEC_C, "activity_recovery", 5.0, 99.90)]
        assert not _unexplained_hold_active(repo)

        assert await FeeEvidenceSync(repo=repo, read=_AlpacaAccount(feed=feed, spy_held=5.0)).tick()

        fees = repo.fee_attribution(now_ms=repo.clock())
        assert fees.known, fees.unresolved
        assert fees.external_fills == (), "C's execution is the manual leg's, never an outside fill"
        assert fees.shares and {share.subject_id for share in fees.shares} == {_MANUAL_SUBJECT}
        assert repo.account_money(cash=10_000, seen_before_ms=repo.clock() + 1).outside == Decimal("499.5")
        assert _account_execution_coverage(repo) == "complete"
        entry = accept_enter(
            repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="after-route-two",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            reference_price=100, envelope=_reading_after_every_execution(repo),
        )
        assert entry.created
    finally:
        repo.close()


async def test_a_bot_entry_filled_only_over_rest_is_fee_complete_once_its_exact_execution_lands(
    day_pnl_repo: ClerkSqliteRepository,
) -> None:
    """The bot-ENTER twin #2787 asked about does not stay incomplete.

    A bot order is never an outside order: its broker id is its own ``orders``
    row's. While only the sweep's REST cumulative stands, the account's fill
    coverage is honestly incomplete for that cumulative, whether or not the
    order's FILL activity was read. Once the stream's exact execution lands
    under the id the activity embeds, the activity is witnessed as the bot's
    own and coverage is complete.
    """
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="filled-over-rest")
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None
    filled = _broker_order_fixture(
        accepted.order_ref, status="filled", quantity=10, filled_quantity=10, filled_avg_price=100,
    ).model_copy(update={"updated_at_ms": NOON, "observed_at_ms": NOON, "filled_at_ms": NOON})
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=filled)
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_1, order_id=filled.order_id, quantity=10, price=100, at_ms=NOON)
    assert await FeeEvidenceSync(repo=repo, read=_AlpacaAccount(feed=feed, spy_held=10.0)).tick()
    assert not repo.fee_attribution(now_ms=repo.clock()).known

    assert await _fill_frame(repo, filled, execution_id=_EXEC_1, quantity=10, price=100) == "order_event"

    fees = repo.fee_attribution(now_ms=repo.clock())
    assert fees.known, fees.unresolved
    assert fees.external_fills == ()
