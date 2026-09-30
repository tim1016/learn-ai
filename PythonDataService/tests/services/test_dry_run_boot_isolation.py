"""A Dry Run boot cannot restore is that bot's failure, never the lane's (#2582).

2026-09-29: a quick ``podman restart`` of the Live clerk crash-looped the
real-money process five times. Boot recovery reopened a Dry Run bot's own
``sim:`` account while the dead process's execution lease on it was still
live, and the ``ExecutionLeaseHeld`` it raised aborted the whole application
-- after the Live account authority had already installed. Waiting that
lease out inside boot recovery instead would have held the real-money lane
off the network for as long as the lease lived, and longer per Dry Run.

These tests drive the runner's real boot recovery and Dry Run restoration
against a real synthetic authority and a real account authority, at the
production lease cadence unless a test says otherwise. Only the other
process is simulated: a second handle on the Dry Run's account, opened under
a foreign lease owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    close_synthetic_clerk_runtimes,
    get_alpaca_clerk,
    get_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sealed_ledger import append_canonical_jsonl_line
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.synthetic_activation import (
    SyntheticActivationInvalid,
    SyntheticActivationRecord,
    SyntheticActivationStore,
)
from app.engine.live.desired_state import DesiredState
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import bot_runner
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import BotTaskRegistry
from app.services.bot_runner_errors import RunAdmissionRefusedError
from app.services.broker_v2_panel.bot_custody import binding_clerk_runtime
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError
from app.utils.timestamps import now_ms_utc
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.test_active_authority import _Broker

SID = "live-dry-dv-spy-0928"
SIM_ACCOUNT = f"sim:{SID}"


def _dry_run_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID, strategy_key="deployment_validation", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-1", created_at_ms=0,
        sealed_account_id=SIM_ACCOUNT, exit_terms=TERMS, budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed",
            world="synthetic",
        ),
    )


@dataclass(frozen=True)
class _Lane:
    registry: BotTaskRegistry
    account: SqliteAlpacaClerkFacade
    artifacts_root: Path

    async def boot_account(self) -> None:
        """The lifespan's own account boot recovery, as it awaits it before serving."""
        await self.registry.run_boot_recovery(recover=self.account.recover, reconcile=self.account.reconcile_once)

    async def start_refusal(self) -> RunAdmissionRefusedError | None:
        """What Start says for the Dry Run, or ``None`` when restoration no longer refuses it."""
        try:
            await self.registry.preview_start_admission(
                broker="alpaca", strategy_instance_id=SID, symbol="SPY", mode="dry_run", exit_terms=TERMS,
            )
        except RunAdmissionRefusedError as refused:
            return refused
        return None


@pytest.fixture
async def restarted_lane(tmp_path: Path) -> AsyncIterator[_Lane]:
    """A lane whose account authority installed, restarted with one deployed Dry Run.

    The Deploy happened in the process that died: it recorded the bot's
    binding and activated its ``sim:`` account. The restarted process has
    installed its account authority and has not yet run boot recovery.
    """
    broker = _Broker()
    account_repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "account")
    account = SqliteAlpacaClerkFacade(repo=account_repo, read=broker, trade=broker, account_mode="paper")  # type: ignore[arg-type]
    binding = _dry_run_binding()
    previous = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    previous._bindings.record_launch(binding, launch_reason="deploy")
    deployed = previous._authority_for(binding)
    await deployed.ensure_recoverable()
    await deployed.release_if_unused()
    assert get_clerk_runtime(SIM_ACCOUNT) is None
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=account, _sqlite_repository=account_repo)
    )
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None)
    try:
        yield _Lane(registry=registry, account=account, artifacts_root=tmp_path)
    finally:
        await registry.stop_all()
        await close_synthetic_clerk_runtimes()
        set_active_clerk_runtime(None)
        account_repo.close()


def _another_process_holds_the_dry_run(artifacts_root: Path, *, lease_ttl_ms: int) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.open(
        account_id=SIM_ACCOUNT,
        artifacts_root=artifacts_root,
        lease_owner="boot:another-process",
        lease_ttl_ms=lease_ttl_ms,
    )


