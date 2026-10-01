"""The account half of a draining lane's lane-quiet answer (#2154).

ADR 0063 Decision 2, as amended 2026-09-19: three of lane quiet's five
conditions are facts about the account — every working order on it has
ended at the broker, it is flat, and no order intent is in flight — read from
the broker's own lists and the ledger's own effects. Flat is also the lane's
own custody (#2344): a lane whose ledger still attributes exposure, or holds an
open episode saying it does not know whether it is flat, is not flat whatever
the broker says. These tests pin each condition, the scope (the whole account,
so a hand-placed order or position blocks), and the re-read rule that stands in
for the missing consistency fence between the order and position reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.external_orders import record_unfoldable_broker_order
from app.broker.alpaca.clerk.sqlite.lane_quiet import (
    AccountQuietObservation,
    observe_account_quiet,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    raise_account_hold,
    raise_failed_enter_filled_uncertainty,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import _REASON_POLICIES
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerPosition
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _broker_order_fixture,
    _broker_position_fixture,
    _clock_at,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
)
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    _held_position,
    _register_second_spy_lane,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    _broker_order as _flatten_broker_order,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    _FakeRead as _ReconcileRead,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    _FakeTrade as _ReconcileTrade,
)

T0 = 1_700_000_000_000


class _ScriptedRead:
    """A read port answering each account read from a script, in order.

    One entry per ``list_orders``/``list_positions`` pair, so a test can make
    the account change between the first read and the second.
    """

    def __init__(
        self,
        reads: list[tuple[list[BrokerOrder], list[BrokerPosition]]],
        *,
        error: Exception | None = None,
        clock: _TestClock | None = None,
    ) -> None:
        self._orders = [orders for orders, _ in reads]
        self._positions = [positions for _, positions in reads]
        self._error = error
        self._clock = clock
        self.order_reads = 0

    async def list_orders(self, *, status: str, limit: int) -> list[BrokerOrder]:
        assert status == "open"
        if self._error is not None:
            raise self._error
        self.order_reads += 1
        if self._clock is not None:
            # Each broker read takes time, as a real one does.
            self._clock.advance(250)
        return self._orders.pop(0)

    async def list_positions(self) -> list[BrokerPosition]:
        if self._error is not None:
            raise self._error
        return self._positions.pop(0)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_at(T0), lease_ttl_ms=300_000
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


EMPTY: tuple[list[BrokerOrder], list[BrokerPosition]] = ([], [])


def _quiet(observation: AccountQuietObservation) -> bool:
    return (
        observation.broker_work_ended
        and observation.account_flat
        and observation.intents_resolved
    )


async def test_an_empty_account_is_quiet_only_after_two_empty_reads(
    repo: ClerkSqliteRepository,
) -> None:
    """Quiet takes two complete reads, the second after the first returned."""
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    read = _ScriptedRead([EMPTY, EMPTY], clock=clock)

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert _quiet(observation)
    assert read.order_reads == 2
    # Stamped before the first read, not after the last: the earliest
    # instant the evidence covers, so freshness ages from there.
    assert clock() == T0 + 500
    assert observation.observed_at_ms == T0


async def test_an_order_placed_by_hand_blocks_quiet(repo: ClerkSqliteRepository) -> None:
    """Account-scoped, not lane-attributed: the ledger never saw this order."""
    hand_placed = _broker_order_fixture("manual-by-hand", status="new")
    read = _ScriptedRead([([hand_placed], [])])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert not observation.broker_work_ended
    assert observation.account_flat
    # Nothing a second read could add once something is already open.
    assert read.order_reads == 1


async def test_a_position_opened_by_hand_blocks_quiet(repo: ClerkSqliteRepository) -> None:
    """A position the bot never opened still means the account is not flat."""
    read = _ScriptedRead([([], [_broker_position_fixture("QQQ", quantity=5)])])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert not observation.account_flat
    assert observation.broker_work_ended


async def test_work_that_appears_between_the_reads_is_not_missed(
    repo: ClerkSqliteRepository,
) -> None:
    """The re-read rule: the first read's empty lists do not stand alone.

    The order and position lists are gathered with no consistency fence, so
    one read could see the order list just before a fill and the position
    list just before the position landed. The second read is what catches it.
    """
    read = _ScriptedRead([EMPTY, ([], [_broker_position_fixture("SPY", quantity=10)])])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert not observation.account_flat
    assert not _quiet(observation)


async def test_an_accepted_intent_blocks_quiet_even_with_the_broker_empty(
    repo: ClerkSqliteRepository,
) -> None:
    """Condition 5 reads every nonterminal effect, not only ``unknown`` ones.

    An accepted ENTER with a working order is not state ``unknown``, so
    ``InstanceCustodyProof.unresolved_intent_refs`` would not list it; it can
    still create custody, so it is outstanding.
    """
    await _make_entry(repo)
    read = _ScriptedRead([EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert not observation.intents_resolved
    assert observation.broker_work_ended and observation.account_flat


async def test_a_drifted_lane_with_an_open_exit_not_flat_episode_is_not_flat(
    repo: ClerkSqliteRepository,
) -> None:
    """#2344: the broker is flat, but the lane's custody still holds exposure.

    A reducing order that fills 4 of 10 fails its EXIT through
    ``EXIT_NOT_FLAT`` with 6 still attributed. Every effect is terminal and
    the broker's lists are empty, so the three account reads alone answer
    quiet — and a lane that still believes it holds custody would hand its
    account over.
    """
    # The market reducing leg goes out only inside the regular session (#2440).
    _walk_clock_to(repo, FIXTURE_RTH_MS)
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    partial = _broker_order(
        "placeholder", side="sell", status="canceled", filled_quantity=4.0, filled_avg_price=101.0
    )
    result = await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(submit_result=partial), pricing=UNPRICEABLE_RECOVERY
    )
    assert result.reducing_order_ref is not None
    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order=partial.model_copy(update={"client_order_id": result.reducing_order_ref}),
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=SID
    )
    assert episode is not None
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert observation.broker_work_ended and observation.intents_resolved
    assert not observation.account_flat
    assert not _quiet(observation)


async def test_attributed_exposure_blocks_quiet_even_with_the_broker_flat(
    repo: ClerkSqliteRepository,
) -> None:
    """#2344: attributed exposure alone, before any episode names the drift."""
    await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    assert repo.attributed_positions_by_symbol() == {"SPY": 10.0}
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert observation.broker_work_ended and observation.intents_resolved
    assert not observation.account_flat


