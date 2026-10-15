"""A Dry Run's simulated account is opened by a Start that goes through, not by a look at one (#3217).

The Deploy page previews on every click, and each preview names a new Dry Run.
What a preview, or a Start that is refused, opened to read custody from must
not stay open for the life of the process: a Clerk, its heartbeat and its
samplers per click.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    close_synthetic_clerk_runtimes,
    get_clerk_runtime,
    reset_alpaca_clerk_for_testing,
)
from app.broker.alpaca.clerk.synthetic_activation import SyntheticActivationStore
from app.services.bot_runner_errors import MarketDataFeedUnavailableError
from tests._helpers.bot_runner.custody import _SID, _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _install_fake_clerk


@pytest.fixture(autouse=True)
async def _no_synthetic_authority_outlives_its_test() -> AsyncIterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    await close_synthetic_clerk_runtimes()
    reset_alpaca_clerk_for_testing()


async def test_a_start_preview_of_a_new_dry_run_leaves_no_open_authority(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    for sid in ("preview-one", "preview-two", "preview-three"):
        for _ in range(2):
            await registry.preview_start_admission(
                exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=sid, symbol="SPY", mode="dry_run",
            )

            assert get_clerk_runtime(f"sim:{sid}") is None


async def test_a_refused_dry_run_start_leaves_no_open_authority(tmp_path: Path) -> None:
    # No market-data feed is installed: the Start is refused on the evidence it has.
    registry = _registry(tmp_path, None)

    for _ in range(2):
        with pytest.raises(MarketDataFeedUnavailableError):
            await registry.deploy_with_admission(
                exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="dry_run",
            )

        assert get_clerk_runtime(f"sim:{_SID}") is None


async def test_an_admitted_dry_run_start_opens_its_authority_once_and_keeps_it_for_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_clerk(monkeypatch, _FakeClerk())
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    account_id = f"sim:{_SID}"

    started = await registry.deploy_with_admission(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="dry_run",
        strategy_key="ema_crossover_signal",
    )
    try:
        assert started.admission.allowed is True
        opened = get_clerk_runtime(account_id)
        assert opened is not None and opened.sqlite_repository.active_run(_SID) is not None
        activation = SyntheticActivationStore(tmp_path)
        assert activation.account_ids() == (account_id,)
        assert activation.latest(account_id).authority_generation == 1
    finally:
        await registry.stop("alpaca", _SID)
