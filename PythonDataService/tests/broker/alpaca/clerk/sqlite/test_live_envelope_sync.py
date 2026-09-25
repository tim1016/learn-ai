"""The fixed-cadence sync that observes the account and raises the loss hold (ADR 0059 D4).

One background tap produces both the envelope's cash observation and the
durable loss hold; ``accept_enter`` consumes them and never contacts the
broker. The whole decision is one ``tick``, so these tests drive it directly
rather than over a running loop: every stamp is the repository clock, pinned
at ``NOON``, and nothing here sleeps or reads a wall clock.

The seeded ledger (``day_pnl_repo``, ``seeded_open_buy``,
``seeded_external_order_today``) comes from ``conftest``: the day-P&L suite
judges the same one, and a second copy would be a second thing to keep true.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SYNC_INTERVAL_S,
    FILL_VISIBILITY_GRACE_MS,
    LIVE_ENVELOPE_CASH_EXCEEDED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrderLeg, BrokerPosition
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

SYNC_LOGGER = "app.broker.alpaca.clerk.sqlite.live_envelope_sync"


class _Read:
    """A read port whose account and positions the test sets per tick."""

    def __init__(
        self,
        *,
        cash: float = 100_000.0,
        last_equity: float | None = 100_000.0,
        unrealized: float = 0.0,
        fail: bool = False,
        fail_unexpectedly: bool = False,
    ) -> None:
        self.cash, self.last_equity, self.unrealized, self.fail = cash, last_equity, unrealized, fail
        # Not a ``BrokerError``: ``tick`` has no verdict for this, so it is
        # what reaches ``run``'s own guard.
        self.fail_unexpectedly = fail_unexpectedly

    async def get_account(self) -> BrokerAccountSnapshot:
        if self.fail_unexpectedly:
            raise RuntimeError("the read port raised something tick does not classify")
        if self.fail:
            raise BrokerUnavailable("account read timed out")
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id="9LIVE0001",
            account_mode="live",
            account_status="ACTIVE",
            currency="USD",
            cash=self.cash,
            equity=self.cash + self.unrealized,
            buying_power=self.cash,
            portfolio_value=self.cash,
            long_market_value=0.0,
            short_market_value=0.0,
            last_equity=self.last_equity,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=None,
            observed_at_ms=NOON,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        if self.unrealized == 0.0:
            return []
        return [
            BrokerPosition(
                broker="alpaca",
                symbol="SPY",
                asset_id=None,
                asset_class=None,
                quantity=1,
                side="long",
                average_entry_price=100.0,
                market_value=100.0 + self.unrealized,
                cost_basis=100.0,
                current_price=None,
                unrealized_pl=self.unrealized,
                unrealized_plpc=None,
                observed_at_ms=NOON,
            )
        ]


@pytest.fixture
async def make_sync() -> AsyncIterator[Callable[..., LiveEnvelopeSync]]:
    """Build syncs, and close every read-only projection connection they opened.

    ``LiveEnvelopeSync`` opens its own ``mode=ro`` connection in the
    constructor and closes it in ``stop``, exactly as the day-P&L suite's
    ``reader`` fixture does — so every sync a test builds is stopped here.
    """
    built: list[LiveEnvelopeSync] = []

    def build(
        repository: ClerkSqliteRepository,
        read: _Read | _LiveBroker,
        *,
        simulated: bool = True,
        **loop: Any,
    ) -> LiveEnvelopeSync:
        sync = LiveEnvelopeSync(
            repo=repository,
            read=read,
            envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=simulated),
            **loop,
        )
        built.append(sync)
        return sync

    yield build
    for sync in built:
        await sync.stop()


def _hold(repository: ClerkSqliteRepository) -> dict | None:
    return repository.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        strategy_instance_id=None,
    )


def _sync_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == SYNC_LOGGER]


async def test_a_tick_publishes_a_fresh_observation_stamped_by_the_repo_clock(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    sync = make_sync(day_pnl_repo, _Read())
    assert await sync.tick() == "observed"
    observation = sync.envelope.fresh_observation(NOON)
    assert observation is not None and observation.observed_at_ms == NOON
    assert observation.cash_available_usd == 100_000.0
    assert observation.last_equity_usd == 100_000.0


async def test_simulated_custody_subtracts_what_the_clerks_own_fills_would_have_spent(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_open_buy: None,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Under simulated custody the broker's cash never moved (plan R2)."""
    sync = make_sync(day_pnl_repo, _Read(), simulated=True)
    await sync.tick()
    shadow = sync.envelope.latest_observation()
    assert shadow is not None and shadow.cash_available_usd == pytest.approx(99_000.0)
    assert shadow.broker_cash_usd == pytest.approx(100_000.0)

    real = make_sync(day_pnl_repo, _Read(), simulated=False)
    await real.tick()
    observation = real.envelope.latest_observation()
    assert observation is not None
    assert observation.cash_available_usd == pytest.approx(100_000.0)


