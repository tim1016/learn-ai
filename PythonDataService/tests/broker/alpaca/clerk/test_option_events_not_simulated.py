"""A simulated broker has no option event to report, and says so as a complete answer.

No broker reports a simulated account's option events. A Dry Run's
simulation settles the contracts it holds past their last session in a step
of its own, and hands those over with their record
(``test_synthetic_broker_option_settlement``): never through this read. So
"none, and that is all" is the truth of the read (#2913). A Live Shadow
takes no option order, and does not pass the live account's events through:
they end positions its book never held.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume

_AFTER_MS = 1_709_269_200_000
_NONE_AND_COMPLETE = BrokerOptionEventEvidence(events=(), share_trades=(), history_complete=True)
_RESUME = BrokerOptionEventResume(page_token="a-token", last_page_oldest_ms=_AFTER_MS)


class _LiveRead:
    """The live Alpaca read port a shadow world reads account truth through."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.option_event_reads = 0

    def capabilities(self) -> Any:
        return ALPACA_LIVE_CAPABILITIES

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> str:
        self.option_event_reads += 1
        return "the live account's option events"


async def test_the_simulation_reports_no_option_events_and_that_none_are_missing() -> None:
    evidence = await SyntheticBroker(account_id="sim:options-1").read_option_event_activities(after_ms=_AFTER_MS)

    assert evidence == _NONE_AND_COMPLETE
    assert evidence.read_whole is True


async def test_a_shadow_world_reports_none_of_the_live_accounts_option_events_as_its_own(tmp_path: Path) -> None:
    live = _LiveRead()
    ports = compose_shadow_ports(live_read=live, live_account_id="9LIVE0001", artifacts_root=tmp_path)

    evidence = await ports.read.read_option_event_activities(after_ms=_AFTER_MS)

    assert evidence == _NONE_AND_COMPLETE
    # A resume it never issued changes nothing: there is no walk of its own to go on with.
    assert await ports.read.read_option_event_activities(after_ms=_AFTER_MS, resume=_RESUME) == _NONE_AND_COMPLETE
    assert live.option_event_reads == 0


async def test_the_simulations_answer_reaches_a_caller_through_the_clerks_guarded_ports() -> None:
    simulation = SyntheticBroker(account_id="sim:options-1")
    read, _trade = guard_broker_ports(read=simulation, trade=simulation, intake=ReentrantAsyncLock())

    assert await read.read_option_event_activities(after_ms=_AFTER_MS, resume=_RESUME) == _NONE_AND_COMPLETE
