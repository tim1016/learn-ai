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
from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)
from app.services.alpaca_live_envelope import LiveEnvelopeNotInstalled, clear_loss_hold
from tests.broker.alpaca.clerk.activation_fixtures import _ActivationStore
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    TEST_ENVELOPE_VALUES,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.sqlite.conftest import TODAY_OPEN, _observe_foreign_order
from tests.broker.alpaca.clerk.test_active_authority import _activation, _Broker
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import (
    NOW_MS,
    _arm,
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


async def test_the_clear_judges_the_sealed_limit_not_a_loosened_configured_one(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
    tmp_path: Path,
) -> None:
    """The regression: raising the limit in the environment must not release a hold.

    ADR 0059 D3 seals every envelope value at arming, so changing one is a
    re-arm and never a silent drift. The clear used to re-observe against the
    *configured* values, which meant an operator could edit
    ``ALPACA_LIVE_LOSS_USD``, restart, and clear a hold the armed envelope
    still says stands -- with ENTER separately refusing
    ``LIVE_ENVELOPE_DISAGREEMENT``, so the account was left holdless *and*
    unable to trade, and the next re-arm restored no hold.
    """
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    broker.unrealized = -5_000.0  # limit = min(0.05 × 100,000, 5,000) = 5,000
    assert await runtime.envelope_sync.tick() == "hold_raised"
    assert runtime.envelope_sync.envelope.sealed == TEST_ENVELOPE_VALUES

    # The operator loosened both halves of the loss limit and restarted the
    # process; nobody re-armed, so the ledger still seals the old envelope.
    runtime.envelope_sync.envelope.values = replace(
        TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09
    )

    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)

    assert refused.outcome == "refused"
    assert refused.reason_code == "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
    # 5,000 is the sealed limit; 9,000 would be the loosened configured one,
    # and -5,000 breaches the first and not the second.
    assert refused.loss_limit_usd == pytest.approx(5_000.0)
    assert "sealed at arming" in refused.detail
    assert _hold(runtime.sqlite_repository) is not None


async def test_a_re_arm_at_the_looser_envelope_is_what_lets_the_clear_release(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
    tmp_path: Path,
) -> None:
    """The other half of the same rule: the CLI re-arm is the release path.

    Without this the fix above could be "the hold can never clear once the
    environment moved", which is a different bug.
    """
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    broker.unrealized = -5_000.0
    assert await runtime.envelope_sync.tick() == "hold_raised"
    loosened = replace(TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09)
    runtime.envelope_sync.envelope.values = loosened

    _arm(tmp_path, envelope=loosened, armed_at_ms=NOW_MS + 1)
    cleared = await clear_loss_hold(runtime, now_ms=NOW_MS)

    assert cleared.outcome == "cleared", cleared.detail
    assert cleared.loss_limit_usd == pytest.approx(9_000.0)
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
    # R5: unknown is not a number. The detail says the day cannot be judged,
    # so the figure beside it must be absent rather than plausible.
    assert refused.day_pnl_usd is None
    assert _hold(runtime.sqlite_repository) is not None


async def test_the_clear_names_an_unreadable_arming_ledger_rather_than_the_broker_feed(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
    tmp_path: Path,
) -> None:
    """The fourth unjudgeable cause reaches this screen, so it must be named here.

    An account whose seal cannot be read has no sealed loss limit to judge
    against, so the clear refuses — but the standing sentence enumerated three
    broker-side causes, and would have sent the operator to debug the feed
    while the actual fault was a corrupt ``live_arming.jsonl``.
    """
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    broker.unrealized = -5_000.0
    assert await runtime.envelope_sync.tick() == "hold_raised"

    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"armed ', 1),
        encoding="utf-8",
    )

    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)

    assert refused.outcome == "refused"
    assert refused.reason_code == "LIVE_ENVELOPE_UNOBSERVED"
    assert "arming inputs could not be read" in refused.detail
    assert refused.day_pnl_usd is None and refused.loss_limit_usd is None
    assert _hold(runtime.sqlite_repository) is not None


async def test_no_hold_is_reported_not_invented(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811 — the imported fixture
) -> None:
    runtime, _broker = shadow_runtime
    assert (await clear_loss_hold(runtime, now_ms=NOW_MS)).outcome == "no_hold"


async def test_a_runtime_without_an_envelope_cannot_clear(paper_runtime: ActiveClerkRuntime) -> None:
    with pytest.raises(LiveEnvelopeNotInstalled):
        await clear_loss_hold(paper_runtime, now_ms=NOW_MS)
