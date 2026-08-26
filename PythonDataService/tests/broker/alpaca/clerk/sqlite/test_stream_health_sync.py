"""Stream-health hold lifecycle over virtual time (#1777 WP4).

Finding S10: the hold was raised and released only inside ENTER-purpose
effect execution. A single bad sample froze entries account-wide, an
unchanged outage appended a refresh per attempt, and on a quiet fleet a
stale hold never released at all -- nothing ran to release it.

The lifecycle now runs on its own fixed cadence. These tests drive that
sync tick-by-tick with scripted providers and an injected clock, so every
timing promise is asserted without sleeping and without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import STREAM_HEALTH_REASON_CODE
from app.broker.alpaca.clerk.sqlite.stream_health_sync import StreamHealthHoldSync
from app.broker.alpaca.clerk.stream_health import StreamHealthGate

INTERVAL_S = 15.0
TICK_MS = 15_000


@dataclass
class _Providers:
    """Scripted channel health.

    ``connected`` is tracked separately from ``healthy`` because the real
    :class:`ChannelHealth` distinguishes them and the account hold turns on
    exactly that distinction: an aggregate feed reports ``healthy=False``
    while still connected when any one symbol is warming.

    ``healthy``/``connected``/``reason`` script *both* channels; the
    ``market_data_*`` fields override that one channel. The override is not
    a convenience -- the two channels are not interchangeable at account
    scope. Market data is judged on connectivity, because per-symbol warm-up
    must not freeze the account; execution is judged on health, because an
    unusable evidence frame is account-wide. A fake that could only say one
    thing about both could not express either case, and the warm-up test
    written against it passed for the wrong reason: it was scripting a
    broken *execution* channel too, which must raise.

    ``observed_at_ms`` freezes unless ``observing``, which models the real
    unhealthy branches -- a disconnected execution channel reports the
    instant it *broke* (``connection_changed_at_ms``), not the instant it
    was asked.
    """

    now_ms: int = 1_000_000
    healthy: bool = True
    connected: bool | None = None
    reason: str = "execution channel is down"
    observing: bool = True
    market_data_healthy: bool | None = None
    market_data_connected: bool | None = None
    _observed_at_ms: int = 1_000_000

    def advance(self, delta_ms: int = TICK_MS) -> None:
        self.now_ms += delta_ms
        if self.observing:
            self._observed_at_ms = self.now_ms

    def _channel(self, stream: str) -> ChannelHealth:
        healthy, connected = self.healthy, self.connected
        if stream == "market_data":
            if self.market_data_healthy is not None:
                healthy = self.market_data_healthy
            if self.market_data_connected is not None:
                connected = self.market_data_connected
        return ChannelHealth(
            stream=stream,
            healthy=healthy,
            connected=healthy if connected is None else connected,
            reason="" if healthy else self.reason,
            observed_at_ms=self._observed_at_ms,
        )

    def gate(self) -> StreamHealthGate:
        return StreamHealthGate(
            market_data=lambda: self._channel("market_data"),
            execution=lambda: self._channel("execution"),
        )


def _sync(repo: ClerkSqliteRepository, providers: _Providers) -> StreamHealthHoldSync:
    return StreamHealthHoldSync(
        repo=repo,
        gate=providers.gate(),
        interval_s=INTERVAL_S,
    )


def _repo(tmp_path: Path) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.initialize(account_id="PA-WP4", artifacts_root=tmp_path)


def _hold(repo: ClerkSqliteRepository):
    return repo.active_hold(scope="ACCOUNT_CLERK", reason_code=STREAM_HEALTH_REASON_CODE)


def _appends(repo: ClerkSqliteRepository, kind: str) -> int:
    return sum(1 for row in repo.custody_transitions() if row["transition_kind"] == kind)


def test_a_single_bad_sample_never_raises_the_hold(tmp_path: Path) -> None:
    """S10's headline: one blip must not freeze entries account-wide."""
    repo, providers = _repo(tmp_path), _Providers()
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    providers.healthy = False
    sync.tick()
    providers.advance()
    providers.healthy = True
    sync.tick()

    assert _hold(repo) is None
    assert _appends(repo, "ACCOUNT_HOLD_RAISED") == 0
    repo.close()


def test_a_sustained_outage_raises_within_two_ticks(tmp_path: Path) -> None:
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    sync.tick()
    assert _hold(repo) is None, "one unhealthy sample is not yet proof"

    providers.advance()
    sync.tick()

    assert _hold(repo) is not None
    assert _appends(repo, "ACCOUNT_HOLD_RAISED") == 1
    repo.close()


def test_an_unchanged_outage_appends_nothing_further(tmp_path: Path) -> None:
    """The revision churn (S16): a persisting outage used to append a
    refresh per attempt, because the evidence identity carried the
    observation timestamp and so never compared equal."""
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    for _ in range(6):
        sync.tick()
        providers.advance()

    assert _hold(repo) is not None
    assert _appends(repo, "ACCOUNT_HOLD_RAISED") == 1
    assert _appends(repo, "ACCOUNT_HOLD_REFRESHED") == 0
    repo.close()