async def test_the_account_boots_at_once_while_a_dry_run_s_account_is_still_held(
    restarted_lane: _Lane,
) -> None:
    """The incident, at production cadence: the Dry Run's lease never holds the account's boot.

    Boot recovery is what the lifespan awaits before the lane serves any
    read. It once crashed on the Dry Run's held lease, then (waiting it out)
    stalled for the lease's whole life; now it finishes while the Dry Run is
    still being restored off the serving path.
    """
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        await asyncio.wait_for(restarted_lane.boot_account(), timeout=5.0)
        restoring = restarted_lane.registry.start_dry_run_restoration()

        # The account authority installed before boot recovery still serves.
        assert get_alpaca_clerk() is restarted_lane.account
        await asyncio.sleep(0.2)
        assert not restoring.done()
        # Start for the Dry Run refuses while it is restored: fail-closed, and in plain words.
        refused = await restarted_lane.start_refusal()
        assert refused is not None
        assert str(refused) == "This Dry Run is still being restored after the Clerk restarted."
        assert refused.detail == "Wait up to a minute, then start it again."
    finally:
        await restarted_lane.registry.stop_all()
        holder.close()


async def test_a_dry_run_whose_account_another_process_still_holds_is_its_own_failure(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held past boot's deadline: that bot's Start says why and what to do, in plain words."""
    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 1.5)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        await restarted_lane.boot_account()
        await restarted_lane.registry.start_dry_run_restoration()
        for _ in range(60):
            if restarted_lane.registry.dry_run_restoration_state(SID) == "account_held":
                break
            await asyncio.sleep(0.05)
        assert restarted_lane.registry.dry_run_restoration_state(SID) == "account_held"

        assert get_alpaca_clerk() is restarted_lane.account
        refused = await restarted_lane.start_refusal()
        assert refused is not None
        # #2670's one owner sentence; Start re-checks the mark before saying
        # it, and while the other copy still holds the account it holds.
        assert str(refused) == (
            "This Dry Run's simulated account is still open in another running copy of this Clerk."
        )
        assert refused.detail == "Stop that copy, then start this bot again."
        for internal in ("lease", "sim:", "live process"):
            assert internal not in f"{refused} {refused.detail}"
    finally:
        await restarted_lane.registry.stop_dry_run_restoration()
        holder.close()


async def test_boot_waits_out_its_dead_predecessor_s_lease_at_production_cadence(
    restarted_lane: _Lane,
) -> None:
    """A quick restart: the dead process's lease lapses, and the Dry Run is restored within a poll.

    Polled at the production interval, never slept a whole lease lifetime:
    a lease lapsing in 1.5 s costs about that, not 30 s.
    """
    # The dead process renewed its lease a moment before it died; nothing renews it now.
    predecessor = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=1_500)
    started_ms = now_ms_utc()
    try:
        await restarted_lane.registry.start_dry_run_restoration()
        elapsed_ms = now_ms_utc() - started_ms

        assert get_clerk_runtime(SIM_ACCOUNT) is not None
        assert 1_400 <= elapsed_ms < 5_000
        assert await restarted_lane.start_refusal() is None
    finally:
        # The restored authority lets go first; the dead handle's close only
        # tidies a connection that no longer holds anything.
        await close_synthetic_clerk_runtimes()
        predecessor.close()


async def test_every_dry_run_shares_one_lease_deadline(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Dry Runs held by a live peer cost one deadline, not one each."""
    second_sid = "live-dry-dv-qqq-0928"
    second = _dry_run_binding().model_copy(
        update={"strategy_instance_id": second_sid, "sealed_account_id": f"sim:{second_sid}"}
    )
    previous = BotTaskRegistry(restarted_lane.artifacts_root, feed_resolver=lambda: None, boot_recovery_required=False)
    previous._bindings.record_launch(second, launch_reason="deploy")
    deployed = previous._authority_for(second)
    await deployed.ensure_recoverable()
    await deployed.release_if_unused()
    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 1.5)
    holders = [
        _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000),
        ClerkSqliteRepository.open(
            account_id=f"sim:{second_sid}",
            artifacts_root=restarted_lane.artifacts_root,
            lease_owner="boot:another-process",
            lease_ttl_ms=60_000,
        ),
    ]
    started_ms = now_ms_utc()
    try:
        await restarted_lane.registry.start_dry_run_restoration()
    finally:
        for holder in holders:
            holder.close()

    assert now_ms_utc() - started_ms < 2_900


