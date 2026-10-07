"""The read of assignments, exercises and expiries stays outside the Clerk's intake fence.

``test_intake_fence.py`` proves every async port method has a guard method.
This proves what this one does: refuses before the broker is touched while
intake is fenced, and passes the call through unchanged otherwise.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.broker_port_guard import BrokerCallUnderIntakeError, guard_broker_ports
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock

_AFTER_MS = 1_709_269_200_000
_TOKEN = "20240301000000000::fee-0299"


class _EventBroker:
    """A broker that records every option-event read that reached it."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.reached: list[tuple[int, str | None]] = []

    async def read_option_event_activities(self, *, after_ms: int, page_token: str | None = None) -> str:
        self.reached.append((after_ms, page_token))
        return "the option events"


async def test_an_option_event_read_made_while_intake_is_fenced_never_reaches_the_broker() -> None:
    fence = ReentrantAsyncLock()
    broker = _EventBroker()
    read, _trade = guard_broker_ports(read=broker, trade=broker, intake=fence)

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match="read_option_event_activities"):
            await read.read_option_event_activities(after_ms=_AFTER_MS)

    assert broker.reached == []


@pytest.mark.parametrize("page_token", [None, _TOKEN])
async def test_an_option_event_read_made_outside_the_fence_reaches_the_broker_unchanged(
    page_token: str | None,
) -> None:
    broker = _EventBroker()
    read, _trade = guard_broker_ports(read=broker, trade=broker, intake=ReentrantAsyncLock())

    assert await read.read_option_event_activities(after_ms=_AFTER_MS, page_token=page_token) == "the option events"
    assert broker.reached == [(_AFTER_MS, page_token)]
