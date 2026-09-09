"""The guarded loss-hold clear (ADR 0059 D4; ADR 0011 §6 shape).

``clear_loss_hold`` is the ONLY release for the loss hold Task 6's sync
raises: it re-observes the account and refuses while the breach still
stands. Every test here composes the real shadow authority from Task 7's
harness -- the ``shadow_runtime`` fixture imported below -- so the wiring
under test is the same ``ActiveClerkRuntime`` production selects, not a
hand-built stand-in.

No wall clock: the shadow repository is opened on a clock pinned to
``NOW_MS``, and every ``clear_loss_hold`` call in this file passes that same
stamp explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.services.alpaca_live_envelope import LiveEnvelopeNotInstalled, clear_loss_hold
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.test_day_pnl import TODAY_OPEN, _observe_foreign_order
from tests.broker.alpaca.clerk.test_active_authority import _activation, _ActivationStore, _Broker
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import (
    NOW_MS,
    _LiveBroker,
    shadow_runtime,  # noqa: F401 — the composed-shadow fixture, reused as-is
)


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
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(_activation()),
        repository_opener=lambda _account_id, _root: repo,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    assert runtime.authority_kind == "sqlite"
    try:
        yield runtime
    finally:
        await runtime.close()


async def test_the_clear_refuses_while_the_breach_stands_then_clears_once_it_has_lifted(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
) -> None:
    runtime, broker = shadow_runtime
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


async def test_the_clear_refuses_an_unknown_fact(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
) -> None:
    runtime, broker = shadow_runtime
    broker.unrealized = -5_000.0
    # Raise the hold first with the fact still known, then make it unknown:
    # an external order observed today (before NOW_MS, same ET day) makes the
    # day's P&L unknowable, which withdraws the envelope's judgement entirely.
    assert await runtime.envelope_sync.tick() == "hold_raised"
    _observe_foreign_order(runtime.sqlite_repository, observed_at_ms=TODAY_OPEN)
    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert refused.outcome == "refused" and refused.reason_code == "LIVE_ENVELOPE_UNOBSERVED"
    assert _hold(runtime.sqlite_repository) is not None


async def test_no_hold_is_reported_not_invented(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
) -> None:
    runtime, _broker = shadow_runtime
    assert (await clear_loss_hold(runtime, now_ms=NOW_MS)).outcome == "no_hold"


async def test_a_runtime_without_an_envelope_cannot_clear(paper_runtime: ActiveClerkRuntime) -> None:
    with pytest.raises(LiveEnvelopeNotInstalled):
        await clear_loss_hold(paper_runtime, now_ms=NOW_MS)