async def test_shutdown_during_a_dry_run_s_restoration_releases_its_account(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancels a restoration mid-opening; the account it opened is released, not leaked."""
    recovering = asyncio.Event()

    async def _recover_until_cancelled(_self: object) -> None:
        recovering.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SqliteAlpacaClerkFacade, "recover", _recover_until_cancelled)
    restarted_lane.registry.start_dry_run_restoration()
    await asyncio.wait_for(recovering.wait(), timeout=5.0)

    await restarted_lane.registry.stop_all()

    # Another process can take the account at once: no lease or heartbeat was left behind.
    _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000).close()


async def test_the_unrestored_dry_runs_panel_says_its_account_did_not_open(
    restarted_lane: _Lane,
) -> None:
    """While another process holds its account, the bot's own panel refuses rather than guessing."""
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        with pytest.raises(PanelUnavailableError) as refused:
            async with binding_clerk_runtime(restarted_lane.registry, _dry_run_binding()):
                pass
    finally:
        holder.close()

    assert refused.value.http_status == 503
    assert "This Dry Run's own simulated Clerk could not be opened." in str(refused.value.detail)


# ── #2668: the restoration runs under its bot's own operation lock ────────────


async def test_a_dry_run_whose_authority_cannot_be_built_is_its_own_failure(
    tmp_path: Path, booting: BotTaskRegistry, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building one Dry Run's authority once aborted the restoration of every later one (#2668).

    The authorities were built while the list was drawn, outside the per-bot
    boundary: one bot's unreadable ledger left every later Dry Run marked
    ``restoring`` until a restart, with Start telling the owner to wait for a
    restoration that was no longer running. Each bot's authority is now built
    inside its own boundary, so that failure refuses that bot alone.
    """
    for sid in ("live-dry-dv-a-0930", "live-dry-dv-b-0930"):
        binding = _dry_run_binding().model_copy(
            update={"strategy_instance_id": sid, "sealed_account_id": f"sim:{sid}", "run_id": f"run-{sid}"}
        )
        booting._bindings.record_launch(binding, launch_reason="deploy")
    authority_for = booting._authority_for

    def _authority_raises_for_one(binding: BrokerBotBinding) -> object:
        if binding.strategy_instance_id == "live-dry-dv-a-0930":
            raise RuntimeError("authority construction failed")
        return authority_for(binding)

    monkeypatch.setattr(booting, "_authority_for", _authority_raises_for_one)

    with caplog.at_level(logging.ERROR, logger=bot_runner.__name__):
        restoring = booting.start_dry_run_restoration()
        await asyncio.wait_for(restoring, timeout=10.0)

    (failed,) = _logged(caplog, "boot_dry_run_restoration_failed")
    assert failed.strategy_instance_id == "live-dry-dv-a-0930"
    assert failed.restoration == "not_restored"
    assert failed.exc_info is not None
    # The second Dry Run is restored as though nothing happened.
    assert get_clerk_runtime("sim:live-dry-dv-b-0930") is not None
    assert booting.dry_run_restoration_state("live-dry-dv-b-0930") is None


async def test_an_archive_of_a_bot_mid_restoration_waits_for_its_repair(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive holds the bot's operation lock, and the restoration now holds it too (#2668).

    The restoration's lease wait already holds the account's runtime lock,
    which Archive's custody read takes as well; its repair of the bot's runs
    does not. Without the operation lock, an Archive read the bot's custody
    while the restoration was still repairing its runs -- the two
    interleaving over one account. Archive now waits for the restoration to
    settle, and only then reads.
    """
    from app.broker.alpaca.clerk.sqlite.idempotency import UnknownStrategyInstanceError
    from app.services.bot_runner_errors import BotRunnerError

    registry = restarted_lane.registry
    repairing, release_repair = asyncio.Event(), asyncio.Event()

    async def _repair_until_released(_binding: BrokerBotBinding) -> tuple[str, ...]:
        repairing.set()
        await release_repair.wait()
        return ()

    # Bound when the restoration's list is drawn, so replaced before it starts.
    monkeypatch.setattr(registry, "_repair_restored_dry_run", _repair_until_released)
    restoring = registry.start_dry_run_restoration()
    await asyncio.wait_for(repairing.wait(), timeout=5.0)

    archive_custody = registry._archive_custody
    custody_read = asyncio.Event()

    def _archive_custody_spy(binding: BrokerBotBinding, reconciled: object) -> object:
        custody_read.set()
        return archive_custody(binding, reconciled)

    monkeypatch.setattr(registry, "_archive_custody", _archive_custody_spy)
    archiving = asyncio.create_task(registry.archive("alpaca", SID))
    # Archive's first step runs to its first wait. Nothing it does before its
    # custody read awaits, so an Archive the lock does not hold off has read
    # custody by the time this resumes.
    await asyncio.sleep(0)
    assert not custody_read.is_set(), "Archive read the bot's custody while its restoration was mid-repair"

    release_repair.set()
    # The fixture's bot never fully deployed, so Archive's own outcome is
    # one of its typed refusals; what is pinned here is the ordering.
    with contextlib.suppress(BotRunnerError, UnknownStrategyInstanceError):
        await asyncio.wait_for(archiving, timeout=10.0)
    await asyncio.wait_for(restoring, timeout=10.0)
    assert custody_read.is_set(), "Archive runs once the bot's restoration settles"
    assert registry.dry_run_restoration_state(SID) is None


async def test_a_panel_read_of_a_bot_still_being_restored_answers_at_once(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel once waited out the restoration's whole lease deadline (#2668).

    The restoration holds the account's runtime lock while it waits out a
    held lease, and the panel's read takes the same lock. A bot still being
    restored now answers at once, saying so, rather than blocking for the
    deadline.
    """
    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 2.0)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        restarted_lane.registry.start_dry_run_restoration()
        await asyncio.sleep(0.2)  # the restoration is now inside its lease wait

        started_ms = now_ms_utc()
        with pytest.raises(PanelUnavailableError) as refused:
            async with binding_clerk_runtime(restarted_lane.registry, _dry_run_binding()):
                pass

        assert now_ms_utc() - started_ms < 500, "a restoring bot's panel must not wait out the lease deadline"
        assert str(refused.value) == "This Dry Run is still being restored after the Clerk restarted."
        assert refused.value.http_status == 503
    finally:
        holder.close()


async def test_a_failed_startup_cancels_the_restoration_and_releases_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup step that raises after the restoration starts ends it too (#2668).

    The failed-startup cleanup closed the synthetic runtimes already
    registered, but a restoration still opening registers its runtime after
    that cleanup has run -- holding its account's lease in a process that
    never serves. The cleanup cancels and awaits the restoration first.
    """
    from app.broker_configuration.worker_lifecycle import close_failed_startup
    from app.services.bot_runner import set_bot_task_registry

    broker = _Broker()
    account_repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "account")
    account = SqliteAlpacaClerkFacade(repo=account_repo, read=broker, trade=broker, account_mode="paper")  # type: ignore[arg-type]
    previous = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    previous._bindings.record_launch(_dry_run_binding(), launch_reason="deploy")
    deployed = previous._authority_for(_dry_run_binding())
    await deployed.ensure_recoverable()
    await deployed.release_if_unused()
    assert get_clerk_runtime(SIM_ACCOUNT) is None
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=account, _sqlite_repository=account_repo)
    )
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None)
    set_bot_task_registry(registry)
    recovering = asyncio.Event()

    async def _recover_until_cancelled(_self: object) -> None:
        recovering.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SqliteAlpacaClerkFacade, "recover", _recover_until_cancelled)
    try:
        restoring = registry.start_dry_run_restoration()
        await asyncio.wait_for(recovering.wait(), timeout=5.0)

        await close_failed_startup()

        assert restoring.done()
        # Another process can take the account at once: no lease was left behind.
        _another_process_holds_the_dry_run(tmp_path, lease_ttl_ms=60_000).close()
    finally:
        # close_failed_startup already closed the account runtime and its
        # repository, as it does for a process that never serves.
        set_bot_task_registry(None)


