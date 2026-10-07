"""Which account authorities get a deadline keeper (#2913).

A Paper account's Clerk rests an option entry at Alpaca, so it keeps the
entry's deadline. A Live Shadow's and a Live Clerk take no option plan yet,
and gain no new background task until they do.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.test_active_authority import _activation, _ActivationStore, _Broker
from tests.broker.alpaca.clerk.test_live_authority_runtime import (
    _RecordingLiveBroker,
    live_runtime,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import (
    _LiveBroker,
    shadow_runtime,  # noqa: F401 -- pytest fixture, used by name
)


async def test_a_paper_accounts_clerk_is_composed_with_a_keeper_that_its_facade_nudges(tmp_path: Path) -> None:
    broker = _Broker()
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)

    runtime = await select_active_clerk_runtime(
        read=broker, trade=broker, artifacts_root=tmp_path, activation_store=_ActivationStore(_activation()),
        repository_opener=lambda _account_id, _root: repo,
    )

    try:
        keeper = runtime.deadline_keeper
        assert isinstance(keeper, DeadlineKeeper)
        assert runtime.clerk._entry_deadline_nudge == keeper.nudge
        # Acts through the Clerk's own guarded trade port, and is not started by the selection.
        assert keeper._trade is runtime.clerk._trade and keeper._task is None

        runtime.start_background_taps()

        assert keeper._task is not None and not keeper._task.done()
    finally:
        await runtime.close()
    assert runtime.deadline_keeper is None and keeper._task is None


async def test_a_live_accounts_clerk_gets_no_keeper(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],  # noqa: F811
) -> None:
    runtime, _broker = live_runtime

    assert runtime.clerk is not None and runtime.clerk.account_mode == "live"
    assert runtime.deadline_keeper is None and runtime.clerk._entry_deadline_nudge is None


async def test_a_live_shadows_clerk_gets_no_keeper(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],  # noqa: F811
) -> None:
    runtime, _broker = shadow_runtime

    assert runtime.authority_kind == "shadow"
    assert runtime.deadline_keeper is None and runtime.clerk._entry_deadline_nudge is None
