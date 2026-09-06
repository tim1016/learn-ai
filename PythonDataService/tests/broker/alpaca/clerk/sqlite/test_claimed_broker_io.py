"""``ClaimedBrokerIO`` tells a lease lost before broker I/O from one lost after it."""

from __future__ import annotations

from typing import Any

import pytest

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
