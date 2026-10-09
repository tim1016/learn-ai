"""``ClaimedBrokerIO`` tells a lease lost before broker I/O from one lost after it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import claimed_broker_io
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.repository import (
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
)


class _Repo:
    """Renews succeed except on one numbered call, which loses the lease."""

    def __init__(self, *, lose_on_call: int) -> None:
        self.calls = 0
        self._lose_on_call = lose_on_call

    def renew_operation_claim(self, *, effect_operation_id: str, token: str, ttl_ms: int = 0) -> bool:
        del effect_operation_id, token, ttl_ms
        self.calls += 1
        if self.calls == self._lose_on_call:
            raise ExecutionLeaseLost("lease lost", account_id="acct-1")
        return True


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
