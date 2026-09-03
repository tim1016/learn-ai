"""One reconnect, end to end through the real chain (#1921).

Every other test in this family pins one link: the feed loop with a fake sink,
the policy with a fake feed, the ledger with hand-built events, the digest with
hand-built rows. Nothing joined them, so a mismatch at a seam -- a policy the
wrapper never forwards, an event the ledger writes out of order, a
``continuity_event_ref`` stamped from the wrong reference, a digest that does
not survive SQLite -- would pass every one of them.

This test uses the real seal builder, the real ``continuity_policy_for``, the
real ``_RetainedSourceBarFeed``, the real ``IbkrMarketDataFeed`` loop with its
real ``MinuteAssembler``, a real ``SourceBarLedger`` on disk, and the real
digest. Only two things are doubles: the IBKR client (a socket) and
``stream_minute_bars`` (the vendor's wire), which is the scripted source the
feed-loop tests already use.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.ibkr.bars import IBKRBarInterrupted
from app.marketdata import ibkr_feed as feed_module
from app.marketdata.feed import ContinuityEventRef, FeedContinuityEvent
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.bot_trade_strategy import _RetainedSourceBarFeed
from app.services.feed_continuity_policy import continuity_policy_for
from app.services.run_replay_proof import continuity_event_digest
from app.services.source_bar_ledger import RetainedContinuityEvent, SourceBarLedger
from tests.marketdata.test_feed_continuity import _client, _ibkr_bar, _Source
from tests.services.test_signal_program_admission import _sealed_binding

# 15:01 and 15:02 ET on 2026-09-02. Neither close is a decision trigger for the
# seal's 15-minute clock (those fall at 15:01:00 and 15:16:00 *bucket* + 60 s),
# so the recovered bar is admitted on its own terms rather than on the wall
# clock this suite happens to run at.
_BAR_ONE_START_MS = 1_788_375_660_000
_BAR_TWO_START_MS = 1_788_375_720_000


@pytest.fixture(autouse=True)
def _no_reconnect_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auto-reconnect monitor is a process singleton, not part of this chain."""
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: None)


async def test_one_reconnect_lands_in_the_ledger_as_ordered_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _sealed_binding()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=binding.sealed_account_id)
    try:
        policy = continuity_policy_for(binding, ledger)
        assert policy is not None, "a sealed RTH binding must carry a continuity policy"

        emitted: list[FeedContinuityEvent] = []
        sink = policy.record_event

        async def _spy(event: FeedContinuityEvent) -> ContinuityEventRef:
            emitted.append(event)
            return await sink(event)

        # Everything else about the policy -- decision clock, substitution
        # authority, allowance -- stays the real object's; only the sink is
        # observed, so the events the feed emitted can be compared with the
        # events the ledger reads back.
        policy = replace(policy, record_event=_spy)

        source = _Source(
            [_ibkr_bar(_BAR_ONE_START_MS), IBKRBarInterrupted("x", cause="socket_down")],
            [_ibkr_bar(_BAR_TWO_START_MS, contribution_count=12, spans_interruption=True)],
        )
        monkeypatch.setattr(feed_module, "stream_minute_bars", source)
        retained = _RetainedSourceBarFeed(
            IbkrMarketDataFeed(_client()),
            ledger,
            run_id=binding.run_id,
            continuity=policy,
        )

        delivered = []
        async for bar in retained.stream_bars("SPY", use_rth=True):
            delivered.append(bar)
            if len(delivered) == 2:
                break

        events = ledger.events(run_id=binding.run_id)
        rows = ledger.bars(provider="ibkr", symbol="SPY")

        # One causal order over bars and events: the interruption and the
        # recovery are both *between* the bar that preceded them and the bar
        # they explain, not merely near it in wall-clock time.
        assert [event.kind for event in events] == ["interruption", "recovered"]
        assert [row.start_ms for row in rows] == [_BAR_ONE_START_MS, _BAR_TWO_START_MS]
        assert (
            rows[0].evidence_seq
            < events[0].evidence_seq
            < events[1].evidence_seq
            < rows[1].evidence_seq
        )

        # The recovered bar names the event that explains it, and every row
        # names the run whose evidence bound has to be able to reach it.
        assert rows[1].provenance == "realtime_across_reconnect"
        assert rows[1].continuity_event_ref == f"{binding.run_id}:{events[1].evidence_seq}"
        assert rows[0].provenance == "realtime" and rows[0].continuity_event_ref is None
        assert {row.run_id for row in rows} == {binding.run_id}

        # What the receipt commits to is what the feed said, after a full
        # round trip through SQLite.
        expected = [
            RetainedContinuityEvent(
                seq=index, run_id=binding.run_id, evidence_seq=index, **event.model_dump()
            )
            for index, event in enumerate(emitted, start=1)
        ]
        assert continuity_event_digest(events) == continuity_event_digest(expected)
    finally:
        ledger.close()