# ── Unbound Dry Run orphans at boot (#2559) ─────────────────────────────────
# A Deploy that crashed between its budget commit and its binding record
# leaves a ``sim:`` authority only its own activation indexes. Boot finds
# those from the activation ledger synchronously, inside the lifespan, so no
# ledger or orphan may raise out of that enumeration (#2661 review, M2).


async def _crash_before_binding(artifacts_root: Path, sid: str) -> None:
    """A Deploy that activated its ``sim:`` account, then died before recording a binding."""
    binding = _dry_run_binding().model_copy(update={"strategy_instance_id": sid, "sealed_account_id": f"sim:{sid}"})
    deploying = BotTaskRegistry(artifacts_root, feed_resolver=lambda: None, boot_recovery_required=False)
    authority = deploying._authority_for(binding)
    await authority.ensure_recoverable()
    await authority.release_if_unused()
    assert get_clerk_runtime(f"sim:{sid}") is None


def _logged(caplog: pytest.LogCaptureFixture, action: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "action", None) == action]


async def test_an_unbound_dry_run_can_only_be_read_or_restored(tmp_path: Path) -> None:
    """#2661 review (M3): the orphan's read-only status is its type, not a flag
    chosen by ``isinstance``. It has no binding and no consent, so it offers a
    read that never recovers and boot's restoration -- nothing that admits,
    launches, projects a lifecycle or reconciles at run end."""
    await _crash_before_binding(tmp_path, "orphan-1")
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    try:
        orphan = registry.unbound_dry_run("orphan-1")

        assert orphan is not None
        assert {name for name in dir(orphan) if not name.startswith("_")} == {
            "account_id", "strategy_instance_id", "runtime_for_projection", "ensure_recoverable",
            "is_restoring", "restoring",
        }
        assert (orphan.account_id, orphan.strategy_instance_id) == ("sim:orphan-1", "orphan-1")
        assert registry.unbound_dry_run("never-deployed") is None
    finally:
        await registry.stop_all()


