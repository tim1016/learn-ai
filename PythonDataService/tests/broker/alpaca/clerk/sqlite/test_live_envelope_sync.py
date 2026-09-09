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
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    OBSERVATION_MAX_AGE_MS,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import NOON

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
        read: _Read,
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
