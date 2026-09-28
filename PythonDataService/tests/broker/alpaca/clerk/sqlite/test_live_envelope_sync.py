"""The fixed-cadence sync that observes the account and raises the loss hold (ADR 0059 D4).

One background tap produces both the envelope's cash observation and the
durable loss hold; ``accept_enter`` consumes them and never contacts the
broker. The whole decision is one ``tick``, so these tests drive it directly
rather than over a running loop: every stamp is the repository clock, pinned
at ``NOON``, and nothing here sleeps or reads a wall clock.

The seeded ledger fixtures come from ``conftest``. They still drive simulated
cash and external-order coverage, while the account-day-P&L basis itself is
broker equity rather than Clerk FIFO.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    FILL_VISIBILITY_GRACE_MS,
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerEvidenceUnavailable, BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerOrderLeg,
    BrokerPosition,
)
from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    TODAY_OPEN,
    _TestClock,
    complete_fee_evidence,
)
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

SYNC_LOGGER = "app.broker.alpaca.clerk.sqlite.live_envelope_sync"


class _Read:
    """A read port whose account and positions the test sets per tick."""

    def __init__(
        self,
        *,
        cash: float = 100_000.0,
        equity: float | None = None,
        last_equity: float | None = 100_000.0,
        unrealized: float = 0.0,
        cash_flows: list[BrokerActivity] | None = None,
        cash_flow_reads: list[list[BrokerActivity]] | None = None,
        account_observed_at_ms: int = NOON,
        activity_error: BrokerEvidenceUnavailable | None = None,
        fail: bool = False,
        fail_positions: bool = False,
        fail_unexpectedly: bool = False,
    ) -> None:
        self.cash = cash
        self.equity = equity
        self.last_equity = last_equity
        self.unrealized = unrealized
        self.cash_flows = [] if cash_flows is None else cash_flows
        self.cash_flow_reads = cash_flow_reads
        self.account_observed_at_ms = account_observed_at_ms
        self.activity_error = activity_error
        self.activity_calls: list[tuple[int | None, int, str | None]] = []
        self.fail = fail
        self.fail_positions = fail_positions
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
            equity=self.cash + self.unrealized if self.equity is None else self.equity,
            buying_power=self.cash,
            portfolio_value=self.cash,
            long_market_value=0.0,
            short_market_value=0.0,
            last_equity=self.last_equity,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=None,
            observed_at_ms=self.account_observed_at_ms,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        if self.fail_positions:
            raise BrokerUnavailable("positions read timed out")
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

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]:
        self.activity_calls.append((after_ms, limit, activity_type))
        if self.activity_error is not None:
            raise self.activity_error
        if self.cash_flow_reads is not None:
            read_index = min(len(self.activity_calls) - 1, len(self.cash_flow_reads) - 1)
            return self.cash_flow_reads[read_index]
        return self.cash_flows


def _cash_flow(activity_type: str, net_amount: float | None) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=f"{activity_type}-{net_amount}",
        activity_type=activity_type,
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=net_amount,
        occurred_at_ms=TODAY_OPEN,
        observed_at_ms=NOON,
    )


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
        simulated: bool = False,
        **loop: Any,
    ) -> LiveEnvelopeSync:
        complete_fee_evidence(repository)
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


async def test_budget_authority_mode_disagreement_remains_visible_through_failed_read(tmp_path, make_sync) -> None:
    from app.broker.contract.errors import BrokerAccountModeDisagreement
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _new_budget_repo

    class Read(_Read):
        disagree = True

        async def get_account(self) -> BrokerAccountSnapshot:
            if self.disagree:
                raise BrokerAccountModeDisagreement("Account changed", broker="alpaca")
            return await super().get_account()

    repo = _new_budget_repo(tmp_path)
    broker = Read()
    sync = make_sync(repo, broker)
    try:
        assert await sync.tick() == "mode_disagreed"
        assert sync.account_mode_disagreed and sync.risk_snapshot().observation is None
        broker.disagree, broker.fail = False, True
        assert await sync.tick() == "read_failed"
        assert sync.account_mode_disagreed
        broker.fail = False
        assert await sync.tick() == "observed"
        assert not sync.account_mode_disagreed
    finally:
        await sync.stop()
        repo.close()


@pytest.mark.parametrize("via_tick", [False, True])
async def test_risk_apply_cannot_republish_evidence_after_mode_disagreement(tmp_path, make_sync, via_tick: bool) -> None:
    from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
    from app.broker.contract.errors import BrokerAccountModeDisagreement
    from tests.broker.alpaca.clerk.sqlite.test_account_risk_policy import _policy
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _new_budget_repo

    class Read(_Read):
        disagree = False

        async def get_account(self) -> BrokerAccountSnapshot:
            if self.disagree:
                raise BrokerAccountModeDisagreement("wrong account", broker="alpaca")
            return await super().get_account()

    repo = _new_budget_repo(tmp_path)
    broker = Read()
    sync = make_sync(repo, broker)
    try:
        assert await sync.tick() == "observed"
        broker.disagree = True
        if via_tick:
            assert await sync.tick() == "mode_disagreed"
        else:
            with pytest.raises(BrokerAccountModeDisagreement):
                await sync.observe()
        sync.apply_risk_policy(_policy(2, 200), expected_revision=1)
        assert sync.account_mode_disagreed
        assert not current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock()).allowed
        broker.disagree = False
        await sync.observe()
        assert not sync.account_mode_disagreed
        assert current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock()).allowed
    finally:
        await sync.stop()
        repo.close()


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


async def test_a_tick_crossing_et_midnight_withdraws_the_observation(
    day_pnl_repo: ClerkSqliteRepository,
    day_pnl_clock: _TestClock,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    just_before_midnight_et = NOON + 12 * 60 * 60 * 1_000 - 1
    just_after_midnight_et = just_before_midnight_et + 2
    sync = make_sync(
        day_pnl_repo,
        _Read(account_observed_at_ms=just_after_midnight_et),
    )
    day_pnl_clock.value = just_before_midnight_et

    assert await sync.tick() == "unknown"
    assert sync.envelope.latest_observation() is None




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


async def test_daily_loss_is_change_since_prior_close_not_lifetime_open_pnl(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Older open gains cannot hide that the account lost 5,000 USD today."""
    sync = make_sync(
        day_pnl_repo,
        _Read(
            cash=90_000.0,
            equity=105_000.0,
            last_equity=110_000.0,
            unrealized=5_000.0,
        ),
    )

    assert await sync.tick() == "hold_raised"
    hold = _hold(day_pnl_repo)
    assert hold is not None
    cause = LossHoldCause.from_mapping(json.loads(hold["facts_json"])["cause_facts"])
    assert cause.day_pnl_usd == pytest.approx(-5_000.0)