async def test_a_breach_raises_the_hold_once_and_the_sync_never_releases_it(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Only the guarded operator action clears the hold (plan R6, R12)."""
    read = _Read(unrealized=-5_000.0)
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "hold_raised"
    hold = _hold(day_pnl_repo)
    assert hold is not None
    cause = LossHoldCause.from_mapping(json.loads(hold["facts_json"])["cause_facts"])
    assert cause.day_pnl_usd == pytest.approx(-5_000.0)
    assert cause.loss_limit_usd == pytest.approx(5_000.0)
    assert cause.last_equity_usd == pytest.approx(100_000.0)
    assert cause.observed_at_ms == NOON

    revision = day_pnl_repo.control_meta_snapshot().control_revision
    read.unrealized = -6_000.0
    assert await sync.tick() == "hold_stands"
    read.unrealized = 0.0
    assert await sync.tick() == "hold_stands"
    assert day_pnl_repo.control_meta_snapshot().control_revision == revision


async def test_a_breached_reading_withdraws_exactly_like_an_unknown_one(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Ruling R-A′: publish only when the reading is judgeable AND not breached.

    Nothing may take new exposure while the account is over its loss limit,
    so a published observation buys nothing — and publishing one *before*
    ``raise_account_hold`` succeeds would leave the gate admitting ENTERs on
    the cash bound alone if that raise threw.
    """
    sync = make_sync(day_pnl_repo, _Read(unrealized=-5_000.0))
    assert await sync.tick() == "hold_raised"
    assert sync.envelope.fresh_observation(NOON) is None
    assert await sync.tick() == "hold_stands"
    assert sync.envelope.fresh_observation(NOON) is None


async def test_a_hold_that_stands_over_an_unjudgeable_account_says_so(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same verdict, different situation: the diagnosis rides on the line.

    A hold over a judgeable account clears on the operator's next guarded
    attempt; a hold over an unjudgeable one cannot be attempted at all,
    because the clear re-observes and refuses UNOBSERVED.
    """
    read = _Read(unrealized=-5_000.0)
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "hold_raised"

    read.last_equity = None
    caplog.clear()  # the raise above is already captured; this asserts the next line
    with caplog.at_level(logging.INFO, logger=SYNC_LOGGER):
        assert await sync.tick() == "hold_stands"

    (record,) = _sync_records(caplog)
    assert record.action == "live_envelope_hold_stands"
    assert record.why == "the account is also unjudgeable"
    assert record.last_equity_known is False


async def test_an_unknown_fact_raises_nothing(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_external_order_today: None,
    make_sync: Callable[..., LiveEnvelopeSync],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An external order observed today leaves the day's P&L unknowable (plan R5)."""
    sync = make_sync(day_pnl_repo, _Read(unrealized=-50_000.0))
    with caplog.at_level(logging.WARNING, logger=SYNC_LOGGER):
        assert await sync.tick() == "unknown"
    assert _hold(day_pnl_repo) is None
    assert sync.envelope.fresh_observation(NOON) is None

    (record,) = _sync_records(caplog)
    assert record.action == "live_envelope_unknown"
    assert record.last_equity_known is True
    assert record.external_orders_today == 1
    assert record.execution_coverage == "incomplete"
    assert record.fee_fidelity == "reported"


async def test_a_missing_last_equity_is_unknown(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """No ``last_equity`` is no loss limit, so the account cannot be judged (plan R3)."""
    sync = make_sync(day_pnl_repo, _Read(last_equity=None, unrealized=-50_000.0))
    assert await sync.tick() == "unknown"
    assert _hold(day_pnl_repo) is None
    assert sync.envelope.fresh_observation(NOON) is None


@pytest.mark.parametrize(
    ("knobs", "fields"),
    [
        ({"last_equity": float("nan")}, ["last_equity"]),
        ({"unrealized": float("inf")}, ["unrealized_pl"]),
        ({"cash": float("nan")}, ["cash"]),
    ],
)
async def test_a_non_finite_risk_figure_withdraws_the_observation(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
    caplog: pytest.LogCaptureFixture,
    knobs: dict[str, float],
    fields: list[str],
) -> None:
    """Alpaca can answer ``"NaN"``, and ``opt_float`` is a bare ``float(value)``.

    A NaN anywhere in the loss inputs makes ``loss_breached`` evaluate False —
    indistinguishable from "nothing breached" — so the account would keep
    admitting ENTERs on the cash bound while the loss rule cannot be judged at
    all. The reading is unjudgeable instead: no observation, no hold, and the
    fault named on its own line.
    """
    sync = make_sync(day_pnl_repo, _Read(**knobs))
    with caplog.at_level(logging.WARNING, logger=SYNC_LOGGER):
        assert await sync.tick() == "unknown"

    assert sync.envelope.fresh_observation(NOON) is None
    assert sync.envelope.latest_observation() is None
    assert _hold(day_pnl_repo) is None

    (record,) = [
        record for record in _sync_records(caplog) if record.action == "live_envelope_read_non_finite"
    ]
    assert record.levelno == logging.WARNING
    assert record.fields == fields
    assert record.account_id == day_pnl_repo.account_id


async def test_an_unknown_tick_withdraws_the_previous_observation_at_once(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """An unjudgeable account refuses every ENTER now, not in 45 seconds.

    A read that *succeeded* but cannot be judged is not a stale observation —
    it is a current one the envelope must not bound an ENTER against, so it is
    withdrawn rather than left to age out.
    """
    read = _Read()
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "observed"
    assert sync.envelope.fresh_observation(NOON) is not None

    read.last_equity = None
    assert await sync.tick() == "unknown"
    assert sync.envelope.fresh_observation(NOON) is None
    assert sync.envelope.latest_observation() is None


async def test_a_failed_read_keeps_the_loop_alive_and_lets_the_observation_age_out(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """A broker outage is not a verdict on the account: the last read stands until it is stale."""
    read = _Read()
    sync = make_sync(day_pnl_repo, read)
    await sync.tick()
    read.fail = True
    assert await sync.tick() == "read_failed"
    assert sync.envelope.fresh_observation(NOON) is not None
    assert sync.envelope.fresh_observation(NOON + OBSERVATION_MAX_AGE_MS + 1) is None


async def test_only_a_change_of_verdict_is_logged(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 15 s cadence must never spam: an unchanged verdict says nothing."""
    read = _Read(fail=True)
    sync = make_sync(day_pnl_repo, read)
    with caplog.at_level(logging.INFO, logger=SYNC_LOGGER):
        assert await sync.tick() == "read_failed"
        assert await sync.tick() == "read_failed"
        read.fail = False
        assert await sync.tick() == "observed"

    assert [(record.levelno, record.action) for record in _sync_records(caplog)] == [
        (logging.WARNING, "live_envelope_read_failed"),
        (logging.INFO, "live_envelope_observed"),
    ]


async def test_the_loop_survives_a_failing_tick(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unattended dead sync is how the envelope goes blind to a losing day.

    The fault is deliberately *not* a ``BrokerError``: ``tick`` classifies
    those into ``read_failed`` and never reaches ``run``'s guard, so a test
    injecting one would leave the guard unexercised.
    """
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    sync = make_sync(
        day_pnl_repo,
        _Read(fail_unexpectedly=True),
        interval_s=15.0,
        sleep=sleep,
        max_ticks=3,
    )
    with caplog.at_level(logging.ERROR, logger=SYNC_LOGGER):
        await sync.run()

    assert slept == [15.0, 15.0, 15.0]
    records = _sync_records(caplog)
    assert len(records) == 3
    assert {record.action for record in records} == {"live_envelope_sync_failed"}
    assert records[0].exc_info is not None


async def test_a_stopped_sync_refuses_to_start_again(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """``stop()`` closes the projection reader, and nothing reopens it."""
    sync = make_sync(day_pnl_repo, _Read())
    await sync.stop()

    with pytest.raises(RuntimeError, match="terminal after stop"):
        sync.start()


# ── A fill the Clerk records while the broker is being read (#2441) ───────────
# An observation's cash is trusted to include every fill recorded before its
# stamp, so the stamp is the instant the reads were *issued*. Stamped when
# they returned, a fill recorded during the round trip -- which the broker's
# answer may well predate -- was released from its reservation, and a second
# instance was admitted against cash the first had already spent.

# Each half of a synthetic broker round trip, on the repo clock. Longer than
# the fill-visibility grace, so the grace alone cannot hide a stamp taken on
# return: a slow read is exactly when the stamp matters.
READ_LEG_MS = FILL_VISIBILITY_GRACE_MS + 1_000


class _FillLandsMidRead(_LiveBroker):
    """The live account, answering the snapshot it took before a fill the Clerk records meanwhile.

    ``during`` names the read whose round trip the fill lands inside. The
    broker's answer is built first -- the pre-fill cash -- and only then does
    the clock move and the Clerk record the fill, so the response returns
    after the fill carrying a figure that does not include it. The fill lands
    once, on the first round trip of that read.
    """

    def __init__(
        self,
        *,
        clock: _TestClock,
        cash: float,
        during: Literal["account", "positions"],
        record_fill: Callable[[], None],
    ) -> None:
        super().__init__(now_ms=clock(), cash=cash)
        self._clock = clock
        self._during: str | None = during
        self._record_fill = record_fill

    def _in_flight(self, read: str) -> None:
        if read != self._during:
            return
        self._during = None
        self._clock.advance(READ_LEG_MS)
        self._record_fill()
        self._clock.advance(READ_LEG_MS)

    async def get_account(self) -> BrokerAccountSnapshot:
        snapshot = await super().get_account()
        self._in_flight("account")
        return snapshot

    async def list_positions(self) -> list[BrokerPosition]:
        positions = await super().list_positions()
        self._in_flight("positions")
        return positions


def _observed_gate(*, cash: float, simulated: bool) -> LiveEnvelopeGate:
    """A gate holding one observation fresh at ``T0``: what the first ENTER is admitted against."""
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=simulated)
    gate.publish(
        AccountObservation(
            observed_at_ms=T0,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            last_equity_usd=cash,
            unrealized_pl_usd=0.0,
            position_count=0,
        )
    )
    return gate


def _enter(
    repo: ClerkSqliteRepository,
    instance: tuple[str, str],
    *,
    symbol: str,
    envelope: LiveEnvelopeGate,
) -> EnterSubmission:
    """A $1,000 market ENTER: 10 shares against a $100 decision-bar close."""
    sid, run_id = instance
    return accept_enter(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=sid,
        decision_id=f"{sid}-entry",
        lifecycle_run_id=run_id,
        leg=BrokerOrderLeg(symbol=symbol, side="buy", quantity=10),
        envelope=envelope,
        reference_price=100.0,
    )


def _fill_all_ten(
    repo: ClerkSqliteRepository, clock: _TestClock, accepted: EnterSubmission
) -> Callable[[], None]:
    return lambda: _append_slice(
        repo, accepted, execution_id="exec-mid-read", quantity=10, source_event_at_ms=clock()
    )


@pytest.mark.parametrize(
    "during",
    [
        pytest.param("positions", id="codex-fill-while-positions-read"),
        pytest.param("account", id="fill-inside-the-single-account-read"),
    ],
)
async def test_a_fill_recorded_while_the_broker_is_read_stays_reserved(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
    make_sync: Callable[..., LiveEnvelopeSync],
    during: Literal["account", "positions"],
) -> None:
    """Two instances share $1,000, and the first's $1,000 fill lands mid-read.

    The broker answers $1,000 -- its snapshot predates the fill -- so only the
    first ENTER's reservation stands between the second instance and cash
    already spent. Codex (#2415 finding A1) reproduced the admission with the
    fill inside the parallel positions read; the single-read variant shows the
    parallel read is not the cause. One account round trip is enough, because
    the fault was the stamp.
    """
    first_instance, second_instance = two_active_instances
    first = _enter(
        envelope_repo,
        first_instance,
        symbol="SPY",
        envelope=_observed_gate(cash=1_000.0, simulated=False),
    )
    read = _FillLandsMidRead(
        clock=envelope_clock,
        cash=1_000.0,
        during=during,
        record_fill=_fill_all_ten(envelope_repo, envelope_clock, first),
    )
    sync = make_sync(envelope_repo, read, simulated=False)

    reading = await sync.observe()

    assert envelope_repo.position(first_instance[0], "SPY") == 10.0
    with pytest.raises(AdmissionBlockedError) as refused:
        _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope)
    assert refused.value.decision.reason_code == LIVE_ENVELOPE_CASH_EXCEEDED
    # The observation is dated when its reads were issued, not when they returned.
    assert reading.observation.observed_at_ms == T0


async def test_a_fill_recorded_just_before_the_read_is_issued_stays_reserved(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """The broker's cash may lag a fill whose trade update it already delivered.

    Alpaca promises no ordering between the two, so a fill the Clerk recorded
    ``FILL_VISIBILITY_GRACE_MS`` before the read was issued -- the boundary,
    inclusive -- is not trusted to be in the answer. Here the broker still
    reports the pre-fill $1,000, and the second instance is refused rather
    than admitted against it. The exact boundary is pinned per order state in
    ``test_envelope_reservations``.
    """
    first_instance, second_instance = two_active_instances
    first = _enter(
        envelope_repo,
        first_instance,
        symbol="SPY",
        envelope=_observed_gate(cash=1_000.0, simulated=False),
    )
    _fill_all_ten(envelope_repo, envelope_clock, first)()
    envelope_clock.advance(FILL_VISIBILITY_GRACE_MS)
    sync = make_sync(
        envelope_repo, _LiveBroker(now_ms=envelope_clock(), cash=1_000.0), simulated=False
    )

    reading = await sync.observe()

    assert reading.observation.observed_at_ms == T0 + FILL_VISIBILITY_GRACE_MS
    with pytest.raises(AdmissionBlockedError) as refused:
        _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope)
    assert refused.value.decision.reason_code == LIVE_ENVELOPE_CASH_EXCEEDED


async def test_under_shadow_a_mid_read_fill_counts_twice_until_the_next_observation(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Shadow errs toward refusing, and recovers on the next observation.

    Under simulated custody the broker's cash never moves, so the envelope
    subtracts what the Clerk's own fills spent (plan R2) -- read *after* the
    broker answered. A fill recorded mid-read is therefore in
    ``cash_available`` and still reserved: counted twice, never zero times.
    The account's true free cash is $1,000 and the envelope offers none, so
    the second $1,000 ENTER is refused. The next observation, issued past the
    fill-visibility grace, counts the fill once and admits it.
    """
    first_instance, second_instance = two_active_instances
    first = _enter(
        envelope_repo,
        first_instance,
        symbol="SPY",
        envelope=_observed_gate(cash=2_000.0, simulated=True),
    )
    read = _FillLandsMidRead(
        clock=envelope_clock,
        cash=2_000.0,
        during="account",
        record_fill=_fill_all_ten(envelope_repo, envelope_clock, first),
    )
    sync = make_sync(envelope_repo, read, simulated=True)

    mid_read = (await sync.observe()).observation
    assert mid_read.cash_available_usd == pytest.approx(1_000.0)
    assert envelope_repo.reserved_cash_usd(observed_at_ms=mid_read.observed_at_ms) == pytest.approx(
        1_000.0
    )
    with pytest.raises(AdmissionBlockedError) as refused:
        _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope)
    assert refused.value.decision.reason_code == LIVE_ENVELOPE_CASH_EXCEEDED

    envelope_clock.advance(int(ENVELOPE_SYNC_INTERVAL_S * 1_000))
    next_tick = (await sync.observe()).observation
    assert next_tick.cash_available_usd == pytest.approx(1_000.0)
    assert envelope_repo.reserved_cash_usd(observed_at_ms=next_tick.observed_at_ms) == 0.0
    assert _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope).created
