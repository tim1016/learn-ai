"""Instance-scoped source-bar evidence, and the world the primary authority custodies in (Direction 2)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from app.broker.alpaca.clerk.account_authority import (
    PAPER_EVIDENCE_ACCOUNT_PREFIX,
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveAlpacaClerk,
    ActiveClerkRuntime,
    reset_alpaca_clerk_for_testing,
    set_active_clerk_runtime,
    set_alpaca_clerk,
)
from app.services.bot_binding_authority import (
    PrimaryAccountBindingAuthority,
    primary_custody_kind,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector
from app.services.bot_start_admission import StartAdmissionUnavailable


def _trade_binding(sid: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=0,
    )


def test_paper_evidence_account_id_for_strategy_is_instance_scoped() -> None:
    account_id = paper_evidence_account_id_for_strategy("bot-a")

    assert account_id == f"{PAPER_EVIDENCE_ACCOUNT_PREFIX}bot-a"
    assert account_id != synthetic_account_id_for_strategy("bot-a")


def test_real_paper_authority_source_bars_opens_instance_scoped_ledger(tmp_path: Path) -> None:
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: "real_paper",
    )

    ledger = authority.source_bars()
    try:
        assert ledger is not None
        assert ledger.account_id == paper_evidence_account_id_for_strategy("bot-a")
        assert ledger.path == (
            tmp_path / "accounts" / "alpaca" / "paper:bot-a" / "source_bars.sqlite3"
        )
    finally:
        ledger.close()


def test_primary_authority_on_the_shadow_world_opens_the_shadow_evidence_ledger(
    tmp_path: Path,
) -> None:
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: "shadow",
    )

    ledger = authority.source_bars()
    try:
        assert ledger.account_id == "shadow-evidence:bot-a"
    finally:
        ledger.close()


def _clerk_double() -> ActiveAlpacaClerk:
    """Any installed Clerk: the custody world is read off the runtime, never the Clerk."""
    return cast(ActiveAlpacaClerk, object())


def test_primary_custody_kind_refuses_to_guess_with_no_authority_installed() -> None:
    """The one function deciding where a live-account bot's evidence lands."""
    reset_alpaca_clerk_for_testing()
    try:
        with pytest.raises(StartAdmissionUnavailable):
            primary_custody_kind()
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_the_real_paper_compat_seam_is_real_paper() -> None:
    """``set_alpaca_clerk`` declares no authority kind; the fallback keeps paper evidence stable."""
    set_alpaca_clerk(_clerk_double())
    try:
        assert primary_custody_kind() == "real_paper"
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_the_shadow_authority_is_shadow() -> None:
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="shadow",
            clerk=_clerk_double(),
            account_id="shadow:9LIVE0001",
            account_authority_kind="shadow",
        )
    )
    try:
        assert primary_custody_kind() == "shadow"
    finally:
        reset_alpaca_clerk_for_testing()


def test_primary_custody_kind_on_a_synthetic_runtime_refuses_to_guess() -> None:
    """No primary boot selects synthetic, so it is not a world to file evidence in.

    ``primary_custody_world`` answers ``None`` for every kind outside the two
    real custody worlds, and this caller refuses rather than filing a
    binding's evidence in an isolated Dry Run namespace.
    """
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="synthetic",
            clerk=_clerk_double(),
            account_id="sim:bot-a",
        )
    )
    try:
        with pytest.raises(StartAdmissionUnavailable):
            primary_custody_kind()
    finally:
        reset_alpaca_clerk_for_testing()


