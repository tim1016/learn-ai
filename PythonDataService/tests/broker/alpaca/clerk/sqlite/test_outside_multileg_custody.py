"""An outside multi-leg order is custody of its legs, linked to the parent it came in (#3309).

Alpaca gives a multi-leg ("mleg") parent no symbol or side; its legs carry
both. The Clerk records an outside parent as the ordinary single-symbol outside
orders its legs are, each naming the parent and its sibling legs in its
evidence refs. The parent has no row of its own. The recordings are the sanitized Paper capture of
the #3225 outside credit spreads (``tests/fixtures/alpaca/option_paper_recordings``).
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.external_orders import (
    ExternalOrderReviewIncompleteError,
    acknowledge_external_order,
    observe_external_order,
    observe_or_record_unfoldable,
)
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerActivityEvidence, BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort

_RECORDINGS = Path(__file__).resolve().parents[4] / "fixtures" / "alpaca" / "option_paper_recordings"
_CREDIT_SPREADS = "3225-credit-sign-check"
_RESTING_ORDER = "3225-dashboard-credit-sign-check-20261016"
_CLIENT_ORDER_ID = "manual/hitl-gate/v1:SANITIZED0000000000001"
_OBSERVED_AT_MS = 1_792_080_284_000
_UNFOLDABLE = "UNFOLDABLE_BROKER_ORDER"
_UNEXPLAINED = "UNEXPLAINED_ORDER_HOLD"


def _id(suffix: int) -> str:
    return f"00000000-0000-0000-0000-{suffix:012d}"


# The opening spread (parent 354: sold 776, bought 777) and its closing order (parent 362).
_LEGS = {
    _id(356): ("SPY261016C00776000", "SELL", "limit", 0.6, 354, 358),
    _id(358): ("SPY261016C00777000", "BUY", "limit", 0.46, 354, 356),
    _id(363): ("SPY261016C00776000", "BUY", "market", 0.61, 362, 364),
    _id(364): ("SPY261016C00777000", "SELL", "market", 0.45, 362, 363),
}


def _event_bodies(recording: str) -> list[str]:
    """The stream's order events as the raw frames Alpaca sent, authorization frames left out."""
    bodies: list[str] = []
    for text in (_RECORDINGS / recording / "stream.jsonl").read_text(encoding="utf-8").splitlines():
        body = json.loads(text)["raw_body"]
        if "event" in json.loads(body).get("data", {}):
            bodies.append(body)
    return bodies


def _parent_payload(body: str) -> dict[str, Any]:
    return json.loads(body)["data"]["order"]


def _parent(body: str) -> BrokerOrder:
    return adapter.from_alpaca_order(_parent_payload(body), observed_at_ms=_OBSERVED_AT_MS)


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str) -> None:
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


class _Capture:
    def record(self, **_kwargs: Any) -> bool:
        return True


class _NoClosedOrders:
    async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
        return []


async def _no_backoff() -> None:
    return


async def _deliver(repo: ClerkSqliteRepository, bodies: list[str]) -> None:
    """Run a fresh consumer over these frames, as a connection that delivered them and ended."""

    def frames() -> AsyncIterator[bytes | str]:
        async def generate() -> AsyncIterator[bytes | str]:
            for body in bodies:
                yield body

        return generate()

    await TradeUpdatesConsumer(
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
        ),
        read=cast(BrokerReadPort, _NoClosedOrders()),
        frame_source=frames,
        journal=cast(CaptureJournal, _Capture()),
        backoff=lambda _attempt: _no_backoff(),
        max_reconnects=0,
    ).run()


@pytest.fixture
def repo(tmp_path: Path) -> Any:
    store = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    yield store
    store.close()


def _unfoldable_ids(repo: ClerkSqliteRepository) -> list[str]:
    episode = repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=_UNFOLDABLE, strategy_instance_id=None)
    return [] if episode is None else json.loads(episode["evidence_refs_json"])


def _held_ids(repo: ClerkSqliteRepository) -> list[str]:
    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code=_UNEXPLAINED)
    return [] if hold is None else json.loads(hold["evidence_refs_json"])


def _observations(repo: ClerkSqliteRepository) -> Counter[str]:
    return Counter(
        transition["broker_order_id"]
        for transition in repo.custody_transitions()
        if transition["transition_kind"] == "EXTERNAL_ORDER_OBSERVED"
    )