def test_a_changed_reason_is_still_recorded(tmp_path: Path) -> None:
    """Append-on-change-only must not become append-never: a genuinely
    different failure is new evidence and belongs in the ledger."""
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    sync.tick()
    providers.advance()
    providers.reason = "market_data feed disconnected"
    sync.tick()

    assert _appends(repo, "ACCOUNT_HOLD_REFRESHED") == 1
    repo.close()


def test_recovery_releases_within_one_tick(tmp_path: Path) -> None:
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    sync.tick()
    assert _hold(repo) is not None

    providers.advance()
    providers.healthy = True
    sync.tick()

    assert _hold(repo) is None
    assert _appends(repo, "ACCOUNT_HOLD_RESOLVED") == 1
    repo.close()


def test_a_released_hold_stays_released_without_further_appends(tmp_path: Path) -> None:
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    sync.tick()
    providers.healthy = True
    for _ in range(4):
        providers.advance()
        sync.tick()

    assert _appends(repo, "ACCOUNT_HOLD_RESOLVED") == 1
    repo.close()


def test_the_hold_survives_restart_and_releases_on_one_fresh_sample(
    tmp_path: Path,
) -> None:
    """#1777: the hold is fold-derived so it survives; the debounce counter
    is process-local so it resets. Release must still cost exactly one
    fresh healthy observation."""
    repo, providers = _repo(tmp_path), _Providers(healthy=False)
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    sync.tick()
    assert _hold(repo) is not None

    restarted = _sync(repo, providers)
    providers.advance()
    providers.healthy = True
    restarted.tick()

    assert _hold(repo) is None
    repo.close()


def _count_resolves(repo: ClerkSqliteRepository) -> list[int]:
    """Spy on the ledger call itself, not just its appends.

    ``resolve_account_hold_if_active`` is a no-op when no hold stands, so
    transition counts cannot tell us whether we took the clerk's write lock
    to discover that. On a fleet whose original failure was lock
    contention, "did we touch the ledger at all" is the question worth
    asserting.
    """
    calls: list[int] = []
    original = repo.resolve_account_hold_if_active

    def counted(**kwargs):
        calls.append(1)
        return original(**kwargs)

    repo.resolve_account_hold_if_active = counted  # type: ignore[method-assign]
    return calls


def test_a_steady_healthy_account_never_touches_the_ledger(tmp_path: Path) -> None:
    repo, providers = _repo(tmp_path), _Providers()
    sync = _sync(repo, providers)

    sync.tick()  # first sample proves the release; after that, nothing to do
    calls = _count_resolves(repo)
    for _ in range(10):
        providers.advance()
        sync.tick()

    assert calls == [], "a healthy account took the write lock on every tick"
    repo.close()


def test_the_first_sample_of_a_fresh_process_still_proves_the_release(
    tmp_path: Path,
) -> None:
    """The counterpart to the test above: a restart cannot assume clear.

    A hold may already stand in the journal, so an unknown belief must
    still reach the ledger even when the very first sample is healthy.
    """
    repo, providers = _repo(tmp_path), _Providers()
    calls = _count_resolves(repo)

    _sync(repo, providers).tick()

    assert calls == [1]
    repo.close()


async def test_the_loop_survives_a_failing_observation(tmp_path: Path) -> None:
    """An unattended dead sync is how a hold becomes permanent."""
    repo = _repo(tmp_path)
    ticks = {"count": 0}

    def exploding() -> ChannelHealth:
        ticks["count"] += 1
        raise RuntimeError("provider blew up")

    async def no_wait(_seconds: float) -> None:
        return None

    sync = StreamHealthHoldSync(
        repo=repo,
        gate=StreamHealthGate(market_data=exploding, execution=exploding),
        interval_s=INTERVAL_S,
        sleep=no_wait,
        max_ticks=3,
    )

    await sync.run()

    assert ticks["count"] == 3, "the loop stopped at the first failure"
    repo.close()


async def test_the_loop_waits_its_own_cadence_between_ticks(tmp_path: Path) -> None:
    """Independence from the reconcile pass is the point: this loop's wait
    is its own fixed interval, never the reconciler's backoff."""
    repo, providers = _repo(tmp_path), _Providers()
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    sync = StreamHealthHoldSync(
        repo=repo,
        gate=providers.gate(),
        interval_s=INTERVAL_S,
        sleep=record,
        max_ticks=3,
    )

    await sync.run()

    assert waits == [INTERVAL_S, INTERVAL_S, INTERVAL_S]
    repo.close()


async def test_failing_ticks_never_slow_the_cadence(tmp_path: Path) -> None:
    """The independence claim, stated as behaviour (#1777 WP4 decision 1).

    The reconciliation loop backs off to 300 s on repeated failure. This
    loop must not: a channel outage is exactly when repeated failures and
    the need for a prompt hold coincide, so backing off would delay the
    raise precisely when it matters. There is no backoff here, and this
    pins that a future "be polite on errors" change cannot add one.
    """
    repo = _repo(tmp_path)
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    def exploding() -> ChannelHealth:
        raise RuntimeError("provider blew up")

    sync = StreamHealthHoldSync(
        repo=repo,
        gate=StreamHealthGate(market_data=exploding, execution=exploding),
        interval_s=INTERVAL_S,
        sleep=record,
        max_ticks=4,
    )

    await sync.run()

    assert waits == [INTERVAL_S] * 4, f"the loop backed off on failure: {waits}"
    repo.close()


