"""The guarded loss-hold clear (ADR 0059 D4; ADR 0011 §6 shape).

``clear_loss_hold`` is the ONLY release for the loss hold Task 6's sync
raises: it re-observes the account and refuses while the breach still
stands. Every loss-clear test composes the real Live authority. Real broker losses belong
only to real custody; Shadow's simulated losses have their own integration tests.

No wall clock: the custody repository is opened on a clock pinned to
``NOW_MS``, and every ``clear_loss_hold`` call in this file passes that same
stamp explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.broker.contract.models import BrokerActivity
from app.services.alpaca_live_envelope import clear_loss_hold
from app.services.broker_v2_panel.budget_deploy import _broker_figures
from tests.broker.alpaca.clerk.activation_fixtures import _ActivationStore
from tests.broker.alpaca.clerk.live_authority_fixtures import compose_live
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.sqlite.conftest import complete_fee_evidence
from tests.broker.alpaca.clerk.test_active_authority import _activation, _Broker
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import (
    NOW_MS,
)


@pytest.fixture()
async def loss_runtime(tmp_path: Path) -> AsyncIterator[tuple[ActiveClerkRuntime, _LiveBroker]]:
    broker = _LiveBroker(now_ms=NOW_MS)
    runtime = await compose_live(tmp_path, broker, now_ms=NOW_MS)
    assert runtime.authority_kind == "sqlite", runtime.startup_failure
    complete_fee_evidence(runtime.sqlite_repository)
    try:
        yield runtime, broker
    finally:
        await runtime.close()


def _hold(repository: ClerkSqliteRepository) -> dict | None:
    return repository.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        strategy_instance_id=None,
    )


@pytest.fixture()
async def paper_runtime(tmp_path: Path) -> AsyncIterator[ActiveClerkRuntime]:
    """A real-paper authority, which composes no envelope at all (Task 7)."""
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    runtime: ActiveClerkRuntime | None = None
    try:
        runtime = await select_active_clerk_runtime(
            read=broker,
            trade=broker,
            artifacts_root=tmp_path,
            activation_store=_ActivationStore(_activation()),
            repository_opener=lambda _account_id, _root: repo,
            live_envelope_values=TEST_ENVELOPE_VALUES,
        )
        assert runtime.authority_kind == "sqlite"
        yield runtime
    finally:
        # A composed runtime owns this handle; a refused selection leaves it
        # to the fixture, execution lease and all.
        if runtime is None:
            repo.close()
        else:
            await runtime.close()


async def test_the_clear_refuses_while_the_breach_stands_then_clears_once_it_has_lifted(
    loss_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    runtime, broker = loss_runtime
    broker.unrealized = -5_000.0
    assert await runtime.envelope_sync.tick() == "hold_raised"
    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert refused.outcome == "refused" and refused.reason_code == "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
    assert refused.day_pnl_usd == pytest.approx(-5_000.0) and refused.loss_limit_usd == pytest.approx(5_000.0)
    assert _hold(runtime.sqlite_repository) is not None
    broker.unrealized = -4_999.0
    cleared = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert cleared.outcome == "cleared" and cleared.reason_code is None
    assert _hold(runtime.sqlite_repository) is None


async def test_a_loosened_configured_limit_cannot_clear_the_original_loss_hold(
    loss_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    """#2543: the hold retains the threshold it was raised on.

    An operator who loosens the configured loss limit and restarts cannot
    clear a hold the original limit still says stands. No arming seal takes
    part any more (#2629): the retained threshold is the whole protection.
    """
    runtime, broker = loss_runtime
    assert runtime.envelope_sync is not None
    broker.unrealized = -5_000.0  # limit = min(0.05 × 100,000, 5,000) = 5,000
    assert await runtime.envelope_sync.tick() == "hold_raised"
    runtime.envelope_sync.envelope.values = replace(
        TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09
    )

    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)

    assert refused.outcome == "refused", refused.detail
    assert refused.loss_limit_usd == pytest.approx(9_000.0)
    assert "recorded when this hold began" in refused.detail
    assert _hold(runtime.sqlite_repository) is not None


async def test_the_clear_refuses_an_unknown_fact(
    loss_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    runtime, broker = loss_runtime
    broker.unrealized = -5_000.0
    # Raise the hold first with the fact still known, then make it unknown:
    # without the broker's prior-close equity there is no day baseline, so the
    # envelope withdraws its judgement entirely.
    assert await runtime.envelope_sync.tick() == "hold_raised"
    broker.last_equity_known = False
    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert refused.outcome == "refused" and refused.reason_code == "LIVE_ENVELOPE_UNOBSERVED"
    # R5: unknown is not a number. The detail says the day cannot be judged,
    # so the figure beside it must be absent rather than plausible.
    assert refused.day_pnl_usd is None
    assert _hold(runtime.sqlite_repository) is not None


async def test_the_clear_names_a_missing_loss_limit_rather_than_the_broker_feed(
    loss_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    """An account with no loss limit to judge against is named as such.

    The standing sentence enumerates broker-side causes, and would send the
    operator to debug the feed while the actual fault is that no limit is set.
    """
    runtime, broker = loss_runtime
    assert runtime.envelope_sync is not None
    broker.unrealized = -5_000.0
    assert await runtime.envelope_sync.tick() == "hold_raised"
    runtime.envelope_sync.envelope.values = None

    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)

    assert refused.outcome == "refused"
    assert refused.reason_code == "LIVE_ENVELOPE_UNOBSERVED"
    assert "No daily loss limit is set" in refused.detail
    assert refused.day_pnl_usd is None and refused.loss_limit_usd is None
    assert _hold(runtime.sqlite_repository) is not None


async def test_no_hold_is_reported_not_invented(
    loss_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
) -> None:
    runtime, _broker = loss_runtime
    assert (await clear_loss_hold(runtime, now_ms=NOW_MS)).outcome == "no_hold"


async def test_paper_uses_the_same_guarded_hold_surface(paper_runtime: ActiveClerkRuntime) -> None:
    assert paper_runtime.envelope_sync is not None
    assert (await clear_loss_hold(paper_runtime, now_ms=NOW_MS)).outcome == "no_hold"


# The limit the half-cent day breaches: min(0.05 × 10_000.70, 3.00) = 3.00.
_HALF_CENT_ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05, loss_usd=3.0, xh_entry_bps=10.0, xh_exit_bps=10.0
)


class _DepositedBroker(_LiveBroker):
    """A 10_000.015 equity over a 10_000.70 close, with 1.10 and 2.20 deposited.

    The broker reports its equity as its own float — ``10_000.015``, not
    ``cash + unrealized`` re-derived here — exactly as a real account read
    does. The exact day is then −3.985, a half-cent tie, while the float the
    loss rule compares lands at −3.98500000000131, so a ``:.2f`` of it reads
    −3.99 against the exact half-even cent −3.98 (#2612).
    """

    def __init__(self) -> None:
        super().__init__(now_ms=NOW_MS, cash=10_000.70, unrealized=-0.685)

    async def get_account(self) -> Any:
        return (await super().get_account()).model_copy(update={"equity": 10_000.015})

    async def list_activities(self, **_kwargs: Any) -> list[BrokerActivity]:
        return [
            BrokerActivity(
                broker="alpaca",
                activity_id=f"deposit-{index}",
                activity_type="CSD",
                category=None,
                symbol=None,
                side=None,
                quantity=None,
                price=None,
                net_amount=amount,
                occurred_at_ms=NOW_MS,
                observed_at_ms=NOW_MS,
            )
            for index, amount in enumerate((1.10, 2.20))
        ]


async def test_the_refusal_names_the_same_cent_deploys_today_pnl_shows(tmp_path: Path) -> None:
    """One half-cent day, two screens, one figure (#2612).

    The refusal's day figure is rendered through the money boundary from the
    same exact ``Decimal`` formula Deploy's "today P&L" uses, so both screens
    name the same cent. On master the message formatted the rule's float with
    ``:.2f`` and read −3.99 while Deploy read −3.98.
    """
    runtime = await compose_live(
        tmp_path,
        _DepositedBroker(),
        now_ms=NOW_MS,
        live_envelope_values=_HALF_CENT_ENVELOPE,
    )
    try:
        assert runtime.authority_kind == "sqlite", runtime.startup_failure
        assert runtime.envelope_sync is not None
        complete_fee_evidence(runtime.sqlite_repository)
        assert await runtime.envelope_sync.tick() == "hold_raised"

        refused = await clear_loss_hold(runtime, now_ms=NOW_MS)

        assert refused.outcome == "refused"
        assert refused.reason_code == "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
        assert "Day P&L -3.98 USD is still at or below the 3.00 USD loss limit" in refused.detail

        observation = runtime.envelope_sync.display_observation(NOW_MS)
        assert observation is not None
        deploy_figures = _broker_figures(runtime.sqlite_repository, observation)
        assert deploy_figures["today_pnl_usd"] == "-3.98"
    finally:
        await runtime.close()
