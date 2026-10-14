"""``ClaimedBrokerIO`` tells a lease or a claim lost before broker I/O from one lost after it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import claimed_broker_io
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.repository import (
    ClaimLostAfterBrokerIO,
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
    OperationClaimError,
)
from app.broker.contract.errors import BrokerUnavailable


class _Repo:
    """Renews succeed except on one numbered call, which loses the lease.

    ``claim_gone_from_call`` is the other loss, from that call on: the store
    answers that the claim token is no longer the live one
    (``renew_operation_claim`` returns False).
    """

    def __init__(self, *, lose_on_call: int = 0, claim_gone_from_call: int = 0) -> None:
        self.calls = 0
        self._lose_on_call = lose_on_call
        self._claim_gone_from_call = claim_gone_from_call

    def renew_operation_claim(self, *, effect_operation_id: str, token: str, ttl_ms: int = 0) -> bool:
        del effect_operation_id, token, ttl_ms
        self.calls += 1
        if self.calls == self._lose_on_call:
            raise ExecutionLeaseLost("lease lost", account_id="acct-1")
        return not (0 < self._claim_gone_from_call <= self.calls)


class _Trade:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    async def submit(self, leg: Any, *, client_order_id: str) -> Any:
        del leg
        self.submitted.append(client_order_id)
        return object()

    async def cancel(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)


def _io(repo: _Repo, trade: _Trade) -> ClaimedBrokerIO:
    return ClaimedBrokerIO(repo=repo, effect_operation_id="op-1", claim_token="tok", trade=trade)  # type: ignore[arg-type]


async def test_a_lease_lost_before_the_broker_is_called_is_the_plain_loss() -> None:
    trade = _Trade()
    with pytest.raises(ExecutionLeaseLost) as lost:
        await _io(_Repo(lose_on_call=1), trade).submit(object(), client_order_id="c-1")

    assert not isinstance(lost.value, ExecutionLeaseLostAfterBrokerIO)
    assert trade.submitted == []


async def test_a_lease_lost_after_a_submit_names_the_broker_call_that_already_happened() -> None:
    trade = _Trade()
    with pytest.raises(ExecutionLeaseLostAfterBrokerIO) as lost:
        await _io(_Repo(lose_on_call=2), trade).submit(object(), client_order_id="c-1")

    # The order went out; the caller must not tell anyone "nothing applied".
    assert trade.submitted == ["c-1"]
    assert lost.value.account_id == "acct-1"


async def test_a_lease_lost_after_a_cancel_names_the_broker_call_too() -> None:
    trade = _Trade()
    with pytest.raises(ExecutionLeaseLostAfterBrokerIO):
        await _io(_Repo(lose_on_call=2), trade).cancel("b-1", order_ref="c-1")

    assert trade.cancelled == ["b-1"]


async def test_a_slow_send_keeps_renewing_its_claim_until_broker_io_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RENEWAL_S", 0.01)
    repo = _Repo(lose_on_call=999)
    finished = asyncio.Event()

    class _SlowTrade(_Trade):
        async def submit(self, leg: Any, *, client_order_id: str) -> Any:
            self.submitted.append(client_order_id)
            await finished.wait()
            return object()

    trade = _SlowTrade()
    send = asyncio.create_task(_io(repo, trade).submit(object(), client_order_id="c-1"))
    try:
        await asyncio.sleep(0.04)
        assert trade.submitted == ["c-1"]
        assert not send.done()
        assert repo.calls >= 3
    finally:
        finished.set()
    await send
    assert repo.calls >= 4


async def test_a_transient_claim_renewal_error_is_retried_while_send_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RENEWAL_S", 0.01)
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RETRY_S", 0.01)

    class _TransientRepo(_Repo):
        def renew_operation_claim(self, *, effect_operation_id: str, token: str, ttl_ms: int = 0) -> bool:
            del effect_operation_id, token, ttl_ms
            self.calls += 1
            if self.calls == 2:
                raise OSError("transient database error")
            return True

    class _SlowTrade(_Trade):
        async def submit(self, leg: Any, *, client_order_id: str) -> Any:
            self.submitted.append(client_order_id)
            await finished.wait()
            return object()

    repo = _TransientRepo(lose_on_call=999)
    finished = asyncio.Event()
    send = asyncio.create_task(_io(repo, _SlowTrade()).submit(object(), client_order_id="c-1"))
    try:
        for _ in range(100):
            if repo.calls >= 3:
                break
            await asyncio.sleep(0.005)
        assert repo.calls >= 3
        assert not send.done()
    finally:
        finished.set()
    await send


async def test_a_claim_proven_lost_during_send_is_reported_as_post_broker_io_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RENEWAL_S", 0.01)
    repo = _Repo(lose_on_call=2)
    finished = asyncio.Event()

    class _SlowTrade(_Trade):
        async def submit(self, leg: Any, *, client_order_id: str) -> Any:
            self.submitted.append(client_order_id)
            await finished.wait()
            return object()

    trade = _SlowTrade()
    send = asyncio.create_task(_io(repo, trade).submit(object(), client_order_id="c-1"))
    try:
        for _ in range(100):
            if repo.calls >= 2:
                break
            await asyncio.sleep(0.005)
        assert repo.calls >= 2
    finally:
        finished.set()
    with pytest.raises(ExecutionLeaseLostAfterBrokerIO) as lost:
        await send
    assert lost.value.account_id == "acct-1"
    assert trade.submitted == ["c-1"]


class _UnavailableTrade(_Trade):
    """A broker that cannot be reached, so what the call did there is unknown."""

    async def submit(self, leg: Any, *, client_order_id: str) -> Any:
        await super().submit(leg, client_order_id=client_order_id)
        raise BrokerUnavailable("response lost", broker="alpaca")

    async def cancel(self, broker_order_id: str) -> None:
        await super().cancel(broker_order_id)
        raise BrokerUnavailable("response lost", broker="alpaca")


async def test_a_claim_lost_after_a_submit_names_the_broker_call_that_already_happened() -> None:
    trade = _Trade()
    with pytest.raises(ClaimLostAfterBrokerIO) as lost:
        await _io(_Repo(claim_gone_from_call=2), trade).submit(object(), client_order_id="c-1")

    # The broker answered, so a plain claim error ("nothing was sent") would be untrue. The claim
    # is still not the lease: the sweep defers such an effect, it does not fail its pass over it.
    assert trade.submitted == ["c-1"]
    assert isinstance(lost.value, OperationClaimError)
    assert not isinstance(lost.value, ExecutionLeaseLost)


async def test_a_claim_lost_after_a_cancel_names_the_broker_call_too() -> None:
    trade = _Trade()
    with pytest.raises(ClaimLostAfterBrokerIO):
        await _io(_Repo(claim_gone_from_call=2), trade).cancel("b-1", order_ref="c-1")

    assert trade.cancelled == ["b-1"]


async def test_a_claim_lost_before_the_broker_is_called_is_the_plain_claim_error() -> None:
    trade = _Trade()
    with pytest.raises(OperationClaimError) as lost:
        await _io(_Repo(claim_gone_from_call=1), trade).submit(object(), client_order_id="c-1")

    assert not isinstance(lost.value, ClaimLostAfterBrokerIO)
    assert trade.submitted == []


@pytest.mark.parametrize("call", ["submit", "cancel"])
async def test_a_claim_lost_after_a_broker_error_still_names_the_broker_call(call: str) -> None:
    """A transport error leaves the outcome unknown; it must not read as "nothing was sent"."""
    trade = _UnavailableTrade()
    io = _io(_Repo(claim_gone_from_call=2), trade)

    with pytest.raises(ClaimLostAfterBrokerIO):
        if call == "submit":
            await io.submit(object(), client_order_id="c-1")
        else:
            await io.cancel("b-1", order_ref="c-1")


@pytest.mark.parametrize("call", ["submit", "cancel"])
async def test_a_lease_lost_after_a_broker_error_still_names_the_broker_call(call: str) -> None:
    trade = _UnavailableTrade()
    io = _io(_Repo(lose_on_call=2), trade)

    with pytest.raises(ExecutionLeaseLostAfterBrokerIO):
        if call == "submit":
            await io.submit(object(), client_order_id="c-1")
        else:
            await io.cancel("b-1", order_ref="c-1")


async def test_a_claim_proven_lost_during_a_send_is_the_claim_error_not_a_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RENEWAL_S", 0.01)
    repo = _Repo(claim_gone_from_call=2)
    trade = _HangingTrade()
    task = asyncio.create_task(_io(repo, trade).submit(object(), client_order_id="c-1"))
    await trade.in_flight.wait()
    for _ in range(200):
        if repo.calls >= 2:
            break
        await asyncio.sleep(0.005)
    trade.release.set()

    with pytest.raises(ClaimLostAfterBrokerIO) as lost:
        await task
    assert not isinstance(lost.value, ExecutionLeaseLost)


class _HangingTrade(_Trade):
    """A broker call that never answers, so the task is cancelled while it is in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, leg: Any, *, client_order_id: str) -> Any:
        self.submitted.append(client_order_id)
        self.in_flight.set()
        await self.release.wait()
        return object()

    async def cancel(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)
        self.in_flight.set()
        await self.release.wait()


