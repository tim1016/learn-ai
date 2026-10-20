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


@pytest.mark.parametrize(
    ("custody_kind", "account_id", "account_dir"),
    [
        pytest.param(
            "real_paper", paper_evidence_account_id_for_strategy("bot-a"), "paper:bot-a",
            id="real-paper-opens-instance-scoped-ledger",
        ),
        pytest.param(
            "shadow", "shadow-evidence:bot-a", "shadow-evidence:bot-a",
            id="shadow-world-opens-the-shadow-evidence-ledger",
        ),
    ],
)
def test_primary_authority_source_bars_opens_the_ledger_of_its_custody_world(
    tmp_path: Path, custody_kind: str, account_id: str, account_dir: str,
) -> None:
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: custody_kind,
    )

    ledger = authority.source_bars()
    try:
        assert ledger is not None, "the authority opens a source-bar ledger"
        assert ledger.account_id == account_id, "the ledger is scoped to the custody world's evidence account"
        assert ledger.path == (
            tmp_path / "accounts" / "alpaca" / account_dir / "source_bars.sqlite3"
        ), "the ledger file lives under that evidence account"
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
    from app.services.broker_v2_panel import bot_custody, budget_deploy
    from app.services.broker_v2_panel.deploy_submissions import DeploySubmission
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
        monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: SimpleNamespace(
            binding_for_control=lambda broker, identity: authority.binding,
            dry_run_restoration_state=lambda _sid: None,
            synthetic_runtime_for_projection=lambda _: authority.runtime_for_projection(),
        ))
        receipt = await budget_deploy.command_receipt("PARENT", DeploySubmission(
            submission_key="submission-0001", strategy_instance_id=sid, claimed_at_ms=0, request_fingerprint="f",
        ))
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
    # A read creates nothing of the account's: not its Clerk store, and not its source-bar ledger.
    assert not (tmp_path / "accounts" / "alpaca" / authority.account_id).exists()


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
            assert runtime.deadline_keeper._task is None
            await selector.for_binding(binding).ensure_recoverable()
            in_use = True
            assert runtime.envelope_sync.risk_snapshot().observation.cash_available_usd == Decimal(500)
            # An operating account keeps its scheduled closes: the keeper runs as it does on a fresh opening.
            assert runtime.deadline_keeper._task is not None and not runtime.deadline_keeper._task.done()
        assert get_clerk_runtime(f"sim:{sid}") is runtime
        assert runtime.envelope_sync._task is not None and not runtime.envelope_sync._task.done()
        assert runtime.deadline_keeper._task is not None and not runtime.deadline_keeper._task.done()
    finally:
        await close_synthetic_clerk_runtimes()


async def test_promoting_an_orphans_read_runs_the_recovery_the_read_skipped(tmp_path: Path) -> None:
    """A read of an unbound orphan opens it without recovery; operating it then recovers it first.

    The orphan's run is still ACTIVE on disk when the read opens it. Promoted
    in the same task, the opening must retire that run exactly as a fresh
    operating opening does, before its scheduled closes are kept.
    """
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.services.bot_binding_authority import BindingAuthoritySelector
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
    from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

    sid = "promoted-orphan-read"
    binding = _trade_binding(sid).model_copy(update={
        "mode": "dry_run", "sealed_account_id": f"sim:{sid}", "exit_terms": TERMS,
    })
    selector = BindingAuthoritySelector(artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        real_projector=cast(AlpacaLifecycleProjector, object()), external_start_guard=None,
        runtime_in_use=lambda _: False, clock=_TestClock(NOON))
    deployed = selector.for_binding(binding)
    try:
        await deployed.ensure_recoverable()
        runtime = get_clerk_runtime(f"sim:{sid}")
        assert runtime is not None and runtime.sqlite_repository is not None
        runtime.sqlite_repository.register_strategy_instance(
            strategy_instance_id=sid, symbol="SPY", config_hash="historical-orphan",
        )
        seed_historical_run_started(
            runtime.sqlite_repository, account_id=f"sim:{sid}", strategy_instance_id=sid,
            lifecycle_run_id=binding.run_id,
        )
        # The process died with the run ACTIVE: nothing stopped it, nothing recovered it.
        await deployed.release_if_unused()

        orphan = selector.unbound_dry_run(sid)
        async with orphan.runtime_for_projection() as read:
            assert read.sqlite_repository.active_run(sid) is not None
            assert read.deadline_keeper._task is None
            await orphan.ensure_recoverable()
            assert read.sqlite_repository.active_run(sid) is None
            assert read.deadline_keeper._task is not None and not read.deadline_keeper._task.done()
    finally:
        await close_synthetic_clerk_runtimes()