@pytest.fixture
async def booting(tmp_path: Path) -> AsyncIterator[BotTaskRegistry]:
    """The restarted process's runner, before its Dry Run restoration starts."""
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None)
    try:
        yield registry
    finally:
        await registry.stop_all()
        await close_synthetic_clerk_runtimes()


async def test_an_out_of_order_activation_ledger_never_fails_the_lanes_boot(
    tmp_path: Path, booting: BotTaskRegistry, caplog: pytest.LogCaptureFixture,
) -> None:
    """The review's reproduction: one ``sim:`` account at generations 2 then 1.

    The ledger lists, but every read of that account's latest activation
    rejects it. Boot once re-read it synchronously and the rejection aborted
    the lifespan, taking the real-money lane down with it. Now that orphan's
    own restoration fails, logged, and boot carries on.
    """
    store = SyntheticActivationStore(tmp_path)
    for generation in (2, 1):
        record = SyntheticActivationRecord.create(
            account_id="sim:orphan-1", authority_generation=generation, db_identity_token="db-token", activated_at_ms=0,
        )
        append_canonical_jsonl_line(
            store.path, asdict(record), invalid=SyntheticActivationInvalid, label="synthetic activation",
        )
    assert store.account_ids() == ("sim:orphan-1",)
    with pytest.raises(SyntheticActivationInvalid):
        store.latest("sim:orphan-1")

    with caplog.at_level(logging.ERROR, logger=bot_runner.__name__):
        restoring = booting.start_dry_run_restoration()
        await asyncio.wait_for(restoring, timeout=10.0)

    (failed,) = _logged(caplog, "boot_dry_run_restoration_failed")
    assert failed.strategy_instance_id == "orphan-1"
    assert failed.restoration == "not_restored"
    assert failed.exc_info is not None
    assert get_clerk_runtime("sim:orphan-1") is None