def test_the_sync_cannot_observe_the_reconciler_at_all(tmp_path: Path) -> None:
    """Structural companion to the test above.

    Independence is easiest to preserve if the sync simply has no way to
    consult the reconcile loop. It takes a repository and a health gate --
    nothing that carries backoff state.
    """
    import inspect

    parameters = set(inspect.signature(StreamHealthHoldSync.__init__).parameters)

    assert parameters == {
        "self",
        "repo",
        "gate",
        "interval_s",
        "sleep",
        "max_ticks",
    }, f"the hold sync grew a dependency; check it is not the reconciler: {parameters}"


def test_a_warming_symbol_never_raises_the_account_hold(tmp_path: Path) -> None:
    """Codex review, #1784: the account hold must not fold per-symbol usability.

    ``IbkrMarketDataFeed.health(None)`` aggregates every subscribed symbol
    and reports ``stale=True`` when any one of them is still warming, so
    the unscoped fact is *not* the connectivity fact the account scope
    needs. Sampling ``healthy`` here recreated finding S6 at account
    scope: one warming symbol freezing entries for every other bot.

    The symbol-scoped refusal at ENTER is what owns per-symbol usability.
    """
    repo = _repo(tmp_path)
    providers = _Providers(
        market_data_healthy=False,
        market_data_connected=True,
        reason="SPY is warming",
    )
    sync = _sync(repo, providers)

    for _ in range(4):
        sync.tick()
        providers.advance()

    assert _hold(repo) is None
    assert _appends(repo, "ACCOUNT_HOLD_RAISED") == 0
    repo.close()


def test_an_unusable_execution_frame_does_raise_the_account_hold(
    tmp_path: Path,
) -> None:
    """Execution gets no warm-up forgiveness, because it has no warm-up.

    `execution_channel_health` reports `connected=True, healthy=False` when
    the trade_updates socket delivers a frame it cannot use. That is broken
    for every symbol on the account, and every submission gate rejects it --
    so the durable account-wide hold, the strictest mechanism of the three,
    must not be the one that calls it fine. Judging the whole snapshot on
    connectivity did exactly that.
    """
    repo = _repo(tmp_path)
    providers = _Providers(
        healthy=False,
        connected=True,
        reason="Alpaca trade_updates received an unusable evidence frame.",
        market_data_healthy=True,
    )
    sync = _sync(repo, providers)

    for _ in range(2):
        sync.tick()
        providers.advance()

    hold = _hold(repo)
    assert hold is not None
    assert "execution" in hold["evidence_refs_json"]
    assert "market_data" not in hold["evidence_refs_json"]
    repo.close()


def test_a_disconnect_raises_even_once_its_break_timestamp_is_old(
    tmp_path: Path,
) -> None:
    """Codex review, #1784: an outage must not age out of being actionable.

    A disconnected execution channel reports ``connection_changed_at_ms``
    -- when it *broke*, frozen thereafter. Judging sample freshness on
    that timestamp meant an outage older than the window sampled
    ``unknown`` and could never raise the hold: a fail-open, and the exact
    inverse of the stale-hold bug this work exists to fix.
    """
    repo = _repo(tmp_path)
    providers = _Providers(healthy=False, connected=False, observing=False)
    # The socket broke well before this process last looked at it.
    providers.now_ms += 10 * TICK_MS
    sync = _sync(repo, providers)

    sync.tick()
    providers.advance()
    sync.tick()

    assert _hold(repo) is not None
    assert _appends(repo, "ACCOUNT_HOLD_RAISED") == 1
    repo.close()


def test_evidence_names_the_disconnected_channel_not_the_warming_one(
    tmp_path: Path,
) -> None:
    """Decision and evidence must come from one predicate.

    If the raise turns on connectivity but the record lists every
    ``healthy=False`` channel, the operator is handed a warming symbol as
    the reason their whole account is frozen.
    """
    repo = _repo(tmp_path)
    clock = [1_000_000]

    def _channel(stream: str, *, connected: bool, reason: str) -> ChannelHealth:
        return ChannelHealth(
            stream=stream,
            healthy=False,
            connected=connected,
            reason=reason,
            observed_at_ms=clock[0],
        )

    gate = StreamHealthGate(
        market_data=lambda: _channel(
            "market_data", connected=True, reason="SPY is warming"
        ),
        execution=lambda: _channel(
            "execution", connected=False, reason="socket is down"
        ),
    )
    sync = StreamHealthHoldSync(
        repo=repo, gate=gate, interval_s=INTERVAL_S
    )

    sync.tick()
    clock[0] += TICK_MS
    sync.tick()

    hold = _hold(repo)
    assert hold is not None
    assert "socket is down" in str(hold)
    assert "SPY is warming" not in str(hold)
    repo.close()