def _dry_run_authority(tmp_path: Path, sid: str):
    """The selector and the Dry Run binding of ``sid``; no bot's task ever owns the account."""
    from app.services.bot_binding_authority import BindingAuthoritySelector
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
    from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

    binding = _trade_binding(sid).model_copy(update={
        "mode": "dry_run", "sealed_account_id": f"sim:{sid}", "exit_terms": TERMS,
    })
    selector = BindingAuthoritySelector(artifacts_root=tmp_path, lifecycle_repo_for=lambda _: None,
        real_projector=cast(AlpacaLifecycleProjector, object()), external_start_guard=None,
        runtime_in_use=lambda _: False, clock=_TestClock(NOON))
    return selector, binding


async def test_a_custody_guard_that_commits_nothing_leaves_a_new_dry_run_no_open_authority(tmp_path: Path) -> None:
    """#3217: a preview, or a Start that is refused, of a Dry Run with no bot in it keeps nothing open.

    Custody is read from the account's own Clerk, so the guard opens it; with
    no bot registered in the account when the guard ends, it is released
    instead of staying registered, heartbeating and sampling for the life of
    the process.
    """
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime

    sid = "guard-first-time"
    selector, binding = _dry_run_authority(tmp_path, sid)
    try:
        async with selector.for_binding(binding).start_custody_guard() as (snapshot, _policy, _terms):
            assert snapshot.account_id == f"sim:{sid}"
            assert get_clerk_runtime(f"sim:{sid}") is not None
        assert get_clerk_runtime(f"sim:{sid}") is None
    finally:
        await close_synthetic_clerk_runtimes()


async def test_a_custody_guard_whose_start_registered_its_bot_keeps_the_authority_open(tmp_path: Path) -> None:
    """The account stays open though no bot's task is in the runner's table: its run-end pass is still owed.

    A bot that ends while its Start is still inside the guard is reaped from
    the runner's table before its own run-end release has taken the account's
    lock, which the guard holds.
    """
    from types import SimpleNamespace

    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.schemas.deployment_budget import DeployBudgetConsent

    sid = "guard-registered"
    selector, binding = _dry_run_authority(tmp_path, sid)
    binding = binding.model_copy(update={"budget_consent": DeployBudgetConsent(
        committed_cents=50_000, risk_revision=0, actor="owner",
        request_fingerprint="reviewed-guard", world="synthetic",
    )})
    try:
        async with selector.for_binding(binding).start_custody_guard():
            opened = get_clerk_runtime(f"sim:{sid}")
            assert opened is not None and opened.clerk is not None
            opened.clerk._quote_source = lambda _symbol, _now: SimpleNamespace(ask=100)
            await opened.clerk.register_strategy_run(binding)
        assert get_clerk_runtime(f"sim:{sid}") is opened is not None
    finally:
        await close_synthetic_clerk_runtimes()


async def test_a_custody_guard_leaves_an_activated_dry_run_s_authority_to_its_caller(tmp_path: Path) -> None:
    """Archive retires a stopped Dry Run's bot through the authority its guard opened, after the guard ends."""
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime

    sid = "guard-activated"
    selector, binding = _dry_run_authority(tmp_path, sid)
    try:
        previous = selector.for_binding(binding)
        await previous.ensure_recoverable()
        await previous.release_if_unused()
        assert get_clerk_runtime(f"sim:{sid}") is None

        async with selector.for_binding(binding).archive_custody_guard():
            pass

        assert get_clerk_runtime(f"sim:{sid}") is not None
    finally:
        await close_synthetic_clerk_runtimes()