async def test_opposing_custody_subjects_do_not_net_to_flat(
    repo: ClerkSqliteRepository,
) -> None:
    """#2382 review: lane A +10 SPY, lane B -10 SPY, broker flat.

    The account-wide attribution nets to zero, but each subject still holds
    exposure it can act on. Flat is per custody subject and symbol, never a sum.
    """
    await _held_position(repo, sid=SID, run_id=RUN_ID)
    sid_b, run_b = _register_second_spy_lane(repo)
    await _held_position(repo, suffix="b", sid=sid_b, run_id=run_b, side="sell")
    assert repo.position(SID, "SPY") == 10.0
    assert repo.position(sid_b, "SPY") == -10.0
    assert repo.attributed_positions_by_symbol() == {"SPY": 0.0}
    assert repo.active_uncertainties() == []
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert observation.broker_work_ended and observation.intents_resolved
    assert not observation.account_flat


async def test_an_open_uncertainty_episode_blocks_quiet_with_custody_flat(
    repo: ClerkSqliteRepository,
) -> None:
    """#2344: an open episode says the Clerk does not know it is flat.

    Nothing is attributed and the broker is empty, but reconciliation has not
    verified custody against a fresh snapshot, so flat is not established.
    """
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="stale",
        explanation="broker snapshot is stale",
        operator_impact="reductions are paused",
        next_step="reconcile",
    )
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert observation.broker_work_ended and observation.intents_resolved
    assert not observation.account_flat


async def test_an_open_execution_coverage_conflict_alone_does_not_block_quiet(
    repo: ClerkSqliteRepository,
) -> None:
    """Its only resolver is refused while draining, so blocking would wedge
    a flat lane forever; the broker-flat and attributed-flat reads carry it."""
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        headline="conflict",
        explanation="exact execution conflicts with aggregate recovery",
        operator_impact="entries are paused",
        next_step="resolve execution coverage",
        cause_facts={"order_ref": "ord-1", "execution_id": "exec-1"},
    )
    assert repo.active_uncertainties()
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert _quiet(observation)


async def test_an_open_account_hold_does_not_block_quiet(repo: ClerkSqliteRepository) -> None:
    """A hold fences entries; it is not a doubt about what the lane holds."""
    raise_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        evidence_refs=[],
        cause_facts={
            "day_start_ms": T0,
            "day_pnl_usd": -600.0,
            "loss_limit_usd": 500.0,
            "last_equity_usd": 9_400.0,
            "observed_at_ms": T0,
        },
    )
    assert repo.active_uncertainties()
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert observation.account_flat