async def test_the_recorded_outside_credit_spreads_are_custody_of_their_four_legs(
    repo: ClerkSqliteRepository,
) -> None:
    await _deliver(repo, _event_bodies(_CREDIT_SPREADS))

    # Before the fix both parents were contained as unrecordable ("symbol must be non-empty").
    assert _unfoldable_ids(repo) == []
    rows = {row["broker_order_id"]: row for row in repo.external_orders()}
    assert set(rows) == set(_LEGS)
    for leg_id, (symbol, side, order_type, price, parent, sibling) in _LEGS.items():
        row = rows[leg_id]
        assert (row["symbol"], row["side"], row["qty"], row["order_type"], row["filled_avg_price"]) == (
            symbol, side, 1.0, order_type, price,
        )
        # The leg is not pretended to be a bot's, and says which parent and sibling it came with.
        assert row["client_order_id"] == _CLIENT_ORDER_ID
        assert row["evidence_refs"] == tuple(
            sorted({leg_id, f"mleg-parent:{_id(parent)}", f"mleg-leg:{_id(sibling)}"})
        )
        assert row["acknowledged_at_ms"] is None
        assert repo.fills_for_order(leg_id) == []
    assert repo.external_order(_id(354)) is None and repo.external_order(_id(362)) is None
    assert repo.attributed_positions_by_symbol() == {}
    # The hold names exactly what can be acknowledged: the legs, never a parent no row stands for.
    assert _held_ids(repo) == sorted(_LEGS)


async def test_redelivering_the_same_broker_state_appends_nothing(repo: ClerkSqliteRepository) -> None:
    bodies = _event_bodies(_CREDIT_SPREADS)
    await _deliver(repo, bodies)

    # Each leg was observed once per state its parent reported: pending_new, new, filled. The legs'
    # own fill frames restated the filled state and added nothing.
    assert _observations(repo) == Counter(dict.fromkeys(_LEGS, 3))
    chain = len(repo.custody_transitions())

    await _deliver(repo, bodies[-1:])
    await _deliver(repo, bodies[2:3])

    assert len(repo.custody_transitions()) == chain


async def test_a_cancel_after_review_asks_for_a_new_review_and_a_replay_does_not(
    repo: ClerkSqliteRepository,
) -> None:
    pending, new, canceled = _event_bodies(_RESTING_ORDER)[:3]
    resting = (_id(370), _id(371))
    await _deliver(repo, [pending, new])
    assert _held_ids(repo) == sorted(resting)
    for leg_id in resting:
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")
    assert _held_ids(repo) == []
    chain = len(repo.custody_transitions())

    await _deliver(repo, [new])
    assert len(repo.custody_transitions()) == chain
    assert _held_ids(repo) == []

    await _deliver(repo, [canceled])

    for leg_id in resting:
        row = repo.external_order(leg_id)
        assert row is not None and row.acknowledged_at_ms is None and row.broker_state == "canceled"
    assert _held_ids(repo) == sorted(resting)
    for leg_id in resting:
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")
    assert _held_ids(repo) == []


async def test_learning_the_parent_of_a_reviewed_leg_does_not_reopen_its_review(
    repo: ClerkSqliteRepository,
) -> None:
    """A leg recorded before this fix has a placeholder client id and no parent. Reviewed, it stays so."""
    fill = _event_bodies(_CREDIT_SPREADS)[2]
    for leg in _parent_payload(fill)["legs"]:
        observe_external_order(
            repo,
            order=adapter.from_alpaca_order({**leg, "client_order_id": None}, observed_at_ms=_OBSERVED_AT_MS),
        )
        acknowledge_external_order(repo, external_order_id=leg["id"], operator="operator-1")
    assert _held_ids(repo) == []

    await _deliver(repo, [fill])

    assert _held_ids(repo) == []
    assert _unfoldable_ids(repo) == []
    for leg_id in (_id(356), _id(358)):
        row = repo.external_order(leg_id)
        assert row is not None and row.acknowledged_at_ms is not None
        assert row.client_order_id == _CLIENT_ORDER_ID
        assert f"mleg-parent:{_id(354)}" in row.evidence_refs


