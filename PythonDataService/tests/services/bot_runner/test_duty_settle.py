"""The reconciliation sweep settles a dead bot's run, no restart (#2589).

A dead bot whose last run never settled used to wait for a Clerk restart
(boot recovery) or a lease revival before Clear would take it; one sealed on
a ``shadow:`` store after its live account graduated was never settled at
all. Now, after every sweep pass, the runner re-reads its own bots and gives
each one whose runner is gone -- and whose run its Clerk has closed -- the boot
scan's own repair. Level-triggered: whichever reconcile closed the run, and
however often a settle failed, the next pass finds it again. A shadow-sealed
bot is settled through its own store, never the installed authority (ADR
0050).

Once settled, such a bot is cleared on what that same store's records show it
holds (#2694): nothing, and Clear retires it there; anything, and Clear
refuses and names it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import get_alpaca_clerk, set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    activate_shadow_clerk_authority,
    select_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.run_ownership import NO_RUNNER_REASON
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    SynthesizedOrderRecord,
)
from app.services.bot_binding_repository import alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector
from app.services.bot_runner import BotTaskRegistry
from app.services.bot_runner_errors import BotRunnerError
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import admission_guard_for
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests._helpers.session_clock import pin_wall_clock_in_session
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_leg, _broker_order_fixture, _FakeTradePort
from tests.broker.alpaca.clerk.test_active_authority import _LiveBroker
from tests.broker.alpaca.clerk.test_shadow_broker import _LiveRead, _retain
from tests.services.test_bot_runner_ema_resume import (
    _STRATEGY_INSTANCE_ID,
    _first_resumed_bar,
    _FlatBroker,
    _ResumeFeed,
    _tradable_market_liveness,
    _wait_for,
)

_SID = _STRATEGY_INSTANCE_ID
_LIVE_ACCOUNT_ID = "9LIVE0001"
_SEALED_ACCOUNT_ID = f"shadow:{_LIVE_ACCOUNT_ID}"


def _account(
    account_id: str, clerk_root: Path
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker]:
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=clerk_root)
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    return repo, clerk, broker


def _compose(
    tmp_path: Path, feed: _ResumeFeed, *, account_id: str, clerk_root: Path
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    repo, clerk, broker = _account(account_id, clerk_root)
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repo, clerk, broker, registry


def _sweep(
    repo: ClerkSqliteRepository,
    clerk: SqliteAlpacaClerkFacade,
    broker: _FlatBroker,
    registry: BotTaskRegistry,
    *,
    passes: int = 1,
) -> ReconciliationSweep:
    """The account's sweep, wired to the runner the way ``main.py`` wires it."""
    return ReconciliationSweep(
        repo=repo,
        read=broker,
        trade=broker,
        intake=clerk.intake,
        run_ownership=clerk.run_ownership,
        max_passes=passes,
        sleep=lambda _delay: asyncio.sleep(0),
        pricing=UNPRICEABLE_RECOVERY,
        on_duty_settle=registry.settle_dead_runs,
    )


def _current_clerk_guard():
    """A start guard that resolves the *installed* Clerk at call time.

    The production default reads the installed authority lazily; a guard
    closed over one Clerk would pin the rehearsal authority past its
    graduation.
    """

    @asynccontextmanager
    async def guard(sid: str) -> AsyncIterator[object]:
        clerk = get_alpaca_clerk()
        assert clerk is not None
        async with clerk.start_admission_snapshot(sid) as snapshot:
            yield snapshot, clerk.program_leg_policy, clerk.exit_terms_for_instance(sid)

    return guard


async def _deploy(registry: BotTaskRegistry) -> None:
    await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
    )


async def _crash_without_settling(
    monkeypatch: pytest.MonkeyPatch, clerk: SqliteAlpacaClerkFacade, registry: BotTaskRegistry
) -> None:
    """End the supervise task with its terminal commit failing: the run stays
    ACTIVE in the Clerk and the lifecycle record stays ON_DUTY."""

    async def _stop_commit_fails(**_kwargs: object) -> None:
        raise RuntimeError("SQLite STOP commit failed")

    monkeypatch.setattr(clerk, "stop_strategy_run", _stop_commit_fails)
    await _deploy(registry)
    await _wait_for(lambda: not registry.any_running())
    # Let the task's done-callbacks run.
    await asyncio.sleep(0)
    assert registry.status("alpaca", _SID).phase == "ON_DUTY"


def _lifecycle(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "runner" / "live_state" / _SID / "lifecycle_state.json").read_text(encoding="utf-8")
    )