async def test_an_episode_with_no_registered_policy_blocks_quiet(
    repo: ClerkSqliteRepository,
) -> None:
    """Fail closed: an unregistered code cannot declare itself harmless."""
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code="NOT_A_REGISTERED_CODE",
        headline="unknown",
        explanation="an unregistered cause",
        operator_impact="entries are paused",
        next_step="investigate",
    )
    read = _ScriptedRead([EMPTY, EMPTY])

    observation = await observe_account_quiet(repo, read)

    assert observation is not None
    assert not observation.account_flat


async def test_an_open_failed_enter_filled_fence_blocks_quiet_until_a_clean_sweep_clears_it(
    repo: ClerkSqliteRepository,
) -> None:
    """#2348 x #2344: the fence says belief and custody disagree, so it blocks.

    It must not wedge a flat draining lane: its one exit is the reconciliation
    sweep's attributed-flat proof on a clean verdict, which a draining lane
    still runs. A flat broker with nothing attributed yields that verdict, and
    the next observation answers quiet.
    """
    raise_failed_enter_filled_uncertainty(
        repo, strategy_instance_id=SID, order_ref="enter-ref-1", symbol="SPY", filled_qty=10.0
    )
    assert repo.attributed_positions_by_symbol() == {}

    blocked = await observe_account_quiet(repo, _ScriptedRead([EMPTY, EMPTY]))

    assert blocked is not None
    assert blocked.broker_work_ended and blocked.intents_resolved
    assert not blocked.account_flat

    swept = await reconcile_account(repo, read=_ReconcileRead(), trade=_ReconcileTrade(), pricing=UNPRICEABLE_RECOVERY)

    assert swept.verdict == "clean"
    assert (
        repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=FAILED_ENTER_FILLED_REASON_CODE,
            strategy_instance_id=SID,
        )
        is None
    )
    cleared = await observe_account_quiet(repo, _ScriptedRead([EMPTY, EMPTY]))
    assert cleared is not None
    assert _quiet(cleared)


async def test_an_open_unfoldable_broker_order_hold_alone_does_not_block_quiet(
    repo: ClerkSqliteRepository,
) -> None:
    """#2363 x #2344: its only exit, the operator acknowledgement, is refused
    while draining, so blocking would wedge a flat lane forever. An order that
    is still working is caught by the broker open-order read instead."""
    ended_mleg = _flatten_broker_order(
        "alpaca-console:mleg-1", order_id="mleg-1", symbol="MSFT", status="canceled"
    ).model_copy(update={"side": "None"})
    record_unfoldable_broker_order(repo, order=ended_mleg, reason="multi-leg parent")
    assert repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        strategy_instance_id=None,
    )

    observation = await observe_account_quiet(repo, _ScriptedRead([EMPTY, EMPTY]))

    assert observation is not None
    assert _quiet(observation)


def test_every_registered_reason_declares_its_lane_quiet_answer() -> None:
    """The whole registry, pinned: a row added or flipped must be decided here.

    ``True`` only where the episode doubts what the lane holds *and* clears
    while draining; ``False`` where the only resolver is refused while
    draining (account holds, the coverage conflict).
    """
    assert {code: policy.blocks_lane_quiet for code, policy in _REASON_POLICIES.items()} == {
        "POSITION_DRIFT": True,
        "BROKER_SNAPSHOT_STALE": True,
        "RECONCILIATION_INCOMPLETE": True,
        "ORDER_OUTCOME_UNKNOWN": True,
        "EXIT_NOT_FLAT": True,
        "EXIT_STUCK": True,
        FAILED_ENTER_FILLED_REASON_CODE: True,
        EXECUTION_COVERAGE_CONFLICT_REASON_CODE: False,
        # #2460: says nothing about any attributed quantity -- the quantity is
        # what both sides agree on -- so it cannot keep a flat lane unanswered.
        "EXECUTION_PRICE_CONFLICT": False,
        "UNEXPLAINED_ORDER_HOLD": False,
        UNFOLDABLE_BROKER_ORDER_REASON_CODE: False,
        "STREAM_HEALTH_HOLD": False,
        LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: False,
    }


async def test_an_unreadable_broker_is_no_answer_not_a_not_quiet_one(
    repo: ClerkSqliteRepository,
) -> None:
    """Reporting orders as outstanding would name something never observed."""
    read = _ScriptedRead([], error=BrokerUnavailable("alpaca down"))

    assert await observe_account_quiet(repo, read) is None