async def test_synthetic_runtime_uses_the_runners_clock_for_broker_custody_and_session(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
    from app.services.bot_binding_authority import SyntheticBindingAuthority
    from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at

    clock = _clock_at(FIXTURE_RTH_MS)
    authority = SyntheticBindingAuthority(
        binding=_trade_binding("clock-bot").model_copy(update={"mode": "dry_run"}),
        artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        runtime_in_use=lambda _: False, brokers={}, clock=clock,
    )
    try:
        await authority.ensure_recoverable()
        async with authority.runtime_for_projection() as runtime:
            repo = runtime.sqlite_repository
            broker = authority.brokers[authority.account_id]
            assert repo.clock() == clock()
            evidence = await broker.get_clock_evidence()
            assert evidence.is_open and evidence.observed_at_ms == clock()
            assert (await broker.get_account()).observed_at_ms == clock()
            clock.advance(15_000)
            repo.renew_execution_lease()
            assert repo.clock() == clock()
            assert (await broker.get_clock_evidence()).observed_at_ms == clock()
    finally:
        await close_synthetic_clerk_runtimes()


async def test_private_budget_seed_refreshes_preview_then_survives_runtime_release(tmp_path: Path, monkeypatch) -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_authority import SyntheticBindingAuthority
    from app.services.broker_v2_panel import budget_deploy
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

    sid = "private-budget"
    clock = _TestClock(NOON)
    consent = DeployBudgetConsent(committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="reviewed", world="synthetic")
    binding = _trade_binding(sid).model_copy(update={"mode": "dry_run", "sealed_account_id": f"sim:{sid}", "exit_terms": TERMS, "budget_consent": consent})
    authority = SyntheticBindingAuthority(binding=binding, artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        runtime_in_use=lambda _: False, brokers={}, clock=clock)
    try:
        await authority.ensure_recoverable()
        async with authority.runtime_for_projection() as runtime:
            assert runtime.sqlite_repository.budget_authority_version() == 2
            assert runtime.envelope_sync.risk_snapshot().observation.cash_available_usd == Decimal(500)
            authority.binding = binding.model_copy(update={"budget_consent": consent.model_copy(update={"committed_cents": 70_000})})
            await authority.ensure_recoverable()
            assert runtime.envelope_sync.risk_snapshot().observation.cash_available_usd == Decimal(700)
            runtime.clerk._quote_source = lambda symbol, now: SimpleNamespace(ask=100)
            await runtime.clerk.register_strategy_run(authority.binding)
            await runtime.clerk.record_deployment_launch(authority.binding)
            await runtime.clerk.stop_strategy_run(strategy_instance_id=sid, run_id=binding.run_id, reason="owner_stop")
            await runtime.envelope_sync.tick()
            before = tuple(runtime.sqlite_repository.custody_transitions())
        await authority.release_if_unused()
        clock.advance(86_400_000)
        # Restart has no transient consent: custody's one stored commitment
        # is the initial cash source and the command remains a read.
        authority.binding = binding.model_copy(update={"budget_consent": None})
        monkeypatch.setattr(budget_deploy, "_primary", lambda account: object())
        monkeypatch.setattr(budget_deploy, "get_bot_task_registry", lambda: SimpleNamespace(
            binding_for_control=lambda broker, identity: authority.binding,
            synthetic_runtime_for_projection=lambda _: authority.runtime_for_projection(),
        ))
        receipt = await budget_deploy.command_receipt("PARENT", sid)
        assert receipt.status == "deployed" and receipt.committed_usd == "700.00"
        async with authority.runtime_for_projection() as recovered:
            assert tuple(recovered.sqlite_repository.custody_transitions()) == before
            assert recovered.envelope_sync.risk_snapshot().observation is None
            assert recovered.envelope_sync._task is None
            assert recovered.sqlite_repository.active_run(sid) is None
            assert len([item for item in recovered.sqlite_repository.custody_transitions() if item["transition_kind"] == "DEPLOY_COMMITTED"]) == 1
    finally:
        await close_synthetic_clerk_runtimes()


async def test_projection_cannot_activate_a_new_private_authority_or_prepare_consent(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.synthetic_activation import SyntheticActivationStore
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_authority import SyntheticBindingAuthority

    sid = "unactivated-read"
    binding = _trade_binding(sid).model_copy(update={
        "mode": "dry_run", "budget_consent": DeployBudgetConsent(
            committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="review", world="synthetic",
        ),
    })
    authority = SyntheticBindingAuthority(binding=binding, artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        runtime_in_use=lambda _: False, brokers={})
    async with authority.runtime_for_projection() as runtime:
        assert runtime.clerk is None
        assert runtime.startup_failure.reason_code == "SYNTHETIC_ACTIVATION_REQUIRED"
    assert SyntheticActivationStore(tmp_path).latest(authority.account_id) is None
    assert not (tmp_path / "accounts" / "alpaca" / authority.account_id / "clerk.db").exists()


async def test_cold_projection_and_concurrent_start_share_lifecycle_fence(tmp_path: Path) -> None:
    import asyncio
    from decimal import Decimal

    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_authority import BindingAuthoritySelector
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

    sid = "projection-start-race"
    binding = _trade_binding(sid).model_copy(update={
        "mode": "dry_run", "sealed_account_id": f"sim:{sid}", "budget_consent": DeployBudgetConsent(
            committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="review", world="synthetic",
        ),
    })
    selector = BindingAuthoritySelector(artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        real_projector=cast(AlpacaLifecycleProjector, object()), external_start_guard=None,
        runtime_in_use=lambda _: False, clock=_TestClock(NOON))
    owner = selector.for_binding(binding)
    await owner.ensure_recoverable()
    await owner.release_if_unused()
    read = selector.for_binding(binding.model_copy(update={"budget_consent": None}))
    starter = selector.for_binding(binding)
    try:
        async with read.runtime_for_projection() as cold:
            assert cold.envelope_sync.risk_snapshot().observation is None
            starting = asyncio.create_task(starter.ensure_recoverable())
            await asyncio.sleep(0)
            assert not starting.done()
            assert get_clerk_runtime(f"sim:{sid}") is cold
        await asyncio.wait_for(starting, timeout=5)
        hot = get_clerk_runtime(f"sim:{sid}")
        assert hot is not None and hot is not cold
        assert hot.envelope_sync.risk_snapshot().observation.cash_available_usd == Decimal(500)
        assert hot.envelope_sync._task is not None and not hot.envelope_sync._task.done()
        # A later projection of an active runtime neither closes nor replaces its cadence.
        async with selector.for_binding(binding).runtime_for_projection() as projected:
            assert projected is hot
        assert get_clerk_runtime(f"sim:{sid}") is hot
        assert not hot.envelope_sync._task.done()
    finally:
        await close_synthetic_clerk_runtimes()


async def test_admission_promotes_a_cold_projection_before_it_becomes_in_use(tmp_path: Path) -> None:
    from decimal import Decimal

    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.schemas.deployment_budget import DeployBudgetConsent
    from app.services.bot_binding_authority import BindingAuthoritySelector
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock

    sid = "promoted-projection"
    binding = _trade_binding(sid).model_copy(update={"mode": "dry_run", "budget_consent": DeployBudgetConsent(
        committed_cents=50_000, risk_revision=0, actor="owner", request_fingerprint="review", world="synthetic",
    )})
    in_use = False
    selector = BindingAuthoritySelector(artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        real_projector=cast(AlpacaLifecycleProjector, object()), external_start_guard=None,
        runtime_in_use=lambda _: in_use, clock=_TestClock(NOON))
    owner = selector.for_binding(binding)
    await owner.ensure_recoverable()
    await owner.release_if_unused()
    try:
        async with selector.for_binding(binding).runtime_for_projection() as runtime:
            assert runtime.envelope_sync._task is None
            await selector.for_binding(binding).ensure_recoverable()
            in_use = True
            assert runtime.envelope_sync.risk_snapshot().observation.cash_available_usd == Decimal(500)
        assert get_clerk_runtime(f"sim:{sid}") is runtime
        assert runtime.envelope_sync._task is not None and not runtime.envelope_sync._task.done()
    finally:
        await close_synthetic_clerk_runtimes()