def _assert_settled_as_runner_gone(tmp_path: Path) -> None:
    record = _lifecycle(tmp_path)
    assert record["phase"] == "OFF_DUTY"
    assert record["duty_outcome"]["kind"] == "EXITED_UNVERIFIED"
    assert record["duty_outcome"]["reason_code"] == "INTERRUPTED_BY_RUNNER_GONE"


def _dead_bot(
    tmp_path: Path,
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, _FlatBroker, BotTaskRegistry]:
    """A paper account and a bot whose feed dies right after its first bar."""
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))
    return _compose(tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk")


@pytest.mark.asyncio
async def test_one_sweep_pass_settles_a_dead_bot_so_clear_needs_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass retires the runner-less run (#2369) and the runner settles its
    duty record right after: OFF_DUTY with interrupted evidence, the desired
    state STOPPED, and Clear takes the bot off the roster. No restart
    happened; the registry never left this process."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        assert repo.active_run(_SID) is not None

        await _sweep(repo, clerk, broker, registry).run()

        assert repo.active_run(_SID) is None
        _assert_settled_as_runner_gone(tmp_path)
        assert registry.desired_state(_SID).value == "STOPPED"

        await registry.archive("alpaca", _SID, updated_by="operator")

        assert registry.status("alpaca", _SID).phase == "RETIRED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_run_another_reconcile_closed_is_settled_by_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clear first (P1-1): Clear's own batch pass -- like Start's admission and
    Reconcile now -- also retires a run whose runner is gone, before any sweep
    pass sees it. The bot is refused as not yet settled, the next pass settles
    it anyway, and Clear then takes it with no restart."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        await clerk.reconcile_through()  # Clear's batch pass comes first
        assert repo.active_run(_SID) is None
        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "BOT_DUTY_NOT_SETTLED"

        await _sweep(repo, clerk, broker, registry).run()

        _assert_settled_as_runner_gone(tmp_path)
        await registry.archive("alpaca", _SID, updated_by="operator")
        assert registry.status("alpaca", _SID).phase == "RETIRED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_settle_that_failed_is_tried_again_on_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2: the first settle's write fails; nothing remembers that, and nothing
    has to -- the next pass reads the same on-duty record and settles it."""
    repo, clerk, broker, registry = _dead_bot(tmp_path)
    real_project_terminal = AlpacaLifecycleProjector.project_terminal
    attempts = {"settle": 0}

    def _first_write_fails(self: AlpacaLifecycleProjector, **kwargs: object):
        if kwargs.get("strategy_instance_id") == _SID and kwargs.get("updated_by") == "bot_runner_duty_settle":
            attempts["settle"] += 1
            if attempts["settle"] == 1:
                raise OSError("transient lifecycle write failure")
        return real_project_terminal(self, **kwargs)

    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
        monkeypatch.setattr(AlpacaLifecycleProjector, "project_terminal", _first_write_fails)

        await _sweep(repo, clerk, broker, registry, passes=2).run()

        assert attempts["settle"] == 2
        _assert_settled_as_runner_gone(tmp_path)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_running_bot_is_never_settled_even_when_its_clerk_closed_the_run(
    tmp_path: Path,
) -> None:
    """P1-4: the one thing standing between this bot and a settle is its live
    task. Its Clerk already retired the run (``NO_RUNNER``), so nothing in the
    Clerk would refuse a terminal projection; the runner's own liveness must.
    The duty record stays ON_DUTY with no outcome."""
    feed = _ResumeFeed()  # no bars: the stream stays open, the bot keeps running
    repo, clerk, broker, registry = _compose(
        tmp_path, feed, account_id="PA-TEST", clerk_root=tmp_path / "clerk"
    )
    set_alpaca_clerk(clerk)
    try:
        await _deploy(registry)
        run = repo.active_run(_SID)
        assert run is not None
        submit_stop_run(
            repo,
            account_id="PA-TEST",
            strategy_instance_id=_SID,
            lifecycle_run_id=run.lifecycle_run_id,
            operator_reason=NO_RUNNER_REASON,
            clock=repo.clock,
        )
        assert repo.active_run(_SID) is None

        await _sweep(repo, clerk, broker, registry).run()

        assert registry.status("alpaca", _SID).running is True
        record = _lifecycle(tmp_path)
        assert (record["phase"], record["duty_outcome"]) == ("ON_DUTY", None)
    finally:
        managed = registry._bots.get(_SID)
        if managed is not None:
            managed.task.cancel()
            await asyncio.gather(managed.task, return_exceptions=True)
        set_alpaca_clerk(None)
        repo.close()


# ── a rehearsal on a live account's Shadow world, then its graduation ───────

#: What a rehearsal leaves in its ``shadow:`` store before the account goes
#: live: given the store, the dead bot's run (still ACTIVE there) and the
#: lane's artifacts root.
_RehearsalSeed = Callable[[ClerkSqliteRepository, str, Path], Awaitable[None]]


@contextmanager
def _sealed_store(runner_root: Path) -> Iterator[ClerkSqliteRepository]:
    """The rehearsal's own store, open under its lease for one look or one seed."""
    sealed = ClerkSqliteRepository.open(account_id=_SEALED_ACCOUNT_ID, artifacts_root=runner_root)
    try:
        yield sealed
    finally:
        sealed.close()


async def _rehearse_until_the_bot_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *seeds: _RehearsalSeed
) -> BotTaskRegistry:
    """A live account's rehearsal: the shadow store is the installed authority,
    so the run is admitted into it and the binding is sealed on it. The bot's
    task then dies with its run still ACTIVE there, and each of ``seeds``
    leaves its mark on the store before the account graduates."""
    runner_root = tmp_path / "runner"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=RuntimeError("feed blew up"))
    await activate_shadow_clerk_authority(live_account_id=_LIVE_ACCOUNT_ID, artifacts_root=runner_root)
    runtime = await select_active_clerk_runtime(
        read=_LiveBroker(),
        trade=_LiveBroker(),
        artifacts_root=runner_root,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    assert runtime.clerk is not None
    registry = BotTaskRegistry(
        runner_root,
        feed_resolver=lambda: feed,
        boot_recovery_required=False,
        start_custody_guard=_current_clerk_guard(),
        market_liveness=_tradable_market_liveness,
    )
    set_alpaca_clerk(runtime.clerk)
    try:
        await _crash_without_settling(monkeypatch, runtime.clerk, registry)
        assert runtime.sqlite_repository.active_run(_SID) is not None
    finally:
        set_alpaca_clerk(None)
        await runtime.close()
    assert registry.binding_for_control("alpaca", _SID).sealed_account_id == _SEALED_ACCOUNT_ID
    with _sealed_store(runner_root) as sealed:
        run = sealed.active_run(_SID)
        assert run is not None
        for seed in seeds:
            await seed(sealed, run.lifecycle_run_id, runner_root)
    return registry


def _traded(*purposes: EffectPurpose) -> _RehearsalSeed:
    """The Clerk's own ENTER (and EXIT) of 1 SPY under the bot's run, each
    filled by the Shadow broker from its bound decision bar."""

    async def seed(sealed: ClerkSqliteRepository, run_id: str, runner_root: Path) -> None:
        ports = compose_shadow_ports(
            live_read=_LiveRead(), live_account_id=_LIVE_ACCOUNT_ID, artifacts_root=runner_root
        )
        facade = SqliteAlpacaClerkFacade(
            repo=sealed,
            read=ports.read,
            trade=ports.trade,
            authority_kind="shadow",
            account_mode="live",
            program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
        )
        evidence = SourceBarLedger(
            artifacts_root=runner_root, account_id=shadow_evidence_account_id_for_strategy(_SID)
        )
        try:
            for index, purpose in enumerate(purposes):
                receipt = await facade.execute_for_instance(
                    strategy_instance_id=_SID,
                    run_id=run_id,
                    decision_id=f"decision-{index}",
                    purpose=purpose,
                    action_plan=alpaca_v1_action_plan("SPY"),
                    quantity=1,
                    use_rth=True,
                    retained_source_bar=_retain(evidence, minute=600 + index, close="100.25"),
                )
                assert receipt.state in {"submitted", "flat"}, receipt.explanation
        finally:
            evidence.close()

    return seed


async def _a_working_order(sealed: ClerkSqliteRepository, run_id: str, _runner_root: Path) -> None:
    """An entry the broker accepted and never filled."""
    await submit_enter(
        sealed,
        account_id=_SEALED_ACCOUNT_ID,
        strategy_instance_id=_SID,
        decision_id="enter-working",
        lifecycle_run_id=run_id,
        leg=_broker_leg(quantity=1),
        trade=_FakeTradePort(),
    )


async def _an_account_wide_custody_problem(
    sealed: ClerkSqliteRepository, _run_id: str, _runner_root: Path
) -> None:
    """A hold on the whole Shadow account: an order nobody could explain."""
    raise_account_hold(sealed, reason_code="UNEXPLAINED_ORDER_HOLD", evidence_refs=["bo-1"])


async def _an_open_order_in_the_shadow_book(
    _sealed: ClerkSqliteRepository, _run_id: str, runner_root: Path
) -> None:
    """A resting simulated order the Clerk's own records never took in."""
    path = runner_root / "accounts" / "alpaca" / _SEALED_ACCOUNT_ID / SYNTHESIZED_ORDER_LEDGER_FILENAME
    resting = SynthesizedOrderRecord(
        seq=1, order=_broker_order_fixture(f"learn-ai/{_SID}/v1:resting", status="new")
    )
    path.write_text(resting.model_dump_json() + "\n", encoding="utf-8")


@dataclass(frozen=True)
class _Graduated:
    """The lane after graduation: the installed authority is the live account's own."""

    repo: ClerkSqliteRepository
    clerk: SqliteAlpacaClerkFacade
    broker: _FlatBroker
    #: Every lifecycle write the installed authority's projector was asked for.
    installed_writes: list[str]

    def sweep(self, registry: BotTaskRegistry, *, passes: int = 1) -> ReconciliationSweep:
        return _sweep(self.repo, self.clerk, self.broker, registry, passes=passes)


@asynccontextmanager
async def _graduated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: BotTaskRegistry
) -> AsyncIterator[_Graduated]:
    repo, clerk, broker = _account("PA-LIVE-2", tmp_path / "clerk-live")
    installed_writes: list[str] = []
    for method in ("project_terminal", "refresh", "retire"):
        real = getattr(registry._lifecycle_projector, method)

        def _spy(*args: object, _real=real, _method=method, **kwargs: object):
            installed_writes.append(f"{_method}:{kwargs.get('strategy_instance_id')}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(registry._lifecycle_projector, method, _spy)
    set_alpaca_clerk(clerk)
    try:
        yield _Graduated(repo=repo, clerk=clerk, broker=broker, installed_writes=installed_writes)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_shadow_sealed_dead_bot_is_settled_through_its_own_store_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-3: a rehearsal bot sealed on ``shadow:<live_account>`` after
    graduation. Its run lives in that store, which the installed live
    authority never reads -- and must never write duty state for (ADR 0050).
    The live account's own sweep passes settle it anyway: through the sealed
    store, whose dead run is closed there first."""
    registry = await _rehearse_until_the_bot_dies(tmp_path, monkeypatch)

    async with _graduated(tmp_path, monkeypatch, registry) as live:
        assert registry.status("alpaca", _SID).phase == "ON_DUTY"

        await live.sweep(registry, passes=2).run()

        _assert_settled_as_runner_gone(tmp_path)
        assert live.installed_writes == []
        assert live.repo.strategy_instances() == []
        with _sealed_store(tmp_path / "runner") as sealed:
            assert sealed.active_run(_SID) is None


@pytest.mark.asyncio
async def test_a_rehearsal_bot_that_traded_and_ended_flat_is_cleared_on_its_own_shadow_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2694: a real rehearsal, graduation, then Clear. The bot entered and
    exited in the Shadow world, so its store holds a filled ENTER whose effect
    never left ``in_progress`` -- no fold ends a filled ENTER -- beside a flat
    position. The installed Clerk has no custody record for it; Clear proves
    it holds nothing from its own store and retires it there, with no restart
    and not one write through the installed authority (ADR 0050)."""
    pin_wall_clock_in_session(monkeypatch)
    registry = await _rehearse_until_the_bot_dies(
        tmp_path, monkeypatch, _traded(EffectPurpose.ENTER, EffectPurpose.EXIT)
    )

    async with _graduated(tmp_path, monkeypatch, registry) as live:
        await live.sweep(registry).run()
        _assert_settled_as_runner_gone(tmp_path)
        with _sealed_store(tmp_path / "runner") as sealed:
            effects = {
                effect.kind: effect.state
                for order in sealed.orders_for_strategy(_SID)
                if (effect := sealed.effect_operation(order.effect_operation_id)) is not None
            }
            assert effects == {"ENTER": "in_progress", "EXIT": "succeeded"}
            assert {order.broker_state for order in sealed.orders_for_strategy(_SID)} == {"filled"}
            assert sealed.attributed_positions_for_strategy(_SID) == {"SPY": 0.0}
            # The roster's coarse "can its custody still change" test would hold this bot for good.
            assert sealed.strategy_instances_with_live_custody() == {_SID}

        await registry.archive("alpaca", _SID, updated_by="operator")

        assert registry.status("alpaca", _SID).phase == "RETIRED"
        with _sealed_store(tmp_path / "runner") as sealed:
            assert sealed.strategy_instance(_SID)["retired_at_ms"] is not None
        assert live.installed_writes == []
        assert live.repo.strategy_instances() == []


@pytest.mark.parametrize(
    ("holding", "named"),
    [
        (_traded(EffectPurpose.ENTER), "1 SPY"),
        (_a_working_order, "1 working order"),
        (_an_account_wide_custody_problem, "1 unresolved custody problem"),
        (_an_open_order_in_the_shadow_book, "1 simulated order still open in the Shadow order book"),
    ],
    ids=["position", "working-order", "account-wide-problem", "open-book-order"],
)
@pytest.mark.asyncio
async def test_a_rehearsal_bot_whose_shadow_records_show_a_holding_is_refused_and_told_what(
    holding: _RehearsalSeed, named: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing reconciles a graduated Shadow store, so whatever its records
    show the bot holds, it holds for good: Clear refuses in a sentence that
    names it, and writes nothing in either store."""
    pin_wall_clock_in_session(monkeypatch)
    registry = await _rehearse_until_the_bot_dies(tmp_path, monkeypatch, holding)

    async with _graduated(tmp_path, monkeypatch, registry) as live:
        await live.sweep(registry).run()
        _assert_settled_as_runner_gone(tmp_path)

        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")

        assert refused.value.reason_code == "ARCHIVE_REHEARSAL_STILL_HOLDS"
        assert str(refused.value).startswith("This bot's Shadow records still show ")
        assert named in str(refused.value)
        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
        with _sealed_store(tmp_path / "runner") as sealed:
            assert sealed.strategy_instance(_SID)["retired_at_ms"] is None
        assert live.installed_writes == []


@pytest.mark.asyncio
async def test_a_rehearsal_bot_whose_shadow_store_another_process_holds_is_not_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's lease is taken in one attempt. Held elsewhere, its records
    cannot be read, and unread records prove nothing: Clear refuses by name."""
    registry = await _rehearse_until_the_bot_dies(tmp_path, monkeypatch)

    async with _graduated(tmp_path, monkeypatch, registry) as live:
        await live.sweep(registry).run()
        with _sealed_store(tmp_path / "runner"), pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")

        assert refused.value.reason_code == "ARCHIVE_REHEARSAL_RECORDS_UNAVAILABLE"
        assert str(refused.value) == "This bot's Shadow records could not be read."
        assert registry.status("alpaca", _SID).phase == "OFF_DUTY"
        assert live.installed_writes == []


@pytest.mark.asyncio
async def test_a_clear_waits_for_a_settle_that_has_the_same_shadow_store_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every rehearsal bot of an account is sealed on the one store. A sweep
    settling one of them holds its lease, and a Clear of another waits its
    turn in this process instead of being refused for a lease its own
    process holds."""
    registry = await _rehearse_until_the_bot_dies(tmp_path, monkeypatch)

    async with _graduated(tmp_path, monkeypatch, registry) as live:
        await live.sweep(registry).run()
        settling = registry._authorities.for_settle(
            registry.binding_for_control("alpaca", _SID), foreign_account_id=_SEALED_ACCOUNT_ID
        )
        async with settling.lifecycle_for_settle():
            clear = asyncio.create_task(registry.archive("alpaca", _SID, updated_by="operator"))
            await asyncio.sleep(0.05)
            assert not clear.done()
        await clear

        assert registry.status("alpaca", _SID).phase == "RETIRED"


@pytest.mark.asyncio
async def test_a_bot_sealed_on_another_real_account_is_left_alone_and_clear_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a ``shadow:`` store is opened for a foreign bot; any other account
    the installed Clerk does not hold has no authority here, so the bot's
    record is left exactly as it is -- and Clear refuses it for its account,
    not with a not-yet-settled promise."""
    repo, clerk, _broker, registry = _dead_bot(tmp_path)
    other_repo, other_clerk, other_broker = _account("PA-OTHER", tmp_path / "clerk-other")
    set_alpaca_clerk(clerk)
    try:
        await _crash_without_settling(monkeypatch, clerk, registry)
    finally:
        set_alpaca_clerk(None)
    set_alpaca_clerk(other_clerk)
    try:
        await _sweep(other_repo, other_clerk, other_broker, registry).run()

        record = _lifecycle(tmp_path)
        assert (record["phase"], record["duty_outcome"]) == ("ON_DUTY", None)
        with pytest.raises(BotRunnerError) as refused:
            await registry.archive("alpaca", _SID, updated_by="operator")
        assert refused.value.reason_code == "ARCHIVE_SEALED_ACCOUNT_CUSTODY"
    finally:
        set_alpaca_clerk(None)
        other_repo.close()
        repo.close()