async def test_a_group_interrupted_between_its_legs_cannot_be_reviewed_until_complete(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _parent(_event_bodies(_CREDIT_SPREADS)[1])
    append = repo.append_external_order_observation_if_changed
    calls = 0

    def die_after_the_first_leg(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("the process died between two legs")
        return append(**kwargs)

    monkeypatch.setattr(repo, "append_external_order_observation_if_changed", die_after_the_first_leg)
    with pytest.raises(RuntimeError, match="died"):
        observe_or_record_unfoldable(repo, order=parent)
    assert [row["broker_order_id"] for row in repo.external_orders()] == [_id(356)]

    with pytest.raises(ExternalOrderReviewIncompleteError, match=_id(358)):
        acknowledge_external_order(repo, external_order_id=_id(356), operator="operator-1")
    assert _held_ids(repo) == [_id(356)]

    monkeypatch.undo()
    assert observe_or_record_unfoldable(repo, order=parent) == "observed"
    acknowledge_external_order(repo, external_order_id=_id(356), operator="operator-1")
    assert _held_ids(repo) == [_id(358)]


def _tamper(label: str, payload: dict[str, Any]) -> None:
    legs = payload["legs"]
    if label == "one leg":
        payload["legs"] = legs[:1]
    elif label == "an unreadable leg value":
        legs[0]["filled_qty"] = "many"
    elif label == "a leg with no symbol":
        legs[1]["symbol"] = ""
    elif label == "a parent that states a symbol":
        payload["symbol"] = legs[0]["symbol"]
    elif label == "a leg that is the parent":
        legs[0]["id"] = payload["id"]
    elif label == "two legs with one id":
        legs[1]["id"] = legs[0]["id"]
    elif label == "legs that are not a list":
        payload["legs"] = "SPY261016C00776000"
    else:
        raise AssertionError(label)


@pytest.mark.parametrize(
    "label",
    [
        "one leg",
        "an unreadable leg value",
        "a leg with no symbol",
        "a parent that states a symbol",
        "a leg that is the parent",
        "two legs with one id",
        "legs that are not a list",
    ],
)
def test_a_parent_whose_legs_cannot_be_stated_is_still_contained_unrecorded(
    repo: ClerkSqliteRepository, label: str
) -> None:
    payload = copy.deepcopy(_parent_payload(_event_bodies(_CREDIT_SPREADS)[1]))
    _tamper(label, payload)

    outcome = observe_or_record_unfoldable(
        repo, order=adapter.from_alpaca_order(payload, observed_at_ms=_OBSERVED_AT_MS)
    )

    assert outcome == "unfoldable"
    assert _unfoldable_ids(repo) == [_id(354)]
    # Nothing is half-recorded: not one leg of a group the Clerk cannot state.
    assert repo.external_orders() == []
    assert _held_ids(repo) == []


class _Snapshot:
    """A reconciliation read of an account whose only order is the given one."""

    def __init__(self, orders: list[BrokerOrder]) -> None:
        self._orders = orders

    async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        return []

    async def read_activity_evidence(self, **_kwargs: Any) -> BrokerActivityEvidence:
        return BrokerActivityEvidence(activities=[], history_complete=True)


class _Trade:
    async def get_order_by_client_order_id(self, client_order_id: str) -> None:
        del client_order_id

    async def submit(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("reconciliation must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("reconciliation must not cancel")


async def test_a_sweep_holds_an_outside_parent_by_its_legs_and_releases_when_they_are_reviewed(
    repo: ClerkSqliteRepository,
) -> None:
    snapshot = _Snapshot([_parent(_event_bodies(_RESTING_ORDER)[1])])
    resting = (_id(370), _id(371))

    async def sweep() -> str:
        result = await reconcile_account(
            repo, read=cast(Any, snapshot), trade=cast(Any, _Trade()), pricing=UNPRICEABLE_RECOVERY
        )
        return result.verdict

    assert await sweep() == "unexplained_order"
    # The hold names the legs a review can release, not the parent no row stands for.
    assert _held_ids(repo) == sorted(resting)
    assert _unfoldable_ids(repo) == []
    chain = len(repo.custody_transitions())
    await sweep()
    assert len(repo.custody_transitions()) == chain

    for leg_id in resting:
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")
    await sweep()

    assert _held_ids(repo) == []
    assert len(repo.custody_transitions()) == chain + 2


def _legs_with_their_own_client_order_ids(body: str) -> str:
    """The frame as Alpaca documents a multi-leg answer: each leg has a client order id of its own."""
    message = json.loads(body)
    for leg in message["data"]["order"]["legs"]:
        leg["client_order_id"] = f"leg-client-{leg['id'][-3:]}"
    return json.dumps(message)


async def test_a_leg_left_working_by_a_missed_cancel_is_refreshed_by_its_own_exact_answer(
    repo: ClerkSqliteRepository,
) -> None:
    _, new, canceled = (_legs_with_their_own_client_order_ids(body) for body in _event_bodies(_RESTING_ORDER)[:3])
    await _deliver(repo, [new])
    answers = {
        leg.client_order_id: leg
        for leg in adapter.from_alpaca_order(_parent_payload(canceled), observed_at_ms=2**41).legs
    }

    class _ExactLookup(_Trade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            return answers.get(client_order_id)

    await reconcile_account(
        repo, read=cast(Any, _Snapshot([])), trade=cast(Any, _ExactLookup()), pricing=UNPRICEABLE_RECOVERY
    )

    for leg_id in (_id(370), _id(371)):
        row = repo.external_order(leg_id)
        assert row is not None and row.broker_state == "canceled"
        assert row.evidence_refs == tuple(
            sorted({leg_id, f"mleg-parent:{_id(369)}", f"mleg-leg:{_id(371 if leg_id == _id(370) else 370)}"})
        )


async def test_a_leg_the_open_orders_already_show_is_not_looked_up_again(repo: ClerkSqliteRepository) -> None:
    resting = _parent(_event_bodies(_RESTING_ORDER)[1])
    snapshot = _Snapshot([resting])

    class _NoLookup(_Trade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> None:
            raise AssertionError(f"a leg of an open parent was looked up by {client_order_id}")

    await reconcile_account(repo, read=cast(Any, snapshot), trade=cast(Any, _NoLookup()), pricing=UNPRICEABLE_RECOVERY)
    await reconcile_account(repo, read=cast(Any, snapshot), trade=cast(Any, _NoLookup()), pricing=UNPRICEABLE_RECOVERY)


async def test_unreviewed_legs_keep_holding_after_their_parent_leaves_the_open_orders(
    repo: ClerkSqliteRepository,
) -> None:
    """The sweep holds from the open orders, but a leg awaiting review is held until it is reviewed."""
    await _deliver(repo, _event_bodies(_CREDIT_SPREADS))
    snapshot = _Snapshot([])

    async def sweep() -> None:
        await reconcile_account(repo, read=cast(Any, snapshot), trade=cast(Any, _Trade()), pricing=UNPRICEABLE_RECOVERY)

    await sweep()
    assert _held_ids(repo) == sorted(_LEGS)

    for leg_id in sorted(_LEGS):
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")
    await sweep()
    assert _held_ids(repo) == []


async def test_a_reopened_review_survives_the_next_sweep(repo: ClerkSqliteRepository) -> None:
    pending, new, canceled = _event_bodies(_RESTING_ORDER)[:3]
    resting = (_id(370), _id(371))
    await _deliver(repo, [pending, new])
    for leg_id in resting:
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")
    await _deliver(repo, [canceled])

    await reconcile_account(repo, read=cast(Any, _Snapshot([])), trade=cast(Any, _Trade()), pricing=UNPRICEABLE_RECOVERY)

    assert _held_ids(repo) == sorted(resting)
    for leg_id in resting:
        row = repo.external_order(leg_id)
        assert row is not None and row.acknowledged_at_ms is None


async def test_an_older_read_of_a_reviewed_leg_does_not_reopen_its_review(repo: ClerkSqliteRepository) -> None:
    """A leg reviewed as cancelled is not un-reviewed by a read that still shows it working."""
    pending, new, canceled = _event_bodies(_RESTING_ORDER)[:3]
    await _deliver(repo, [pending, new, canceled])
    for leg_id in (_id(370), _id(371)):
        acknowledge_external_order(repo, external_order_id=leg_id, operator="operator-1")

    await _deliver(repo, [new])

    assert _held_ids(repo) == []
    for leg_id in (_id(370), _id(371)):
        row = repo.external_order(leg_id)
        assert row is not None and row.acknowledged_at_ms is not None


@pytest.mark.parametrize("reviewed_again", [False, True], ids=["reopened", "reviewed-again"])
async def test_a_store_rebuilt_from_its_mirror_folds_a_reopened_review_as_the_live_store_did(
    tmp_path: Path, reviewed_again: bool
) -> None:
    pending, new, canceled = _event_bodies(_RESTING_ORDER)[:3]
    live = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    await _deliver(live, [pending, new])
    for leg_id in (_id(370), _id(371)):
        acknowledge_external_order(live, external_order_id=leg_id, operator="operator-1")
    await _deliver(live, [canceled])
    if reviewed_again:
        for leg_id in (_id(370), _id(371)):
            acknowledge_external_order(live, external_order_id=leg_id, operator="operator-2")
    expected = (live.external_orders(), live.active_hold(scope="ACCOUNT_CLERK", reason_code=_UNEXPLAINED))
    clock = live.clock
    db_path = live.db_path
    live.close()
    db_path.rename(db_path.with_suffix(".db.preserved"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="PA-TEST", artifacts_root=tmp_path, clock=clock)
    try:
        assert (rebuilt.external_orders(), rebuilt.active_hold(scope="ACCOUNT_CLERK", reason_code=_UNEXPLAINED)) == expected
    finally:
        rebuilt.close()