async def test_an_unreadable_activation_ledger_is_one_logged_skip(
    tmp_path: Path, booting: BotTaskRegistry, caplog: pytest.LogCaptureFixture,
) -> None:
    store = SyntheticActivationStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("this is not a sealed activation row\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger=bot_runner.__name__):
        restoring = booting.start_dry_run_restoration()
        await asyncio.wait_for(restoring, timeout=10.0)

    (unreadable,) = _logged(caplog, "boot_dry_run_activations_unreadable")
    assert unreadable.exc_info is not None
    assert _logged(caplog, "boot_dry_run_restoration_failed") == []


async def test_an_orphan_whose_binding_cannot_be_read_is_skipped_and_its_siblings_restored(
    tmp_path: Path, booting: BotTaskRegistry, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _crash_before_binding(tmp_path, "orphan-unmatched")
    await _crash_before_binding(tmp_path, "orphan-ok")
    read_binding = booting._read_binding

    def _unreadable_for_one(sid: str) -> BrokerBotBinding | None:
        if sid == "orphan-unmatched":
            raise OSError("binding file unreadable")
        return read_binding(sid)

    monkeypatch.setattr(booting, "_read_binding", _unreadable_for_one)

    with caplog.at_level(logging.ERROR, logger=bot_runner.__name__):
        restoring = booting.start_dry_run_restoration()
        await asyncio.wait_for(restoring, timeout=10.0)

    (unmatched,) = _logged(caplog, "boot_dry_run_activation_unmatched")
    assert unmatched.account_id == "sim:orphan-unmatched"
    assert unmatched.exc_info is not None
    # Skipped, not opened; the sibling is restored as though nothing happened.
    assert get_clerk_runtime("sim:orphan-unmatched") is None
    assert get_clerk_runtime("sim:orphan-ok") is not None
    assert _logged(caplog, "boot_dry_run_restoration_failed") == []


async def test_an_orphan_whose_recovery_raises_is_its_own_failure(
    tmp_path: Path, booting: BotTaskRegistry, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _crash_before_binding(tmp_path, "orphan-broken")
    await _crash_before_binding(tmp_path, "orphan-ok")
    recover = SqliteAlpacaClerkFacade.recover

    async def _recovery_raises_for_one(facade: SqliteAlpacaClerkFacade) -> None:
        if facade.account_id == "sim:orphan-broken":
            raise RuntimeError("startup recovery failed")
        await recover(facade)

    monkeypatch.setattr(SqliteAlpacaClerkFacade, "recover", _recovery_raises_for_one)

    with caplog.at_level(logging.ERROR, logger=bot_runner.__name__):
        restoring = booting.start_dry_run_restoration()
        await asyncio.wait_for(restoring, timeout=10.0)

    (failed,) = _logged(caplog, "boot_dry_run_restoration_failed")
    assert failed.strategy_instance_id == "orphan-broken"
    assert failed.restoration == "not_restored"
    assert "startup recovery failed" in failed.error_detail
    assert failed.exc_info is not None
    assert get_clerk_runtime("sim:orphan-broken") is None
    assert get_clerk_runtime("sim:orphan-ok") is not None


async def test_a_lease_held_dry_run_panel_shows_only_the_owner_sentence(
    restarted_lane: _Lane,
) -> None:
    """#2670: a Dry Run whose account another live process holds refuses with the
    one owner sentence on every surface -- never the internal ``sim:`` id,
    never the words "execution lease" the repository's exception carries."""
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        with pytest.raises(PanelUnavailableError) as refused:
            async with binding_clerk_runtime(restarted_lane.registry, _dry_run_binding()):
                pass

        sentence = f"{refused.value.detail}"
        assert "still open in another running copy of this Clerk" in sentence
        assert "Stop that copy" in sentence
        assert "sim:" not in sentence
        assert "execution lease" not in sentence
    finally:
        holder.close()


async def test_start_re_checks_a_held_dry_run_account_and_agrees_with_the_panel(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2670, owner decision 2026-09-30: Start re-checks an ``account_held`` Dry Run.

    While the other copy holds the account, Start answers the one owner
    sentence at once -- never the boot lease deadline -- and the panel's own
    refusal names the same state in the same words. Once the other copy lets
    go, Start itself completes the restoration, the mark clears, and Start no
    longer says it: no restart, ever."""
    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 0.3)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        restarted_lane.registry.start_dry_run_restoration()
        for _ in range(50):
            if restarted_lane.registry.dry_run_restoration_state(SID) == "account_held":
                break
            await asyncio.sleep(0.05)
        assert restarted_lane.registry.dry_run_restoration_state(SID) == "account_held"

        first = await restarted_lane.start_refusal()
        assert first is not None
        assert "still open in another running copy of this Clerk" in str(first)
        assert "sim:" not in str(first)
        assert "execution lease" not in (first.detail or "")

        started_ms = now_ms_utc()
        second = await restarted_lane.start_refusal()
        assert now_ms_utc() - started_ms < 500, "a still-held answer must not wait the lease deadline"
        assert str(second) == str(first)
    finally:
        await restarted_lane.registry.stop_dry_run_restoration()
        holder.close()

    third = await restarted_lane.start_refusal()
    assert restarted_lane.registry.dry_run_restoration_state(SID) is None, "Start's re-check restored the account"
    assert third is None or "another running copy" not in str(third)


async def test_the_lane_wide_stop_never_waits_out_a_restoring_dry_run_s_lease(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2684: the lane-wide Stop answers at once for a bot still being restored.

    The restoration holds the bot's operation lock for as long as its lease
    wait lasts; the lane's Stop used to queue behind it, so one Dry Run's
    restoration held every Deploy and the lane's Stop. It is refused at once
    with its own words now -- the restoration records the bot's stopped
    intent when it settles."""
    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 60.0)
    restarted_lane.registry._desired_repo(SID).set(
        DesiredState.RUNNING, updated_by="test", now_ms=now_ms_utc(), reason="deploy"
    )
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=3_600_000)
    try:
        restarted_lane.registry.start_dry_run_restoration()
        await asyncio.sleep(0.2)  # the restoration now holds the lock inside its lease wait

        started_ms = now_ms_utc()
        outcome = await asyncio.wait_for(
            restarted_lane.registry.stop_every_running_bot(updated_by="operator", reason="lane stop"),
            timeout=1.0,
        )
        assert now_ms_utc() - started_ms < 1_000
        assert not outcome.still_running
        (refusal,) = outcome.refused
        assert refusal.strategy_instance_id == SID
        assert "still being restored" in refusal.message
    finally:
        await restarted_lane.registry.stop_dry_run_restoration()
        holder.close()


async def test_every_account_reader_is_answered_at_once_during_a_restoration(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2684's one account-layer check: while boot's restoration holds the
    account, every opening is refused at once -- the replay receipt's
    repository read, the fees read, any reader -- instead of queueing behind
    the restoration's lease wait."""
    from app.services.bot_binding_authority import SyntheticAccountRestoring

    monkeypatch.setattr(bot_runner, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 60.0)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=3_600_000)
    try:
        restarted_lane.registry.start_dry_run_restoration()
        await asyncio.sleep(0.2)  # the restoration now holds the account inside its lease wait

        authority = restarted_lane.registry._authority_for(_dry_run_binding())
        started_ms = now_ms_utc()
        with pytest.raises(SyntheticAccountRestoring):
            await asyncio.wait_for(authority.runtime_for_projection().__aenter__(), timeout=1.0)
        assert now_ms_utc() - started_ms < 1_000
    finally:
        await restarted_lane.registry.stop_dry_run_restoration()
        holder.close()
