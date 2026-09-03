"""Paper trade runs retain their source bars (Direction 2, deliverable 1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataBar,
    MarketDataFeedError,
    SubstitutionRefusal,
)
from app.services.bot_runtime import PauseAwareFeed, execute_bot_run
from app.services.bot_trade_strategy import _RetainedSourceBarFeed, run_trade_bot
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import (  # noqa: F401 -- autouse fixture
    _binding,
    _fresh_live_market_liveness,
    _PhaseFeed,
)

_T0 = 1_700_000_000_000


class _MixedPhaseFeed:
    """Yields bars across session phases so the RTH filter has something to drop."""

    feed_id = "mixed-phase"

    def __init__(self, bars: list[MarketDataBar]) -> None:
        self._bars = bars

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth, continuity
        for bar in self._bars:
            yield bar


def _phase_bar(index: int, phase: str) -> MarketDataBar:
    start = _T0 + index * 60_000
    return MarketDataBar(
        symbol="SPY",
        start_ms=start,
        end_ms=start + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=100,
        fetched_at_ms=start + 60_500,
        feed_id="mixed-phase",
        session_phase=phase,
    )


def _bar(start_ms: int) -> MarketDataBar:
    """An RTH bar whose ``feed_id`` is ``_FakeFeed``'s own.

    The ledger keys a retained row by the *bar's* provider, so a bar streamed
    by the fake must say ``fake`` for ``ledger.bars(provider="fake", ...)`` to
    find it again.
    """
    return _phase_bar(0, "RTH").model_copy(
        update={
            "start_ms": start_ms,
            "end_ms": start_ms + 60_000,
            "fetched_at_ms": start_ms + 60_500,
            "feed_id": "fake",
        }
    )


@pytest.mark.asyncio
async def test_retained_feed_consumes_modes_of_filtered_bars(tmp_path: Path) -> None:
    """Filtered (non-RTH) bars must not leak captured evaluation modes.

    The session pops a captured mode only for bars it actually evaluates, and
    it never sees a bar the retained feed filters out. Without the retained
    feed consuming those modes, PauseAwareFeed's map would grow unbounded over
    a long-running paper session (Codex PR #1764 P2).
    """
    gate = asyncio.Event()
    gate.set()  # DECIDE mode
    bars = [_phase_bar(0, "PRE"), _phase_bar(1, "RTH"), _phase_bar(2, "POST")]
    pause_feed = PauseAwareFeed(_MixedPhaseFeed(bars), gate)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:leak")
    try:
        retained = _RetainedSourceBarFeed(pause_feed, ledger, run_id="run-1")

        yielded = [bar async for bar in retained.stream_bars("SPY", use_rth=True)]

        assert [bar.session_phase for bar in yielded] == ["RTH"]
        # Only the yielded RTH bar's mode remains (the real session pops it as
        # it evaluates); the two filtered bars leaked nothing.
        assert set(pause_feed._captured_modes) == {
            (bar.symbol, bar.start_ms, bar.end_ms) for bar in yielded
        }
        # And all three bars were still durably retained (capture-first).
        assert len(ledger.bars(provider="mixed-phase", symbol="SPY")) == 3
    finally:
        ledger.close(checkpoint=False)


def _continuity_policy() -> ContinuityPolicy:
    """A policy whose callables are never invoked -- its identity is the assertion.

    Forwarding tests care only that the caller's exact object reaches the
    source feed, so every callable here raises or returns a constant.
    """

    async def _record(event: FeedContinuityEvent) -> ContinuityEventRef:  # pragma: no cover - never called
        raise AssertionError("forwarding a policy must not record a continuity event")

    return ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=lambda last_end_ms: last_end_ms + 60_000,
        substitution_grant=lambda start_ms, end_ms: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=_record,
    )


def _recording_policy(*, trigger_ms: int) -> tuple[ContinuityPolicy, list[FeedContinuityEvent]]:
    """A policy with one fixed decision instant and a sink that just collects.

    The admission tests below turn on exactly two things -- whether a bar's
    close is the trigger, and what the feed recorded -- so the clock is a
    constant and the sink keeps the events in memory rather than in SQLite.
    """
    events: list[FeedContinuityEvent] = []

    async def _sink(event: FeedContinuityEvent) -> ContinuityEventRef:
        events.append(event)
        return ContinuityEventRef(run_id="run-x", evidence_seq=len(events))

    policy = ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=lambda last_end_ms: trigger_ms,
        substitution_grant=lambda start_ms, end_ms: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=_sink,
    )
    return policy, events


def _serving_warmup(bars: list[MarketDataBar]):
    """Replace ``_FakeFeed.recent_closed_bars``, which serves no warmup by default."""

    async def _recent(symbol: str, *, use_rth: bool = True, lookback_days: int = 5):
        del symbol, use_rth, lookback_days
        return list(bars)

    return _recent


@pytest.mark.asyncio
async def test_pause_aware_feed_forwards_the_continuity_policy_to_its_source() -> None:
    """#1921: the pause wrapper is transparent to continuity.

    Pause gating and reconnect continuity are independent concerns. If this
    wrapper dropped the kwarg, a bot behind a run gate would silently lose the
    continuity signal -- exactly the failure class #1921 exists to close, and
    one a green suite would otherwise never notice.
    """
    gate = asyncio.Event()
    gate.set()
    source = _FakeFeed([_phase_bar(0, "RTH")], mode="finite")
    policy = _continuity_policy()
    feed = PauseAwareFeed(source, gate)

    yielded = [bar async for bar in feed.stream_bars("SPY", continuity=policy)]

    assert [bar.session_phase for bar in yielded] == ["RTH"]
    assert source.continuity_seen is policy


@pytest.mark.asyncio
async def test_retained_feed_refuses_a_continuity_policy_that_is_not_its_runs(tmp_path: Path) -> None:
    """#1921 (ruling P1): the run's policy is authored once, at construction.

    This wrapper already rewrites ``use_rth`` on the way through (capture
    first, filter locally), and continuity is the same shape of decision: the
    run owns its decision clock and its evidence sink, so the policy the
    constructor was given -- never one a caller passes per stream -- is what
    reaches the source. The Protocol's ``continuity`` kwarg stays on
    ``stream_bars`` for conformance; a *different* policy is refused rather
    than dropped, because dropping one would retarget the run's evidence
    without saying so.
    """
    source = _FakeFeed([_phase_bar(0, "RTH")], mode="finite")
    policy = _continuity_policy()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:continuity")
    try:
        retained = _RetainedSourceBarFeed(source, ledger, run_id="run-1", continuity=policy)

        with pytest.raises(ValueError, match="may not substitute"):
            async for _bar in retained.stream_bars("SPY", use_rth=True, continuity=_continuity_policy()):
                pass
        assert source.continuity_seen is None  # the source was never opened

        yielded = [
            bar async for bar in retained.stream_bars("SPY", use_rth=True, continuity=policy)
        ]

        assert [bar.session_phase for bar in yielded] == ["RTH"]
        assert source.continuity_seen is policy
    finally:
        ledger.close(checkpoint=False)


@pytest.mark.asyncio
async def test_run_trade_bot_retains_every_live_source_bar(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        bars = _ema_parity_bars_through_first_exit()

        await run_trade_bot(_binding(run_id="run-1"), _PhaseFeed(live_bars=bars), source_bars=ledger)

        # Retention keys each row by ``bar.feed_id`` (the canonical stream
        # identity), which for this golden fixture is "lean-golden" — in
        # production the feed's ``feed_id`` and its bars' ``feed_id`` coincide.
        provider = bars[0].feed_id
        retained = ledger.bars(provider=provider, symbol="SPY")
        assert [row.end_ms for row in retained] == [bar.end_ms for bar in bars]
        assert [str(row.close) for row in retained] == [str(bar.close) for bar in bars]
        # The run's own identity reaches the rows, so its continuity events
        # (journalled under the same run_id) can be ordered against them.
        assert {row.run_id for row in retained} == {"run-1"}
    finally:
        ledger.close()
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_retained_feed_appends_bars_with_the_run_id_and_provenance(tmp_path: Path) -> None:
    """A retained observation must say which run it is evidence for (#1921).

    Continuity events are journalled per run; a bar retained without the run's
    identity could not be ordered against them.
    """
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        feed = _RetainedSourceBarFeed(_FakeFeed([_bar(_T0)], mode="finite"), ledger, run_id="run-x")

        async for _ in feed.stream_bars("SPY", use_rth=True):
            pass

        retained = ledger.bars(provider="fake", symbol="SPY")
        assert [row.run_id for row in retained] == ["run-x"]
        assert retained[0].provenance == "realtime"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_retained_warmup_bars_keep_their_continuity_provenance(tmp_path: Path) -> None:
    """Resume must not launder a recovered bar into an ordinary one (ruling P4).

    Warmup after a crash replays the retained rows, so the provenance a
    reconnect wrote -- how the bar was produced, what authorized it, which
    continuity event explains it -- has to survive the rebuild. Dropping it
    would let a resumed run's evidence claim every warmup bar was ordinary.
    """
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        ledger.append(
            _bar(_T0).model_copy(
                update={
                    "provenance": "realtime_across_reconnect",
                    "authorization_id": "grant-1",
                    "continuity_event_ref": "run-x:7",
                }
            ),
            run_id="run-x",
        )
        feed = _RetainedSourceBarFeed(_FakeFeed([], mode="finite"), ledger, run_id="run-x")

        warmup = await feed.recent_closed_bars("SPY", use_rth=True)

        assert [bar.provenance for bar in warmup] == ["realtime_across_reconnect"]
        assert warmup[0].authorization_id == "grant-1"
        assert warmup[0].continuity_event_ref == "run-x:7"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_late_non_realtime_trigger_bar_is_refused_as_decision_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered decision bar delivered past its allowance is not a decision.

    Surviving a reconnect is worth nothing if the bot then acts on a trigger
    bar whose decision instant has already gone by: the trade would be priced
    against a market that has since moved. The refusal is recorded before it
    is raised, so the run's evidence explains the crash.
    """
    from app.services import bot_trade_strategy as module
    from app.services.feed_continuity_policy import FeedContinuityRefused

    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    # The bar's own close is the trigger, so it is a decision bar.
    policy, events = _recording_policy(trigger_ms=_T0 + 60_000)
    late = _bar(_T0).model_copy(update={"provenance": "realtime_across_reconnect"})
    monkeypatch.setattr(module, "now_ms_utc", lambda: _T0 + 60_000 + 20_001)
    try:
        feed = _RetainedSourceBarFeed(
            _FakeFeed([late], mode="finite"), ledger, run_id="run-x", continuity=policy
        )

        with pytest.raises(FeedContinuityRefused) as excinfo:
            async for _ in feed.stream_bars("SPY", use_rth=True):
                pass

        assert excinfo.value.reason == "DECISION_LATE"
        assert events[-1].kind == "refused" and events[-1].reason == "DECISION_LATE"
        assert ledger.bars(provider="fake", symbol="SPY") == []
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_a_refusal_the_sink_cannot_take_is_typed_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot layer's own refusal writes through the feed's typed wrapper.

    Spec §4.2 rule 9 is about the evidence, not about who writes it: a sink
    that cannot take this refusal must end the run as
    ``CONTINUITY_EVIDENCE_UNWRITABLE``, not leak the sink's own exception past
    the port on the way to the run outcome.
    """
    from app.services import bot_trade_strategy as module

    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    policy, _ = _recording_policy(trigger_ms=_T0 + 60_000)

    async def _unwritable(event: FeedContinuityEvent) -> ContinuityEventRef:
        raise OSError("journal unwritable")

    policy = ContinuityPolicy(
        decision_session=policy.decision_session,
        next_trigger_ms=policy.next_trigger_ms,
        substitution_grant=policy.substitution_grant,
        record_event=_unwritable,
    )
    late = _bar(_T0).model_copy(update={"provenance": "realtime_across_reconnect"})
    monkeypatch.setattr(module, "now_ms_utc", lambda: _T0 + 60_000 + 20_001)
    try:
        feed = _RetainedSourceBarFeed(
            _FakeFeed([late], mode="finite"), ledger, run_id="run-x", continuity=policy
        )

        with pytest.raises(MarketDataFeedError) as excinfo:
            async for _ in feed.stream_bars("SPY", use_rth=True):
                pass

        assert excinfo.value.reason == "CONTINUITY_EVIDENCE_UNWRITABLE"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_warmup_bars_fetched_from_the_source_are_journalled_to_this_run(
    tmp_path: Path,
) -> None:
    """A warmup row with no run is a row no receipt's evidence bound can reach.

    The ledger's journal is per-run, and the receipt bounds a run's evidence by
    ``evidence_seq``; a warmup bar appended without ``run_id`` lands in the
    journal anonymously, so the run that actually consumed it cannot claim it.
    """
    source = _FakeFeed([], mode="finite")
    source.recent_closed_bars = _serving_warmup([_bar(_T0)])  # type: ignore[method-assign]
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        feed = _RetainedSourceBarFeed(source, ledger, run_id="run-x")

        await feed.recent_closed_bars("SPY", use_rth=True)

        assert [row.run_id for row in ledger.bars(provider="fake", symbol="SPY")] == ["run-x"]
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_late_non_trigger_bar_is_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lateness only bites on a bar the consumer decides on.

    Every other recovered minute is warmup and consolidator input; refusing it
    for arriving late would turn a survivable reconnect back into a crash --
    the exact failure #1921 exists to close.
    """
    from app.services import bot_trade_strategy as module

    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    # The trigger is a full bucket away, so this bar's close (_T0 + 60_000) is
    # an ordinary minute rather than a decision.
    policy, events = _recording_policy(trigger_ms=_T0 + 15 * 60_000)
    late = _bar(_T0).model_copy(update={"provenance": "realtime_across_reconnect"})
    monkeypatch.setattr(module, "now_ms_utc", lambda: _T0 + 60_000 + 20_001)
    try:
        feed = _RetainedSourceBarFeed(
            _FakeFeed([late], mode="finite"), ledger, run_id="run-x", continuity=policy
        )

        async for _ in feed.stream_bars("SPY", use_rth=True):
            pass

        assert len(ledger.bars(provider="fake", symbol="SPY")) == 1
        assert events == []
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_execute_bot_run_trade_mode_requires_source_bar_ledger(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="source-bar ledger"):
        await execute_bot_run(
            _binding(run_id="run-1"),
            _PhaseFeed(),
            run_gate=None,
            instance_dir=tmp_path,
            source_bars=None,
        )