async def test_an_earlier_open_loss_does_not_keep_the_hold_engaged_today(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """A position can remain down since entry while the account recovers today."""
    sync = make_sync(
        day_pnl_repo,
        _Read(
            cash=115_000.0,
            equity=105_000.0,
            last_equity=100_000.0,
            unrealized=-10_000.0,
        ),
    )

    assert await sync.tick() == "observed"
    assert _hold(day_pnl_repo) is None
    observation = sync.envelope.latest_observation()
    assert observation is not None and observation.equity_usd == pytest.approx(105_000.0)


@pytest.mark.parametrize(
    ("equity", "activity_type", "net_amount"),
    [
        pytest.param(110_000.0, "CSD", 10_000.0, id="deposit"),
        pytest.param(90_000.0, "CSW", -10_000.0, id="withdrawal"),
    ],
)
async def test_same_day_cash_transfers_are_not_counted_as_pnl(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
    equity: float,
    activity_type: str,
    net_amount: float,
) -> None:
    read = _Read(
        cash=equity,
        equity=equity,
        last_equity=100_000.0,
        cash_flows=[_cash_flow(activity_type, net_amount)],
    )
    sync = make_sync(day_pnl_repo, read)

    reading = await sync.observe()

    assert reading.day_pnl is not None and reading.day_pnl.known
    assert reading.day_pnl.total_usd == pytest.approx(0.0)
    prior_close_ms = previous_completed_session_close_ms(NOON)
    assert reading.day_pnl.day_start_ms == prior_close_ms
    assert read.activity_calls == [(prior_close_ms, 100, "TRANS")] * 2


async def test_a_transfer_that_straddles_the_account_read_makes_the_tick_unknown(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    deposit = _cash_flow("CSD", 10_000.0)
    read = _Read(
        cash=110_000.0,
        equity=110_000.0,
        last_equity=100_000.0,
        cash_flow_reads=[[], [deposit]],
    )
    sync = make_sync(day_pnl_repo, read)

    assert await sync.tick() == "unknown"
    assert sync.envelope.latest_observation() is None
    assert _hold(day_pnl_repo) is None
    assert len(read.activity_calls) == 2


async def test_positions_are_not_required_for_the_equity_loss_verdict(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    sync = make_sync(day_pnl_repo, _Read(fail_positions=True))

    assert await sync.tick() == "observed"
    observation = sync.envelope.latest_observation()
    assert observation is not None
    assert observation.position_count is None


async def test_incomplete_cash_transfer_evidence_withdraws_the_observation(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    sync = make_sync(
        day_pnl_repo,
        _Read(cash_flows=[_cash_flow("CSD", None)]),
    )

    assert await sync.tick() == "unknown"
    assert sync.envelope.latest_observation() is None
    assert _hold(day_pnl_repo) is None


async def test_rejected_cash_transfer_evidence_withdraws_the_previous_observation(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    read = _Read()
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "observed"
    assert sync.envelope.latest_observation() is not None

    read.activity_error = BrokerEvidenceUnavailable(
        "Alpaca transfer activity evidence was malformed."
    )

    assert await sync.tick() == "unknown"
    assert sync.envelope.latest_observation() is None


async def test_rejected_cash_transfer_evidence_withdraws_on_direct_observe(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    read = _Read()
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "observed"

    read.activity_error = BrokerEvidenceUnavailable(
        "Alpaca transfer activity evidence was malformed."
    )

    with pytest.raises(BrokerEvidenceUnavailable):
        await sync.observe()
    assert sync.envelope.latest_observation() is None


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


async def test_a_breached_reading_still_shows_the_accounts_money_until_it_ages_or_the_read_fails(
    day_pnl_repo: ClerkSqliteRepository,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """PRD #2560 review A6: withdrawing admission is not forgetting the read.

    The money bar shows the last reading while a loss hold stands; it is
    forgotten, like the envelope's, when the read itself fails, and ages out
    on the envelope's own freshness bound.
    """
    read = _Read(unrealized=-5_000.0)
    sync = make_sync(day_pnl_repo, read)
    assert await sync.tick() == "hold_raised"

    assert sync.envelope.fresh_observation(NOON) is None
    shown = sync.display_observation(NOON)
    assert shown is not None and shown.equity_usd == 95_000.0
    assert sync.display_observation(NOON + OBSERVATION_MAX_AGE_MS + 1) is None

    read.activity_error = BrokerEvidenceUnavailable("Alpaca transfer activity evidence was malformed.")
    with pytest.raises(BrokerEvidenceUnavailable):
        await sync.observe()
    assert sync.display_observation(NOON) is None


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


async def test_an_external_order_does_not_hide_the_broker_wide_equity_change(
    day_pnl_repo: ClerkSqliteRepository,
    seeded_external_order_today: None,
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Broker equity already includes activity outside the Clerk's own FIFO book."""
    sync = make_sync(day_pnl_repo, _Read(unrealized=-50_000.0))
    assert await sync.tick() == "hold_raised"
    assert _hold(day_pnl_repo) is not None
    assert sync.envelope.fresh_observation(NOON) is None


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
        ({"equity": float("inf")}, ["equity"]),
        ({"cash": float("nan"), "equity": 100_000.0}, ["cash"]),
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

    The account answer is built first -- the pre-fill cash -- and only then
    does the clock move and the Clerk record the fill, so the response returns
    after the fill carrying a figure that does not include it.
    """

    def __init__(
        self,
        *,
        clock: _TestClock,
        cash: float,
        record_fill: Callable[[], None],
    ) -> None:
        super().__init__(now_ms=clock(), cash=cash)
        self._clock = clock
        self._fill_pending = True
        self._record_fill = record_fill

    def _record_in_flight_fill(self) -> None:
        if not self._fill_pending:
            return
        self._fill_pending = False
        self._clock.advance(READ_LEG_MS)
        self._record_fill()
        self._clock.advance(READ_LEG_MS)

    async def get_account(self) -> BrokerAccountSnapshot:
        snapshot = await super().get_account()
        self._record_in_flight_fill()
        return snapshot


def _observed_gate(*, cash: float, simulated: bool) -> LiveEnvelopeGate:
    """A gate holding one observation fresh at ``T0``: what the first ENTER is admitted against."""
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=simulated)
    gate.publish(
        AccountObservation(
            observed_at_ms=T0,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            equity_usd=cash,
            risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=day_pnl_window_start_ms(T0), risk_equity_window_start_ms=day_pnl_window_start_ms(T0),
            last_equity_usd=cash,
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
        repo, accepted, execution_id="exec-mid-read", quantity=10, source_event_at_ms=clock(), fee=0.0
    )


async def test_a_fill_recorded_while_the_broker_is_read_stays_reserved(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
    make_sync: Callable[..., LiveEnvelopeSync],
) -> None:
    """Two instances share $1,000, and the first's $1,000 fill lands mid-read.

    The broker answers $1,000 -- its snapshot predates the fill -- so only the
    first ENTER's reservation stands between the second instance and cash
    already spent. One account round trip is enough, because the fault was the
    stamp; the positions endpoint is no longer part of this equity-only verdict.
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
        record_fill=_fill_all_ten(envelope_repo, envelope_clock, first),
    )
    sync = make_sync(envelope_repo, read, simulated=False)

    reading = await sync.observe()

    assert envelope_repo.position(first_instance[0], "SPY") == 10.0
    with pytest.raises(AdmissionBlockedError) as refused:
        _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope)
    assert refused.value.decision.reason_code == LIVE_ENVELOPE_UNOBSERVED
    # The observation is dated when its reads were issued, not when they returned.
    assert reading.observation.observed_at_ms == T0


@pytest.mark.parametrize(
    "recorded_before_read_ms",
    [
        pytest.param(1, id="just-before-the-read"),
        pytest.param(FILL_VISIBILITY_GRACE_MS, id="at-the-grace-boundary"),
    ],
)
async def test_a_fill_recorded_just_before_the_read_is_issued_stays_reserved(
    envelope_repo: ClerkSqliteRepository,
    envelope_clock: _TestClock,
    two_active_instances: tuple[tuple[str, str], tuple[str, str]],
    make_sync: Callable[..., LiveEnvelopeSync],
    recorded_before_read_ms: int,
) -> None:
    """The broker's cash may lag a fill whose trade update it already delivered.

    Alpaca promises no ordering between the two, so a fill the Clerk recorded
    up to ``FILL_VISIBILITY_GRACE_MS`` before the read was issued -- the
    boundary, inclusive -- is not trusted to be in the answer. Here the broker
    still reports the pre-fill $1,000, and the second instance is refused
    rather than admitted against it. The 1 ms case is the one a zero grace
    would release; the boundary case is the one a grace applied off by one
    would.
    """
    first_instance, second_instance = two_active_instances
    first = _enter(
        envelope_repo,
        first_instance,
        symbol="SPY",
        envelope=_observed_gate(cash=1_000.0, simulated=False),
    )
    _fill_all_ten(envelope_repo, envelope_clock, first)()
    envelope_clock.advance(recorded_before_read_ms)
    sync = make_sync(
        envelope_repo, _LiveBroker(now_ms=envelope_clock(), cash=1_000.0), simulated=False
    )

    reading = await sync.observe()

    assert reading.observation.observed_at_ms == T0 + recorded_before_read_ms
    with pytest.raises(AdmissionBlockedError) as refused:
        _enter(envelope_repo, second_instance, symbol="QQQ", envelope=sync.envelope)
    assert refused.value.decision.reason_code == LIVE_ENVELOPE_CASH_EXCEEDED




@pytest.mark.parametrize("via_tick", [False, True])
async def test_rejected_transfer_read_cannot_be_revived_by_risk_apply(tmp_path, make_sync, via_tick: bool) -> None:
    from tests.broker.alpaca.clerk.sqlite.test_account_risk_policy import _policy
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _new_budget_repo

    repo = _new_budget_repo(tmp_path)
    broker = _Read()
    sync = make_sync(repo, broker)
    try:
        assert await sync.tick() == "observed"
        broker.activity_error = BrokerEvidenceUnavailable("incomplete transfer pages")
        if via_tick:
            assert await sync.tick() == "unknown"
        else:
            with pytest.raises(BrokerEvidenceUnavailable):
                await sync.observe()
        assert sync.apply_risk_policy(_policy(2, 200), expected_revision=1).observation is None
        assert sync.envelope.latest_observation() is None
        broker.activity_error = None
        assert await sync.tick() == "observed"
    finally:
        await sync.stop()
        repo.close()