@pytest.fixture
def folds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(claimed_broker_io, "fold_uncertain", lambda repo, **fold: recorded.append(fold))
    return recorded


async def _cancel_while_in_flight(call: str, repo: _Repo) -> asyncio.Task[Any]:
    trade = _HangingTrade()
    io = _io(repo, trade)
    task = asyncio.create_task(
        io.submit(object(), client_order_id="c-1") if call == "submit" else io.cancel("b-1", order_ref="c-1")
    )
    await trade.in_flight.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return task


@pytest.mark.parametrize("call", ["submit", "cancel"])
async def test_cancelling_a_call_whose_claim_is_gone_stays_a_cancellation(
    call: str, folds: list[dict[str, Any]]
) -> None:
    task = await _cancel_while_in_flight(call, _Repo(claim_gone_from_call=2))

    # A task group must see its own cancellation, not an OperationClaimError in its place.
    assert task.cancelled()
    assert folds == []


async def test_cancelling_a_send_whose_claim_renewal_already_failed_stays_a_cancellation(
    monkeypatch: pytest.MonkeyPatch, folds: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(claimed_broker_io, "_SEND_CLAIM_RENEWAL_S", 0.01)
    repo = _Repo(claim_gone_from_call=2)
    trade = _HangingTrade()
    task = asyncio.create_task(_io(repo, trade).submit(object(), client_order_id="c-1"))
    await trade.in_flight.wait()
    for _ in range(200):
        if repo.calls >= 2:
            break
        await asyncio.sleep(0.005)
    assert repo.calls >= 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The in-flight renewal proved the claim lost; the cancel must still surface as a cancel.
    assert task.cancelled()
    assert folds == []


@pytest.mark.parametrize("call", ["submit", "cancel"])
async def test_cancelling_a_call_that_still_holds_its_claim_folds_the_unknown(
    call: str, folds: list[dict[str, Any]]
) -> None:
    task = await _cancel_while_in_flight(call, _Repo())

    assert task.cancelled()
    assert [fold["order_ref"] for fold in folds] == ["c-1"]
